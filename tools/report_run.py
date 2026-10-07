#!/usr/bin/env python3
"""Report coding-agent runs to the Agent Harness Performance Tracker.

One file, standard library only:

    python report_run.py --setup         store the webhook, register the hook
    python report_run.py --show-files    print this directory's harness files
    python report_run.py --outcome pass  report one run from any agent
    (hook payload on stdin, no args)     the Claude Code and Codex hook

HARNESS VERSION (fingerprint schema 2): a short SHA-256 over the files that
shape the agent's behaviour. Home Assistant holds the choice: Automatic uses
this file's profile for the agent program (Claude Code, Codex); Manual uses a
list of files and folders, relative paths resolved against the project root.
Recognised settings files are hashed on their harness keys only; model and
effort lines are stripped from agent, skill and command files; credentials,
caches, history, logs and transcripts are never read. Saved approvals and
loaded memory are digested beside the version, never in it, so an "always
allow" click does not start a new version.

HOOK: a payload is handled only when it identifies its client (Claude Code or
Codex); anything else, such as another agent running Claude's hooks, exits
without writing or posting. Every turn appends a ledger line; a
`Verdict: pass|fail|partial [verified|unverified] [task=<id>]` line in the
agent's final message holds a run; the person's next prompt posts it (`/pass`,
`/fail`, `/partial` or `/verdict <outcome> task=<id> class=<x>` confirm it as
the person's verdict). Each run carries a run_key; Home Assistant records a
key once.

CONFIG: ~/.config/ha-harness-tracker.json (0600), written by --setup:
    {"agents": {"claude_code": {"webhook_url": "...", "insecure": false}}}
The 0.3 layout (top-level webhook_url, harness, label) is still read.
HARNESS_LEDGER_CONFIG and HARNESS_LEDGER_STATE override the two paths.
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import glob
import hashlib
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

try:
    import tomllib
except ImportError:  # Python 3.10: TOML files are hashed byte for byte
    tomllib = None  # type: ignore[assignment]

SCHEMA = 2
UTC_ZONE = timezone.utc  # noqa: UP017 - datetime.UTC is 3.11; this runs on 3.10
HOME = os.path.expanduser("~")
CONFIG = os.environ.get("HARNESS_LEDGER_CONFIG") or os.path.join(
    HOME, ".config", "ha-harness-tracker.json"
)
_LEGACY_STATE = os.path.join(HOME, ".claude", "harness-ledger")
STATE_DIR = os.environ.get("HARNESS_LEDGER_STATE") or (
    _LEGACY_STATE
    if os.path.isdir(_LEGACY_STATE)
    else os.path.join(HOME, ".config", "ha-harness-tracker", "state")
)
INSTALL_DIR = os.path.join(HOME, ".config", "ha-harness-tracker")

AUTOMATIC = (
    "claude_code",
    "codex",
    "copilot_cli",
    "cursor",
    "antigravity",
    "cline",
    "opencode",
    "kilo_code",
)
PROGRAM_NAMES = {
    "claude_code": "Claude Code",
    "codex": "Codex",
    "copilot_cli": "GitHub Copilot CLI",
    "cursor": "Cursor",
    "antigravity": "Antigravity",
    "cline": "Cline",
    "opencode": "OpenCode",
    "kilo_code": "Kilo Code",
}
# Programs whose prompt and session-end hooks let a run wait for the person's
# verdict. The others post the agent's own verdict when the turn ends.
HOLDS_FOR_PERSON = ("claude_code", "codex", "copilot_cli", "cursor")
MAX_FILE_BYTES = 5_000_000
MAX_FILES = 5000
MAX_MANIFEST = 100
SKIP_DIRS = {"node_modules", "__pycache__", ".git", "cache", "logs"}
SKIP_SUFFIXES = (".pyc", ".bak", ".tmp", ".log", ".lock")
MODEL_KEYS = ("model", "effort", "model_reasoning_effort", "reasoning_effort")
SECRET_KEY_RE = re.compile(
    r"key|token|secret|password|passwd|auth|cookie|credential", re.I
)
# Environment names: MAX_OUTPUT_TOKENS is a limit, ANTHROPIC_API_KEY a secret.
ENV_SECRET_RE = re.compile(
    r"API_?KEY|_KEY$|SECRET|PASSW|AUTH_TOKEN|ACCESS_TOKEN|REFRESH_TOKEN|BEARER"
    r"|CREDENTIAL|COOKIE|_TOKEN$",
    re.I,
)

FIELDS = (
    "task_id",
    "task_class",
    "verified",
    "turns",
    "tool_calls",
    "duration_s",
    "input_tokens",
    "output_tokens",
    "cost_usd",
    "denials",
    "retries",
    "interventions",
    "notes",
    "model",
    "effort",
    "client_version",
)


# ------------------------------------------------------------------ projections
def canonical(obj: object) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()


def _read_json_bytes(data: bytes) -> object | None:
    try:
        return json.loads(data)
    except ValueError:
        return None


def _load_toml(data: bytes) -> dict | None:
    if tomllib is None:
        return None
    # Two clauses: ruff's py314 formatter rewrites a tuple into 3.14-only syntax,
    # and this file runs under whatever Python runs the agent's hooks.
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    try:
        return tomllib.loads(text)
    except ValueError:
        return None


def strip_model_lines(text: str) -> str:
    """Drop top-level model and effort keys from a leading `---` front matter block."""
    if not text.startswith("---"):
        return text
    lines = text.split("\n")
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return text
    kept: list[str] = [lines[0]]
    dropping = False
    for line in lines[1:end]:
        top = re.match(r"^([A-Za-z_][\w-]*)\s*:", line)
        if top:
            dropping = top.group(1) in MODEL_KEYS
        elif line[:1] not in (" ", "\t"):
            dropping = False
        if not dropping:
            kept.append(line)
    return "\n".join(kept + lines[end:])


def without_model(obj: object) -> object:
    """A mapping with model and effort keys removed at every level."""
    if isinstance(obj, dict):
        return {k: without_model(v) for k, v in obj.items() if k not in MODEL_KEYS}
    if isinstance(obj, list):
        return [without_model(v) for v in obj]
    return obj


def mcp_without_secrets(servers: object) -> object:
    """MCP server definitions without env, header and auth values; names stay."""
    if not isinstance(servers, dict):
        return servers
    out: dict = {}
    for name, spec in servers.items():
        if not isinstance(spec, dict):
            out[name] = spec
            continue
        clean = {}
        for k, v in spec.items():
            if k in ("env", "headers", "http_headers", "env_http_headers", "oauth"):
                clean[k] = sorted(v) if isinstance(v, dict) else "<set>"
            elif SECRET_KEY_RE.search(k):
                clean[k] = "<set>"
            else:
                clean[k] = v
        out[name] = clean
    return out


# Claude Code settings.json: harness keys, and keys known to be preference or state.
CLAUDE_HARNESS = (
    "permissions",
    "hooks",
    "disableAllHooks",
    "autoMode",
    "enabledPlugins",
    "sandbox",
    "claudeMdExcludes",
    "outputStyle",
    "attribution",
    "skillOverrides",
    "enableAllProjectMcpServers",
    "enabledMcpjsonServers",
    "disabledMcpjsonServers",
    "allowedMcpServers",
    "deniedMcpServers",
    "agent",
    "includeGitInstructions",
    "includeCoAuthoredBy",
)
CLAUDE_PREFERENCE = (
    "$schema",
    "model",
    "effortLevel",
    "theme",
    "tui",
    "statusLine",
    "editorMode",
    "cleanupPeriodDays",
    "preferredNotifChannel",
    "notifications",
    "spinnerTipsEnabled",
    "spinnerVerbs",
    "spinnerTipsOverride",
    "autoUpdatesChannel",
    "feedbackSurveyRate",
    "forceLoginMethod",
    "apiKeyHelper",
    "awsAuthRefresh",
    "awsCredentialExport",
    "otelHeadersHelper",
    "companyAnnouncements",
    "extraKnownMarketplaces",
    "alwaysThinkingEnabled",
    "language",
    "voiceEnabled",
    "agentPushNotifEnabled",
    "inputNeededNotifEnabled",
    "skipWorkflowUsageWarning",
    "switchModelsOnFlag",
)
CLAUDE_ENV_RE = re.compile(r"^(CLAUDE_CODE_|DISABLE_|BASH_|MCP_|MAX_THINKING|ENABLE_)")
AGENTS_MD_MODE = ("pluginConfigs", "agents-md@builtin", "options", "instructionFiles")


def project_claude_settings(doc: dict, local: bool) -> tuple[dict, dict, list[str]]:
    """(harness, approvals, unclassified keys) of a Claude settings file. In
    settings.local.json the permission rules are click-saved approvals."""
    harness: dict = {}
    approvals: dict = {}
    for key in CLAUDE_HARNESS:
        if key not in doc:
            continue
        value = doc[key]
        if key == "agent":
            value = without_model(value)
        if key == "permissions" and local:
            approvals = value if isinstance(value, dict) else {"rules": value}
            continue
        harness[key] = value
    env = doc.get("env")
    if isinstance(env, dict):
        kept = {
            k: v
            for k, v in env.items()
            if CLAUDE_ENV_RE.match(k) and not ENV_SECRET_RE.search(k)
        }
        if kept:
            harness["env"] = kept
    node: object = doc
    for part in AGENTS_MD_MODE:
        node = node.get(part) if isinstance(node, dict) else None
    if node is not None:
        harness["agentsMdMode"] = node
    known = set(CLAUDE_HARNESS) | set(CLAUDE_PREFERENCE) | {"env", "pluginConfigs"}
    unclassified = sorted(k for k in doc if k not in known)
    return harness, approvals, unclassified


def _claude_project_key(projects: dict, cwd: str) -> str | None:
    want = os.path.normcase(os.path.normpath(cwd))
    for key in projects:
        if os.path.normcase(os.path.normpath(key)) == want:
            return key
    return None


def project_claude_json(doc: dict, cwd: str | None) -> tuple[dict, dict]:
    """(harness, approvals) of ~/.claude.json: user MCP servers, and this
    project's MCP servers and enable lists; its allowedTools are approvals."""
    harness: dict = {"mcpServers": mcp_without_secrets(doc.get("mcpServers") or {})}
    approvals: dict = {}
    projects = doc.get("projects") if isinstance(doc.get("projects"), dict) else {}
    key = _claude_project_key(projects, cwd) if cwd else None
    if key:
        proj = projects[key] if isinstance(projects[key], dict) else {}
        harness["project"] = {
            "mcpServers": mcp_without_secrets(proj.get("mcpServers") or {}),
            "enabledMcpjsonServers": proj.get("enabledMcpjsonServers") or [],
            "disabledMcpjsonServers": proj.get("disabledMcpjsonServers") or [],
        }
        if proj.get("allowedTools"):
            approvals = {"allowedTools": sorted(map(str, proj["allowedTools"]))}
    return harness, approvals


CODEX_HARNESS = (
    "approval_policy",
    "approvals_reviewer",
    "sandbox_mode",
    "sandbox_workspace_write",
    "windows",
    "permissions",
    "shell_environment_policy",
    "mcp_servers",
    "hooks",
    "features",
    "agents",
    "skills",
    "apps",
    "plugins",
    "marketplaces",
    "developer_instructions",
    "model_instructions_file",
    "project_doc_max_bytes",
    "project_doc_fallback_filenames",
    "computer_use",
    "browser_use",
)
CODEX_PREFERENCE = (
    "model",
    "model_provider",
    "model_providers",
    "model_reasoning_effort",
    "model_reasoning_summary",
    "model_verbosity",
    "model_context_window",
    "service_tier",
    "profile",
    "profiles",
    "tui",
    "history",
    "log_dir",
    "otel",
    "notify",
    "personality",
    "memories",
    "projects",
    "file_opener",
    "hide_agent_reasoning",
    "show_raw_agent_reasoning",
    "cli_auth_credentials_store",
    "preferred_auth_method",
    "check_for_update_on_startup",
    "analytics",
    "feedback",
    "windows_wsl_setup_acknowledged",
    "notice",
)


def project_codex_config(doc: dict) -> tuple[dict, list[str]]:
    """(harness, unclassified keys) of a Codex config.toml. Trust state is left
    out: an untrusted hook does not run, and its hash moves with the hook anyway."""
    harness: dict = {}
    for key in CODEX_HARNESS:
        if key not in doc:
            continue
        value = doc[key]
        if key == "mcp_servers":
            value = mcp_without_secrets(value)
        elif key == "hooks" and isinstance(value, dict):
            value = {k: v for k, v in value.items() if k != "state"}
        elif key == "agents":
            value = without_model(value)
        harness[key] = value
    desktop = doc.get("desktop")
    if isinstance(desktop, dict):
        text = {k: v for k, v in desktop.items() if "instructions" in k}
        if text:
            harness["desktop"] = text
    known = set(CODEX_HARNESS) | set(CODEX_PREFERENCE) | {"desktop"}
    return harness, sorted(k for k in doc if k not in known)


def read_jsonc(data: bytes) -> object | None:
    """JSON, or JSON with comments and trailing commas as OpenCode and Kilo accept."""
    doc = _read_json_bytes(data)
    if doc is not None:
        return doc
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None
    out: list[str] = []
    i, n, in_str = 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(text[i : i + 2] if c == "\\" else c)
            i += 2 if c == "\\" else 1
            in_str = c != '"'
            continue
        if c == '"':
            in_str = True
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end < 0 else end
            continue
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        out.append(c)
        i += 1
    return _read_json_bytes(re.sub(r",(\s*[}\]])", r"\1", "".join(out)).encode())


# JSON settings of the other agents: keys that are preference, state or display.
PREFERENCE_KEYS = {
    "$schema",
    "theme",
    "themeMode",
    "tui",
    "share",
    "autoupdate",
    "username",
    "provider",
    "small_model",
    "editor",
    "display",
    "notifications",
    "hints",
    "keybinds",
    "layout",
    "userSettings",
    "hasChangedDefaultModel",
    "selectedModel",
    "modelParameters",
    "modelSlashCommands",
    "version",
}
MCP_KEYS = ("mcp", "mcpServers", "mcp_servers", "servers")
OPENCODE_HARNESS = (
    "instructions",
    "permission",
    "agent",
    "mode",
    "command",
    "mcp",
    "plugin",
    "skills",
    "tools",
    "default_agent",
    "subagent_depth",
)


def project_settings(doc: dict) -> dict:
    """Harness keys of another agent's JSON settings: model, preference and
    secret-looking keys out, MCP servers without their env, header and auth values."""
    harness: dict = {}
    for key, value in doc.items():
        if key in MODEL_KEYS or key in PREFERENCE_KEYS or SECRET_KEY_RE.search(key):
            continue
        harness[key] = mcp_without_secrets(value) if key in MCP_KEYS else value
    return without_model(harness)  # type: ignore[return-value]


def project_file(
    projection: str, data: bytes, path: str, sel: Selection
) -> tuple[bytes, dict] | None:
    """The bytes that count for a file a profile names with a projection, or None
    to hash it as it is. Approvals go beside the version, never in it."""
    doc = read_jsonc(data)
    if not isinstance(doc, dict):
        return None
    if projection == "approvals":
        if doc:
            sel.approvals.append((sel.key(path), canonical(doc)))
        return b"", {"beside": True}
    if projection == "mcp":
        servers = {k: mcp_without_secrets(v) for k, v in doc.items() if k in MCP_KEYS}
        return canonical(servers), {"keys": sorted(servers)}
    if projection == "claude_hooks":
        hooks = {k: doc[k] for k in ("hooks", "disableAllHooks") if k in doc}
        return canonical(hooks), {"keys": sorted(hooks)}
    if projection == "cursor_cli":
        perms = doc.get("permissions")
        if perms:
            sel.approvals.append((sel.key(path), canonical(perms)))
        harness = {k: doc[k] for k in ("approvalMode", "sandbox") if k in doc}
        return canonical(harness), {"keys": sorted(harness)}
    if projection == "agy_config":
        harness = {k: doc[k] for k in ("plugins",) if k in doc}
        return canonical(harness), {"keys": sorted(harness)}
    if projection == "opencode_config":
        harness = {}
        for key in OPENCODE_HARNESS:
            if key in doc:
                value = doc[key]
                harness[key] = mcp_without_secrets(value) if key == "mcp" else value
        known = set(OPENCODE_HARNESS) | PREFERENCE_KEYS | set(MODEL_KEYS)
        return canonical(without_model(harness)), {
            "keys": sorted(harness),
            "unclassified": sorted(k for k in doc if k not in known),
        }
    harness = project_settings(doc)
    return canonical(harness), {"keys": sorted(harness)}


# ------------------------------------------------------------------- selection
class Selection:
    """The files one run's harness is made of, and what sits beside the version."""

    def __init__(self, program: str, mode: str, root: str | None) -> None:
        self.program = program
        self.mode = mode
        self.root = root
        self.files: list[tuple[str, str, bytes]] = []  # (key, display, bytes)
        self.groups: list[dict] = []  # manifest rows for Home Assistant
        self.approvals: list[tuple[str, bytes]] = []
        self.memory: list[tuple[str, bytes]] = []
        self.missing: list[str] = []
        self._seen: set[str] = set()

    def display(self, path: str) -> str:
        path = os.path.normpath(path)
        home = os.path.normpath(HOME)
        if os.path.normcase(path).startswith(os.path.normcase(home + os.sep)):
            return "~/" + os.path.relpath(path, home).replace(os.sep, "/")
        return path.replace(os.sep, "/")

    def key(self, path: str) -> str:
        """Hash key: project files by their path inside the project, so a clone
        elsewhere hashes the same; home files as ~/; anything else absolute."""
        path = os.path.normpath(path)
        if self.root:
            root = os.path.normpath(self.root)
            if os.path.normcase(path).startswith(os.path.normcase(root + os.sep)):
                return "<project>/" + os.path.relpath(path, root).replace(os.sep, "/")
        return self.display(path)

    def claim(self, path: str) -> bool:
        norm = os.path.normcase(os.path.normpath(os.path.abspath(path)))
        if norm in self._seen:
            return False
        self._seen.add(norm)
        return True

    def add(self, path: str, data: bytes, group: dict | None = None) -> None:
        self.files.append((self.key(path), self.display(path), data))
        if group is not None and len(self.groups) < MAX_MANIFEST:
            self.groups.append(group)

    def version(self, label: str | None) -> str:
        h = hashlib.sha256()
        for key, _, data in sorted(self.files, key=lambda f: f[0]):
            h.update(key.encode())
            h.update(b"\0")
            h.update(hashlib.sha256(data).digest())
        digest = "sha256:" + h.hexdigest()[:12]
        return f"{label} {digest}" if label else digest

    @staticmethod
    def _digest(pairs: list[tuple[str, bytes]]) -> str | None:
        if not pairs:
            return None
        h = hashlib.sha256()
        for name, data in sorted(pairs):
            h.update(name.encode())
            h.update(b"\0")
            h.update(data)
        return "sha256:" + h.hexdigest()[:12]

    def approvals_digest(self) -> str | None:
        return self._digest(self.approvals)

    def memory_digest(self) -> str | None:
        return self._digest(self.memory)

    def manifest(self) -> dict:
        rules = 0
        for _, data in self.approvals:
            doc = _read_json_bytes(data)
            if isinstance(doc, dict):
                rules += sum(len(v) for v in doc.values() if isinstance(v, list))
            else:
                rules += sum(1 for line in data.splitlines() if line.strip())
        return {
            "program": self.program,
            "mode": self.mode,
            "project": self.display(self.root) if self.root else None,
            "files": len(self.files),
            "groups": self.groups,
            "missing": [self.display(p) for p in self.missing][:20],
            "approvals": {"digest": self.approvals_digest(), "rules": rules},
            "memory": [self.display(p) for p, _ in self.memory],
        }


def project_root(cwd: str | None) -> str | None:
    if not cwd:
        return None
    here = os.path.abspath(cwd)
    probe = here
    while True:
        if os.path.exists(os.path.join(probe, ".git")):
            return probe
        parent = os.path.dirname(probe)
        if parent == probe:
            return here
        probe = parent


