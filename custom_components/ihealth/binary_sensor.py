"""Binary sensors for the iHealth PO3M."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import IHealthConfigEntry, Po3mData
from .entity import IHealthEntity


@dataclass(frozen=True, kw_only=True)
class IHealthBinarySensorDescription(BinarySensorEntityDescription):
    value_fn: Callable[[Po3mData], bool]


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


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IHealthConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(IHealthBinarySensor(coordinator, d) for d in BINARY_SENSORS)


class IHealthBinarySensor(IHealthEntity, BinarySensorEntity):
    entity_description: IHealthBinarySensorDescription

    @property
    def is_on(self) -> bool:
        return self.entity_description.value_fn(self.coordinator.data)
