"""Sensors for the iHealth PO3M, KN-550BT and AM4."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfEnergy,
    UnitOfPressure,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .am4.protocol import MODES
from .const import KIND_AM4, KIND_KN550BT
from .coordinator import IHealthConfigEntry
from .entity import IHealthEntity

BPM = "bpm"


@dataclass(frozen=True, kw_only=True)
class IHealthSensorDescription(SensorEntityDescription):
    value_fn: Callable[[Any], Any]


SENSORS: tuple[IHealthSensorDescription, ...] = (
    IHealthSensorDescription(
        key="spo2",
        translation_key="spo2",
        icon="mdi:water-percent",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.spo2,
    ),
    IHealthSensorDescription(
        key="pulse_rate",
        translation_key="pulse_rate",
        icon="mdi:heart-pulse",
        native_unit_of_measurement=BPM,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.pulse_rate,
    ),
    IHealthSensorDescription(
        key="perfusion_index",
        translation_key="perfusion_index",
        icon="mdi:chart-bell-curve",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda d: d.perfusion_index,
    ),
    IHealthSensorDescription(
        key="measured_at",
        translation_key="measured_at",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda d: d.measured_at,
    ),
    IHealthSensorDescription(
        key="battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.battery,
    ),
    # Live stream: about one state per second during a measurement -> disabled by default
    IHealthSensorDescription(
        key="live_spo2",
        translation_key="live_spo2",
        icon="mdi:water-percent",
        native_unit_of_measurement=PERCENTAGE,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.live_spo2 if d.measuring else None,
    ),
    IHealthSensorDescription(
        key="live_pulse_rate",
        translation_key="live_pulse_rate",
        icon="mdi:heart-pulse",
        native_unit_of_measurement=BPM,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.live_pulse_rate if d.measuring else None,
    ),
    IHealthSensorDescription(
        key="live_perfusion_index",
        translation_key="live_perfusion_index",
        icon="mdi:chart-bell-curve",
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=1,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.live_perfusion_index if d.measuring else None,
    ),
    IHealthSensorDescription(
        key="live_pulse_strength",
        translation_key="live_pulse_strength",
        icon="mdi:sine-wave",
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.live_pulse_strength if d.measuring else None,
    ),
)


BP_SENSORS: tuple[IHealthSensorDescription, ...] = (
    IHealthSensorDescription(
        key="systolic",
        translation_key="systolic",
        icon="mdi:heart-box",
        native_unit_of_measurement=UnitOfPressure.MMHG,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.systolic,
    ),
    IHealthSensorDescription(
        key="diastolic",
        translation_key="diastolic",
        icon="mdi:heart-box-outline",
        native_unit_of_measurement=UnitOfPressure.MMHG,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.diastolic,
    ),
    IHealthSensorDescription(
        key="pulse_rate",
        translation_key="pulse_rate",
        icon="mdi:heart-pulse",
        native_unit_of_measurement=BPM,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.pulse_rate,
    ),
    IHealthSensorDescription(
        key="measured_at",
        translation_key="measured_at",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda d: d.measured_at,
    ),
    IHealthSensorDescription(
        key="battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.battery,
    ),
    IHealthSensorDescription(
        key="stored_readings",
        translation_key="stored_readings",
        icon="mdi:memory",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.stored_readings,
    ),
)


AM4_SENSORS: tuple[IHealthSensorDescription, ...] = (
    IHealthSensorDescription(
        key="steps",
        translation_key="steps",
        icon="mdi:shoe-print",
        native_unit_of_measurement="steps",
        state_class=SensorStateClass.TOTAL_INCREASING,  # reset by the tracker at midnight
        value_fn=lambda d: d.steps,
    ),
    IHealthSensorDescription(
        key="calories",
        translation_key="calories",
        icon="mdi:fire",
        native_unit_of_measurement=UnitOfEnergy.KILO_CALORIE,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda d: d.calories,
    ),
    IHealthSensorDescription(
        key="synced_at",
        translation_key="synced_at",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.synced_at,
    ),
    IHealthSensorDescription(
        key="battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.battery,
    ),
    IHealthSensorDescription(
        key="mode",
        translation_key="mode",
        device_class=SensorDeviceClass.ENUM,
        options=list(MODES.values()),
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.mode,
    ),
    # Filled only when the history download is switched on
    IHealthSensorDescription(
        key="sleep_minutes",
        translation_key="sleep_minutes",
        icon="mdi:sleep",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        value_fn=lambda d: d.sleep_minutes,
    ),
    IHealthSensorDescription(
        key="deep_sleep_minutes",
        translation_key="deep_sleep_minutes",
        icon="mdi:power-sleep",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        value_fn=lambda d: d.deep_sleep_minutes,
    ),
    IHealthSensorDescription(
        key="light_sleep_minutes",
        translation_key="light_sleep_minutes",
        icon="mdi:weather-night",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        value_fn=lambda d: d.light_sleep_minutes,
    ),
    IHealthSensorDescription(
        key="awake_minutes",
        translation_key="awake_minutes",
        icon="mdi:eye-outline",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        value_fn=lambda d: d.awake_minutes,
    ),
    IHealthSensorDescription(
        key="sleep_start",
        translation_key="sleep_start",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda d: d.sleep_start,
    ),
    IHealthSensorDescription(
        key="sleep_end",
        translation_key="sleep_end",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda d: d.sleep_end,
    ),
)

DESCRIPTIONS = {KIND_KN550BT: BP_SENSORS, KIND_AM4: AM4_SENSORS}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IHealthConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    descriptions = DESCRIPTIONS.get(coordinator.kind, SENSORS)
    async_add_entities(IHealthSensor(coordinator, desc) for desc in descriptions)


class IHealthSensor(IHealthEntity, SensorEntity):
    entity_description: IHealthSensorDescription

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data)
