"""The reporter, without Home Assistant: file selection, projections, identity, Codex.

Loads tools/report_run.py by path, points its home, config and state at a
temporary tree, and builds each agent's files there.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys

import pytest

from tests.winposix import install_socketpair_escape

# The Home Assistant test plugin's per-test event loop needs socketpair on Windows.
install_socketpair_escape()

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
spec = importlib.util.spec_from_file_location(
    "report_run", os.path.join(ROOT, "tools", "report_run.py")
)
rr = importlib.util.module_from_spec(spec)
sys.modules["report_run"] = rr
spec.loader.exec_module(rr)


def write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mode = "wb" if isinstance(content, bytes) else "w"
    kwargs = {} if isinstance(content, bytes) else {"encoding": "utf-8"}
    if isinstance(content, (dict, list)):
        content = json.dumps(content)
    with open(path, mode, **kwargs) as fh:
        fh.write(content)
    return path


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A home directory with Claude and Codex config dirs and a git project."""
    h = tmp_path / "home"
    (h / "proj" / ".git").mkdir(parents=True)
    monkeypatch.setattr(rr, "HOME", str(h))
    # expanduser reads USERPROFILE on Windows and HOME elsewhere.
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    monkeypatch.setattr(rr, "STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(rr, "CONFIG", str(tmp_path / "config.json"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    return h


def keys(sel):
    return sorted(k for k, _, _ in sel.files)


# ------------------------------------------------------------------ fingerprint
def test_same_named_files_under_two_roots_are_both_kept(home):
    a = write(str(home / "a" / "rules" / "x.md"), "one")
    b = write(str(home / "b" / "rules" / "x.md"), "two")
    sel = rr.select_manual([os.path.dirname(a), os.path.dirname(b)], None, "other")
    assert keys(sel) == ["~/a/rules/x.md", "~/b/rules/x.md"]
    before = sel.version(None)
    write(b, "three")
    after = rr.select_manual([os.path.dirname(a), os.path.dirname(b)], None, "other")
    assert after.version(None) != before


def test_project_files_hash_the_same_in_another_clone(home, tmp_path):
    for clone in ("proj", "proj2"):
        (home / clone / ".git").mkdir(parents=True, exist_ok=True)
        write(str(home / clone / "AGENTS.md"), "rules")
    one = rr.select_manual(["AGENTS.md"], str(home / "proj"), "other")
    two = rr.select_manual(["AGENTS.md"], str(home / "proj2"), "other")
    assert keys(one) == ["<project>/AGENTS.md"]
    assert one.version(None) == two.version(None)


def test_manual_list_reports_missing_paths(home):
    sel = rr.select_manual(["nope.md", "~/also-nope"], str(home / "proj"), "other")
    assert sel.files == []
    assert sorted(sel.display(p) for p in sel.missing) == [
        "~/also-nope",
        "~/proj/nope.md",
    ]


def test_label_prefixes_the_version(home):
    write(str(home / "proj" / "AGENTS.md"), "x")
    sel = rr.select_manual(["AGENTS.md"], str(home / "proj"), "other")
    assert sel.version("main").startswith("main sha256:")


# ------------------------------------------------------------------ projections
def claude_settings_version(home, doc, name="settings.json"):
    path = write(str(home / ".claude" / name), doc)
    return rr.select_manual([path], None, "claude_code")


def test_claude_settings_count_by_harness_keys(home):
    base = {"permissions": {"deny": ["Bash(rm:*)"]}, "theme": "dark", "model": "a"}
    v1 = claude_settings_version(home, base).version(None)
    v2 = claude_settings_version(home, {**base, "theme": "light", "model": "b"})
    assert v2.version(None) == v1
    v3 = claude_settings_version(home, {**base, "autoMode": {"allow": ["x"]}})
    assert v3.version(None) != v1


def test_unknown_settings_keys_are_shown_not_hashed(home):
    base = {"permissions": {}}
    v1 = claude_settings_version(home, base).version(None)
    sel = claude_settings_version(home, {**base, "brandNewKey": 1})
    assert sel.version(None) == v1
    assert sel.groups[0]["unclassified"] == ["brandNewKey"]


def test_claude_env_keeps_behaviour_flags_and_drops_secrets(home):
    base = {"env": {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "1"}}
    v1 = claude_settings_version(home, base).version(None)
    secret = {"env": {**base["env"], "ANTHROPIC_API_KEY": "a"}}
    assert claude_settings_version(home, secret).version(None) == v1
    changed = {"env": {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "2"}}
    assert claude_settings_version(home, changed).version(None) != v1


def test_local_permissions_are_approvals_not_harness(home):
    one = claude_settings_version(
        home, {"permissions": {"allow": ["Bash(ls)"]}}, "settings.local.json"
    )
    two = claude_settings_version(
        home,
        {"permissions": {"allow": ["Bash(ls)", "Bash(git status)"]}},
        "settings.local.json",
    )
    assert one.version(None) == two.version(None)
    assert one.approvals_digest() != two.approvals_digest()
    assert two.manifest()["approvals"]["rules"] == 2


def test_agent_front_matter_model_lines_are_ignored(home):
    path = str(home / ".claude" / "agents" / "reviewer.md")
    body = "---\nname: reviewer\nmodel: {}\neffort: high\ntools: [Read]\n---\nReview.\n"
    write(path, body.format("opus"))
    v1 = rr.select_manual([path], None, "claude_code").version(None)
    write(path, body.format("sonnet").replace("effort: high", "effort: low"))
    assert rr.select_manual([path], None, "claude_code").version(None) == v1
    write(path, body.format("opus").replace("Review.", "Review twice."))
    assert rr.select_manual([path], None, "claude_code").version(None) != v1
    write(path, body.format("opus").replace("[Read]", "[Read, Edit]"))
    assert rr.select_manual([path], None, "claude_code").version(None) != v1


def test_mcp_secrets_do_not_move_the_version(home):
    path = str(home / "proj" / ".mcp.json")
    doc = {"mcpServers": {"x": {"command": "run", "env": {"TOKEN": "a"}}}}
    write(path, doc)
    v1 = rr.select_manual([path], None, "claude_code").version(None)
    doc["mcpServers"]["x"]["env"]["TOKEN"] = "b"
    write(path, doc)
    assert rr.select_manual([path], None, "claude_code").version(None) == v1
    doc["mcpServers"]["x"]["command"] = "run2"
    write(path, doc)
    assert rr.select_manual([path], None, "claude_code").version(None) != v1


@pytest.mark.skipif(rr.tomllib is None, reason="needs tomllib")
def test_codex_config_counts_by_harness_keys(home):
    path = str(home / ".codex" / "config.toml")
    base = (
        'model = "{m}"\napproval_policy = "{a}"\n'
        '[mcp_servers.x]\ncommand = "run"\nenv = {{ TOKEN = "{t}" }}\n'
        '[hooks.state."k"]\ntrusted_hash = "{h}"\n'
    )
    write(path, base.format(m="a", a="on-request", t="1", h="1"))
    v1 = rr.select_manual([path], None, "codex").version(None)
    write(path, base.format(m="b", a="on-request", t="2", h="2"))
    assert rr.select_manual([path], None, "codex").version(None) == v1
    write(path, base.format(m="a", a="never", t="1", h="1"))
    assert rr.select_manual([path], None, "codex").version(None) != v1


# ---------------------------------------------------------------- claude profile
def test_claude_profile(home):
    c = home / ".claude"
    proj = home / "proj"
    write(str(c / "CLAUDE.md"), "user rules")
    write(str(c / "settings.json"), {"permissions": {"deny": ["x"]}})
    write(str(c / "skills" / "s" / "SKILL.md"), "---\nname: s\n---\nskill")
    write(str(c / "agents" / "a.md"), "---\nname: a\nmodel: opus\n---\nagent")
    write(str(c / ".credentials.json"), "secret")
    write(str(c / "projects" / "p" / "t.jsonl"), "{}")
    write(str(c / "history.jsonl"), "{}")
    write(str(proj / "CLAUDE.md"), "see @docs/extra.md")
    write(str(proj / "docs" / "extra.md"), "imported")
    write(str(proj / "AGENTS.md"), "not read while CLAUDE.md is on the path")
    write(
        str(proj / ".claude" / "settings.local.json"), {"permissions": {"allow": ["y"]}}
    )
    write(str(proj / ".mcp.json"), {"mcpServers": {}})
    write(str(home / ".claude" / ".claude.json"), {"machineID": "x"})
    memory = c / "projects" / rr.claude_project_dir(str(proj)) / "memory" / "MEMORY.md"
    write(str(memory), "- a memory")
    sel = rr.select_claude(str(proj))
    got = keys(sel)
    assert got == [
        "<project>/.claude/settings.local.json",
        "<project>/.mcp.json",
        "<project>/CLAUDE.md",
        "<project>/docs/extra.md",
        "~/.claude/CLAUDE.md",
        "~/.claude/agents/a.md",
        "~/.claude/settings.json",
        "~/.claude/skills/s/SKILL.md",
    ]
    assert sel.approvals and sel.memory
    assert sel.manifest()["memory"] == [sel.display(str(memory))]


def test_claude_reads_agents_md_only_without_claude_md(home):
    proj = home / "proj"
    write(str(proj / "AGENTS.md"), "rules")
    assert "<project>/AGENTS.md" in keys(rr.select_claude(str(proj)))


def test_hook_scripts_are_hashed_and_interpreters_are_not(home, tmp_path):
    script = write(str(home / ".claude" / "hooks" / "guard.py"), "print(1)")
    interpreter = write(str(tmp_path / "bin" / "python.exe"), b"MZ")
    write(
        str(home / ".claude" / "settings.json"),
        {
            "hooks": {
                "Stop": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": f'"{interpreter}" "{script}"',
                            }
                        ]
                    }
                ]
            }
        },
    )
    got = keys(rr.select_claude(str(home / "proj")))
    assert "~/.claude/hooks/guard.py" in got
    assert not any(k.endswith("python.exe") for k in got)


