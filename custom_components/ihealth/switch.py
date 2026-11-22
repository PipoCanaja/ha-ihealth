"""Switch to enable/disable automatic connection to the oximeter."""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .coordinator import IHealthConfigEntry
from .entity import IHealthEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IHealthConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([AutoConnectSwitch(entry.runtime_data)])


class AutoConnectSwitch(IHealthEntity, SwitchEntity, RestoreEntity):
    """When off, Home Assistant leaves the oximeter alone (free for the phone app)."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator) -> None:
        from homeassistant.helpers.entity import EntityDescription

        super().__init__(
            coordinator,
            EntityDescription(key="auto_connect", translation_key="auto_connect"),
        )
        self._attr_is_on = True

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            self._attr_is_on = last.state == "on"
        self.coordinator.async_set_auto_connect(self._attr_is_on)

    async def async_turn_on(self, **kwargs) -> None:
        self._attr_is_on = True
        self.coordinator.async_set_auto_connect(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self._attr_is_on = False
        self.coordinator.async_set_auto_connect(False)
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return True
