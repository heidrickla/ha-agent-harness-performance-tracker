# Agent Harness Performance Tracker

A Home Assistant integration that records each run an AI agent reports,
groups the runs by the harness version they ran under, and tells you whether a
harness change made the agent better or worse.

A harness is everything around a frozen model: the rules file, the hooks, the
memory, the tools, the prompts. Salesforce's DarwinX work showed an agent going
from 43.5% to 93% task completion with the model unchanged, every gain from
evolving the harness under a preserve-and-extend gate: a change has to improve
something without breaking what already worked. This integration is that gate,
kept in the place you already watch.

## What it measures

Each run is one unit of work the agent finished. The reporter says which
harness version it ran under and how it went; the integration does the rest.

| Figure | Meaning |
|---|---|
| Success rate | Runs that passed on the current harness version, as a percentage. Partial counts as not passed. |
| Verified rate | Runs whose result was verified by effect rather than taken on trust. |
| Median turns, median duration | Effort per run on the current version. |
| Interventions per run | Times a human had to correct or redirect the agent. |
| Denials per run | Permission or guard denials the agent hit. |
| Baseline harness version | The best confirmed version, or the one you pinned. |
| Improvement over baseline | Current success rate minus the baseline's, in points. |
| Regressed tasks | Task ids the baseline solved whose latest run on the current version failed. |
| Harness regressed | On while the gate is failing. Raises a repair issue and fires an event. |

## The gate

A version is confirmed once it has `min_runs` runs on it (default 10). The
baseline is the confirmed version with the highest success rate, most recent on
a tie, unless you pin one. The gate reports a regression when either half fires:

| Half | Fires when |
|---|---|
| Aggregate | The current version is confirmed and its success rate is more than `tolerance` points (default 5) below the baseline's. |
| Per task | A task id that passed on the baseline has its latest run on the current version fail. Fires before the current version is confirmed: one broken task is evidence on its own. |

Both halves need a baseline. Until some version has `min_runs` runs, nothing
is confirmed and the gate stays off; pin a version with `set_baseline` to
start measuring sooner. One bad run on a fresh version never trips the
aggregate half: a lucky or unlucky rollout is exploration, and a version earns
a verdict only after confirmation.

## Installation

HACS: add `https://github.com/heidrickla/ha-agent-harness-performance-tracker`
as a custom repository of type Integration, install, restart Home Assistant.

Manual: copy `custom_components/agent_harness_performance_tracker` into
`config/custom_components/`, restart.

Then Settings, Devices & services, Add integration, Agent Harness Performance
Tracker. One entry per agent.

## Configuration

| Field | Where | Default | Effect |
|---|---|---|---|
| Agent name | setup, reconfigure | | Device name and entity prefix. Must be unique. |
| Runs to confirm a harness version | options | 10 | Runs before a version's success rate counts as a baseline. |
| Regression tolerance (points) | options | 5 | Allowed drop below the baseline before the aggregate half fires. |
| Runs to keep | options | 2000 | Older runs are dropped once this many are stored. |

Saving options reloads the entry and re-runs the gate against the stored runs.
Reconfigure renames the agent; the webhook and the runs stay.

The confirmation screen after setup shows the webhook path once. It is the
reporter's only credential and is not shown again; reconfigure does not reveal
it, and diagnostics redact it. Remove and re-add the agent to get a new one.

## Recording a run

Two ways in, one record. Every field except the first two is optional.

| Field | Type | Meaning |
|---|---|---|
| `harness_version` | text | Label or fingerprint of the harness the run used. |
| `outcome` | `pass`, `fail`, `partial` | How the run ended. |
| `task_id` | text | Stable id of a repeatable task. Needed for the per-task half of the gate. |
| `task_class` | text | Category of work: publish, diagnose, build. |
| `verified` | boolean | The result was verified by effect. Default false. |
| `turns` | integer | Assistant turns. |
| `tool_calls` | integer | Tool calls made. |
| `duration_s` | number | Wall-clock seconds. |
| `input_tokens`, `output_tokens` | integer | Tokens consumed and produced. |
| `cost_usd` | number | Inference spend. |
| `denials` | integer | Permission or guard denials. Default 0. |
| `retries` | integer | Steps repeated. Default 0. |
| `interventions` | integer | Human corrections. Default 0. |
| `notes` | text | Up to 500 characters. Redacted in diagnostics. |

Action, from an automation, a script or the REST API:

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

Webhook, from anything that can POST JSON, no token needed:

```bash
curl -X POST https://homeassistant.local:8123/api/webhook/<id> \
  -H 'Content-Type: application/json' \
  -d '{"harness_version":"sha256:3f9a1c2e","outcome":"pass","turns":14}'
```

Both answer with the run count, the current version, its success rate and
whether the gate is failing. A bad field is refused with its name.

### The reporter

`tools/report_run.py` posts a run from a workstation and computes the harness
version for you: a short SHA-256 over the files that make up the harness.

```bash
python tools/report_run.py --harness CLAUDE.md --harness ~/.claude/hooks \
  --outcome pass --task-id hacs-audit --turns 14 --duration 640
```

