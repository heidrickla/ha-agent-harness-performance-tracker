"""The webhook: same record, same answer, and every refusal it can give."""

from __future__ import annotations

from http import HTTPStatus

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from .conftest import WEBHOOK_ID, run

URL = f"/api/webhook/{WEBHOOK_ID}"
PREFIX = "sensor.claude_code_on_a_workstation_"


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_webhook_records_a_run(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    await _setup(hass, config_entry)
    client = await hass_client_no_auth()
    resp = await client.post(URL, json=run(harness="v1", outcome="pass", turns=9))
    assert resp.status == HTTPStatus.OK
    assert await resp.json() == {
        "recorded": True,
        "run_count": 1,
        "harness_version": "v1",
        "pass_rate": 100.0,
        "current_runs": 1,
        "confirmed": False,
        "comparable_tasks": 0,
        "model_changed": False,
        "regressed": False,
        "window": {
            "size": 10,
            "runs": 1,
            "pass_rate": 100.0,
            "prior_pass_rate": None,
            "denials_per_run": 0.0,
            "prior_denials_per_run": None,
            "denials_per_100_calls": None,
            "prior_denials_per_100_calls": None,
            "improvement": None,
            "shared_tasks": 0,
            "regressed": False,
            "versions": 1,
            "recurring_denials": [],
        },
    }
    await hass.async_block_till_done()
    assert hass.states.get(PREFIX + "runs").state == "1"
    assert hass.states.get(PREFIX + "median_turns").state == "9.0"


async def test_webhook_refuses_bad_bodies(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    await _setup(hass, config_entry)
    client = await hass_client_no_auth()

    resp = await client.post(URL, data=b"not json")
    assert resp.status == HTTPStatus.BAD_REQUEST
    assert (await resp.json())["error"] == "body is not JSON"

    resp = await client.post(URL, json=["a", "list"])
    assert resp.status == HTTPStatus.BAD_REQUEST
    assert (await resp.json())["error"] == "body must be an object"

    resp = await client.post(URL, json={"harness_version": "v1"})
    assert resp.status == HTTPStatus.BAD_REQUEST
    body = await resp.json()
    assert body["field"] == "outcome"

    resp = await client.post(URL, json=run(harness="v1", outcome="pass", cost_usd=-1))
    assert resp.status == HTTPStatus.BAD_REQUEST
    assert (await resp.json())["field"] == "cost_usd"

    assert hass.states.get(PREFIX + "runs").state == "0"


async def test_webhook_accepts_only_get_and_post(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    await _setup(hass, config_entry)
    client = await hass_client_no_auth()
    resp = await client.put(URL, json=run())
    assert resp.status == HTTPStatus.METHOD_NOT_ALLOWED


async def test_webhook_is_gone_once_the_entry_is_unloaded(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    await _setup(hass, config_entry)
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    client = await hass_client_no_auth()
    resp = await client.post(URL, json=run())
    # Home Assistant's webhook component answers an unregistered id with 200
    # and no body, by design, so that probing for ids learns nothing.
    assert resp.status == HTTPStatus.OK
    assert await resp.text() == ""
    # The post reached nothing: after a reload the log is still empty.
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(PREFIX + "runs").state == "0"


async def test_get_answers_the_reporter_settings(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    await _setup(hass, config_entry)
    client = await hass_client_no_auth()
    resp = await client.get(URL)
    assert resp.status == HTTPStatus.OK
    # A 0.3 entry migrates to Other, whose files are a manual list.
    assert await resp.json() == {
        "agent": "Claude Code on a workstation",
        "agent_program": "other",
        "selection": "manual",
        "harness_files": [],
        "version_label": None,
        "fingerprint_schema": 2,
    }


async def test_get_answers_the_options_for_an_automatic_program(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    entry = MockConfigEntry(
        domain="agent_harness_performance_tracker",
        title="Codex",
        unique_id="codex",
        version=1,
        minor_version=2,
        data={"agent": "Codex", "agent_program": "codex", "webhook_id": WEBHOOK_ID},
        options={
            "selection": "automatic",
            "harness_files": [],
            "version_label": "main",
        },
    )
    await _setup(hass, entry)
    client = await hass_client_no_auth()
    body = await (await client.get(URL)).json()
    assert body["agent_program"] == "codex"
    assert body["selection"] == "automatic"
    assert body["version_label"] == "main"


async def test_a_repeated_run_key_is_recorded_once(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    await _setup(hass, config_entry)
    client = await hass_client_no_auth()
    first = await client.post(URL, json=run(run_key="k1"))
    assert (await first.json())["recorded"] is True
    again = await client.post(URL, json=run(run_key="k1", outcome="fail"))
    assert again.status == HTTPStatus.OK
    assert await again.json() == {
        "recorded": False,
        "duplicate": True,
        "run_count": 1,
    }
    other = await client.post(URL, json=run(run_key="k2"))
    assert (await other.json())["run_count"] == 2
    await hass.async_block_till_done()
    assert hass.states.get(PREFIX + "runs").state == "2"


async def test_the_manifest_is_kept_as_the_last_selection_not_in_the_run(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    await _setup(hass, config_entry)
    client = await hass_client_no_auth()
    manifest = {
        "program": "claude_code",
        "mode": "automatic",
        "project": "~/project",
        "files": 3,
        "groups": [
            {"path": "~/.claude/settings.json", "kind": "settings", "keys": ["hooks"]},
            {"path": "~/.claude/skills/", "kind": "skills", "count": 2, "future": 1},
        ],
        "approvals": {"digest": "sha256:ab", "rules": 4},
        "memory": ["~/.claude/projects/p/memory/MEMORY.md"],
        "newer_reporter_field": True,
    }
    resp = await client.post(
        URL,
        json=run(harness_manifest=manifest, client="claude_code", effort="high"),
    )
    assert resp.status == HTTPStatus.OK
    store = config_entry.runtime_data.store
    assert "harness_manifest" not in store.runs[-1]
    assert store.runs[-1]["client"] == "claude_code"
    assert store.runs[-1]["effort"] == "high"
    selection = store.selection
    assert selection is not None
    assert selection["harness_version"] == "v1"
    assert selection["groups"][1] == {
        "path": "~/.claude/skills/",
        "kind": "skills",
        "count": 2,
    }
    assert "newer_reporter_field" not in selection


async def test_an_oversized_manifest_is_refused_by_name(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    await _setup(hass, config_entry)
    client = await hass_client_no_auth()
    groups = [{"path": f"~/f{i}", "kind": "rules"} for i in range(101)]
    resp = await client.post(URL, json=run(harness_manifest={"groups": groups}))
    assert resp.status == HTTPStatus.BAD_REQUEST
    assert (await resp.json())["field"] == "harness_manifest.groups"
