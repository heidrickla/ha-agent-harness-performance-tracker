"""The window gate end to end: repair issue, event, entities, denial classes."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.agent_harness_performance_tracker.const import (
    CONF_AGENT,
    CONF_WEBHOOK_ID,
    CONF_WINDOW,
    DOMAIN,
    EVENT_REGRESSION,
    ISSUE_WINDOW_REGRESSED,
)

from .conftest import AGENT, WEBHOOK_ID, run

URL = f"/api/webhook/{WEBHOOK_ID}"
PREFIX = "sensor.claude_code_on_a_workstation_"
GATE = "binary_sensor.claude_code_on_a_workstation_harness_regressed"


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title=AGENT,
        unique_id="claude_code_on_a_workstation",
        data={CONF_AGENT: AGENT, CONF_WEBHOOK_ID: WEBHOOK_ID},
        options={CONF_WINDOW: 3},
    )


async def _post(client: Any, runs: list[dict[str, Any]]) -> dict[str, Any]:
    body: dict[str, Any] = {}
    for r in runs:
        resp = await client.post(URL, json=r)
        assert resp.status == HTTPStatus.OK
        body = await resp.json()
    return body


async def test_window_gate_raises_and_clears_its_repair_issue(
    hass: HomeAssistant, hass_client_no_auth: ClientSessionGenerator
) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    events: list[Event] = []
    hass.bus.async_listen(EVENT_REGRESSION, events.append)
    client = await hass_client_no_auth()
    issue_id = f"{ISSUE_WINDOW_REGRESSED}_{entry.entry_id}"
    registry = ir.async_get(hass)

    await _post(client, [run("v1", task_id="t")] * 3)
    body = await _post(client, [run(f"v{i}", "fail", task_id="t") for i in (2, 3, 4)])
    await hass.async_block_till_done()
    assert body["window"]["regressed"] is True
    assert body["window"]["improvement"] == -100.0 and body["window"]["versions"] == 3
    issue = registry.async_get_issue(DOMAIN, issue_id)
    assert issue is not None and issue.translation_key == ISSUE_WINDOW_REGRESSED
    assert issue.translation_placeholders["versions"] == "v2, v3, v4"
    assert hass.states.get(GATE).state == "on"
    assert hass.states.get(GATE).attributes["window_regressed"] is True
    assert [e.data["kind"] for e in events] == ["window"]

    # Still regressed on the next run: no second event, the issue stays.
    await _post(client, [run("v4", "fail", task_id="t")])
    await hass.async_block_till_done()
    assert len(events) == 1 and registry.async_get_issue(DOMAIN, issue_id)

    body = await _post(client, [run("v5", task_id="t")] * 3)
    await hass.async_block_till_done()
    assert body["window"]["regressed"] is False
    assert registry.async_get_issue(DOMAIN, issue_id) is None
    assert hass.states.get(GATE).state == "off"
    assert hass.states.get(PREFIX + "window_success_rate").state == "100.0"


async def test_denial_classes_are_counted_and_recurring_ones_named(
    hass: HomeAssistant, hass_client_no_auth: ClientSessionGenerator
) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    client = await hass_client_no_auth()
    hook = {"hook:owner-key-guard": 1}
    body = await _post(
        client,
        [
            run(denials=1, denial_classes=hook),
            run(denials=1, denial_classes=hook),
            run(denials=1, denial_classes={"person": 1}),
        ],
    )
    await hass.async_block_till_done()
    assert body["window"]["recurring_denials"] == [["hook:owner-key-guard", 2]]
    state = hass.states.get(PREFIX + "recurring_denial_classes")
    assert state.state == "1"
    assert state.attributes["classes"] == {"hook:owner-key-guard": 2}
    assert hass.states.get(PREFIX + "window_denials_per_run").state == "1.0"


async def test_malformed_denial_classes_are_refused_naming_the_field(
    hass: HomeAssistant, hass_client_no_auth: ClientSessionGenerator
) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    client = await hass_client_no_auth()
    for bad in (
        {"hook:x": -1},
        {"hook:x": "many"},
        {"": 1},
        {f"c{i}": 1 for i in range(21)},
        ["hook:x"],
    ):
        resp = await client.post(URL, json=run(denial_classes=bad))
        assert resp.status == HTTPStatus.BAD_REQUEST, bad
        assert (await resp.json())["field"].startswith("denial_classes"), bad
