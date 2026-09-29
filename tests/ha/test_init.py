"""Setup, unload, removal and the two not-ready paths."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.agent_harness_performance_tracker.const import DOMAIN

from .conftest import AGENT, WEBHOOK_ID

EXPECTED_ENTITIES = 17


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_setup_creates_the_device_and_every_entity(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED

    device = dr.async_get(hass).async_get_device(
        identifiers={(DOMAIN, config_entry.entry_id)}
    )
    assert device is not None
    assert device.name == AGENT

    entities = er.async_entries_for_config_entry(
        er.async_get(hass), config_entry.entry_id
    )
    assert len(entities) == EXPECTED_ENTITIES
    assert all(e.device_id == device.id for e in entities)
    # Nothing has been recorded, so the figures are unknown rather than zero.
    assert hass.states.get("sensor.claude_code_on_a_workstation_runs").state == "0"
    assert (
        hass.states.get("sensor.claude_code_on_a_workstation_harness_version").state
        == "unknown"
    )
    assert (
        hass.states.get(
            "binary_sensor.claude_code_on_a_workstation_harness_regressed"
        ).state
        == "off"
    )


async def test_a_0_3_entry_migrates_to_program_other(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    assert config_entry.minor_version == 1
    await _setup(hass, config_entry)
    assert config_entry.minor_version == 2
    assert config_entry.data["agent_program"] == "other"
    assert config_entry.data["webhook_id"] == WEBHOOK_ID


async def test_an_entry_from_a_newer_major_version_is_refused(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="future",
        unique_id="future",
        version=2,
        data={"agent": "future", "webhook_id": "future-webhook"},
    )
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.MIGRATION_ERROR


async def test_unload_unregisters_the_webhook_and_reload_re_registers(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    assert WEBHOOK_ID in hass.data["webhook"]
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.NOT_LOADED
    assert WEBHOOK_ID not in hass.data["webhook"]
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert WEBHOOK_ID in hass.data["webhook"]


async def test_remove_deletes_the_run_log(
    hass: HomeAssistant, config_entry: MockConfigEntry, hass_storage: dict
) -> None:
    await _setup(hass, config_entry)
    await hass.services.async_call(
        DOMAIN,
        "record_run",
        {
            "config_entry_id": config_entry.entry_id,
            "harness_version": "v1",
            "outcome": "pass",
        },
        blocking=True,
    )
    key = f"{DOMAIN}.{config_entry.entry_id}"
    assert key in hass_storage
    await hass.config_entries.async_remove(config_entry.entry_id)
    await hass.async_block_till_done()
    assert key not in hass_storage


async def test_unreadable_store_is_not_ready(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    # Patched on this integration's own class: patching the shared Store
    # helper breaks the http component's load first and the entry never runs.
    with patch(
        "custom_components.agent_harness_performance_tracker.store.RunStore.async_load",
        side_effect=OSError("disk"),
    ):
        assert not await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_taken_webhook_id_is_not_ready(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    with patch(
        "custom_components.agent_harness_performance_tracker.webhook.webhook.async_register",
        side_effect=ValueError("Handler is already defined!"),
    ):
        assert not await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_RETRY