# ----------------------------------------------------------------- codex profile
def test_codex_profile(home):
    x = home / ".codex"
    proj = home / "proj"
    sub = proj / "pkg"
    write(str(x / "AGENTS.md"), "global")
    write(str(x / "AGENTS.override.md"), "override wins")
    write(str(x / "rules" / "default.rules"), "# saved\nprefix_rule(x)\n")
    write(str(x / "rules" / "team.rules"), "prefix_rule(y)\n")
    write(str(x / "agents" / "r.toml"), 'model = "x"\ninstructions = "review"\n')
    write(str(x / "auth.json"), "secret")
    write(str(x / "sessions" / "2026" / "rollout-1.jsonl"), "{}")
    write(str(proj / "AGENTS.md"), "root")
    write(str(sub / "AGENTS.override.md"), "sub override")
    write(str(sub / "AGENTS.md"), "shadowed by the override")
    write(str(home / ".agents" / "skills" / "k" / "SKILL.md"), "skill")
    sel = rr.select_codex(str(sub))
    assert keys(sel) == [
        "<project>/AGENTS.md",
        "<project>/pkg/AGENTS.override.md",
        "~/.agents/skills/k/SKILL.md",
        "~/.codex/AGENTS.override.md",
        "~/.codex/agents/r.toml",
        "~/.codex/rules/team.rules",
    ]
    assert sel.approvals and not sel.memory


