"""Async BLE client for the classic iHealth PO3 (POV11 profile).

One custom characteristic carries both writes and notifications. The transport
is ACK-driven: the session emits ACK frames which we must write back promptly so
the device sends the next fragment.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime

from bleak.backends.device import BLEDevice
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from .protocol import (
    CHAR_UUID,
    CMD_AUTH_FAIL,
    CMD_AUTH_OK,
    CMD_BATTERY,
    CMD_GET_TIME,
    CMD_LIVE,
    CMD_MEASURE_ACK,
    LiveData,
    Po3Error,
    Po3Session,
    decode_battery,
    decode_live,
    decode_time,
)

_LOGGER = logging.getLogger(__name__)

AUTH_TIMEOUT = 6.0
RESULT_SILENCE = 1.2  # app: result = last live frame after 1.2 s without frames


class Po3Disconnected(Po3Error):
    pass


class Po3ClassicClient:
    def __init__(self, ble_device: BLEDevice) -> None:
        self._ble_device = ble_device
        self._client: BleakClientWithServiceCache | None = None
        self._session = Po3Session(writer=self._schedule_write, on_message=self._on_message)
        self._write_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._writer_task: asyncio.Task | None = None
        self._waiters: dict[int, asyncio.Future] = {}
        self._live_cb: Callable[[LiveData], None] | None = None
        self._last_live: LiveData | None = None
        self._last_valid: LiveData | None = None
        self._live_event = asyncio.Event()
        self._auth_event = asyncio.Event()
        self._auth_ok = False
        self._disconnected = asyncio.Event()

    @property
    def connected(self) -> bool:
        return self._client is not None and not self._disconnected.is_set()

    async def __aenter__(self) -> "Po3ClassicClient":
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
        _LOGGER.debug("%s: connected, enabling notifications", self._ble_device.address)
        await self._client.start_notify(CHAR_UUID, self._on_notify)
        self._writer_task = asyncio.get_running_loop().create_task(self._writer_loop())
        await asyncio.sleep(0.3)  # let the device settle after CCCD write
        self._auth_event.clear()
        self._auth_ok = False
        _LOGGER.debug("%s: starting authentication", self._ble_device.address)
        self._session.start_auth()
        try:
            await asyncio.wait_for(self._auth_event.wait(), AUTH_TIMEOUT)
        except TimeoutError:
            raise Po3Error("no FB challenge from device (auth timed out)") from None
        if not self._auth_ok:
            raise Po3Error("PO3 refused authentication")
        _LOGGER.debug("%s: authenticated (classic PO3)", self._ble_device.address)

    async def disconnect(self) -> None:
        if self._writer_task:
            self._writer_task.cancel()
            self._writer_task = None
        client, self._client = self._client, None
        if client and client.is_connected:
            try:
                await client.disconnect()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("disconnect error: %s", err)

    def _on_disconnect(self, _client) -> None:
        self._disconnected.set()
        if self._writer_task:
            self._writer_task.cancel()
            self._writer_task = None
        self._auth_event.set()
        self._live_event.set()
        for fut in set(self._waiters.values()):
            if not fut.done():
                fut.set_exception(Po3Disconnected("device disconnected"))
        self._waiters.clear()

    # ---- low level ----
    def _on_notify(self, _char, data: bytearray) -> None:
        _LOGGER.debug("%s: notify %s", self._ble_device.address, bytes(data).hex())
        try:
            self._session.feed(bytes(data))
        except Po3Error as err:
            _LOGGER.warning("%s: frame dropped: %s", self._ble_device.address, err)

    def _schedule_write(self, frame: bytes) -> None:
        # Queue frames so fragments go out strictly in order (the device ACKs
        # each one; sending them concurrently loses fragments).
        self._write_queue.put_nowait(frame)

    async def _writer_loop(self) -> None:
        # Writes go out strictly in order (awaited one at a time). No extra
        # delay: ACKs must reach the device promptly or it retransmits its
        # challenge, which derails the handshake.
        while True:
            frame = await self._write_queue.get()
            if not self.connected:
                continue
            try:
                await self._client.write_gatt_char(CHAR_UUID, frame, response=False)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("%s: write failed: %s", self._ble_device.address, err)

    def _expect(self, *cmds: int) -> asyncio.Future:
        fut = asyncio.get_running_loop().create_future()
        for c in cmds:
            self._waiters[c] = fut
        return fut

    def _on_message(self, cmd: int, data: bytes) -> None:
        if cmd == CMD_AUTH_OK:
            self._auth_ok = True
            self._auth_event.set()
        elif cmd == CMD_AUTH_FAIL:
            self._auth_ok = False
            self._auth_event.set()
        elif cmd == CMD_LIVE:
            prev = self._last_live.perfusion_index if self._last_live else None
            live = decode_live(data, prev)
            if live:
                self._last_live = live
                if live.spo2 and live.pulse_rate:
                    self._last_valid = live
                self._live_event.set()
                if self._live_cb:
                    self._live_cb(live)
        fut = self._waiters.get(cmd)
        if fut and not fut.done():
            fut.set_result((cmd, data))

    # ---- commands ----
    async def get_battery(self) -> int | None:
        fut = self._expect(CMD_BATTERY)
        self._session.get_battery()
        try:
            _, d = await asyncio.wait_for(fut, 4)
            return decode_battery(d)
        except asyncio.TimeoutError:
            return None
        finally:
            self._waiters.pop(CMD_BATTERY, None)

    async def get_time(self) -> datetime | None:
        fut = self._expect(CMD_GET_TIME)
        self._session.get_time()
        try:
            _, d = await asyncio.wait_for(fut, 4)
            return decode_time(d)
        except asyncio.TimeoutError:
            return None
        finally:
            self._waiters.pop(CMD_GET_TIME, None)

    async def set_time(self) -> None:
        self._session.set_time()

    async def measure(
        self,
        on_live: Callable[[LiveData], None] | None = None,
        on_result: Callable[[LiveData], None] | None = None,
        timeout: float = 600,
        idle_timeout: float = 45,
    ) -> LiveData | None:
        """Stream readings until the device goes quiet or disconnects.

        The session is kept open for the whole time the oximeter is on. Live
        frames update continuously via on_live; when the stream settles for
        RESULT_SILENCE, on_result gets the last stable reading. We only stop
        when the device disconnects or stays silent for idle_timeout (it was
        switched off), never after a single reading.
        """
        self._live_cb, self._last_live, self._last_valid = on_live, None, None
        self._session.start_measure()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        settled = None
        idle = 0.0
        try:
            while loop.time() < deadline and self.connected:
                self._live_event.clear()
                try:
                    await asyncio.wait_for(self._live_event.wait(), RESULT_SILENCE)
                    idle = 0.0
                except asyncio.TimeoutError:
                    # a gap in the stream: publish the settled reading, keep going
                    if self._last_valid and self._last_valid is not settled:
                        settled = self._last_valid
                        if on_result:
                            on_result(settled)
                    idle += RESULT_SILENCE
                    if idle >= idle_timeout:
                        break
            return self._last_valid
        finally:
            self._live_cb = None
