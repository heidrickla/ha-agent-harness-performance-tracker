"""The webhook: the same run record, posted as JSON; and the agent's settings.

For reporters that have no Home Assistant token, or that run where a
long-lived token should not live. The webhook id is the only credential, so
the URL is treated as a secret: it is shown once when the entry is created and
never stored in an entity attribute.

GET answers the settings the reporter needs: the agent program, how to pick the
harness files, and the version label. POST records a run.
"""

from __future__ import annotations

import logging
from http import HTTPStatus
from typing import Any

import voluptuous as vol
from aiohttp import web
from homeassistant.components import webhook
from homeassistant.core import HomeAssistant, callback

from .const import (
    CONF_AGENT,
    CONF_AGENT_PROGRAM,
    CONF_HARNESS_FILES,
    CONF_SELECTION,
    CONF_VERSION_LABEL,
    CONF_WEBHOOK_ID,
    DOMAIN,
    FINGERPRINT_SCHEMA,
    SELECTION_AUTOMATIC,
    SELECTION_MANUAL,
)
from .coordinator import TrackerConfigEntry
from .metrics import Snapshot
from .programs import PROGRAM_OTHER, is_automatic
from .schema import validate_run

_LOGGER = logging.getLogger(__name__)


def settings_for(entry: TrackerConfigEntry) -> dict[str, Any]:
    """What the reporter reads before it computes a version."""
    program = str(entry.data.get(CONF_AGENT_PROGRAM, PROGRAM_OTHER))
    selection = entry.options.get(CONF_SELECTION) or SELECTION_AUTOMATIC
    if not is_automatic(program):
        selection = SELECTION_MANUAL
    return {
        "agent": entry.data[CONF_AGENT],
        "agent_program": program,
        "selection": selection,
        "harness_files": list(entry.options.get(CONF_HARNESS_FILES, [])),
        "version_label": entry.options.get(CONF_VERSION_LABEL) or None,
        "fingerprint_schema": FINGERPRINT_SCHEMA,
    }


async def _handle(
    hass: HomeAssistant, webhook_id: str, request: web.Request
) -> web.Response:
    entry = _entry_for(hass, webhook_id)
    if entry is None:
        return web.json_response(
            {"error": "unknown webhook"}, status=HTTPStatus.NOT_FOUND
        )
    if request.method == "GET":
        return web.json_response(settings_for(entry))
    try:
        payload: Any = await request.json()
    except ValueError:
        return web.json_response(
            {"error": "body is not JSON"}, status=HTTPStatus.BAD_REQUEST
        )
    if not isinstance(payload, dict):
        return web.json_response(
            {"error": "body must be an object"}, status=HTTPStatus.BAD_REQUEST
        )
    try:
        run = validate_run(payload)
    except vol.Invalid as err:
        return web.json_response(
            {"error": str(err), "field": ".".join(str(p) for p in err.path)},
            status=HTTPStatus.BAD_REQUEST,
        )
    coordinator = entry.runtime_data.coordinator
    if coordinator.is_duplicate(run):
        # Answered as a success so the reporter drops its copy instead of retrying.
        return web.json_response(
            {
                "recorded": False,
                "duplicate": True,
                "run_count": coordinator.data.total_runs,
            }
        )
    snap = await coordinator.async_record(run)
    return web.json_response(
        {
            "recorded": True,
            "run_count": snap.total_runs,
            "harness_version": snap.current.version if snap.current else None,
            "pass_rate": snap.current.pass_rate if snap.current else None,
            "current_runs": snap.current.runs if snap.current else 0,
            "confirmed": snap.confirmed,
            "comparable_tasks": snap.comparable_tasks,
            "model_changed": snap.model_changed,
            "regressed": snap.regressed,
            "window": _window_reply(snap),
        }
    )


def _window_reply(snap: Snapshot) -> dict[str, Any]:
    """The window gate, which the reporter shows at the next session start."""
    w = snap.window
    return {
        "size": w.size,
        "runs": w.recent.runs if w.recent else 0,
        "pass_rate": w.recent.pass_rate if w.recent else None,
        "prior_pass_rate": w.prior.pass_rate if w.prior else None,
        "denials_per_run": w.recent.denials_per_run if w.recent else None,
        "prior_denials_per_run": w.prior.denials_per_run if w.prior else None,
        "denials_per_100_calls": w.recent.denials_per_100_calls if w.recent else None,
        "prior_denials_per_100_calls": (
            w.prior.denials_per_100_calls if w.prior else None
        ),
        "improvement": w.improvement,
        "shared_tasks": w.shared_tasks,
        "regressed": w.regressed,
        "versions": len(w.versions),
        "recurring_denials": [[name, count] for name, count in w.recurring[:5]],
    }


def _entry_for(hass: HomeAssistant, webhook_id: str) -> TrackerConfigEntry | None:
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        if entry.data.get(CONF_WEBHOOK_ID) == webhook_id:
            return entry
    return None


@callback
def async_register(hass: HomeAssistant, entry: TrackerConfigEntry) -> None:
    webhook.async_register(
        hass,
        DOMAIN,
        f"{entry.title} runs",
        entry.data[CONF_WEBHOOK_ID],
        _handle,
        allowed_methods=["GET", "POST"],
    )


@callback
def async_unregister(hass: HomeAssistant, entry: TrackerConfigEntry) -> None:
    webhook.async_unregister(hass, entry.data[CONF_WEBHOOK_ID])
