"""Shared entity base: every entity sits on the agent's device."""

from __future__ import annotations

from typing import override

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, VERSION
from .coordinator import TrackerCoordinator


class TrackerEntity(CoordinatorEntity[TrackerCoordinator]):
    """Base for every entity of one agent."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: TrackerCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry_id)},
            name=coordinator.agent,
            manufacturer=MANUFACTURER,
            model="Agent harness tracker",
            sw_version=VERSION,
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    @override
    def available(self) -> bool:
        """The store is local; the only way to be unavailable is a failed load."""
        return bool(super().available) and self.coordinator.data is not None
