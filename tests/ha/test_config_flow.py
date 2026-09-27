"""Config, reconfigure and options flows."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.agent_harness_performance_tracker.const import (
    CONF_AGENT,
    CONF_MIN_RUNS,
    CONF_RETENTION,
    CONF_TOLERANCE,
    CONF_WEBHOOK_ID,
    CONF_WINDOW,
    DOMAIN,
)

from .conftest import AGENT


async def _start(hass: HomeAssistant) -> dict[str, Any]:
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )


async def test_user_flow_creates_an_entry_with_a_webhook(hass: HomeAssistant) -> None:
    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_AGENT: "  Claude Code  "}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Claude Code"
    assert result["data"][CONF_AGENT] == "Claude Code"
    webhook_id = result["data"][CONF_WEBHOOK_ID]
    assert len(webhook_id) > 20
    # The path is shown once, here, and nowhere else.
    assert (
        result["description_placeholders"]["webhook_url"]
        == f"/api/webhook/{webhook_id}"
    )
    assert result["result"].unique_id == "claude_code"


async def test_user_flow_refuses_an_empty_or_over_long_name(
    hass: HomeAssistant,
) -> None:
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_AGENT: "   "}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_AGENT: "invalid_name"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_AGENT: "x" * 201}
    )
    assert result["errors"] == {CONF_AGENT: "invalid_name"}

    # Recovery: a good name after a bad one still creates the entry.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_AGENT: "ok"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_aborts_on_a_duplicate_agent(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_AGENT: AGENT.upper()}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reconfigure_renames_and_keeps_the_webhook(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    result = await config_entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_AGENT: "Renamed agent"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert config_entry.title == "Renamed agent"
    assert config_entry.unique_id == "renamed_agent"
    assert config_entry.data[CONF_WEBHOOK_ID] == "test-webhook-id-0123456789abcdef"


async def test_reconfigure_refuses_a_name_another_agent_holds(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    other = MockConfigEntry(
        domain=DOMAIN,
        title="Other",
        unique_id="other",
        data={CONF_AGENT: "Other", CONF_WEBHOOK_ID: "other-webhook-id"},
    )
    other.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    result = await config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_AGENT: "other"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_AGENT: "name_taken"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_AGENT: ""}
    )
    assert result["errors"] == {CONF_AGENT: "invalid_name"}
    # The entry's own name is not "taken".
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_AGENT: AGENT}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"


async def test_options_flow_sets_thresholds_and_applies_without_restart(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_MIN_RUNS: 3, CONF_TOLERANCE: 2.5, CONF_RETENTION: 50, CONF_WINDOW: 5},
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options == {
        CONF_MIN_RUNS: 3,
        CONF_TOLERANCE: 2.5,
        CONF_RETENTION: 50,
        CONF_WINDOW: 5,
    }
    # OptionsFlowWithReload reloaded the entry; the store took the new retention
    # and the window gate the new size.
    assert config_entry.runtime_data.store._retention == 50
    assert config_entry.runtime_data.coordinator.data.window.size == 5
