"""Session handling for the iHealth KN-550BT blood-pressure monitor.

The KN-550BT measures on its own and stores each reading. It advertises for a
while after a measurement; each time Home Assistant sees it, one BLE session is
run:

    connect + auth -> battery -> device clock -> offline memory -> set clock
    -> disconnect

The device clock is read before the download (firmware > 2.0.0, stream
profile): readings flagged "time needs fixing" are rebased on it, as the app
does.

The memory is only read, never cleared, so the phone app keeps working.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from time import monotonic

from bleak.exc import BleakError

from homeassistant.components import bluetooth
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    EVENT_BP_RECORD,
    KIND_KN550BT,
    KN550BT_MODEL,
    KN550BT_SESSION_COOLDOWN,
    SESSION_COOLDOWN,
)
from .kn550bt.client import Kn550Client, Kn550Disconnected
from .kn550bt.protocol import BpRecord, Kn550Error, fix_timestamp

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
POLL_INTERVAL = timedelta(seconds=10)
MAX_SEEN = 400  # remembered record keys (the monitor holds fewer)


@dataclass(frozen=True, slots=True)
class BpData:
    """State exposed to the entities."""

    connected: bool = False
    battery: int | None = None
    systolic: int | None = None
    diastolic: int | None = None
    pulse_rate: int | None = None
    irregular_heartbeat: bool | None = None
    body_movement: bool | None = None
    measured_at: datetime | None = None
    stored_readings: int | None = None


def _key(r: BpRecord) -> str:
    """Identity of a stored reading: its raw (uncorrected) device timestamp.

    The raw value never changes while the reading sits in memory, whereas the
    corrected one depends on the clock offset at download time.
    """
    return f"{r.timestamp.isoformat()}|{r.systolic}|{r.diastolic}|{r.pulse_rate}"


class Kn550Coordinator(DataUpdateCoordinator[BpData]):
    """Download the KN-550BT memory each time the monitor shows up."""

    kind = KIND_KN550BT
    model = KN550BT_MODEL

    def __init__(self, hass: HomeAssistant, entry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.data[CONF_ADDRESS]}",
        )
        self.address: str = entry.data[CONF_ADDRESS]
        self.data = BpData()
        self._store: Store[dict] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self._seen: list[str] = []
        self._session_task: asyncio.Task | None = None
        self._next_session = 0.0
        self._last_adv_time = 0.0
        self.auto_connect = True
        self._current_client: Kn550Client | None = None
        self._firmware_done = False

    # ------------------------------------------------------------ lifecycle
    async def async_setup(self) -> None:
        stored = await self._store.async_load() or {}
        self._seen = list(stored.get("seen", []))
        if last := stored.get("last_result"):
            self.data = replace(
                self.data,
                battery=last.get("battery"),
                systolic=last.get("systolic"),
                diastolic=last.get("diastolic"),
                pulse_rate=last.get("pulse_rate"),
                irregular_heartbeat=last.get("irregular_heartbeat"),
                body_movement=last.get("body_movement"),
                stored_readings=last.get("stored_readings"),
                measured_at=dt_util.parse_datetime(last["measured_at"])
                if last.get("measured_at")
                else None,
            )

    async def _async_save(self) -> None:
        d = self.data
        await self._store.async_save(
            {
                "seen": self._seen[-MAX_SEEN:],
                "last_result": {
                    "battery": d.battery,
                    "systolic": d.systolic,
                    "diastolic": d.diastolic,
                    "pulse_rate": d.pulse_rate,
                    "irregular_heartbeat": d.irregular_heartbeat,
                    "body_movement": d.body_movement,
                    "stored_readings": d.stored_readings,
                    "measured_at": d.measured_at.isoformat() if d.measured_at else None,
                },
            }
        )

    @callback
    def async_start(self) -> CALLBACK_TYPE:
        """Listen for advertisements (plus a periodic check, see Po3mCoordinator)."""
        unsub_adv = bluetooth.async_register_callback(
            self.hass,
            self._async_on_advertisement,
            bluetooth.BluetoothCallbackMatcher(address=self.address, connectable=True),
            bluetooth.BluetoothScanningMode.ACTIVE,
        )
        unsub_poll = async_track_time_interval(
            self.hass, self._async_poll_advertisement, POLL_INTERVAL
        )

        @callback
        def _unsub() -> None:
            unsub_adv()
            unsub_poll()

        return _unsub

    @callback
    def _async_poll_advertisement(self, _now: datetime | None) -> None:
        if not self.auto_connect:
            return
        info = bluetooth.async_last_service_info(self.hass, self.address, connectable=True)
        if info is not None and getattr(info, "time", 0.0) > self._last_adv_time:
            self._async_on_advertisement(info, None)

    @callback
    def async_set_auto_connect(self, enabled: bool) -> None:
        """Switch entity: when off, leave the monitor to the phone app."""
        self.auto_connect = enabled
        if not enabled and self._current_client is not None:
            client = self._current_client
            self.config_entry.async_create_background_task(
                self.hass, client.disconnect(), f"{DOMAIN}_stop_{self.address}"
            )
        elif enabled:
            self._next_session = 0.0
            self._async_poll_advertisement(None)

    async def _async_update_data(self) -> BpData:
        return self.data

    def _set(self, **changes) -> None:
        self.async_set_updated_data(replace(self.data, **changes))

    # ------------------------------------------------------------ BLE session
    @callback
    def _async_on_advertisement(
        self,
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange | None,
    ) -> None:
        if not self.auto_connect:
            return
        if self._session_task and not self._session_task.done():
            return
        if monotonic() < self._next_session:
            return
        self._last_adv_time = getattr(service_info, "time", monotonic())
        self._session_task = self.config_entry.async_create_background_task(
            self.hass,
            self._async_session(service_info.device),
            f"{DOMAIN}_session_{self.address}",
        )

    async def _async_session(self, ble_device) -> None:
        client = Kn550Client(ble_device)
        self._current_client = client
        synced = False
        try:
            await client.connect()
            self._set(connected=True)
            if not self._firmware_done and client.firmware:
                self._update_device_registry(client.firmware)
                self._firmware_done = True

            battery = await self._step(client.get_battery)
            if battery is not None:
                self._set(battery=battery)
            # read the clock before set_time() overwrites it
            device_now = await self._step(client.get_time)
            real_now = dt_util.now().replace(tzinfo=None)
            history = await self._step(client.get_history)
            if history is not None:
                self._set(stored_readings=len(history))
                self._import_history(history, device_now, real_now)
                synced = True
            # the app sends this after the download: it also sets the clock
            await self._step(client.set_time)
        except Kn550Disconnected:
            _LOGGER.debug("%s: device left during the session", self.address)
        except (Kn550Error, BleakError, TimeoutError) as err:
            _LOGGER.debug("%s: session failed: %s", self.address, err)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("%s: unexpected error during the session", self.address)
        finally:
            self._next_session = monotonic() + (
                KN550BT_SESSION_COOLDOWN if synced else SESSION_COOLDOWN
            )
            self._set(connected=False)
            await self._async_save()
            await client.disconnect()
            self._current_client = None

    async def _step(self, func):
        """Run a non-critical command; keep going on failure (except disconnect)."""
        try:
            return await func()
        except Kn550Disconnected:
            raise
        except (Kn550Error, TimeoutError) as err:
            _LOGGER.debug("%s: %s failed: %s", self.address, func.__name__, err)
            return None

    # ------------------------------------------------------------ memory
    def _import_history(
        self,
        records: list[BpRecord],
        device_now: datetime | None,
        real_now: datetime,
    ) -> None:
        tz = dt_util.get_default_time_zone()
        seen = set(self._seen)
        new: list[tuple[datetime, BpRecord]] = []
        for rec in records:
            if rec.timestamp is None or _key(rec) in seen:
                continue
            if rec.needs_time_fix and device_now is None:
                _LOGGER.debug(
                    "%s: reading %s needs a time fix but the device clock is unknown",
                    self.address, rec.timestamp,
                )
            ts = fix_timestamp(rec, device_now, real_now).replace(tzinfo=tz)
            new.append((ts, rec))
        new.sort(key=lambda item: item[0])
        if not new:
            return
        for ts, rec in new:
            self._seen.append(_key(rec))
            self.hass.bus.async_fire(
                EVENT_BP_RECORD,
                {
                    "address": self.address,
                    "measured_at": ts.isoformat(),
                    "systolic": rec.systolic,
                    "diastolic": rec.diastolic,
                    "pulse_rate": rec.pulse_rate,
                    "irregular_heartbeat": rec.arrhythmia,
                    "body_movement": rec.body_movement,
                    "time_fixed": rec.needs_time_fix and device_now is not None,
                },
            )
        del self._seen[:-MAX_SEEN]
        last_ts, last = new[-1]
        _LOGGER.debug("%s: %d new reading(s), latest %s", self.address, len(new), last)
        if self.data.measured_at is None or last_ts >= self.data.measured_at:
            self._set(
                systolic=last.systolic,
                diastolic=last.diastolic,
                pulse_rate=last.pulse_rate,
                irregular_heartbeat=last.arrhythmia,
                body_movement=last.body_movement,
                measured_at=last_ts,
            )

    def _update_device_registry(self, firmware: str) -> None:
        registry = dr.async_get(self.hass)
        device = registry.async_get_device(
            connections={(dr.CONNECTION_BLUETOOTH, self.address)}
        )
        if device:
            registry.async_update_device(device.id, sw_version=firmware)
