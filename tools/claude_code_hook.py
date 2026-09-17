#!/usr/bin/env python3
"""Turn real sessions into runs for the Agent Harness Performance Tracker.

Four hook events, one file:

  Stop              append one ledger line for the turn that just finished:
                    tool calls, tokens, duration, denials, human prompts, the
                    harness fingerprint. Read only the transcript bytes past
                    the saved offset. No network.
  SubagentStop      the same for a subagent's transcript, folded into the
                    session's ledger with turns=0, so a fan-out's tool calls
                    and denials count.
  UserPromptSubmit  on `/verdict pass|fail|partial [task-id] [--verified]
  UserPromptExpansion  [class=<x>] [notes...]` (or `verdict ...` with no
                    slash), roll every ledger line since the last verdict
                    into one run and post it to the tracker's webhook. The
                    verdict is the one thing a transcript cannot prove:
                    whether the work was right. A custom `/verdict` command
                    arrives on UserPromptExpansion as command_name plus
                    arguments; both events are handled and a double delivery
                    is harmless because the second finds the span empty.
  SessionEnd        if a span is open with no verdict, leave it in the ledger
                    and say so on stderr. No verdict, no run.

WHY A VERDICT AND NOT A GUESS. The tracker exists to tell whether a harness
change helped. A hook that inferred the outcome from the transcript would be
the harness grading its own work, which is the one signal the gate must not
be built on. Turns, tool calls, tokens, duration and denials are facts the
transcript holds; interventions are your prompts beyond the first, which is
attention you had to spend whether it was a correction or an answer; outcome
and verified come from you.

WHAT THE TRANSCRIPT LOOKS LIKE, measured 2026-09-17 on a 22 MB session:
one API response is split across several `assistant` entries sharing a
`requestId`, each carrying the SAME `usage` object, so tokens are summed once
per requestId from `usage.iterations` (3 of 1130 requests had no iterations;
the top-level object is used for those). `input_tokens` alone is the uncached
slice, 6 k against 633 M cache reads over that session, so the three input
fields are summed. A human prompt is a `user` entry whose content is a string
or a list with a `text` block; a tool result is a `user` entry with a
`tool_result` block. A denial is a tool result carrying "Permission for this
action was denied" (the permission system) or "BLOCKED --" (a PreToolUse
guard hook's refusal, by convention). Checked against an independent count
over the live transcript: 1062 tool calls, 100 prompts, 31 denials, 1127
requests, all equal.

CONFIG, outside every clone, at ~/.config/ha-harness-tracker.json:
  {"webhook_url": "https://<home assistant>/api/webhook/<id>",
   "harness": ["~/work/CLAUDE.md", "~/.claude/hooks", "~/.claude/settings.json"],
   "label": "", "insecure": false}
`harness` is the list of files and directories whose bytes define a version;
settings.json is hashed on its permissions and hooks keys only. `insecure`
skips certificate verification for a self-signed Home Assistant. The webhook
id is the reporter's only credential and lives in that file at 0600, never in
a hook argument or a log line. HARNESS_LEDGER_CONFIG and HARNESS_LEDGER_STATE
override the two paths, for tests.

STATE, at ~/.claude/harness-ledger/<session>.jsonl (the ledger),
<session>.state.json (byte offsets and the open span) and versions/<digest>.json
(each version's per-file hashes, so two versions can be diffed by name). All
survive a session; a span left open is closed by the next verdict in any later
session.

INSTALL: copy this file to ~/.claude/hooks/, then in ~/.claude/settings.json
register `python <path>` as a command hook under Stop, SubagentStop,
UserPromptSubmit and UserPromptExpansion (timeout 40, they post) and
SessionEnd. Hooks load at session start. Fail open, and loud: any exception
is one line on stderr and the command or prompt proceeds. Test with
`python <path> --selftest`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime

CONFIG = os.environ.get("HARNESS_LEDGER_CONFIG") or os.path.expanduser(
    "~/.config/ha-harness-tracker.json"
)
STATE_DIR = os.environ.get("HARNESS_LEDGER_STATE") or os.path.expanduser(
    "~/.claude/harness-ledger"
)
DENIAL_MARKS = ("Permission for this action was denied", "BLOCKED --")
# `/verdict ...` or `verdict ...`: the slash form is a custom command, the
# bare form needs no command routing at all.
VERDICT_RE = re.compile(
    r"^\s*/?verdict\s+(?P<outcome>pass|fail|partial)"
    r"(?:\s+(?P<task>[A-Za-z0-9][A-Za-z0-9._:-]*))?"
    r"(?P<rest>.*)$",
    re.I | re.S,
)


# ---------------------------------------------------------------- fingerprint
def _file_bytes(path: str) -> bytes:
    """The bytes that count as harness. settings.json is hashed on its
    `permissions` and `hooks` keys only; theme and notification flags are not
    harness and would make a version move without a rule changing."""
    with open(path, "rb") as fh:
        data = fh.read()
    if os.path.basename(path) == "settings.json":
        try:
            doc = json.loads(data)
            keep = {k: doc.get(k) for k in ("permissions", "hooks")}
            return json.dumps(keep, sort_keys=True).encode()
        except ValueError:
            pass
    return data


def harness_files(paths: list[str]) -> list[tuple[str, bytes]]:
    """(name, bytes) for every file that makes up the harness, in stable order."""
    files: list[tuple[str, bytes]] = []
    for root in sorted(paths):
        root = os.path.expanduser(root)
        if os.path.isdir(root):
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = sorted(
                    d for d in dirnames if not d.startswith((".", "__"))
                )
                for name in sorted(filenames):
                    if name.startswith(".") or name.endswith((".pyc", ".bak")):
                        continue
                    full = os.path.join(dirpath, name)
                    rel = os.path.relpath(full, root).replace(os.sep, "/")
                    files.append((rel, _file_bytes(full)))
        elif os.path.isfile(root):
            files.append((os.path.basename(root), _file_bytes(root)))
    return files


def fingerprint(paths: list[str], label: str | None) -> str:
    """sha256 over the harness files as `sha256:<12 hex>`.

    The twin of fingerprint() in the tracker repository's tools/report_run.py:
    same walk, same skips, name then bytes per file, so a rename changes the
    version as an edit does. Verified equal on the same inputs 2026-09-17.
    """
    h = hashlib.sha256()
    for name, data in harness_files(paths):
        h.update(name.encode())
        h.update(data)
    digest = "sha256:" + h.hexdigest()[:12]
    return f"{label} {digest}" if label else digest


def describe_version(version: str, paths: list[str]) -> None:
    """Write versions/<digest>.json once: each file's own hash, so two versions
    can be diffed by name instead of read as opaque ids."""
    digest = version.split(":")[-1]
    out_dir = os.path.join(STATE_DIR, "versions")
    out = os.path.join(out_dir, f"{digest}.json")
    if os.path.exists(out):
        return
    os.makedirs(out_dir, exist_ok=True)
    manifest = {
        "version": version,
        "recorded": datetime.now().astimezone().isoformat(timespec="seconds"),
        "paths": [os.path.expanduser(p) for p in paths],
        "files": {
            name: hashlib.sha256(data).hexdigest()[:12]
            for name, data in harness_files(paths)
        },
    }
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1, sort_keys=True)


# ------------------------------------------------------------------ transcript
def _ts(entry: dict) -> datetime | None:
    raw = entry.get("timestamp")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_slice(path: str, offset: int) -> tuple[dict, int]:
    """Figures for the transcript bytes past `offset`, and the new offset.

    Tokens are counted once per requestId. A human prompt that is itself a
    /verdict is not counted: it closes a span rather than starting work.
    """
    figures = {
        "tool_calls": 0,
        "denials": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "human_prompts": 0,
        "first_ts": None,
        "last_ts": None,
        "api_calls": 0,
    }
    seen_requests: set[str] = set()
    first: datetime | None = None
    last: datetime | None = None
    with open(path, "rb") as fh:
        fh.seek(offset)
        data = fh.read()
    # Only whole lines: a line still being written is left for the next call.
    cut = data.rfind(b"\n")
    if cut < 0:
        return figures, offset
    data = data[: cut + 1]
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
        message = entry.get("message") or {}
        content = message.get("content")
        if kind == "assistant":
            blocks = content if isinstance(content, list) else []
            figures["tool_calls"] += sum(
                1 for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"
            )
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
            if isinstance(content, str):
                if not VERDICT_RE.match(content):
                    figures["human_prompts"] += 1
            elif isinstance(content, list):
                kinds = {b.get("type") for b in content if isinstance(b, dict)}
                if "tool_result" in kinds:
                    text = json.dumps(content)
                    if any(mark in text for mark in DENIAL_MARKS):
                        figures["denials"] += 1
                elif "text" in kinds:
                    text = " ".join(
                        str(b.get("text", "")) for b in content if isinstance(b, dict)
                    )
                    if not VERDICT_RE.match(text):
                        figures["human_prompts"] += 1
    figures["first_ts"] = first.isoformat() if first else None
    figures["last_ts"] = last.isoformat() if last else None
    return figures, offset + len(data)


# ----------------------------------------------------------------------- state
def _read_json(path: str, default: dict) -> dict:
    """The file's JSON object, or `default` when it is missing or malformed.

    Two clauses on purpose: under this repository's py314 target ruff's
    formatter rewrites `except (A, B):` into the 3.14-only `except A, B:`, and
    this file runs under whatever Python runs Claude Code hooks.
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


