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
| Verified rate | Runs whose result was confirmed by something other than the agent: a person, a test oracle, an effect checked by the reporter. |
| Median turns, median duration | Effort per run on the current version. |
| Interventions per run | Times a human had to correct or redirect the agent. |
| Denials per run | Permission or guard denials the agent hit. |
| Baseline harness version | The best confirmed version, or the one you pinned. |
| Improvement over baseline | Current success rate minus the baseline's, in points, over only the task ids both versions ran. Unknown when they share none. |
| Regressed tasks | Task ids the baseline solved whose latest run on the current version failed. |
| Window success rate, window denials per 100 tool calls | The last `window` runs, whatever harness versions they used. |
| Recurring denial classes | Denial classes seen at least twice in the last `window` runs, with counts. |
| Harness regressed | On while either gate is failing. Each gate raises its own repair issue and fires an event. |

## The gate

A version is confirmed once it has `min_runs` runs on it (default 10). The
baseline is the confirmed version with the highest success rate, most recent on
a tie, unless you pin one. The gate reports a regression when either half fires:

| Half | Fires when |
|---|---|
| Aggregate | The current version is confirmed and, over the task ids both versions ran, its success rate is more than `tolerance` points (default 5) below the baseline's. Off when they share no task, and off when the model most runs reported differs between the two versions. |
| Per task | A task id that passed on the baseline has its latest run on the current version fail. Fires before the current version is confirmed: one broken task is evidence on its own. |

Both halves need a baseline. Until some version has `min_runs` runs, nothing
is confirmed and the gate stays off; pin a version with `set_baseline` to
start measuring sooner. One bad run on a fresh version never trips the
aggregate half: a lucky or unlucky rollout is exploration, and a version earns
a verdict only after confirmation.

### The window gate

A harness edited several times a day never gives one version `min_runs` runs,
so the version gate rarely compares anything. The window gate judges the last
`window` runs (default 10) against the `window` before them, whatever versions
they span, and names the versions inside.

| Fires when | Held when |
|---|---|
| Over the task ids both windows ran, the success rate drops by more than max(`tolerance`, 150 / `window`) points: with ten runs, two bad runs, not one | The model most runs reported differs between the windows |
| Denials per 100 tool calls rise by 1.0 or more, with at least 100 calls on each side. Per run, one long span reads as a harness getting worse | |

Nothing is compared until there are twice `window` runs. The repair issue lists
the versions in the window and the recurring denial classes; the change that
caused it is among those versions.

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
| Runs per window | options | 10 | Size of each half of the window gate, 3 to 100. |

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
| `task_class` | text | Category of work: `publish`, `build`, `ops`, or your own. |
| `verified` | boolean | The result was confirmed by a person or an oracle, not by the agent's own report. Default false. |
| `turns` | integer | Assistant turns. |
| `tool_calls` | integer | Tool calls made. |
| `duration_s` | number | Wall-clock seconds. |
| `input_tokens`, `output_tokens` | integer | Tokens consumed and produced. |
| `cost_usd` | number | Inference spend. |
| `denials` | integer | Permission or guard denials. Default 0. |
| `denial_classes` | map | Why the denials happened, class to count, at most 20: `classifier:<rule>`, `hook:<name>`, `person`, `settings`. |
| `retries` | integer | Steps repeated. Default 0. |
| `interventions` | integer | Human corrections. Default 0. |
| `notes` | text | Up to 500 characters. Redacted in diagnostics. |
| `model` | text | Model that did the work. Kept out of the harness version, so a model change does not read as a harness change. |
| `client_version` | text | Agent client version, e.g. Claude Code 2.1.280. |

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

`tools/claude_code_hook.py` reports runs from real sessions with nothing to
remember. The agent ends each piece of work with one line in its final
message; the hook does the rest.

| Step | Who | What |
|---|---|---|
| 1 | hook, every turn | Appends a ledger line: tool calls, writes, pushes, tokens (cache reads included), duration, denials, prompts, harness fingerprint. Reads only the transcript bytes past the last offset. No network. |
| 2 | agent, end of work | `Verdict: pass\|fail\|partial verified\|unverified [task=<id>] [notes]` as the last line. The hook rolls the turns since the last verdict into a run and holds it. |
| 3 | person, next prompt | `/fail`, `/pass` or `/partial` posts the run with that outcome and `verified: true`. `/verdict <outcome> task=<id> class=<x>` does the same with overrides. Any other prompt posts the run as the agent reported it, `verified: false`. |
| 4 | hook, session end | Posts a held run. Says on stderr if turns still have no verdict. A run left by a killed session posts from any session six hours later, or at once with `--flush`. |
| 5 | hook, session start | Prints one line from the tracker's last answer: the window gate and the recurring denial classes. The agent reads it before its first task. |

