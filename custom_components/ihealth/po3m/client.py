"""Async BLE client for the iHealth PO3M (bleak / bleak-retry-connector)."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime

from bleak.backends.device import BLEDevice
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from .protocol import (
    CMD_AUTH_FAIL,
    CMD_AUTH_OK,
    CMD_GET_BATTERY,
    CMD_GET_DEVICE_INFO,
    CMD_GET_TIME,
    CMD_LIVE_DATA,
    CMD_MEMORY_COUNT,
    CMD_MEMORY_DELETE,
    CMD_MEMORY_READ,
    CMD_NOTIFY_BATTERY,
    CMD_START_MEASURE,
    NOTIFY_CHAR_UUID,
    WRITE_CHAR_UUID,
    HistoryRecord,
    LiveData,
    Po3mError,
    Po3mSession,
    _ts,
    decode_battery,
    decode_device_info,
    decode_history_chunk,
    decode_live,
    set_time_payload,
)

_LOGGER = logging.getLogger(__name__)

AUTH_TIMEOUT = 5.0
RESPONSE_TIMEOUT = 2.0  # app: resend every 2 s ...
RETRIES = 3  # ... and give up after 3 attempts
RESULT_SILENCE = 1.2  # app: result = last live frame after 1.2 s without frames


class Po3mDisconnected(Po3mError):
    """The device went away during a session."""


class Po3mClient:
    """One BLE session with a PO3M: connect, authenticate, run commands."""

    def __init__(self, ble_device: BLEDevice) -> None:
        self._ble_device = ble_device
        self._client: BleakClientWithServiceCache | None = None
        self._session = Po3mSession(
            writer=self._schedule_write, on_message=self._on_message
        )
        self._tasks: set[asyncio.Task] = set()
        self._waiters: dict[int, asyncio.Future] = {}
        self._live_cb: Callable[[LiveData], None] | None = None
        self._last_live: LiveData | None = None
        self._last_valid: LiveData | None = None
        self._live_event = asyncio.Event()
        self._disconnected = asyncio.Event()
        self.on_battery_notify: Callable[[int], None] | None = None

    @property
    def connected(self) -> bool:
        return self._client is not None and not self._disconnected.is_set()

    # ------------------------------------------------------------ connection
    async def __aenter__(self) -> Po3mClient:
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.disconnect()

    async def connect(self) -> None:
        self._disconnected.clear()
        self._client = await establish_connection(
            BleakClientWithServiceCache,
            self._ble_device,
            self._ble_device.name or self._ble_device.address,
            disconnected_callback=self._on_disconnect,
        )
        await self._client.start_notify(NOTIFY_CHAR_UUID, self._on_notify)
        fut = self._expect(CMD_AUTH_OK, CMD_AUTH_FAIL)
        # FA is written now; FC is written from the notification callback
        self._session.start_auth()
        cmd, _ = await self._wait(fut, AUTH_TIMEOUT)
        if cmd != CMD_AUTH_OK:
            raise Po3mError("PO3M refused authentication")
        _LOGGER.debug("%s: authenticated", self._ble_device.address)

    async def disconnect(self) -> None:
        client, self._client = self._client, None
        if client and client.is_connected:
            try:
                await client.disconnect()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("disconnect error: %s", err)

    def _on_disconnect(self, _client) -> None:
        _LOGGER.debug("%s: disconnected", self._ble_device.address)
        self._disconnected.set()
        self._live_event.set()
        for fut in set(self._waiters.values()):
            if not fut.done():
                fut.set_exception(Po3mDisconnected("device disconnected"))
        self._waiters.clear()

    # ------------------------------------------------------------ low level
    def _on_notify(self, _char, data: bytearray) -> None:
        try:
            self._session.feed(bytes(data))
        except Po3mError as err:
            _LOGGER.warning("%s: frame dropped: %s", self._ble_device.address, err)

    def _schedule_write(self, frame: bytes) -> None:
        task = asyncio.get_running_loop().create_task(self._write(frame))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _write(self, frame: bytes) -> None:
        if not self.connected:
            raise Po3mDisconnected("not connected")
        await self._client.write_gatt_char(WRITE_CHAR_UUID, frame, response=True)

    def _expect(self, *cmds: int) -> asyncio.Future:
        fut = asyncio.get_running_loop().create_future()
        for cmd in cmds:
            self._waiters[cmd] = fut
        return fut

    async def _wait(self, fut: asyncio.Future, timeout: float):
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            for key in [k for k, v in self._waiters.items() if v is fut]:
                del self._waiters[key]

    def _on_message(self, cmd: int, data: bytes) -> None:
        if cmd == CMD_LIVE_DATA:
            prev_pi = self._last_live.perfusion_index if self._last_live else None
            live = decode_live(data, prev_pi)
            if live:
                self._last_live = live
                if live.spo2 and live.pulse_rate:
                    self._last_valid = live
                self._live_event.set()
                if self._live_cb:
                    self._live_cb(live)
            return
        if cmd == CMD_NOTIFY_BATTERY and self.on_battery_notify and data:
            self.on_battery_notify(data[0])
        fut = self._waiters.get(cmd)
        if fut and not fut.done():
            fut.set_result((cmd, data))

    async def request(self, payload: bytes, *expect: int) -> tuple[int, bytes]:
        """Send a command and wait for its answer (2 s timeout, 3 attempts)."""
        expect = expect or (payload[0],)
        for attempt in range(RETRIES):
            if not self.connected:
                raise Po3mDisconnected("not connected")
            fut = self._expect(*expect)
            # every attempt is a new frame (new CTR counter), like the app
            await self._write(self._session.build_frame(payload))
            try:
                return await self._wait(fut, RESPONSE_TIMEOUT)
            except asyncio.TimeoutError:
                _LOGGER.debug(
                    "timeout on 0x%02X (attempt %d/%d)", payload[0], attempt + 1, RETRIES
                )
        raise Po3mError(f"no response to command 0x{payload[0]:02X}")

    # ------------------------------------------------------------ commands
    async def get_battery(self) -> dict:
        _, data = await self.request(bytes([CMD_GET_BATTERY]))
        return decode_battery(data)

    async def get_device_info(self) -> dict:
        _, data = await self.request(bytes([CMD_GET_DEVICE_INFO]))
        return decode_device_info(data)

    async def get_time(self) -> datetime | None:
        _, data = await self.request(bytes([CMD_GET_TIME]))
        return _ts(data)

    async def set_time(self, now: datetime | None = None) -> None:
        await self.request(set_time_payload(now))

    async def measure(
        self,
        on_live: Callable[[LiveData], None] | None = None,
        timeout: float = 120,
    ) -> LiveData | None:
        """Start a measurement and stream live frames.

        Returns the last valid frame once the stream stops for RESULT_SILENCE
        seconds (what the app calls the result), the device disconnects, or
        `timeout` expires. None if no valid frame was received.
        """
        self._live_cb, self._last_live, self._last_valid = on_live, None, None
        await self.request(bytes([CMD_START_MEASURE]))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        try:
            while loop.time() < deadline and self.connected:
                self._live_event.clear()
                try:
                    await asyncio.wait_for(self._live_event.wait(), RESULT_SILENCE)
                except asyncio.TimeoutError:
                    if self._last_valid:
                        break
            return self._last_valid
        finally:
            self._live_cb = None

    async def get_history(self) -> list[HistoryRecord]:
        _, data = await self.request(bytes([CMD_MEMORY_COUNT]))
        if len(data) < 2 or (data[0] | data[1] << 8) == 0:
            return []
        records: list[HistoryRecord] = []
        more = True
        while more:
            _, data = await self.request(bytes([CMD_MEMORY_READ]))
            more, chunk = decode_history_chunk(data)
            records += chunk
        return records

    async def delete_history(self) -> None:
        await self.request(bytes([CMD_MEMORY_DELETE]))
