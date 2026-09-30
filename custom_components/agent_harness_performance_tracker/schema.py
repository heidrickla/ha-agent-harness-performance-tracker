"""One run schema for the action and the webhook.

Validated once, in one place, so a reporter written against either entry
point is accepted by the other unchanged.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.helpers import config_validation as cv

from .const import (
    FIELD_APPROVALS,
    FIELD_CLIENT,
    FIELD_CLIENT_VERSION,
    FIELD_COST,
    FIELD_DENIAL_CLASSES,
    FIELD_DENIALS,
    FIELD_DURATION,
    FIELD_EFFORT,
    FIELD_HARNESS,
    FIELD_INPUT_TOKENS,
    FIELD_INTERVENTIONS,
    FIELD_MANIFEST,
    FIELD_MEMORY,
    FIELD_MODEL,
    FIELD_NOTES,
    FIELD_OUTCOME,
    FIELD_OUTPUT_TOKENS,
    FIELD_RETRIES,
    FIELD_RUN_KEY,
    FIELD_SCHEMA,
    FIELD_TASK_CLASS,
    FIELD_TASK_ID,
    FIELD_TOOL_CALLS,
    FIELD_TURNS,
    FIELD_VERIFIED,
    MAX_CLASS_NAME,
    MAX_DENIAL_CLASSES,
    MAX_NOTES,
    MAX_PATH,
    MAX_TEXT,
    OUTCOMES,
    SELECTION_AUTOMATIC,
    SELECTION_MANUAL,
)

TEXT = vol.All(cv.string, vol.Length(min=1, max=MAX_TEXT))
_PATH = vol.All(cv.string, vol.Length(min=1, max=MAX_PATH))
_COUNT = vol.All(vol.Coerce(int), vol.Range(min=0))
_AMOUNT = vol.All(vol.Coerce(float), vol.Range(min=0))
_CLASSES = vol.All(
    {vol.All(cv.string, vol.Length(min=1, max=MAX_CLASS_NAME)): _COUNT},
    vol.Length(max=MAX_DENIAL_CLASSES),
)
_NAMES = vol.All([TEXT], vol.Length(max=50))

# What the reporter selected, for display. Unknown keys are dropped rather than
# refused, so a newer reporter still records its runs.
MANIFEST = vol.Schema(
    {
        vol.Optional("program"): TEXT,
        vol.Optional("mode"): vol.In((SELECTION_AUTOMATIC, SELECTION_MANUAL)),
        vol.Optional("project"): vol.Any(None, _PATH),
        vol.Optional("files"): _COUNT,
        vol.Optional("groups"): vol.All(
            [
                vol.Schema(
                    {
                        vol.Required("path"): _PATH,
                        vol.Required("kind"): TEXT,
                        vol.Optional("count"): _COUNT,
                        vol.Optional("keys"): _NAMES,
                        vol.Optional("unclassified"): _NAMES,
                        vol.Optional("model_ignored"): cv.boolean,
                    },
                    extra=vol.REMOVE_EXTRA,
                )
            ],
            vol.Length(max=100),
        ),
        vol.Optional("missing"): vol.All([_PATH], vol.Length(max=20)),
        vol.Optional("approvals"): vol.Schema(
            {
                vol.Optional("digest"): vol.Any(None, TEXT),
                vol.Optional("rules"): _COUNT,
            },
            extra=vol.REMOVE_EXTRA,
        ),
        vol.Optional("memory"): vol.All([_PATH], vol.Length(max=10)),
    },
    extra=vol.REMOVE_EXTRA,
)

RUN_FIELDS: dict[Any, Any] = {
    vol.Required(FIELD_HARNESS): TEXT,
    vol.Required(FIELD_OUTCOME): vol.In(OUTCOMES),
    vol.Optional(FIELD_TASK_ID): TEXT,
    vol.Optional(FIELD_TASK_CLASS): TEXT,
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
    vol.Optional(FIELD_MODEL): TEXT,
    vol.Optional(FIELD_EFFORT): TEXT,
    vol.Optional(FIELD_CLIENT): TEXT,
    vol.Optional(FIELD_CLIENT_VERSION): TEXT,
    vol.Optional(FIELD_SCHEMA): vol.All(vol.Coerce(int), vol.Range(min=1)),
    vol.Optional(FIELD_APPROVALS): TEXT,
    vol.Optional(FIELD_MEMORY): TEXT,
    vol.Optional(FIELD_RUN_KEY): TEXT,
    vol.Optional(FIELD_MANIFEST): MANIFEST,
}

RUN_SCHEMA = vol.Schema(RUN_FIELDS)


def validate_run(payload: dict[str, Any]) -> dict[str, Any]:
    """The run as stored, or raise vol.Invalid naming the field."""
    return dict(RUN_SCHEMA(payload))
