# Agent Harness Performance Tracker

A Home Assistant integration that records each run an AI coding agent reports, groups the runs by the harness version they ran under, and tells you whether a harness change made the agent better or worse.

A harness is everything around a frozen model: the rules files, the hooks, the skills, the settings, the prompts. The integration is a preserve-and-extend gate for it: a harness change has to improve something without breaking what already worked. The idea comes from Salesforce's DarwinX work, where harness changes alone took an agent from 43.5% to 93% task completion.

## What it measures

Each run is one unit of work the agent finished. The reporter says which harness version it ran under and how it went; the integration does the rest.

| Figure | Meaning |
|---|---|
| Runs | Runs stored, every version. |
| Harness version | The version of the latest run; attributes `runs_on_version`, `first_seen`, `confirmed`. |
| Success rate | Runs that passed on the current harness version, as a percentage. Partial counts as not passed. |
| Verified rate | Runs whose result was confirmed by something other than the agent: a person, a test oracle, an effect checked by the reporter. |
| Median turns, median duration | Effort per run on the current version. |
| Interventions per run | Times a human had to correct or redirect the agent. |
| Denials per run | Permission or guard denials the agent hit. |
| Cost | `cost_usd` summed over the stored runs. |
| Baseline harness version | The best confirmed version, or the one you pinned. |
| Improvement over baseline | Current success rate minus the baseline's, in points, over only the task ids both versions ran. Unknown when they share none. |
| Regressed tasks | Count of task ids the baseline solved whose latest run on the current version failed; the ids are in the `tasks` attribute. |
| Window success rate, window denials per 100 tool calls | The last `window` runs, whatever harness versions they used. The success rate's attributes carry the prior window and the versions inside. |
| Recurring denial classes | Count of denial classes seen at least twice in the last `window` runs; `classes` maps each to its count. |
| Last run | Outcome of the latest run; attributes carry its task, version, turns and duration. |
| Harness regressed | On while either gate is failing. Each gate raises its own repair issue and fires an event. |

## The gate

A version is confirmed once it has `min_runs` runs on it (default 10). The baseline is the confirmed version with the highest success rate, most recent on a tie, unless you pin one. The gate reports a regression when either half fires:

| Half | Fires when |
|---|---|
| Aggregate | The current version is confirmed and, over the task ids both versions ran, its success rate is more than `tolerance` points (default 5) below the baseline's. Off when they share no task, and off when the model most runs reported differs between the two versions. |
| Per task | A task id that passed on the baseline has its latest run on the current version fail. Fires before the current version is confirmed: one broken task is evidence on its own. |

Both halves need a baseline. Until some version has `min_runs` runs, nothing is confirmed and the gate stays off; pin a version with `set_baseline` to start measuring sooner. One bad run on a fresh version never trips the aggregate half: a lucky or unlucky rollout is exploration, and a version earns a verdict only after confirmation.

### The window gate

A harness edited several times a day never gives one version `min_runs` runs, so the version gate rarely compares anything. The window gate judges the last `window` runs (default 10) against the `window` before them, whatever versions they span, and names the versions inside.

| Fires when | Held when |
|---|---|
| Over the task ids both windows ran, the success rate drops by more than max(`tolerance`, 150 / `window`) points: with ten runs, two bad runs, not one | The model most runs reported differs between the windows |
| Denials per 100 tool calls rise by 1.0 or more, with at least 100 calls on each side. Per run, one long span reads as a harness getting worse | |

Nothing is compared until there are twice `window` runs. The repair issue lists the versions in the window and the recurring denial classes; the change that caused it is among those versions.

## Installation

HACS: add this repository as a custom repository (category: Integration), download it, restart Home Assistant.

Manual: copy `custom_components/agent_harness_performance_tracker` into `config/custom_components/` and restart Home Assistant.

Minimum Home Assistant version: 2026.3.0.

Then Settings, Devices & services, Add integration, Agent Harness Performance Tracker. One entry per agent.

## Setting up an agent

1. Add the agent in Home Assistant: a name and the agent program.
2. Open the agent's Configure screen and copy the webhook address at the top. The confirmation screen shows it too, when Home Assistant displays one.
3. On the machine the agent runs on, from the project directory:

