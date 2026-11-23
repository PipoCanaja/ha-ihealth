"""Binary sensors for the iHealth PO3M and KN-550BT."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import KIND_KN550BT
from .coordinator import IHealthConfigEntry
from .entity import IHealthEntity


@dataclass(frozen=True, kw_only=True)
class IHealthBinarySensorDescription(BinarySensorEntityDescription):
    value_fn: Callable[[Any], bool | None]


BINARY_SENSORS: tuple[IHealthBinarySensorDescription, ...] = (
    IHealthBinarySensorDescription(
        key="measuring",
        translation_key="measuring",
        device_class=BinarySensorDeviceClass.RUNNING,
        value_fn=lambda d: d.measuring,
    ),
    IHealthBinarySensorDescription(
        key="connected",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.connected,
    ),
)


BP_BINARY_SENSORS: tuple[IHealthBinarySensorDescription, ...] = (
    IHealthBinarySensorDescription(
        key="irregular_heartbeat",
        translation_key="irregular_heartbeat",
        icon="mdi:heart-flash",
        value_fn=lambda d: d.irregular_heartbeat,
    ),
    IHealthBinarySensorDescription(
        key="body_movement",
        translation_key="body_movement",
        icon="mdi:human-handsup",
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.body_movement,
    ),
    IHealthBinarySensorDescription(
        key="connected",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.connected,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IHealthConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    descriptions = BP_BINARY_SENSORS if coordinator.kind == KIND_KN550BT else BINARY_SENSORS
    async_add_entities(IHealthBinarySensor(coordinator, d) for d in descriptions)


class IHealthBinarySensor(IHealthEntity, BinarySensorEntity):
    entity_description: IHealthBinarySensorDescription

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value_fn(self.coordinator.data)
