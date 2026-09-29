"""The gate end to end: entities, the repair issue and the regression event."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.agent_harness_performance_tracker.const import (
    CONF_MIN_RUNS,
    CONF_RETENTION,
    CONF_TOLERANCE,
    DOMAIN,
)

from .conftest import run

PREFIX = "sensor.claude_code_on_a_workstation_"
GATE = "binary_sensor.claude_code_on_a_workstation_harness_regressed"


async def _setup(hass: HomeAssistant, entry: MockConfigEntry, **options: Any) -> None:
    entry.add_to_hass(hass)
    if options:
        hass.config_entries.async_update_entry(entry, options=options)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _record(hass: HomeAssistant, entry_id: str, **fields: Any) -> None:
    await hass.services.async_call(
        DOMAIN,
        "record_run",
        {"config_entry_id": entry_id, **run(**fields)},
        blocking=True,
    )
    await hass.async_block_till_done()


def _issue(hass: HomeAssistant, entry_id: str) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, f"harness_regressed_{entry_id}")


async def test_gate_stays_off_while_the_new_harness_is_unconfirmed(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry, **{CONF_MIN_RUNS: 3, CONF_TOLERANCE: 5.0})
    for _ in range(3):
        await _record(
            hass, config_entry.entry_id, harness="v1", outcome="pass", task_id="t"
        )
    assert hass.states.get(PREFIX + "baseline_harness_version").state == "v1"
    # Ends on a pass so the per-task half stays off: only the aggregate half is tested.
    await _record(
        hass, config_entry.entry_id, harness="v2", outcome="fail", task_id="t"
    )
    await _record(
        hass, config_entry.entry_id, harness="v2", outcome="pass", task_id="t"
    )
    gate = hass.states.get(GATE)
    assert gate.state == "off"
    assert gate.attributes["current_confirmed"] is False
    assert gate.attributes["improvement"] == -50.0
    assert gate.attributes["comparable_tasks"] == 1
    assert _issue(hass, config_entry.entry_id) is None


async def test_aggregate_regression_raises_the_issue_and_fires_once(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry, **{CONF_MIN_RUNS: 3, CONF_TOLERANCE: 5.0})
    events: list[Any] = []
    hass.bus.async_listen(f"{DOMAIN}_regression", lambda e: events.append(e.data))

    for _ in range(3):
        await _record(
            hass, config_entry.entry_id, harness="v1", outcome="pass", task_id="t"
        )
    # Fail, fail, pass: confirmed at 33.3% on the shared task, latest run passing.
    for outcome in ("fail", "fail", "pass"):
        await _record(
            hass, config_entry.entry_id, harness="v2", outcome=outcome, task_id="t"
        )

    gate = hass.states.get(GATE)
    assert gate.state == "on"
    assert gate.attributes["baseline_version"] == "v1"
    assert gate.attributes["regressed_tasks"] == []
    assert hass.states.get(PREFIX + "improvement_over_baseline").state == "-66.7"

    issue = _issue(hass, config_entry.entry_id)
    assert issue is not None
    assert issue.translation_placeholders["current"] == "v2"
    assert issue.translation_placeholders["baseline_rate"] == "100.0"
    assert len(events) == 1
    assert events[0]["harness_version"] == "v2"

    # More failing runs keep the issue but do not re-fire the event.
    await _record(
        hass, config_entry.entry_id, harness="v2", outcome="fail", task_id="t"
    )
    assert len(events) == 1
    assert _issue(hass, config_entry.entry_id) is not None


async def test_task_regression_fires_before_confirmation_and_clears_when_fixed(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    # min_runs of 5 keeps v2 unconfirmed throughout, so only the per-task
    # half of the gate can fire here; the aggregate half is tested above.
    await _setup(hass, config_entry, **{CONF_MIN_RUNS: 5, CONF_TOLERANCE: 5.0})
    await _record(
        hass, config_entry.entry_id, harness="v1", outcome="pass", task_id="deploy"
    )
    await _record(
        hass, config_entry.entry_id, harness="v1", outcome="pass", task_id="audit"
    )
    for _ in range(3):
        await _record(hass, config_entry.entry_id, harness="v1", outcome="pass")
    await _record(
        hass, config_entry.entry_id, harness="v2", outcome="fail", task_id="deploy"
    )

    regressions = hass.states.get(PREFIX + "regressed_tasks")
    assert regressions.state == "1"
    assert regressions.attributes["tasks"] == ["deploy"]
    gate = hass.states.get(GATE)
    assert gate.state == "on"
    assert gate.attributes["current_confirmed"] is False
    assert _issue(hass, config_entry.entry_id).translation_placeholders["tasks"] == "1"

    # The same task passing again on v2 clears the per-task half, and with v2
    # still unconfirmed nothing else holds the gate.
    await _record(
        hass, config_entry.entry_id, harness="v2", outcome="pass", task_id="deploy"
    )
    assert hass.states.get(PREFIX + "regressed_tasks").state == "0"
    assert hass.states.get(GATE).state == "off"
    assert _issue(hass, config_entry.entry_id) is None


async def test_pinning_the_baseline_changes_the_verdict(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry, **{CONF_MIN_RUNS: 2, CONF_TOLERANCE: 5.0})
    for _ in range(2):
        await _record(
            hass, config_entry.entry_id, harness="v1", outcome="pass", task_id="t"
        )
    for _ in range(2):
        await _record(
            hass, config_entry.entry_id, harness="v2", outcome="fail", task_id="t"
        )
    assert hass.states.get(GATE).state == "on"

    await hass.services.async_call(
        DOMAIN,
        "set_baseline",
        {"config_entry_id": config_entry.entry_id, "harness_version": "v2"},
        blocking=True,
    )
    await hass.async_block_till_done()
    # Measured against itself there is nothing to regress.
    assert hass.states.get(GATE).state == "off"
    assert hass.states.get(PREFIX + "improvement_over_baseline").state == "unknown"
    assert _issue(hass, config_entry.entry_id) is None


async def _save_options(
    hass: HomeAssistant, entry: MockConfigEntry, **options: Any
) -> None:
    """Through the options flow, which reloads the entry, as a user would."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_MIN_RUNS: 2,
            CONF_TOLERANCE: 5.0,
            CONF_RETENTION: 100,
            "harness": {"selection": "manual", "harness_files": ["AGENTS.md"]},
            **options,
        },
    )
    await hass.async_block_till_done()


