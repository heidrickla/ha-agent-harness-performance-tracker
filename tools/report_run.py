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
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta

try:
    import tomllib
except ImportError:  # Python 3.10: TOML files are hashed byte for byte
    tomllib = None  # type: ignore[assignment]

SCHEMA = 2
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

AUTOMATIC = ("claude_code", "codex")
PROGRAM_NAMES = {"claude_code": "Claude Code", "codex": "Codex"}
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


def _read(path: str) -> bytes | None:
    try:
        if os.path.getsize(path) > MAX_FILE_BYTES:
            return None
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def projected(path: str, sel: Selection, cwd: str | None) -> tuple[bytes, dict]:
    """The bytes that count for a recognised file, and its manifest detail."""
    data = _read(path) or b""
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
) -> None:
    """Add a file, or every file under a directory, once."""
    path = os.path.abspath(os.path.expanduser(path))
    if os.path.isfile(path):
        if not sel.claim(path) or os.path.basename(path) in exclude:
            return
        data, detail = projected(path, sel, cwd)
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
                if k in ("command", "args") and isinstance(v, (str, list)):
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
        return select_claude(cwd) if program == "claude_code" else select_codex(cwd)
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
    """The webhook and TLS choice for one agent program; the 0.3 top level otherwise."""
    agents = cfg.get("agents") if isinstance(cfg.get("agents"), dict) else {}
    own = agents.get(program)
    if isinstance(own, dict) and own.get("webhook_url"):
        return own
    return cfg


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
    The words verified/unverified (bare or in brackets) and --verified are
    flags, not notes."""
    overrides: dict = {}
    words: list[str] = []
    for tok in rest.split():
        low = tok.lower().strip("(),.;")
        if low.startswith("task=") and len(low) > 5:
            overrides["task_id"] = tok.split("=", 1)[1].strip("(),.;")
        elif low.startswith("class=") and len(low) > 6:
            overrides["task_class"] = tok.split("=", 1)[1].strip("(),.;")
        elif low in ("verified", "unverified", "--verified"):
            overrides["claimed_verified"] = low == "verified" or low == "--verified"
        else:
            words.append(tok)
    return overrides, " ".join(words).strip()


def self_verdict(text: str) -> dict | None:
    """The agent's verdict line in its final message, or None."""
    matches = list(SELF_VERDICT_RE.finditer(text or ""))
    if not matches:
        return None
    m = matches[-1]
    overrides, notes = _split_rest(m.group("rest") or "")
    return {"outcome": m.group("outcome").lower(), "notes": notes, **overrides}


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


