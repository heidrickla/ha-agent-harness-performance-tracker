"""Sensors: what the current harness is doing, and how it compares."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, override

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType

from .const import (
    FIELD_DURATION,
    FIELD_HARNESS,
    FIELD_OUTCOME,
    FIELD_RECORDED_AT,
    FIELD_TASK_CLASS,
    FIELD_TASK_ID,
    FIELD_TURNS,
    FIELD_VERIFIED,
    OUTCOMES,
)
from .coordinator import TrackerConfigEntry, TrackerCoordinator
from .entity import TrackerEntity
from .metrics import Snapshot

# Push-driven: a recorded run updates every entity through the coordinator.
PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class TrackerSensorDescription(SensorEntityDescription):
    value: Callable[[Snapshot], StateType]
    attributes: Callable[[Snapshot], dict[str, Any]] | None = None


def _current(snap: Snapshot, attr: str) -> StateType:
    return getattr(snap.current, attr) if snap.current else None


def _version_attrs(snap: Snapshot) -> dict[str, Any]:
    if not snap.current:
        return {}
    return {
        "runs_on_version": snap.current.runs,
        "first_seen": snap.current.first_seen,
        "confirmed": snap.confirmed,
    }


def _baseline_attrs(snap: Snapshot) -> dict[str, Any]:
    if not snap.baseline:
        return {"pinned": snap.baseline_pinned}
    return {
        "pinned": snap.baseline_pinned,
        "runs": snap.baseline.runs,
        "pass_rate": snap.baseline.pass_rate,
        "last_seen": snap.baseline.last_seen,
    }


def _last_run_attrs(snap: Snapshot) -> dict[str, Any]:
    run = snap.last_run
    if not run:
        return {}
    return {
        "task_id": run.get(FIELD_TASK_ID),
        "task_class": run.get(FIELD_TASK_CLASS),
        FIELD_HARNESS: run.get(FIELD_HARNESS),
        "verified": bool(run.get(FIELD_VERIFIED)),
        "turns": run.get(FIELD_TURNS),
        "duration_s": run.get(FIELD_DURATION),
        "recorded_at": run.get(FIELD_RECORDED_AT),
    }


DESCRIPTIONS: tuple[TrackerSensorDescription, ...] = (
    TrackerSensorDescription(
        key="runs",
        translation_key="runs",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value=lambda s: s.total_runs,
    ),
    TrackerSensorDescription(
        key="harness_version",
        translation_key="harness_version",
        value=lambda s: s.current.version if s.current else None,
        attributes=_version_attrs,
    ),
    TrackerSensorDescription(
        key="success_rate",
        translation_key="success_rate",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value=lambda s: _current(s, "pass_rate"),
    ),
    TrackerSensorDescription(
        key="verified_rate",
        translation_key="verified_rate",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value=lambda s: _current(s, "verified_rate"),
    ),
    TrackerSensorDescription(
        key="median_turns",
        translation_key="median_turns",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value=lambda s: _current(s, "median_turns"),
    ),
    TrackerSensorDescription(
        key="median_duration",
        translation_key="median_duration",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value=lambda s: _current(s, "median_duration"),
    ),
    TrackerSensorDescription(
        key="interventions_per_run",
        translation_key="interventions_per_run",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value=lambda s: _current(s, "interventions_per_run"),
    ),
    TrackerSensorDescription(
        key="denials_per_run",
        translation_key="denials_per_run",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value=lambda s: _current(s, "denials_per_run"),
    ),
    TrackerSensorDescription(
        key="cost",
        translation_key="cost",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="USD",
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        value=lambda s: s.total_cost,
    ),
    TrackerSensorDescription(
        key="baseline_harness_version",
        translation_key="baseline_harness_version",
        value=lambda s: s.baseline.version if s.baseline else None,
        attributes=_baseline_attrs,
    ),
    TrackerSensorDescription(
        key="improvement",
        translation_key="improvement",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value=lambda s: s.improvement,
    ),
    TrackerSensorDescription(
        key="regressions",
        translation_key="regressions",
        state_class=SensorStateClass.MEASUREMENT,
        value=lambda s: len(s.regressed_tasks),
        attributes=lambda s: {"tasks": list(s.regressed_tasks)},
    ),
    TrackerSensorDescription(
        key="last_run",
        translation_key="last_run",
        device_class=SensorDeviceClass.ENUM,
        options=list(OUTCOMES),
        value=lambda s: s.last_run.get(FIELD_OUTCOME) if s.last_run else None,
        attributes=_last_run_attrs,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TrackerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.coordinator
    async_add_entities(TrackerSensor(coordinator, d) for d in DESCRIPTIONS)


class TrackerSensor(TrackerEntity, SensorEntity):
    entity_description: TrackerSensorDescription

    def __init__(
        self, coordinator: TrackerCoordinator, description: TrackerSensorDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    @override
    def native_value(self) -> StateType:
        return self.entity_description.value(self.coordinator.data)

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attributes is None:
            return None
        return self.entity_description.attributes(self.coordinator.data)