# ---------------------------------------------------------------- precedence
def test_home_assistant_choice_and_the_local_fallback(home):
    proj = str(home / "proj")
    write(str(home / "proj" / "AGENTS.md"), "x")
    write(str(home / "listed.md"), "y")
    local = {"harness": ["~/listed.md"]}
    manual = rr.select(
        {"selection": "manual", "harness_files": ["AGENTS.md"]},
        local,
        proj,
        "claude_code",
    )
    assert keys(manual) == ["<project>/AGENTS.md"]
    empty_manual = rr.select(
        {"selection": "manual", "harness_files": []}, local, proj, "claude_code"
    )
    assert keys(empty_manual) == ["~/listed.md"]
    no_settings = rr.select({}, local, proj, "claude_code")
    assert keys(no_settings) == ["~/listed.md"]
    automatic = rr.select({"selection": "automatic"}, local, proj, "claude_code")
    assert automatic.mode == "automatic"


# ---------------------------------------------------------------- identity
def test_identify_positive_and_foreign(home):
    claude_t = str(home / ".claude" / "projects" / "p" / "s.jsonl")
    codex_t = str(home / ".codex" / "sessions" / "2026" / "rollout.jsonl")
    base = {"hook_event_name": "Stop", "session_id": "s"}
    assert rr.identify({**base, "transcript_path": claude_t}) == "claude_code"
    assert rr.identify({**base, "transcript_path": codex_t}) == "codex"
    # Cursor, Copilot CLI and Continue running Claude's hooks, and junk.
    assert (
        rr.identify({**base, "transcript_path": claude_t, "cursor_version": "3"})
        is None
    )
    assert (
        rr.identify(
            {
                "hook_event_name": "agentStop",
                "sessionId": "s",
                "transcriptPath": claude_t,
            }
        )
        is None
    )
    continue_t = str(home / ".continue" / "sessions" / "s.json")
    assert rr.identify({**base, "transcript_path": continue_t}) is None
    assert rr.identify({**base}) is None
    assert rr.identify(["not", "a", "payload"]) is None
    assert (
        rr.identify(
            {
                **base,
                "hook_event_name": "UserPromptExpansion",
                "transcript_path": codex_t,
            }
        )
        is None
    )