```bash
python tools/report_run.py --setup
```

Paste the address when asked. Setup checks it against Home Assistant, offers to trust a self-signed certificate, writes the local config, prints the harness files it selected for the current directory, and for a program with a hook offers to register it. It copies itself to `~/.config/ha-harness-tracker/report_run.py`, so the hook does not depend on the clone.

| Agent program | Harness files | Runs reported by |
|---|---|---|
| Claude Code | automatic | hook |
| GitHub Copilot (VS Code) | suggested list | command |
| Codex | automatic | hook |
| Cursor | automatic | hook |
| OpenCode | automatic | plugin |
| Google Antigravity | automatic | hook |
| JetBrains Junie | suggested list | command |
| Cline | automatic | hook |
| GitHub Copilot CLI | automatic | hook |
| Kilo Code | automatic | plugin |
| Other | manual | command |

A suggested list is taken from the agent's documentation, checked on a real install, and starts the options' manual list; check each path. The command is `report_run.py --agent <program> --outcome pass|fail|partial`, run by the agent as its last step, by its own end-of-task hook, or by a script.

## Configuration

| Field | Where | Default | Effect |
|---|---|---|---|
| Agent name | setup, reconfigure | | Device name and entity prefix. Must be unique. |
| Agent program | setup, reconfigure | Claude Code | Which file profile and suggestions apply. |
| Runs to confirm a harness version | options | 10 | Runs before a version's success rate counts as a baseline. |
| Regression tolerance (points) | options | 5 | Allowed drop below the baseline before the aggregate half fires. |
| Runs to keep | options | 2000 | Older runs are dropped once this many are stored. |
| Runs per window | options | 10 | Size of each half of the window gate, 3 to 100. |
| Selection | options, Harness files | Automatic for a program with a file profile, else Manual | Automatic uses the program's file profile; Manual uses the list below. |
| Harness files | options, Harness files | the program's suggested list | Files and folders on the agent's machine. Relative paths resolve against the project root (the git root of the working directory). |
| Version label | options, Harness files | | Optional prefix for the computed version, such as a git branch. |

The options screen lists the files the reporter selected for the last run, with the settings keys it hashed, the keys it did not recognise, and what it tracked beside the version. The reporter reads the settings at each session start; a change applies from the next session.

Saving options reloads the entry and re-runs the gate against the stored runs. Reconfigure renames the agent or changes its program; the webhook and the runs stay.

The webhook address is the reporter's only credential. It is on the confirmation screen and the agent's Configure screen, which only administrators can open; diagnostics redact it. Remove and re-add the agent to get a new one.

## The harness version

The reporter hashes the selected files into a short SHA-256, `sha256:<12 hex>`, fingerprint schema 2. A change to any selected file starts a new version. Project files are hashed by their path inside the project, so two clones of a project hash the same.

What Automatic selects:

| | Claude Code | Codex |
|---|---|---|
| Rules | `CLAUDE.md`, `.claude/CLAUDE.md` and `CLAUDE.local.md` in the working directory and every parent, with their `@` imports; `AGENTS.md` when no `CLAUDE.md` is on the path; `~/.claude/CLAUDE.md`; `rules/` at user and project level; the managed `CLAUDE.md` | `~/.codex/AGENTS.override.md` or `AGENTS.md`; from the git root down to the working directory, each directory's `AGENTS.override.md`, `AGENTS.md` or fallback file |
| Settings | user, project, local and managed `settings.json` | `~/.codex/config.toml`, and `.codex/config.toml` in trusted projects |
| Hooks | the `hooks` key, and the script files its commands run | `hooks.json`, the `[hooks]` table, and the script files they run |
| Skills, commands, agents | `skills/`, `commands/`, `agents/`, `output-styles/` at user and project level; installed plugin versions | `.agents/skills` from the working directory up, `~/.agents/skills`, `~/.codex/prompts`, `~/.codex/agents` |
| MCP servers | `~/.claude.json` (user and this project), `.mcp.json`, `managed-mcp.json` | the `[mcp_servers]` table |

