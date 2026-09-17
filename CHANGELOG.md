# Changelog

Newest first, in the style of [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- `tools/claude_code_hook.py`, a Claude Code hook that ledgers every turn
  from the session transcript (tool calls, tokens with cache reads included,
  duration, denials, prompts, harness fingerprint) and posts one run on
  `/verdict pass|fail|partial [task-id] [--verified] [class=<x>] [notes]`.
  The outcome is typed by the person, never inferred. Self-test with
  `--selftest`; proven end to end against a live install.

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
