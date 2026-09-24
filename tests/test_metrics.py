"""The arithmetic, without Home Assistant.

Loads metrics.py by path so a bare checkout can run this suite. The gate is
the thing that matters here, so each of its two halves is driven to both
outcomes and the threshold edges are pinned.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
PKG = os.path.join(ROOT, "custom_components", "agent_harness_performance_tracker")


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"agent_harness_performance_tracker.{name}", os.path.join(PKG, f"{name}.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# metrics imports `.const` relatively, so give it a package to be relative to.
_pkg = types.ModuleType("agent_harness_performance_tracker")
_pkg.__path__ = [PKG]
sys.modules["agent_harness_performance_tracker"] = _pkg
const = _load("const")
metrics = _load("metrics")


def run(harness, outcome="pass", task=None, **extra):
    r = {
        "harness_version": harness,
        "outcome": outcome,
        "recorded_at": extra.pop("at", ""),
    }
    if task:
        r["task_id"] = task
    r.update(extra)
    return r


def test_by_harness_counts_passes_and_medians():
    runs = [
        run("v1", turns=10, duration_s=100.0, interventions=1, cost_usd=0.5, at="a"),
        run("v1", "fail", turns=30, duration_s=300.0, denials=2, at="b"),
        run("v1", "partial", at="c"),
    ]
    s = metrics.by_harness(runs)["v1"]
    assert s.runs == 3
    assert s.passes == 1
    assert s.pass_rate == 33.3
    assert s.median_turns == 20.0
    assert s.median_duration == 200.0
    assert s.interventions_per_run == 0.33
    assert s.denials_per_run == 0.67
    assert s.cost == 0.5
    assert s.first_seen == "a" and s.last_seen == "c"


def test_empty_stats_are_zero_not_errors():
    s = metrics.HarnessStats(version="x")
    assert s.pass_rate == 0.0
    assert s.median_turns is None
    assert s.median_duration is None
    assert s.as_dict()["runs"] == 0


def test_baseline_needs_confirmation():
    runs = [run("v1")] * 3 + [run("v2", "fail")] * 2
    stats = metrics.by_harness(runs)
    assert metrics.baseline_version(stats, None, min_runs=5) is None
    assert metrics.baseline_version(stats, None, min_runs=3) == "v1"


def test_baseline_prefers_higher_rate_then_recency():
    runs = [run("old", at="1")] * 4 + [run("new", at="2")] * 4
    stats = metrics.by_harness(runs)
    # Equal rates: the more recently seen version wins.
    assert metrics.baseline_version(stats, None, 4) == "new"
    runs2 = [*runs, run("new", "fail", at="3")]
    stats2 = metrics.by_harness(runs2)
    assert metrics.baseline_version(stats2, None, 4) == "old"


def test_pinned_baseline_wins_even_if_unconfirmed():
    runs = [run("v1")] * 5 + [run("v2", "fail")]
    stats = metrics.by_harness(runs)
    assert metrics.baseline_version(stats, "v2", 5) == "v2"
    # A pin naming a version with no runs is ignored rather than trusted.
    assert metrics.baseline_version(stats, "ghost", 5) == "v1"


def test_regressions_are_tasks_the_baseline_solved_that_now_fail():
    runs = [
        run("v1", task="a"),
        run("v1", task="b"),
        run("v1", "fail", task="c"),
        run("v2", "fail", task="a"),  # regressed
        run("v2", task="b"),  # kept
        run("v2", task="c"),  # newly solved, not a regression
        run("v2", "fail", task="d"),  # never solved before, not a regression
    ]
    assert metrics.regressions(runs, "v1", "v2") == ["a"]
    assert metrics.regressions(runs, "v1", "v1") == []
    assert metrics.regressions(runs, None, "v2") == []


def test_regression_uses_latest_outcome_per_task():
    runs = [run("v1", task="a"), run("v2", "fail", task="a"), run("v2", task="a")]
    assert metrics.regressions(runs, "v1", "v2") == []


def test_snapshot_aggregate_gate_needs_confirmation_and_tolerance():
    # Each v2 block ends on a pass, so the per-task half stays off and only the
    # aggregate half is under test.
    base = [run("v1", task="t", at=str(i)) for i in range(10)]  # 100% baseline
    runs = [*base, run("v2", "fail", task="t", at="x"), run("v2", task="t")]
    snap = metrics.snapshot(runs, None, min_runs=10, tolerance=5.0)
    assert snap.baseline.version == "v1"
    assert snap.current.version == "v2"
    assert snap.improvement == -50.0
    assert snap.comparable_tasks == 1
    assert snap.confirmed is False
    assert snap.regressed is False  # two runs on a fresh harness are not a verdict

    runs = base + [run("v2", "fail", task="t")] * 9 + [run("v2", task="t")]
    snap = metrics.snapshot(runs, None, 10, 5.0)
    assert snap.confirmed is True
    assert snap.regressed_tasks == []
    assert snap.regressed is True


def test_snapshot_tolerance_edge():
    base = [run("v1", task="t", at=str(i)) for i in range(10)]
    runs = (
        base + [run("v2", "fail", task="t")] + [run("v2", task="t") for _ in range(19)]
    )
    assert (
        metrics.snapshot(runs, None, 10, 5.0).regressed is False
    )  # exactly -5.0 (95%)
    runs = (
        base
        + [run("v2", "fail", task="t")] * 2
        + [run("v2", task="t") for _ in range(19)]
    )
    assert metrics.snapshot(runs, None, 10, 5.0).regressed is True  # 90.5%


def test_snapshot_different_task_mixes_are_not_compared():
    # v1 only ran an easy task, v2 only a hard one: the rates differ by the mix.
    runs = [run("v1", task="easy", at=str(i)) for i in range(10)]
    runs += [run("v2", "fail", task="hard") for _ in range(10)]
    snap = metrics.snapshot(runs, None, 10, 5.0)
    assert snap.confirmed is True
    assert snap.comparable_tasks == 0
    assert snap.improvement is None
    assert snap.regressed is False


def test_snapshot_compares_only_the_shared_tasks():
    runs = [run("v1", task="t", at=str(i)) for i in range(10)]
    runs += [
        run("v1", "fail", task="only-v1") for _ in range(10)
    ]  # drags v1's raw rate to 50%
    runs += [run("v2", "fail", task="t")] * 5 + [run("v2", task="t")] * 5
    snap = metrics.snapshot(runs, None, 10, 5.0)
    assert snap.baseline.pass_rate == 50.0 and snap.current.pass_rate == 50.0
    assert snap.comparable_tasks == 1
    assert snap.improvement == -50.0  # 100% -> 50% on the task both ran
    assert snap.regressed is True


def test_snapshot_model_change_holds_the_aggregate_gate():
    runs = [run("v1", task="t", model="m1", at=str(i)) for i in range(10)]
    runs += [run("v2", "fail", task="t", model="m2")] * 9 + [
        run("v2", task="t", model="m2")
    ]
    snap = metrics.snapshot(runs, None, 10, 5.0)
    assert snap.model_changed is True
    assert snap.improvement == -90.0
    assert snap.regressed is False  # the drop cannot be laid at the harness's door
    same = [dict(r, model="m1") for r in runs]
    assert metrics.snapshot(same, None, 10, 5.0).regressed is True


def test_runs_without_a_task_id_are_not_compared():
    runs = [run("v1", at=str(i)) for i in range(10)] + [run("v2", "fail")] * 10
    snap = metrics.snapshot(runs, None, 10, 5.0)
    assert snap.improvement is None and snap.regressed is False


def test_snapshot_task_gate_fires_before_confirmation():
    runs = [run("v1", task="t") for _ in range(10)] + [run("v2", "fail", task="t")]
    snap = metrics.snapshot(runs, None, 10, 5.0)
    assert snap.confirmed is False
    assert snap.regressed_tasks == ["t"]
    assert snap.regressed is True


def test_snapshot_empty_and_single_version():
    empty = metrics.snapshot([], None, 10, 5.0)
    assert empty.total_runs == 0 and empty.current is None and empty.regressed is False
    one = metrics.snapshot([run("v1")] * 12, None, 10, 5.0)
    assert (
        one.baseline.version == "v1"
        and one.improvement is None
        and one.regressed is False
    )


def test_snapshot_totals():
    runs = [run("v1", cost_usd=1.25), run("v2", cost_usd=0.75)]
    snap = metrics.snapshot(runs, None, 1, 5.0)
    assert snap.total_cost == 2.0
    assert snap.last_run["harness_version"] == "v2"
    assert set(snap.versions) == {"v1", "v2"}
