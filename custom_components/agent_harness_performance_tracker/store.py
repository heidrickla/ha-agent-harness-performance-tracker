"""Persistent run log, one per agent.

Kept through Home Assistant's `Store` so it lands in `.storage`, survives a
restart and rides along in a backup. Bounded: the newest `retention` runs are
kept and older ones dropped, because the value is in the per-harness figures
and the recorder already holds every sensor value those produced. Beside the
runs: the pinned baseline, the reporter's last file selection, and the recent
run keys that make a repeated post a no-op.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN, MAX_RUN_KEYS

STORAGE_VERSION = 1


class RunStore:
    """Load and save one agent's runs and its pinned baseline."""

    def __init__(self, hass: HomeAssistant, entry_id: str, retention: int) -> None:
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}"
        )
        self._runs: list[dict[str, Any]] = []
        self._pinned: str | None = None
        self._selection: dict[str, Any] | None = None
        self._run_keys: list[str] = []
        self._retention = retention
        self._loaded = False

    async def async_load(self) -> None:
        data = await self._store.async_load()
        if data:
            self._runs = list(data.get("runs", []))
            self._pinned = data.get("pinned") or None
            self._selection = data.get("selection") or None
            self._run_keys = [str(k) for k in data.get("run_keys", [])]
        self._loaded = True

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def runs(self) -> list[dict[str, Any]]:
        return self._runs

    @property
    def pinned(self) -> str | None:
        return self._pinned

    @property
    def selection(self) -> dict[str, Any] | None:
        """The harness files the reporter selected for the last run."""
        return self._selection

    def seen(self, run_key: str) -> bool:
        return run_key in self._run_keys

    def _trim(self) -> None:
        if len(self._runs) > self._retention:
            del self._runs[: len(self._runs) - self._retention]
        if len(self._run_keys) > MAX_RUN_KEYS:
            del self._run_keys[: len(self._run_keys) - MAX_RUN_KEYS]

    async def async_add_run(
        self,
        run: dict[str, Any],
        run_key: str | None = None,
        selection: dict[str, Any] | None = None,
    ) -> None:
        self._runs.append(run)
        if run_key:
            self._run_keys.append(run_key)
        if selection is not None:
            self._selection = selection
        self._trim()
        await self._async_save()

    async def async_set_pinned(self, version: str | None) -> None:
        self._pinned = version or None
        await self._async_save()

    async def _async_save(self) -> None:
        await self._store.async_save(
            {
                "runs": self._runs,
                "pinned": self._pinned,
                "selection": self._selection,
                "run_keys": self._run_keys,
            }
        )

    async def async_remove(self) -> None:
        """Delete the file. Called when the entry is removed."""
        await self._store.async_remove()