def ancestors(cwd: str | None, stop: str | None = None) -> list[str]:
    """cwd and every directory above it, nearest first, up to `stop` or the root."""
    if not cwd:
        return []
    out: list[str] = []
    here = os.path.abspath(cwd)
    while True:
        out.append(here)
        if stop and os.path.normcase(here) == os.path.normcase(os.path.abspath(stop)):
            break
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent
    return out


def _walk(root: str) -> list[str]:
    files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS
        )
        for name in sorted(filenames):
            if name.startswith(".") or name.endswith(SKIP_SUFFIXES):
                continue
            files.append(os.path.join(dirpath, name))
            if len(files) >= MAX_FILES:
                return files
    return files


def long_path(path: str) -> str:
    """The path Windows can open. Past 259 characters it needs the \\\\?\\ prefix,
    and Cursor's transcript paths get there with an ordinary project name."""
    if not sys.platform.startswith("win") or len(path) < 250:
        return path
    full = os.path.abspath(path)
    if full.startswith("\\\\"):
        return full
    return "\\\\?\\" + full


def _read(path: str) -> bytes | None:
    try:
        path = long_path(path)
        if os.path.getsize(path) > MAX_FILE_BYTES:
            return None
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def projected(
    path: str, sel: Selection, cwd: str | None, projection: str | None = None
) -> tuple[bytes, dict]:
    """The bytes that count for a recognised file, and its manifest detail."""
    data = _read(path) or b""
    if projection:
        got = project_file(projection, data, path, sel)
        if got is not None:
            return got
    name = os.path.basename(path)
    parent = os.path.basename(os.path.dirname(path))
    norm = path.replace(os.sep, "/")
    detail: dict = {}
    in_claude_home = os.path.normcase(os.path.dirname(os.path.abspath(path))) == (
        os.path.normcase(os.path.abspath(claude_home()))
    )
    if name in ("settings.json", "settings.local.json", "managed-settings.json") and (
        parent == ".claude" or name == "managed-settings.json" or in_claude_home
    ):
        doc = _read_json_bytes(data)
        if isinstance(doc, dict):
            harness, approvals, unclassified = project_claude_settings(
                doc, local=name == "settings.local.json"
            )
            if approvals:
                sel.approvals.append((sel.key(path), canonical(approvals)))
            detail = {"keys": sorted(harness), "unclassified": unclassified}
            return canonical(harness), detail
    if name == ".claude.json" and parent != ".claude":
        doc = _read_json_bytes(data)
        if isinstance(doc, dict):
            harness, approvals = project_claude_json(doc, cwd)
            if approvals:
                sel.approvals.append((sel.key(path), canonical(approvals)))
            return canonical(harness), {"keys": sorted(harness)}
    in_codex_home = os.path.normcase(os.path.dirname(os.path.abspath(path))) == (
        os.path.normcase(os.path.abspath(codex_home()))
    )
    if name == "config.toml" and (
        in_codex_home or "/.codex" in norm or "/codex/" in norm
    ):
        doc = _load_toml(data)
        if doc is not None:
            harness, unclassified = project_codex_config(doc)
            return canonical(harness), {
                "keys": sorted(harness),
                "unclassified": unclassified,
            }
    if name == "installed_plugins.json":
        doc = _read_json_bytes(data)
        if isinstance(doc, dict) and isinstance(doc.get("plugins"), dict):
            keep = {
                plugin: sorted(
                    (
                        str(i.get("scope")),
                        str(i.get("projectPath") or ""),
                        str(i.get("version")),
                        str(i.get("gitCommitSha") or ""),
                    )
                    for i in installs
                    if isinstance(i, dict)
                )
                for plugin, installs in doc["plugins"].items()
                if isinstance(installs, list)
            }
            return canonical(keep), {"keys": ["versions"]}
    if name == ".mcp.json" or name == "managed-mcp.json":
        doc = _read_json_bytes(data)
        if isinstance(doc, dict):
            return canonical(
                {"mcpServers": mcp_without_secrets(doc.get("mcpServers") or {})}
            ), {"keys": ["mcpServers"]}
    if name.endswith(".toml") and parent == "agents":
        doc = _load_toml(data)
        if doc is not None:
            return canonical(without_model(doc)), {"model_ignored": True}
    if name.endswith(".md") and data.startswith(b"---"):
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return data, detail
        stripped = strip_model_lines(text)
        if stripped != text:
            return stripped.encode("utf-8"), {"model_ignored": True}
    return data, detail


def add_path(
    sel: Selection,
    path: str,
    kind: str,
    cwd: str | None,
    report_missing: bool = False,
    exclude: tuple[str, ...] = (),
    projection: str | None = None,
) -> None:
    """Add a file, or every file under a directory, once."""
    path = os.path.abspath(os.path.expanduser(path))
    if os.path.isfile(path):
        if not sel.claim(path) or os.path.basename(path) in exclude:
            return
        data, detail = projected(path, sel, cwd, projection)
        if detail.pop("beside", False):
            return
        sel.add(path, data, {"path": sel.display(path), "kind": kind, **detail})
    elif os.path.isdir(path):
        files = [
            f
            for f in _walk(path)
            if os.path.basename(f) not in exclude and sel.claim(f)
        ]
        if not files:
            return
        model_ignored = False
        for f in files:
            data, detail = projected(f, sel, cwd)
            model_ignored = model_ignored or bool(detail.get("model_ignored"))
            sel.add(f, data)
        group = {"path": sel.display(path) + "/", "kind": kind, "count": len(files)}
        if model_ignored:
            group["model_ignored"] = True
        if len(sel.groups) < MAX_MANIFEST:
            sel.groups.append(group)
    elif report_missing:
        sel.missing.append(path)


SCRIPT_SUFFIXES = (
    ".py", ".sh", ".bash", ".zsh", ".ps1", ".psm1", ".js", ".mjs", ".cjs", ".ts",
    ".rb", ".pl", ".cmd", ".bat", ".lua",
)  # fmt: skip


def hook_scripts(doc: object) -> list[str]:
    """Script files named in hook commands, so an edited hook script moves the
    version. Interpreters and other executables are not scripts and stay out."""
    found: list[str] = []

    def visit(node: object) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k in ("command", "args", "powershell", "bash") and isinstance(
                    v, (str, list)
                ):
                    parts = [v] if isinstance(v, str) else [str(x) for x in v]
                    for part in parts:
                        for token in re.split(r"""[\s"']+""", part):
                            token = os.path.expandvars(os.path.expanduser(token))
                            if token.lower().endswith(
                                SCRIPT_SUFFIXES
                            ) and os.path.isfile(token):
                                found.append(token)
                else:
                    visit(v)
        elif isinstance(node, list):
            for v in node:
                visit(v)

    visit(doc)
    return found


def claude_home() -> str:
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")


def codex_home() -> str:
    return os.environ.get("CODEX_HOME") or os.path.join(HOME, ".codex")


def _managed_claude_dir() -> str:
    if sys.platform.startswith("win"):
        return r"C:\Program Files\ClaudeCode"
    if sys.platform == "darwin":
        return "/Library/Application Support/ClaudeCode"
    return "/etc/claude-code"


def claude_project_dir(cwd: str) -> str:
    """Claude's per-project state directory name: every non-alphanumeric becomes '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(cwd))


def select_claude(cwd: str | None) -> Selection:
    root = project_root(cwd)
    sel = Selection("claude_code", "automatic", root)
    home = claude_home()
    managed = _managed_claude_dir()
    settings_files = [
        os.path.join(managed, "managed-settings.json"),
        os.path.join(home, "settings.json"),
    ]
    project_dirs = []
    for d in (cwd, root):
        if d and d not in project_dirs:
            project_dirs.append(d)
    for d in project_dirs:
        settings_files += [
            os.path.join(d, ".claude", "settings.json"),
            os.path.join(d, ".claude", "settings.local.json"),
        ]
    user_settings = _read_json_bytes(_read(os.path.join(home, "settings.json")) or b"")
    mode = "claude-md-or-agents-md"
    node: object = user_settings
    for part in AGENTS_MD_MODE:
        node = node.get(part) if isinstance(node, dict) else None
    if isinstance(node, str):
        mode = node

    # Rules: managed, user, then CLAUDE.md files on the path; AGENTS.md per the mode.
    add_path(sel, os.path.join(managed, "CLAUDE.md"), "rules", cwd)
    add_path(sel, os.path.join(home, "CLAUDE.md"), "rules", cwd)
    add_path(sel, os.path.join(home, "rules"), "rules", cwd)
    chain = ancestors(cwd)
    claude_md = [
        p
        for d in chain
        for p in (
            os.path.join(d, "CLAUDE.md"),
            os.path.join(d, ".claude", "CLAUDE.md"),
            os.path.join(d, "CLAUDE.local.md"),
        )
        if os.path.isfile(p)
    ]
    for p in claude_md:
        add_path(sel, p, "rules", cwd)
        for imported in claude_imports(p):
            add_path(sel, imported, "rules", cwd)
    if mode == "claude-md-and-agents-md" or (
        mode == "claude-md-or-agents-md" and not claude_md
    ):
        for d in chain:
            add_path(sel, os.path.join(d, "AGENTS.md"), "rules", cwd)
    for d in ancestors(cwd, stop=root):
        add_path(sel, os.path.join(d, ".claude", "rules"), "rules", cwd)

    for path in settings_files:
        add_path(sel, path, "settings", cwd)
        doc = _read_json_bytes(_read(path) or b"") if os.path.isfile(path) else None
        if isinstance(doc, dict):
            for script in hook_scripts(doc.get("hooks") or {}):
                add_path(sel, script, "hooks", cwd)

    for name, kind in (
        ("skills", "skills"),
        ("commands", "commands"),
        ("agents", "agents"),
        ("output-styles", "output styles"),
    ):
        add_path(sel, os.path.join(home, name), kind, cwd)
        for d in ancestors(cwd, stop=root):
            add_path(sel, os.path.join(d, ".claude", name), kind, cwd)
    add_path(
        sel, os.path.join(home, "plugins", "installed_plugins.json"), "plugins", cwd
    )

    claude_json = (
        os.path.join(home, ".claude.json")
        if os.environ.get("CLAUDE_CONFIG_DIR")
        else os.path.join(HOME, ".claude.json")
    )
    add_path(sel, claude_json, "mcp", cwd)
    add_path(sel, os.path.join(managed, "managed-mcp.json"), "mcp", cwd)
    for d in project_dirs:
        add_path(sel, os.path.join(d, ".mcp.json"), "mcp", cwd)

    if cwd:
        memory = os.path.join(
            home, "projects", claude_project_dir(cwd), "memory", "MEMORY.md"
        )
        data = _read(memory)
        if data is not None:
            sel.memory.append((memory, data))
    return sel


IMPORT_RE = re.compile(r"(?<![\w`])@((?:~|\.{1,2})?[/\\]?[\w./\\-]+\.\w+)")


def claude_imports(path: str, depth: int = 0) -> list[str]:
    """Files a CLAUDE.md pulls in with @path, followed up to four hops."""
    if depth >= 4:
        return []
    data = _read(path)
    if data is None:
        return []
    text = re.sub(r"```.*?```", "", data.decode("utf-8", "replace"), flags=re.S)
    text = re.sub(r"`[^`\n]*`", "", text)
    found: list[str] = []
    for match in IMPORT_RE.finditer(text):
        target = os.path.expanduser(match.group(1))
        if not os.path.isabs(target):
            target = os.path.join(os.path.dirname(path), target)
        if os.path.isfile(target):
            found.append(os.path.abspath(target))
            found.extend(claude_imports(target, depth + 1))
    return found


def _codex_trusted(config: dict | None, path: str) -> bool:
    projects = (config or {}).get("projects") or {}
    want = os.path.normcase(os.path.normpath(path))
    for key, spec in projects.items():
        if os.path.normcase(os.path.normpath(key)) == want and isinstance(spec, dict):
            return spec.get("trust_level") == "trusted"
    return False


def select_codex(cwd: str | None) -> Selection:
    root = project_root(cwd)
    sel = Selection("codex", "automatic", root)
    home = codex_home()
    config_path = os.path.join(home, "config.toml")
    config = _load_toml(_read(config_path) or b"")
    override = os.path.join(home, "AGENTS.override.md")
    add_path(
        sel,
        override if os.path.isfile(override) else os.path.join(home, "AGENTS.md"),
        "rules",
        cwd,
    )
    fallbacks = [
        str(x) for x in ((config or {}).get("project_doc_fallback_filenames") or [])
    ]
    for d in reversed(ancestors(cwd, stop=root)):
        for name in ("AGENTS.override.md", "AGENTS.md", *fallbacks):
            candidate = os.path.join(d, name)
            if os.path.isfile(candidate):
                add_path(sel, candidate, "rules", cwd)
                break

    add_path(sel, config_path, "settings", cwd)
    if not sys.platform.startswith("win"):
        add_path(sel, "/etc/codex/config.toml", "settings", cwd)
    hooks_doc = _read_json_bytes(_read(os.path.join(home, "hooks.json")) or b"")
    add_path(sel, os.path.join(home, "hooks.json"), "hooks", cwd)
    for script in hook_scripts(hooks_doc) + hook_scripts((config or {}).get("hooks")):
        add_path(sel, script, "hooks", cwd)
    trusted = [d for d in ancestors(cwd, stop=root) if _codex_trusted(config, d)]
    for d in trusted:
        add_path(sel, os.path.join(d, ".codex", "config.toml"), "settings", cwd)
        add_path(sel, os.path.join(d, ".codex", "hooks.json"), "hooks", cwd)
        add_path(
            sel,
            os.path.join(d, ".codex", "rules"),
            "rules",
            cwd,
            exclude=("default.rules",),
        )
        add_path(sel, os.path.join(d, ".codex", "agents"), "agents", cwd)

    rules_dir = os.path.join(home, "rules")
    add_path(sel, rules_dir, "rules", cwd, exclude=("default.rules",))
    saved = os.path.join(rules_dir, "default.rules")
    data = _read(saved)
    if data is not None:
        rules = sorted(
            line.strip()
            for line in data.decode("utf-8", "replace").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
        sel.approvals.append((sel.key(saved), "\n".join(rules).encode()))

    for d in ancestors(cwd, stop=root):
        add_path(sel, os.path.join(d, ".agents", "skills"), "skills", cwd)
    add_path(sel, os.path.join(HOME, ".agents", "skills"), "skills", cwd)
    if not sys.platform.startswith("win"):
        add_path(sel, "/etc/codex/skills", "skills", cwd)
    add_path(sel, os.path.join(home, "prompts"), "commands", cwd)
    add_path(sel, os.path.join(home, "agents"), "agents", cwd)
    return sel


def copilot_home() -> str:
    return os.environ.get("COPILOT_HOME") or os.path.join(HOME, ".copilot")


def cline_home() -> str:
    return os.environ.get("CLINE_DIR") or os.path.join(HOME, ".cline")


def cline_data() -> str:
    return os.environ.get("CLINE_DATA_DIR") or os.path.join(cline_home(), "data")


def _xdg(kind: str, *fallback: str) -> str:
    return os.environ.get(f"XDG_{kind}_HOME") or os.path.join(HOME, *fallback)


# OpenCode and Kilo: (config and data directory, project directory, config stem).
OPENCODE_FAMILY = {
    "opencode": ("opencode", ".opencode", "opencode"),
    "kilo_code": ("kilo", ".kilo", "kilo"),
}


def opencode_config_dir(program: str) -> str:
    return os.path.join(_xdg("CONFIG", ".config"), OPENCODE_FAMILY[program][0])


def opencode_db(program: str) -> str:
    name = OPENCODE_FAMILY[program][0]
    return os.path.join(_xdg("DATA", ".local", "share"), name, f"{name}.db")


def program_home(program: str) -> str:
    return {
        "copilot_cli": copilot_home,
        "cursor": lambda: os.path.join(HOME, ".cursor"),
        "antigravity": lambda: os.path.join(HOME, ".gemini"),
        "cline": cline_home,
        "opencode": lambda: opencode_config_dir("opencode"),
        "kilo_code": lambda: opencode_config_dir("kilo_code"),
    }[program]()


# Each row: (kind, where, path or first-of paths, projection). `project` is every
# directory from the working directory up to the project root; `home` is the
# agent's own directory; `user` the home directory; `data` Cline's data directory.
PROFILES: dict[str, tuple[tuple[str, str, str | tuple[str, ...], str | None], ...]] = {
    "copilot_cli": (
        ("rules", "project", "AGENTS.md", None),
        ("rules", "project", "CLAUDE.md", None),
        ("rules", "project", "GEMINI.md", None),
        ("rules", "project", ".github/copilot-instructions.md", None),
        ("rules", "project", ".github/instructions", None),
        ("rules", "project", ".claude/rules", None),
        ("rules", "home", "copilot-instructions.md", None),
        ("rules", "home", "instructions", None),
        ("agents", "project", ".github/agents", None),
        ("agents", "home", "agents", None),
        ("skills", "project", ".github/skills", None),
        ("skills", "project", ".claude/skills", None),
        ("skills", "project", ".agents/skills", None),
        ("skills", "home", "skills", None),
        ("skills", "user", ".agents/skills", None),
        ("hooks", "home", "hooks", None),
        ("hooks", "project", ".github/hooks", None),
        ("settings", "home", "settings.json", "settings"),
        ("settings", "project", ".github/copilot/settings.json", "settings"),
        ("settings", "project", ".github/copilot/settings.local.json", "settings"),
        ("hooks", "project", ".claude/settings.json", "claude_hooks"),
        ("hooks", "project", ".claude/settings.local.json", "claude_hooks"),
        ("mcp", "home", "mcp-config.json", "mcp"),
        ("mcp", "project", ".mcp.json", "mcp"),
        ("mcp", "project", ".github/mcp.json", "mcp"),
        ("approvals", "home", "permissions-config.json", "approvals"),
    ),
    "cursor": (
        ("rules", "project", "AGENTS.md", None),
        ("rules", "project", "CLAUDE.md", None),
        ("rules", "project", ".cursorrules", None),
        ("rules", "project", ".cursor/rules", None),
        ("rules", "home", "rules", None),
        ("commands", "project", ".cursor/commands", None),
        ("commands", "home", "commands", None),
        ("skills", "project", ".cursor/skills", None),
        ("skills", "project", ".agents/skills", None),
        ("skills", "project", ".claude/skills", None),
        ("skills", "project", ".codex/skills", None),
        ("skills", "home", "skills", None),
        ("skills", "user", ".agents/skills", None),
        ("skills", "user", ".claude/skills", None),
        ("skills", "user", ".codex/skills", None),
        ("agents", "project", ".cursor/agents", None),
        ("agents", "project", ".claude/agents", None),
        ("agents", "project", ".codex/agents", None),
        ("agents", "home", "agents", None),
        ("agents", "user", ".claude/agents", None),
        ("agents", "user", ".codex/agents", None),
        ("hooks", "project", ".cursor/hooks.json", None),
        ("hooks", "home", "hooks.json", None),
        # Cursor runs Claude Code's hooks too.
        ("hooks", "user", ".claude/settings.json", "claude_hooks"),
        ("hooks", "project", ".claude/settings.json", "claude_hooks"),
        ("mcp", "home", "mcp.json", "mcp"),
        ("mcp", "project", ".cursor/mcp.json", "mcp"),
        ("settings", "home", "cli-config.json", "cursor_cli"),
        ("settings", "home", "permissions.json", "settings"),
        ("settings", "home", "sandbox.json", "settings"),
    ),
    "antigravity": (
        ("rules", "project", "AGENTS.md", None),
        ("rules", "project", "GEMINI.md", None),
        ("rules", "project", ".agents/rules", None),
        ("rules", "home", "GEMINI.md", None),
        ("rules", "home", "AGENTS.md", None),
        ("rules", "home", "config/rules", None),
        ("skills", "project", ".agents/skills", None),
        ("skills", "home", "config/skills", None),
        ("agents", "project", ".agents/agents", None),
        ("agents", "home", "config/agents", None),
        ("hooks", "project", ".agents/hooks.json", None),
        ("hooks", "home", "config/hooks.json", None),
        ("settings", "home", "config/config.json", "agy_config"),
        ("mcp", "home", "config/mcp_config.json", "mcp"),
    ),
    "cline": (
        ("hooks", "project", ".clinerules/hooks", None),
        ("hooks", "project", ".cline/hooks", None),
        ("hooks", "home", "hooks", None),
        ("hooks", "user", "Documents/Cline/Hooks", None),
        ("commands", "project", ".clinerules/workflows", None),
        ("commands", "project", ".cline/workflows", None),
        ("commands", "home", "workflows", None),
        ("commands", "user", "Documents/Cline/Workflows", None),
        ("skills", "project", ".clinerules/skills", None),
        ("skills", "project", ".cline/skills", None),
        ("skills", "project", ".agents/skills", None),
        ("skills", "home", "skills", None),
        ("skills", "user", ".agents/skills", None),
        ("rules", "project", "AGENTS.md", None),
        ("rules", "project", ".clinerules", None),
        ("rules", "project", ".cline/rules", None),
        ("rules", "project", ".cursorrules", None),
        ("rules", "project", ".windsurfrules", None),
        ("rules", "home", "rules", None),
        ("rules", "user", "Documents/Cline/Rules", None),
        ("rules", "user", ".agents/AGENTS.md", None),
        ("agents", "project", ".cline/agents", None),
        ("agents", "home", "agents", None),
        ("mcp", "data", "settings/cline_mcp_settings.json", "mcp"),
        ("settings", "data", "settings/global-settings.json", "settings"),
    ),
    "opencode": (
        ("rules", "project", ("AGENTS.md", "CLAUDE.md"), None),
        ("rules", "home", ("AGENTS.md", "~/.claude/CLAUDE.md"), None),
        ("settings", "home", ("opencode.jsonc", "opencode.json"), "opencode_config"),
        ("settings", "project", ("opencode.jsonc", "opencode.json"), "opencode_config"),
        (
            "settings",
            "project",
            (".opencode/opencode.jsonc", ".opencode/opencode.json"),
            "opencode_config",
        ),
        ("agents", "project", ".opencode/agents", None),
        ("agents", "project", ".opencode/agent", None),
        ("agents", "home", "agents", None),
        ("agents", "home", "agent", None),
        ("commands", "project", ".opencode/commands", None),
        ("commands", "project", ".opencode/command", None),
        ("commands", "home", "commands", None),
        ("commands", "home", "command", None),
        ("skills", "project", ".opencode/skills", None),
        ("skills", "project", ".claude/skills", None),
        ("skills", "project", ".agents/skills", None),
        ("skills", "home", "skills", None),
        ("skills", "user", ".claude/skills", None),
        ("skills", "user", ".agents/skills", None),
        ("plugins", "project", ".opencode/plugins", None),
        ("plugins", "project", ".opencode/plugin", None),
        ("plugins", "home", "plugins", None),
        ("plugins", "home", "plugin", None),
    ),
    "kilo_code": (
        ("rules", "project", ("AGENTS.md", "CLAUDE.md"), None),
        ("rules", "project", ".kilocode/rules", None),
        ("rules", "home", "AGENTS.md", None),
        ("settings", "home", ("kilo.jsonc", "kilo.json"), "opencode_config"),
        ("settings", "project", ("kilo.jsonc", "kilo.json"), "opencode_config"),
        (
            "settings",
            "project",
            (".kilo/kilo.jsonc", ".kilo/kilo.json"),
            "opencode_config",
        ),
        ("agents", "project", ".kilo/agents", None),
        ("agents", "project", ".kilo/agent", None),
        ("agents", "home", "agents", None),
        ("agents", "home", "agent", None),
        ("commands", "project", ".kilo/commands", None),
        ("commands", "home", "commands", None),
        ("skills", "project", ".kilo/skills", None),
        ("skills", "project", ".claude/skills", None),
        ("skills", "project", ".agents/skills", None),
        ("skills", "home", "skills", None),
        ("skills", "user", ".agents/skills", None),
        ("plugins", "project", ".kilo/plugins", None),
        ("plugins", "home", "plugins", None),
    ),
}


def _profile_bases(
    where: str, program: str, cwd: str | None, root: str | None
) -> list[str]:
    if where == "project":
        return ancestors(cwd, stop=root)
    if where == "home":
        return [program_home(program)]
    if where == "data":
        return [cline_data()]
    return [HOME]


def _add_hook_scripts(sel: Selection, path: str, cwd: str | None) -> None:
    """Scripts named by the hook definitions in a JSON file or a folder of them."""
    files = (
        [path] if os.path.isfile(path) else _walk(path) if os.path.isdir(path) else []
    )
    for f in files:
        if f.endswith(".json"):
            doc = read_jsonc(_read(f) or b"")
            hooks = doc.get("hooks", doc) if isinstance(doc, dict) else doc
            for script in hook_scripts(hooks):
                add_path(sel, script, "hooks", cwd)


def _add_instructions(
    sel: Selection, program: str, cwd: str | None, root: str | None
) -> None:
    """Files an OpenCode or Kilo config names under `instructions`."""
    stem, project_dir = OPENCODE_FAMILY[program][2], OPENCODE_FAMILY[program][1]
    configs = [
        (program_home(program), f"{stem}.jsonc"),
        (program_home(program), f"{stem}.json"),
    ]
    for d in ancestors(cwd, stop=root):
        for name in (f"{stem}.jsonc", f"{stem}.json"):
            configs += [(d, name), (d, os.path.join(project_dir, name))]
    for base, name in configs:
        doc = read_jsonc(_read(os.path.join(base, name)) or b"")
        entries = doc.get("instructions") if isinstance(doc, dict) else None
        for entry in entries if isinstance(entries, list) else []:
            pattern = os.path.expanduser(str(entry))
            if not os.path.isabs(pattern):
                pattern = os.path.join(base, pattern)
            for found in sorted(glob.glob(pattern, recursive=True))[:MAX_MANIFEST]:
                add_path(sel, found, "rules", cwd)


def select_profile(program: str, cwd: str | None) -> Selection:
    """The files this agent program loads, from its profile. A row with several
    paths takes the first that exists in each place."""
    root = project_root(cwd)
    sel = Selection(program, "automatic", root)
    for kind, where, paths, projection in PROFILES[program]:
        candidates = paths if isinstance(paths, tuple) else (paths,)
        for base in _profile_bases(where, program, cwd, root):
            for candidate in candidates:
                full = (
                    os.path.expanduser(candidate)
                    if candidate.startswith("~")
                    else os.path.join(base, candidate)
                )
                if not os.path.exists(full):
                    continue
                add_path(sel, full, kind, cwd, projection=projection)
                if kind == "hooks":
                    _add_hook_scripts(sel, full, cwd)
                break
    if program in OPENCODE_FAMILY:
        _add_instructions(sel, program, cwd, root)
    return sel


def select_manual(paths: list[str], cwd: str | None, program: str) -> Selection:
    """A person's list: ~ expanded, relative paths resolved against the project root."""
    root = project_root(cwd)
    sel = Selection(program, "manual", root)
    base = root or os.getcwd()
    for raw in paths:
        raw = str(raw).strip()
        if not raw:
            continue
        path = os.path.expanduser(raw)
        if not os.path.isabs(path):
            path = os.path.join(base, path)
        add_path(sel, path, "listed", cwd, report_missing=True)
    return sel


