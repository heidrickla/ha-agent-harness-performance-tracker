#!/usr/bin/env python3
"""Turn real Claude Code sessions into runs for the Agent Harness Performance
Tracker, with nothing for the person to remember.

HOW A RUN HAPPENS

  1. Every turn, Stop appends one ledger line from the transcript bytes past
     the saved offset: tool calls, writes, pushes, tokens (cache reads
     included), duration, denials, prompts, the harness fingerprint.
  2. When the agent ends a piece of work, its final message carries one line:
       Verdict: pass|fail|partial [verified|unverified] [task=<id>] [notes]
     Stop rolls the open span into a run and holds it as PENDING.
  3. The person's next prompt decides:
       /pass, /fail, /partial (or the bare word)  ->  post with that outcome,
                                                     verified=true
       /verdict <outcome> [task=<id>] [class=<x>]  ->  the same, with overrides
       anything else                              ->  post the pending run as
                                                     the agent reported it,
                                                     verified=false
     So the default costs nothing, and disagreeing is one word.
  4. SessionEnd posts a pending run and says on stderr if turns are still
     waiting for a verdict. A pending run left by a killed session is posted
     by the next event of any session once it is six hours old, or by
     `--flush` at once.

`verified` means confirmed by a person; the agent's own "verified" goes into
the notes. Interventions are the person's prompts beyond the first in the
span. The outcome is never inferred from the transcript: a hook grading the
transcript would be the harness scoring itself.

TASK IDS come from the hook: `<directory>:<class>` where class is `publish`
when the span pushed (git push, gh pr/release create), `build` when it wrote
files, `ops` otherwise. The agent may name a repeatable job with `task=` in
its verdict line; a person may with `/verdict fail task=<id>`.

WHAT THE TRANSCRIPT LOOKS LIKE, measured 2026-09-17 on a 22 MB session:
one API response is split across several `assistant` entries sharing a
`requestId`, each carrying the SAME `usage` object, so tokens are summed once
per requestId from `usage.iterations` (3 of 1130 requests had no iterations;
the top-level object is used for those). `input_tokens` alone is the uncached
slice, 6 k against 633 M cache reads over that session, so the three input
fields are summed. A human prompt is a `user` entry whose content is a string
or a list with a `text` block, unless the client wrote it (INJECTED: 242 of the
362 counted in one session on 2026-09-27 were task notifications, meta entries,
compaction summaries and command wrappers); a tool result is a `user` entry with a
`tool_result` block. A denial is an error tool result that opens with a refusal,
classed by denial_class: the classifier's rule, the guard hook, the person, or a
settings rule. Runs carry the classes, so a recurring one is visible and can
become a capability. Checked against an independent count
over the live transcript: 1062 tool calls, 100 prompts, 31 denials, 1127
requests, all equal.

CONFIG, outside every clone, at ~/.config/ha-harness-tracker.json:
  {"webhook_url": "https://<home assistant>/api/webhook/<id>",
   "harness": ["~/work/AGENTS.md", "~/.claude/hooks", "~/.claude/settings.json",
               "~/.claude/skills"],
   "label": "", "insecure": false}
`harness` is the list of files and directories whose bytes define a version;
settings.json is hashed on its permissions and hooks keys only. `insecure`
skips certificate verification for a self-signed Home Assistant. The webhook
id is the reporter's only credential and lives in that file at 0600, never in
a hook argument or a log line. HARNESS_LEDGER_CONFIG and HARNESS_LEDGER_STATE
override the two paths, for tests.

STATE, at ~/.claude/harness-ledger/<session>.jsonl (the ledger),
<session>.state.json (byte offsets, the open span, the pending run) and
versions/<digest>.json (each version's per-file hashes, so two versions can
be diffed by name). All survive a session.

INSTALL: copy this file to ~/.claude/hooks/, then in ~/.claude/settings.json
register `python <path>` as a command hook under Stop, SubagentStop,
UserPromptSubmit, UserPromptExpansion (matcher `verdict|pass|fail|partial`)
and SessionEnd; give the two prompt events a 40 second timeout because they
post. Hooks load at session start. Fail open, and loud: any exception is one
line on stderr and the command or prompt proceeds. Test with
`python <path> --selftest`.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta

CONFIG = os.environ.get("HARNESS_LEDGER_CONFIG") or os.path.expanduser(
    "~/.config/ha-harness-tracker.json"
)
STATE_DIR = os.environ.get("HARNESS_LEDGER_STATE") or os.path.expanduser(
    "~/.claude/harness-ledger"
)


def denial_class(block: dict) -> str | None:
    """Why a tool call was refused, or None when it was not.

    Only an error result that opens with a refusal counts: a result that merely
    contains the words, such as a read of a guard hook's source, is not a denial
    (2026-09-27: 3 of the 21 counted in one session were reads, and the one
    refusal by Lewis was not counted at all).
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


