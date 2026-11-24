"""Switches: automatic connection, and (AM4) history download."""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import KIND_AM4
from .coordinator import IHealthConfigEntry
from .entity import IHealthEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IHealthConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[SwitchEntity] = [AutoConnectSwitch(coordinator)]
    if coordinator.kind == KIND_AM4:
        entities.append(HistorySyncSwitch(coordinator))
    async_add_entities(entities)


class AutoConnectSwitch(IHealthEntity, SwitchEntity, RestoreEntity):
    """When off, Home Assistant leaves the device alone (free for the phone app)."""

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


class HistorySyncSwitch(IHealthEntity, SwitchEntity, RestoreEntity):
    """AM4: also download activity, sleep and stage reports (off by default).

    The tracker keeps what it has sent (the phone app still gets it); each
    download is complete again, so it runs at most every AM4_HISTORY_INTERVAL.
    """

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator) -> None:
        from homeassistant.helpers.entity import EntityDescription

        super().__init__(
            coordinator,
            EntityDescription(key="history_sync", translation_key="history_sync"),
        )
        self._attr_is_on = False

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            self._attr_is_on = last.state == "on"
        self.coordinator.async_set_history_sync(self._attr_is_on)

    async def async_turn_on(self, **kwargs) -> None:
        self._attr_is_on = True
        self.coordinator.async_set_history_sync(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self._attr_is_on = False
        self.coordinator.async_set_history_sync(False)
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return True