`verified` on a run means a person confirmed it; the agent's own claim goes
into the notes. Interventions are the person's prompts beyond the first in
the span; task notifications, meta entries, compaction summaries and command
wrappers are the client's, not the person's, and do not count. A denial is an
error tool result that opens with a refusal, classed as `classifier:<rule>`,
`hook:<name>`, `person` or `settings`; output that merely quotes one does not
count. The hook never infers an outcome from the transcript.

The task id is `<directory>:<class>`, class being `publish` if the span
pushed (`git push`, `gh pr create`, `gh release create`), `build` if it wrote
files, `ops` otherwise. The agent names a repeatable job with `task=` in its
verdict line; a person with `/verdict fail task=<id>`. The per-task half of
the gate needs the same id to recur, so name the jobs that matter.

Register the hook in `~/.claude/settings.json` under `Stop`, `SubagentStop`,
`UserPromptSubmit`, `UserPromptExpansion` (matcher `verdict|pass|fail|partial`),
`SessionStart` and `SessionEnd`:

```json
{"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "python",
  "args": ["/home/you/.claude/hooks/claude_code_hook.py"], "timeout": 10}]}]}}
```

Use the absolute path (`~` is not expanded in `args`) and a 40 second
timeout on the two prompt events because they post. Hooks load at session
start. The bare words `pass`, `fail`, `partial` work as whole prompts with
no slash; `/pass` and the others as custom commands need a skill or command
of that name, whose only job is to acknowledge the hook's line. A verdict
delivered on both prompt events posts once.

Config lives outside every clone at `~/.config/ha-harness-tracker.json`:

```json
{"webhook_url": "https://homeassistant.local:8123/api/webhook/<id>",
 "harness": ["~/work/CLAUDE.md", "~/.claude/hooks", "~/.claude/settings.json",
             "~/.claude/skills"],
 "insecure": false}
```

`harness` names the files whose bytes are the version, the same fingerprint
as the reporter; `settings.json` is hashed on its `permissions` and `hooks`
keys only, so a theme change is not a new harness. Set `insecure` for a
self-signed certificate. Keep the file at 0600: the webhook id is the
credential.

The ledger sits in `~/.claude/harness-ledger/`: one `.jsonl` per session, a
state file with the byte offsets and the held run, and
`versions/<digest>.json` listing each version's per-file hashes so two
versions can be diffed by name. `python claude_code_hook.py --selftest`
checks the parser and the flow on a synthetic transcript.

## Actions

| Action | Fields | Response |
|---|---|---|
| `record_run` | `config_entry_id` plus the run fields above | `run_count`, `harness_version`, `pass_rate`, `regressed`, `regressed_tasks` |

The webhook answers `recorded`, `run_count`, `harness_version`, `pass_rate`, `current_runs`, `confirmed`, `comparable_tasks`, `model_changed` and `regressed`: a pass rate is read with the number of runs behind it. `window` carries the window gate: `size`, `runs`, `pass_rate`, `prior_pass_rate`, `denials_per_run`, `prior_denials_per_run`, `denials_per_100_calls`, `prior_denials_per_100_calls`, `improvement`, `shared_tasks`, `regressed`, `versions` and `recurring_denials`.
| `set_baseline` | `config_entry_id`, optional `harness_version` | `baseline_version`, `pinned`. Empty version unpins. |

Both refuse an unknown entry, an unloaded entry and, for `set_baseline`, a
version with no recorded runs, each with a message saying which.

## Events

| Event | When | Data |
|---|---|---|
| `agent_harness_performance_tracker_run_recorded` | every run | `entry_id`, `agent`, every run field, `recorded_at` |
| `agent_harness_performance_tracker_regression` | a gate turns on | `entry_id`, `agent`, `kind` (`version` or `window`); the version gate adds `harness_version`, `baseline_version`, `improvement`, `regressed_tasks`; the window gate adds `window`, `improvement`, `denials_per_100_calls`, `prior_denials_per_100_calls`, `versions`, `recurring_denials` |

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
