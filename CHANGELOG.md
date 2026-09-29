# Changelog

Newest first, in the style of [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.3.0] - 2026-09-27

### Added

- The window gate, for a harness edited faster than one version collects
  `min_runs` runs: the last `window` runs (option, default 10) against the
  `window` before them, whatever versions they span. It regresses when the pass
  rate over shared tasks drops by more than max(tolerance, 150 / window) points
  or denials per 100 tool calls rise by 1.0 or more (at least 100 calls on each
  side), holds when the model changed, and raises its own repair issue naming
  the versions in the window.
- Optional run field `denial_classes`, a map of class to count; a class seen
  twice in the window is recurring.
- Sensors window success rate, window denials per 100 tool calls and recurring
  denial classes; webhook reply field `window`; regression event field `kind`;
  `denials_per_100_calls` in the per-version statistics.

### Changed

- The problem sensor is on while either gate reports a regression.
- `tools/claude_code_hook.py` counts only prompts a person typed, not task
  notifications, meta entries, compaction summaries or command wrappers. It
  counts a denial only from an error result that opens with a refusal (output
  quoting one was counted before), counts the person's declines (missed
  before), posts each run's denial classes, and prints the window gate at
  session start.

## [0.2.0] - 2026-09-24

### Changed

- The aggregate half of the gate compares only the task ids both versions ran;
  rates over different task mixes measured the mix. No shared task means no
  comparison. Runs without a task id are no longer compared at all.
- The aggregate half stays off when the model most runs reported differs
  between baseline and current.

### Added

- Optional run fields `model` and `client_version`.
- Webhook reply fields `current_runs`, `confirmed`, `comparable_tasks`,
  `model_changed`; gate attributes `comparable_tasks`, `model_changed`.
- `tools/claude_code_hook.py`, a Claude Code hook that ledgers every turn
  from the session transcript (tool calls, writes, pushes, tokens with cache
  reads included, duration, denials, prompts, harness fingerprint). The agent
  ends a piece of work with `Verdict: pass|fail|partial verified|unverified
  [task=<id>]`; the hook holds the run and posts it on the person's next
  prompt, as reported and unverified, or as confirmed when that prompt is
  `/pass`, `/fail` or `/partial`. Task ids default to `<directory>:<class>`
  with the class read from what the span did. Nothing is inferred from the
  transcript beyond the figures. Self-test with `--selftest`; proven end to
  end against a live install.

## [0.1.0] - 2026-09-17

### Added

- One config entry per agent, with a webhook id generated at setup and shown
  once on the confirmation screen.
- `record_run` action and a webhook that take the same run record: harness
  version, outcome, task id and class, verified flag, turns, tool calls,
  duration, tokens, cost, denials, retries, interventions, notes.
- Per-harness statistics: success rate, verified rate, median turns and
  duration, interventions and denials per run, cost.
- The preserve-and-extend gate: a baseline is the best confirmed version or a
  pinned one; a regression is a confirmed drop beyond the tolerance or a task
  the baseline solved failing on the current version. Shown as a binary
  sensor, raised as a repair issue, fired as an event on the transition.
- `set_baseline` action to pin or unpin the baseline.
- Options for the confirmation threshold, the tolerance and the retention;
  saving reloads the entry and re-runs the gate.
- Diagnostics with the webhook id and the run notes redacted.
- `tools/report_run.py`, a reporter that fingerprints the harness files and
  posts a run through the action or the webhook.
