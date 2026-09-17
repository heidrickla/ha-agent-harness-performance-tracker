"""The arithmetic: per-harness statistics, the baseline, and the regression gate.

Pure functions over plain dicts so the whole model is testable without Home
Assistant. A run is the dict the action, the webhook and the store all share;
see const.py for its fields.

The gate follows the preserve-and-extend contract from Salesforce's DarwinX
work: a harness version earns trust only after enough runs (confirmation, not
one lucky rollout), the best confirmed version is the baseline, and the
current version regresses when its pass rate drops below the baseline by more
than a tolerance OR when a task the baseline solved now fails.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median
from typing import Any

from .const import (
    FIELD_COST,
    FIELD_DENIALS,
    FIELD_DURATION,
    FIELD_HARNESS,
    FIELD_INTERVENTIONS,
    FIELD_OUTCOME,
    FIELD_RECORDED_AT,
    FIELD_TASK_ID,
    FIELD_TURNS,
    FIELD_VERIFIED,
    OUTCOME_PASS,
)


@dataclass
class HarnessStats:
    """What one harness version has done so far."""

    version: str
    runs: int = 0
    passes: int = 0
    verified: int = 0
    turns: list[int] = field(default_factory=list)
    durations: list[float] = field(default_factory=list)
    interventions: int = 0
    denials: int = 0
    cost: float = 0.0
    first_seen: str = ""
    last_seen: str = ""

    @property
    def pass_rate(self) -> float:
        """Passes as a percentage of runs. Partial counts as not passed."""
        return round(100.0 * self.passes / self.runs, 1) if self.runs else 0.0

    @property
    def verified_rate(self) -> float:
        return round(100.0 * self.verified / self.runs, 1) if self.runs else 0.0

    @property
    def median_turns(self) -> float | None:
        return float(median(self.turns)) if self.turns else None

    @property
    def median_duration(self) -> float | None:
        return round(float(median(self.durations)), 1) if self.durations else None

    @property
    def interventions_per_run(self) -> float:
        return round(self.interventions / self.runs, 2) if self.runs else 0.0

    @property
    def denials_per_run(self) -> float:
        return round(self.denials / self.runs, 2) if self.runs else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "runs": self.runs,
            "pass_rate": self.pass_rate,
            "verified_rate": self.verified_rate,
            "median_turns": self.median_turns,
            "median_duration_s": self.median_duration,
            "interventions_per_run": self.interventions_per_run,
            "denials_per_run": self.denials_per_run,
            "cost_usd": round(self.cost, 4),
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
        }


def by_harness(runs: list[dict[str, Any]]) -> dict[str, HarnessStats]:
    """Statistics per harness version, in first-seen order."""
    stats: dict[str, HarnessStats] = {}
    for run in runs:
        version = str(run[FIELD_HARNESS])
        s = stats.get(version)
        if s is None:
            s = stats[version] = HarnessStats(
                version=version, first_seen=str(run.get(FIELD_RECORDED_AT, ""))
            )
        s.runs += 1
        s.passes += run.get(FIELD_OUTCOME) == OUTCOME_PASS
        s.verified += bool(run.get(FIELD_VERIFIED))
        if run.get(FIELD_TURNS) is not None:
            s.turns.append(int(run[FIELD_TURNS]))
        if run.get(FIELD_DURATION) is not None:
            s.durations.append(float(run[FIELD_DURATION]))
        s.interventions += int(run.get(FIELD_INTERVENTIONS) or 0)
        s.denials += int(run.get(FIELD_DENIALS) or 0)
        s.cost += float(run.get(FIELD_COST) or 0.0)
        s.last_seen = str(run.get(FIELD_RECORDED_AT, ""))
    return stats


def current_version(runs: list[dict[str, Any]]) -> str | None:
    """The harness the most recent run used."""
    return str(runs[-1][FIELD_HARNESS]) if runs else None


def baseline_version(
    stats: dict[str, HarnessStats], pinned: str | None, min_runs: int
) -> str | None:
    """The version the current one is measured against.

    A pinned version wins outright, so a human can hold the bar where they
    want it. Otherwise it is the best confirmed version by pass rate; ties go
    to the one seen most recently, so a newer equal harness becomes the bar
    rather than an older one lingering.
    """
    if pinned and pinned in stats:
        return pinned
    confirmed = [s for s in stats.values() if s.runs >= min_runs]
    if not confirmed:
        return None
    best = max(confirmed, key=lambda s: (s.pass_rate, s.last_seen))
    return best.version


def solved_tasks(runs: list[dict[str, Any]], version: str) -> set[str]:
    """Task ids that passed at least once on this version."""
    return {
        str(r[FIELD_TASK_ID])
        for r in runs
        if r.get(FIELD_TASK_ID)
        and str(r[FIELD_HARNESS]) == version
        and r.get(FIELD_OUTCOME) == OUTCOME_PASS
    }


def latest_outcome_by_task(runs: list[dict[str, Any]], version: str) -> dict[str, str]:
    """The most recent outcome per task id on this version."""
    latest: dict[str, str] = {}
    for r in runs:
        if r.get(FIELD_TASK_ID) and str(r[FIELD_HARNESS]) == version:
            latest[str(r[FIELD_TASK_ID])] = str(r.get(FIELD_OUTCOME))
    return latest


def regressions(
    runs: list[dict[str, Any]], baseline: str | None, current: str | None
) -> list[str]:
    """Tasks the baseline solved whose latest run on the current version failed.

    This is the per-task half of preserve-and-extend: a higher aggregate pass
    rate does not excuse breaking something that used to work.
    """
    if not baseline or not current or baseline == current:
        return []
    solved = solved_tasks(runs, baseline)
    latest = latest_outcome_by_task(runs, current)
    return sorted(
        t for t, outcome in latest.items() if t in solved and outcome != OUTCOME_PASS
    )


@dataclass
class Snapshot:
    """Everything the entities show, computed from the run list."""

    total_runs: int
    total_cost: float
    current: HarnessStats | None
    baseline: HarnessStats | None
    baseline_pinned: bool
    regressed_tasks: list[str]
    improvement: float | None
    regressed: bool
    confirmed: bool
    last_run: dict[str, Any] | None
    versions: dict[str, HarnessStats]


def snapshot(
    runs: list[dict[str, Any]], pinned: str | None, min_runs: int, tolerance: float
) -> Snapshot:
    """Compute the gate and every derived figure in one place."""
    stats = by_harness(runs)
    cur = current_version(runs)
    base = baseline_version(stats, pinned, min_runs)
    cur_stats = stats.get(cur) if cur else None
    base_stats = stats.get(base) if base else None
    regressed_tasks = regressions(runs, base, cur)
    improvement = None
    if cur_stats and base_stats and cur != base:
        improvement = round(cur_stats.pass_rate - base_stats.pass_rate, 1)
    confirmed = bool(cur_stats and cur_stats.runs >= min_runs)
    # Aggregate half of the gate needs the current version confirmed, or one
    # bad early run would flag every fresh harness. The per-task half fires
    # immediately: a task that used to pass and now fails is evidence on its own.
    aggregate_drop = bool(
        confirmed and improvement is not None and improvement < -abs(tolerance)
    )
    regressed = aggregate_drop or bool(regressed_tasks)
    return Snapshot(
        total_runs=len(runs),
        total_cost=round(sum(float(r.get(FIELD_COST) or 0.0) for r in runs), 4),
        current=cur_stats,
        baseline=base_stats,
        baseline_pinned=bool(pinned and pinned in stats),
        regressed_tasks=regressed_tasks,
        improvement=improvement,
        regressed=regressed,
        confirmed=confirmed,
        last_run=runs[-1] if runs else None,
        versions=stats,
    )
