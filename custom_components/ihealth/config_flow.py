"""Config flow for the iHealth integration."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ADDRESS

from .const import DOMAIN, PO3M_LOCAL_NAME, PO3M_MODEL, PO3M_SERVICE_UUID


def _is_po3m(info: BluetoothServiceInfoBleak) -> bool:
    return PO3M_SERVICE_UUID in info.service_uuids or PO3M_LOCAL_NAME in (info.name or "")


def _title(info: BluetoothServiceInfoBleak) -> str:
    return f"iHealth {PO3M_MODEL} {info.address[-5:].replace(':', '')}"


class IHealthConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for iHealth."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovery: BluetoothServiceInfoBleak | None = None
        self._discovered: dict[str, BluetoothServiceInfoBleak] = {}

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        if not _is_po3m(discovery_info):
            return self.async_abort(reason="not_supported")
        self._discovery = discovery_info
        self.context["title_placeholders"] = {"name": _title(discovery_info)}
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._discovery is not None
        title = _title(self._discovery)
        if user_input is not None:
            return self.async_create_entry(
                title=title, data={CONF_ADDRESS: self._discovery.address}
            )
        self._set_confirm_only()
        return self.async_show_form(
            step_id="bluetooth_confirm", description_placeholders={"name": title}
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=_title(self._discovered[address]), data={CONF_ADDRESS: address}
            )

        configured = self._async_current_ids(include_ignore=False)
        for info in async_discovered_service_info(self.hass, connectable=True):
            if info.address not in configured and _is_po3m(info):
                self._discovered[info.address] = info
        if not self._discovered:
            return self.async_abort(reason="no_devices_found")

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): vol.In(
                        {a: f"{i.name or PO3M_MODEL} ({a})" for a, i in self._discovered.items()}
                    )
                }
            ),
        )
