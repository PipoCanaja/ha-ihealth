"""iHealth Bluetooth devices (PO3 / PO3M pulse oximeter, KN-550BT blood-pressure monitor)."""
from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .bp_coordinator import Kn550Coordinator
from .const import CONF_KIND, KIND_KN550BT, KIND_PO3
from .coordinator import IHealthConfigEntry, Po3mCoordinator

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR, Platform.SWITCH]

COORDINATORS = {KIND_PO3: Po3mCoordinator, KIND_KN550BT: Kn550Coordinator}


async def async_setup_entry(hass: HomeAssistant, entry: IHealthConfigEntry) -> bool:
    # entries created before the KN-550BT support have no kind: oximeter
    coordinator = COORDINATORS[entry.data.get(CONF_KIND, KIND_PO3)](hass, entry)
    await coordinator.async_setup()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(coordinator.async_start())
    return True


async def async_unload_entry(hass: HomeAssistant, entry: IHealthConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
