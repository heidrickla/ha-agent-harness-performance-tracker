# Changelog

Newest first, in the style of [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.6.0] - 2026-09-30

### Added

- Action `retag_runs`: gives up to 50 named runs (by `run_key` or `recorded_at`) a new task id, all or none, recomputes the figures, and answers each change with the id it replaced. A run keeps its place in the log, which a removal and a new `record_run` would not.

## [0.5.2] - 2026-09-30

### Fixed

- The reporter reads a verdict's `task=`, `class=` and `verified` in brackets as well as bare, as in `Verdict: pass [task=hacs-audit]`. A bracketed task id went into the notes and the run was filed under `<directory>:<class>`.

## [0.5.1] - 2026-09-29

### Fixed

- The Runs sensor's state class is `total`, not `total_increasing`: `remove_runs` and a lower "Runs to keep" reduce it, which Home Assistant logged as a state that is not strictly increasing.

## [0.5.0] - 2026-09-29

### Added

- Automatic handling for GitHub Copilot CLI, Cursor, Antigravity, Cline, OpenCode and Kilo Code: a harness-file profile for each, and a reporting hook `--setup` registers (a hooks file for Copilot CLI, Cursor and Antigravity, a `TaskComplete.js` hook for Cline, a plugin for OpenCode and Kilo Code). Each reads its agent's own transcript or store for tool calls, prompts, model, client version and, where the agent records them, tokens and denials.
- `report_run.py --hook <program> [<event>]` as the hook entry for these agents; the payload must still have that agent's shape and point into its store.
- Hook failures are written to `hook-errors.log` in the ledger directory.
- Action `remove_runs`: removes up to 50 named runs (by `run_key` or `recorded_at`), all or none, recomputes the figures, and answers with the removed runs.

### Changed

- Antigravity, Cline, OpenCode and Kilo Code post a run when the turn ends, since they have no prompt and session-end hooks to hold it for; Claude Code, Codex, Copilot CLI and Cursor hold it for the person's verdict as before.
- A run leaves out tokens and denials when its agent does not record them, instead of sending zero.
- The 0.3 config's top-level webhook is used for Claude Code only; another program without its own webhook records nothing.
- `report_run.py` without `--agent` reports for the one agent configured, and refuses when there are several instead of picking one; setup prints the command with `--agent`.
- The OpenCode and Kilo Code plugin and the Cline hook call the reporter with no timeout; a hook run stops itself after 60 seconds.
- In an interactive Cursor session, what the session end finds after the last stop joins that turn instead of counting as another.
- A run the person confirms keeps what the agent reported in its notes (`agent reported pass`).
- Windows paths past 259 characters are read with the long-path prefix.

## [0.4.0] - 2026-09-29

### Added

- Agent program on each entry: Claude Code, GitHub Copilot (VS Code), Codex, Cursor, OpenCode, Google Antigravity, JetBrains Junie, Cline, GitHub Copilot CLI, Kilo Code, or Other. Entries from 0.3 migrate to Other.
- Options section Harness files: Automatic (Claude Code and Codex) or Manual, a file list prefilled from the program's documented locations, and a version label. The options screen lists the files the reporter selected for the last run.
- Webhook GET answers the agent's settings for the reporter.
- Run fields `effort`, `client`, `fingerprint_schema`, `approvals`, `memory`, `run_key` and `harness_manifest`. A repeated `run_key` is recorded once; the action and the webhook answer `duplicate`.
- `tools/report_run.py --setup`: stores the webhook address in `~/.config/ha-harness-tracker.json`, readable by the owner alone (mode 0600, or an owner-only ACL on Windows), trusts a self-signed certificate on request, prints the selected files and registers the Claude Code or Codex hook, backing up the file it edits. `--show-files` prints the selection.
- Automatic harness-file profiles for Claude Code and Codex, and a Codex hook that reads the rollout for tool calls, tokens, model, effort and the approval reviewer's refusals.
- The confirmation screen and the agent's Configure screen show the full webhook address and the setup command. The address can be read when the confirmation screen is not shown.

### Changed