def _classify_tool(name: str, command: str) -> tuple[int, int]:
    """(writes, pushes) contributed by one tool call."""
    if name in WRITE_TOOLS or name.endswith(WRITE_SUFFIXES):
        return 1, 0
    if (name in SHELL_TOOLS or name.endswith("PowerShell")) and PUSH_RE.search(command):
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
    with open(path, "rb") as fh:
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
    figures["self_verdict"] = self_verdict(last_text)
    figures["first_ts"] = first.isoformat() if first else None
    figures["last_ts"] = last.isoformat() if last else None
    return figures, new_offset


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
    run: dict = {
        "harness_version": version,
        "outcome": verdict["outcome"],
        "verified": by_person,
        "task_id": task_id,
        "task_class": task_class,
        "turns": sum(int(x.get("turns", 0)) for x in lines),
        "tool_calls": sum(int(x.get("tool_calls", 0)) for x in lines),
        "duration_s": round(sum(float(x.get("duration_s", 0)) for x in lines), 1),
        "input_tokens": sum(int(x.get("input_tokens", 0)) for x in lines),
        "output_tokens": sum(int(x.get("output_tokens", 0)) for x in lines),
        "denials": sum(int(x.get("denials", 0)) for x in lines),
        "interventions": max(0, human - 1),
        "notes": "; ".join(parts)[:500],
        "fingerprint_schema": SCHEMA,
    }
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
            f"{run['tool_calls']} tool calls, {run['denials']} denials, "
            f"{run['interventions']} interventions; tracker now at "
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
    payload: dict, cfg: dict, turns: int, program: str = "claude_code"
) -> dict | None:
    """Append one ledger line; hold a run when the agent gave a verdict.
    turns is 1 for the main agent, 0 for a subagent."""
    transcript = payload.get("transcript_path")
    if not transcript or not os.path.isfile(transcript):
        return None
    ledger_path, state_path = paths_for(str(payload.get("session_id")))
    state = load_state(state_path)
    offset = int(state["offsets"].get(transcript, 0))
    parse = parse_codex_slice if program == "codex" else parse_slice
    figures, new_offset = parse(transcript, offset)
    if new_offset == offset and turns == 0:
        return None
    if program == "codex":
        figures["human_prompts"] = int(state.pop("prompts", 0))
        figures["first_prompt"] = str(state.pop("first_prompt", ""))
        if (
            turns
            and not figures["self_verdict"]
            and payload.get("last_assistant_message")
        ):
            figures["self_verdict"] = self_verdict(
                str(payload["last_assistant_message"])
            )
    duration = 0.0
    if figures["first_ts"] and figures["last_ts"]:
        a = datetime.fromisoformat(figures["first_ts"])
        b = datetime.fromisoformat(figures["last_ts"])
        duration = max(0.0, (b - a).total_seconds())
    verdict = figures["self_verdict"] if turns else None
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
    state["offsets"][transcript] = new_offset
    if verdict:
        # A pending run nobody answered (a resumed session) posts as reported.
        post_pending(agent_cfg, state, state_path)
        lines = open_span(ledger_path, state)
        state["pending"] = {
            "run": roll_up(lines, verdict, by_person=False),
            "created": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        state["span_start_line"] = int(state.get("span_start_line", 0)) + len(lines)
    save_state(state_path, state)
    return line


def on_prompt(payload: dict, cfg: dict, program: str = "claude_code") -> str | None:
    """A verdict word closes or overrides; any other prompt posts what is
    pending. Both Claude prompt events may fire for one typed prompt: the second
    is told apart by prompt_id, or finds nothing left to do."""
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
        run["outcome"] = verdict["outcome"]
        run["verified"] = True
        for key in ("task_id", "task_class"):
            if verdict.get(key):
                run[key] = verdict[key]
        run["notes"] = "; ".join(
            p
            for p in ("confirmed by hand", verdict.get("notes"), run.get("notes"))
            if p
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


def on_session_end(payload: dict, cfg: dict, program: str = "claude_code") -> None:
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


def handle(payload: dict) -> str | None:
    program = identify(payload)
    if program is None:
        return None
    cfg = load_config()
    if not agent_config(cfg, program).get("webhook_url"):
        # Set up for another agent on this machine: nothing to record for this one.
        return None
    event = payload.get("hook_event_name")
    _, state_path = paths_for(str(payload.get("session_id")))
    flush_stale(cfg, state_path)
    if event == "Stop":
        on_stop(payload, cfg, turns=1, program=program)
    elif event == "SubagentStop":
        on_stop(payload, cfg, turns=0, program=program)
    elif event in ("UserPromptSubmit", "UserPromptExpansion"):
        return on_prompt(payload, cfg, program)
    elif event == "SessionEnd":
        on_session_end(payload, cfg, program)
    elif event == "SessionStart":
        # Short: a session-start hook is often given ten seconds in all.
        fetch_settings(agent_config(cfg, program), program, timeout=5)
        return loop_line(program)
    return None


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


def register_hook(program: str, script: str) -> tuple[str, int, str | None]:
    """(file, events added, backup) after merging the hook into its config."""
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


def write_config(cfg: dict) -> None:
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    tmp = CONFIG + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=1)
    os.replace(tmp, CONFIG)
    with contextlib.suppress(OSError):
        os.chmod(CONFIG, 0o600)


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
        print("Report runs with: python report_run.py --outcome pass|fail|partial")
        print("from the agent's last step, its own end-of-task hook or a script.")
        return 0
    home = claude_home() if program == "claude_code" else codex_home()
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


def _program_for(cfg: dict, wanted: str | None) -> str:
    if wanted:
        return wanted
    agents = cfg.get("agents") if isinstance(cfg.get("agents"), dict) else {}
    if len(agents) == 1:
        return next(iter(agents))
    return "claude_code" if cfg.get("webhook_url") else "other"


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
                "confirmed by hand; self-reported, verified by effect; first try"
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
            "a Verdict line mid-message still counts",
            self_verdict("Verdict: fail\nmore text")
            == {"outcome": "fail", "notes": ""},
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


def main() -> None:
    argv = sys.argv[1:]
    if "--selftest" in argv:
        sys.exit(_selftest())
    if "--flush" in argv:
        n = flush_stale(load_config(), "", force=True)
        print(f"harness-ledger: posted {n} pending run(s)")
        return
    # A hook runs with no arguments, or with a client flag some installers add. The
    # flag is not trusted: the payload still has to identify its client.
    if [a for a in argv if a not in HOOK_FLAGS] or sys.stdin.isatty():
        sys.exit(cli(argv))
    try:
        payload = json.load(sys.stdin)
    except Exception as exc:
        sys.stderr.write(
            f"harness-ledger: unreadable hook input, nothing recorded: {exc!r}\n"
        )
        return
    try:
        out = handle(payload)
    except Exception as exc:
        sys.stderr.write(f"harness-ledger: failed, nothing recorded: {exc!r}\n")
        return
    if out:
        # For the prompt and session-start events, stdout is context the agent sees.
        sys.stdout.write(out + "\n")


if __name__ == "__main__":
    main()
