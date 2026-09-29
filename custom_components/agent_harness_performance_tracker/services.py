"""Actions: record a run, pin a baseline.

Registered at component setup, not per entry, so an automation calling one
while the entry is unloaded gets a translated refusal rather than "action not
found".
"""

from __future__ import annotations

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .const import (
    ATTR_CONFIG_ENTRY_ID,
    DOMAIN,
    FIELD_HARNESS,
    SERVICE_RECORD_RUN,
    SERVICE_SET_BASELINE,
)
from .coordinator import TrackerConfigEntry
from .schema import RUN_FIELDS

RECORD_SCHEMA = vol.Schema(
    {vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string, **RUN_FIELDS}
)
BASELINE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Optional(FIELD_HARNESS): cv.string,
    }
)


def _entry(hass: HomeAssistant, entry_id: str) -> TrackerConfigEntry:
    """The loaded entry the call targets, or a translated refusal."""
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="entry_not_found",
            translation_placeholders={"entry_id": entry_id},
        )
    if entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="not_loaded",
            translation_placeholders={"agent": entry.title},
        )
    return entry


async def _record_run(call: ServiceCall) -> ServiceResponse:
    entry = _entry(call.hass, call.data[ATTR_CONFIG_ENTRY_ID])
    run = {k: v for k, v in call.data.items() if k != ATTR_CONFIG_ENTRY_ID}
    coordinator = entry.runtime_data.coordinator
    if coordinator.is_duplicate(run):
        return {"duplicate": True, "run_count": coordinator.data.total_runs}
    snap = await coordinator.async_record(run)
    current = snap.current
    return {
        "duplicate": False,
        "run_count": snap.total_runs,
        "harness_version": current.version if current else None,
        "pass_rate": current.pass_rate if current else None,
        "regressed": snap.regressed,
        "regressed_tasks": list(snap.regressed_tasks),
    }


async def _set_baseline(call: ServiceCall) -> ServiceResponse:
    entry = _entry(call.hass, call.data[ATTR_CONFIG_ENTRY_ID])
    version = call.data.get(FIELD_HARNESS) or None
    coordinator = entry.runtime_data.coordinator
    if version and version not in coordinator.data.versions:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unknown_harness",
            translation_placeholders={"version": version},
        )
    snap = await coordinator.async_pin(version)
    return {
        "baseline_version": snap.baseline.version if snap.baseline else None,
        "pinned": snap.baseline_pinned,
    }


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    hass.services.async_register(
        DOMAIN,
        SERVICE_RECORD_RUN,
        _record_run,
        schema=RECORD_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_BASELINE,
        _set_baseline,
        schema=BASELINE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