OUTCOMES = ("pass", "fail", "partial")
STALE_PENDING = timedelta(hours=6)
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
WRITE_SUFFIXES = ("write_file", "edit_block", "create_file", "update_file")
SHELL_TOOLS = {"Bash", "PowerShell"}
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


# -------------------------------------------------------------------- verdicts
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
    """What the person typed. UserPromptSubmit carries the prompt; a custom
    command reaches UserPromptExpansion as command_name plus arguments."""
    if payload.get("hook_event_name") == "UserPromptExpansion":
        name = str(payload.get("command_name") or "").lstrip("/").lower()
        if name not in ("verdict", *OUTCOMES):
            return ""
        return f"/{name} {payload.get('arguments') or ''}"
    return str(payload.get("prompt") or "")


# ------------------------------------------------------------------ transcript
def _ts(entry: dict) -> datetime | None:
    raw = entry.get("timestamp")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def _classify_tool(block: dict) -> tuple[int, int]:
    """(writes, pushes) contributed by one tool_use block."""
    name = str(block.get("name") or "")
    if name in WRITE_TOOLS or name.endswith(WRITE_SUFFIXES):
        return 1, 0
    if name in SHELL_TOOLS or name.endswith("PowerShell"):
        inp = block.get("input") if isinstance(block.get("input"), dict) else {}
        command = str(inp.get("command") or inp.get("cmd") or "")
        if PUSH_RE.search(command):
            return 0, 1
    return 0, 0


def parse_slice(path: str, offset: int) -> tuple[dict, int]:
    """Figures for the transcript bytes past `offset`, and the new offset.

    Tokens are counted once per requestId. A human prompt that is itself a
    verdict is not counted: it closes a span rather than starting work.
    """
    figures = {
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
        "client_version": None,
    }
    models: dict[str, int] = {}
    seen_requests: set[str] = set()
    first: datetime | None = None
    last: datetime | None = None
    last_text = ""
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
                    w, p = _classify_tool(b)
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
        os.path.join(cwd or ".", "AGENTS.md"),
        os.path.join(cwd or ".", "CLAUDE.md"),
        "~/.claude/hooks",
    ]
    paths = [p for p in paths if os.path.exists(os.path.expanduser(p))]
    if not paths:
        return "unknown"
    version = fingerprint(paths, cfg.get("label") or None)
    describe_version(version, paths)
    return version


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
    }
    classes: dict[str, int] = {}
    for x in lines:
        for name, n in (x.get("denial_classes") or {}).items():
            classes[str(name)[:80]] = classes.get(str(name)[:80], 0) + int(n)
    if classes:
        top = sorted(classes.items(), key=lambda kv: (-kv[1], kv[0]))[:20]
        run["denial_classes"] = dict(top)
    # Kept out of the harness version: a model change must not read as a harness change.
    models = [str(x["model"]) for x in lines if x.get("model")]
    if models:
        run["model"] = min(set(models), key=lambda m: (-models.count(m), m))
    clients = [str(x["client_version"]) for x in lines if x.get("client_version")]
    if clients:
        run["client_version"] = clients[-1]
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


def send(cfg: dict, run: dict) -> tuple[int, str]:
    """Post a run and keep the tracker's answer for the next session start."""
    status, body = post(cfg, run)
    if 200 <= status < 300:
        save_reply(body)
    return status, body