It reads `HA_URL` and `HA_TOKEN` to call the action, or `--webhook <url>` to
post without a token. `--print-version` shows the fingerprint and exits, so
two machines can confirm they run the same harness.

### The Claude Code hook

`tools/claude_code_hook.py` reports runs from real sessions without the agent
grading itself. Copy it to `~/.claude/hooks/` and register it in
`~/.claude/settings.json` under five events:

| Event | What the hook does |
|---|---|
| `Stop` | Appends one ledger line for the finished turn: tool calls, tokens (cache reads included), duration, denials, prompts, harness fingerprint. Reads only the transcript bytes past the last offset. No network. |
| `SubagentStop` | The same for a subagent's transcript, folded into the session's ledger. |
| `UserPromptSubmit` | On `verdict pass\|fail\|partial [task-id] [--verified] [class=<x>] [notes]`, with or without a leading slash, rolls every ledger line since the last verdict into one run and posts it. The reply lands in the conversation. |
| `UserPromptExpansion` | The same when `/verdict` is a custom command and arrives as a command name plus arguments. Matcher `verdict`. A verdict delivered on both events posts once; the second finds the span empty. |
| `SessionEnd` | Says on stderr if turns are still waiting for a verdict. No verdict, no run. |

```json
{"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "python",
  "args": ["/home/you/.claude/hooks/claude_code_hook.py"], "timeout": 10}]}]}}
```

Repeat the block for the other events with the absolute path (`~` is not
expanded in `args`); give the two prompt events a timeout of 40 seconds
because they post. Hooks load at session start. A `verdict` skill or command
is optional: the bare `verdict pass ...` form needs no command routing, and
a skill's only job is to acknowledge the hook's line.

Config lives outside every clone at `~/.config/ha-harness-tracker.json`:

```json
{"webhook_url": "https://homeassistant.local:8123/api/webhook/<id>",
 "harness": ["~/work/CLAUDE.md", "~/.claude/hooks", "~/.claude/settings.json"],
 "insecure": false}
```

`harness` names the files whose bytes are the version, the same fingerprint
as the reporter; `settings.json` is hashed on its `permissions` and `hooks`
keys only, so a theme change is not a new harness. Set `insecure` for a
self-signed certificate. Keep the file at 0600: the webhook id is the
credential.

The ledger sits in `~/.claude/harness-ledger/`: one `.jsonl` per session, a
state file with the byte offsets, and `versions/<digest>.json` listing each
version's per-file hashes so two versions can be diffed by name. A span left
open at session end is closed by the next verdict, in any later session.
Interventions are the person's prompts beyond the first in the span. The
outcome and the verified flag come only from the verdict; the hook infers
neither. `python claude_code_hook.py --selftest` checks the parser on a
synthetic transcript.

## Actions

| Action | Fields | Response |
|---|---|---|
| `record_run` | `config_entry_id` plus the run fields above | `run_count`, `harness_version`, `pass_rate`, `regressed`, `regressed_tasks` |
| `set_baseline` | `config_entry_id`, optional `harness_version` | `baseline_version`, `pinned`. Empty version unpins. |

Both refuse an unknown entry, an unloaded entry and, for `set_baseline`, a
version with no recorded runs, each with a message saying which.

## Events

| Event | When | Data |
|---|---|---|
| `agent_harness_performance_tracker_run_recorded` | every run | `entry_id`, `agent`, every run field, `recorded_at` |
| `agent_harness_performance_tracker_regression` | the gate turns on | `entry_id`, `agent`, `harness_version`, `baseline_version`, `improvement`, `regressed_tasks` |

The regression event fires on the transition only; the repair issue stays
until the gate clears.

## Use

- Fingerprint the harness in the reporter so every rules or hook change is a
  new version automatically, then read `improvement_over_baseline` a day later.
- Give repeatable jobs a `task_id`. The per-task half of the gate is the only
  thing that can tell you a change broke something the old harness did.
- Automate on the regression event: a notification, or a script that pins the
  previous baseline and files the diff for review.
- Keep `verified` honest. A high success rate with a low verified rate is a
  harness that reports well, not one that works.

## Known limitations

- The gate compares the current version to one baseline. Two versions
  running side by side interleave, and the current version is whichever ran
  last.
- Task ids are the reporter's. Two reporters naming the same job differently
  are two tasks.
- Cost is summed from what reporters send; nothing is looked up.
- Runs older than the retention are gone from the store. The recorder keeps
  the sensor history.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Baseline stays unknown | No version has `min_runs` runs yet | Wait, lower the option, or pin a version with `set_baseline`. |
| Gate on right after a harness change | A task the old version solved failed on the new one | Read the `tasks` attribute on Regressed tasks; the aggregate half cannot fire until the new version is confirmed. |
| Webhook answers 200 with no body | The id is not registered: the entry is unloaded or the id is wrong | Reload the entry. Home Assistant answers unknown ids that way on purpose. |
| Webhook answers 400 | A field failed validation | The body names the field. |
| Action refused: not loaded | The entry is unloaded | Reload it; the action exists either way. |

## Removing it

Delete the entry under Settings, Devices & services. The device, its
entities, the webhook and the run log are removed with it.

## Licence

MIT. See `LICENSE`.