async def test_saving_options_reloads_and_re_evaluates_the_gate(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry, **{CONF_MIN_RUNS: 2, CONF_TOLERANCE: 50.0})
    for _ in range(2):
        await _record(
            hass, config_entry.entry_id, harness="v1", outcome="pass", task_id="t"
        )
    # Fail then pass: the latest run passes, so only the aggregate half can fire.
    await _record(
        hass, config_entry.entry_id, harness="v2", outcome="fail", task_id="t"
    )
    await _record(
        hass, config_entry.entry_id, harness="v2", outcome="pass", task_id="t"
    )
    # 50 points below at a 50-point tolerance is not a regression.
    assert hass.states.get(GATE).state == "off"
    assert _issue(hass, config_entry.entry_id) is None

    await _save_options(hass, config_entry, **{CONF_TOLERANCE: 5.0})
    assert hass.states.get(GATE).state == "on"
    # The issue was raised at load, from the persisted runs and the new threshold.
    assert _issue(hass, config_entry.entry_id) is not None

    # Widening the tolerance again clears an issue that would otherwise
    # outlive the condition, because issues persist and thresholds do not.
    await _save_options(hass, config_entry, **{CONF_TOLERANCE: 50.0})
    assert hass.states.get(GATE).state == "off"
    assert _issue(hass, config_entry.entry_id) is None


async def test_every_entity_has_a_value_after_a_full_run(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry, **{CONF_MIN_RUNS: 1})
    await _record(
        hass,
        config_entry.entry_id,
        harness="v1",
        outcome="partial",
        turns=7,
        tool_calls=40,
        duration_s=61.5,
        input_tokens=1000,
        output_tokens=200,
        cost_usd=0.02,
        denials=1,
        retries=2,
        interventions=3,
        notes="first",
    )
    states = {
        key: hass.states.get(PREFIX + key).state
        for key in (
            "runs",
            "harness_version",
            "success_rate",
            "verified_rate",
            "median_turns",
            "median_duration",
            "interventions_per_run",
            "denials_per_run",
            "cost",
            "baseline_harness_version",
            "regressed_tasks",
            "last_run",
        )
    }
    assert states == {
        "runs": "1",
        "harness_version": "v1",
        "success_rate": "0.0",
        "verified_rate": "0.0",
        "median_turns": "7.0",
        "median_duration": "61.5",
        "interventions_per_run": "3.0",
        "denials_per_run": "1.0",
        "cost": "0.02",
        "baseline_harness_version": "v1",
        "regressed_tasks": "0",
        "last_run": "partial",
    }
    # Baseline is the current version: no comparison to make.
    assert hass.states.get(PREFIX + "improvement_over_baseline").state == "unknown"
    version = hass.states.get(PREFIX + "harness_version")
    assert version.attributes["runs_on_version"] == 1
    assert version.attributes["confirmed"] is True


async def test_figures_that_removal_lowers_are_totals(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    # remove_runs and a lower retention both reduce them; total_increasing would read
    # each drop as a meter reset and log that the state is not strictly increasing.
    await _setup(hass, config_entry)
    await _record(hass, config_entry.entry_id, harness="v1", cost_usd=0.02)
    for key in ("runs", "cost"):
        assert hass.states.get(PREFIX + key).attributes["state_class"] == "total", key
