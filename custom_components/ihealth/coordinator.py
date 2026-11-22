"""Session handling for the iHealth PO3M.

The PO3M only advertises while it is switched on (finger inserted). Each time
an advertisement is seen, one BLE session is run:

    connect + auth -> battery -> set clock -> device info (once)
    -> live measurement -> offline memory -> disconnect
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import datetime
from time import monotonic

from bleak.exc import BleakError

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    EVENT_HISTORY_RECORD,
    MEASURE_TIMEOUT,
    PO3_CLASSIC_SERVICE_UUID,
    SESSION_COOLDOWN,
)
from .po3classic.client import Po3ClassicClient, Po3Disconnected
from .po3classic.protocol import Po3Error
from .po3m.client import Po3mClient, Po3mDisconnected
from .po3m.protocol import HistoryRecord, LiveData, Po3mError

DISCONNECTED = (Po3mDisconnected, Po3Disconnected)
PROTOCOL_ERRORS = (Po3mError, Po3Error)

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1

type IHealthConfigEntry = ConfigEntry[Po3mCoordinator]


@dataclass(frozen=True, slots=True)
class Po3mData:
    """State exposed to the entities."""

    connected: bool = False
    measuring: bool = False
    battery: int | None = None
    # last result (live measurement or offline memory)
    spo2: int | None = None
    pulse_rate: int | None = None
    perfusion_index: float | None = None
    measured_at: datetime | None = None
    # live stream
    live_spo2: int | None = None
    live_pulse_rate: int | None = None
    live_perfusion_index: float | None = None
    live_pulse_strength: int | None = None


class Po3mCoordinator(DataUpdateCoordinator[Po3mData]):
    """Run a BLE session each time the PO3M shows up."""

    config_entry: IHealthConfigEntry

    def __init__(self, hass: HomeAssistant, entry: IHealthConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.data[CONF_ADDRESS]}",
        )
        self.address: str = entry.data[CONF_ADDRESS]
        self.data = Po3mData()
        self._store: Store[dict] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self._last_history: datetime | None = None
        self._session_task: asyncio.Task | None = None
        self._next_session = 0.0
        self._device_info_done = False

    # ------------------------------------------------------------ lifecycle
    async def async_setup(self) -> None:
        stored = await self._store.async_load() or {}
        if ts := stored.get("last_history"):
            self._last_history = dt_util.parse_datetime(ts)
        if last := stored.get("last_result"):
            self.data = replace(
                self.data,
                battery=last.get("battery"),
                spo2=last.get("spo2"),
                pulse_rate=last.get("pulse_rate"),
                perfusion_index=last.get("perfusion_index"),
                measured_at=dt_util.parse_datetime(last["measured_at"])
                if last.get("measured_at")
                else None,
            )

    async def _async_save(self) -> None:
        d = self.data
        await self._store.async_save(
            {
                "last_history": self._last_history.isoformat()
                if self._last_history
                else None,
                "last_result": {
                    "battery": d.battery,
                    "spo2": d.spo2,
                    "pulse_rate": d.pulse_rate,
                    "perfusion_index": d.perfusion_index,
                    "measured_at": d.measured_at.isoformat() if d.measured_at else None,
                },
            }
        )

    @callback
    def async_start(self) -> CALLBACK_TYPE:
        """Listen for advertisements; returns the unsubscribe callback."""
        return bluetooth.async_register_callback(
            self.hass,
            self._async_on_advertisement,
            bluetooth.BluetoothCallbackMatcher(address=self.address, connectable=True),
            bluetooth.BluetoothScanningMode.ACTIVE,
        )

    async def _async_update_data(self) -> Po3mData:
        # No polling: data is pushed by the BLE session.
        return self.data

    def _set(self, **changes) -> None:
        self.async_set_updated_data(replace(self.data, **changes))

    # ------------------------------------------------------------ BLE session
    @callback
    def _async_on_advertisement(
        self,
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        if self._session_task and not self._session_task.done():
            return
        if monotonic() < self._next_session:
            return
        classic = PO3_CLASSIC_SERVICE_UUID in (service_info.service_uuids or ())
        self._session_task = self.config_entry.async_create_background_task(
            self.hass,
            self._async_session(service_info.device, classic),
            f"{DOMAIN}_session_{self.address}",
        )

    async def _async_session(self, ble_device, classic: bool) -> None:
        client = Po3ClassicClient(ble_device) if classic else Po3mClient(ble_device)
        try:
            await client.connect()
            self._set(connected=True)

            battery = await self._step(client.get_battery)
            if battery is not None:
                # POV12 returns a dict, classic returns an int
                self._set(battery=battery["battery"] if isinstance(battery, dict) else battery)
            await self._step(client.set_time)
            if not classic and not self._device_info_done:
                if info := await self._step(client.get_device_info):
                    self._update_device_registry(info)
                    self._device_info_done = True

            result = await client.measure(on_live=self._on_live, timeout=MEASURE_TIMEOUT)
            self._set(measuring=False)
            if result:
                _LOGGER.debug("%s: result %s", self.address, result)
                self._set(
                    spo2=result.spo2,
                    pulse_rate=result.pulse_rate,
                    perfusion_index=result.perfusion_index,
                    measured_at=dt_util.utcnow(),
                )

            if not classic:
                if (history := await self._step(client.get_history)) is not None:
                    await self._import_history(history)
        except DISCONNECTED:
            _LOGGER.debug("%s: device left during the session", self.address)
        except (*PROTOCOL_ERRORS, BleakError, TimeoutError) as err:
            _LOGGER.debug("%s: session failed: %s", self.address, err)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("%s: unexpected error during the session", self.address)
        finally:
            await client.disconnect()
            self._next_session = monotonic() + SESSION_COOLDOWN
            self._set(connected=False, measuring=False)
            await self._async_save()

    async def _step(self, func):
        """Run a non-critical command; keep going on failure (except disconnect)."""
        try:
            return await func()
        except DISCONNECTED:
            raise
        except PROTOCOL_ERRORS as err:
            _LOGGER.debug("%s: %s failed: %s", self.address, func.__name__, err)
            return None

    @callback
    def _on_live(self, live: LiveData) -> None:
        self._set(
            measuring=True,
            live_spo2=live.spo2,
            live_pulse_rate=live.pulse_rate,
            live_perfusion_index=live.perfusion_index,
            live_pulse_strength=live.pulse_strength,
        )

    # ------------------------------------------------------------ offline memory
    async def _import_history(self, records: list[HistoryRecord]) -> None:
        tz = dt_util.get_default_time_zone()
        new = sorted(
            (
                (r.timestamp.replace(tzinfo=tz), r)
                for r in records
                if r.timestamp is not None
            ),
            key=lambda item: item[0],
        )
        if self._last_history:
            new = [(ts, r) for ts, r in new if ts > self._last_history]
        if not new:
            return
        for ts, rec in new:
            self.hass.bus.async_fire(
                EVENT_HISTORY_RECORD,
                {
                    "address": self.address,
                    "measured_at": ts.isoformat(),
                    "spo2": rec.spo2,
                    "pulse_rate": rec.pulse_rate,
                    "perfusion_index": rec.perfusion_index,
                    "time_calibrated": bool(rec.time_calibrated),
                },
            )
        last_ts, last = new[-1]
        self._last_history = last_ts
        _LOGGER.debug("%s: %d new offline record(s)", self.address, len(new))
        if self.data.measured_at is None or last_ts > self.data.measured_at:
            self._set(
                spo2=last.spo2,
                pulse_rate=last.pulse_rate,
                perfusion_index=last.perfusion_index,
                measured_at=last_ts,
            )

    def _update_device_registry(self, info: dict) -> None:
        registry = dr.async_get(self.hass)
        device = registry.async_get_device(
            connections={(dr.CONNECTION_BLUETOOTH, self.address)}
        )
        if not device:
            return
        registry.async_update_device(
            device.id,
            sw_version=info.get("firmware") or None,
            hw_version=info.get("hardware") or None,
            serial_number=info.get("serial") or None,
            model_id=info.get("model") or None,
        )
