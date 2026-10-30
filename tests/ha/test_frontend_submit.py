"""Each form, submitted the way the dialog sends it.

The data comes from the schema the frontend receives, through a port of its
initial-data code (frontend_form.py); no section is ever opened.
"""

from __future__ import annotations

from typing import Any

import pytest
import voluptuous as vol
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component

from custom_components.agent_harness_performance_tracker.const import (
    CONF_ADVANCED,
    CONF_AGENT,
    CONF_AGENT_PROGRAM,
    CONF_HARNESS_FILES,
    CONF_SELECTION,
    DOMAIN,
)

from .frontend_form import frontend_submission
from .test_config_flow import REMOTE, _program_entry


@pytest.fixture(autouse=True)
def local_address(hass: HomeAssistant) -> None:
    hass.config.internal_url = "http://ha.example.com:8123"


def _form(result: Any) -> dict[str, Any]:
    """A form with no fields reaches the frontend as an empty schema."""
    return {**result, "data_schema": result["data_schema"] or vol.Schema({})}


async def _open(hass: HomeAssistant) -> Any:
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )


async def test_the_first_screen_submits_untouched(hass: HomeAssistant) -> None:
    result = await _open(hass)
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], frontend_submission(result)
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Claude Code"


async def test_the_harness_step_submits_untouched(hass: HomeAssistant) -> None:
    result = await _open(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], frontend_submission(result, {CONF_AGENT_PROGRAM: "junie"})
    )
    assert result["step_id"] == "harness"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], frontend_submission(result)
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].options[CONF_HARNESS_FILES]


async def test_the_no_url_step_checks_again_on_submit(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert await async_setup_component(hass, "webhook", {})
    hass.config.internal_url = None
    monkeypatch.setattr(hass.config, "api", None)
    result = await _open(hass)
    assert result["step_id"] == "no_url"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], frontend_submission(_form(result))
    )
    assert result["errors"] == {"base": "still_no_url"}

    hass.config.external_url = REMOTE
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], frontend_submission(_form(result))
    )
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], frontend_submission(result)
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.parametrize(
    ("program", "selection"), [("claude_code", "automatic"), ("junie", "manual")]
)
async def test_the_options_submit_untouched(
    hass: HomeAssistant, program: str, selection: str
) -> None:
    entry = _program_entry(program)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], frontend_submission(result)
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_SELECTION] == selection


async def test_what_was_typed_in_advanced_comes_back_after_an_error(
    hass: HomeAssistant,
) -> None:
    result = await _open(hass)
    sent = frontend_submission(result)
    sent[CONF_ADVANCED] = {CONF_AGENT: "x" * 201}
    shown = await hass.config_entries.flow.async_configure(result["flow_id"], sent)
    assert shown["errors"] == {CONF_AGENT: "invalid_name"}

    # The section reopens holding the typed name, and Submit sends it back.
    again = frontend_submission(shown)
    assert again[CONF_ADVANCED].get(CONF_AGENT) == "x" * 201
    hass.config_entries.flow.async_abort(shown["flow_id"])
