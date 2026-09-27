"""The gates as an entity: on while either gate reports a regression."""

from __future__ import annotations

from typing import Any, override

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import TrackerConfigEntry, TrackerCoordinator
from .entity import TrackerEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TrackerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([RegressedSensor(entry.runtime_data.coordinator)])


class RegressedSensor(TrackerEntity, BinarySensorEntity):
    """Problem while preserve-and-extend is violated."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_translation_key = "harness_regressed"

    def __init__(self, coordinator: TrackerCoordinator) -> None:
        super().__init__(coordinator, "harness_regressed")

    @property
    @override
    def is_on(self) -> bool:
        snap = self.coordinator.data
        return snap.regressed or snap.window.regressed

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        snap = self.coordinator.data
        return {
            "current_version": snap.current.version if snap.current else None,
            "baseline_version": snap.baseline.version if snap.baseline else None,
            "improvement": snap.improvement,
            "comparable_tasks": snap.comparable_tasks,
            "model_changed": snap.model_changed,
            "regressed_tasks": list(snap.regressed_tasks),
            "current_confirmed": snap.confirmed,
            "version_regressed": snap.regressed,
            "window_regressed": snap.window.regressed,
            "window_improvement": snap.window.improvement,
        }
