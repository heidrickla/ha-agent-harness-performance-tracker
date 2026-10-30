"""Config, reconfigure and options flows."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.agent_harness_performance_tracker.const import (
    CONF_ADVANCED,
    CONF_AGENT,
    CONF_AGENT_PROGRAM,
    CONF_HARNESS,
    CONF_HARNESS_FILES,
    CONF_LOCAL_ONLY,
    CONF_MIN_RUNS,
    CONF_RETENTION,
    CONF_SELECTION,
    CONF_TOLERANCE,
    CONF_VERSION_LABEL,
    CONF_WEBHOOK_ID,
    CONF_WINDOW,
    DOMAIN,
    REPORTER_URL,
    VERSION,
)

from .conftest import AGENT
from .frontend_form import frontend_submission

LOCAL = "http://ha.example.com:8123"
REMOTE = "https://remote.example.com"


@pytest.fixture(autouse=True)
def local_address(hass: HomeAssistant) -> None:
    """Home Assistant knows its local address, as a running install does."""
    hass.config.internal_url = LOCAL


async def _start(hass: HomeAssistant) -> dict[str, Any]:
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )


def _named(name: str, program: str = "claude_code") -> dict[str, Any]:
    """The first form with a name typed into the Advanced section."""
    return {CONF_AGENT_PROGRAM: program, CONF_ADVANCED: {CONF_AGENT: name}}


def _section(result: dict[str, Any]) -> Any:
    for marker, value in result["data_schema"].schema.items():
        if str(marker) == CONF_ADVANCED:
            return value
    raise AssertionError("no advanced section in the form")


async def test_the_first_screen_asks_only_for_the_program(
    hass: HomeAssistant,
) -> None:
    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}
    fields = {str(k): k for k in result["data_schema"].schema}
    assert list(fields) == [CONF_AGENT_PROGRAM, CONF_ADVANCED]
    assert fields[CONF_AGENT_PROGRAM].default() == "claude_code"
    advanced = _section(result)
    assert advanced.options == {"collapsed": True}
    assert [str(k) for k in advanced.schema.schema] == [CONF_AGENT]


async def test_user_flow_creates_an_entry_with_a_webhook(hass: HomeAssistant) -> None:
    result = await _start(hass)
    # Submitted untouched: the program's default and the name it implies.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], frontend_submission(result)
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Claude Code"
    assert result["data"][CONF_AGENT] == "Claude Code"
    assert result["data"][CONF_AGENT_PROGRAM] == "claude_code"
    webhook_id = result["data"][CONF_WEBHOOK_ID]
    assert len(webhook_id) > 20
    # The address is shown here and on the options screen, nowhere else.
    assert (
        result["description_placeholders"]["webhook_url"]
        == f"{LOCAL}/api/webhook/{webhook_id}"
    )
    assert result["description_placeholders"]["reporter_url"] == REPORTER_URL
    assert REPORTER_URL.endswith(f"/v{VERSION}/tools/report_run.py")
    assert result["result"].unique_id == "claude_code"
    # A new agent's webhook answers only the local network.
    assert result["result"].options == {CONF_LOCAL_ONLY: True}


async def test_a_typed_name_is_kept(hass: HomeAssistant) -> None:
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _named("  Coding agent  ", "codex")
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Coding agent"
    assert result["data"][CONF_AGENT_PROGRAM] == "codex"
    assert result["result"].unique_id == "coding_agent"


async def test_the_default_name_is_numbered_when_taken(hass: HomeAssistant) -> None:
    for unique_id in ("codex", "codex_2"):
        MockConfigEntry(
            domain=DOMAIN,
            unique_id=unique_id,
            data={CONF_AGENT: unique_id, CONF_WEBHOOK_ID: f"{unique_id}-hook"},
        ).add_to_hass(hass)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], frontend_submission(result, {CONF_AGENT_PROGRAM: "codex"})
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Codex 3"
    assert result["result"].unique_id == "codex_3"


async def test_user_flow_refuses_an_over_long_name(
    hass: HomeAssistant,
) -> None:
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _named("x" * 201)
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_AGENT: "invalid_name"}
    # The error reopens the section and keeps what was typed.
    advanced = _section(result)
    assert advanced.options == {"collapsed": False}
    (marker,) = advanced.schema.schema
    assert marker.description == {"suggested_value": "x" * 201}

    # Recovery: a good name after a bad one still creates the entry.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _named("ok")
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_refuses_a_name_another_agent_holds(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _named(AGENT.upper())
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_AGENT: "name_taken"}
    assert _section(result).options == {"collapsed": False}


async def test_without_an_address_the_flow_says_where_and_checks_again(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No URL set and no plain-HTTP local address to fall back on. The http
    # server is up first, or loading the flow's dependencies sets an address.
    assert await async_setup_component(hass, "webhook", {})
    hass.config.internal_url = None
    monkeypatch.setattr(hass.config, "api", None)
    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "no_url"
    assert result["errors"] == {}
    assert result["description_placeholders"] == {"network_url": "/config/network"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "no_url"
    assert result["errors"] == {"base": "still_no_url"}
    hass.config.external_url = REMOTE
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], frontend_submission(result)
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    webhook_id = result["data"][CONF_WEBHOOK_ID]
    assert (
        result["description_placeholders"]["webhook_url"]
        == f"{REMOTE}/api/webhook/{webhook_id}"
    )
    # Only an internet address to show, so the webhook must answer it.
    assert result["result"].options == {CONF_LOCAL_ONLY: False}


async def test_a_program_without_a_profile_confirms_its_suggested_files(
    hass: HomeAssistant,
) -> None:
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], frontend_submission(result, {CONF_AGENT_PROGRAM: "junie"})
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "harness"
    assert result["description_placeholders"] == {"agent": "JetBrains Junie"}
    (marker,) = result["data_schema"].schema
    assert str(marker) == CONF_HARNESS_FILES
    suggested = marker.default()
    assert ".junie/rules" in suggested and "AGENTS.md" in suggested
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "JetBrains Junie"
    assert result["result"].options == {
        CONF_LOCAL_ONLY: True,
        CONF_SELECTION: "manual",
        CONF_HARNESS_FILES: suggested,
    }


async def test_other_must_name_its_harness_files(hass: HomeAssistant) -> None:
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], frontend_submission(result, {CONF_AGENT_PROGRAM: "other"})
    )
    assert result["step_id"] == "harness"
    (marker,) = result["data_schema"].schema
    assert marker.default() == []
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HARNESS_FILES: [" "]}
    )
    assert result["errors"] == {CONF_HARNESS_FILES: "harness_files_required"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HARNESS_FILES: ["x" * 301]}
    )
    assert result["errors"] == {CONF_HARNESS_FILES: "harness_files_invalid"}
    (marker,) = result["data_schema"].schema
    assert marker.description == {"suggested_value": ["x" * 301]}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HARNESS_FILES: ["RULES.md", "RULES.md"]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Other"
    assert result["result"].options[CONF_HARNESS_FILES] == ["RULES.md"]


async def test_reconfigure_renames_and_keeps_the_webhook(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    result = await config_entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_AGENT: "Renamed agent"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert config_entry.title == "Renamed agent"
    assert config_entry.unique_id == "renamed_agent"
    assert config_entry.data[CONF_WEBHOOK_ID] == "test-webhook-id-0123456789abcdef"


async def test_reconfigure_refuses_a_name_another_agent_holds(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    other = MockConfigEntry(
        domain=DOMAIN,
        title="Other",
        unique_id="other",
        data={CONF_AGENT: "Other", CONF_WEBHOOK_ID: "other-webhook-id"},
    )
    other.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    result = await config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_AGENT: "other"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_AGENT: "name_taken"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_AGENT: ""}
    )
    assert result["errors"] == {CONF_AGENT: "invalid_name"}
    # The entry's own name is not "taken".
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_AGENT: AGENT}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"


async def test_options_flow_sets_thresholds_and_applies_without_restart(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_MIN_RUNS: 3,
            CONF_TOLERANCE: 2.5,
            CONF_RETENTION: 50,
            CONF_WINDOW: 5,
            CONF_HARNESS: {
                CONF_SELECTION: "manual",
                CONF_HARNESS_FILES: ["AGENTS.md", " ", "AGENTS.md", "~/hooks"],
                CONF_VERSION_LABEL: "",
            },
        },
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options == {
        CONF_MIN_RUNS: 3,
        CONF_TOLERANCE: 2.5,
        CONF_RETENTION: 50,
        CONF_WINDOW: 5,
        CONF_LOCAL_ONLY: False,
        CONF_SELECTION: "manual",
        CONF_HARNESS_FILES: ["AGENTS.md", "~/hooks"],
        CONF_VERSION_LABEL: "",
    }
    # OptionsFlowWithReload reloaded the entry; the store took the new retention
    # and the window gate the new size.
    assert config_entry.runtime_data.store._retention == 50
    assert config_entry.runtime_data.coordinator.data.window.size == 5


def _program_entry(program: str) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title=program,
        unique_id=program,
        version=1,
        minor_version=2,
        data={
            CONF_AGENT: program,
            CONF_AGENT_PROGRAM: program,
            CONF_WEBHOOK_ID: f"{program}-webhook-id-0123456789",
        },
    )


def _options(harness: dict[str, Any]) -> dict[str, Any]:
    return {
        CONF_MIN_RUNS: 10,
        CONF_TOLERANCE: 5,
        CONF_RETENTION: 2000,
        CONF_WINDOW: 10,
        CONF_HARNESS: harness,
    }


def _schema_default(result: dict[str, Any], section: str, key: str) -> Any:
    for marker, value in result["data_schema"].schema.items():
        if str(marker) == section:
            for inner in value.schema.schema:
                if str(inner) == key:
                    return inner.default()
    raise AssertionError(f"{section}.{key} not in the form")


async def test_options_offer_automatic_for_claude_code(hass: HomeAssistant) -> None:
    entry = _program_entry("claude_code")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert _schema_default(result, CONF_HARNESS, CONF_SELECTION) == "automatic"
    assert result["description_placeholders"]["webhook_url"] == (
        f"{LOCAL}/api/webhook/claude_code-webhook-id-0123456789"
    )
    assert result["description_placeholders"]["reporter_url"] == REPORTER_URL
    assert "No run has reported" in result["description_placeholders"]["selection"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        _options(
            {
                CONF_SELECTION: "automatic",
                CONF_HARNESS_FILES: [],
                CONF_VERSION_LABEL: "main",
            }
        ),
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_SELECTION] == "automatic"
    assert entry.options[CONF_VERSION_LABEL] == "main"


async def test_options_show_the_path_when_the_address_is_gone(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = _program_entry("claude_code")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    hass.config.internal_url = None
    monkeypatch.setattr(hass.config, "api", None)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["description_placeholders"]["webhook_url"] == (
        "/api/webhook/claude_code-webhook-id-0123456789"
    )


async def test_options_suggest_a_list_for_junie_and_require_one(
    hass: HomeAssistant,
) -> None:
    entry = _program_entry("junie")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert _schema_default(result, CONF_HARNESS, CONF_SELECTION) == "manual"
    suggested = _schema_default(result, CONF_HARNESS, CONF_HARNESS_FILES)
    assert ".junie/rules" in suggested and "AGENTS.md" in suggested
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        _options(
            {CONF_SELECTION: "manual", CONF_HARNESS_FILES: [], CONF_VERSION_LABEL: ""}
        ),
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "harness_files_required"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        _options(
            {
                CONF_SELECTION: "manual",
                CONF_HARNESS_FILES: [".junie/rules"],
                CONF_VERSION_LABEL: "two words",
            }
        ),
    )
    assert result["errors"] == {"base": "version_label_invalid"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        _options(
            {
                CONF_SELECTION: "manual",
                CONF_HARNESS_FILES: ["x" * 301],
                CONF_VERSION_LABEL: "",
            }
        ),
    )
    assert result["errors"] == {"base": "harness_files_invalid"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        _options(
            {
                CONF_SELECTION: "manual",
                CONF_HARNESS_FILES: [".junie/rules"],
                CONF_VERSION_LABEL: "",
            }
        ),
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_HARNESS_FILES] == [".junie/rules"]


@pytest.mark.parametrize(
    "program",
    ["copilot_cli", "cursor", "antigravity", "cline", "opencode", "kilo_code"],
)
async def test_options_offer_automatic_for_profiled_programs(
    hass: HomeAssistant, program: str
) -> None:
    entry = _program_entry(program)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert _schema_default(result, CONF_HARNESS, CONF_SELECTION) == "automatic"


async def test_options_show_the_last_selection(hass: HomeAssistant) -> None:
    entry = _program_entry("claude_code")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await entry.runtime_data.coordinator.async_record(
        {
            "harness_version": "sha256:abc",
            "outcome": "pass",
            "harness_manifest": {
                "project": "~/project",
                "groups": [
                    {
                        "path": "~/.claude/settings.json",
                        "kind": "settings",
                        "keys": ["hooks"],
                    },
                    {"path": "~/.claude/skills/", "kind": "skills", "count": 14},
                ],
                "missing": ["~/gone.md"],
                "approvals": {"digest": "sha256:ab", "rules": 12},
                "memory": ["~/.claude/projects/p/memory/MEMORY.md"],
            },
        }
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    text = result["description_placeholders"]["selection"]
    assert "project `~/project`" in text
    assert "- settings: `~/.claude/settings.json` (keys hooks)" in text
    assert "- skills: `~/.claude/skills/` (14 files)" in text
    assert "- missing: `~/gone.md`" in text
    assert "12 saved approvals" in text
    assert "memory `~/.claude/projects/p/memory/MEMORY.md`" in text


async def test_reconfigure_changes_the_program(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    result = await config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_AGENT: AGENT, CONF_AGENT_PROGRAM: "codex"}
    )
    assert result["reason"] == "reconfigure_successful"
    assert config_entry.data[CONF_AGENT_PROGRAM] == "codex"
