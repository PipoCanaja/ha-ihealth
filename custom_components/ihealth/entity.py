"""Base entity for the iHealth integration."""
from __future__ import annotations

from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .bp_coordinator import Kn550Coordinator
from .const import MANUFACTURER
from .coordinator import Po3mCoordinator


class IHealthEntity(CoordinatorEntity[Po3mCoordinator | Kn550Coordinator]):
    """Entity tied to one device. Always available: the device sleeps most of the time."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: Po3mCoordinator | Kn550Coordinator, description: EntityDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.address}_{description.key}"
        self._attr_device_info = DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, coordinator.address)},
            manufacturer=MANUFACTURER,
            model=coordinator.model,
            name=coordinator.config_entry.title,
        )

    @property
    def available(self) -> bool:
        return True