def select(settings: dict, local: dict, cwd: str | None, program: str) -> Selection:
    """Home Assistant's choice first. The 0.3 local `harness` list stands in for
    a manual list Home Assistant has not been given, and for a Home Assistant
    that has no settings to give."""
    mode = settings.get("selection")
    files = [str(p) for p in (settings.get("harness_files") or []) if str(p).strip()]
    legacy = [str(p) for p in (local.get("harness") or []) if str(p).strip()]
    automatic = program in AUTOMATIC
    if mode == "manual" or (not mode and legacy):
        return select_manual(files or legacy, cwd, program)
    if automatic:
        if program == "claude_code":
            return select_claude(cwd)
        if program == "codex":
            return select_codex(cwd)
        return select_profile(program, cwd)
    return select_manual(legacy, cwd, program)


def describe_selection(sel: Selection) -> list[str]:
    """The selection as lines for a terminal."""
    name = PROGRAM_NAMES.get(sel.program, sel.program)
    how = (
        "selected automatically" if sel.mode == "automatic" else "from the manual list"
    )
    lines = [
        f"Harness files for {name}, {how}"
        + (f", project {sel.display(sel.root)}" if sel.root else "")
        + ":"
    ]
    for g in sel.groups:
        extra = []
        if g.get("count"):
            extra.append(f"{g['count']} files")
        if g.get("keys"):
            extra.append("keys " + ", ".join(g["keys"]))
        if g.get("unclassified"):
            extra.append("unclassified, not hashed: " + ", ".join(g["unclassified"]))
        if g.get("model_ignored"):
            extra.append("model and effort lines ignored")
        lines.append(
            f"  {g['kind']:<13} {g['path']}"
            + (f"  ({'; '.join(extra)})" if extra else "")
        )
    for path in sel.missing:
        lines.append(f"  missing       {sel.display(path)}")
    if not sel.groups and not sel.missing:
        lines.append("  none")
    beside = []
    if sel.approvals:
        beside.append(f"saved approvals {sel.approvals_digest()}")
    if sel.memory:
        beside.append("memory " + ", ".join(sel.display(p) for p, _ in sel.memory))
    if beside:
        lines.append("Tracked beside the version, not in it: " + "; ".join(beside))
    return lines


# ------------------------------------------------------------------- config & http
def _read_json(path: str, default: dict) -> dict:
    """The file's JSON object, or `default` when it is missing or malformed.

    Two clauses on purpose: under this repository's py314 target ruff's
    formatter rewrites `except (A, B):` into the 3.14-only `except A, B:`, and
    this file runs under whatever Python runs the agent's hooks.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except OSError:
        return default
    except ValueError:
        return default
    return doc if isinstance(doc, dict) else default


def load_config() -> dict:
    return _read_json(CONFIG, {})


def agent_config(cfg: dict, program: str) -> dict:
    """The webhook and TLS choice for one agent program. The 0.3 layout's single
    top-level webhook was Claude Code's: no other program's runs go there."""
    agents = cfg.get("agents") if isinstance(cfg.get("agents"), dict) else {}
    own = agents.get(program)
    if isinstance(own, dict) and own.get("webhook_url"):
        return own
    return cfg if program in ("claude_code", "other") else {}


def http(
    method: str, url: str, body: dict | None, insecure: bool, timeout: float = 30
) -> tuple[int, str]:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    ctx = ssl._create_unverified_context() if insecure else None
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode("utf-8", "replace")
    except urllib.error.URLError as err:
        return 0, str(err.reason)
    except OSError as err:
        return 0, str(err)


def post(cfg: dict, run: dict) -> tuple[int, str]:
    url = cfg.get("webhook_url")
    if not url:
        return 0, "no webhook_url in " + CONFIG
    return http("POST", url, run, bool(cfg.get("insecure")))


def settings_path(program: str) -> str:
    return os.path.join(STATE_DIR, f"settings-{program}.json")


def fetch_settings(cfg: dict, program: str, timeout: float = 10) -> dict | None:
    """Home Assistant's settings for this agent, cached for the hook's later turns."""
    url = cfg.get("webhook_url")
    if not url:
        return None
    status, body = http("GET", url, None, bool(cfg.get("insecure")), timeout)
    if status != 200:
        return None
    doc = _read_json_bytes(body.encode())
    if not isinstance(doc, dict):
        return None
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(settings_path(program), "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    return doc


def cached_settings(cfg: dict, program: str) -> dict:
    cached = _read_json(settings_path(program), {})
    if cached:
        return cached
    return fetch_settings(cfg, program, timeout=5) or {}


def label_for(settings: dict, local: dict) -> str | None:
    return str(settings.get("version_label") or local.get("label") or "") or None


# ------------------------------------------------------------------- verdicts
OUTCOMES = ("pass", "fail", "partial")
STALE_PENDING = timedelta(hours=6)
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit", "apply_patch"}
WRITE_SUFFIXES = ("write_file", "edit_block", "create_file", "update_file")
SHELL_TOOLS = {"Bash", "PowerShell", "shell", "shell_command", "exec_command", "exec"}
PUSH_RE = re.compile(r"\bgit\b[^\n|;&]*\bpush\b|\bgh\s+(?:pr|release)\s+create\b", re.I)
# The agent's line, anywhere in its final message; the last one counts.
SELF_VERDICT_RE = re.compile(
    r"^\s*Verdict:\s*(?P<outcome>pass|fail|partial)\b(?P<rest>[^\n]*)", re.I | re.M
)
# The person's whole prompt: a bare word, or the long form with overrides.
HUMAN_WORD_RE = re.compile(r"^\s*/?(?P<outcome>pass|fail|partial)\s*$", re.I)
HUMAN_LONG_RE = re.compile(
    r"^\s*/?verdict\s+(?P<outcome>pass|fail|partial)\b(?P<rest>.*)$", re.I | re.S
)


def _split_rest(rest: str) -> tuple[dict, str]:
    """`task=` and `class=` out of a verdict's trailing words; the rest is notes.
    The words verified/unverified and --verified are flags, not notes. Each may
    sit in brackets, as the documented form `[task=<id>]` shows them."""
    overrides: dict = {}
    words: list[str] = []
    for tok in rest.split():
        bare = tok.strip("()[],.;")
        low = bare.lower()
        if low.startswith("task=") and len(low) > 5:
            overrides["task_id"] = bare.split("=", 1)[1]
        elif low.startswith("class=") and len(low) > 6:
            overrides["task_class"] = bare.split("=", 1)[1]
        elif low in ("verified", "unverified", "--verified"):
            overrides["claimed_verified"] = low == "verified" or low == "--verified"
        else:
            words.append(tok)
    return overrides, " ".join(words).strip()


def self_verdict(text: str) -> dict | None:
    """The agent's verdict line in its final message, or None. A partial or fail
    on a reply that ends in a question is marked `asks` (ADVICE)."""
    matches = list(SELF_VERDICT_RE.finditer(text or ""))
    if not matches:
        return None
    m = matches[-1]
    overrides, notes = _split_rest(m.group("rest") or "")
    v = {"outcome": m.group("outcome").lower(), "notes": notes, **overrides}
    if v["outcome"] != "pass" and ends_asking(text):
        v["asks"] = True
    return v


def ends_asking(text: str) -> bool:
    """Whether a reply ends waiting on the person: a question among its last six
    lines (the question, then its numbered options)."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return any(ln.endswith("?") for ln in lines[-6:])


# Three of five runs on bda4a44a98bc (2026-10-07) were partials put on questions
# about the span's own unfinished work, each finished in the next span.
ADVICE = (
    "harness-ledger: that {outcome} verdict sat on a reply ending in a question; a "
    "question about the span's own unfinished work carries no verdict, and the span "
    "stays open until the work lands (AGENTS.md)"
)
ADVICE_LOG = "advice.log"


def with_advice(message: str | None, pending: dict) -> str | None:
    """The posted run's line, then the advice held with it."""
    if not message or not pending.get("advice"):
        return message
    return f"{message}\n{pending['advice']}"


def human_verdict(text: str) -> dict | None:
    """The person's verdict, from a bare word or the long form, or None."""
    m = HUMAN_WORD_RE.match(text or "")
    if m:
        return {"outcome": m.group("outcome").lower(), "notes": ""}
    m = HUMAN_LONG_RE.match(text or "")
    if not m:
        return None
    overrides, notes = _split_rest(m.group("rest") or "")
    return {"outcome": m.group("outcome").lower(), "notes": notes, **overrides}


def verdict_text(payload: dict) -> str:
    """What the person typed. UserPromptSubmit carries the prompt; a Claude custom
    command reaches UserPromptExpansion as command_name plus arguments."""
    if payload.get("hook_event_name") == "UserPromptExpansion":
        name = str(payload.get("command_name") or "").lstrip("/").lower()
        if name not in ("verdict", *OUTCOMES):
            return ""
        return f"/{name} {payload.get('arguments') or ''}"
    return str(payload.get("prompt") or "")


# ------------------------------------------------------------- client identity
CLAUDE_EVENTS = {
    "Stop",
    "SubagentStop",
    "UserPromptSubmit",
    "UserPromptExpansion",
    "SessionStart",
    "SessionEnd",
}
CODEX_EVENTS = {
    "Stop",
    "SubagentStop",
    "UserPromptSubmit",
    "SessionStart",
    "SessionEnd",
}
# Fields other agents put in payloads they send to hooks they borrow from Claude.
FOREIGN_FIELDS = (
    "cursor_version",
    "conversation_id",
    "generation_id",
    "workspace_roots",
    "sessionId",
    "transcriptPath",
    "clineVersion",
    "conversationId",
)


def _under(path: str, base: str) -> bool:
    path = os.path.normcase(os.path.abspath(path))
    base = os.path.normcase(os.path.abspath(base))
    return path.startswith(base + os.sep)


def identify(payload: object) -> str | None:
    """The client that sent a hook payload, or None when it cannot be told apart.

    Positive identification only: the event is one the client sends and the
    transcript lives in that client's own session store. Cursor, Copilot CLI
    and Continue run Claude's hooks with their own payloads and transcripts.
    """
    if not isinstance(payload, dict) or any(f in payload for f in FOREIGN_FIELDS):
        return None
    event = payload.get("hook_event_name")
    if not isinstance(event, str) or not isinstance(payload.get("session_id"), str):
        return None
    transcript = payload.get("transcript_path")
    if not isinstance(transcript, str) or not transcript:
        return None
    if event in CLAUDE_EVENTS and _under(
        transcript, os.path.join(claude_home(), "projects")
    ):
        return "claude_code"
    if event in CODEX_EVENTS and (
        _under(transcript, os.path.join(codex_home(), "sessions"))
        or _under(transcript, os.path.join(codex_home(), "archived_sessions"))
    ):
        return "codex"
    return None


# The other agents: each registers its hook with --hook <program> [<event>], and
# the payload must still have that agent's own shape and point into its own store.
SESSION_ID_RE = re.compile(r"^[\w.-]{4,120}$")
COPILOT_EVENTS = {
    "agentStop": "Stop",
    "subagentStop": "SubagentStop",
    "userPromptSubmitted": "UserPromptSubmit",
    "sessionStart": "SessionStart",
    "sessionEnd": "SessionEnd",
}
CURSOR_EVENTS = {
    "stop": "Stop",
    "sessionEnd": "SessionEnd",
    "beforeSubmitPrompt": "UserPromptSubmit",
    "sessionStart": "SessionStart",
}
AGY_BRAINS = ("antigravity-cli", "antigravity")


def _sid(value: object) -> str | None:
    return value if isinstance(value, str) and SESSION_ID_RE.match(value) else None


def _first(values: object) -> str | None:
    return str(values[0]) if isinstance(values, list) and values else None


def _copilot_event(payload: dict, event: str | None) -> dict | None:
    name = COPILOT_EVENTS.get(event or "")
    sid = _sid(payload.get("sessionId"))
    if not name or not sid or "hook_event_name" in payload:
        return None
    if not isinstance(payload.get("timestamp"), (int, float)):
        return None
    transcript = os.path.join(copilot_home(), "session-state", sid, "events.jsonl")
    given = payload.get("transcriptPath")
    if given and os.path.normcase(os.path.abspath(str(given))) != os.path.normcase(
        os.path.abspath(transcript)
    ):
        return None
    if not os.path.isfile(transcript):
        return None
    return {
        "hook_event_name": name,
        "session_id": sid,
        "transcript_path": transcript,
        "cwd": payload.get("cwd"),
        "prompt": payload.get("prompt") if name == "UserPromptSubmit" else None,
        "reply": "",
    }


