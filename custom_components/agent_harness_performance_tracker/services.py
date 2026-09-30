"""Actions: record a run, pin a baseline, remove named runs, retag named runs.

Registered at component setup, not per entry, so an automation calling one
while the entry is unloaded gets a translated refusal rather than "action not
found".
"""

from __future__ import annotations

from typing import cast

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
from homeassistant.util.json import JsonValueType

from .const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_RECORDED_AT,
    ATTR_RUN_KEYS,
    ATTR_RUNS,
    DOMAIN,
    FIELD_HARNESS,
    MAX_REMOVE,
    MAX_RETAG,
    SERVICE_RECORD_RUN,
    SERVICE_REMOVE_RUNS,
    SERVICE_RETAG_RUNS,
    SERVICE_SET_BASELINE,
)
from .coordinator import TrackerConfigEntry
from .schema import RUN_FIELDS, TEXT

RECORD_SCHEMA = vol.Schema(
    {vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string, **RUN_FIELDS}
)
BASELINE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Optional(FIELD_HARNESS): cv.string,
    }
)
REMOVE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Optional(ATTR_RUN_KEYS, default=[]): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional(ATTR_RECORDED_AT, default=[]): vol.All(
            cv.ensure_list, [cv.string]
        ),
    }
)

# Each run, by run key or recorded_at, to its new task id.
RETAG_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required(ATTR_RUNS): {cv.string: TEXT},
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


async def _remove_runs(call: ServiceCall) -> ServiceResponse:
    """Remove the named runs, all or none. The answer carries each removed run
    whole, so one removed by mistake can be recorded again."""
    entry = _entry(call.hass, call.data[ATTR_CONFIG_ENTRY_ID])
    keys = {k.strip() for k in call.data[ATTR_RUN_KEYS] if k.strip()}
    times = {t.strip() for t in call.data[ATTR_RECORDED_AT] if t.strip()}
    named = len(keys) + len(times)
    if not named or named > MAX_REMOVE:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="remove_count",
            translation_placeholders={"limit": str(MAX_REMOVE)},
        )
    coordinator = entry.runtime_data.coordinator
    missing = coordinator.missing(keys, times)
    if missing:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unknown_runs",
            translation_placeholders={"runs": ", ".join(missing)},
        )
    removed, snap = await coordinator.async_remove(keys, times)
    # Stored runs are the validated JSON the webhook and record_run accepted.
    runs = cast("list[JsonValueType]", removed)
    return {"removed": runs, "run_count": snap.total_runs}


async def _retag_runs(call: ServiceCall) -> ServiceResponse:
    """Give each named run a new task id, all or none. The answer carries each
    change with the id it had, so a retag made by mistake can be undone."""
    entry = _entry(call.hass, call.data[ATTR_CONFIG_ENTRY_ID])
    task_ids = {k.strip(): v.strip() for k, v in call.data[ATTR_RUNS].items()}
    task_ids.pop("", None)
    if not task_ids or len(task_ids) > MAX_RETAG:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="retag_count",
            translation_placeholders={"limit": str(MAX_RETAG)},
        )
    coordinator = entry.runtime_data.coordinator
    names = set(task_ids)
    missing = coordinator.missing(names, names)
    if missing:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unknown_retag",
            translation_placeholders={"runs": ", ".join(missing)},
        )
    changed, snap = await coordinator.async_retag(task_ids)
    return {
        "retagged": cast("list[JsonValueType]", changed),
        "run_count": snap.total_runs,
        "regressed": snap.regressed,
        "regressed_tasks": list(snap.regressed_tasks),
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
    hass.services.async_register(
        DOMAIN,
        SERVICE_REMOVE_RUNS,
        _remove_runs,
        schema=REMOVE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_RETAG_RUNS,
        _retag_runs,
        schema=RETAG_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
