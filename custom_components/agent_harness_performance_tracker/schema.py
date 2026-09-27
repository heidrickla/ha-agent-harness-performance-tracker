"""One run schema for the action and the webhook.

Validated once, in one place, so a reporter written against either entry
point is accepted by the other unchanged.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.helpers import config_validation as cv

from .const import (
    FIELD_CLIENT_VERSION,
    FIELD_COST,
    FIELD_DENIAL_CLASSES,
    FIELD_DENIALS,
    FIELD_DURATION,
    FIELD_HARNESS,
    FIELD_INPUT_TOKENS,
    FIELD_INTERVENTIONS,
    FIELD_MODEL,
    FIELD_NOTES,
    FIELD_OUTCOME,
    FIELD_OUTPUT_TOKENS,
    FIELD_RETRIES,
    FIELD_TASK_CLASS,
    FIELD_TASK_ID,
    FIELD_TOOL_CALLS,
    FIELD_TURNS,
    FIELD_VERIFIED,
    MAX_CLASS_NAME,
    MAX_DENIAL_CLASSES,
    MAX_NOTES,
    MAX_TEXT,
    OUTCOMES,
)

_TEXT = vol.All(cv.string, vol.Length(min=1, max=MAX_TEXT))
_COUNT = vol.All(vol.Coerce(int), vol.Range(min=0))
_AMOUNT = vol.All(vol.Coerce(float), vol.Range(min=0))
_CLASSES = vol.All(
    {vol.All(cv.string, vol.Length(min=1, max=MAX_CLASS_NAME)): _COUNT},
    vol.Length(max=MAX_DENIAL_CLASSES),
)

RUN_FIELDS: dict[Any, Any] = {
    vol.Required(FIELD_HARNESS): _TEXT,
    vol.Required(FIELD_OUTCOME): vol.In(OUTCOMES),
    vol.Optional(FIELD_TASK_ID): _TEXT,
    vol.Optional(FIELD_TASK_CLASS): _TEXT,
    vol.Optional(FIELD_VERIFIED, default=False): cv.boolean,
    vol.Optional(FIELD_TURNS): _COUNT,
    vol.Optional(FIELD_TOOL_CALLS): _COUNT,
    vol.Optional(FIELD_DURATION): _AMOUNT,
    vol.Optional(FIELD_INPUT_TOKENS): _COUNT,
    vol.Optional(FIELD_OUTPUT_TOKENS): _COUNT,
    vol.Optional(FIELD_COST): _AMOUNT,
    vol.Optional(FIELD_DENIALS, default=0): _COUNT,
    vol.Optional(FIELD_DENIAL_CLASSES): _CLASSES,
    vol.Optional(FIELD_RETRIES, default=0): _COUNT,
    vol.Optional(FIELD_INTERVENTIONS, default=0): _COUNT,
    vol.Optional(FIELD_NOTES): vol.All(cv.string, vol.Length(max=MAX_NOTES)),
    vol.Optional(FIELD_MODEL): _TEXT,
    vol.Optional(FIELD_CLIENT_VERSION): _TEXT,
}

RUN_SCHEMA = vol.Schema(RUN_FIELDS)


def validate_run(payload: dict[str, Any]) -> dict[str, Any]:
    """The run as stored, or raise vol.Invalid naming the field."""
    return dict(RUN_SCHEMA(payload))