def _cursor_event(payload: dict, event: str | None) -> dict | None:
    raw = payload.get("hook_event_name")
    name = CURSOR_EVENTS.get(str(raw))
    sid = _sid(payload.get("conversation_id"))
    if not name or not sid or not payload.get("cursor_version"):
        return None
    if event and event != raw:
        return None
    transcript = payload.get("transcript_path")
    if transcript and not _under(
        str(transcript), os.path.join(HOME, ".cursor", "projects")
    ):
        return None
    if name in ("Stop", "SessionEnd") and not (
        transcript and os.path.isfile(long_path(str(transcript)))
    ):
        return None
    ev = {
        "hook_event_name": name,
        "session_id": sid,
        "transcript_path": transcript,
        "cwd": payload.get("cwd") or _first(payload.get("workspace_roots")),
        "prompt": payload.get("prompt") if name == "UserPromptSubmit" else None,
        "model": payload.get("model"),
        "client_version": str(payload["cursor_version"]),
        # Cursor reads each hook's answer as JSON.
        "reply": '{"continue": true}' if name == "UserPromptSubmit" else "{}",
    }
    if name == "SessionEnd":
        # Print mode fires no stop: the session's end closes its only turn.
        ev["end_as_stop"] = True
        if isinstance(payload.get("duration_ms"), (int, float)):
            ev["duration_s"] = payload["duration_ms"] / 1000
    return ev


def _agy_event(payload: dict, event: str | None) -> dict | None:
    if event not in ("Stop", "SessionStart") or "hook_event_name" in payload:
        return None
    sid = _sid(payload.get("conversationId"))
    transcript = payload.get("transcriptPath")
    if not sid or not isinstance(transcript, str):
        return None
    brains = [os.path.join(HOME, ".gemini", b, "brain") for b in AGY_BRAINS]
    if not any(_under(transcript, b) for b in brains):
        return None
    if event == "Stop" and not os.path.isfile(transcript):
        return None
    return {
        "hook_event_name": event,
        "session_id": sid,
        "transcript_path": transcript,
        "cwd": _first(payload.get("workspacePaths")),
        "model": payload.get("modelName"),
        "reply": "",
    }


def _cline_event(payload: dict, event: str | None) -> dict | None:
    if (
        event != "TaskComplete"
        or "clineVersion" not in payload
        or not payload.get("taskId")
    ):
        return None
    context = (
        payload.get("sessionContext")
        if isinstance(payload.get("sessionContext"), dict)
        else {}
    )
    root = _sid(context.get("rootSessionId"))
    if not root:
        return None
    transcript = os.path.join(cline_data(), "sessions", root, f"{root}.messages.json")
    if not os.path.isfile(transcript):
        return None
    info = (
        payload.get("workspaceInfo")
        if isinstance(payload.get("workspaceInfo"), dict)
        else {}
    )
    turn = payload.get("turn") if isinstance(payload.get("turn"), dict) else {}
    return {
        "hook_event_name": "SubagentStop" if payload.get("parent_agent_id") else "Stop",
        "session_id": root,
        "transcript_path": transcript,
        "cwd": info.get("rootPath") or _first(payload.get("workspaceRoots")),
        "last_assistant_message": turn.get("outputText"),
        "client_version": payload.get("clineVersion") or None,
        "reply": "{}",
    }


def _opencode_event(payload: dict, event: str | None, program: str) -> dict | None:
    """OpenCode and Kilo run the tracker's own plugin, which sends this payload."""
    name = payload.get("hook_event_name")
    sid = _sid(payload.get("session_id"))
    if payload.get("source") != "ha-harness-tracker" or not sid:
        return None
    if name not in ("Stop", "UserPromptSubmit", "SessionStart"):
        return None
    db = opencode_db(program)
    if not os.path.isfile(db):
        return None
    ev = {
        "hook_event_name": name,
        "session_id": sid,
        "transcript_path": db,
        "transcript_session": sid,
        "offset_key": f"{db}#{sid}",
        "cwd": payload.get("cwd"),
        "prompt": payload.get("prompt") if name == "UserPromptSubmit" else None,
        "reply": "",
    }
    parent = _opencode_parent(db, sid)
    if parent:
        # A subagent's session: its figures join the session that started it.
        ev["session_id"] = parent
        if name == "Stop":
            ev["hook_event_name"] = "SubagentStop"
    return ev


def _opencode_parent(db: str, sid: str) -> str | None:
    """The top session above `sid`, or None when `sid` is itself a top session."""
    try:
        import sqlite3
        import urllib.request as _url

        con = sqlite3.connect(
            "file:" + _url.pathname2url(os.path.abspath(db)) + "?mode=ro",
            uri=True,
            timeout=5,
        )
    except Exception:
        return None
    top = None
    try:
        current = sid
        for _ in range(10):
            row = con.execute(
                "select parent_id from session where id = ?", (current,)
            ).fetchone()
            if not row or not row[0]:
                break
            current = top = str(row[0])
    except Exception:
        return None
    finally:
        con.close()
    return top


def normalize(
    payload: object, program: str | None = None, event: str | None = None
) -> dict | None:
    """A hook payload as one event shape, or None when it does not positively come
    from the named agent. Claude Code and Codex are told apart by the payload alone."""
    if not isinstance(payload, dict):
        return None
    if program in (None, "claude_code", "codex"):
        found = identify(payload)
        if found is None or (program and found != program):
            return None
        return {**payload, "program": found}
    if program == "copilot_cli":
        ev = _copilot_event(payload, event)
    elif program == "cursor":
        ev = _cursor_event(payload, event)
    elif program == "antigravity":
        ev = _agy_event(payload, event)
    elif program == "cline":
        ev = _cline_event(payload, event)
    elif program in OPENCODE_FAMILY:
        ev = _opencode_event(payload, event, program)
    else:
        ev = None
    return {**ev, "program": program} if ev else None


# ----------------------------------------------------------- claude transcript
def denial_class(block: dict) -> str | None:
    """Why a Claude tool call was refused, or None when it was not.

    Only an error result that opens with a refusal counts: a result that merely
    contains the words, such as a read of a guard hook's source, is not a denial.
    """
    if not block.get("is_error"):
        return None
    content = block.get("content")
    if isinstance(content, list):
        content = " ".join(
            str(b.get("text", "")) for b in content if isinstance(b, dict)
        )
    text = str(content or "").lstrip()
    if text.startswith("Permission for this action was denied"):
        m = re.search(r"Reason: \[([^\]\n]{1,60})\]", text)
        return f"classifier:{m.group(1)}" if m else "classifier:unexplained"
    if text.startswith("PreToolUse:") and "BLOCKED --" in text.split("\n", 1)[0]:
        m = re.search(r"claude-hooks/([\w.-]{1,60}?)\.py", text)
        return f"hook:{m.group(1)}" if m else "hook:unnamed"
    if text.startswith("The user doesn't want to proceed with this tool use"):
        return "person"
    if text.startswith("Permission to use") and "denied" in text[:200]:
        return "settings"
    return None


# User-role text the client writes itself, never a person's prompt.
INJECTED = (
    "<task-notification>",
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<local-command",
    "<system-reminder>",
    "This session is being continued from a previous conversation",
)


def injected(entry: dict, text: str) -> bool:
    if entry.get("isMeta") or entry.get("isCompactSummary"):
        return True
    return text.lstrip().startswith(INJECTED)


def _ts(entry: dict) -> datetime | None:
    raw = entry.get("timestamp")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


# Other agents' tool names, compared in lower case.
WRITE_TOOLS_ANY_CASE = {
    "edit",
    "write",
    "multiedit",
    "patch",
    "create",
    "str_replace",
    "strreplace",
    "edit_file",
    "write_to_file",
    "replace_in_file",
    "replace_file_content",
    "multi_replace_file_content",
}
SHELL_TOOLS_ANY_CASE = {
    "bash",
    "shell",
    "powershell",
    "run_command",
    "run_commands",
    "run_in_terminal",
}


def _classify_tool(name: str, command: str) -> tuple[int, int]:
    """(writes, pushes) contributed by one tool call."""
    low = name.lower()
    if (
        name in WRITE_TOOLS
        or name.endswith(WRITE_SUFFIXES)
        or low in WRITE_TOOLS_ANY_CASE
    ):
        return 1, 0
    shell = (
        name in SHELL_TOOLS
        or name.endswith("PowerShell")
        or low in SHELL_TOOLS_ANY_CASE
    )
    if shell and PUSH_RE.search(command):
        return 0, 1
    return 0, 0


def _figures() -> dict:
    return {
        "tool_calls": 0,
        "writes": 0,
        "pushes": 0,
        "denials": 0,
        "denial_classes": {},
        "input_tokens": 0,
        "output_tokens": 0,
        "human_prompts": 0,
        "first_prompt": "",
        "self_verdict": None,
        "first_ts": None,
        "last_ts": None,
        "api_calls": 0,
        "model": None,
        "effort": None,
        "client_version": None,
    }


def _whole_lines(path: str, offset: int) -> tuple[bytes, int] | None:
    with open(long_path(path), "rb") as fh:
        fh.seek(offset)
        data = fh.read()
    # Only whole lines: a line still being written is left for the next call.
    cut = data.rfind(b"\n")
    if cut < 0:
        return None
    return data[: cut + 1], offset + cut + 1


def parse_slice(path: str, offset: int) -> tuple[dict, int]:
    """Figures for the Claude transcript bytes past `offset`, and the new offset.

    Tokens are counted once per requestId. A human prompt that is itself a
    verdict is not counted: it closes a span rather than starting work.
    """
    figures = _figures()
    got = _whole_lines(path, offset)
    if got is None:
        return figures, offset
    data, new_offset = got
    models: dict[str, int] = {}
    seen_requests: set[str] = set()
    first: datetime | None = None
    last: datetime | None = None
    last_text = ""
    for raw in data.split(b"\n"):
        if not raw.strip():
            continue
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        t = _ts(entry)
        if t:
            first = first or t
            last = t
        kind = entry.get("type")
        if entry.get("version"):
            figures["client_version"] = str(entry["version"])
        message = entry.get("message") or {}
        content = message.get("content")
        if kind == "assistant":
            name = str(message.get("model") or "")
            # "<synthetic>" marks messages the client wrote itself, not a model.
            if name and not name.startswith("<"):
                models[name] = models.get(name, 0) + 1
            blocks = (
                [b for b in content if isinstance(b, dict)]
                if isinstance(content, list)
                else []
            )
            for b in blocks:
                if b.get("type") == "tool_use":
                    figures["tool_calls"] += 1
                    inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                    command = str(inp.get("command") or inp.get("cmd") or "")
                    w, p = _classify_tool(str(b.get("name") or ""), command)
                    figures["writes"] += w
                    figures["pushes"] += p
                elif b.get("type") == "text" and str(b.get("text") or "").strip():
                    last_text = str(b.get("text"))
            rid = entry.get("requestId")
            if rid and rid not in seen_requests:
                seen_requests.add(rid)
                figures["api_calls"] += 1
                usage = message.get("usage") or {}
                iterations = usage.get("iterations") or [usage]
                for it in iterations:
                    # Everything the model read: the uncached slice alone was
                    # 6 k tokens against 633 M cache reads over one session.
                    figures["input_tokens"] += sum(
                        int(it.get(k) or 0)
                        for k in (
                            "input_tokens",
                            "cache_creation_input_tokens",
                            "cache_read_input_tokens",
                        )
                    )
                    figures["output_tokens"] += int(it.get("output_tokens") or 0)
        elif kind == "user":
            text = None
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                kinds = {b.get("type") for b in content if isinstance(b, dict)}
                if "tool_result" in kinds:
                    for b in content:
                        why = denial_class(b) if isinstance(b, dict) else None
                        if why:
                            figures["denials"] += 1
                            classes = figures["denial_classes"]
                            classes[why] = classes.get(why, 0) + 1
                elif "text" in kinds:
                    text = " ".join(
                        str(b.get("text", "")) for b in content if isinstance(b, dict)
                    )
            if (
                text is not None
                and not injected(entry, text)
                and human_verdict(text) is None
            ):
                figures["human_prompts"] += 1
                if not figures["first_prompt"]:
                    figures["first_prompt"] = " ".join(text.split())[:120]
    figures["self_verdict"] = self_verdict(last_text)
    if models:
        figures["model"] = min(models, key=lambda m: (-models[m], m))
    figures["first_ts"] = first.isoformat() if first else None
    figures["last_ts"] = last.isoformat() if last else None
    return figures, new_offset


def subagent_dir(transcript: str) -> str:
    """Claude Code keeps a session's subagent transcripts in <session>/subagents/."""
    return os.path.join(os.path.splitext(transcript)[0], "subagents")


def subagent_tokens(
    transcript: str, state: dict, seed: bool = False
) -> tuple[int, int, int]:
    """Input and output tokens the session's subagents used since the last read,
    and the responses they came from. The parent transcript never carries them.

    One response per message id (agent-console's accounting rule): every line of
    a response repeats its usage, so the first line counts and the rest do not,
    including lines that arrive in the next read. Tool calls stay the parent's.
    seed starts every existing file at its end: a session already running when
    this was installed has history no single run should carry.
    """
    folder = subagent_dir(transcript)
    try:
        names = sorted(n for n in os.listdir(long_path(folder)) if n.endswith(".jsonl"))
    except OSError:
        names = []
    offsets = state.setdefault("offsets", {})
    last_ids = state.setdefault("subagent_last_id", {})
    tokens_in = tokens_out = responses = 0
    for name in names:
        path = os.path.join(folder, name)
        if seed:
            with contextlib.suppress(OSError):
                offsets[path] = os.path.getsize(long_path(path))
            continue
        got = _whole_lines(path, int(offsets.get(path, 0)))
        if got is None:
            continue
        data, offsets[path] = got
        seen = {last_ids[path]} if last_ids.get(path) else set()
        for raw in data.split(b"\n"):
            if b'"assistant"' not in raw:
                continue
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            message = entry.get("message") or {}
            mid = message.get("id")
            if entry.get("type") != "assistant" or not mid or mid in seen:
                continue
            seen.add(mid)
            last_ids[path] = mid
            if str(message.get("model") or "").startswith("<"):
                continue
            responses += 1
            usage = message.get("usage") or {}
            for it in usage.get("iterations") or [usage]:
                tokens_in += sum(
                    int(it.get(k) or 0)
                    for k in (
                        "input_tokens",
                        "cache_creation_input_tokens",
                        "cache_read_input_tokens",
                    )
                )
                tokens_out += int(it.get("output_tokens") or 0)
    return tokens_in, tokens_out, responses


# ------------------------------------------------------------ codex rollout
CODEX_REFUSAL = "This action was rejected"


def parse_codex_slice(path: str, offset: int) -> tuple[dict, int]:
    """Figures for the Codex rollout bytes past `offset`, and the new offset.

    Tool calls are function and custom tool calls; tokens come from
    token_usage_record, once per response id; the model and effort from
    turn_context. Prompts are counted by the prompt hook, not here: the
    rollout's user messages mix typed text with injected context. Denials are
    the approval reviewer's refusals; Codex writes no other denial to the rollout.
    """
    figures = _figures()
    got = _whole_lines(path, offset)
    if got is None:
        return figures, offset
    data, new_offset = got
    seen: set[str] = set()
    records = running_totals = 0
    first: datetime | None = None
    last: datetime | None = None
    last_text = ""
    for raw in data.split(b"\n"):
        if not raw.strip():
            continue
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        t = _ts(entry)
        if t:
            first = first or t
            last = t
        kind = entry.get("type")
        p = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
        sub = p.get("type")
        if kind == "token_usage_record":
            records += 1
        elif kind == "event_msg" and sub == "token_count":
            running_totals += 1
        if kind == "session_meta" and p.get("cli_version"):
            figures["client_version"] = str(p["cli_version"])
        elif kind == "turn_context":
            if p.get("model"):
                figures["model"] = str(p["model"])
            if p.get("effort"):
                figures["effort"] = str(p["effort"])
        elif kind == "token_usage_record":
            rid = str(p.get("response_id") or "")
            usage = p.get("usage") if isinstance(p.get("usage"), dict) else {}
            if rid and rid in seen:
                continue
            seen.add(rid)
            figures["api_calls"] += 1
            figures["input_tokens"] += int(usage.get("input_tokens") or 0)
            figures["output_tokens"] += int(usage.get("output_tokens") or 0)
        elif kind == "response_item" and sub in (
            "function_call",
            "custom_tool_call",
            "local_shell_call",
            "web_search_call",
        ):
            figures["tool_calls"] += 1
            name = str(p.get("name") or "")
            args = p.get("arguments") if sub == "function_call" else p.get("input")
            w, push = _classify_tool(name, str(args or ""))
            figures["writes"] += w
            figures["pushes"] += push
        elif kind == "response_item" and sub in (
            "function_call_output",
            "custom_tool_call_output",
        ):
            out = p.get("output")
            text = out if isinstance(out, str) else json.dumps(out)
            if text.lstrip().startswith(CODEX_REFUSAL):
                figures["denials"] += 1
                classes = figures["denial_classes"]
                classes["reviewer"] = classes.get("reviewer", 0) + 1
        elif (
            kind == "response_item"
            and sub == "message"
            and p.get("role") == "assistant"
        ):
            for c in p.get("content") or []:
                if isinstance(c, dict) and str(c.get("text") or "").strip():
                    last_text = str(c.get("text"))
        elif (
            kind == "event_msg"
            and sub == "task_complete"
            and p.get("last_agent_message")
        ):
            last_text = str(p["last_agent_message"])
    if running_totals and not records:
        # A Codex that writes running totals without per-response records: the
        # usage is unknown, which is not zero.
        figures["input_tokens"] = figures["output_tokens"] = None
    figures["self_verdict"] = self_verdict(last_text)
    figures["first_ts"] = first.isoformat() if first else None
    figures["last_ts"] = last.isoformat() if last else None
    return figures, new_offset


# ------------------------------------------------------ other agents' transcripts
def _jsonl(data: bytes) -> list[dict]:
    rows = []
    for raw in data.split(b"\n"):
        if raw.strip():
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _prompt(figures: dict, text: str) -> None:
    """Count a typed prompt; a verdict word closes a span rather than starting work."""
    text = " ".join(text.split())
    if not text or human_verdict(text) is not None:
        return
    figures["human_prompts"] += 1
    if not figures["first_prompt"]:
        figures["first_prompt"] = text[:120]


def _tool(figures: dict, name: str, command: str) -> None:
    figures["tool_calls"] += 1
    w, p = _classify_tool(name, command)
    figures["writes"] += w
    figures["pushes"] += p


def _deny(figures: dict, why: str) -> None:
    figures["denials"] = int(figures["denials"] or 0) + 1
    classes = figures["denial_classes"]
    classes[why] = classes.get(why, 0) + 1


def _span(figures: dict, first: datetime | None, last: datetime | None) -> None:
    figures["first_ts"] = first.isoformat() if first else None
    figures["last_ts"] = last.isoformat() if last else None


def _model_of(models: dict[str, int]) -> str | None:
    return min(models, key=lambda m: (-models[m], m)) if models else None


def _token_count(details: dict, name: str) -> int:
    node = details.get(name)
    return int(node.get("tokenCount") or 0) if isinstance(node, dict) else 0


def parse_copilot_slice(path: str, offset: int) -> tuple[dict, int]:
    """GitHub Copilot's session events (the CLI's events.jsonl; VS Code writes the
    same schema). Tokens appear only in the shutdown record, after the last hook."""
    figures = _figures()
    figures["input_tokens"] = figures["output_tokens"] = None
    got = _whole_lines(path, offset)
    if got is None:
        return figures, offset
    data, new_offset = got
    models: dict[str, int] = {}
    denied: dict[str, str] = {}
    first = last = None
    last_text = ""
    for entry in _jsonl(data):
        t = _ts(entry)
        if t:
            first = first or t
            last = t
        kind = entry.get("type")
        d = entry.get("data") if isinstance(entry.get("data"), dict) else {}
        if kind == "session.start" and d.get("copilotVersion"):
            figures["client_version"] = str(d["copilotVersion"])
        elif kind == "user.message":
            reasoning = d.get("responsesReasoning")
            if isinstance(reasoning, dict) and reasoning.get("effort"):
                figures["effort"] = str(reasoning["effort"])
            _prompt(figures, str(d.get("content") or ""))
        elif kind == "assistant.message":
            if d.get("model"):
                models[str(d["model"])] = models.get(str(d["model"]), 0) + 1
            if str(d.get("content") or "").strip():
                last_text = str(d["content"])
        elif kind == "tool.execution_start":
            args = d.get("arguments") if isinstance(d.get("arguments"), dict) else {}
            _tool(figures, str(d.get("toolName") or ""), str(args.get("command") or ""))
        elif kind == "permission.completed":
            result = d.get("result") if isinstance(d.get("result"), dict) else {}
            if str(result.get("kind") or "").startswith("denied"):
                denied[str(d.get("toolCallId"))] = str(result["kind"])[:60]
        elif kind == "tool.execution_complete" and d.get("success") is False:
            error = d.get("error") if isinstance(d.get("error"), dict) else {}
            if error.get("code") == "denied":
                why = denied.get(str(d.get("toolCallId")), "denied")
                _deny(figures, f"permission:{why}")
        elif kind == "session.shutdown":
            details = (
                d.get("tokenDetails") if isinstance(d.get("tokenDetails"), dict) else {}
            )
            figures["input_tokens"] = int(figures["input_tokens"] or 0) + sum(
                _token_count(details, n) for n in ("input", "cache_read", "cache_write")
            )
            figures["output_tokens"] = int(figures["output_tokens"] or 0) + (
                _token_count(details, "output")
            )
    figures["model"] = _model_of(models)
    figures["self_verdict"] = self_verdict(last_text)
    _span(figures, first, last)
    return figures, new_offset


