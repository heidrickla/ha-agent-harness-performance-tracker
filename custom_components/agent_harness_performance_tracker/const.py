"""Constants."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "agent_harness_performance_tracker"
NAME: Final = "Agent Harness Performance Tracker"
MANUFACTURER: Final = "Lewis Heidrick"
VERSION: Final = "0.5.0"

CONF_AGENT: Final = "agent"
CONF_AGENT_PROGRAM: Final = "agent_program"
CONF_WEBHOOK_ID: Final = "webhook_id"
# Options: how the reporter picks the harness files, and the version prefix.
CONF_HARNESS: Final = "harness"
CONF_SELECTION: Final = "selection"
CONF_HARNESS_FILES: Final = "harness_files"
CONF_VERSION_LABEL: Final = "version_label"
SELECTION_AUTOMATIC: Final = "automatic"
SELECTION_MANUAL: Final = "manual"
MAX_HARNESS_FILES: Final = 100
MAX_PATH: Final = 300
# The reporter's fingerprint scheme. Versions from different schemes are different
# hashes of different inputs and are never compared.
FINGERPRINT_SCHEMA: Final = 2
CONF_MIN_RUNS: Final = "min_runs"
CONF_TOLERANCE: Final = "tolerance"
CONF_RETENTION: Final = "retention"
CONF_WINDOW: Final = "window"

# A harness version is confirmed once this many runs have been recorded on
# it. Below that, its pass rate is a hypothesis, not a baseline.
DEFAULT_MIN_RUNS: Final = 10
# Points of pass rate the current harness may sit below the baseline before
# the gate reports a regression.
DEFAULT_TOLERANCE: Final = 5.0
# Runs in each half of the window gate: the last this many against the this many before.
DEFAULT_WINDOW: Final = 10
MIN_WINDOW: Final = 3
MAX_WINDOW: Final = 100
# Runs kept per agent. Older runs are dropped oldest first.
DEFAULT_RETENTION: Final = 2000
MAX_RETENTION: Final = 20000

OUTCOME_PASS: Final = "pass"
OUTCOME_FAIL: Final = "fail"
OUTCOME_PARTIAL: Final = "partial"
OUTCOMES: Final = (OUTCOME_PASS, OUTCOME_FAIL, OUTCOME_PARTIAL)

# Run fields as they travel in the action call, the webhook body and the
# store. One vocabulary everywhere, so a reporter written against the action
# posts to the webhook unchanged.
FIELD_HARNESS: Final = "harness_version"
FIELD_TASK_ID: Final = "task_id"
FIELD_TASK_CLASS: Final = "task_class"
FIELD_OUTCOME: Final = "outcome"
FIELD_VERIFIED: Final = "verified"
FIELD_TURNS: Final = "turns"
FIELD_TOOL_CALLS: Final = "tool_calls"
FIELD_DURATION: Final = "duration_s"
FIELD_INPUT_TOKENS: Final = "input_tokens"
FIELD_OUTPUT_TOKENS: Final = "output_tokens"
FIELD_COST: Final = "cost_usd"
FIELD_DENIALS: Final = "denials"
# Why the denials happened, {class: count}: a classifier rule, a hook, the person.
FIELD_DENIAL_CLASSES: Final = "denial_classes"
MAX_DENIAL_CLASSES: Final = 20
MAX_CLASS_NAME: Final = 80
FIELD_RETRIES: Final = "retries"
FIELD_INTERVENTIONS: Final = "interventions"
FIELD_NOTES: Final = "notes"
FIELD_MODEL: Final = "model"
FIELD_EFFORT: Final = "effort"
FIELD_CLIENT: Final = "client"
FIELD_CLIENT_VERSION: Final = "client_version"
FIELD_SCHEMA: Final = "fingerprint_schema"
# Digests of state that changes the agent's behaviour but moves too often to be the
# harness: approvals saved by "always allow" clicks, and the memory loaded at start.
FIELD_APPROVALS: Final = "approvals"
FIELD_MEMORY: Final = "memory"
# Idempotency: a run posted twice with the same key is recorded once.
FIELD_RUN_KEY: Final = "run_key"
MAX_RUN_KEYS: Final = 1000
# The reporter's list of selected files. Kept per entry, not per run.
FIELD_MANIFEST: Final = "harness_manifest"
FIELD_RECORDED_AT: Final = "recorded_at"

MAX_TEXT: Final = 200
MAX_NOTES: Final = 500

SERVICE_RECORD_RUN: Final = "record_run"
SERVICE_SET_BASELINE: Final = "set_baseline"
ATTR_CONFIG_ENTRY_ID: Final = "config_entry_id"

EVENT_RUN_RECORDED: Final = f"{DOMAIN}_run_recorded"
EVENT_REGRESSION: Final = f"{DOMAIN}_regression"

ISSUE_REGRESSED: Final = "harness_regressed"
ISSUE_WINDOW_REGRESSED: Final = "window_regressed"
