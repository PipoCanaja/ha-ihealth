"""Sensors for the iHealth PO3M."""
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
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import IHealthConfigEntry, Po3mData
from .entity import IHealthEntity

BPM = "bpm"


@dataclass(frozen=True, kw_only=True)
class IHealthSensorDescription(SensorEntityDescription):
    value_fn: Callable[[Po3mData], Any]


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


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IHealthConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(IHealthSensor(coordinator, desc) for desc in SENSORS)


class IHealthSensor(IHealthEntity, SensorEntity):
    entity_description: IHealthSensorDescription

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data)