def parse_cursor_slice(path: str, offset: int) -> tuple[dict, int]:
    """Cursor's agent transcript: messages and tool calls, with no timestamps,
    tokens or tool results; the model and duration come from the hook."""
    figures = _figures()
    figures["input_tokens"] = figures["output_tokens"] = figures["denials"] = None
    got = _whole_lines(path, offset)
    if got is None:
        return figures, offset
    data, new_offset = got
    last_text = ""
    for entry in _jsonl(data):
        message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
        blocks = [b for b in message.get("content") or [] if isinstance(b, dict)]
        if entry.get("role") == "user":
            text = " ".join(
                str(b.get("text") or "") for b in blocks if b.get("type") == "text"
            )
            _prompt(
                figures,
                re.sub(
                    r"<timestamp>.*?</timestamp>|</?user_query>", "", text, flags=re.S
                ),
            )
        elif entry.get("role") == "assistant":
            figures["api_calls"] += 1
            for b in blocks:
                if b.get("type") == "tool_use":
                    inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                    _tool(
                        figures, str(b.get("name") or ""), str(inp.get("command") or "")
                    )
                elif b.get("type") == "text" and str(b.get("text") or "").strip():
                    last_text = str(b["text"])
    figures["self_verdict"] = self_verdict(last_text)
    return figures, new_offset


def parse_agy_slice(path: str, offset: int) -> tuple[dict, int]:
    """Antigravity's transcript steps: user input, planner responses and their
    tool calls. No tokens; the model comes from the hook."""
    figures = _figures()
    figures["input_tokens"] = figures["output_tokens"] = figures["denials"] = None
    got = _whole_lines(path, offset)
    if got is None:
        return figures, offset
    data, new_offset = got
    first = last = None
    last_text = ""
    for entry in _jsonl(data):
        t = _ts({"timestamp": entry.get("created_at")})
        if t:
            first = first or t
            last = t
        kind = entry.get("type")
        if kind == "USER_INPUT" and entry.get("source") == "USER_EXPLICIT":
            _prompt(
                figures,
                re.sub(r"</?USER_REQUEST>", "", str(entry.get("content") or "")),
            )
        elif kind == "PLANNER_RESPONSE":
            for call in entry.get("tool_calls") or []:
                if isinstance(call, dict):
                    args = (
                        call.get("args") if isinstance(call.get("args"), dict) else {}
                    )
                    _tool(
                        figures,
                        str(call.get("name") or ""),
                        str(args.get("CommandLine") or ""),
                    )
            if str(entry.get("content") or "").strip():
                last_text = str(entry["content"])
    figures["self_verdict"] = self_verdict(last_text)
    _span(figures, first, last)
    return figures, new_offset


def parse_cline(path: str, offset: int) -> tuple[dict, int]:
    """Cline's session messages file, rewritten whole on each change: the offset
    is the number of messages already counted."""
    figures = _figures()
    figures["denials"] = None
    doc = _read_json_bytes(_read(path) or b"")
    messages = doc.get("messages") if isinstance(doc, dict) else None
    if not isinstance(messages, list) or len(messages) < offset:
        return figures, offset
    origin = doc.get("origin") if isinstance(doc.get("origin"), dict) else {}
    if origin.get("version"):
        figures["client_version"] = str(origin["version"])
    models: dict[str, int] = {}
    first = last = None
    last_text = ""
    for m in messages[offset:]:
        if not isinstance(m, dict):
            continue
        if isinstance(m.get("ts"), (int, float)):
            t = datetime.fromtimestamp(m["ts"] / 1000, tz=UTC_ZONE)
            first = first or t
            last = t
        blocks = [b for b in m.get("content") or [] if isinstance(b, dict)]
        if m.get("role") == "user" and not any(
            b.get("type") == "tool_result" for b in blocks
        ):
            text = " ".join(
                str(b.get("text") or "") for b in blocks if b.get("type") == "text"
            )
            _prompt(figures, re.sub(r"</?user_input[^>]*>", "", text))
        elif m.get("role") == "assistant":
            info = m.get("modelInfo") if isinstance(m.get("modelInfo"), dict) else {}
            if info.get("id"):
                models[str(info["id"])] = models.get(str(info["id"]), 0) + 1
            metrics = m.get("metrics") if isinstance(m.get("metrics"), dict) else {}
            figures["api_calls"] += 1
            figures["input_tokens"] += sum(
                int(metrics.get(k) or 0)
                for k in ("inputTokens", "cacheReadTokens", "cacheWriteTokens")
            )
            figures["output_tokens"] += int(metrics.get("outputTokens") or 0)
            for b in blocks:
                if b.get("type") == "tool_use":
                    inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                    commands = inp.get("commands") or inp.get("command") or ""
                    _tool(figures, str(b.get("name") or ""), json.dumps(commands))
                elif b.get("type") == "text" and str(b.get("text") or "").strip():
                    last_text = str(b["text"])
    figures["model"] = _model_of(models)
    figures["self_verdict"] = self_verdict(last_text)
    _span(figures, first, last)
    return figures, len(messages)


OPENCODE_DENIED = ("DeniedError", "RejectedError", "rejected permission")


def parse_opencode(path: str, session_id: str, offset: int) -> tuple[dict, int]:
    """One OpenCode or Kilo session in the agent's SQLite store, read-only. The
    offset is the creation time, in milliseconds, of the last message counted."""
    figures = _figures()
    try:
        import sqlite3
        import urllib.request as _url

        uri = "file:" + _url.pathname2url(os.path.abspath(path)) + "?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=5)
    except Exception:  # no sqlite3 module, or no database
        return figures, offset
    try:
        session = con.execute(
            "select version from session where id = ?", (session_id,)
        ).fetchone()
        rows = con.execute(
            "select id, time_created, data from message where session_id = ? "
            "and time_created > ? order by time_created",
            (session_id, offset),
        ).fetchall()
        parts: dict[str, list[dict]] = {}
        if rows:
            for mid, data in con.execute(
                "select message_id, data from part where session_id = ? "
                "and message_id in (select id from message where session_id = ? "
                "and time_created > ?) order by time_created",
                (session_id, session_id, offset),
            ):
                doc = _read_json_bytes(str(data).encode())
                if isinstance(doc, dict):
                    parts.setdefault(str(mid), []).append(doc)
    except Exception:  # locked or a schema this file does not know
        return figures, offset
    finally:
        con.close()
    if session and session[0]:
        figures["client_version"] = str(session[0])
    models: dict[str, int] = {}
    first = last = None
    last_text = ""
    new_offset = offset
    for mid, created, data in rows:
        new_offset = max(new_offset, int(created))
        t = datetime.fromtimestamp(int(created) / 1000, tz=UTC_ZONE)
        first = first or t
        last = t
        doc = _read_json_bytes(str(data).encode())
        doc = doc if isinstance(doc, dict) else {}
        own = parts.get(str(mid), [])
        if doc.get("role") == "user":
            text = " ".join(
                str(p.get("text") or "")
                for p in own
                if p.get("type") == "text" and not p.get("synthetic")
            ).strip()
            if len(text) > 1 and text[0] == text[-1] == '"':
                text = text[1:-1]
            _prompt(figures, text)
        elif doc.get("role") == "assistant":
            if doc.get("modelID"):
                models[str(doc["modelID"])] = models.get(str(doc["modelID"]), 0) + 1
            tokens = doc.get("tokens") if isinstance(doc.get("tokens"), dict) else {}
            cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
            figures["api_calls"] += 1
            figures["input_tokens"] += int(tokens.get("input") or 0) + sum(
                int(cache.get(k) or 0) for k in ("read", "write")
            )
            figures["output_tokens"] += int(tokens.get("output") or 0) + int(
                tokens.get("reasoning") or 0
            )
            for p in own:
                if p.get("type") == "tool":
                    state = p.get("state") if isinstance(p.get("state"), dict) else {}
                    inp = (
                        state.get("input")
                        if isinstance(state.get("input"), dict)
                        else {}
                    )
                    _tool(
                        figures, str(p.get("tool") or ""), str(inp.get("command") or "")
                    )
                    if state.get("status") == "error" and any(
                        s in str(state.get("error") or "") for s in OPENCODE_DENIED
                    ):
                        _deny(figures, "permission")
                elif p.get("type") == "text" and str(p.get("text") or "").strip():
                    last_text = str(p["text"])
    figures["model"] = _model_of(models)
    figures["self_verdict"] = self_verdict(last_text)
    _span(figures, first, last)
    return figures, new_offset


def parse_event(ev: dict, offset: int) -> tuple[dict, int]:
    """The figures for one hook event's slice of its agent's transcript."""
    program, path = ev["program"], ev["transcript_path"]
    if program == "codex":
        return parse_codex_slice(path, offset)
    if program == "copilot_cli":
        return parse_copilot_slice(path, offset)
    if program == "cursor":
        return parse_cursor_slice(path, offset)
    if program == "antigravity":
        return parse_agy_slice(path, offset)
    if program == "cline":
        return parse_cline(path, offset)
    if program in OPENCODE_FAMILY:
        return parse_opencode(path, ev["transcript_session"], offset)
    return parse_slice(path, offset)


# ----------------------------------------------------------------------- state
def paths_for(session_id: str) -> tuple[str, str]:
    os.makedirs(STATE_DIR, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", session_id or "unknown")
    return os.path.join(STATE_DIR, f"{safe}.jsonl"), os.path.join(
        STATE_DIR, f"{safe}.state.json"
    )


def load_state(state_path: str) -> dict:
    return _read_json(state_path, {"offsets": {}, "span_start_line": 0})


def save_state(state_path: str, state: dict) -> None:
    tmp = state_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    os.replace(tmp, state_path)


def open_span(ledger_path: str, state: dict) -> list[dict]:
    lines: list[dict] = []
    try:
        with open(ledger_path, encoding="utf-8") as fh:
            for i, raw in enumerate(fh):
                if i < int(state.get("span_start_line", 0)):
                    continue
                try:
                    lines.append(json.loads(raw))
                except ValueError:
                    continue
    except OSError:
        pass
    return lines


def write_manifest(version: str, sel: Selection) -> None:
    """versions/<digest>.json once: each file's own hash, so two versions can be
    diffed by name instead of read as opaque ids."""
    digest = version.split(":")[-1]
    out_dir = os.path.join(STATE_DIR, "versions")
    out = os.path.join(out_dir, f"{digest}.json")
    if os.path.exists(out):
        return
    os.makedirs(out_dir, exist_ok=True)
    doc = {
        "version": version,
        "schema": SCHEMA,
        "recorded": datetime.now().astimezone().isoformat(timespec="seconds"),
        "program": sel.program,
        "mode": sel.mode,
        "files": {
            key: hashlib.sha256(data).hexdigest()[:12] for key, _, data in sel.files
        },
    }
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, sort_keys=True)


# --------------------------------------------------------------------- roll-up
def roll_up(lines: list[dict], verdict: dict, by_person: bool) -> dict:
    """One run from the span's ledger lines and a verdict.

    The task id is `<directory>:<class>` unless the verdict names one; class
    is publish if the span pushed, build if it wrote, ops otherwise.
    """
    human = sum(int(x.get("human_prompts", 0)) for x in lines)
    versions = [x.get("harness_version") for x in lines if x.get("harness_version")]
    version = versions[0] if versions else "unknown"
    pushes = sum(int(x.get("pushes", 0)) for x in lines)
    writes = sum(int(x.get("writes", 0)) for x in lines)
    task_class = verdict.get("task_class") or (
        "publish" if pushes else "build" if writes else "ops"
    )
    cwd = next((x.get("cwd") for x in lines if x.get("cwd")), None)
    where = os.path.basename(os.path.normpath(cwd)) if cwd else "session"
    task_id = verdict.get("task_id") or f"{where}:{task_class}"
    first_prompt = next(
        (x.get("first_prompt") for x in lines if x.get("first_prompt")), ""
    )
    who = "confirmed by hand" if by_person else "self-reported"
    if not by_person and "claimed_verified" in verdict:
        who += ", verified by effect" if verdict["claimed_verified"] else ", unverified"
    parts = [who]
    if verdict.get("notes"):
        parts.append(verdict["notes"])
    if first_prompt:
        parts.append(f"prompt: {first_prompt}")
    if len(set(versions)) > 1:
        parts.append("harness changed during the task")
    # A sum over known and unknown token readings is a floor, and says so.
    if any(x.get("input_tokens", 0) is None for x in lines) and any(
        isinstance(x.get("input_tokens"), (int, float)) for x in lines
    ):
        parts.append("token counts are a floor: part of the span had no usage records")
    run: dict = {
        "harness_version": version,
        "outcome": verdict["outcome"],
        "verified": by_person,
        "task_id": task_id,
        "task_class": task_class,
        "turns": sum(int(x.get("turns", 0)) for x in lines),
        "tool_calls": sum(int(x.get("tool_calls", 0)) for x in lines),
        "duration_s": round(sum(float(x.get("duration_s", 0)) for x in lines), 1),
        "interventions": max(0, human - 1),
        "notes": "; ".join(parts)[:500],
        "fingerprint_schema": SCHEMA,
    }
    # An agent that does not record tokens or denials leaves them out, not zero.
    for field in ("input_tokens", "output_tokens", "denials"):
        known = [x[field] for x in lines if isinstance(x.get(field), (int, float))]
        if known:
            run[field] = int(sum(known))
    classes: dict[str, int] = {}
    for x in lines:
        for name, n in (x.get("denial_classes") or {}).items():
            classes[str(name)[:80]] = classes.get(str(name)[:80], 0) + int(n)
    if classes:
        top = sorted(classes.items(), key=lambda kv: (-kv[1], kv[0]))[:20]
        run["denial_classes"] = dict(top)
    # Kept out of the harness version: a model change must not read as a harness change.
    for field in ("model", "effort"):
        values = [str(x[field]) for x in lines if x.get(field)]
        if values:
            run[field] = min(set(values), key=lambda m: (-values.count(m), m))
    for field in ("client_version", "client", "approvals", "memory"):
        values = [str(x[field]) for x in lines if x.get(field)]
        if values:
            run[field] = values[-1]
    manifests = [x["harness_manifest"] for x in lines if x.get("harness_manifest")]
    if manifests:
        run["harness_manifest"] = manifests[-1]
    first = lines[0] if lines else {}
    ident = (
        f"{first.get('session_id', '')}:{first.get('seq', '')}:{first.get('at', '')}"
    )
    run["run_key"] = hashlib.sha256(
        f"{run.get('client', '')}:{ident}".encode()
    ).hexdigest()[:24]
    return run


def unattributable(run: dict) -> str | None:
    """Why a run must not be recorded, or None. A run whose harness version
    could not be read would be filed under "unknown" and pool unrelated work into
    one line of the trend."""
    if str(run.get("harness_version") or "unknown") == "unknown":
        return (
            "harness-ledger: NOT recorded: the harness version could not be read, "
            "so the run cannot be attributed; dropped rather than filed under unknown"
        )
    return None


def send(cfg: dict, run: dict) -> tuple[int, str]:
    """Post a run and keep the tracker's answer for the next session start."""
    status, body = post(cfg, run)
    if 200 <= status < 300:
        save_reply(body, str(run.get("client") or ""))
    return status, body


def describe(run: dict, status: int, body: str) -> str:
    """One line for the conversation about a post that succeeded or failed."""
    if 200 <= status < 300:
        answer = _read_json_bytes(body.encode())
        answer = answer if isinstance(answer, dict) else {}
        if answer.get("duplicate"):
            return f"harness-ledger: run {run['task_id']} was already recorded"
        who = "confirmed" if run.get("verified") else "self-reported"
        return (
            f"harness-ledger: recorded {who} run {run['task_id']} as "
            f"{run['outcome']} on {run['harness_version']} - {run['turns']} turns, "
            f"{run['tool_calls']} tool calls, "
            + (f"{run['denials']} denials, " if "denials" in run else "")
            + f"{run['interventions']} interventions; tracker now at "
            f"{answer.get('run_count')} runs, {rate_text(answer)}, "
            f"regressed={answer.get('regressed')}"
        )
    return (
        f"harness-ledger: NOT recorded ({status} {body[:160]}); "
        "the run is kept for a retry"
    )


def rate_text(answer: dict) -> str:
    """The pass rate with the sample behind it. A rate on one or two runs of a
    fresh harness version is noise, and printed bare it read as a verdict."""
    rate = f"pass rate {answer.get('pass_rate')}%"
    runs = answer.get("current_runs")
    if runs is None:
        return rate
    text = f"this harness version: {rate} over {runs} run{'' if runs == 1 else 's'}"
    if answer.get("confirmed") is False:
        text += " (unconfirmed)"
    if answer.get("model_changed"):
        text += ", model changed since the baseline"
    return text


def post_pending(cfg: dict, state: dict, state_path: str) -> str | None:
    """Post the session's pending run, if any. Keeps it on failure."""
    pending = state.get("pending")
    if not pending:
        return None
    refused = unattributable(pending["run"])
    if refused:
        state.pop("pending", None)
        save_state(state_path, state)
        return refused
    status, body = send(cfg, pending["run"])
    if 200 <= status < 300:
        state.pop("pending", None)
        save_state(state_path, state)
        return with_advice(describe(pending["run"], status, body), pending)
    return describe(pending["run"], status, body)


def reply_path(program: str = "") -> str:
    if program and program != "claude_code":
        return os.path.join(STATE_DIR, f"last-reply-{program}.json")
    return os.path.join(STATE_DIR, "last-reply.json")


def save_reply(body: str, program: str) -> None:
    """Keep the tracker's last answer: the session-start line is read from it."""
    answer = _read_json_bytes(body.encode())
    if isinstance(answer, dict) and not answer.get("duplicate"):
        answer["saved_at"] = datetime.now().astimezone().isoformat(timespec="minutes")
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(reply_path(program), "w", encoding="utf-8") as fh:
            json.dump(answer, fh)


def loop_line(program: str = "claude_code") -> str | None:
    """The window gate and the recurring denial classes, from the tracker's last
    answer. A regression or a recurring class is the session's first input."""
    answer = _read_json(reply_path(program), {})
    w = answer.get("window") or {}
    if not w.get("runs"):
        return None
    head = f"last {w['runs']} runs passed {w.get('pass_rate')}%"
    if w.get("prior_pass_rate") is not None:
        head += f" against {w['prior_pass_rate']}% in the {w.get('size')} before"
        # The gate compares shared tasks only; raw rates over two task mixes mislead.
        if w.get("improvement") is not None:
            n = w.get("shared_tasks")
            head += (
                f", {w['improvement']:+} points over the {n} task"
                f"{'' if n == 1 else 's'} both ran"
            )
        else:
            head += ", no task in both, so no pass-rate comparison"
    if w.get("denials_per_100_calls") is not None:
        parts = [head, f"denials per 100 tool calls {w['denials_per_100_calls']}"]
        if w.get("prior_denials_per_100_calls") is not None:
            parts[-1] += f" against {w['prior_denials_per_100_calls']}"
    else:
        parts = [head, f"denials per run {w.get('denials_per_run')}"]
    if w.get("regressed"):
        parts.append(
            f"THE WINDOW GATE REGRESSED across {w.get('versions')} harness versions: "
            "find the change that caused it, then fix or revert it"
        )
    recurring = w.get("recurring_denials") or []
    if recurring:
        parts.append(
            "recurring denials "
            + ", ".join(f"{name} x{n}" for name, n in recurring[:3])
            + ": a second occurrence becomes a hook, skill or rule change"
        )
    return f"harness loop ({answer.get('saved_at')}): " + "; ".join(parts)


