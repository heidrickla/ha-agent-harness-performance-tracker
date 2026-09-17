"""Config, reconfigure and options flows.

One entry per agent. The entry is created with a fresh webhook id and the
URL is shown once, on the confirmation screen, because the id is the only
credential a reporter needs.
"""

from __future__ import annotations

from typing import Any, override

import voluptuous as vol
from homeassistant.components import webhook
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.util import slugify

from .const import (
    CONF_AGENT,
    CONF_MIN_RUNS,
    CONF_RETENTION,
    CONF_TOLERANCE,
    CONF_WEBHOOK_ID,
    DEFAULT_MIN_RUNS,
    DEFAULT_RETENTION,
    DEFAULT_TOLERANCE,
    DOMAIN,
    MAX_RETENTION,
    MAX_TEXT,
)


def _agent_schema(default: str = "") -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_AGENT, default=default): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
            )
        }
    )


def _options_schema(current: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(
                CONF_MIN_RUNS, default=current.get(CONF_MIN_RUNS, DEFAULT_MIN_RUNS)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1, max=1000, step=1, mode=selector.NumberSelectorMode.BOX
                )
            ),
            vol.Optional(
                CONF_TOLERANCE, default=current.get(CONF_TOLERANCE, DEFAULT_TOLERANCE)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0, max=100, step=0.5, mode=selector.NumberSelectorMode.BOX
                )
            ),
            vol.Optional(
                CONF_RETENTION, default=current.get(CONF_RETENTION, DEFAULT_RETENTION)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=10,
                    max=MAX_RETENTION,
                    step=10,
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
        }
    )


def _clean_agent(value: Any) -> str | None:
    name = str(value or "").strip()
    if not name or len(name) > MAX_TEXT:
        return None
    return name


class TrackerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Create one entry per agent."""

    VERSION = 1

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            name = _clean_agent(user_input.get(CONF_AGENT))
            if name is None:
                errors[CONF_AGENT] = "invalid_name"
            else:
                await self.async_set_unique_id(slugify(name))
                self._abort_if_unique_id_configured()
                webhook_id = webhook.async_generate_id()
                return self.async_create_entry(
                    title=name,
                    data={CONF_AGENT: name, CONF_WEBHOOK_ID: webhook_id},
                    description_placeholders={
                        "agent": name,
                        "webhook_url": webhook.async_generate_path(webhook_id),
                    },
                )
        return self.async_show_form(
            step_id="user",
            data_schema=_agent_schema(str((user_input or {}).get(CONF_AGENT, ""))),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Rename the agent. The webhook id and the run log stay."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            name = _clean_agent(user_input.get(CONF_AGENT))
            slug = slugify(name) if name else None
            other = (
                self.hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, slug)
                if slug and slug != entry.unique_id
                else None
            )
            if name is None or slug is None:
                errors[CONF_AGENT] = "invalid_name"
            elif other is not None:
                errors[CONF_AGENT] = "name_taken"
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    unique_id=slug,
                    title=name,
                    data={**entry.data, CONF_AGENT: name},
                )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_agent_schema(str(entry.data[CONF_AGENT])),
            errors=errors,
        )

    @staticmethod
    @callback
    @override
    def async_get_options_flow(config_entry: ConfigEntry) -> TrackerOptionsFlow:
        return TrackerOptionsFlow()


class TrackerOptionsFlow(OptionsFlowWithReload):
    """Thresholds and retention."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                data={
                    CONF_MIN_RUNS: int(user_input[CONF_MIN_RUNS]),
                    CONF_TOLERANCE: float(user_input[CONF_TOLERANCE]),
                    CONF_RETENTION: int(user_input[CONF_RETENTION]),
                }
            )
        return self.async_show_form(
            step_id="init", data_schema=_options_schema(dict(self.config_entry.options))
        )
