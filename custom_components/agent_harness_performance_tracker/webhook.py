"""The webhook: the same run record, posted as JSON.

For reporters that have no Home Assistant token, or that run where a
long-lived token should not live. The webhook id is the only credential, so
the URL is treated as a secret: it is shown once when the entry is created and
never stored in an entity attribute.
"""

from __future__ import annotations

import logging
from http import HTTPStatus
from typing import Any

import voluptuous as vol
from aiohttp import web
from homeassistant.components import webhook
from homeassistant.core import HomeAssistant, callback

from .const import CONF_WEBHOOK_ID, DOMAIN
from .coordinator import TrackerConfigEntry
from .schema import validate_run

_LOGGER = logging.getLogger(__name__)


async def _handle(
    hass: HomeAssistant, webhook_id: str, request: web.Request
) -> web.Response:
    entry = _entry_for(hass, webhook_id)
    if entry is None:
        return web.json_response(
            {"error": "unknown webhook"}, status=HTTPStatus.NOT_FOUND
        )
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
    snap = await entry.runtime_data.coordinator.async_record(run)
    return web.json_response(
        {
            "recorded": True,
            "run_count": snap.total_runs,
            "harness_version": snap.current.version if snap.current else None,
            "pass_rate": snap.current.pass_rate if snap.current else None,
            "regressed": snap.regressed,
        }
    )


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
        allowed_methods=["POST"],
    )


@callback
def async_unregister(hass: HomeAssistant, entry: TrackerConfigEntry) -> None:
    webhook.async_unregister(hass, entry.data[CONF_WEBHOOK_ID])