def flush_stale(cfg: dict, current_state_path: str, force: bool = False) -> int:
    """Post pending runs left behind by other sessions, once old enough."""
    posted = 0
    for path in glob.glob(os.path.join(STATE_DIR, "*.state.json")):
        if os.path.normcase(path) == os.path.normcase(current_state_path):
            continue
        state = load_state(path)
        pending = state.get("pending")
        if not pending:
            continue
        created = _ts({"timestamp": pending.get("created")})
        now = datetime.now().astimezone()
        if not force and created and now - created < STALE_PENDING:
            continue
        program = str(pending["run"].get("client") or "claude_code")
        line = post_pending(agent_config(cfg, program), state, path)
        if line and "NOT recorded" not in line:
            posted += 1
    return posted


# ---------------------------------------------------------------------- events
def on_stop(
    payload: dict,
    cfg: dict,
    turns: int,
    program: str | None = None,
    tail: bool = False,
) -> dict | None:
    """Append one ledger line; hold a run when the agent gave a verdict, and post
    it at once for an agent that has no prompt hook to confirm it with.
    turns is 1 for the main agent, 0 for a subagent. tail marks what a session's
    end finds past the last stop: its verdict counts, and nothing is written
    when it holds no activity."""
    program = program or payload.get("program") or "claude_code"
    ev = {**payload, "program": program}
    transcript = payload.get("transcript_path")
    if not transcript or not os.path.isfile(long_path(str(transcript))):
        return None
    ledger_path, state_path = paths_for(str(payload.get("session_id")))
    state = load_state(state_path)
    key = str(payload.get("offset_key") or transcript)
    offset = int(state["offsets"].get(key, 0))
    figures, new_offset = parse_event(ev, offset)
    if new_offset == offset and (turns == 0 or tail):
        return None
    if payload.get("hook_event_name") == "Stop":
        state["stopped"] = True
    active = any(
        figures[k] for k in ("tool_calls", "human_prompts", "api_calls", "self_verdict")
    )
    if tail and not active:
        # Cursor writes its turn_ended record at the session's end, after the last stop.
        state["offsets"][key] = new_offset
        save_state(state_path, state)
        return None
    for field in ("model", "effort", "client_version"):
        if not figures.get(field) and payload.get(field):
            figures[field] = str(payload[field])
    seed = "subagent_last_id" not in state and offset > 0
    sub_in, sub_out, sub_responses = (
        subagent_tokens(str(transcript), state, seed)
        if program == "claude_code"
        else (0, 0, 0)
    )
    if sub_responses:
        figures["input_tokens"] += sub_in
        figures["output_tokens"] += sub_out
    if program == "codex":
        figures["human_prompts"] = int(state.pop("prompts", 0))
        figures["first_prompt"] = str(state.pop("first_prompt", ""))
    if turns and not figures["self_verdict"] and payload.get("last_assistant_message"):
        figures["self_verdict"] = self_verdict(str(payload["last_assistant_message"]))
    duration = float(payload.get("duration_s") or 0.0)
    if figures["first_ts"] and figures["last_ts"]:
        a = datetime.fromisoformat(figures["first_ts"])
        b = datetime.fromisoformat(figures["last_ts"])
        duration = max(0.0, (b - a).total_seconds())
    verdict = figures["self_verdict"] if (turns or tail) else None
    agent_cfg = agent_config(cfg, program)
    settings = cached_settings(agent_cfg, program)
    sel = select(settings, cfg, payload.get("cwd"), program)
    version = sel.version(label_for(settings, cfg)) if sel.files else "unknown"
    if sel.files:
        write_manifest(version, sel)
    state["seq"] = int(state.get("seq", 0)) + 1
    line = {
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "seq": state["seq"],
        "session_id": payload.get("session_id"),
        "event": payload.get("hook_event_name"),
        "client": program,
        "turns": turns,
        "tool_calls": figures["tool_calls"],
        "writes": figures["writes"],
        "pushes": figures["pushes"],
        "api_calls": figures["api_calls"],
        "denials": figures["denials"],
        "denial_classes": figures["denial_classes"],
        "input_tokens": figures["input_tokens"],
        "output_tokens": figures["output_tokens"],
        "subagent_tokens": {
            "input": sub_in,
            "output": sub_out,
            "responses": sub_responses,
        },
        "human_prompts": figures["human_prompts"],
        "first_prompt": figures["first_prompt"],
        "duration_s": round(duration, 1),
        "harness_version": version,
        "approvals": sel.approvals_digest(),
        "memory": sel.memory_digest(),
        "harness_manifest": sel.manifest() if turns else None,
        "model": figures["model"],
        "effort": figures["effort"],
        "client_version": figures["client_version"],
        "cwd": payload.get("cwd"),
        "self_verdict": verdict,
    }
    with open(ledger_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")
    state["offsets"][key] = new_offset
    if verdict:
        # A pending run nobody answered (a resumed session) posts as reported.
        post_pending(agent_cfg, state, state_path)
        lines = open_span(ledger_path, state)
        state["pending"] = {
            "run": roll_up(lines, verdict, by_person=False),
            "created": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        if verdict.get("asks"):
            state["pending"]["advice"] = ADVICE.format(outcome=verdict["outcome"])
            # names only: the time, the client, the task and the outcome
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(os.path.join(STATE_DIR, ADVICE_LOG), "a", encoding="utf-8") as fh:
                fh.write(
                    f"{line['at']} {program} "
                    f"{state['pending']['run'].get('task_id')} "
                    f"{verdict['outcome']} on a reply ending in a question\n"
                )
        state["span_start_line"] = int(state.get("span_start_line", 0)) + len(lines)
    save_state(state_path, state)
    if verdict and program not in HOLDS_FOR_PERSON:
        posted = post_pending(agent_cfg, state, state_path)
        if posted:
            line["posted"] = posted
    return line


def on_prompt(payload: dict, cfg: dict, program: str | None = None) -> str | None:
    """A verdict word closes or overrides; any other prompt posts what is
    pending. Both Claude prompt events may fire for one typed prompt: the second
    is told apart by prompt_id, or finds nothing left to do."""
    program = program or payload.get("program") or "claude_code"
    ledger_path, state_path = paths_for(str(payload.get("session_id")))
    state = load_state(state_path)
    agent_cfg = agent_config(cfg, program)
    prompt_id = payload.get("prompt_id")
    if prompt_id and state.get("last_prompt_id") == prompt_id:
        return None
    if prompt_id:
        state["last_prompt_id"] = prompt_id
    verdict = human_verdict(verdict_text(payload))
    if program == "codex" and verdict is None:
        # Codex's rollout mixes typed text with injected context; the hook sees
        # only what was typed.
        state["prompts"] = int(state.get("prompts", 0)) + 1
        if not state.get("first_prompt"):
            state["first_prompt"] = " ".join(str(payload.get("prompt") or "").split())[
                :120
            ]
    save_state(state_path, state)
    if verdict is None:
        return post_pending(agent_cfg, state, state_path)
    pending = state.get("pending")
    if pending:
        run = dict(pending["run"])
        # The held run's notes open with "self-reported"; confirmed, they keep what
        # the agent said instead.
        held = str(run.get("notes") or "")
        if held.startswith("self-reported"):
            held = f"agent reported {run['outcome']}" + held[len("self-reported") :]
        run["outcome"] = verdict["outcome"]
        run["verified"] = True
        for key in ("task_id", "task_class"):
            if verdict.get(key):
                run[key] = verdict[key]
        run["notes"] = "; ".join(
            p for p in ("confirmed by hand", verdict.get("notes"), held) if p
        )[:500]
        refused = unattributable(run)
        if refused:
            state.pop("pending", None)
            save_state(state_path, state)
            return refused
        status, body = send(agent_cfg, run)
        if 200 <= status < 300:
            state.pop("pending", None)
            save_state(state_path, state)
        return describe(run, status, body)
    lines = open_span(ledger_path, state)
    if not lines:
        return "harness-ledger: nothing to record; no turns since the last verdict"
    run = roll_up(lines, verdict, by_person=True)
    refused = unattributable(run)
    if refused:
        state["span_start_line"] = int(state.get("span_start_line", 0)) + len(lines)
        save_state(state_path, state)
        return refused
    status, body = send(agent_cfg, run)
    if 200 <= status < 300:
        state["span_start_line"] = int(state.get("span_start_line", 0)) + len(lines)
        save_state(state_path, state)
    return describe(run, status, body)


def on_session_end(payload: dict, cfg: dict, program: str | None = None) -> None:
    program = program or payload.get("program") or "claude_code"
    ledger_path, state_path = paths_for(str(payload.get("session_id")))
    state = load_state(state_path)
    if program == "codex":
        # Codex gives SessionEnd hooks three seconds; the next session posts it.
        return
    line = post_pending(agent_config(cfg, program), state, state_path)
    if line:
        sys.stderr.write(line + "\n")
    lines = open_span(ledger_path, state)
    if lines:
        sys.stderr.write(
            f"harness-ledger: {len(lines)} turn(s) have no verdict; "
            f"the span stays open in {ledger_path}\n"
        )


def handle(
    payload: object, program: str | None = None, event: str | None = None
) -> str | None:
    """Act on one hook call. Returns what goes to the agent on stdout: context
    text for Claude Code and Codex, the JSON answer an agent expects otherwise;
    reports for the other agents go to stderr."""
    ev = normalize(payload, program, event)
    if ev is None:
        return None
    program = ev["program"]
    cfg = load_config()
    if not agent_config(cfg, program).get("webhook_url"):
        # Set up for another agent on this machine: nothing to record for this one.
        return ev.get("reply")
    name = ev.get("hook_event_name")
    _, state_path = paths_for(str(ev.get("session_id")))
    flush_stale(cfg, state_path)
    text: str | None = None
    if name == "Stop":
        line = on_stop(ev, cfg, turns=1)
        text = line.get("posted") if line else None
    elif name == "SubagentStop":
        on_stop(ev, cfg, turns=0)
    elif name in ("UserPromptSubmit", "UserPromptExpansion"):
        text = on_prompt(ev, cfg)
    elif name == "SessionEnd":
        if ev.get("end_as_stop"):
            # Print mode fires no stop, so the end closes the one turn; after a stop
            # the end holds only the last turn's tail.
            stopped = load_state(state_path).get("stopped")
            on_stop(ev, cfg, turns=0 if stopped else 1, tail=True)
        on_session_end(ev, cfg)
    elif name == "SessionStart":
        # Short: a session-start hook is often given ten seconds in all.
        fetch_settings(agent_config(cfg, program), program, timeout=5)
        text = loop_line(program)
    if "reply" not in ev:
        return text
    if text:
        sys.stderr.write(text + "\n")
    return ev["reply"]


# ----------------------------------------------------------------------- setup
def _ask(question: str, default: bool) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    try:
        answer = input(f"{question} {hint} ").strip().lower()
    except EOFError:
        return default
    return default if not answer else answer.startswith("y")


def _install_copy() -> str:
    """This file, copied where hooks can find it whatever happens to the clone."""
    os.makedirs(INSTALL_DIR, exist_ok=True)
    target = os.path.join(INSTALL_DIR, "report_run.py")
    if os.path.abspath(__file__) != os.path.abspath(target):
        shutil.copyfile(__file__, target)
    return target


def _hook_command(script: str) -> str:
    return f'"{sys.executable}" "{script}"'


def _backup(path: str) -> str | None:
    if not os.path.isfile(path):
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = f"{path}.bak-{stamp}"
    shutil.copyfile(path, backup)
    return backup


def _merge_hooks(
    doc: dict, events: list[tuple[str, str | None, int]], command: str
) -> int:
    """Add one command hook per event unless an entry already runs it."""
    hooks = doc.setdefault("hooks", {})
    added = 0
    for event, matcher, timeout in events:
        groups = hooks.setdefault(event, [])
        present = any(
            h.get("command") == command
            for g in groups
            if isinstance(g, dict)
            for h in g.get("hooks", [])
            if isinstance(h, dict)
        )
        if present:
            continue
        group: dict = {
            "hooks": [{"type": "command", "command": command, "timeout": timeout}]
        }
        if matcher:
            group = {"matcher": matcher, **group}
        groups.append(group)
        added += 1
    return added


CLAUDE_HOOK_EVENTS = [
    ("Stop", None, 30),
    ("SubagentStop", None, 30),
    ("UserPromptSubmit", None, 40),
    ("UserPromptExpansion", "verdict|pass|fail|partial", 40),
    ("SessionStart", None, 20),
    ("SessionEnd", None, 10),
]
CODEX_HOOK_EVENTS = [
    ("Stop", None, 30),
    ("UserPromptSubmit", None, 40),
    ("SessionStart", None, 20),
    ("SessionEnd", None, 3),
]


TRACKER_NAME = "ha-harness-tracker"
COPILOT_HOOK_EVENTS = [
    ("sessionStart", 20),
    ("userPromptSubmitted", 40),
    ("agentStop", 30),
    ("subagentStop", 30),
    ("sessionEnd", 30),
]
CURSOR_HOOK_EVENTS = ["sessionStart", "beforeSubmitPrompt", "stop", "sessionEnd"]
AGY_HOOK_EVENTS = [("SessionStart", 20), ("Stop", 30)]

CLINE_SHIM = """// Reports finished tasks to the harness tracker (ha-harness-tracker).
// The reporter stops itself at its own deadline.
const { spawnSync } = require("child_process");
const input = require("fs").readFileSync(0);
spawnSync(PYTHON, [SCRIPT, "--hook", "cline", "TaskComplete"], {
  input,
  windowsHide: true,
  stdio: ["pipe", "ignore", "ignore"],
});
process.stdout.write("{}");
"""

OPENCODE_PLUGIN = """// Reports turns to the harness tracker (ha-harness-tracker).
import { spawnSync } from "node:child_process";

// No spawnSync timeout: under OpenCode's Bun 1.3.14 a timed call made about 30 s
// into a session fails at once. The reporter stops itself at its own deadline.
function report(fields) {
  spawnSync(PYTHON, [SCRIPT, "--hook", PROGRAM], {
    input: JSON.stringify({ source: "ha-harness-tracker", ...fields }),
    windowsHide: true,
    stdio: ["pipe", "ignore", "ignore"],
  });
}

export const HaHarnessTracker = async ({ directory }) => ({
  event: async ({ event }) => {
    const id = event.properties && event.properties.sessionID;
    if (event.type === "session.idle" && id) {
      report({ hook_event_name: "Stop", session_id: id, cwd: directory });
    } else if (event.type === "session.created" && id) {
      report({ hook_event_name: "SessionStart", session_id: id, cwd: directory });
    }
  },
  "chat.message": async (input, output) => {
    const text = (output.parts || [])
      .filter((p) => p.type === "text" && !p.synthetic)
      .map((p) => p.text)
      .join("\\n");
    report({
      hook_event_name: "UserPromptSubmit",
      session_id: input.sessionID,
      cwd: directory,
      prompt: text,
    });
  },
});
"""


def _hook_argv(script: str, program: str, event: str | None = None) -> list[str]:
    return [sys.executable, script, "--hook", program] + ([event] if event else [])


def _ps_command(argv: list[str]) -> str:
    return "& " + " ".join("'" + a.replace("'", "''") + "'" for a in argv)


def _sh_command(argv: list[str]) -> str:
    import shlex

    return " ".join(shlex.quote(a) for a in argv)


def _short_path(path: str) -> str:
    """A path without spaces. Antigravity splits a hook command on spaces and keeps
    quote characters, so on Windows a path with spaces becomes its 8.3 name."""
    if " " not in path or not sys.platform.startswith("win"):
        return path
    import ctypes

    buf = ctypes.create_unicode_buffer(1024)
    got = ctypes.windll.kernel32.GetShortPathNameW(path, buf, 1024)  # type: ignore[attr-defined]
    return buf.value if got else path


def _write_doc(path: str, text: str) -> str | None:
    """Write through a temporary file, keeping a backup of what was there."""
    backup = _backup(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, path)
    return backup


def _own_file(path: str, text: str) -> tuple[str, int, str | None]:
    """A file this tracker owns outright: replaced when it differs. A file named
    after the tracker is its own; another name must carry the tracker's name inside."""
    old = _read(path)
    if old == text.encode("utf-8"):
        return path, 0, None
    named = TRACKER_NAME in os.path.basename(path)
    if (
        old is not None
        and not named
        and TRACKER_NAME not in old.decode("utf-8", "replace")
    ):
        raise SystemExit(f"{path} exists and is not the tracker's; left unchanged")
    return path, 1, _write_doc(path, text)


def _register_copilot(script: str) -> tuple[str, int, str | None]:
    path = os.path.join(copilot_home(), "hooks", f"{TRACKER_NAME}.json")
    hooks = {
        event: [
            {
                "type": "command",
                "powershell": _ps_command(_hook_argv(script, "copilot_cli", event)),
                "bash": _sh_command(_hook_argv(script, "copilot_cli", event)),
                "timeoutSec": timeout,
            }
        ]
        for event, timeout in COPILOT_HOOK_EVENTS
    }
    doc = {"version": 1, "hooks": hooks}
    return _own_file(path, json.dumps(doc, indent=2) + "\n")


def _register_cursor(script: str) -> tuple[str, int, str | None]:
    path = os.path.join(HOME, ".cursor", "hooks.json")
    doc = _read_json(path, {})
    if os.path.isfile(path) and not doc:
        raise SystemExit(f"{path} is not a JSON object; left unchanged")
    argv = _hook_argv(script, "cursor")
    # Cursor runs a hook command through PowerShell on Windows and sh elsewhere.
    command = _ps_command(argv) if sys.platform.startswith("win") else _sh_command(argv)
    doc.setdefault("version", 1)
    hooks = doc.setdefault("hooks", {})
    added = 0
    for event in CURSOR_HOOK_EVENTS:
        entries = hooks.setdefault(event, [])
        if not any(
            isinstance(e, dict) and e.get("command") == command for e in entries
        ):
            entries.append({"command": command})
            added += 1
    backup = _write_doc(path, json.dumps(doc, indent=2) + "\n") if added else None
    return path, added, backup


def _register_agy(script: str) -> tuple[str, int, str | None]:
    path = os.path.join(HOME, ".gemini", "config", "hooks.json")
    doc = _read_json(path, {})
    if os.path.isfile(path) and not doc:
        raise SystemExit(f"{path} is not a JSON object; left unchanged")
    group: dict = {"enabled": True}
    for event, timeout in AGY_HOOK_EVENTS:
        argv = [_short_path(a) for a in _hook_argv(script, "antigravity", event)]
        if any(" " in a for a in argv):
            raise SystemExit(
                "Antigravity cannot run a hook whose path has spaces: " + " ".join(argv)
            )
        group[event] = [
            {"type": "command", "command": " ".join(argv), "timeout": timeout}
        ]
    if doc.get(TRACKER_NAME) == group:
        return path, 0, None
    doc[TRACKER_NAME] = group
    return (
        path,
        len(AGY_HOOK_EVENTS),
        _write_doc(path, json.dumps(doc, indent=2) + "\n"),
    )


def _register_cline(script: str) -> tuple[str, int, str | None]:
    hooks_dir = os.path.join(cline_home(), "hooks")
    path = os.path.join(hooks_dir, "TaskComplete.js")
    others = [
        f
        for f in (os.listdir(hooks_dir) if os.path.isdir(hooks_dir) else [])
        if f.startswith("TaskComplete") and f != "TaskComplete.js"
    ]
    if others:
        raise SystemExit(f"{hooks_dir} already has {others[0]}; left unchanged")
    text = CLINE_SHIM.replace("PYTHON", json.dumps(sys.executable), 1).replace(
        "SCRIPT", json.dumps(script), 1
    )
    return _own_file(path, text)


def _register_opencode(script: str, program: str) -> tuple[str, int, str | None]:
    path = os.path.join(opencode_config_dir(program), "plugins", f"{TRACKER_NAME}.js")
    text = (
        OPENCODE_PLUGIN.replace("PYTHON", json.dumps(sys.executable), 1)
        .replace("SCRIPT", json.dumps(script), 1)
        .replace("PROGRAM", json.dumps(program), 1)
    )
    return _own_file(path, text)


def register_hook(program: str, script: str) -> tuple[str, int, str | None]:
    """(file, events added, backup) after merging the hook into its config."""
    if program == "copilot_cli":
        return _register_copilot(script)
    if program == "cursor":
        return _register_cursor(script)
    if program == "antigravity":
        return _register_agy(script)
    if program == "cline":
        return _register_cline(script)
    if program in OPENCODE_FAMILY:
        return _register_opencode(script, program)
    command = _hook_command(script)
    if program == "claude_code":
        path = os.path.join(claude_home(), "settings.json")
        events = CLAUDE_HOOK_EVENTS
    else:
        path = os.path.join(codex_home(), "hooks.json")
        events = CODEX_HOOK_EVENTS
    doc = _read_json(path, {})
    if os.path.isfile(path) and not doc:
        raise SystemExit(f"{path} is not a JSON object; left unchanged")
    added = _merge_hooks(doc, events, command)
    backup = None
    if added:
        backup = _backup(path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    return path, added, backup


def owner_only(path: str) -> bool:
    """Make a file readable by its owner alone. Windows ignores the mode bits, so
    there the inherited entries are removed and the user is granted full control."""
    if not sys.platform.startswith("win"):
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
            return True
        return False
    user = os.environ.get("USERNAME")
    if not user:
        return False
    try:
        done = subprocess.run(
            ["icacls", path, "/inheritance:r", "/grant:r", f"{user}:F"],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except OSError:
        return False
    return done.returncode == 0


def write_config(cfg: dict) -> None:
    """Write the config through a temporary file made owner-only before it
    replaces the old one, so the webhook address is never readable by others."""
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    tmp = CONFIG + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=1)
    if not owner_only(tmp):
        sys.stderr.write(f"Could not restrict {CONFIG} to its owner; do it by hand.\n")
    os.replace(tmp, CONFIG)


def setup(url: str | None, register: bool, cwd: str) -> int:
    url = url or getpass.getpass("Webhook address: ").strip()
    if not url.startswith(("http://", "https://")) or "/api/webhook/" not in url:
        print("That is not a Home Assistant webhook address (…/api/webhook/<id>).")
        return 2
    insecure = False
    status, body = http("GET", url, None, False, 15)
    if status == 0 and "CERTIFICATE_VERIFY_FAILED" in body:
        if not _ask("The certificate is not trusted (self-signed?). Trust it?", False):
            return 2
        insecure = True
        status, body = http("GET", url, None, True, 15)
    if status != 200:
        print(f"Home Assistant did not answer the webhook: {status} {body[:160]}")
        return 1
    settings = _read_json_bytes(body.encode())
    if not isinstance(settings, dict):
        print("Home Assistant answered, but not with the tracker's settings.")
        return 1
    program = str(settings.get("agent_program") or "other")
    name = settings.get("agent") or "the agent"
    print(f'Connected to "{name}" ({PROGRAM_NAMES.get(program, program)}).')
    cfg = load_config()
    agents = cfg.get("agents") if isinstance(cfg.get("agents"), dict) else {}
    agents[program] = {"webhook_url": url, "insecure": insecure}
    cfg["agents"] = agents
    write_config(cfg)
    print(f"Wrote {CONFIG} (0600).")
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(settings_path(program), "w", encoding="utf-8") as fh:
        json.dump(settings, fh)
    sel = select(settings, cfg, cwd, program)
    print()
    print("\n".join(describe_selection(sel)))
    print()
    if program not in AUTOMATIC:
        print(
            f"Report runs with: python report_run.py --agent {program} "
            "--outcome pass|fail|partial"
        )
        print("from the agent's last step, its own end-of-task hook or a script.")
        return 0
    home = {"claude_code": claude_home, "codex": codex_home}.get(
        program, lambda: program_home(program)
    )()
    if program in OPENCODE_FAMILY:
        # OpenCode and Kilo create their data directory on the first run.
        home = os.path.dirname(opencode_db(program))
    if not os.path.isdir(home):
        print(f"{PROGRAM_NAMES[program]} was not found at {home}; no hook registered.")
        return 0
    if not register or not _ask(
        f"Register the reporting hook for {PROGRAM_NAMES[program]}?", True
    ):
        print("No hook registered.")
        return 0
    script = _install_copy()
    path, added, backup = register_hook(program, script)
    if not added:
        print(f"The hook was already registered in {path}.")
    else:
        print(
            f"Registered {added} hook events in {path}"
            + (f" (backup {backup})." if backup else ".")
        )
    if program == "codex":
        print("Approve the new hooks in Codex: run /hooks and trust them.")
    print("New sessions report runs.")
    return 0


# ------------------------------------------------------------------ manual report
def build_run(args: argparse.Namespace, cfg: dict, program: str) -> dict:
    settings = (
        cached_settings(agent_config(cfg, program), program) if not args.harness else {}
    )
    cwd = args.cwd or os.getcwd()
    if args.harness_version:
        run: dict = {"harness_version": args.harness_version}
        sel = None
    else:
        sel = (
            select_manual(args.harness, cwd, program)
            if args.harness
            else select(settings, cfg, cwd, program)
        )
        if sel.missing and args.harness:
            raise SystemExit(
                "--harness: no such file or directory: "
                + ", ".join(sel.display(p) for p in sel.missing)
            )
        if not sel.files:
            raise SystemExit(
                "no harness files: set them in Home Assistant, "
                "or name them with --harness"
            )
        label = args.label or label_for(settings, cfg)
        run = {"harness_version": sel.version(label), "fingerprint_schema": SCHEMA}
        if sel.approvals:
            run["approvals"] = sel.approvals_digest()
        if sel.memory:
            run["memory"] = sel.memory_digest()
        run["harness_manifest"] = sel.manifest()
    run["outcome"] = args.outcome
    run["client"] = program
    for field in FIELDS:
        value = getattr(args, field, None)
        if value is not None:
            run[field] = value
    run["run_key"] = args.run_key or uuid.uuid4().hex
    return run


def configured_programs(cfg: dict) -> list[str]:
    """The programs with a webhook in the local config; the 0.3 top-level one is
    Claude Code's."""
    agents = cfg.get("agents") if isinstance(cfg.get("agents"), dict) else {}
    names = {
        p for p, a in agents.items() if isinstance(a, dict) and a.get("webhook_url")
    }
    if cfg.get("webhook_url"):
        names.add("claude_code")
    return sorted(names)


def _program_for(cfg: dict, wanted: str | None) -> str:
    """The program a command reports for: the one named, or the only one
    configured. With several configured, a guess posts a run to the wrong agent."""
    if wanted:
        return wanted
    names = configured_programs(cfg)
    if len(names) > 1:
        raise SystemExit(
            f"several agents are configured ({', '.join(names)}); name one with --agent"
        )
    return names[0] if names else "other"


def cli(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--setup", action="store_true", help="store the webhook and register the hook"
    )
    p.add_argument(
        "--webhook",
        metavar="URL",
        help="with --setup: the webhook address; otherwise post here",
    )
    p.add_argument(
        "--no-hook", action="store_true", help="with --setup: do not register a hook"
    )
    p.add_argument(
        "--show-files",
        action="store_true",
        help="print the selected harness files and exit",
    )
    p.add_argument(
        "--agent", metavar="PROGRAM", help="agent program when the config holds several"
    )
    p.add_argument(
        "--cwd", metavar="DIR", help="project directory (default: the current one)"
    )
    p.add_argument(
        "--harness",
        action="append",
        default=[],
        metavar="PATH",
        help="harness file or directory; repeatable; replaces the selection",
    )
    p.add_argument("--label", help="prefix for the computed version, e.g. a git branch")
    p.add_argument(
        "--harness-version", help="use this version instead of computing one"
    )
    p.add_argument(
        "--print-version", action="store_true", help="print the version and exit"
    )
    p.add_argument("--outcome", choices=OUTCOMES)
    p.add_argument("--task-id")
    p.add_argument("--task-class")
    p.add_argument("--verified", action="store_true", default=None)
    p.add_argument("--turns", type=int)
    p.add_argument("--tool-calls", type=int)
    p.add_argument("--duration", dest="duration_s", type=float, metavar="SECONDS")
    p.add_argument("--input-tokens", type=int)
    p.add_argument("--output-tokens", type=int)
    p.add_argument("--cost", dest="cost_usd", type=float, metavar="USD")
    p.add_argument("--denials", type=int)
    p.add_argument("--retries", type=int)
    p.add_argument("--interventions", type=int)
    p.add_argument("--notes")
    p.add_argument("--model")
    p.add_argument("--effort")
    p.add_argument("--client-version")
    p.add_argument("--run-key", help="idempotency key; a repeated key is recorded once")
    p.add_argument(
        "--insecure", action="store_true", help="skip TLS verification (self-signed)"
    )
    p.add_argument("--dry-run", action="store_true", help="print the record and exit")
    args = p.parse_args(argv)

    if args.setup:
        return setup(args.webhook, not args.no_hook, args.cwd or os.getcwd())
    cfg = load_config()
    program = _program_for(cfg, args.agent)
    if args.show_files:
        settings = cached_settings(agent_config(cfg, program), program)
        sel = (
            select_manual(args.harness, args.cwd or os.getcwd(), program)
            if args.harness
            else select(settings, cfg, args.cwd or os.getcwd(), program)
        )
        print("\n".join(describe_selection(sel)))
        print(
            "Version: "
            + (
                sel.version(args.label or label_for(settings, cfg))
                if sel.files
                else "none"
            )
        )
        return 0
    if args.print_version:
        run = build_run(args, cfg, program)
        print(run["harness_version"])
        return 0
    if not args.outcome:
        p.error("--outcome is required")
    run = build_run(args, cfg, program)
    if args.dry_run:
        print(json.dumps(run, indent=2))
        return 0
    target = agent_config(cfg, program)
    url = args.webhook or target.get("webhook_url")
    if not url:
        p.error("no webhook: run --setup, or give --webhook")
    status, body = http("POST", url, run, args.insecure or bool(target.get("insecure")))
    print(f"{status} {body.strip()[:300]}")
    return 0 if 200 <= status < 300 else 1


# --------------------------------------------------------------------- selftest
def _selftest() -> int:
    """A synthetic transcript with known figures, parsed, held and posted."""
    import tempfile

    global STATE_DIR, post
    tmp = tempfile.mkdtemp()
    STATE_DIR = os.path.join(tmp, "state")
    projects = os.path.join(claude_home(), "projects", "selftest")
    transcript = os.path.join(tmp, "t.jsonl")

    def human(ts: str, text: str) -> dict:
        return {
            "type": "user",
            "timestamp": ts,
            "message": {"role": "user", "content": text},
        }

    def result(ts: str, text: str, error: bool = False) -> dict:
        block = {"type": "tool_result", "content": text, "is_error": error}
        return {
            "type": "user",
            "timestamp": ts,
            "message": {"role": "user", "content": [block]},
        }

    def assistant(ts: str, rid: str, blocks: list[dict], usage: dict) -> dict:
        message = {"role": "assistant", "content": blocks, "usage": usage}
        return {
            "type": "assistant",
            "timestamp": ts,
            "requestId": rid,
            "message": message,
        }

    def write_lines(entries: list[dict], mode: str = "w") -> None:
        with open(transcript, mode, encoding="utf-8") as fh:
            for e in entries:
                fh.write(json.dumps(e) + "\n")

    usage_r1 = {
        "input_tokens": 0,
        "output_tokens": 0,
        "iterations": [{"input_tokens": 100, "output_tokens": 20}],
    }
    usage_r2 = {
        "iterations": [
            {
                "input_tokens": 50,
                "output_tokens": 5,
                "cache_read_input_tokens": 1000,
                "cache_creation_input_tokens": 200,
            }
        ]
    }
    edit = {"type": "tool_use", "name": "Edit", "input": {"file_path": "x"}}
    push = {
        "type": "tool_use",
        "name": "Bash",
        "input": {"command": "git -C r push gitea main"},
    }
    look = {"type": "tool_use", "name": "Read", "input": {"file_path": "x"}}
    write_lines(
        [
            human(
                "2026-09-17T09:59:00Z", "<task-notification>done</task-notification>"
            ),
            {**human("2026-09-17T09:59:01Z", "skill body"), "isMeta": True},
            {**human("2026-09-17T09:59:02Z", "summary"), "isCompactSummary": True},
            human("2026-09-17T09:59:03Z", "<command-name>/pass</command-name>"),
            human(
                "2026-09-17T09:59:04Z",
                "This session is being continued from a previous conversation",
            ),
            human("2026-09-17T10:00:00Z", "Build it"),
            assistant(
                "2026-09-17T10:00:05Z",
                "r1",
                [{"type": "text", "text": "ok"}, edit],
                usage_r1,
            ),
            assistant("2026-09-17T10:00:06Z", "r1", [look], usage_r1),
            result(
                "2026-09-17T10:00:07Z",
                "Permission for this action was denied by the Claude Code auto mode "
                "classifier. Reason: [DNS / Domain / Cert Changes]. If you have",
                error=True,
            ),
            result(
                "2026-09-17T10:00:08Z",
                "PreToolUse:Bash hook error: BLOCKED -- a heredoc\n"
                "  -> tools/claude-hooks/block-guard.py",
                error=True,
            ),
            result(
                "2026-09-17T10:00:08Z",
                "PreToolUse:Bash hook error: BLOCKED -- quoted in a log",
            ),
            result(
                "2026-09-17T10:00:08Z",
                "The user doesn't want to proceed with this tool use. "
                "The tool use was rejected",
                error=True,
            ),
            result("2026-09-17T10:00:09Z", "fine"),
            human("2026-09-17T10:01:00Z", "no, the other one"),
            assistant("2026-09-17T10:01:30Z", "r2", [push], usage_r2),
            assistant(
                "2026-09-17T10:02:00Z",
                "r3",
                [
                    {
                        "type": "text",
                        "text": "Done.\n\nVerdict: pass (verified) "
                        "task=build-x first try",
                    }
                ],
                {"iterations": [{"input_tokens": 1, "output_tokens": 1}]},
            ),
        ]
    )
    figures, new_offset = parse_slice(transcript, 0)
    checks = [
        ("tool calls counted across blocks", figures["tool_calls"] == 3),
        (
            "writes and pushes classified",
            figures["writes"] == 1 and figures["pushes"] == 1,
        ),
        (
            "tokens counted once per request, cache included",
            figures["input_tokens"] == 1351 and figures["output_tokens"] == 26,
        ),
        (
            "denials classed; quoted refusal text and a plain result ignored",
            figures["denials"] == 3
            and figures["denial_classes"]
            == {
                "classifier:DNS / Domain / Cert Changes": 1,
                "hook:block-guard": 1,
                "person": 1,
            },
        ),
        ("two human prompts", figures["human_prompts"] == 2),
        ("first prompt kept", figures["first_prompt"] == "Build it"),
        (
            "the agent's verdict line is read from the final message",
            figures["self_verdict"]
            == {
                "outcome": "pass",
                "notes": "first try",
                "claimed_verified": True,
                "task_id": "build-x",
            },
        ),
        ("offset advances to the end", new_offset == os.path.getsize(transcript)),
        (
            "a re-read from the new offset finds nothing",
            parse_slice(transcript, new_offset)[0]["tool_calls"] == 0,
        ),
    ]

    # Settings projections: a rule change moves the version, a theme change does not.
    settings = os.path.join(tmp, ".claude", "settings.json")
    os.makedirs(os.path.dirname(settings), exist_ok=True)

    def settings_version(doc: dict) -> str:
        with open(settings, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        return select_manual([settings], tmp, "claude_code").version(None)

    v1 = settings_version(
        {
            "permissions": {"deny": ["Bash(rm:*)"]},
            "hooks": {},
            "theme": "dark",
            "model": "a",
        }
    )
    v2 = settings_version(
        {
            "permissions": {"deny": ["Bash(rm:*)"]},
            "hooks": {},
            "theme": "light",
            "model": "b",
        }
    )
    v3 = settings_version({"permissions": {"deny": []}, "hooks": {}, "theme": "light"})
    checks.append(("settings.json theme and model changes keep the version", v1 == v2))
    checks.append(("settings.json deny-rule change moves the version", v1 != v3))

    # Posting is captured, not sent.
    sent: list[dict] = []
    real_post = post
    window = {
        "size": 10,
        "runs": 10,
        "pass_rate": 80.0,
        "prior_pass_rate": 90.0,
        "improvement": -20.0,
        "shared_tasks": 3,
        "denials_per_run": 1.2,
        "prior_denials_per_run": 0.1,
        "denials_per_100_calls": 1.4,
        "prior_denials_per_100_calls": 0.3,
        "regressed": True,
        "versions": 4,
        "recurring_denials": [["hook:chain-guard", 3], ["person", 2]],
    }

    def fake_post(cfg: dict, run: dict) -> tuple[int, str]:
        sent.append(run)
        return 200, json.dumps(
            {
                "run_count": len(sent),
                "pass_rate": 100.0,
                "regressed": False,
                "window": window,
            }
        )

    post = fake_post
    os.makedirs(os.path.join(STATE_DIR), exist_ok=True)
    with open(settings_path("claude_code"), "w", encoding="utf-8") as fh:
        json.dump({"selection": "manual", "harness_files": [transcript]}, fh)
    cfg = {"webhook_url": "fake"}
    base = {
        "session_id": "selftest",
        "transcript_path": transcript,
        "cwd": os.path.join(tmp, "repo-a"),
    }
    line = on_stop({**base, "hook_event_name": "Stop"}, cfg, turns=1)
    _, state_path = paths_for("selftest")
    state = load_state(state_path)
    checks.append(
        (
            "stop wrote a ledger line with a fingerprint",
            bool(line) and line["harness_version"].startswith("sha256:"),
        )
    )
    manifest = os.path.join(
        STATE_DIR, "versions", line["harness_version"].split(":")[-1] + ".json"
    )
    with open(manifest, encoding="utf-8") as fh:
        listed = json.load(fh)["files"]
    checks.append(
        (
            "a new version gets a manifest of per-file hashes",
            any(k.endswith("t.jsonl") for k in listed),
        )
    )
    pending = state.get("pending", {}).get("run")
    checks.append(
        (
            "a verdict line holds a pending run, nothing posted yet",
            bool(pending) and not sent,
        )
    )
    checks.append(
        (
            "pending run is self-reported and unverified",
            pending
            and pending["verified"] is False
            and pending["notes"].startswith("self-reported, verified by effect"),
        )
    )
    checks.append(
        (
            "task id from the verdict line wins",
            pending and pending["task_id"] == "build-x",
        )
    )
    checks.append(
        (
            "class inferred as publish from the push",
            pending and pending["task_class"] == "publish",
        )
    )
    checks.append(
        (
            "interventions are prompts beyond the first",
            pending and pending["interventions"] == 1,
        )
    )
    checks.append(
        (
            "pending run carries the first prompt",
            pending and "prompt: Build it" in pending["notes"],
        )
    )
    checks.append(
        (
            "the run carries a run key and the schema",
            pending
            and len(pending["run_key"]) == 24
            and pending["fingerprint_schema"] == SCHEMA,
        )
    )
    checks.append(("the span moved past the held lines", state["span_start_line"] == 1))
    checks.append(
        (
            "the run carries its denial classes",
            pending
            and pending.get("denial_classes")
            == {
                "classifier:DNS / Domain / Cert Changes": 1,
                "hook:block-guard": 1,
                "person": 1,
            },
        )
    )
    checks.append(("no tracker answer yet, no session-start line", loop_line() is None))

    out = on_prompt(
        {
            **base,
            "hook_event_name": "UserPromptSubmit",
            "prompt": "/fail",
            "prompt_id": "p1",
        },
        cfg,
    )
    checks.append(
        (
            "a bare /fail overrides and posts",
            len(sent) == 1
            and sent[-1]["outcome"] == "fail"
            and sent[-1]["verified"] is True,
        )
    )
    checks.append(
        (
            "confirmed notes keep the agent's claim and the first prompt",
            sent[-1]["notes"].startswith(
                "confirmed by hand; agent reported pass, verified by effect; first try"
            )
            and "prompt: Build it" in sent[-1]["notes"],
        )
    )
    start = loop_line()
    checks.append(
        (
            "the next session starts with the window gate and the recurring classes",
            bool(start)
            and "last 10 runs passed 80.0% against 90.0% in the 10 before, "
            "-20.0 points over the 3 tasks both ran"
            in start
            and "denials per 100 tool calls 1.4 against 0.3" in start
            and "THE WINDOW GATE REGRESSED across 4 harness versions" in start
            and "recurring denials hook:chain-guard x3, person x2" in start,
        )
    )
    checks.append(
        (
            "the reply names the confirmed run",
            bool(out)
            and out.startswith(
                "harness-ledger: recorded confirmed run build-x as fail"
            ),
        )
    )
    out2 = on_prompt(
        {
            **base,
            "hook_event_name": "UserPromptExpansion",
            "command_name": "fail",
            "arguments": "",
            "prompt_id": "p1",
        },
        cfg,
    )
    checks.append(
        (
            "the same prompt on the other event is silent",
            out2 is None and len(sent) == 1,
        )
    )

    write_lines(
        [
            human("2026-09-17T11:00:00Z", "now diagnose the thing"),
            assistant(
                "2026-09-17T11:00:30Z",
                "r4",
                [look, {"type": "text", "text": "Verdict: partial unverified"}],
                {"iterations": [{"input_tokens": 2, "output_tokens": 2}]},
            ),
        ],
        mode="a",
    )
    on_stop({**base, "hook_event_name": "Stop"}, cfg, turns=1)
    out = on_prompt(
        {
            **base,
            "hook_event_name": "UserPromptSubmit",
            "prompt": "what next?",
            "prompt_id": "p2",
        },
        cfg,
    )
    checks.append(
        (
            "an ordinary prompt posts the pending run as reported",
            len(sent) == 2
            and sent[-1]["outcome"] == "partial"
            and sent[-1]["verified"] is False,
        )
    )
    checks.append(
        (
            "task id derived from directory and class",
            sent[-1]["task_id"] == "repo-a:ops",
        )
    )
    checks.append(
        (
            "unverified claim lands in the notes",
            sent[-1]["notes"].startswith("self-reported, unverified"),
        )
    )
    checks.append(
        (
            "the reply names the self-reported run",
            bool(out) and "recorded self-reported run repo-a:ops as partial" in out,
        )
    )
    checks.append(
        ("two spans get two run keys", sent[0]["run_key"] != sent[1]["run_key"])
    )
    out = on_prompt(
        {
            **base,
            "hook_event_name": "UserPromptSubmit",
            "prompt": "and then?",
            "prompt_id": "p3",
        },
        cfg,
    )
    checks.append(
        (
            "nothing pending, an ordinary prompt is silent",
            out is None and len(sent) == 2,
        )
    )

    write_lines(
        [
            human("2026-09-17T12:00:00Z", "one more"),
            assistant(
                "2026-09-17T12:00:10Z",
                "r5",
                [edit],
                {"iterations": [{"input_tokens": 1, "output_tokens": 1}]},
            ),
        ],
        mode="a",
    )
    on_stop({**base, "hook_event_name": "Stop"}, cfg, turns=1)
    out = on_prompt(
        {
            **base,
            "hook_event_name": "UserPromptSubmit",
            "prompt": "/verdict pass task=hacs-audit class=publish clean",
            "prompt_id": "p4",
        },
        cfg,
    )
    checks.append(
        (
            "a long-form verdict closes the open span with overrides",
            len(sent) == 3
            and sent[-1]["task_id"] == "hacs-audit"
            and sent[-1]["task_class"] == "publish"
            and sent[-1]["verified"] is True,
        )
    )
    out = on_prompt(
        {
            **base,
            "hook_event_name": "UserPromptSubmit",
            "prompt": "pass",
            "prompt_id": "p5",
        },
        cfg,
    )
    checks.append(
        (
            "a verdict with nothing to close says so",
            bool(out) and "nothing to record" in out,
        )
    )
    checks.append(
        (
            "an ordinary sentence starting with pass is not a verdict",
            human_verdict("pass the salt") is None,
        )
    )
    checks.append(
        (
            "'verdict on this?' is not a verdict",
            human_verdict("verdict on this?") is None,
        )
    )
    checks.append(
        (
            "a partial on a reply ending in a question is marked",
            (
                self_verdict(
                    "Done.\n\nVerdict: partial verified task=x\n\nWhich one?\n"
                    "1. a\n2. b"
                )
                or {}
            ).get("asks")
            is True,
        )
    )
    checks.append(
        (
            "a pass above a question about something else is not marked",
            "asks"
            not in (
                self_verdict("Verdict: pass verified\n\nShall I add it?\n1. y") or {}
            ),
        )
    )
    checks.append(
        (
            "a partial with no question after it is not marked",
            "asks"
            not in (self_verdict("Verdict: partial\nthe rest is pending.") or {}),
        )
    )
    checks.append(
        (
            "the advice follows the posted line, and only when held",
            with_advice("recorded", {"advice": "advice"}) == "recorded\nadvice"
            and with_advice("recorded", {}) == "recorded"
            and with_advice(None, {"advice": "advice"}) is None,
        )
    )
    checks.append(
        (
            "a Verdict line mid-message still counts",
            self_verdict("Verdict: fail\nmore text")
            == {"outcome": "fail", "notes": ""},
        )
    )
    checks.append(
        (
            "a task id in brackets names the task",
            self_verdict(
                "Verdict: pass [verified] [task=ai-research:build] (class=build)"
            )
            == {
                "outcome": "pass",
                "notes": "",
                "claimed_verified": True,
                "task_id": "ai-research:build",
                "task_class": "build",
            },
        )
    )
    checks.append(
        (
            "another command's expansion is not a verdict",
            verdict_text(
                {
                    "hook_event_name": "UserPromptExpansion",
                    "command_name": "review",
                    "arguments": "x",
                }
            )
            == "",
        )
    )

    other_state = os.path.join(STATE_DIR, "other.state.json")
    old = (datetime.now().astimezone() - timedelta(hours=7)).isoformat(
        timespec="seconds"
    )
    save_state(
        other_state,
        {
            "offsets": {},
            "span_start_line": 0,
            "pending": {"created": old, "run": {**sent[0], "task_id": "other:ops"}},
        },
    )
    n = flush_stale(cfg, state_path)
    checks.append(
        (
            "a seven hour old pending run from another session is posted",
            n == 1 and sent[-1]["task_id"] == "other:ops",
        )
    )
    save_state(
        other_state,
        {
            "offsets": {},
            "span_start_line": 0,
            "pending": {
                "created": datetime.now().astimezone().isoformat(timespec="seconds"),
                "run": sent[0],
            },
        },
    )
    checks.append(
        ("a fresh one is left for its own session", flush_stale(cfg, state_path) == 0)
    )
    checks.append(
        ("--flush posts it regardless", flush_stale(cfg, state_path, force=True) == 1)
    )

    before = len(sent)
    ustate = {
        "offsets": {},
        "pending": {"run": dict(sent[0], harness_version="unknown")},
    }
    note = post_pending(cfg, ustate, os.path.join(tmp, "u.json"))
    checks.append(
        (
            "an unattributable run is dropped, not posted",
            len(sent) == before
            and "pending" not in ustate
            and "NOT recorded" in (note or ""),
        )
    )
    checks.append(
        (
            "the rate is printed with its sample and confirmation",
            rate_text({"pass_rate": 0.0, "current_runs": 1, "confirmed": False})
            == "this harness version: pass rate 0.0% over 1 run (unconfirmed)",
        )
    )

    # Identity: a Claude payload is handled; a borrowed hook in Cursor is not.
    claude_t = os.path.join(projects, "s.jsonl")
    checks.append(
        (
            "a Claude payload is identified",
            identify(
                {
                    "hook_event_name": "Stop",
                    "session_id": "s",
                    "transcript_path": claude_t,
                }
            )
            == "claude_code",
        )
    )
    checks.append(
        (
            "Cursor running Claude's hooks is not",
            identify(
                {
                    "hook_event_name": "Stop",
                    "session_id": "s",
                    "transcript_path": claude_t,
                    "cursor_version": "3",
                }
            )
            is None,
        )
    )
    checks.append(
        (
            "a transcript outside Claude's store is not",
            identify(
                {
                    "hook_event_name": "Stop",
                    "session_id": "s",
                    "transcript_path": transcript,
                }
            )
            is None,
        )
    )

    # Subagent transcripts: their tokens join the parent's, one response per message id.
    def sub_msg(mid: str, usage: dict, model: str = "claude-opus-5-5") -> dict:
        return {
            "type": "assistant",
            "timestamp": "2026-09-17T12:00:00Z",
            "isSidechain": True,
            "agentId": "a",
            "message": {
                "id": mid,
                "model": model,
                "role": "assistant",
                "content": [],
                "usage": usage,
            },
        }

    def append_jsonl(path: str, entries: list[dict]) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            for e in entries:
                fh.write(json.dumps(e) + "\n")

    lone = os.path.join(tmp, "lone.jsonl")
    agent_file = os.path.join(subagent_dir(lone), "agent-a.jsonl")
    u1 = {"input_tokens": 100, "cache_read_input_tokens": 1000, "output_tokens": 10}
    u2 = {"input_tokens": 5, "output_tokens": 7}
    append_jsonl(agent_file, [sub_msg("m1", u1), sub_msg("m1", u1), sub_msg("m2", u2)])
    sub_state: dict = {}
    first_read = subagent_tokens(lone, sub_state)
    second_read = subagent_tokens(lone, sub_state)
    append_jsonl(
        agent_file,
        [sub_msg("m2", u2), sub_msg("m4", {"input_tokens": 1, "output_tokens": 1})],
    )
    straddle_read = subagent_tokens(lone, sub_state)
    append_jsonl(
        agent_file,
        [sub_msg("m5", {"input_tokens": 9, "output_tokens": 9}, model="<synthetic>")],
    )
    synthetic_read = subagent_tokens(lone, sub_state)
    checks.append(
        ("a subagent's tokens count once per message", first_read == (1105, 17, 2))
    )
    checks.append(
        ("a subagent file is read from its own offset", second_read == (0, 0, 0))
    )
    checks.append(
        (
            "a response whose lines straddle two reads counts once",
            straddle_read == (1, 1, 1),
        )
    )
    checks.append(
        ("a synthetic subagent message is not a response", synthetic_read == (0, 0, 0))
    )
    checks.append(
        (
            "a session without subagents adds nothing",
            subagent_tokens(os.path.join(tmp, "none.jsonl"), {}) == (0, 0, 0),
        )
    )
    parent = os.path.join(tmp, "parent.jsonl")
    append_jsonl(
        parent,
        [
            human("2026-09-17T12:00:00Z", "fan out"),
            assistant(
                "2026-09-17T12:00:05Z",
                "rp",
                [{"type": "text", "text": "done"}],
                {"input_tokens": 10, "output_tokens": 2},
            ),
        ],
    )
    append_jsonl(
        os.path.join(subagent_dir(parent), "agent-b.jsonl"),
        [sub_msg("s1", {"input_tokens": 7, "output_tokens": 3})],
    )
    parent_line = on_stop(
        {
            "session_id": "subsession",
            "transcript_path": parent,
            "cwd": tmp,
            "hook_event_name": "Stop",
        },
        cfg,
        turns=1,
    )
    checks.append(
        (
            "a stop adds the subagents' tokens to the parent's",
            bool(parent_line)
            and parent_line["input_tokens"] == 17
            and parent_line["output_tokens"] == 5
            and parent_line["subagent_tokens"]
            == {"input": 7, "output": 3, "responses": 1},
        )
    )

    # A session running when this was installed: its subagent history is skipped.
    old = os.path.join(tmp, "old.jsonl")
    append_jsonl(old, [human("2026-09-17T12:10:00Z", "earlier work")])
    append_jsonl(
        os.path.join(subagent_dir(old), "agent-c.jsonl"),
        [sub_msg("h1", {"input_tokens": 900, "output_tokens": 90})],
    )
    _, old_state_path = paths_for("oldsession")
    save_state(
        old_state_path, {"offsets": {old: os.path.getsize(old)}, "span_start_line": 0}
    )
    append_jsonl(
        old,
        [
            human("2026-09-17T12:11:00Z", "more"),
            assistant(
                "2026-09-17T12:11:05Z",
                "ro",
                [{"type": "text", "text": "ok"}],
                {"input_tokens": 4, "output_tokens": 1},
            ),
        ],
    )
    append_jsonl(
        os.path.join(subagent_dir(old), "agent-d.jsonl"),
        [sub_msg("n1", {"input_tokens": 6, "output_tokens": 2})],
    )
    seed_state: dict = {}
    seeded = subagent_tokens(old, seed_state, seed=True)
    old_line = on_stop(
        {
            "session_id": "oldsession",
            "transcript_path": old,
            "cwd": tmp,
            "hook_event_name": "Stop",
        },
        cfg,
        turns=1,
    )
    checks.append(
        (
            "seeding reads nothing and marks every file read",
            seeded == (0, 0, 0) and len(seed_state["offsets"]) == 2,
        )
    )
    checks.append(
        (
            "a session that predates subagent counting skips their history",
            bool(old_line)
            and old_line["input_tokens"] == 4
            and old_line["subagent_tokens"]["responses"] == 0,
        )
    )
    append_jsonl(
        os.path.join(subagent_dir(old), "agent-d.jsonl"),
        [sub_msg("n2", {"input_tokens": 3, "output_tokens": 1})],
    )
    append_jsonl(
        old,
        [
            assistant(
                "2026-09-17T12:12:05Z",
                "ro2",
                [{"type": "text", "text": "ok"}],
                {"input_tokens": 1, "output_tokens": 1},
            )
        ],
    )
    later_line = on_stop(
        {
            "session_id": "oldsession",
            "transcript_path": old,
            "cwd": tmp,
            "hook_event_name": "Stop",
        },
        cfg,
        turns=1,
    )
    checks.append(
        (
            "after seeding, new subagent work counts",
            bool(later_line)
            and later_line["input_tokens"] == 4
            and later_line["subagent_tokens"]["responses"] == 1,
        )
    )

    # Codex: usage is unknown without per-response records.
    rollout = os.path.join(tmp, "rollout.jsonl")
    total = {"input_tokens": 50, "output_tokens": 5}
    append_jsonl(
        rollout,
        [
            {
                "timestamp": "2026-09-17T12:00:00Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": total},
                },
            }
        ],
    )
    totals_only, mark = parse_codex_slice(rollout, 0)
    append_jsonl(
        rollout,
        [
            {
                "timestamp": "2026-09-17T12:00:01Z",
                "type": "token_usage_record",
                "payload": {
                    "response_id": "x1",
                    "usage": {"input_tokens": 40, "output_tokens": 4},
                },
            }
        ],
    )
    with_record, _ = parse_codex_slice(rollout, mark)
    checks.append(
        (
            "a Codex slice with running totals and no records has unknown tokens",
            totals_only["input_tokens"] is None
            and totals_only["output_tokens"] is None,
        )
    )
    checks.append(
        (
            "a Codex slice with a record counts it",
            with_record["input_tokens"] == 40 and with_record["output_tokens"] == 4,
        )
    )
    floor_run = roll_up(
        [
            {"input_tokens": 10, "output_tokens": 1},
            {"input_tokens": None, "output_tokens": None},
        ],
        {"outcome": "pass"},
        by_person=False,
    )
    whole_run = roll_up(
        [{"input_tokens": 10, "output_tokens": 1}], {"outcome": "pass"}, by_person=False
    )
    checks.append(
        (
            "a token sum over an unknown reading says it is a floor",
            "floor" in floor_run["notes"] and floor_run["input_tokens"] == 10,
        )
    )
    checks.append(
        (
            "a complete token sum carries no floor note",
            "floor" not in whole_run["notes"],
        )
    )

    post = real_post
    status, body = post({}, sent[0])
    checks.append(
        (
            "posting without a webhook is refused, not silent",
            status == 0 and "webhook_url" in body,
        )
    )
    ok = True
    for label, passed in checks:
        ok = ok and bool(passed)
        print(("  ok   " if passed else "  FAIL ") + label)
    print(f"{sum(bool(p) for _, p in checks)}/{len(checks)} checks passed")
    return 0 if ok else 1


# ------------------------------------------------------------------------ main
HOOK_FLAGS = ("--hook", "--codex", "--claude")


def hook_args(argv: list[str]) -> tuple[str | None, str | None] | None:
    """(program, event) when argv is a hook call, else None. Claude Code and
    Codex hooks run with no arguments or a bare client flag some installers add;
    the other agents' hooks run with --hook <program> [<event>]. Neither is
    trusted: the payload still has to come from that agent."""
    if argv[:1] == ["--hook"] and len(argv) in (2, 3) and argv[1] in AUTOMATIC:
        return argv[1], argv[2] if len(argv) == 3 else None
    if not [a for a in argv if a not in HOOK_FLAGS]:
        return None, None
    return None


HOOK_ERRORS = "hook-errors.log"


def log_hook_error(program: str | None, event: str | None, message: str) -> None:
    """Say it on stderr and keep it in the state directory: an agent that swallows
    a hook's stderr otherwise leaves a failed report indistinguishable from none."""
    sys.stderr.write(message + "\n")
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    path = os.path.join(STATE_DIR, HOOK_ERRORS)
    with contextlib.suppress(OSError):
        os.makedirs(STATE_DIR, exist_ok=True)
        if os.path.isfile(path) and os.path.getsize(path) > 100_000:
            os.replace(path, path + ".1")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {program or 'claude/codex'} {event or '-'} {message}\n")


HOOK_DEADLINE_S = 60.0


def start_deadline(
    program: str | None, event: str | None, fallback: str | None
) -> None:
    """End a hook run that is still going after its deadline. The OpenCode plugin
    and the Cline shim run the reporter with no timeout of their own."""
    import threading

    seconds = float(os.environ.get("HARNESS_LEDGER_HOOK_DEADLINE") or HOOK_DEADLINE_S)

    def expire() -> None:
        message = f"harness-ledger: stopped at its {seconds:g} s deadline"
        log_hook_error(program, event, message + "; nothing more saved")
        if fallback:
            sys.stdout.write(fallback + "\n")
            sys.stdout.flush()
        os._exit(0)

    timer = threading.Timer(seconds, expire)
    timer.daemon = True
    timer.start()


def main() -> None:
    argv = sys.argv[1:]
    if "--selftest" in argv:
        sys.exit(_selftest())
    if "--flush" in argv:
        n = flush_stale(load_config(), "", force=True)
        print(f"harness-ledger: posted {n} pending run(s)")
        return
    target = hook_args(argv)
    if target is None or sys.stdin.isatty():
        sys.exit(cli(argv))
    program, event = target
    # Cursor reads every hook's stdout as JSON and holds the prompt without it.
    fallback = '{"continue": true}' if program == "cursor" else None
    start_deadline(program, event, fallback)
    try:
        # Cursor's hook input starts with a byte-order mark.
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
    except Exception as exc:
        log_hook_error(
            program,
            event,
            f"harness-ledger: unreadable hook input, nothing recorded: {exc!r}",
        )
        out = fallback
    else:
        try:
            out = handle(payload, program, event)
        except Exception as exc:
            import traceback

            line = traceback.extract_tb(exc.__traceback__)[-1].lineno
            message = (
                f"harness-ledger: failed, nothing recorded: {exc!r} at line {line}"
            )
            log_hook_error(program, event, message)
            out = fallback
        if out is None:
            out = fallback
    if out:
        # For the prompt and session-start events, stdout is context the agent sees.
        sys.stdout.write(out + "\n")


if __name__ == "__main__":
    main()