| Program | Project: the working directory up to the git root | User |
|---|---|---|
| GitHub Copilot CLI | `AGENTS.md`, `CLAUDE.md`, `GEMINI.md`, `.github/copilot-instructions.md`, `.github/instructions`, `.github/agents`, `.github/skills`, `.github/hooks`, `.github/copilot/settings*.json`, `.github/mcp.json`, `.mcp.json`, `.claude/rules`, `.claude/skills`, `.agents/skills`, the hooks in `.claude/settings*.json` | `~/.copilot`: `copilot-instructions.md`, `instructions`, `agents`, `skills`, `hooks`, `settings.json`, `mcp-config.json`; `~/.agents/skills` |
| Cursor | `AGENTS.md`, `CLAUDE.md`, `.cursorrules`, `.cursor/rules`, `.cursor/commands`, `.cursor/hooks.json`, `.cursor/mcp.json`; skills and agents under `.cursor`, `.agents`, `.claude` and `.codex`; the hooks in `.claude/settings.json` | `~/.cursor`: `rules`, `commands`, `skills`, `agents`, `hooks.json`, `mcp.json`, `cli-config.json`, `permissions.json`, `sandbox.json`; skills and agents under `~/.claude`, `~/.codex` and `~/.agents`; the hooks in `~/.claude/settings.json`, which Cursor runs |
| Antigravity | `AGENTS.md`, `GEMINI.md`, `.agents/rules`, `.agents/skills`, `.agents/agents`, `.agents/hooks.json` | `~/.gemini`: `GEMINI.md`, `AGENTS.md`, `config/rules`, `config/skills`, `config/agents`, `config/hooks.json`, the plugins in `config/config.json`, `config/mcp_config.json` |
| Cline | `AGENTS.md`, `.clinerules` (file or folder), `.cline/rules`, `.cursorrules`, `.windsurfrules`; workflows, skills and hooks under `.clinerules` and `.cline`; `.cline/agents`, `.agents/skills` | `~/.cline`: `rules`, `workflows`, `skills`, `agents`, `hooks`, `data/settings/cline_mcp_settings.json`, `data/settings/global-settings.json`; `~/Documents/Cline` rules, workflows and hooks; `~/.agents/AGENTS.md`, `~/.agents/skills` |
| OpenCode | `AGENTS.md`, else `CLAUDE.md`; `opencode.json(c)` and `.opencode/opencode.json(c)`, with the files their `instructions` name; agents, commands, skills and plugins under `.opencode`; `.claude/skills`, `.agents/skills` | `~/.config/opencode`: `AGENTS.md` (else `~/.claude/CLAUDE.md`), `opencode.json(c)`, agents, commands, skills, plugins; `~/.claude/skills`, `~/.agents/skills` |
| Kilo Code | as OpenCode, with `kilo.json(c)`, `.kilo` and `.kilocode/rules` | `~/.config/kilo`: `AGENTS.md`, `kilo.json(c)`, agents, commands, skills, plugins; `~/.agents/skills` |

Settings files count by their harness keys only: permissions, hooks, sandbox, plugins, MCP servers, instructions and the like. Preferences such as the theme and the model do not move the version. A key the reporter does not recognise is listed in the options, not hashed. `model` and `effort` lines are stripped from agent, skill and command files, and MCP server `env` and header values are dropped: a model change or a rotated key is not a harness change.

Never read: credentials, transcripts, history, caches and logs.

Beside the version, never in it, so they do not start a new version:

| Field | What |
|---|---|
| `approvals` | Digest of the permissions saved by "always allow" clicks: Claude Code's `settings.local.json` permissions and a project's `allowedTools`, Codex's `rules/default.rules`, Copilot CLI's `permissions-config.json`, the Cursor CLI's `cli-config.json` permissions. |
| `memory` | Digest of the memory index Claude Code loads at session start. |

In a manual list, recognised settings files count by their harness keys as above; other files count byte for byte.

## Recording a run

Two ways in, one record. Every field except the first two is optional.

