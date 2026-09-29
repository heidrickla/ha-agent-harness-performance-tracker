"""Config, reconfigure and options flows.

One entry per agent. The entry is created with a fresh webhook id and the
URL is shown once, on the confirmation screen, because the id is the only
credential a reporter needs. The options carry how the reporter picks the
harness files, and show the files it picked for the last run.
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
from homeassistant.data_entry_flow import section
from homeassistant.helpers import selector
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.util import slugify

from .const import (
    CONF_AGENT,
    CONF_AGENT_PROGRAM,
    CONF_HARNESS,
    CONF_HARNESS_FILES,
    CONF_MIN_RUNS,
    CONF_RETENTION,
    CONF_SELECTION,
    CONF_TOLERANCE,
    CONF_VERSION_LABEL,
    CONF_WEBHOOK_ID,
    CONF_WINDOW,
    DEFAULT_MIN_RUNS,
    DEFAULT_RETENTION,
    DEFAULT_TOLERANCE,
    DEFAULT_WINDOW,
    DOMAIN,
    MAX_HARNESS_FILES,
    MAX_PATH,
    MAX_RETENTION,
    MAX_TEXT,
    MAX_WINDOW,
    MIN_WINDOW,
    SELECTION_AUTOMATIC,
    SELECTION_MANUAL,
)
from .programs import PROGRAM_OTHER, PROGRAMS, SUGGESTED, is_automatic

MAX_LABEL = 40
SHOWN_GROUPS = 30


def _agent_schema(default: str = "", program: str = "claude_code") -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_AGENT, default=default): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
            ),
            vol.Required(CONF_AGENT_PROGRAM, default=program): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=list(PROGRAMS),
                    translation_key=CONF_AGENT_PROGRAM,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
        }
    )


def _number(minimum: float, maximum: float, step: float) -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=minimum, max=maximum, step=step, mode=selector.NumberSelectorMode.BOX
        )
    )


def _options_schema(current: dict[str, Any], program: str) -> vol.Schema:
    automatic = is_automatic(program)
    selection = current.get(CONF_SELECTION) or (
        SELECTION_AUTOMATIC if automatic else SELECTION_MANUAL
    )
    if not automatic:
        selection = SELECTION_MANUAL
    files = list(current.get(CONF_HARNESS_FILES) or SUGGESTED.get(program, ()))
    return vol.Schema(
        {
            vol.Optional(
                CONF_MIN_RUNS, default=current.get(CONF_MIN_RUNS, DEFAULT_MIN_RUNS)
            ): _number(1, 1000, 1),
            vol.Optional(
                CONF_TOLERANCE, default=current.get(CONF_TOLERANCE, DEFAULT_TOLERANCE)
            ): _number(0, 100, 0.5),
            vol.Optional(
                CONF_RETENTION, default=current.get(CONF_RETENTION, DEFAULT_RETENTION)
            ): _number(10, MAX_RETENTION, 10),
            vol.Optional(
                CONF_WINDOW, default=current.get(CONF_WINDOW, DEFAULT_WINDOW)
            ): _number(MIN_WINDOW, MAX_WINDOW, 1),
            vol.Required(CONF_HARNESS): section(
                vol.Schema(
                    {
                        vol.Required(
                            CONF_SELECTION, default=selection
                        ): selector.SelectSelector(
                            selector.SelectSelectorConfig(
                                options=(
                                    [SELECTION_AUTOMATIC, SELECTION_MANUAL]
                                    if automatic
                                    else [SELECTION_MANUAL]
                                ),
                                translation_key=CONF_SELECTION,
                                mode=selector.SelectSelectorMode.DROPDOWN,
                            )
                        ),
                        vol.Optional(
                            CONF_HARNESS_FILES, default=files
                        ): selector.TextSelector(
                            selector.TextSelectorConfig(multiple=True)
                        ),
                        vol.Optional(
                            CONF_VERSION_LABEL,
                            default=str(current.get(CONF_VERSION_LABEL) or ""),
                        ): selector.TextSelector(),
                    }
                ),
                {"collapsed": False},
            ),
        }
    )


def _clean_agent(value: Any) -> str | None:
    name = str(value or "").strip()
    if not name or len(name) > MAX_TEXT:
        return None
    return name


def _clean_program(value: Any) -> str:
    program = str(value or PROGRAM_OTHER)
    return program if program in PROGRAMS else PROGRAM_OTHER


def _clean_files(value: Any) -> list[str] | None:
    """The listed paths, blanks dropped, or None when one is too long or too many."""
    items = value if isinstance(value, list) else [value] if value else []
    files = [str(v).strip() for v in items if str(v).strip()]
    if len(files) > MAX_HARNESS_FILES or any(len(f) > MAX_PATH for f in files):
        return None
    return list(dict.fromkeys(files))


def selection_text(selection: dict[str, Any] | None) -> str:
    """The reporter's last selection as Markdown, for the options screen."""
    if not selection:
        return "No run has reported its harness files yet."
    head = "Selected at the last run"
    if selection.get("recorded_at"):
        head += f", {str(selection['recorded_at'])[:16].replace('T', ' ')} UTC"
    if selection.get("project"):
        head += f", project `{selection['project']}`"
    lines = [head + ":", ""]
    for group in list(selection.get("groups") or [])[:SHOWN_GROUPS]:
        extra = []
        if group.get("count"):
            extra.append(f"{group['count']} files")
        if group.get("keys"):
            extra.append("keys " + ", ".join(group["keys"]))
        if group.get("unclassified"):
            extra.append("not hashed: " + ", ".join(group["unclassified"]))
        if group.get("model_ignored"):
            extra.append("model and effort lines ignored")
        tail = f" ({'; '.join(extra)})" if extra else ""
        lines.append(f"- {group.get('kind')}: `{group.get('path')}`{tail}")
    hidden = len(selection.get("groups") or []) - SHOWN_GROUPS
    if hidden > 0:
        lines.append(f"- and {hidden} more")
    for path in selection.get("missing") or []:
        lines.append(f"- missing: `{path}`")
    approvals = selection.get("approvals") or {}
    if approvals.get("digest"):
        lines.append(
            f"- beside the version: {approvals.get('rules', 0)} saved approvals"
        )
    for path in selection.get("memory") or []:
        lines.append(f"- beside the version: memory `{path}`")
    return "\n".join(lines)


class TrackerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Create one entry per agent."""

    VERSION = 1
    MINOR_VERSION = 2

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            name = _clean_agent(user_input.get(CONF_AGENT))
            program = _clean_program(user_input.get(CONF_AGENT_PROGRAM))
            if name is None:
                errors[CONF_AGENT] = "invalid_name"
            else:
                await self.async_set_unique_id(slugify(name))
                self._abort_if_unique_id_configured()
                webhook_id = webhook.async_generate_id()
                path = webhook.async_generate_path(webhook_id)
                try:
                    url = get_url(self.hass).rstrip("/") + path
                except NoURLAvailableError:
                    url = path
                return self.async_create_entry(
                    title=name,
                    data={
                        CONF_AGENT: name,
                        CONF_AGENT_PROGRAM: program,
                        CONF_WEBHOOK_ID: webhook_id,
                    },
                    description_placeholders={"agent": name, "webhook_url": url},
                )
        user_input = user_input or {}
        return self.async_show_form(
            step_id="user",
            data_schema=_agent_schema(
                str(user_input.get(CONF_AGENT, "")),
                _clean_program(user_input.get(CONF_AGENT_PROGRAM, "claude_code")),
            ),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Rename the agent or change its program. The webhook and the runs stay."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            name = _clean_agent(user_input.get(CONF_AGENT))
            program = _clean_program(user_input.get(CONF_AGENT_PROGRAM))
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
                    data={**entry.data, CONF_AGENT: name, CONF_AGENT_PROGRAM: program},
                )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_agent_schema(
                str(entry.data[CONF_AGENT]),
                str(entry.data.get(CONF_AGENT_PROGRAM, PROGRAM_OTHER)),
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    @override
    def async_get_options_flow(config_entry: ConfigEntry) -> TrackerOptionsFlow:
        return TrackerOptionsFlow()


class TrackerOptionsFlow(OptionsFlowWithReload):
    """Thresholds, retention, and the harness files."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        program = str(self.config_entry.data.get(CONF_AGENT_PROGRAM, PROGRAM_OTHER))
        errors: dict[str, str] = {}
        if user_input is not None:
            harness = user_input.get(CONF_HARNESS) or {}
            selection = str(harness.get(CONF_SELECTION) or SELECTION_MANUAL)
            if not is_automatic(program):
                selection = SELECTION_MANUAL
            files = _clean_files(harness.get(CONF_HARNESS_FILES))
            label = str(harness.get(CONF_VERSION_LABEL) or "").strip()
            if files is None:
                errors["base"] = "harness_files_invalid"
            elif selection == SELECTION_MANUAL and not files:
                errors["base"] = "harness_files_required"
            elif len(label) > MAX_LABEL or " " in label:
                errors["base"] = "version_label_invalid"
            else:
                return self.async_create_entry(
                    data={
                        CONF_MIN_RUNS: int(user_input[CONF_MIN_RUNS]),
                        CONF_TOLERANCE: float(user_input[CONF_TOLERANCE]),
                        CONF_RETENTION: int(user_input[CONF_RETENTION]),
                        CONF_WINDOW: int(user_input[CONF_WINDOW]),
                        CONF_SELECTION: selection,
                        CONF_HARNESS_FILES: files,
                        CONF_VERSION_LABEL: label,
                    }
                )
        current = dict(self.config_entry.options)
        runtime = getattr(self.config_entry, "runtime_data", None)
        last = runtime.store.selection if runtime is not None else None
        return self.async_show_form(
            step_id="init",
            data_schema=_options_schema(current, program),
            errors=errors,
            description_placeholders={"selection": selection_text(last)},
        )
