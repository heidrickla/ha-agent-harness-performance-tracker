"""The arithmetic: per-harness statistics, the baseline, and the regression gate.

Pure functions over plain dicts so the whole model is testable without Home
Assistant. A run is the dict the action, the webhook and the store all share;
see const.py for its fields.

The gate follows the preserve-and-extend contract from Salesforce's DarwinX
work: a harness version earns trust only after enough runs (confirmation, not
one lucky rollout), the best confirmed version is the baseline, and the
current version regresses when its pass rate drops below the baseline by more
than a tolerance OR when a task the baseline solved now fails.

The aggregate half compares like with like: both rates are taken over only the
task ids both versions ran, because rates over different task mixes measure the
mix. With no shared task there is no comparison, and when the model differs
between the two sets the aggregate half stays off, since the drop would not be
the harness's.

A harness edited several times a day never gives one version enough runs to be
confirmed (one agent: 60 versions in ten days, a median of two runs each), so
the window gate judges the last `window` runs against the `window` before them,
whatever versions they span, and names the versions inside. It regresses when
the pass rate over shared tasks drops by more than the noise floor, or denials
per 100 tool calls rise by DENIALS_RISE or more.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from statistics import median
from typing import Any

from .const import (
    DEFAULT_WINDOW,
    FIELD_COST,
    FIELD_DENIAL_CLASSES,
    FIELD_DENIALS,
    FIELD_DURATION,
    FIELD_HARNESS,
    FIELD_INTERVENTIONS,
    FIELD_MODEL,
    FIELD_OUTCOME,
    FIELD_RECORDED_AT,
    FIELD_TASK_ID,
    FIELD_TOOL_CALLS,
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
    tool_calls: int = 0
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

    @property
    def denials_per_100_calls(self) -> float | None:
        """A refusal happens per action, so this rate does not grow with run size."""
        if not self.tool_calls:
            return None
        return round(100.0 * self.denials / self.tool_calls, 2)

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
            "denials_per_100_calls": self.denials_per_100_calls,
            "cost_usd": round(self.cost, 4),
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
        }


def _add(s: HarnessStats, run: dict[str, Any]) -> None:
    s.runs += 1
    s.passes += run.get(FIELD_OUTCOME) == OUTCOME_PASS
    s.verified += bool(run.get(FIELD_VERIFIED))
    if run.get(FIELD_TURNS) is not None:
        s.turns.append(int(run[FIELD_TURNS]))
    if run.get(FIELD_DURATION) is not None:
        s.durations.append(float(run[FIELD_DURATION]))
    s.interventions += int(run.get(FIELD_INTERVENTIONS) or 0)
    s.denials += int(run.get(FIELD_DENIALS) or 0)
    s.tool_calls += int(run.get(FIELD_TOOL_CALLS) or 0)
    s.cost += float(run.get(FIELD_COST) or 0.0)
    s.last_seen = str(run.get(FIELD_RECORDED_AT, ""))


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
        _add(s, run)
    return stats


def aggregate(runs: list[dict[str, Any]], label: str) -> HarnessStats:
    """One set of statistics over any runs, whatever versions they used."""
    s = HarnessStats(
        version=label,
        first_seen=str(runs[0].get(FIELD_RECORDED_AT, "")) if runs else "",
    )
    for run in runs:
        _add(s, run)
    return s


def denial_classes(runs: list[dict[str, Any]]) -> Counter[str]:
    """Why runs were refused, summed: a classifier rule, a hook, or the person."""
    total: Counter[str] = Counter()
    for run in runs:
        for name, count in (run.get(FIELD_DENIAL_CLASSES) or {}).items():
            total[str(name)] += int(count)
    return total


def versions_in(runs: list[dict[str, Any]]) -> list[tuple[str, int]]:
    """The harness versions these runs used, first-seen order, with run counts."""
    counts: Counter[str] = Counter(str(r[FIELD_HARNESS]) for r in runs)
    return [(v, counts[v]) for v in dict.fromkeys(str(r[FIELD_HARNESS]) for r in runs)]


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


def shared_rates(
    before: list[dict[str, Any]], after: list[dict[str, Any]]
) -> tuple[float, float, int] | None:
    """Pass rates of two run sets over only the task ids both ran, and how many."""

    def tasks(rs: list[dict[str, Any]]) -> set[str]:
        return {str(r[FIELD_TASK_ID]) for r in rs if r.get(FIELD_TASK_ID)}

    common = tasks(before) & tasks(after)
    if not common:
        return None

    def rate(rs: list[dict[str, Any]]) -> float:
        mine = [r for r in rs if str(r.get(FIELD_TASK_ID)) in common]
        passed = sum(1 for r in mine if r.get(FIELD_OUTCOME) == OUTCOME_PASS)
        return round(100.0 * passed / len(mine), 1)

    return rate(before), rate(after), len(common)


def matched_rates(
    runs: list[dict[str, Any]], baseline: str | None, current: str | None
) -> tuple[float, float, int] | None:
    """Baseline and current pass rates over only the tasks both ran, and how many."""
    if not baseline or not current or baseline == current:
        return None
    return shared_rates(
        [r for r in runs if str(r[FIELD_HARNESS]) == baseline],
        [r for r in runs if str(r[FIELD_HARNESS]) == current],
    )


def model_of(runs: list[dict[str, Any]]) -> str | None:
    """The model most of these runs reported; a tie goes to the first name."""
    counts = Counter(str(r[FIELD_MODEL]) for r in runs if r.get(FIELD_MODEL))
    if not counts:
        return None
    return min(counts, key=lambda m: (-counts[m], m))


def dominant_model(runs: list[dict[str, Any]], version: str | None) -> str | None:
    """The model most runs on this version reported; a tie goes to the first name."""
    if not version:
        return None
    return model_of([r for r in runs if str(r[FIELD_HARNESS]) == version])


# Denials per 100 tool calls the recent window may rise by before it counts as a
# regression, judged only over MIN_CALLS calls or more on both sides. Per run, one long
# span reads as a harness getting worse (0.3 to 1.5 per run was 0.44 to 0.70 per 100
# calls, one run holding 1768 of the window's 2154 calls).
DENIALS_RISE = 1.0
MIN_CALLS = 100
# A denial class seen this often in the recent window is recurring: by the harness rule,
# the second occurrence of a failure becomes a capability.
RECURRING = 2


@dataclass
class Window:
    """The last `size` runs judged against the `size` before them."""

    size: int
    recent: HarnessStats | None = None
    prior: HarnessStats | None = None
    improvement: float | None = None
    shared_tasks: int = 0
    model_changed: bool = False
    regressed: bool = False
    versions: list[tuple[str, int]] = field(default_factory=list)
    recurring: list[tuple[str, int]] = field(default_factory=list)


def window(runs: list[dict[str, Any]], size: int, tolerance: float) -> Window:
    """The window gate. Needs twice `size` runs before it compares anything.

    The pass-rate drop must exceed max(tolerance, 150 / size) points: with ten runs a
    window moves ten points per run, so a single partial is noise and two are a signal.
    """
    w = Window(size=size)
    if size < 1 or not runs:
        return w
    recent = runs[-size:]
    w.recent = aggregate(recent, f"last {size} runs")
    w.versions = versions_in(recent)
    w.recurring = [
        (name, n) for name, n in denial_classes(recent).most_common() if n >= RECURRING
    ]
    if len(runs) < 2 * size:
        return w
    prior = runs[-2 * size : -size]
    w.prior = aggregate(prior, f"the {size} runs before")
    shared = shared_rates(prior, recent)
    if shared:
        w.improvement = round(shared[1] - shared[0], 1)
        w.shared_tasks = shared[2]
    before, after = model_of(prior), model_of(recent)
    w.model_changed = bool(before and after and before != after)
    floor = max(abs(tolerance), 150.0 / size)
    dropped = w.improvement is not None and w.improvement < -floor
    rate_now = w.recent.denials_per_100_calls
    rate_before = w.prior.denials_per_100_calls
    more_denials = (
        rate_now is not None
        and rate_before is not None
        and min(w.recent.tool_calls, w.prior.tool_calls) >= MIN_CALLS
        and rate_now - rate_before >= DENIALS_RISE
    )
    w.regressed = not w.model_changed and (dropped or more_denials)
    return w


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
    comparable_tasks: int
    model_changed: bool
    regressed: bool
    confirmed: bool
    last_run: dict[str, Any] | None
    versions: dict[str, HarnessStats]
    window: Window


def snapshot(
    runs: list[dict[str, Any]],
    pinned: str | None,
    min_runs: int,
    tolerance: float,
    window_size: int = DEFAULT_WINDOW,
) -> Snapshot:
    """Compute both gates and every derived figure in one place."""
    stats = by_harness(runs)
    cur = current_version(runs)
    base = baseline_version(stats, pinned, min_runs)
    cur_stats = stats.get(cur) if cur else None
    base_stats = stats.get(base) if base else None
    regressed_tasks = regressions(runs, base, cur)
    matched = matched_rates(runs, base, cur)
    improvement = round(matched[1] - matched[0], 1) if matched else None
    base_model, cur_model = dominant_model(runs, base), dominant_model(runs, cur)
    model_changed = bool(base_model and cur_model and base_model != cur_model)
    confirmed = bool(cur_stats and cur_stats.runs >= min_runs)
    # Aggregate half of the gate needs the current version confirmed, or one
    # bad early run would flag every fresh harness. The per-task half fires
    # immediately: a task that used to pass and now fails is evidence on its own.
    aggregate_drop = bool(
        confirmed
        and not model_changed
        and improvement is not None
        and improvement < -abs(tolerance)
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
        comparable_tasks=matched[2] if matched else 0,
        model_changed=model_changed,
        regressed=regressed,
        confirmed=confirmed,
        last_run=runs[-1] if runs else None,
        versions=stats,
        window=window(runs, window_size, tolerance),
    )