| Field | Type | Meaning |
|---|---|---|
| `harness_version` | text | Label or fingerprint of the harness the run used. |
| `outcome` | `pass`, `fail`, `partial` | How the run ended. |
| `task_id` | text | Stable id of a repeatable task. Needed for the per-task half of the gate. |
| `task_class` | text | Category of work: `publish`, `build`, `ops`, or your own. |
| `verified` | boolean | The result was confirmed by a person or an oracle, not by the agent's own report. Default false. |
| `turns` | integer | Assistant turns. |
| `tool_calls` | integer | Tool calls made. |
| `duration_s` | number | Wall-clock seconds. |
| `input_tokens`, `output_tokens` | integer | Tokens consumed and produced. |
| `cost_usd` | number | Inference spend. |
| `denials` | integer | Permission or guard denials. Default 0. |
| `denial_classes` | map | Why the denials happened, class to count, at most 20: `classifier:<rule>`, `hook:<name>`, `person`, `settings`, `reviewer`. |
| `retries` | integer | Steps repeated. Default 0. |
| `interventions` | integer | Human corrections. Default 0. |
| `notes` | text | Up to 500 characters. Redacted in diagnostics. |
| `model`, `effort` | text | Model and reasoning effort that did the work. Kept out of the harness version. |
| `client`, `client_version` | text | Agent program and its version, e.g. `claude_code`, 2.1.283. |
| `fingerprint_schema` | integer | How the version was computed; 2 for this reporter. |
| `approvals`, `memory` | text | The digests above. |
| `run_key` | text | Idempotency key. A run posted twice with the same key is recorded once. |
| `harness_manifest` | object | The files selected, for the options screen. Kept as the entry's last selection, not in the run. |

Action, from an automation, a script or the REST API. `config_entry_id` names the agent to record against:

```yaml
action: agent_harness_performance_tracker.record_run
data:
  config_entry_id: 01J...
  harness_version: sha256:3f9a1c2e
  task_id: hacs-audit
  task_class: publish
  outcome: pass
  verified: true
  turns: 14
  duration_s: 640
```

Webhook, from anything that can POST JSON, no token needed. Save the run as `run.json`:

```json
{
  "harness_version": "sha256:3f9a1c2e",
  "outcome": "pass",
  "turns": 14
}
```

Then post it:

```bash
URL='https://homeassistant.local:8123/api/webhook/<id>'
curl --json @run.json "$URL"
```

Both answer with the run count, the current version, its success rate and whether the gate is failing. A bad field is refused with its name. A GET on the webhook answers the agent's settings: `agent`, `agent_program`, `selection`, `harness_files`, `version_label`, `fingerprint_schema`.

### The reporter

`tools/report_run.py`, standard library only, Python 3.10 or later; TOML settings are parsed from 3.11.

```bash
python tools/report_run.py --outcome pass --task-id hacs-audit
```

| Flag | Meaning |
|---|---|
| `--setup` | Store the webhook, show the harness files, register the hook. With `--webhook URL` it does not ask; `--no-hook` skips the hook. |
| `--show-files` | Print the harness files selected for the working directory, and the version, then exit. |
| `--agent PROGRAM` | The configured agent to report for. Required when the config holds more than one; the 0.3 top-level webhook counts as Claude Code's. |
| `--cwd DIR` | Project directory, default the working directory. |
| `--harness PATH` | File or directory that is part of the harness; repeatable. Replaces the selection. |
| `--harness-version` | Use this version instead of computing one. |
| `--label` | Prefix for the computed version. |
| `--print-version` | Print the version and exit, so two machines can confirm they run the same harness. |
| `--outcome` | `pass`, `fail` or `partial`. |
| `--task-id`, `--task-class`, `--verified`, `--turns`, `--tool-calls`, `--duration SECONDS`, `--input-tokens`, `--output-tokens`, `--cost USD`, `--denials`, `--retries`, `--interventions`, `--notes`, `--model`, `--effort`, `--client-version` | The run fields above. |
| `--run-key` | Idempotency key; a random one when absent. |
| `--webhook URL` | Post here instead of the configured webhook. |
| `--insecure` | Skip TLS verification, for a self-signed certificate. |
| `--dry-run` | Print the record and exit. |

Exit 0 means recorded, 1 refused by Home Assistant, 2 bad arguments.

### The hook

`report_run.py` is also the hook. `--setup` registers it for the program the entry names:

| Program | Registered in | Events | Transcript read |
|---|---|---|---|
| Claude Code | `~/.claude/settings.json` | Stop, SubagentStop, UserPromptSubmit, UserPromptExpansion, SessionStart, SessionEnd | `~/.claude/projects/<project>/<session>.jsonl` |
| Codex | `~/.codex/hooks.json` | Stop, UserPromptSubmit, SessionStart, SessionEnd | `~/.codex/sessions/.../rollout-*.jsonl` |
| GitHub Copilot CLI | `~/.copilot/hooks/ha-harness-tracker.json` | sessionStart, userPromptSubmitted, agentStop, subagentStop, sessionEnd | `~/.copilot/session-state/<session>/events.jsonl` |
| Cursor | `~/.cursor/hooks.json` | sessionStart, beforeSubmitPrompt, stop, sessionEnd | `~/.cursor/projects/<project>/agent-transcripts/<id>/<id>.jsonl` |
| Antigravity | `~/.gemini/config/hooks.json`, group `ha-harness-tracker` | SessionStart, Stop | `~/.gemini/antigravity-cli/brain/<id>/.system_generated/logs/transcript_full.jsonl` |
| Cline | `~/.cline/hooks/TaskComplete.js`, which runs the reporter | TaskComplete | `~/.cline/data/sessions/<id>/<id>.messages.json` |
| OpenCode | `~/.config/opencode/plugins/ha-harness-tracker.js` | session created, chat message, session idle | `~/.local/share/opencode/opencode.db`, read-only |
| Kilo Code | `~/.config/kilo/plugins/ha-harness-tracker.js` | as OpenCode | `~/.local/share/kilo/kilo.db`, read-only |