def test_foreign_payload_writes_and_posts_nothing(home, monkeypatch):
    posted = []
    monkeypatch.setattr(rr, "post", lambda cfg, run: posted.append(run) or (200, "{}"))
    write(rr.CONFIG, {"webhook_url": "https://homeassistant.local:8123/api/webhook/x"})
    claude_t = write(str(home / ".claude" / "projects" / "p" / "s.jsonl"), "")
    out = rr.handle(
        {
            "hook_event_name": "Stop",
            "session_id": "s",
            "transcript_path": claude_t,
            "cursor_version": "3",
        }
    )
    assert out is None and posted == []
    assert not os.path.exists(rr.STATE_DIR) or os.listdir(rr.STATE_DIR) == []


def test_no_webhook_for_this_agent_means_no_ledger(home):
    write(
        rr.CONFIG,
        {
            "agents": {
                "codex": {
                    "webhook_url": "https://homeassistant.local:8123/api/webhook/x"
                }
            }
        },
    )
    claude_t = write(str(home / ".claude" / "projects" / "p" / "s.jsonl"), "")
    assert (
        rr.handle(
            {"hook_event_name": "Stop", "session_id": "s", "transcript_path": claude_t}
        )
        is None
    )
    assert not os.path.exists(rr.STATE_DIR) or os.listdir(rr.STATE_DIR) == []


