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
    DEFAULT_MIN_RUNS,
    DEFAULT_TOLERANCE,
    DOMAIN,
    EVENT_REGRESSION,
    EVENT_RUN_RECORDED,
    FIELD_HARNESS,
    FIELD_RECORDED_AT,
    ISSUE_REGRESSED,
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

    async def async_record(self, run: dict[str, Any]) -> Snapshot:
        """Append a run, recompute, fire the events, keep the repair issue honest."""
        run = {**run, FIELD_RECORDED_AT: dt_util.utcnow().isoformat()}
        was_regressed = bool(self.data and self.data.regressed)
        await self._store.async_add_run(run)
        snap = self._compute()
        self.async_set_updated_data(snap)
        self.hass.bus.async_fire(
            EVENT_RUN_RECORDED,
            {"entry_id": self.entry_id, "agent": self.agent, **run},
        )
        self._sync_issue(snap, newly=snap.regressed and not was_regressed)
        return snap

    async def async_pin(self, version: str | None) -> Snapshot:
        await self._store.async_set_pinned(version)
        snap = self._compute()
        self.async_set_updated_data(snap)
        self._sync_issue(snap, newly=False)
        return snap

    def _sync_issue(self, snap: Snapshot, newly: bool) -> None:
        issue_id = f"{ISSUE_REGRESSED}_{self.entry_id}"
        if snap.regressed and snap.current and snap.baseline:
            if newly:
                self.hass.bus.async_fire(
                    EVENT_REGRESSION,
                    {
                        "entry_id": self.entry_id,
                        "agent": self.agent,
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
