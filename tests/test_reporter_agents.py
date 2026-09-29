"""The reporter's automatic handling of Copilot CLI, Cursor, Antigravity, Cline,
OpenCode and Kilo: file profiles, hook payloads, transcripts and registration.

Payload and transcript shapes follow records captured from each agent's real
install (Copilot CLI 1.0.89, Cursor CLI 2026.09.28, agy 1.2.13, Cline CLI
3.0.65, OpenCode 1.18.33, Kilo 7.8.1), with identifying values replaced.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys

import pytest

from tests.test_reporter import ROOT, keys, rr, write

URL = "https://homeassistant.local:8123/api/webhook/x"


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / "proj" / ".git").mkdir(parents=True)
    monkeypatch.setattr(rr, "HOME", str(h))
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    monkeypatch.setattr(rr, "STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(rr, "CONFIG", str(tmp_path / "config.json"))
    for name in (
        "CLAUDE_CONFIG_DIR",
        "CODEX_HOME",
        "COPILOT_HOME",
        "CLINE_DIR",
        "CLINE_DATA_DIR",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
    ):
        monkeypatch.delenv(name, raising=False)
    return h


@pytest.fixture
def posted(monkeypatch):
    sent: list[dict] = []

    def fake_post(cfg, run):
        sent.append(run)
        return 200, json.dumps({"run_count": len(sent), "pass_rate": 100.0})

    monkeypatch.setattr(rr, "post", fake_post)
    monkeypatch.setattr(rr, "fetch_settings", lambda cfg, program, timeout=10: None)
    return sent


def configure(program):
    write(rr.CONFIG, {"agents": {program: {"webhook_url": URL}}})
    os.makedirs(rr.STATE_DIR, exist_ok=True)
    write(
        rr.settings_path(program), {"selection": "automatic", "agent_program": program}
    )


def jsonl(path, rows):
    return write(path, "".join(json.dumps(r) + "\n" for r in rows))


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# ------------------------------------------------------------------ Copilot CLI
def copilot_rows():
    return [
        {
            "type": "session.start",
            "data": {
                "sessionId": "cdfa762a-3f36-4811-922e-51ae5caa45f7",
                "copilotVersion": "1.0.89",
            },
            "timestamp": "2026-09-29T17:58:13Z",
        },
        {
            "type": "user.message",
            "data": {
                "content": "ship the fix",
                "responsesReasoning": {"model": "gpt-x", "effort": "medium"},
            },
            "timestamp": "2026-09-29T17:58:16Z",
        },
        {
            "type": "assistant.message",
            "data": {"model": "gpt-x", "content": "", "toolRequests": [{}]},
            "timestamp": "2026-09-29T17:58:19Z",
        },
        {
            "type": "tool.execution_start",
            "data": {
                "toolCallId": "c1",
                "toolName": "powershell",
                "arguments": {"command": "git push origin main"},
            },
            "timestamp": "2026-09-29T17:58:20Z",
        },
        {
            "type": "tool.execution_complete",
            "data": {"toolCallId": "c1", "success": True},
            "timestamp": "2026-09-29T17:58:21Z",
        },
        {
            "type": "tool.execution_start",
            "data": {
                "toolCallId": "c2",
                "toolName": "powershell",
                "arguments": {"command": "New-Item x"},
            },
            "timestamp": "2026-09-29T17:58:22Z",
        },
        {
            "type": "permission.completed",
            "data": {
                "toolCallId": "c2",
                "result": {
                    "kind": "denied-no-approval-rule-and-could-not-request-from-user"
                },
            },
            "timestamp": "2026-09-29T17:58:22Z",
        },
        {
            "type": "tool.execution_complete",
            "data": {"toolCallId": "c2", "success": False, "error": {"code": "denied"}},
            "timestamp": "2026-09-29T17:58:22Z",
        },
        {
            "type": "assistant.message",
            "data": {"model": "gpt-x", "content": "done\nVerdict: pass task=capture"},
            "timestamp": "2026-09-29T17:58:26Z",
        },
    ]


def test_copilot_events_figures(tmp_path):
    rows = [
        *copilot_rows(),
        {
            "type": "session.shutdown",
            "data": {
                "tokenDetails": {
                    "input": {"tokenCount": 100},
                    "cache_read": {"tokenCount": 50},
                    "output": {"tokenCount": 7},
                }
            },
            "timestamp": "2026-09-29T17:58:27Z",
        },
    ]
    path = jsonl(str(tmp_path / "events.jsonl"), rows)
    fig, offset = rr.parse_copilot_slice(path, 0)
    assert (fig["tool_calls"], fig["pushes"], fig["human_prompts"]) == (2, 1, 1)
    assert fig["denials"] == 1
    assert fig["denial_classes"] == {
        "permission:denied-no-approval-rule-and-could-not-request-from-user": 1
    }
    assert (fig["model"], fig["effort"], fig["client_version"]) == (
        "gpt-x",
        "medium",
        "1.0.89",
    )
    assert (fig["input_tokens"], fig["output_tokens"]) == (150, 7)
    assert fig["self_verdict"]["task_id"] == "capture"
    assert offset == os.path.getsize(path)


def test_copilot_tokens_are_unknown_before_shutdown(tmp_path):
    fig, _ = rr.parse_copilot_slice(jsonl(str(tmp_path / "e.jsonl"), copilot_rows()), 0)
    assert fig["input_tokens"] is None and fig["output_tokens"] is None


def copilot_payload(home, **extra):
    return {
        "sessionId": "cdfa762a-3f36-4811-922e-51ae5caa45f7",
        "timestamp": 1790704706150,
        "cwd": str(home / "proj"),
        **extra,
    }


def test_copilot_payload_identity(home):
    transcript = jsonl(
        str(
            home
            / ".copilot"
            / "session-state"
            / "cdfa762a-3f36-4811-922e-51ae5caa45f7"
            / "events.jsonl"
        ),
        copilot_rows(),
    )
    stop = copilot_payload(home, transcriptPath=transcript, stopReason="end_turn")
    ev = rr.normalize(stop, "copilot_cli", "agentStop")
    assert ev["hook_event_name"] == "Stop" and ev["transcript_path"] == transcript
    # Not Claude's or Cursor's payload; a transcript claimed elsewhere is refused.
    assert rr.normalize(stop) is None
    assert rr.normalize(stop, "cursor", None) is None
    assert (
        rr.normalize(
            copilot_payload(home, transcriptPath=str(home / "x.jsonl")),
            "copilot_cli",
            "agentStop",
        )
        is None
    )
    assert (
        rr.normalize({**stop, "hook_event_name": "Stop"}, "copilot_cli", "agentStop")
        is None
    )
    assert rr.normalize(stop, "copilot_cli", "notAnEvent") is None
    assert (
        rr.normalize(
            copilot_payload(home, sessionId="0000aaaa-3f36-4811-922e-51ae5caa45f7"),
            "copilot_cli",
            "sessionEnd",
        )
        is None
    )


def test_copilot_run_waits_for_the_session_end(home, posted):
    configure("copilot_cli")
    write(str(home / "proj" / "AGENTS.md"), "rules")
    transcript = jsonl(
        str(
            home
            / ".copilot"
            / "session-state"
            / "cdfa762a-3f36-4811-922e-51ae5caa45f7"
            / "events.jsonl"
        ),
        copilot_rows(),
    )
    assert (
        rr.handle(
            copilot_payload(home, prompt="ship the fix"),
            "copilot_cli",
            "userPromptSubmitted",
        )
        == ""
    )
    rr.handle(
        copilot_payload(home, transcriptPath=transcript, stopReason="end_turn"),
        "copilot_cli",
        "agentStop",
    )
    assert posted == []
    rr.handle(copilot_payload(home, reason="complete"), "copilot_cli", "sessionEnd")
    (run,) = posted
    assert run["client"] == "copilot_cli" and run["task_id"] == "capture"
    assert run["denials"] == 1 and run["tool_calls"] == 2
    assert "input_tokens" not in run
    assert run["harness_manifest"]["program"] == "copilot_cli"


# ------------------------------------------------------------------------ Cursor
def cursor_rows():
    return [
        {
            "role": "user",
            "message": {
                "content": [
                    {
                        "type": "text",
                        "text": "<timestamp>Tuesday</timestamp>\n"
                        "<user_query>\nrun it\n</user_query>",
                    }
                ]
            },
        },
        {
            "role": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "Running."},
                    {
                        "type": "tool_use",
                        "name": "Shell",
                        "input": {"command": "git push"},
                    },
                ]
            },
        },
        {
            "role": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "done\nVerdict: pass task=capture"}
                ]
            },
        },
        {"type": "turn_ended", "status": "success"},
    ]


def cursor_transcript(home, name="proj"):
    # Cursor names the project directory after the whole working path, which can
    # take the transcript past Windows' 260-character limit.
    project = "C-" + "-".join(["segment"] * 30) + "-" + name
    conv = "4730dfbd-4f1a-4b23-bac3-f9b7f5c5f381"
    folder = os.path.join(
        str(home), ".cursor", "projects", project, "agent-transcripts", conv
    )
    os.makedirs(rr.long_path(folder), exist_ok=True)
    path = os.path.join(folder, conv + ".jsonl")
    with open(rr.long_path(path), "w", encoding="utf-8") as fh:
        fh.write("".join(json.dumps(r) + "\n" for r in cursor_rows()))
    return path, conv


def test_cursor_transcript_figures_past_the_path_limit(home):
    path, _ = cursor_transcript(home)
    assert len(path) > 260
    fig, offset = rr.parse_cursor_slice(path, 0)
    assert (fig["tool_calls"], fig["pushes"], fig["human_prompts"]) == (1, 1, 1)
    assert fig["first_prompt"] == "run it"
    assert fig["denials"] is None and fig["input_tokens"] is None
    assert fig["self_verdict"]["outcome"] == "pass" and offset > 0


def cursor_payload(home, event, transcript, conv, **extra):
    return {
        "conversation_id": conv,
        "session_id": conv,
        "hook_event_name": event,
        "cursor_version": "2026.09.28-64d2043",
        "model": "default",
        "workspace_roots": [str(home / "proj")],
        "transcript_path": transcript,
        **extra,
    }


def test_cursor_print_mode_posts_at_session_end(home, posted):
    configure("cursor")
    write(str(home / "proj" / "AGENTS.md"), "rules")
    path, conv = cursor_transcript(home)
    start = cursor_payload(home, "sessionStart", None, conv)
    assert rr.handle(start, "cursor") == "{}"
    end = cursor_payload(
        home, "sessionEnd", path, conv, reason="completed", duration_ms=17159
    )
    assert rr.handle(end, "cursor") == "{}"
    (run,) = posted
    assert (run["client"], run["model"], run["client_version"]) == (
        "cursor",
        "default",
        "2026.09.28-64d2043",
    )
    assert run["duration_s"] == 17.2 and run["turns"] == 1
    assert "denials" not in run


def test_cursor_interactive_stop_then_end_is_one_turn(home, posted):
    configure("cursor")
    write(str(home / "proj" / "AGENTS.md"), "rules")
    path, conv = cursor_transcript(home)
    rr.handle(
        cursor_payload(home, "stop", path, conv, status="completed", loop_count=0),
        "cursor",
    )
    assert posted == []  # held for the person's next prompt
    rr.handle(
        cursor_payload(home, "sessionEnd", path, conv, reason="completed"), "cursor"
    )
    (run,) = posted
    assert run["turns"] == 1
    # The end added no line: a second one would open a span with a turn nobody took.
    ledger, _ = rr.paths_for(conv)
    assert len(read(ledger).splitlines()) == 1


def test_cursor_prompt_answers_continue_and_foreign_paths_are_refused(home, posted):
    configure("cursor")
    path, conv = cursor_transcript(home)
    prompt = cursor_payload(home, "beforeSubmitPrompt", path, conv, prompt="hello")
    assert rr.handle(prompt, "cursor") == '{"continue": true}'
    # A real file, outside Cursor's store.
    outside = jsonl(str(home / "x.jsonl"), cursor_rows())
    elsewhere = cursor_payload(home, "sessionEnd", outside, conv)
    assert rr.normalize(elsewhere, "cursor") is None
    no_version = {k: v for k, v in prompt.items() if k != "cursor_version"}
    assert rr.normalize(no_version, "cursor") is None


# ------------------------------------------------------------------- Antigravity
def agy_rows():
    return [
        {
            "step_index": 0,
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "status": "DONE",
            "created_at": "2026-09-29T17:58:14Z",
            "content": "<USER_REQUEST>\nrun it\n</USER_REQUEST>",
        },
        {
            "step_index": 1,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "created_at": "2026-09-29T17:58:15Z",
            "tool_calls": [{"name": "run_command", "args": {"CommandLine": "echo hi"}}],
        },
        {
            "step_index": 2,
            "source": "MODEL",
            "type": "GENERIC",
            "status": "DONE",
            "created_at": "2026-09-29T17:58:20Z",
            "content": "hi",
        },
        {
            "step_index": 3,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "created_at": "2026-09-29T17:58:22Z",
            "content": "done\nVerdict: pass task=capture",
        },
    ]


def agy_payload(home, transcript):
    return {
        "artifactDirectoryPath": os.path.dirname(transcript),
        "conversationId": "6fcab4e5-7bcf-482b-ab0c-bfc58cf44d89",
        "modelName": "gemini-x",
        "transcriptPath": transcript,
        "workspacePaths": [str(home / "proj")],
        "fullyIdle": True,
        "terminationReason": "NO_TOOL_CALL",
    }


def agy_transcript(home):
    brain = (
        home
        / ".gemini"
        / "antigravity-cli"
        / "brain"
        / "6fcab4e5-7bcf-482b-ab0c-bfc58cf44d89"
    )
    return jsonl(
        str(brain / ".system_generated" / "logs" / "transcript_full.jsonl"), agy_rows()
    )


def test_agy_steps_figures(home):
    fig, _ = rr.parse_agy_slice(agy_transcript(home), 0)
    assert (fig["tool_calls"], fig["human_prompts"], fig["first_prompt"]) == (
        1,
        1,
        "run it",
    )
    assert fig["self_verdict"]["task_id"] == "capture"
    assert fig["first_ts"] and fig["last_ts"] and fig["denials"] is None


def test_agy_posts_the_agents_verdict_at_once(home, posted):
    configure("antigravity")
    write(str(home / "proj" / "GEMINI.md"), "rules")
    payload = agy_payload(home, agy_transcript(home))
    assert rr.handle(payload, "antigravity", "Stop") == ""
    (run,) = posted
    assert (run["client"], run["model"], run["verified"]) == (
        "antigravity",
        "gemini-x",
        False,
    )
    assert rr.normalize(payload, "antigravity", "PreToolUse") is None
    # A real file, outside Antigravity's brain directory.
    outside = jsonl(str(home / "t.jsonl"), agy_rows())
    assert (
        rr.normalize({**payload, "transcriptPath": outside}, "antigravity", "Stop")
        is None
    )


# ------------------------------------------------------------------------ Cline
def cline_doc():
    return {
        "version": 1,
        "origin": {"source": "cli", "version": "3.0.65"},
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": '<user_input mode="act">run it</user_input>',
                    }
                ],
                "ts": 1790704886921,
            },
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "name": "run_commands",
                        "input": {"commands": ["git push"]},
                    }
                ],
                "ts": 1790704901331,
                "modelInfo": {"id": "bonsai"},
                "metrics": {
                    "inputTokens": 100,
                    "outputTokens": 5,
                    "cacheReadTokens": 10,
                },
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "content": "ok"}],
                "ts": 1790704901840,
            },
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "done\nVerdict: pass task=capture"}
                ],
                "ts": 1790704902711,
                "modelInfo": {"id": "bonsai"},
                "metrics": {"inputTokens": 20, "outputTokens": 3},
            },
        ],
    }


def cline_payload(home, **extra):
    return {
        "clineVersion": "",
        "taskId": "conv_1",
        "hookName": "agent_end",
        "sessionContext": {"rootSessionId": "1790704886505_mmvbd"},
        "workspaceRoots": [str(home / "proj")],
        "agent_id": "agent_1",
        "parent_agent_id": None,
        "turn": {
            "outputText": "done\nVerdict: pass task=capture",
            "status": "completed",
        },
        **extra,
    }


def cline_messages(home):
    folder = home / ".cline" / "data" / "sessions" / "1790704886505_mmvbd"
    return write(str(folder / "1790704886505_mmvbd.messages.json"), cline_doc())


def test_cline_messages_figures_and_offset(home):
    path = cline_messages(home)
    fig, offset = rr.parse_cline(path, 0)
    assert (fig["tool_calls"], fig["pushes"], fig["human_prompts"]) == (1, 1, 1)
    assert (fig["input_tokens"], fig["output_tokens"], fig["model"]) == (
        130,
        8,
        "bonsai",
    )
    assert fig["client_version"] == "3.0.65" and offset == 4
    again, same = rr.parse_cline(path, offset)
    assert again["tool_calls"] == 0 and same == 4


def test_cline_task_complete_posts(home, posted):
    configure("cline")
    write(str(home / "proj" / "AGENTS.md"), "rules")
    cline_messages(home)
    assert rr.handle(cline_payload(home), "cline", "TaskComplete") == "{}"
    (run,) = posted
    assert run["client"] == "cline" and run["input_tokens"] == 130
    sub = rr.normalize(
        cline_payload(home, parent_agent_id="agent_0"), "cline", "TaskComplete"
    )
    assert sub["hook_event_name"] == "SubagentStop"
    missing = cline_payload(home, sessionContext={"rootSessionId": "nope_1"})
    assert rr.normalize(missing, "cline", "TaskComplete") is None
    assert rr.normalize(cline_payload(home), "cline", "TaskStart") is None


def test_the_0_3_webhook_is_claude_codes_alone(home, posted):
    """A 0.3 config's top-level webhook belongs to Claude Code. Cline strips the
    environment a test would redirect the config with, so its hook reads this one."""
    write(rr.CONFIG, {"webhook_url": URL, "agents": {"codex": {"webhook_url": URL}}})
    write(str(home / "proj" / "AGENTS.md"), "rules")
    cline_messages(home)
    assert rr.handle(cline_payload(home), "cline", "TaskComplete") == "{}"
    assert posted == []
    assert not os.path.exists(rr.STATE_DIR) or os.listdir(rr.STATE_DIR) == []
    assert rr.agent_config({"webhook_url": URL}, "claude_code") == {"webhook_url": URL}
    for program in rr.AUTOMATIC[1:]:
        assert rr.agent_config({"webhook_url": URL}, program) == {}


# ------------------------------------------------------------ OpenCode and Kilo
def opencode_db(path, sessions):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    # The columns the reporter reads; the real tables have more.
    con.execute("create table session (id, parent_id, version, time_created)")
    con.execute("create table message (id, session_id, time_created, data)")
    con.execute("create table part (id, message_id, session_id, time_created, data)")
    for sid, parent in sessions:
        con.execute("insert into session values (?, ?, '1.18.33', 1)", (sid, parent))
    rows = [
        ("m1", 1000, {"role": "user"}, [{"type": "text", "text": '"run it"'}]),
        (
            "m2",
            2000,
            {
                "role": "assistant",
                "modelID": "bonsai",
                "tokens": {
                    "input": 100,
                    "output": 5,
                    "reasoning": 1,
                    "cache": {"read": 10, "write": 0},
                },
            },
            [
                {
                    "type": "tool",
                    "tool": "bash",
                    "state": {"status": "completed", "input": {"command": "git push"}},
                },
                {
                    "type": "tool",
                    "tool": "edit",
                    "state": {"status": "error", "error": "DeniedError: rejected"},
                },
            ],
        ),
        (
            "m3",
            3000,
            {
                "role": "assistant",
                "modelID": "bonsai",
                "tokens": {"input": 20, "output": 3},
            },
            [{"type": "text", "text": "done\nVerdict: pass task=capture"}],
        ),
    ]
    sid = sessions[0][0]
    for mid, created, data, parts in rows:
        con.execute(
            "insert into message values (?, ?, ?, ?)",
            (mid, sid, created, json.dumps(data)),
        )
        for i, p in enumerate(parts):
            con.execute(
                "insert into part values (?, ?, ?, ?, ?)",
                (f"{mid}p{i}", mid, sid, created + i, json.dumps(p)),
            )
    con.commit()
    con.close()
    return path


def test_opencode_session_figures(tmp_path):
    db = opencode_db(str(tmp_path / "opencode.db"), [("ses_top0000001", None)])
    fig, offset = rr.parse_opencode(db, "ses_top0000001", 0)
    assert (fig["tool_calls"], fig["writes"], fig["pushes"]) == (2, 1, 1)
    assert fig["denials"] == 1 and fig["denial_classes"] == {"permission": 1}
    assert (fig["input_tokens"], fig["output_tokens"]) == (130, 9)
    assert (fig["model"], fig["client_version"], fig["first_prompt"]) == (
        "bonsai",
        "1.18.33",
        "run it",
    )
    assert offset == 3000
    again, same = rr.parse_opencode(db, "ses_top0000001", offset)
    assert again["tool_calls"] == 0 and same == 3000


@pytest.mark.parametrize("program", ["opencode", "kilo_code"])
def test_opencode_family_stop_posts_and_subagents_join_the_parent(
    home, posted, program, monkeypatch
):
    monkeypatch.setenv("XDG_DATA_HOME", str(home / "data"))
    configure(program)
    write(str(home / "proj" / "AGENTS.md"), "rules")
    db = opencode_db(
        rr.opencode_db(program),
        [("ses_top0000001", None), ("ses_sub0000001", "ses_top0000001")],
    )
    assert db.endswith(("opencode.db", "kilo.db"))
    stop = {
        "source": "ha-harness-tracker",
        "hook_event_name": "Stop",
        "session_id": "ses_top0000001",
        "cwd": str(home / "proj"),
    }
    assert rr.handle(stop, program) == ""
    (run,) = posted
    assert run["client"] == program and run["denials"] == 1
    sub = rr.normalize({**stop, "session_id": "ses_sub0000001"}, program)
    assert (sub["session_id"], sub["hook_event_name"], sub["transcript_session"]) == (
        "ses_top0000001",
        "SubagentStop",
        "ses_sub0000001",
    )
    assert (
        rr.normalize({k: v for k, v in stop.items() if k != "source"}, program) is None
    )


# ---------------------------------------------------------------------- profiles
def test_copilot_profile_selects_documented_files(home):
    proj = home / "proj"
    for rel in (
        "AGENTS.md",
        ".github/copilot-instructions.md",
        ".github/instructions/a.instructions.md",
        ".github/agents/t.agent.md",
        ".github/skills/s/SKILL.md",
    ):
        write(str(proj / rel), "x")
    write(str(home / ".copilot" / "copilot-instructions.md"), "u")
    write(
        str(home / ".copilot" / "hooks" / "h.json"),
        {
            "version": 1,
            "hooks": {"agentStop": [{"powershell": f"python {home / 'hook.py'}"}]},
        },
    )
    write(str(home / "hook.py"), "print()")
    write(str(home / ".copilot" / "permissions-config.json"), {"allow": ["shell(git)"]})
    write(str(home / ".copilot" / "config.json"), {"trustedFolders": ["x"]})
    sel = rr.select({"selection": "automatic"}, {}, str(proj), "copilot_cli")
    assert keys(sel) == sorted(
        [
            "<project>/AGENTS.md",
            "<project>/.github/copilot-instructions.md",
            "<project>/.github/instructions/a.instructions.md",
            "<project>/.github/agents/t.agent.md",
            "<project>/.github/skills/s/SKILL.md",
            "~/.copilot/copilot-instructions.md",
            "~/.copilot/hooks/h.json",
            "~/hook.py",
        ]
    )
    assert sel.approvals_digest() is not None  # saved permissions beside the version


def test_cursor_profile_reads_claude_hooks_only(home):
    proj = home / "proj"
    write(str(proj / ".cursor" / "rules" / "r.mdc"), "x")
    claude = home / ".claude" / "settings.json"
    write(str(claude), {"theme": "dark", "hooks": {"Stop": []}})
    write(
        str(home / ".cursor" / "cli-config.json"),
        {"permissions": {"allow": ["Shell(ls)"]}, "model": {"modelId": "a"}},
    )
    first = rr.select_profile("cursor", str(proj))
    write(str(claude), {"theme": "light", "hooks": {"Stop": []}})
    write(
        str(home / ".cursor" / "cli-config.json"),
        {
            "permissions": {"allow": ["Shell(ls)", "Shell(git)"]},
            "model": {"modelId": "b"},
        },
    )
    second = rr.select_profile("cursor", str(proj))
    assert second.version(None) == first.version(None)
    assert second.approvals_digest() != first.approvals_digest()
    write(str(claude), {"theme": "light", "hooks": {"Stop": [{"hooks": []}]}})
    assert rr.select_profile("cursor", str(proj)).version(None) != first.version(None)


def test_opencode_profile_reads_jsonc_and_instructions(home, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "cfg"))
    proj = home / "proj"
    write(str(proj / "docs" / "style.md"), "style")
    jsonc = (
        '{\n  // comment\n  "model": "a/b",\n  "instructions": ["docs/*.md"],\n'
        '  "permission": {"bash": "ask"},\n}\n'
    )
    write(str(proj / "opencode.jsonc"), jsonc)
    write(str(home / "cfg" / "opencode" / "AGENTS.md"), "user rules")
    sel = rr.select_profile("opencode", str(proj))
    assert "<project>/docs/style.md" in keys(sel)
    assert "~/cfg/opencode/AGENTS.md" in keys(sel)
    before = sel.version(None)
    plain = {
        "model": "c/d",
        "instructions": ["docs/*.md"],
        "permission": {"bash": "ask"},
    }
    write(str(proj / "opencode.jsonc"), json.dumps(plain))
    assert rr.select_profile("opencode", str(proj)).version(None) == before


def test_opencode_user_rules_fall_back_to_claude(home, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "cfg"))
    write(str(home / ".claude" / "CLAUDE.md"), "claude rules")
    assert "~/.claude/CLAUDE.md" in keys(
        rr.select_profile("opencode", str(home / "proj"))
    )
    write(str(home / "cfg" / "opencode" / "AGENTS.md"), "own rules")
    chosen = keys(rr.select_profile("opencode", str(home / "proj")))
    assert "~/cfg/opencode/AGENTS.md" in chosen and "~/.claude/CLAUDE.md" not in chosen


def test_every_profile_selects_without_error(home):
    for program in rr.PROFILES:
        rr.select_profile(program, str(home / "proj"))


# ------------------------------------------------------------------ registration
def test_copilot_registration_is_owned_and_idempotent(home):
    path, added, _ = rr.register_hook("copilot_cli", str(home / "rr.py"))
    assert added == 1 and path.endswith(
        os.path.join("hooks", "ha-harness-tracker.json")
    )
    doc = json.loads(read(path))
    stop = doc["hooks"]["agentStop"][0]
    assert stop["powershell"].startswith("& '") and stop["powershell"].endswith(
        "'agentStop'"
    )
    assert stop["timeoutSec"] == 30 and "userPromptSubmitted" in doc["hooks"]
    assert rr.register_hook("copilot_cli", str(home / "rr.py"))[1] == 0
    write(path, {"version": 1, "hooks": {}})  # an older copy of its own file
    assert rr.register_hook("copilot_cli", str(home / "rr.py"))[1] == 1


def test_cursor_registration_merges(home):
    other = {"command": "other"}
    write(
        str(home / ".cursor" / "hooks.json"), {"version": 1, "hooks": {"stop": [other]}}
    )
    path, added, backup = rr.register_hook("cursor", str(home / "rr.py"))
    assert added == len(rr.CURSOR_HOOK_EVENTS) and backup
    doc = json.loads(read(path))
    assert doc["hooks"]["stop"][0] == other and len(doc["hooks"]["stop"]) == 2
    assert "--hook" in doc["hooks"]["sessionEnd"][0]["command"]
    assert rr.register_hook("cursor", str(home / "rr.py"))[1] == 0


def test_agy_registration_keeps_other_groups(home):
    write(str(home / ".gemini" / "config" / "hooks.json"), {"mine": {"enabled": True}})
    path, added, _ = rr.register_hook("antigravity", str(home / "rr.py"))
    doc = json.loads(read(path))
    assert added == 2 and doc["mine"] == {"enabled": True}
    stop = doc["ha-harness-tracker"]["Stop"][0]["command"]
    assert '"' not in stop and stop.endswith("--hook antigravity Stop")
    assert rr.register_hook("antigravity", str(home / "rr.py"))[1] == 0


def test_cline_registration_writes_the_shim_or_refuses(home):
    path, added, _ = rr.register_hook("cline", str(home / "rr.py"))
    text = read(path)
    assert (
        added == 1
        and path.endswith("TaskComplete.js")
        and json.dumps(str(home / "rr.py")) in text
    )
    write(path, "// someone else's hook\n")
    with pytest.raises(SystemExit):
        rr.register_hook("cline", str(home / "rr.py"))
    os.remove(path)
    write(str(home / ".cline" / "hooks" / "TaskComplete.ps1"), "x")
    with pytest.raises(SystemExit):
        rr.register_hook("cline", str(home / "rr.py"))


@pytest.mark.parametrize("program", ["opencode", "kilo_code"])
def test_opencode_family_registration_writes_the_plugin(home, program, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "cfg"))
    path, added, _ = rr.register_hook(program, str(home / "rr.py"))
    text = read(path)
    assert added == 1 and os.path.dirname(path) == os.path.join(
        rr.opencode_config_dir(program), "plugins"
    )
    assert (
        json.dumps(program) in text
        and "session.idle" in text
        and "chat.message" in text
    )


# ------------------------------------------------------------------ entry point
@pytest.mark.parametrize(
    ("argv", "want"),
    [
        ([], (None, None)),
        (["--codex"], (None, None)),
        (["--hook"], (None, None)),
        (["--hook", "cursor"], ("cursor", None)),
        (["--hook", "copilot_cli", "agentStop"], ("copilot_cli", "agentStop")),
        (["--hook", "bogus"], None),
        (["--outcome", "pass"], None),
    ],
)
def test_hook_args(argv, want):
    assert rr.hook_args(argv) == want


def test_cursor_hook_answers_json_even_on_bad_input(tmp_path):
    env = {
        **os.environ,
        "HARNESS_LEDGER_CONFIG": str(tmp_path / "c.json"),
        "HARNESS_LEDGER_STATE": str(tmp_path / "s"),
    }
    res = subprocess.run(
        [
            sys.executable,
            os.path.join(ROOT, "tools", "report_run.py"),
            "--hook",
            "cursor",
        ],
        input=b"\xef\xbb\xbfnot json",
        capture_output=True,
        env=env,
        timeout=60,
    )
    assert res.returncode == 0 and res.stdout.strip() == b'{"continue": true}'
    logged = read(str(tmp_path / "s" / rr.HOOK_ERRORS))
    assert " cursor - harness-ledger: unreadable hook input" in logged


def test_roll_up_leaves_out_what_the_agent_does_not_record():
    lines = [
        {
            "session_id": "s",
            "seq": 1,
            "at": "t",
            "harness_version": "v",
            "input_tokens": None,
            "denials": None,
            "tool_calls": 2,
        }
    ]
    run = rr.roll_up(lines, {"outcome": "pass"}, by_person=False)
    assert "input_tokens" not in run and "denials" not in run and run["tool_calls"] == 2


# ---------------------------------------------------------------- the command
def test_the_command_reports_for_the_one_agent_configured_or_asks():
    legacy_and_codex = {"webhook_url": URL, "agents": {"codex": {"webhook_url": URL}}}
    with pytest.raises(SystemExit) as err:
        rr._program_for(legacy_and_codex, None)
    assert "claude_code, codex" in str(err.value)
    assert rr._program_for(legacy_and_codex, "claude_code") == "claude_code"
    assert rr._program_for({"webhook_url": URL}, None) == "claude_code"
    assert rr._program_for({"agents": {"cursor": {"webhook_url": URL}}}, None) == (
        "cursor"
    )
    assert rr._program_for({"agents": {"cursor": {}}}, None) == "other"
    assert rr._program_for({}, None) == "other"


def settings_answer(program):
    body = {"agent": "a", "agent_program": program, "selection": "automatic"}
    return lambda method, url, body_, insecure, timeout=30: (200, json.dumps(body))


def test_setup_registers_the_hook_for_an_automatic_program(home, monkeypatch, capsys):
    (home / ".gemini").mkdir()
    write(str(home / "proj" / "GEMINI.md"), "rules")
    monkeypatch.setattr(rr, "http", settings_answer("antigravity"))
    monkeypatch.setattr(rr, "_ask", lambda question, default: True)
    monkeypatch.setattr(rr, "INSTALL_DIR", str(home / "install"))
    assert rr.setup(URL, True, str(home / "proj")) == 0
    out = capsys.readouterr().out
    assert "Harness files for Antigravity, selected automatically" in out
    assert "<project>" not in out and "GEMINI.md" in out
    cfg = json.loads(read(rr.CONFIG))
    assert cfg["agents"]["antigravity"]["webhook_url"] == URL
    hooks = json.loads(read(str(home / ".gemini" / "config" / "hooks.json")))
    stop = hooks["ha-harness-tracker"]["Stop"][0]["command"]
    assert os.path.join("install", "report_run.py") in stop


def test_setup_for_a_manual_program_names_the_agent_in_the_command(
    home, monkeypatch, capsys
):
    monkeypatch.setattr(rr, "http", settings_answer("junie"))
    assert rr.setup(URL, True, str(home / "proj")) == 0
    assert "report_run.py --agent junie --outcome" in capsys.readouterr().out
