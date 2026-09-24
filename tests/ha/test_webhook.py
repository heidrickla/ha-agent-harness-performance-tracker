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


async def test_webhook_only_accepts_post(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    await _setup(hass, config_entry)
    client = await hass_client_no_auth()
    resp = await client.get(URL)
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