- Fingerprint schema 2: files are keyed by their path inside the project or under the home directory, so two same-named files in different folders are both counted and two clones of a project hash the same. Settings files count by their harness keys (Claude Code's `settings.json` now includes `autoMode`, `enabledPlugins`, `sandbox` and the other behaviour keys; Codex's `config.toml` likewise); unrecognised keys are listed, not hashed. Model and effort lines in agent, skill and command files and MCP secret values no longer move the version. Versions from 0.3 reporters do not carry over.
- Saved approvals and loaded memory are digested beside the version instead of in it.
- The hook handles a payload only when it identifies Claude Code or Codex; another agent running Claude Code's hooks is ignored.
- `tools/claude_code_hook.py` is merged into `tools/report_run.py`, which is both the command and the hook.

### Removed

- The reporter's action route (`HA_URL`, `HA_TOKEN`, `--entry-id`); it posts to the webhook.

## [0.3.0] - 2026-09-27

### Added

- The window gate, for a harness edited faster than one version collects `min_runs` runs: the last `window` runs (option, default 10) against the `window` before them, whatever versions they span. It regresses when the pass rate over shared tasks drops by more than max(tolerance, 150 / window) points or denials per 100 tool calls rise by 1.0 or more (at least 100 calls on each side), holds when the model changed, and raises its own repair issue naming the versions in the window.
- Optional run field `denial_classes`, a map of class to count; a class seen twice in the window is recurring.
- Sensors window success rate, window denials per 100 tool calls and recurring denial classes; webhook reply field `window`; regression event field `kind`; `denials_per_100_calls` in the per-version statistics.

### Changed

- The problem sensor is on while either gate reports a regression.
- `tools/claude_code_hook.py` counts only prompts a person typed, not task notifications, meta entries, compaction summaries or command wrappers. It counts a denial only from an error result that opens with a refusal (output quoting one was counted before), counts the person's declines (missed before), posts each run's denial classes, and prints the window gate at session start.

## [0.2.0] - 2026-09-24

### Changed

- The aggregate half of the gate compares only the task ids both versions ran; rates over different task mixes measured the mix. No shared task means no comparison. Runs without a task id are no longer compared at all.
- The aggregate half stays off when the model most runs reported differs between baseline and current.

### Added

- Optional run fields `model` and `client_version`.
- Webhook reply fields `current_runs`, `confirmed`, `comparable_tasks`, `model_changed`; gate attributes `comparable_tasks`, `model_changed`.
- `tools/claude_code_hook.py`, a Claude Code hook that ledgers every turn from the session transcript (tool calls, writes, pushes, tokens with cache reads included, duration, denials, prompts, harness fingerprint). The agent ends a piece of work with `Verdict: pass|fail|partial verified|unverified [task=<id>]`; the hook holds the run and posts it on the person's next prompt, as reported and unverified, or as confirmed when that prompt is `/pass`, `/fail` or `/partial`. Task ids default to `<directory>:<class>` with the class read from what the span did. Nothing is inferred from the transcript beyond the figures. Self-test with `--selftest`; proven end to end against a live install.

## [0.1.0] - 2026-09-17

### Added

- One config entry per agent, with a webhook id generated at setup and shown once on the confirmation screen.
- `record_run` action and a webhook that take the same run record: harness version, outcome, task id and class, verified flag, turns, tool calls, duration, tokens, cost, denials, retries, interventions, notes.
- Per-harness statistics: success rate, verified rate, median turns and duration, interventions and denials per run, cost.
- The preserve-and-extend gate: a baseline is the best confirmed version or a pinned one; a regression is a confirmed drop beyond the tolerance or a task the baseline solved failing on the current version. Shown as a binary sensor, raised as a repair issue, fired as an event on the transition.
- `set_baseline` action to pin or unpin the baseline.
- Options for the confirmation threshold, the tolerance and the retention; saving reloads the entry and re-runs the gate.
- Diagnostics with the webhook id and the run notes redacted.
- `tools/report_run.py`, a reporter that fingerprints the harness files and posts a run through the action or the webhook.
