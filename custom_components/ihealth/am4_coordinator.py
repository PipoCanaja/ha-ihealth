"""Session handling for the iHealth AM4 activity tracker.

The AM4 advertises all the time. Each time Home Assistant sees it (at most
every AM4_SESSION_INTERVAL), one BLE session is run:

    connect + auth -> state/battery -> set clock -> today's steps/calories
    [-> stage reports -> activity points -> sleep]   (only if "history" is on)
    -> disconnect

Downloading the history is off by default: the tracker may drop what it has
sent, and the phone app would then never get it.
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

from .am4.client import Am4AuthTimeout, Am4Client, Am4Disconnected
from .am4.protocol import ActivityPoint, Am4Error, SleepSession, StageRecord
from .const import (
    AM4_MODEL,
    AM4_SESSION_INTERVAL,
    DOMAIN,
    EVENT_AM4_ACTIVITY,
    EVENT_AM4_SLEEP,
    EVENT_AM4_STAGE,
    KIND_AM4,
    SESSION_COOLDOWN,
)

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
POLL_INTERVAL = timedelta(seconds=10)
AUTH_RETRY_DELAY = 5  # s: a stuck link usually works again on a new connection
STATUS_LOG_EVERY = 60  # s: debug line telling why no session runs
MAX_SEEN = 400


@dataclass(frozen=True, slots=True)
class Am4Data:
    """State exposed to the entities."""

    connected: bool = False
    battery: int | None = None
    mode: str | None = None
    steps: int | None = None
    calories: int | None = None
    synced_at: datetime | None = None
    sleep_start: datetime | None = None
    sleep_end: datetime | None = None
    sleep_minutes: int | None = None     # light + deep
    deep_sleep_minutes: int | None = None
    light_sleep_minutes: int | None = None
    awake_minutes: int | None = None


_PERSISTED = (
    "battery", "mode", "steps", "calories", "synced_at", "sleep_start", "sleep_end",
    "sleep_minutes", "deep_sleep_minutes", "light_sleep_minutes", "awake_minutes",
)
_DATETIMES = {"synced_at", "sleep_start", "sleep_end"}


class Am4Coordinator(DataUpdateCoordinator[Am4Data]):
    """Read the AM4 regularly while it is in range."""

    kind = KIND_AM4
    model = AM4_MODEL

    def __init__(self, hass: HomeAssistant, entry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.data[CONF_ADDRESS]}",
        )
        self.address: str = entry.data[CONF_ADDRESS]
        self.data = Am4Data()
        self._store: Store[dict] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self._seen: list[str] = []
        self._activity_last: str | None = None   # time of the newest activity point sent
        self._session_task: asyncio.Task | None = None
        self._next_session = 0.0
        self._last_adv_time = 0.0
        self.auto_connect = True
        self.history_sync = False
        self._current_client: Am4Client | None = None
        self._firmware_done = False
        self._last_status_log = 0.0

    # ------------------------------------------------------------ lifecycle
    async def async_setup(self) -> None:
        stored = await self._store.async_load() or {}
        self._seen = list(stored.get("seen", []))
        self._activity_last = stored.get("activity_last")
        last = stored.get("last_result") or {}
        values = {}
        for key in _PERSISTED:
            value = last.get(key)
            if key in _DATETIMES and value:
                value = dt_util.parse_datetime(value)
            values[key] = value
        self.data = replace(self.data, **values)

    async def _async_save(self) -> None:
        last = {}
        for key in _PERSISTED:
            value = getattr(self.data, key)
            last[key] = value.isoformat() if isinstance(value, datetime) else value
        await self._store.async_save(
            {
                "seen": self._seen[-MAX_SEEN:],
                "activity_last": self._activity_last,
                "last_result": last,
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
        info = bluetooth.async_last_service_info(self.hass, self.address, connectable=True)
        self._log_status(info)
        if not self.auto_connect:
            return
        # the tracker advertises the same content all the time: HA does not
        # repeat the callback, so also start a session from the last one seen
        # (only if it is newer than the one that started the previous session)
        if info is not None and getattr(info, "time", 0.0) > self._last_adv_time:
            self._async_on_advertisement(info, None)

    def _log_status(self, info) -> None:
        """Once a minute, say in the debug log why no session is running."""
        now = monotonic()
        if now - self._last_status_log < STATUS_LOG_EVERY:
            return
        self._last_status_log = now
        if self._session_task and not self._session_task.done():
            return  # a session logs on its own
        if info is None:
            adv = "no connectable advertisement known to HA (out of range, "
            adv += "connected to another device, or not advertising)"
        else:
            age = now - getattr(info, "time", now)
            adv = f"last advertisement {age:.0f} s ago, rssi {info.rssi}, via {info.source}"
            if getattr(info, "time", 0.0) <= self._last_adv_time:
                adv += " (already used, waiting for a newer one)"
        wait = max(0.0, self._next_session - now)
        _LOGGER.debug(
            "%s: idle - auto connect %s, history %s, next session allowed in %.0f s, %s",
            self.address, "on" if self.auto_connect else "OFF",
            "on" if self.history_sync else "off", wait, adv,
        )

    @callback
    def async_set_auto_connect(self, enabled: bool) -> None:
        """Switch entity: when off, leave the tracker to the phone app."""
        self.auto_connect = enabled
        if not enabled and self._current_client is not None:
            client = self._current_client
            self.config_entry.async_create_background_task(
                self.hass, client.disconnect(), f"{DOMAIN}_stop_{self.address}"
            )
        elif enabled:
            self._next_session = 0.0
            self._async_poll_advertisement(None)

    @callback
    def async_set_history_sync(self, enabled: bool) -> None:
        """Switch entity: also download activity / sleep / stage reports."""
        was = self.history_sync
        self.history_sync = enabled
        if enabled and not was:
            self._next_session = 0.0
            self._async_poll_advertisement(None)

    async def _async_update_data(self) -> Am4Data:
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
        client = Am4Client(ble_device)
        self._current_client = client
        ok = False
        retry_soon = False
        try:
            await client.connect()
            self._set(connected=True)
            if not self._firmware_done and client.firmware:
                self._update_device_registry(client.firmware)
                self._firmware_done = True

            state = await self._step(client.get_state)
            if state is not None:
                self._set(mode=state.mode, battery=state.battery)
            await self._step(client.sync_time)
            real = await self._step(client.get_realtime)
            if real is not None:
                self._set(steps=real.steps, calories=real.calories, synced_at=dt_util.now())
                ok = True
            if self.history_sync:
                await self._sync_history(client)
        except Am4AuthTimeout as err:
            _LOGGER.debug("%s: %s, reconnecting in %d s", self.address, err, AUTH_RETRY_DELAY)
            retry_soon = True
        except Am4Disconnected:
            _LOGGER.debug("%s: device left during the session", self.address)
        except (Am4Error, BleakError, TimeoutError) as err:
            _LOGGER.debug("%s: session failed: %s", self.address, err)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("%s: unexpected error during the session", self.address)
        finally:
            self._next_session = monotonic() + (
                AM4_SESSION_INTERVAL if ok else AUTH_RETRY_DELAY if retry_soon else SESSION_COOLDOWN
            )
            if retry_soon:
                self._last_adv_time = 0.0  # the next poll may reuse the last advertisement
            self._set(connected=False)
            await self._async_save()
            await client.disconnect()
            self._current_client = None

    async def _step(self, func):
        """Run a non-critical command; keep going on failure (except disconnect)."""
        try:
            return await func()
        except Am4Disconnected:
            raise
        except (Am4Error, TimeoutError) as err:
            _LOGGER.debug("%s: %s failed: %r", self.address, func.__name__, err)
            return None

    # ------------------------------------------------------------ history
    def _local(self, t: datetime | None) -> datetime | None:
        return t.replace(tzinfo=dt_util.get_default_time_zone()) if t else None

    async def _sync_history(self, client: Am4Client) -> None:
        # same order as the app (AM4ViewModel.d())
        stage = await self._step(client.get_stage)
        if stage:
            self._import_stage(stage)
        activity = await self._step(client.get_activity)
        if activity:
            self._import_activity(activity)
        sleep = await self._step(client.get_sleep)
        if sleep:
            self._import_sleep(sleep)
        del self._seen[:-MAX_SEEN]

    def _import_stage(self, records: list[StageRecord]) -> None:
        seen = set(self._seen)
        for rec in records:
            key = f"stage|{rec.type}|{rec.time.isoformat() if rec.time else ''}|{rec.duration}"
            if key in seen:
                continue
            self._seen.append(key)
            t = self._local(rec.time)
            self.hass.bus.async_fire(
                EVENT_AM4_STAGE,
                {
                    "address": self.address,
                    "type": rec.type,
                    "end": t.isoformat() if t else None,
                    "start": (t - timedelta(minutes=rec.duration)).isoformat()
                    if t and rec.duration is not None
                    else None,
                    "duration_minutes": rec.duration,
                    **rec.values,
                },
            )

    def _import_activity(self, points: list[ActivityPoint]) -> None:
        last = datetime.fromisoformat(self._activity_last) if self._activity_last else None
        unique = {p.time: p for p in points}   # a re-sent message must not count twice
        new = sorted((p for p in unique.values() if last is None or p.time > last),
                     key=lambda p: p.time)
        if not new:
            return
        by_day: dict = {}
        for p in new:
            by_day.setdefault(p.time.date(), []).append(p)
        for day, pts in by_day.items():
            self.hass.bus.async_fire(
                EVENT_AM4_ACTIVITY,
                {
                    "address": self.address,
                    "date": day.isoformat(),
                    "steps": pts[-1].steps,          # cumulative for the day
                    "calories": pts[-1].calories,
                    "step_length": pts[-1].step_length,
                    "points": [
                        {"time": self._local(p.time).isoformat(), "steps": p.steps,
                         "calories": p.calories}
                        for p in pts
                    ],
                },
            )
        self._activity_last = new[-1].time.isoformat()
        _LOGGER.debug("%s: %d new activity point(s)", self.address, len(new))

    def _import_sleep(self, sessions: list[SleepSession]) -> None:
        seen = set(self._seen)
        latest: SleepSession | None = None
        for s in sorted(sessions, key=lambda s: s.start):
            key = f"sleep|{s.start.isoformat()}|{len(s.levels)}"
            if key in seen:
                continue
            self._seen.append(key)
            start, end = self._local(s.start), self._local(s.end)
            self.hass.bus.async_fire(
                EVENT_AM4_SLEEP,
                {
                    "address": self.address,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "awake_minutes": s.minutes(0),
                    "light_minutes": s.minutes(1),
                    "deep_minutes": s.minutes(2),
                    "levels": s.levels,   # one per 5 minutes: 0 awake, 1 light, 2 deep
                },
            )
            latest = s
        if latest is None:
            return
        end = self._local(latest.end)
        if self.data.sleep_end is None or end >= self.data.sleep_end:
            self._set(
                sleep_start=self._local(latest.start),
                sleep_end=end,
                sleep_minutes=latest.minutes(1) + latest.minutes(2),
                deep_sleep_minutes=latest.minutes(2),
                light_sleep_minutes=latest.minutes(1),
                awake_minutes=latest.minutes(0),
            )

    def _update_device_registry(self, firmware: str) -> None:
        registry = dr.async_get(self.hass)
        device = registry.async_get_device(
            connections={(dr.CONNECTION_BLUETOOTH, self.address)}
        )
        if device:
            registry.async_update_device(device.id, sw_version=firmware)