# ---------------------------------------------------------------- codex runs
def rollout_lines():
    return [
        {
            "timestamp": "2026-09-29T10:00:00Z",
            "type": "session_meta",
            "payload": {"cli_version": "0.155.1", "cwd": "x"},
        },
        {
            "timestamp": "2026-09-29T10:00:01Z",
            "type": "turn_context",
            "payload": {"model": "gpt-x", "effort": "high"},
        },
        {
            "timestamp": "2026-09-29T10:00:02Z",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "apply_patch",
                "input": "*** Begin Patch",
            },
        },
        {
            "timestamp": "2026-09-29T10:00:03Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "shell_command",
                "arguments": '{"command": "git push origin main"}',
            },
        },
        {
            "timestamp": "2026-09-29T10:00:04Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "output": "This action was rejected due to unacceptable risk.\n"
                "Reason: x",
            },
        },
        {
            "timestamp": "2026-09-29T10:00:04Z",
            "type": "response_item",
            "payload": {"type": "function_call_output", "output": "fine, not rejected"},
        },
        {
            "timestamp": "2026-09-29T10:00:05Z",
            "type": "token_usage_record",
            "payload": {
                "response_id": "r1",
                "usage": {"input_tokens": 100, "output_tokens": 10},
            },
        },
        {
            "timestamp": "2026-09-29T10:00:05Z",
            "type": "token_usage_record",
            "payload": {
                "response_id": "r1",
                "usage": {"input_tokens": 100, "output_tokens": 10},
            },
        },
        {
            "timestamp": "2026-09-29T10:00:06Z",
            "type": "token_usage_record",
            "payload": {
                "response_id": "r2",
                "usage": {"input_tokens": 50, "output_tokens": 5},
            },
        },
        {
            "timestamp": "2026-09-29T10:00:30Z",
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "last_agent_message": "Done.\nVerdict: pass verified task=ship",
            },
        },
    ]


def test_codex_rollout_figures(tmp_path):
    path = tmp_path / "rollout.jsonl"
    path.write_text(
        "\n".join(json.dumps(x) for x in rollout_lines()) + "\n", encoding="utf-8"
    )
    fig, offset = rr.parse_codex_slice(str(path), 0)
    assert fig["tool_calls"] == 2
    assert (fig["writes"], fig["pushes"]) == (1, 1)
    assert (fig["input_tokens"], fig["output_tokens"], fig["api_calls"]) == (150, 15, 2)
    assert fig["denials"] == 1 and fig["denial_classes"] == {"reviewer": 1}
    assert (fig["model"], fig["effort"], fig["client_version"]) == (
        "gpt-x",
        "high",
        "0.155.1",
    )
    assert fig["self_verdict"]["outcome"] == "pass"
    assert fig["self_verdict"]["task_id"] == "ship"
    assert offset == path.stat().st_size


def test_codex_hook_flow(home, monkeypatch):
    posted = []

    def fake_post(cfg, run):
        posted.append((cfg.get("webhook_url"), run))
        return 200, json.dumps({"run_count": len(posted), "pass_rate": 100.0})

    monkeypatch.setattr(rr, "post", fake_post)
    write(
        rr.CONFIG,
        {
            "agents": {
                "codex": {
                    "webhook_url": "https://homeassistant.local:8123/api/webhook/codex"
                }
            }
        },
    )
    os.makedirs(rr.STATE_DIR, exist_ok=True)
    write(
        rr.settings_path("codex"), {"selection": "automatic", "agent_program": "codex"}
    )
    write(str(home / ".codex" / "AGENTS.md"), "rules")
    rollout = home / ".codex" / "sessions" / "2026" / "rollout-s.jsonl"
    write(str(rollout), "\n".join(json.dumps(x) for x in rollout_lines()) + "\n")
    base = {
        "session_id": "s",
        "transcript_path": str(rollout),
        "cwd": str(home / "proj"),
    }
    rr.handle({**base, "hook_event_name": "UserPromptSubmit", "prompt": "ship it"})
    rr.handle({**base, "hook_event_name": "Stop"})
    assert posted == []  # held until the person's next prompt
    out = rr.handle({**base, "hook_event_name": "UserPromptSubmit", "prompt": "pass"})
    assert out and "recorded confirmed run ship as pass" in out
    url, run = posted[0]
    assert url == "https://homeassistant.local:8123/api/webhook/codex"
    assert (
        run["client"] == "codex" and run["model"] == "gpt-x" and run["effort"] == "high"
    )
    assert (
        run["fingerprint_schema"] == 2 and run["harness_manifest"]["program"] == "codex"
    )
    assert "prompt: ship it" in run["notes"]


