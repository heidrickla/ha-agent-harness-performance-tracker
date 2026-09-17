"""Persistent run log, one per agent.

Kept through Home Assistant's `Store` so it lands in `.storage`, survives a
restart and rides along in a backup. Bounded: the newest `retention` runs are
kept and older ones dropped, because the value is in the per-harness figures
and the recorder already holds every sensor value those produced.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

STORAGE_VERSION = 1


class RunStore:
    """Load and save one agent's runs and its pinned baseline."""

    def __init__(self, hass: HomeAssistant, entry_id: str, retention: int) -> None:
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}"
        )
        self._runs: list[dict[str, Any]] = []
        self._pinned: str | None = None
        self._retention = retention
        self._loaded = False

    async def async_load(self) -> None:
        data = await self._store.async_load()
        if data:
            self._runs = list(data.get("runs", []))
            self._pinned = data.get("pinned") or None
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

    def _trim(self) -> None:
        if len(self._runs) > self._retention:
            del self._runs[: len(self._runs) - self._retention]

    async def async_add_run(self, run: dict[str, Any]) -> None:
        self._runs.append(run)
        self._trim()
        await self._async_save()

    async def async_set_pinned(self, version: str | None) -> None:
        self._pinned = version or None
        await self._async_save()

    async def _async_save(self) -> None:
        await self._store.async_save({"runs": self._runs, "pinned": self._pinned})

    async def async_remove(self) -> None:
        """Delete the file. Called when the entry is removed."""
        await self._store.async_remove()
