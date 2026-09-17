"""Diagnostics: the figures are there, the credential and the notes are not."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.agent_harness_performance_tracker.const import DOMAIN
from custom_components.agent_harness_performance_tracker.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .conftest import WEBHOOK_ID, run


async def test_diagnostics_redact_the_webhook_and_the_notes(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    for outcome in ("pass", "pass", "fail"):
        await hass.services.async_call(
            DOMAIN,
            "record_run",
            {
                "config_entry_id": config_entry.entry_id,
                **run(
                    harness="v1",
                    outcome=outcome,
                    task_id="t",
                    notes="the lab box at home",
                ),
            },
            blocking=True,
        )

    diag = await async_get_config_entry_diagnostics(hass, config_entry)
    text = str(diag)
    assert WEBHOOK_ID not in text
    assert "the lab box at home" not in text
    assert diag["entry"]["data"]["webhook_id"] == "**REDACTED**"
    assert diag["runs"] == {"total": 3, "retained": 3, "pinned_baseline": None}
    assert diag["gate"]["current"]["pass_rate"] == 66.7
    assert diag["gate"]["baseline"] is None
    assert diag["versions"][0]["version"] == "v1"
    assert len(diag["last_runs"]) == 3
    assert diag["last_runs"][0]["task_id"] == "t"
    assert diag["last_runs"][0]["notes"] == "**REDACTED**"