def test_run_keys_follow_the_span(home):
    lines = [{"session_id": "s", "seq": 1, "at": "t", "harness_version": "v"}]
    a = rr.roll_up(lines, {"outcome": "pass"}, by_person=False)
    b = rr.roll_up(lines, {"outcome": "fail"}, by_person=True)
    c = rr.roll_up([{**lines[0], "seq": 2}], {"outcome": "pass"}, by_person=False)
    assert a["run_key"] == b["run_key"] != c["run_key"]


# ---------------------------------------------------------------- setup
def test_register_hook_merges_backs_up_and_is_idempotent(home):
    settings = home / ".claude" / "settings.json"
    other = {"type": "command", "command": "other-hook"}
    write(str(settings), {"theme": "dark", "hooks": {"Stop": [{"hooks": [other]}]}})
    _, added, backup = rr.register_hook("claude_code", str(home / "rr.py"))
    assert added == len(rr.CLAUDE_HOOK_EVENTS) and backup and os.path.isfile(backup)
    doc = json.loads(settings.read_text(encoding="utf-8"))
    assert doc["theme"] == "dark"
    assert doc["hooks"]["Stop"][0]["hooks"][0] == other
    assert len(doc["hooks"]["Stop"]) == 2
    expansion = doc["hooks"]["UserPromptExpansion"][0]
    assert expansion["matcher"] == "verdict|pass|fail|partial"
    _, again, _ = rr.register_hook("claude_code", str(home / "rr.py"))
    assert again == 0


def test_register_hook_for_codex_writes_hooks_json(home):
    (home / ".codex").mkdir()
    path, added, backup = rr.register_hook("codex", str(home / "rr.py"))
    assert path.endswith("hooks.json") and added == len(rr.CODEX_HOOK_EVENTS)
    assert backup is None
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    assert doc["hooks"]["SessionEnd"][0]["hooks"][0]["timeout"] == 3


def test_register_hook_refuses_a_broken_settings_file(home):
    write(str(home / ".claude" / "settings.json"), "{not json")
    with pytest.raises(SystemExit):
        rr.register_hook("claude_code", str(home / "rr.py"))


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX permissions")
def test_config_is_written_owner_only(home):
    rr.write_config({"agents": {}})
    assert os.stat(rr.CONFIG).st_mode & 0o777 == 0o600


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows ACLs")
def test_config_acl_names_only_the_owner_on_windows(home):
    import subprocess

    rr.write_config({"agents": {}})
    listing = subprocess.run(
        ["icacls", rr.CONFIG], capture_output=True, text=True, check=True
    ).stdout
    entries = [line.strip() for line in listing.splitlines()[:-2] if line.strip()]
    grants = [e.split(" ", 1)[-1] if e.startswith(rr.CONFIG) else e for e in entries]
    assert len(grants) == 1, grants
    assert os.environ["USERNAME"].lower() in grants[0].lower()
    assert "(I)" not in listing  # nothing inherited


@pytest.mark.parametrize("flags", [[], ["--codex"], ["--hook"]])
def test_a_hook_run_reads_the_payload_with_or_without_a_client_flag(tmp_path, flags):
    """Run as an installer registers it: stdin is a pipe, and some add --codex."""
    import subprocess

    env = {
        **os.environ,
        "HARNESS_LEDGER_CONFIG": str(tmp_path / "config.json"),
        "HARNESS_LEDGER_STATE": str(tmp_path / "state"),
    }
    res = subprocess.run(
        [sys.executable, os.path.join(ROOT, "tools", "report_run.py"), *flags],
        input="not json",
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    # Hook mode reports unreadable input on stderr and exits 0; command mode
    # would have refused the arguments or asked for --outcome.
    assert res.returncode == 0
    assert "unreadable hook input" in res.stderr
    assert "--outcome" not in res.stderr
