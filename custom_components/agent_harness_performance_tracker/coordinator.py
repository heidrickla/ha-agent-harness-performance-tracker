"""Turns the run log into the snapshot the entities show, and runs the gate.

Push-driven: nothing is polled. A run arrives through the action or the
webhook, the store appends it, and the coordinator recomputes the snapshot
and hands it to the entities. The gate's side effects - the repair issue and
the regression event - live here so the action and the webhook share them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, override

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    CONF_AGENT,
    CONF_MIN_RUNS,
    CONF_TOLERANCE,
    CONF_WINDOW,
    DEFAULT_MIN_RUNS,
    DEFAULT_TOLERANCE,
    DEFAULT_WINDOW,
    DOMAIN,
    EVENT_REGRESSION,
    EVENT_RUN_RECORDED,
    FIELD_HARNESS,
    FIELD_MANIFEST,
    FIELD_RECORDED_AT,
    FIELD_RUN_KEY,
    ISSUE_REGRESSED,
    ISSUE_WINDOW_REGRESSED,
)
from .metrics import Snapshot, snapshot
from .store import RunStore

_LOGGER = logging.getLogger(__name__)


@dataclass
class TrackerData:
    """What one config entry owns at runtime, on `entry.runtime_data`."""

    coordinator: TrackerCoordinator
    store: RunStore


type TrackerConfigEntry = ConfigEntry[TrackerData]


class TrackerCoordinator(DataUpdateCoordinator[Snapshot]):
    """One per agent. `data` is the current Snapshot."""

    config_entry: TrackerConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: TrackerConfigEntry, store: RunStore
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.data[CONF_AGENT]}",
            update_interval=None,
        )
        self._store = store
        self._issue_open = False

    @property
    def agent(self) -> str:
        return str(self.config_entry.data[CONF_AGENT])

    @property
    def entry_id(self) -> str:
        return self.config_entry.entry_id

    def _compute(self) -> Snapshot:
        options = self.config_entry.options
        return snapshot(
            self._store.runs,
            self._store.pinned,
            int(options.get(CONF_MIN_RUNS, DEFAULT_MIN_RUNS)),
            float(options.get(CONF_TOLERANCE, DEFAULT_TOLERANCE)),
            int(options.get(CONF_WINDOW, DEFAULT_WINDOW)),
        )

    @override
    async def _async_update_data(self) -> Snapshot:
        return self._compute()

    def sync_issue(self) -> None:
        """Make the repair issue match the gate after a load or reload.

        Issues persist across restarts; the thresholds may not have. This
        never fires the regression event, which marks a transition, not a state.
        """
        self._sync_issue(self.data, newly=False)
        self._sync_window_issue(self.data, newly=False)

    def is_duplicate(self, run: dict[str, Any]) -> bool:
        """True when a run with this run_key was recorded before."""
        key = run.get(FIELD_RUN_KEY)
        return bool(key) and self._store.seen(str(key))

    async def async_record(self, run: dict[str, Any]) -> Snapshot:
        """Append a run, recompute, fire the events, keep the repair issues honest.

        The file manifest is kept per entry, as the last selection, not per run.
        Callers check is_duplicate first.
        """
        manifest = run.get(FIELD_MANIFEST)
        run = {
            **{k: v for k, v in run.items() if k != FIELD_MANIFEST},
            FIELD_RECORDED_AT: dt_util.utcnow().isoformat(),
        }
        selection = None
        if isinstance(manifest, dict):
            selection = {
                **manifest,
                FIELD_RECORDED_AT: run[FIELD_RECORDED_AT],
                FIELD_HARNESS: run[FIELD_HARNESS],
            }
        was_regressed = bool(self.data and self.data.regressed)
        was_window = bool(self.data and self.data.window.regressed)
        key = run.get(FIELD_RUN_KEY)
        await self._store.async_add_run(
            run, str(key) if key else None, selection=selection
        )
        snap = self._compute()
        self.async_set_updated_data(snap)
        self.hass.bus.async_fire(
            EVENT_RUN_RECORDED,
            {"entry_id": self.entry_id, "agent": self.agent, **run},
        )
        self._sync_issue(snap, newly=snap.regressed and not was_regressed)
        self._sync_window_issue(snap, newly=snap.window.regressed and not was_window)
        return snap

    def missing(self, run_keys: set[str], times: set[str]) -> list[str]:
        """The named run keys and recording times that match no stored run."""
        found = self._store.matching(run_keys, times)
        have = {str(r.get(FIELD_RUN_KEY) or "") for r in found} | {
            str(r.get(FIELD_RECORDED_AT) or "") for r in found
        }
        return sorted((run_keys | times) - have)

    async def async_remove(
        self, run_keys: set[str], times: set[str]
    ) -> tuple[list[dict[str, Any]], Snapshot]:
        """Remove the named runs and recompute. Repair issues follow the new
        figures; no regression event fires, since nothing new was measured."""
        removed = await self._store.async_remove_runs(run_keys, times)
        snap = self._compute()
        self.async_set_updated_data(snap)
        self._sync_issue(snap, newly=False)
        self._sync_window_issue(snap, newly=False)
        return removed, snap

    async def async_pin(self, version: str | None) -> Snapshot:
        await self._store.async_set_pinned(version)
        snap = self._compute()
        self.async_set_updated_data(snap)
        self._sync_issue(snap, newly=False)
        return snap

    def _sync_window_issue(self, snap: Snapshot, newly: bool) -> None:
        issue_id = f"{ISSUE_WINDOW_REGRESSED}_{self.entry_id}"
        w = snap.window
        if not (w.regressed and w.recent and w.prior):
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return
        versions = [v for v, _ in w.versions]
        if newly:
            self.hass.bus.async_fire(
                EVENT_REGRESSION,
                {
                    "entry_id": self.entry_id,
                    "agent": self.agent,
                    "kind": "window",
                    "window": w.size,
                    "improvement": w.improvement,
                    "denials_per_100_calls": w.recent.denials_per_100_calls,
                    "prior_denials_per_100_calls": w.prior.denials_per_100_calls,
                    "versions": versions,
                    "recurring_denials": [name for name, _ in w.recurring],
                },
            )
        shown = ", ".join(versions[-5:])
        if len(versions) > 5:
            shown = f"{len(versions) - 5} earlier and {shown}"
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_WINDOW_REGRESSED,
            translation_placeholders={
                "agent": self.agent,
                "size": str(w.size),
                "rate": str(w.recent.pass_rate),
                "prior_rate": str(w.prior.pass_rate),
                "denials": str(w.recent.denials_per_100_calls),
                "prior_denials": str(w.prior.denials_per_100_calls),
                "versions": shown,
                "recurring": ", ".join(f"{n} x{c}" for n, c in w.recurring[:3])
                or "none",
            },
        )

    def _sync_issue(self, snap: Snapshot, newly: bool) -> None:
        issue_id = f"{ISSUE_REGRESSED}_{self.entry_id}"
        if snap.regressed and snap.current and snap.baseline:
            if newly:
                self.hass.bus.async_fire(
                    EVENT_REGRESSION,
                    {
                        "entry_id": self.entry_id,
                        "agent": self.agent,
                        "kind": "version",
                        "harness_version": snap.current.version,
                        "baseline_version": snap.baseline.version,
                        "improvement": snap.improvement,
                        "regressed_tasks": snap.regressed_tasks,
                    },
                )
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_REGRESSED,
                translation_placeholders={
                    "agent": self.agent,
                    "current": snap.current.version,
                    "baseline": snap.baseline.version,
                    "current_rate": str(snap.current.pass_rate),
                    "baseline_rate": str(snap.baseline.pass_rate),
                    "tasks": str(len(snap.regressed_tasks)),
                },
            )
            self._issue_open = True
        elif self._issue_open or not snap.regressed:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            self._issue_open = False

    @staticmethod
    def version_of(run: dict[str, Any]) -> str:
        return str(run[FIELD_HARNESS])
