"""AM4: button that empties the tracker after a complete download."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import KIND_AM4
from .coordinator import IHealthConfigEntry
from .entity import IHealthEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IHealthConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    if coordinator.kind == KIND_AM4:
        async_add_entities([ResetAfterDownloadButton(coordinator)])


class ResetAfterDownloadButton(IHealthEntity, ButtonEntity):
    """At the next session: download the whole history, then reset the tracker
    (A1) and write its user id, profile, hour mode, reminder and alarms back,
    so the phone app still takes it as paired. Disabled by default.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator) -> None:
        super().__init__(
            coordinator,
            EntityDescription(key="reset_after_download", translation_key="reset_after_download"),
        )

    async def async_press(self) -> None:
        self.coordinator.async_request_reset()

    @property
    def available(self) -> bool:
        return True