Registrations run the copy in `~/.config/ha-harness-tracker/`: Claude Code and Codex run it with no arguments and it identifies the client from the payload; the other six run `report_run.py --hook <program> [<event>]`. Paths follow `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `COPILOT_HOME`, `CLINE_DIR`, `CLINE_DATA_DIR`, `XDG_CONFIG_HOME` and `XDG_DATA_HOME`. On Windows, transcripts past 259 characters of path are read with the long-path prefix. Codex asks for new hooks to be trusted with `/hooks`; the others load them at the next session.

A payload is handled only when it comes from the program the hook was registered for: its fields have that program's shape and its transcript lies in that program's store. Cursor, Copilot and Continue also run Claude Code's hooks; those payloads are left alone, so nothing is recorded twice or under the wrong agent. A program with no webhook of its own in the local config records nothing; the 0.3 layout's top-level webhook is Claude Code's.

| Step | Who | What |
|---|---|---|
| 1 | hook, end of each turn | Appends a ledger line: tool calls, writes, pushes, tokens, duration, denials, prompts, harness version. Reads only the transcript past the last offset. No network. |
| 2 | agent, end of work | `Verdict: pass verified task=<id> <notes>` as the last line: outcome `pass`, `fail` or `partial`, then `verified` or `unverified`; `task=` and notes are optional, and the words may be bracketed. The hook rolls the turns since the last verdict into a run. |
| 3 | person, next prompt | Claude Code, Codex, Copilot CLI and Cursor hold the run for it: `/fail`, `/pass` or `/partial` posts the run with that outcome and `verified: true`; `/verdict <outcome> task=<id> class=<x>` does the same with overrides; any other prompt posts it as the agent reported it, `verified: false`. Antigravity, Cline, OpenCode and Kilo Code post the run when the turn ends; a verdict prompt in OpenCode or Kilo Code closes a span the agent left without one. |
| 4 | hook, session end | Claude Code, Copilot CLI and Cursor post a held run and say on stderr if turns still have no verdict. Cursor's print mode fires no stop, so its session end also closes the turn; in an interactive session, what the end finds after the last stop joins that turn. A run left by a killed session posts from any session six hours later, or at once with `--flush`. |
| 5 | hook, session start | Reads the settings from Home Assistant. Claude Code and Codex print one line from the tracker's last answer: the window gate and the recurring denial classes, which the agent reads before its first task. |

`verified` on a run means a person confirmed it; the agent's own claim goes into the notes (`agent reported pass`, and whether it said it verified by effect). Interventions are the person's prompts beyond the first in the span. The hook never infers an outcome from the transcript.

| Program | Tokens | Denials | Model |
|---|---|---|---|
| Claude Code | once per request, cache reads included | an error tool result that opens with a refusal: `classifier:<rule>`, `hook:<name>`, `person`, `settings` | transcript |
| Codex | once per response, from the token usage records | the approval reviewer's refusals, `reviewer` | turn context, with effort |
| GitHub Copilot CLI | the session's total, recorded at shutdown | a denied tool call, `permission:<kind>` | transcript, with effort |
| Cursor | field left out | field left out | hook payload |
| Antigravity | field left out | field left out | hook payload |
| Cline | per message, cache included | field left out | transcript |
| OpenCode, Kilo Code | per message, cache and reasoning included | a tool refused by a permission rule, `permission` | transcript |

Prompts are the person's prompts in the transcript; injected context, task notifications and command wrappers do not count. Codex counts the prompts its prompt hook saw, since its rollout mixes typed text with injected context. Codex gives session-end hooks three seconds, so its held run posts from the next session.

Cursor reads each hook's stdout as JSON: the hook answers `{"continue": true}` to a prompt and `{}` otherwise, also when it fails. A hook failure is written to stderr and to `hook-errors.log` in the ledger directory. A hook run stops itself after 60 seconds (`HARNESS_LEDGER_HOOK_DEADLINE`); the OpenCode plugin and the Cline hook run it with no timeout of their own.

The task id is `<directory>:<class>`, class being `publish` if the span pushed (`git push`, `gh pr create`, `gh release create`), `build` if it wrote files, `ops` otherwise. The agent names a repeatable job with `task=` in its verdict line; a person with `/verdict fail task=<id>`. The per-task half of the gate needs the same id to recur, so name the jobs that matter.

In Claude Code the bare words `pass`, `fail`, `partial` work as whole prompts with no slash; `/pass` and the others as custom commands need a skill or command of that name, whose only job is to acknowledge the hook's line. A verdict delivered on both prompt events posts once.

The local config lives outside every clone at `~/.config/ha-harness-tracker.json`, written by `--setup` readable by you alone (mode 0600; on Windows an ACL naming only you), because the webhook address is the credential:

```json
{
  "agents": {
    "claude_code": {
      "webhook_url": "https://homeassistant.local:8123/api/webhook/<id>",
      "insecure": false
    }
  }
}
```

The ledger sits in `~/.claude/harness-ledger/` when that directory exists, otherwise in `~/.config/ha-harness-tracker/state/`: one `.jsonl` per session, a state file with the byte offsets and the held run, the cached settings, and `versions/<digest>.json` listing each version's per-file hashes so two versions can be diffed by name. `python report_run.py --selftest` checks the parser and the flow on a synthetic transcript.

## Actions

| Action | Fields | Response |
|---|---|---|
| `record_run` | `config_entry_id` plus the run fields above | `duplicate`, `run_count`, `harness_version`, `pass_rate`, `regressed`, `regressed_tasks`; a repeated `run_key` answers `duplicate: true` and the run count |
| `set_baseline` | `config_entry_id`, optional `harness_version` | `baseline_version`, `pinned`. Empty version unpins. |
| `remove_runs` | `config_entry_id`, `run_keys` and/or `recorded_at` (1 to 50 runs in all) | `removed` (each removed run whole), `run_count`. Removes all the named runs or none; forgets their run keys so a run removed by mistake can be recorded again, and drops the file selection a removed run reported. |
| `retag_runs` | `config_entry_id`, `runs`: each run's `run_key` or `recorded_at` mapped to its new task id (1 to 50) | `retagged` (each change: `run`, `recorded_at`, `from`, `to`), `run_count`, `regressed`, `regressed_tasks`. Changes all the named runs or none; everything else about a run stays, including its place in the log. |

All four refuse an unknown entry and an unloaded entry; `set_baseline` refuses a version with no recorded runs, and `remove_runs` and `retag_runs` a name that matches no stored run, each with a message saying which. Runs recorded before 0.4 have no `run_key`; name them by `recorded_at`, which the run-recorded event and the Last run sensor carry.

The webhook answers `recorded`, `run_count`, `harness_version`, `pass_rate`, `current_runs`, `confirmed`, `comparable_tasks`, `model_changed`, `regressed` and `window`; a pass rate is read with the number of runs behind it. `window` carries the window gate: `size`, `runs`, `pass_rate`, `prior_pass_rate`, `denials_per_run`, `prior_denials_per_run`, `denials_per_100_calls`, `prior_denials_per_100_calls`, `improvement`, `shared_tasks`, `regressed`, `versions` (a count) and `recurring_denials` (up to five `[class, count]` pairs). A repeated `run_key` answers `recorded: false`, `duplicate: true` and the run count.

## Events

| Event | When | Data |
|---|---|---|
| `agent_harness_performance_tracker_run_recorded` | every run | `entry_id`, `agent`, every run field except the manifest, `recorded_at` |
| `agent_harness_performance_tracker_regression` | a gate turns on | `entry_id`, `agent`, `kind` (`version` or `window`); the version gate adds `harness_version`, `baseline_version`, `improvement`, `regressed_tasks`; the window gate adds `window`, `improvement`, `denials_per_100_calls`, `prior_denials_per_100_calls`, `versions`, `recurring_denials` |

The regression event fires on the transition only; the repair issue stays until the gate clears.

## Use

- Let the reporter fingerprint the harness so every rules or hook change is a new version automatically, then read `improvement_over_baseline` a day later.
- Give repeatable jobs a `task_id`. Both gates compare success over shared task ids, and the per-task half names the task a change broke.
- Automate on the regression event: a notification, or a script that pins the previous baseline and files the diff for review.
- Keep `verified` honest. A high success rate with a low verified rate is a harness that reports well, not one that works.

## Design notes

- The version gate compares the current version to one baseline. Two versions running side by side interleave, and the current version is whichever ran last; the window gate judges runs whatever their version.
- Task ids are the reporter's. Two reporters naming the same job differently are two tasks.
- Cost is summed from what reporters send; nothing is looked up.
- Runs older than the retention are gone from the store. The recorder keeps the sensor history.
- Versions from fingerprint schema 1 and 2 are different hashes of different inputs, so a reporter upgrade starts a new version.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Baseline stays unknown | No version has `min_runs` runs yet | Wait, lower the option, or pin a version with `set_baseline`. |
| Gate on right after a harness change | A task the old version solved failed on the new one | Read the `tasks` attribute on Regressed tasks; the aggregate half cannot fire until the new version is confirmed. |
| A new version every session | A selected file changes by itself | `--show-files` twice, compare, and move that file out: use Manual, or report it. |
| Options say no run has reported its files | No run from a 0.4 reporter yet | Finish a piece of work with a verdict line. |
| A finished task posted nothing | The hook failed, or the agent's last message had no verdict line | Read `hook-errors.log` in the ledger directory, then the session's ledger file. |
| Setup refuses the address | It is not `…/api/webhook/<id>`, or Home Assistant did not answer | Copy the address from the agent's Configure screen. |
| Webhook answers 200 with no body | The id is not registered: the entry is unloaded or the id is wrong | Reload the entry. Home Assistant answers unknown ids that way on purpose. |
| Webhook answers 400 | A field failed validation | The body names the field. |
| Action refused: not loaded | The entry is unloaded | Reload it; the action exists either way. |

## Removing it

Delete the entry under Settings, Devices & services. The device, its entities, the webhook and the run log are removed with it. On the agent's machine:

| Program | Remove |
|---|---|
| Claude Code, Codex, Cursor | the hook entries running `report_run.py` in `~/.claude/settings.json`, `~/.codex/hooks.json` or `~/.cursor/hooks.json` |
| Antigravity | the `ha-harness-tracker` group in `~/.gemini/config/hooks.json` |
| GitHub Copilot CLI, Cline, OpenCode, Kilo Code | `~/.copilot/hooks/ha-harness-tracker.json`, `~/.cline/hooks/TaskComplete.js`, or `plugins/ha-harness-tracker.js` in the OpenCode or Kilo config directory |

Then delete `~/.config/ha-harness-tracker.json` and `~/.config/ha-harness-tracker/`.

## Licence

MIT. See `LICENSE`.