def harness_version(cfg: dict, cwd: str | None) -> str:
    paths = cfg.get("harness") or [
        os.path.join(cwd or ".", "CLAUDE.md"),
        "~/.claude/hooks",
    ]
    paths = [p for p in paths if os.path.exists(os.path.expanduser(p))]
    if not paths:
        return "unknown"
    version = fingerprint(paths, cfg.get("label") or None)
    describe_version(version, paths)
    return version


# ---------------------------------------------------------------------- events
def on_stop(payload: dict, cfg: dict, turns: int) -> dict | None:
    """Append one ledger line. turns is 1 for the main agent, 0 for a subagent."""
    transcript = payload.get("transcript_path")
    if not transcript or not os.path.isfile(transcript):
        return None
    ledger_path, state_path = paths_for(str(payload.get("session_id")))
    state = load_state(state_path)
    offset = int(state["offsets"].get(transcript, 0))
    figures, new_offset = parse_slice(transcript, offset)
    if new_offset == offset and turns == 0:
        return None
    duration = 0.0
    if figures["first_ts"] and figures["last_ts"]:
        a = datetime.fromisoformat(figures["first_ts"])
        b = datetime.fromisoformat(figures["last_ts"])
        duration = max(0.0, (b - a).total_seconds())
    line = {
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "event": payload.get("hook_event_name"),
        "turns": turns,
        "tool_calls": figures["tool_calls"],
        "api_calls": figures["api_calls"],
        "denials": figures["denials"],
        "input_tokens": figures["input_tokens"],
        "output_tokens": figures["output_tokens"],
        "human_prompts": figures["human_prompts"],
        "duration_s": round(duration, 1),
        "harness_version": harness_version(cfg, payload.get("cwd")),
        "cwd": payload.get("cwd"),
    }
    with open(ledger_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")
    state["offsets"][transcript] = new_offset
    save_state(state_path, state)
    return line


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


def roll_up(lines: list[dict], outcome: str, task: str | None, rest: str) -> dict:
    """One run from the span's ledger lines and the verdict's words."""
    tokens = rest.split()
    verified = "--verified" in tokens
    task_class = next(
        (t.split("=", 1)[1] for t in tokens if t.startswith("class=")), None
    )
    notes = " ".join(
        t for t in tokens if t != "--verified" and not t.startswith("class=")
    )
    human = sum(int(x.get("human_prompts", 0)) for x in lines)
    versions = [x.get("harness_version") for x in lines if x.get("harness_version")]
    version = versions[0] if versions else "unknown"
    if len(set(versions)) > 1:
        notes = (notes + " harness changed during the task").strip()
    run: dict = {
        "harness_version": version,
        "outcome": outcome,
        "verified": verified,
        "turns": sum(int(x.get("turns", 0)) for x in lines),
        "tool_calls": sum(int(x.get("tool_calls", 0)) for x in lines),
        "duration_s": round(sum(float(x.get("duration_s", 0)) for x in lines), 1),
        "input_tokens": sum(int(x.get("input_tokens", 0)) for x in lines),
        "output_tokens": sum(int(x.get("output_tokens", 0)) for x in lines),
        "denials": sum(int(x.get("denials", 0)) for x in lines),
        "interventions": max(0, human - 1),
    }
    if task:
        run["task_id"] = task
    if task_class:
        run["task_class"] = task_class
    if notes:
        run["notes"] = notes[:500]
    return run


def post(cfg: dict, run: dict) -> tuple[int, str]:
    url = cfg.get("webhook_url")
    if not url:
        return 0, "no webhook_url in " + CONFIG
    data = json.dumps(run).encode()
    req = urllib.request.Request(
        url, data=data, method="POST", headers={"Content-Type": "application/json"}
    )
    ctx = ssl._create_unverified_context() if cfg.get("insecure") else None
    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as err:
        return 0, str(err)


def verdict_text(payload: dict) -> str:
    """The verdict as typed. UserPromptSubmit carries the prompt; a custom
    command reaches UserPromptExpansion as command_name plus arguments."""
    if payload.get("hook_event_name") == "UserPromptExpansion":
        if str(payload.get("command_name") or "").lstrip("/") != "verdict":
            return ""
        return f"/verdict {payload.get('arguments') or ''}"
    return str(payload.get("prompt") or "")


def on_prompt(payload: dict, cfg: dict) -> str | None:
    """Close the open span on a verdict. Returns a line for the transcript.

    Both prompt events may fire for one typed verdict; the first closes the
    span and the second finds it empty, so nothing posts twice.
    """
    m = VERDICT_RE.match(verdict_text(payload))
    if not m:
        return None
    # The turn that just ended has not been ledgered if this prompt arrived
    # before its Stop fired; Stop runs before the prompt is accepted, so it has.
    ledger_path, state_path = paths_for(str(payload.get("session_id")))
    state = load_state(state_path)
    lines = open_span(ledger_path, state)
    if not lines:
        return (
            "harness-ledger: no turns recorded since the last verdict; nothing posted"
        )
    run = roll_up(
        lines, m.group("outcome").lower(), m.group("task"), m.group("rest") or ""
    )
    status, body = post(cfg, run)
    if 200 <= status < 300:
        state["span_start_line"] = int(state.get("span_start_line", 0)) + len(lines)
        save_state(state_path, state)
        try:
            answer = json.loads(body)
        except ValueError:
            answer = {}
        task = run.get("task_id", "(no task id)")
        return (
            f"harness-ledger: recorded run {task} as {run['outcome']} on "
            f"{run['harness_version']} - {run['turns']} turns, "
            f"{run['tool_calls']} tool calls, {run['denials']} denials, "
            f"{run['interventions']} interventions; tracker now at "
            f"{answer.get('run_count')} runs, pass rate {answer.get('pass_rate')}%, "
            f"regressed={answer.get('regressed')}"
        )
    return (
        f"harness-ledger: NOT recorded ({status} {body[:160]}); "
        "the span stays open for a retry"
    )


def on_session_end(payload: dict) -> None:
    ledger_path, state_path = paths_for(str(payload.get("session_id")))
    lines = open_span(ledger_path, load_state(state_path))
    if lines:
        sys.stderr.write(
            f"harness-ledger: {len(lines)} turn(s) have no verdict; "
            f"the span stays open in {ledger_path}\n"
        )


# ------------------------------------------------------------------------ main
def handle(payload: dict) -> str | None:
    cfg = load_config()
    event = payload.get("hook_event_name")
    if event == "Stop":
        on_stop(payload, cfg, turns=1)
    elif event == "SubagentStop":
        on_stop(payload, cfg, turns=0)
    elif event in ("UserPromptSubmit", "UserPromptExpansion"):
        return on_prompt(payload, cfg)
    elif event == "SessionEnd":
        on_session_end(payload)
    return None


def _selftest() -> int:
    """A synthetic transcript with known figures, parsed and rolled up."""
    import tempfile

    tmp = tempfile.mkdtemp()
    transcript = os.path.join(tmp, "t.jsonl")

    def human(ts: str, text: str) -> dict:
        return {
            "type": "user",
            "timestamp": ts,
            "message": {"role": "user", "content": text},
        }

    def result(ts: str, text: str) -> dict:
        block = {"type": "tool_result", "content": text}
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
    entries = [
        human("2026-09-17T10:00:00Z", "Build it"),
        assistant(
            "2026-09-17T10:00:05Z",
            "r1",
            [{"type": "text", "text": "ok"}, {"type": "tool_use"}],
            usage_r1,
        ),
        # The same API response, second block, same usage: must not double count.
        assistant("2026-09-17T10:00:06Z", "r1", [{"type": "tool_use"}], usage_r1),
        result(
            "2026-09-17T10:00:07Z",
            "Permission for this action was denied by the classifier",
        ),
        result("2026-09-17T10:00:08Z", "BLOCKED -- a heredoc"),
        result("2026-09-17T10:00:09Z", "fine"),
        human("2026-09-17T10:01:00Z", "no, the other one"),
        assistant("2026-09-17T10:01:30Z", "r2", [{"type": "tool_use"}], usage_r2),
        human("2026-09-17T10:02:00Z", "/verdict pass build-x --verified"),
    ]
    with open(transcript, "w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")
    figures, new_offset = parse_slice(transcript, 0)
    checks = [
        ("tool calls counted across blocks", figures["tool_calls"] == 3),
        (
            "tokens counted once per request, cache included",
            figures["input_tokens"] == 1350 and figures["output_tokens"] == 25,
        ),
        ("two denials, one plain result ignored", figures["denials"] == 2),
        ("two human prompts, the verdict excluded", figures["human_prompts"] == 2),
        (
            "duration spans the slice",
            figures["first_ts"].startswith("2026-09-17T10:00:00")
            and figures["last_ts"].startswith("2026-09-17T10:02:00"),
        ),
        ("offset advances to the end", new_offset == os.path.getsize(transcript)),
        (
            "a re-read from the new offset finds nothing",
            parse_slice(transcript, new_offset)[0]["tool_calls"] == 0,
        ),
    ]
    global STATE_DIR
    STATE_DIR = os.path.join(tmp, "state")
    # A settings.json moves the version on a rule change and not on a theme change.
    settings = os.path.join(tmp, "settings.json")
    with open(settings, "w", encoding="utf-8") as fh:
        json.dump(
            {"permissions": {"deny": ["Bash(rm:*)"]}, "hooks": {}, "theme": "dark"}, fh
        )
    v1 = fingerprint([settings], None)
    with open(settings, "w", encoding="utf-8") as fh:
        json.dump(
            {"permissions": {"deny": ["Bash(rm:*)"]}, "hooks": {}, "theme": "light"}, fh
        )
    v2 = fingerprint([settings], None)
    with open(settings, "w", encoding="utf-8") as fh:
        json.dump({"permissions": {"deny": []}, "hooks": {}, "theme": "light"}, fh)
    v3 = fingerprint([settings], None)
    checks.append(("settings.json theme change keeps the version", v1 == v2))
    checks.append(("settings.json deny-rule change moves the version", v1 != v3))
    payload = {
        "hook_event_name": "Stop",
        "session_id": "selftest",
        "transcript_path": transcript,
        "cwd": tmp,
    }
    line = on_stop(payload, {"harness": [transcript]}, turns=1)
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
        ("a new version gets a manifest of per-file hashes", "t.jsonl" in listed)
    )
    line2 = on_stop(payload, {"harness": [transcript]}, turns=1)
    checks.append(
        (
            "a second stop with nothing new still counts the turn",
            line2 is not None and line2["tool_calls"] == 0,
        )
    )
    ledger_path, state_path = paths_for("selftest")
    lines = open_span(ledger_path, load_state(state_path))
    run = roll_up(lines, "pass", "build-x", " --verified class=build first try")
    checks.extend(
        [
            ("roll-up sums turns", run["turns"] == 2),
            ("roll-up sums tool calls", run["tool_calls"] == 3),
            ("interventions are prompts beyond the first", run["interventions"] == 1),
            ("verified flag parsed", run["verified"] is True),
            ("class parsed", run.get("task_class") == "build"),
            ("notes keep the rest", run.get("notes") == "first try"),
        ]
    )
    m = VERDICT_RE.match("/verdict FAIL --verified")
    checks.append(
        ("verdict without a task id parses", bool(m) and m.group("task") is None)
    )
    m = VERDICT_RE.match("/verdict partial hacs-audit class=publish")
    checks.append(
        (
            "verdict with task and class parses",
            bool(m) and m.group("task") == "hacs-audit",
        )
    )
    checks.append(
        (
            "an ordinary prompt is not a verdict",
            VERDICT_RE.match("verdict on this?") is None,
        )
    )
    m = VERDICT_RE.match("verdict pass no-slash")
    checks.append(
        (
            "the bare form without a slash parses",
            bool(m) and m.group("task") == "no-slash",
        )
    )
    expansion = {
        "hook_event_name": "UserPromptExpansion",
        "command_name": "verdict",
        "arguments": "fail build-x class=build",
    }
    m = VERDICT_RE.match(verdict_text(expansion))
    checks.append(
        (
            "a custom command's expansion event is a verdict",
            bool(m) and m.group("outcome") == "fail" and m.group("task") == "build-x",
        )
    )
    other = {**expansion, "command_name": "review"}
    checks.append(("another command's expansion is not", verdict_text(other) == ""))
    # No config: the post is refused loudly and the span stays open.
    status, body = post({}, run)
    checks.append(
        (
            "posting without a webhook is refused, not silent",
            status == 0 and "webhook_url" in body,
        )
    )
    ok = True
    for label, passed in checks:
        ok = ok and passed
        print(("  ok   " if passed else "  FAIL ") + label)
    print(f"{sum(p for _, p in checks)}/{len(checks)} checks passed")
    return 0 if ok else 1


def main() -> None:
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
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
        # For UserPromptSubmit, stdout becomes context the assistant sees.
        sys.stdout.write(out + "\n")


if __name__ == "__main__":
    main()