def describe(run: dict, status: int, body: str) -> str:
    """One line for the conversation about a post that succeeded or failed."""
    if 200 <= status < 300:
        try:
            answer = json.loads(body)
        except ValueError:
            answer = {}
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
    fresh harness version is noise, and printed bare it read as a verdict (100%
    then 0% on one partial)."""
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


def reply_path() -> str:
    return os.path.join(STATE_DIR, "last-reply.json")


def save_reply(body: str) -> None:
    """Keep the tracker's last answer: the session-start line is read from it."""
    try:
        answer = json.loads(body)
    except ValueError:
        return
    if isinstance(answer, dict):
        answer["saved_at"] = datetime.now().astimezone().isoformat(timespec="minutes")
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(reply_path(), "w", encoding="utf-8") as fh:
            json.dump(answer, fh)


def loop_line() -> str | None:
    """Loop 5 at session start: the window gate and the recurring denial classes,
    from the tracker's last answer. A regression or a recurring class is the
    session's first input."""
    answer = _read_json(reply_path(), {})
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
        line = post_pending(cfg, state, path)
        if line and "NOT recorded" not in line:
            posted += 1
    return posted


# ---------------------------------------------------------------------- events
def on_stop(payload: dict, cfg: dict, turns: int) -> dict | None:
    """Append one ledger line; hold a run when the agent gave a verdict.
    turns is 1 for the main agent, 0 for a subagent."""
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
    verdict = figures["self_verdict"] if turns else None
    line = {
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "event": payload.get("hook_event_name"),
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
        "harness_version": harness_version(cfg, payload.get("cwd")),
        "model": figures["model"],
        "client_version": figures["client_version"],
        "cwd": payload.get("cwd"),
        "self_verdict": verdict,
    }
    with open(ledger_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")
    state["offsets"][transcript] = new_offset
    if verdict:
        # A pending run nobody answered (a resumed session) posts as reported.
        post_pending(cfg, state, state_path)
        lines = open_span(ledger_path, state)
        state["pending"] = {
            "run": roll_up(lines, verdict, by_person=False),
            "created": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        state["span_start_line"] = int(state.get("span_start_line", 0)) + len(lines)
    save_state(state_path, state)
    return line


def on_prompt(payload: dict, cfg: dict) -> str | None:
    """A verdict word closes or overrides; any other prompt posts what is
    pending. Both prompt events may fire for one typed prompt: the second is
    told apart by prompt_id, or finds nothing left to do."""
    ledger_path, state_path = paths_for(str(payload.get("session_id")))
    state = load_state(state_path)
    prompt_id = payload.get("prompt_id")
    if prompt_id and state.get("last_prompt_id") == prompt_id:
        return None
    if prompt_id:
        state["last_prompt_id"] = prompt_id
        save_state(state_path, state)
    verdict = human_verdict(verdict_text(payload))
    if verdict is None:
        return post_pending(cfg, state, state_path)
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
        status, body = send(cfg, run)
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
    status, body = send(cfg, run)
    if 200 <= status < 300:
        state["span_start_line"] = int(state.get("span_start_line", 0)) + len(lines)
        save_state(state_path, state)
    return describe(run, status, body)


def on_session_end(payload: dict, cfg: dict) -> None:
    ledger_path, state_path = paths_for(str(payload.get("session_id")))
    state = load_state(state_path)
    line = post_pending(cfg, state, state_path)
    if line:
        sys.stderr.write(line + "\n")
    lines = open_span(ledger_path, state)
    if lines:
        sys.stderr.write(
            f"harness-ledger: {len(lines)} turn(s) have no verdict; "
            f"the span stays open in {ledger_path}\n"
        )


# ------------------------------------------------------------------------ main
def handle(payload: dict) -> str | None:
    cfg = load_config()
    event = payload.get("hook_event_name")
    _, state_path = paths_for(str(payload.get("session_id")))
    flush_stale(cfg, state_path)
    if event == "Stop":
        on_stop(payload, cfg, turns=1)
    elif event == "SubagentStop":
        on_stop(payload, cfg, turns=0)
    elif event in ("UserPromptSubmit", "UserPromptExpansion"):
        return on_prompt(payload, cfg)
    elif event == "SessionEnd":
        on_session_end(payload, cfg)
    elif event == "SessionStart":
        return loop_line()
    return None


def _selftest() -> int:
    """A synthetic transcript with known figures, parsed, held and posted."""
    import tempfile

    global STATE_DIR, post
    tmp = tempfile.mkdtemp()
    STATE_DIR = os.path.join(tmp, "state")
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
            # Client-written user entries: none is a prompt or the first prompt.
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
            # The same API response, second block, same usage: must not double count.
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
            # Output that quotes a refusal, a log or a guard's source, is no denial.
            result(
                "2026-09-17T10:00:08Z",
                "PreToolUse:Bash hook error: BLOCKED -- quoted in a log",
            ),
            result(
                "2026-09-17T10:00:08Z",
                "The user doesn't want to proceed with this tool use. The tool use "
                "was rejected",
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
                        "text": "Done.\n\nVerdict: pass (verified) task=build-x "
                        "first try",
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
    # A settings.json moves the version on a rule change and not on a theme change.
    settings = os.path.join(tmp, "settings.json")

    def settings_version(doc: dict) -> str:
        with open(settings, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        return fingerprint([settings], None)

    v1 = settings_version(
        {"permissions": {"deny": ["Bash(rm:*)"]}, "hooks": {}, "theme": "dark"}
    )
    v2 = settings_version(
        {"permissions": {"deny": ["Bash(rm:*)"]}, "hooks": {}, "theme": "light"}
    )
    v3 = settings_version({"permissions": {"deny": []}, "hooks": {}, "theme": "light"})
    checks.append(("settings.json theme change keeps the version", v1 == v2))
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
    cfg = {"harness": [transcript], "webhook_url": "fake"}
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
        ("a new version gets a manifest of per-file hashes", "t.jsonl" in listed)
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

    # The person disagrees with one word: the pending run posts as their verdict.
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
    start = handle({"hook_event_name": "SessionStart", "session_id": "selftest"})
    checks.append(
        (
            "the next session starts with the window gate and the recurring classes",
            os.path.isfile(reply_path())
            and bool(start)
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

    # Second piece of work, no word from the person: an ordinary prompt posts it.
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

    # The person's verdict with no line from the agent closes the open span.
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

    # Words that are not verdicts.
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

    # Stale pending runs from another session post after six hours, or on --flush.
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

    # Honest trends: model and client version read from the transcript, kept out of the
    # harness version; an unattributable run dropped; the rate printed with its sample.
    mt = os.path.join(tmp, "m.jsonl")
    with open(mt, "w", encoding="utf-8") as fh:
        for e in (
            {
                "type": "user",
                "version": "2.1.280",
                "timestamp": "2026-09-24T10:00:00Z",
                "message": {"role": "user", "content": "go"},
            },
            {
                "type": "assistant",
                "version": "2.1.280",
                "timestamp": "2026-09-24T10:00:01Z",
                "requestId": "m1",
                "message": {
                    "role": "assistant",
                    "model": "claude-x",
                    "content": [{"type": "text", "text": "done"}],
                    "usage": {},
                },
            },
            {
                "type": "assistant",
                "timestamp": "2026-09-24T10:00:02Z",
                "requestId": "m2",
                "message": {
                    "role": "assistant",
                    "model": "<synthetic>",
                    "content": [],
                    "usage": {},
                },
            },
        ):
            fh.write(json.dumps(e) + "\n")
    mfig, _ = parse_slice(mt, 0)
    checks.append(
        ("the model is read and <synthetic> ignored", mfig["model"] == "claude-x")
    )
    checks.append(("the client version is read", mfig["client_version"] == "2.1.280"))
    mrun = roll_up(
        [{"harness_version": "h1", "model": "claude-x", "client_version": "2.1.280"}],
        {"outcome": "pass"},
        by_person=False,
    )
    checks.append(
        (
            "a run carries model and client version outside the harness version",
            mrun.get("model") == "claude-x"
            and mrun.get("client_version") == "2.1.280"
            and mrun["harness_version"] == "h1",
        )
    )
    before = len(sent)
    ustate_path = os.path.join(tmp, "u.json")
    ustate = {"offsets": {}, "pending": {"run": dict(mrun, harness_version="unknown")}}
    note = post_pending(cfg, ustate, ustate_path)
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
    checks.append(
        (
            "an older tracker reply still prints the bare rate",
            rate_text({"pass_rate": 50.0}) == "pass rate 50.0%",
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


def main() -> None:
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    if "--flush" in sys.argv:
        n = flush_stale(load_config(), "", force=True)
        print(f"harness-ledger: posted {n} pending run(s)")
        return
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
