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


async def _remove(hass: HomeAssistant, entry_id: str, **fields: Any) -> dict[str, Any]:
    return await hass.services.async_call(
        DOMAIN,
        "remove_runs",
        {"config_entry_id": entry_id, **fields},
        blocking=True,
        return_response=True,
    )


async def test_remove_runs_by_key_and_time_and_record_again(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    await _record(hass, config_entry.entry_id, harness="v1", outcome="pass")
    await _record(hass, config_entry.entry_id, harness="v1", outcome="fail")
    manifest = {"program": "cline", "mode": "automatic", "files": 3}
    await _record(
        hass,
        config_entry.entry_id,
        harness="v2",
        outcome="pass",
        run_key="stray",
        harness_manifest=manifest,
    )
    store = config_entry.runtime_data.store
    assert store.selection and store.selection["program"] == "cline"
    fail_time = store.runs[1]["recorded_at"]
    response = await _remove(
        hass, config_entry.entry_id, run_keys=["stray"], recorded_at=[fail_time]
    )
    assert response["run_count"] == 1
    assert sorted(r["outcome"] for r in response["removed"]) == ["fail", "pass"]
    assert hass.states.get(PREFIX + "runs").state == "1"
    assert hass.states.get(PREFIX + "success_rate").state == "100.0"
    assert hass.states.get(PREFIX + "harness_version").state == "v1"
    # The selection the removed run reported goes with it.
    assert store.selection is None
    # The key is forgotten, so a run removed by mistake can be recorded again.
    again = await _record(hass, config_entry.entry_id, **run(run_key="stray"))
    assert again["duplicate"] is False
    # And the removal survives a reload.
    await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(PREFIX + "runs").state == "2"


async def test_remove_runs_is_all_or_nothing(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    await _record(hass, config_entry.entry_id, **run(run_key="real"))
    with pytest.raises(ServiceValidationError) as err:
        await _remove(hass, config_entry.entry_id, run_keys=["real", "nope"])
    assert err.value.translation_key == "unknown_runs"
    assert err.value.translation_placeholders == {"runs": "nope"}
    assert hass.states.get(PREFIX + "runs").state == "1"


async def _retag(
    hass: HomeAssistant, entry_id: str, runs: dict[str, str]
) -> dict[str, Any]:
    return await hass.services.async_call(
        DOMAIN,
        "retag_runs",
        {"config_entry_id": entry_id, "runs": runs},
        blocking=True,
        return_response=True,
    )


async def test_retag_runs_refiles_a_run_and_the_gate_follows(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    for _ in range(10):
        await _record(
            hass, config_entry.entry_id, harness="v1", outcome="pass", task_id="x"
        )
    await _record(
        hass,
        config_entry.entry_id,
        harness="v2",
        outcome="partial",
        task_id="x",
        run_key="k",
    )
    await _record(
        hass, config_entry.entry_id, harness="v2", outcome="pass", task_id="z"
    )
    store = config_entry.runtime_data.store
    assert (
        hass.states.get(
            "binary_sensor.claude_code_on_a_workstation_harness_regressed"
        ).state
        == "on"
    )
    last = store.runs[-1]["recorded_at"]
    response = await _retag(hass, config_entry.entry_id, {"k": "y", last: "w"})
    assert sorted(response["retagged"], key=lambda c: c["to"]) == [
        {"run": last, "recorded_at": last, "from": "z", "to": "w"},
        {
            "run": "k",
            "recorded_at": store.runs[-2]["recorded_at"],
            "from": "x",
            "to": "y",
        },
    ]
    assert response["run_count"] == 12
    assert response["regressed"] is False
    assert response["regressed_tasks"] == []
    assert (
        hass.states.get(
            "binary_sensor.claude_code_on_a_workstation_harness_regressed"
        ).state
        == "off"
    )
    # Nothing else about the runs changes, and the retag survives a reload.
    assert [r["outcome"] for r in store.runs[-2:]] == ["partial", "pass"]
    await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert [r["task_id"] for r in config_entry.runtime_data.store.runs[-2:]] == [
        "y",
        "w",
    ]


async def test_retag_runs_is_all_or_nothing_and_names_one_to_fifty(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    await _record(hass, config_entry.entry_id, **run(run_key="real", task_id="a"))
    with pytest.raises(ServiceValidationError) as err:
        await _retag(hass, config_entry.entry_id, {"real": "b", "nope": "c"})
    assert err.value.translation_key == "unknown_retag"
    assert err.value.translation_placeholders == {"runs": "nope"}
    assert config_entry.runtime_data.store.runs[0]["task_id"] == "a"
    for runs in ({}, {f"k{i}": "t" for i in range(51)}):
        with pytest.raises(ServiceValidationError) as err:
            await _retag(hass, config_entry.entry_id, runs)
        assert err.value.translation_key == "retag_count"
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    with pytest.raises(ServiceValidationError) as err:
        await _retag(hass, config_entry.entry_id, {"real": "b"})
    assert err.value.translation_key == "not_loaded"


async def test_remove_runs_names_one_to_fifty(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    await _record(hass, config_entry.entry_id, **run(run_key="real"))
    for fields in ({}, {"run_keys": [f"k{i}" for i in range(51)]}):
        with pytest.raises(ServiceValidationError) as err:
            await _remove(hass, config_entry.entry_id, **fields)
        assert err.value.translation_key == "remove_count"
    assert hass.states.get(PREFIX + "runs").state == "1"
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    with pytest.raises(ServiceValidationError) as err:
        await _remove(hass, config_entry.entry_id, run_keys=["real"])
    assert err.value.translation_key == "not_loaded"
