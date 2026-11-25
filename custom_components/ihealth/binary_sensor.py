"""Binary sensors for the iHealth PO3M, KN-550BT and AM4."""
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

from .const import KIND_AM4, KIND_KN550BT
from .coordinator import IHealthConfigEntry
from .entity import IHealthEntity


@dataclass(frozen=True, kw_only=True)
class IHealthBinarySensorDescription(BinarySensorEntityDescription):
    value_fn: Callable[[Any], bool | None]
    attrs_fn: Callable[[Any], dict | None] | None = None


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


AM4_BINARY_SENSORS: tuple[IHealthBinarySensorDescription, ...] = (
    IHealthBinarySensorDescription(
        key="connected",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.connected,
    ),
    IHealthBinarySensorDescription(
        key="advertising",
        translation_key="advertising",
        icon="mdi:bluetooth-audio",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.advertising,
        attrs_fn=lambda d: {
            "last_advertisement": d.advertising_changed.isoformat()
            if d.advertising_changed else None
        } if d.advertising is False else None,
    ),
)

DESCRIPTIONS = {KIND_KN550BT: BP_BINARY_SENSORS, KIND_AM4: AM4_BINARY_SENSORS}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IHealthConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    descriptions = DESCRIPTIONS.get(coordinator.kind, BINARY_SENSORS)
    async_add_entities(IHealthBinarySensor(coordinator, d) for d in descriptions)


class IHealthBinarySensor(IHealthEntity, BinarySensorEntity):
    entity_description: IHealthBinarySensorDescription

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict | None:
        fn = self.entity_description.attrs_fn
        return fn(self.coordinator.data) if fn else None
