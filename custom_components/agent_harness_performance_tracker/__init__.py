"""Agent Harness Performance Tracker.

Records each run an AI agent reports, groups them by the harness version the
run used, and tells you whether a harness change made the agent better or
worse against the best version it has confirmed so far.
"""

from __future__ import annotations

import logging

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from . import services, webhook
from .const import CONF_AGENT_PROGRAM, CONF_RETENTION, DEFAULT_RETENTION, DOMAIN
from .coordinator import TrackerConfigEntry, TrackerCoordinator, TrackerData
from .programs import PROGRAM_OTHER
from .store import RunStore

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the actions here so they exist while an entry is unloaded."""
    services.async_setup_services(hass)
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: TrackerConfigEntry) -> bool:
    """1.1 to 1.2: entries made before agent programs are Other, so the reporter
    keeps using the harness list in its local config until the program is set."""
    if entry.version > 1:
        return False
    if entry.minor_version < 2:
        hass.config_entries.async_update_entry(
            entry,
            data={CONF_AGENT_PROGRAM: PROGRAM_OTHER, **entry.data},
            minor_version=2,
        )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: TrackerConfigEntry) -> bool:
    store = RunStore(
        hass, entry.entry_id, int(entry.options.get(CONF_RETENTION, DEFAULT_RETENTION))
    )
    try:
        await store.async_load()
    except Exception as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="store_unreadable",
            translation_placeholders={"error": str(err)},
        ) from err

    coordinator = TrackerCoordinator(hass, entry, store)
    await coordinator.async_config_entry_first_refresh()
    coordinator.sync_issue()
    entry.runtime_data = TrackerData(coordinator=coordinator, store=store)

    try:
        webhook.async_register(hass, entry)
    except ValueError as err:
        # Another entry holds this id. Nothing here can free it, so retry
        # rather than run without the webhook and report every run as lost.
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="webhook_taken",
        ) from err

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Options are read at setup; the options flow is OptionsFlowWithReload,
    # so saving them reloads the entry and the gate is re-run with the new
    # thresholds. Home Assistant refuses an update listener alongside that.
    return True


async def async_unload_entry(hass: HomeAssistant, entry: TrackerConfigEntry) -> bool:
    webhook.async_unregister(hass, entry)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: TrackerConfigEntry) -> None:
    """Delete the run log with the entry."""
    await RunStore(hass, entry.entry_id, DEFAULT_RETENTION).async_remove()
