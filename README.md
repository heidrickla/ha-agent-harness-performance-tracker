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

Copy `custom_components/agent_harness_performance_tracker` into `config/custom_components/` and restart Home Assistant.

Then Settings, Devices & services, Add integration, Agent Harness Performance Tracker. One entry per agent.

## Setting up an agent

1. Add the agent in Home Assistant: a name and the agent program.
2. Open the agent's Configure screen and copy the webhook address at the top. The confirmation screen shows it too, when Home Assistant displays one.
3. On the machine the agent runs on, from the project directory:

```bash
python tools/report_run.py --setup
```

Paste the address when asked. Setup checks it against Home Assistant, offers to trust a self-signed certificate, writes the local config, prints the harness files it selected for the current directory, and for Claude Code and Codex offers to register the reporting hook. It copies itself to `~/.config/ha-harness-tracker/report_run.py`, so the hook does not depend on the clone.

| Agent program | Harness files | Runs reported by |
|---|---|---|
| Claude Code | automatic | hook |
| Codex | automatic | hook |
| GitHub Copilot (VS Code) | suggested list | command |
| Cursor | suggested list | command |
| OpenCode | suggested list | command |
| Google Antigravity | suggested list | command |
| JetBrains Junie | suggested list | command |
| Cline | suggested list | command |
| GitHub Copilot CLI | suggested list | command |
| Kilo Code | suggested list | command |
| Other | manual | command |

A suggested list is taken from the agent's documentation and starts the options' manual list; check each path. The command is `report_run.py --outcome pass|fail|partial`, run by the agent as its last step, by its own end-of-task hook, or by a script.

## Configuration

| Field | Where | Default | Effect |
|---|---|---|---|
| Agent name | setup, reconfigure | | Device name and entity prefix. Must be unique. |
| Agent program | setup, reconfigure | Claude Code | Which file profile and suggestions apply. |
| Runs to confirm a harness version | options | 10 | Runs before a version's success rate counts as a baseline. |
| Regression tolerance (points) | options | 5 | Allowed drop below the baseline before the aggregate half fires. |
| Runs to keep | options | 2000 | Older runs are dropped once this many are stored. |
| Runs per window | options | 10 | Size of each half of the window gate, 3 to 100. |
| Selection | options, Harness files | Automatic for Claude Code and Codex, else Manual | Automatic uses the program's file profile; Manual uses the list below. |
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

Settings files count by their harness keys only: permissions, hooks, sandbox, plugins, MCP servers, instructions and the like. Preferences such as the theme and the model do not move the version. A key the reporter does not recognise is listed in the options, not hashed. `model` and `effort` lines are stripped from agent, skill and command files, and MCP server `env` and header values are dropped: a model change or a rotated key is not a harness change.

Never read: credentials, transcripts, history, caches and logs.

Beside the version, never in it, so they do not start a new version:

| Field | What |
|---|---|
| `approvals` | Digest of the permissions saved by "always allow" clicks: Claude Code's `settings.local.json` permissions and a project's `allowedTools`, Codex's `rules/default.rules`. |
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
| `--agent PROGRAM` | Which configured agent to report for, when the config holds several. |
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

For Claude Code and Codex, `report_run.py` is also the hook: registered by `--setup`, it reads the hook payload on stdin. A payload is handled only when it identifies its client: the event is one that client sends and the transcript lives in that client's session store. Cursor, Copilot CLI and Continue can run Claude Code's hooks; their payloads are left alone, so nothing is recorded twice or under the wrong agent.

| Step | Who | What |
|---|---|---|
| 1 | hook, every turn | Appends a ledger line: tool calls, writes, pushes, tokens, duration, denials, prompts, harness version. Reads only the transcript bytes past the last offset. No network. |
| 2 | agent, end of work | `Verdict: pass\|fail\|partial verified\|unverified [task=<id>] [notes]` as the last line. The hook rolls the turns since the last verdict into a run and holds it. |
| 3 | person, next prompt | `/fail`, `/pass` or `/partial` posts the run with that outcome and `verified: true`. `/verdict <outcome> task=<id> class=<x>` does the same with overrides. Any other prompt posts the run as the agent reported it, `verified: false`. |
| 4 | hook, session end | Claude Code: posts a held run and says on stderr if turns still have no verdict. A run left by a killed session posts from any session six hours later, or at once with `--flush`. |
| 5 | hook, session start | Reads the settings from Home Assistant, and prints one line from the tracker's last answer: the window gate and the recurring denial classes. The agent reads it before its first task. |

`verified` on a run means a person confirmed it; the agent's own claim goes into the notes. Interventions are the person's prompts beyond the first in the span. The hook never infers an outcome from the transcript.

| | Claude Code | Codex |
|---|---|---|
| Transcript | `~/.claude/projects/<project>/<session>.jsonl` | `~/.codex/sessions/.../rollout-*.jsonl` |
| Tokens | once per request, cache reads included | once per response, from the token usage records |
| Prompts | the person's prompts in the transcript; task notifications, meta entries, compaction summaries and command wrappers do not count | the prompts the prompt hook saw, since the rollout mixes typed text with injected context |
| Denials | an error tool result that opens with a refusal: `classifier:<rule>`, `hook:<name>`, `person`, `settings` | the approval reviewer's refusals, `reviewer` |
| Session end | posts the held run | leaves it for the next session: Codex gives session-end hooks three seconds |
| After setup | hooks load at session start | approve the new hooks in Codex with `/hooks` |

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

Both refuse an unknown entry, an unloaded entry and, for `set_baseline`, a version with no recorded runs, each with a message saying which.

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
| Setup refuses the address | It is not `…/api/webhook/<id>`, or Home Assistant did not answer | Copy the address from the agent's Configure screen. |
| Webhook answers 200 with no body | The id is not registered: the entry is unloaded or the id is wrong | Reload the entry. Home Assistant answers unknown ids that way on purpose. |
| Webhook answers 400 | A field failed validation | The body names the field. |
| Action refused: not loaded | The entry is unloaded | Reload it; the action exists either way. |

## Removing it

Delete the entry under Settings, Devices & services. The device, its entities, the webhook and the run log are removed with it. Remove the hook entries from `~/.claude/settings.json` or `~/.codex/hooks.json`, and delete `~/.config/ha-harness-tracker.json` and `~/.config/ha-harness-tracker/`.

## Licence

MIT. See `LICENSE`.
