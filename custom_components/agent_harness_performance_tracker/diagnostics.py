"""Downloadable diagnostics.

The per-harness figures and the last runs are the diagnostic value. The
webhook id is the reporter's only credential and is redacted; run notes are
free text a reporter may have filled with anything and are redacted too.
Task ids and harness versions stay: they are labels the operator chose and
the whole point of a report about them.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_WEBHOOK_ID, FIELD_NOTES
from .coordinator import TrackerConfigEntry

TO_REDACT = {CONF_WEBHOOK_ID, FIELD_NOTES}
LAST_RUNS = 25


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: TrackerConfigEntry
) -> dict[str, Any]:
    data = entry.runtime_data
    snap = data.coordinator.data
    return {
        "entry": {
            "title": entry.title,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "runs": {
            "total": snap.total_runs,
            "retained": len(data.store.runs),
            "pinned_baseline": data.store.pinned,
        },
        "gate": {
            "current": snap.current.as_dict() if snap.current else None,
            "baseline": snap.baseline.as_dict() if snap.baseline else None,
            "baseline_pinned": snap.baseline_pinned,
            "confirmed": snap.confirmed,
            "improvement": snap.improvement,
            "comparable_tasks": snap.comparable_tasks,
            "model_changed": snap.model_changed,
            "regressed": snap.regressed,
            "regressed_tasks": list(snap.regressed_tasks),
        },
        "versions": [s.as_dict() for s in snap.versions.values()],
        "last_runs": [
            async_redact_data(dict(run), TO_REDACT)
            for run in data.store.runs[-LAST_RUNS:]
        ],
    }
