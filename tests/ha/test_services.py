"""The two actions: what they record, what they answer, what they refuse."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.agent_harness_performance_tracker.const import DOMAIN

from .conftest import run

PREFIX = "sensor.claude_code_on_a_workstation_"


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _record(hass: HomeAssistant, entry_id: str, **fields: Any) -> dict[str, Any]:
    return await hass.services.async_call(
        DOMAIN,
        "record_run",
        {"config_entry_id": entry_id, **run(**fields)},
        blocking=True,
        return_response=True,
    )


async def test_record_run_updates_entities_and_answers(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    response = await _record(
        hass,
        config_entry.entry_id,
        harness="v1",
        outcome="pass",
        turns=12,
        duration_s=300,
        cost_usd=0.4,
        task_id="t1",
        task_class="build",
        verified=True,
    )
    assert response == {
        "duplicate": False,
        "run_count": 1,
        "harness_version": "v1",
        "pass_rate": 100.0,
        "regressed": False,
        "regressed_tasks": [],
    }
    assert hass.states.get(PREFIX + "runs").state == "1"
    assert hass.states.get(PREFIX + "harness_version").state == "v1"
    assert hass.states.get(PREFIX + "success_rate").state == "100.0"
    assert hass.states.get(PREFIX + "verified_rate").state == "100.0"
    assert hass.states.get(PREFIX + "median_turns").state == "12.0"
    assert hass.states.get(PREFIX + "median_duration").state == "300.0"
    assert hass.states.get(PREFIX + "cost").state == "0.4"
    last = hass.states.get(PREFIX + "last_run")
    assert last.state == "pass"
    assert last.attributes["task_id"] == "t1"
    assert last.attributes["task_class"] == "build"
    assert last.attributes["verified"] is True
    assert last.attributes["recorded_at"]
    # No baseline yet: one run is a hypothesis.
    assert hass.states.get(PREFIX + "baseline_harness_version").state == "unknown"
    assert hass.states.get(PREFIX + "improvement_over_baseline").state == "unknown"


async def test_record_run_fires_the_event(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    seen: list[Any] = []
    hass.bus.async_listen(f"{DOMAIN}_run_recorded", lambda e: seen.append(e.data))
    await _record(hass, config_entry.entry_id, harness="v1", outcome="fail", denials=2)
    await hass.async_block_till_done()
    assert len(seen) == 1
    assert seen[0]["agent"] == config_entry.title
    assert seen[0]["harness_version"] == "v1"
    assert seen[0]["denials"] == 2
    assert seen[0]["entry_id"] == config_entry.entry_id


async def test_record_run_refuses_a_bad_field(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    with pytest.raises(Exception, match="outcome"):
        await _record(hass, config_entry.entry_id, harness="v1", outcome="great")
    with pytest.raises(Exception, match="turns"):
        await _record(
            hass, config_entry.entry_id, harness="v1", outcome="pass", turns=-1
        )
    assert hass.states.get(PREFIX + "runs").state == "0"


async def test_actions_refuse_an_unknown_or_unloaded_entry(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    with pytest.raises(ServiceValidationError) as err:
        await _record(hass, "does-not-exist", harness="v1", outcome="pass")
    assert err.value.translation_key == "entry_not_found"

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    # The action still exists while the entry is unloaded, and says why it cannot run.
    assert hass.services.has_service(DOMAIN, "record_run")
    with pytest.raises(ServiceValidationError) as err:
        await _record(hass, config_entry.entry_id, harness="v1", outcome="pass")
    assert err.value.translation_key == "not_loaded"


async def test_set_baseline_pins_and_unpins(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    for _ in range(3):
        await _record(
            hass, config_entry.entry_id, harness="v1", outcome="pass", task_id="t"
        )
    await _record(
        hass, config_entry.entry_id, harness="v2", outcome="fail", task_id="t"
    )
    # min_runs defaults to 10: nothing is confirmed, so there is no baseline.
    assert hass.states.get(PREFIX + "baseline_harness_version").state == "unknown"

    response = await hass.services.async_call(
        DOMAIN,
        "set_baseline",
        {"config_entry_id": config_entry.entry_id, "harness_version": "v1"},
        blocking=True,
        return_response=True,
    )
    assert response == {"baseline_version": "v1", "pinned": True}
    base = hass.states.get(PREFIX + "baseline_harness_version")
    assert base.state == "v1"
    assert base.attributes["pinned"] is True
    assert hass.states.get(PREFIX + "improvement_over_baseline").state == "-100.0"

    response = await hass.services.async_call(
        DOMAIN,
        "set_baseline",
        {"config_entry_id": config_entry.entry_id},
        blocking=True,
        return_response=True,
    )
    assert response == {"baseline_version": None, "pinned": False}
    assert hass.states.get(PREFIX + "baseline_harness_version").state == "unknown"


async def test_set_baseline_refuses_an_unknown_version(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    await _record(hass, config_entry.entry_id, harness="v1", outcome="pass")
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            "set_baseline",
            {"config_entry_id": config_entry.entry_id, "harness_version": "never-seen"},
            blocking=True,
        )
    assert err.value.translation_key == "unknown_harness"


async def test_runs_survive_a_reload(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    await _record(hass, config_entry.entry_id, harness="v1", outcome="pass")
    await _record(hass, config_entry.entry_id, harness="v1", outcome="fail")
    await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(PREFIX + "runs").state == "2"
    assert hass.states.get(PREFIX + "success_rate").state == "50.0"


async def test_record_run_records_a_run_key_once(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    first = await _record(hass, config_entry.entry_id, **run(run_key="k"))
    assert first["duplicate"] is False
    repeat = await _record(hass, config_entry.entry_id, **run(run_key="k"))
    assert repeat == {"duplicate": True, "run_count": 1}
