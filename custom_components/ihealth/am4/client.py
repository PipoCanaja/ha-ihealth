"""Async BLE client for the iHealth AM4 activity tracker.

A session is: connect -> read firmware (Device Information) -> authenticate
-> state/battery -> set clock -> today's totals -> (optionally) the stored
history -> disconnect.

Commands that would change the tracker's configuration or account binding
(reset, user id, user info, alarms...) are never sent.
"""
from __future__ import annotations

import asyncio
import logging

from bleak.backends.device import BLEDevice
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from .protocol import (
    BULK_DATA,
    BULK_FINISH,
    CHAR_FIRMWARE,
    CMD_ACTIVITY_FINISH,
    CMD_ACTIVITY_START,
    CMD_AUTH_FAIL,
    CMD_AUTH_OK,
    CMD_QUERY_STATE,
    CMD_REALTIME,
    CMD_SLEEP_FINISH,
    CMD_SLEEP_START,
    CMD_STAGE_FINISH,
    CMD_STAGE_START,
    CMD_SYNC_TIME,
    CMD_USER_ID_GET,
    FW_STAGE_DATA,
    RX_UUID,
    SERVICE_UUID,
    TX_UUID,
    ActivityPoint,
    Am4Error,
    Am4Session,
    Realtime,
    SleepSession,
    StageRecord,
    State,
    decode_activity,
    decode_realtime,
    decode_sleep,
    decode_stage,
    decode_state,
    firmware_number,
)

_LOGGER = logging.getLogger(__name__)

AUTH_TIMEOUT = 3.0   # per attempt (frames are re-sent from 1 s)
AUTH_ATTEMPTS = 3    # the tracker sometimes ignores the first FA after connecting
SETTLE_DELAY = 0.5
CMD_TIMEOUT = 4.0
# A bulk transfer may take long; give up only after this long without any
# message of that transfer
BULK_IDLE_TIMEOUT = 6.0
BULK_MAX_DURATION = 600.0
# BleCommProtocol.SendTimerTask: 10 ms between writes (bursts get lost)
WRITE_GAP = 0.01
RETRANSMIT_PERIOD = 0.25


class Am4Disconnected(Am4Error):
    pass


def _clean(raw: bytes | bytearray | None) -> str | None:
    if not raw:
        return None
    text = bytes(raw).split(b"\0", 1)[0].decode("utf-8", "replace").strip()
    return text or None


class Am4Client:
    def __init__(self, ble_device: BLEDevice) -> None:
        self._ble_device = ble_device
        self._client: BleakClientWithServiceCache | None = None
        self._session: Am4Session | None = None
        self._write_response = False
        self._write_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._writer_task: asyncio.Task | None = None
        self._retransmit_task: asyncio.Task | None = None
        self._waiters: dict[int, asyncio.Future] = {}
        self._progress: asyncio.Event | None = None
        self._auth_event = asyncio.Event()
        self._auth_ok = False
        self._disconnected = asyncio.Event()
        self.firmware: str | None = None

    @property
    def connected(self) -> bool:
        return self._client is not None and not self._disconnected.is_set()

    @property
    def _addr(self) -> str:
        return self._ble_device.address

    @property
    def fw(self) -> int | None:
        return firmware_number(self.firmware)

    async def __aenter__(self) -> "Am4Client":
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.disconnect()

    # ------------------------------------------------------------ connection
    async def connect(self) -> None:
        self._disconnected.clear()
        self._client = await establish_connection(
            BleakClientWithServiceCache,
            self._ble_device,
            self._ble_device.name or self._addr,
            disconnected_callback=self._on_disconnect,
        )
        services = self._client.services
        if services.get_service(SERVICE_UUID) is None:
            raise Am4Error("no AM4 service found on the device")
        char = services.get_characteristic(TX_UUID)
        props = getattr(char, "properties", None) or ()
        self._write_response = "write-without-response" not in props
        self.firmware = await self._read(CHAR_FIRMWARE)
        _LOGGER.debug("%s: firmware %s", self._addr, self.firmware)
        self._session = Am4Session(writer=self._schedule_write, on_message=self._on_message)
        await self._client.start_notify(RX_UUID, self._on_notify)
        loop = asyncio.get_running_loop()
        self._writer_task = loop.create_task(self._writer_loop())
        self._retransmit_task = loop.create_task(self._retransmit_loop())
        await asyncio.sleep(SETTLE_DELAY)  # let the device settle after the CCCD write
        self._auth_event.clear()
        self._auth_ok = False
        for attempt in range(1, AUTH_ATTEMPTS + 1):
            self._session.start_auth()
            try:
                await asyncio.wait_for(self._auth_event.wait(), AUTH_TIMEOUT)
                break
            except TimeoutError:
                if attempt == AUTH_ATTEMPTS:
                    raise Am4Error(
                        f"no answer to authentication ({AUTH_ATTEMPTS} attempts)"
                    ) from None
                _LOGGER.debug("%s: no answer to authentication, retrying", self._addr)
        if self._disconnected.is_set():
            raise Am4Disconnected("device disconnected during authentication")
        if not self._auth_ok:
            raise Am4Error("AM4 refused authentication")
        _LOGGER.debug("%s: authenticated", self._addr)

    async def _read(self, uuid: str) -> str | None:
        if self._client.services.get_characteristic(uuid) is None:
            return None
        try:
            return _clean(await self._client.read_gatt_char(uuid))
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("%s: cannot read %s: %s", self._addr, uuid, err)
            return None

    def _stop_tasks(self) -> None:
        for task in (self._writer_task, self._retransmit_task):
            if task:
                task.cancel()
        self._writer_task = self._retransmit_task = None

    async def disconnect(self) -> None:
        self._stop_tasks()
        client, self._client = self._client, None
        if client and client.is_connected:
            try:
                await client.disconnect()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("disconnect error: %s", err)

    def _on_disconnect(self, _client) -> None:
        self._disconnected.set()
        self._stop_tasks()
        self._auth_event.set()
        if self._progress:
            self._progress.set()
        for fut in set(self._waiters.values()):
            if not fut.done():
                fut.set_exception(Am4Disconnected("device disconnected"))
        self._waiters.clear()

    # ------------------------------------------------------------ low level
    def _on_notify(self, _char, data: bytearray) -> None:
        _LOGGER.debug("%s: notify %s", self._addr, bytes(data).hex())
        try:
            self._session.feed(bytes(data))
        except Am4Error as err:
            _LOGGER.warning("%s: frame dropped: %s", self._addr, err)

    def _schedule_write(self, frame: bytes) -> None:
        # strictly ordered, no added delay (see PO3: ACKs must go out promptly)
        self._write_queue.put_nowait(frame)

    async def _writer_loop(self) -> None:
        while True:
            frame = await self._write_queue.get()
            if not self.connected:
                continue
            try:
                await self._client.write_gatt_char(TX_UUID, frame, response=self._write_response)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("%s: write failed: %s", self._addr, err)
            if not self._write_response and not self._write_queue.empty():
                await asyncio.sleep(WRITE_GAP)

    async def _retransmit_loop(self) -> None:
        while True:
            await asyncio.sleep(RETRANSMIT_PERIOD)
            if self.connected and self._session:
                self._session.retransmit()

    def _on_message(self, cmd: int, data: bytes) -> None:
        if cmd == CMD_AUTH_OK:
            self._auth_ok = True
            self._auth_event.set()
            return
        if cmd == CMD_AUTH_FAIL:
            self._auth_ok = False
            self._auth_event.set()
            return
        if self._progress and (cmd in BULK_DATA or cmd in BULK_FINISH
                               or cmd in (CMD_ACTIVITY_START, CMD_SLEEP_START, CMD_STAGE_START)):
            self._progress.set()
        fut = self._waiters.get(cmd)
        if fut and not fut.done():
            fut.set_result((cmd, data))

    async def _request(self, send, *replies: int, timeout: float = CMD_TIMEOUT) -> tuple[int, bytes]:
        if not self.connected:
            raise Am4Disconnected("not connected")
        fut = asyncio.get_running_loop().create_future()
        for c in replies:
            self._waiters[c] = fut
        try:
            send()
            return await asyncio.wait_for(fut, timeout)
        finally:
            for c in replies:
                if self._waiters.get(c) is fut:
                    del self._waiters[c]

    async def _bulk(self, send, start: int, finish: int) -> list[bytes]:
        """Run one start/data.../finish transfer, waiting while it progresses."""
        if not self.connected:
            raise Am4Disconnected("not connected")
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._waiters[finish] = fut
        self._progress = asyncio.Event()
        deadline = loop.time() + BULK_MAX_DURATION
        try:
            send()
            while not fut.done():
                if self._disconnected.is_set():
                    raise Am4Disconnected("device disconnected")
                if loop.time() > deadline:
                    raise TimeoutError("transfer too long")
                self._progress.clear()
                try:
                    await asyncio.wait_for(self._progress.wait(), BULK_IDLE_TIMEOUT)
                except TimeoutError:
                    if not fut.done():
                        raise TimeoutError(
                            f"transfer stalled (no message for {BULK_IDLE_TIMEOUT:.0f} s)"
                        ) from None
            fut.result()
        finally:
            self._progress = None
            if self._waiters.get(finish) is fut:
                del self._waiters[finish]
        return list(self._session.blocks[start])

    # ------------------------------------------------------------ commands
    async def get_state(self) -> State | None:
        _, data = await self._request(self._session.query_state, CMD_QUERY_STATE)
        return decode_state(data, self.fw)

    async def get_user_id(self) -> int | None:
        _, data = await self._request(self._session.get_user_id, CMD_USER_ID_GET)
        if len(data) < 4:
            return None
        return int.from_bytes(data[:4], "big")

    async def sync_time(self) -> None:
        await self._request(self._session.sync_time, CMD_SYNC_TIME)

    async def get_realtime(self) -> Realtime | None:
        _, data = await self._request(self._session.get_realtime, CMD_REALTIME)
        return decode_realtime(data)

    async def get_activity(self) -> list[ActivityPoint]:
        blocks = await self._bulk(self._session.sync_activity, CMD_ACTIVITY_START, CMD_ACTIVITY_FINISH)
        return decode_activity(blocks)

    async def get_sleep(self) -> list[SleepSession]:
        blocks = await self._bulk(self._session.sync_sleep, CMD_SLEEP_START, CMD_SLEEP_FINISH)
        return decode_sleep(blocks)

    async def get_stage(self) -> list[StageRecord]:
        blocks = await self._bulk(self._session.sync_stage, CMD_STAGE_START, CMD_STAGE_FINISH)
        if self.fw is not None and self.fw < FW_STAGE_DATA:
            return []  # AaInsSet: ignored on older firmware
        return decode_stage(b"".join(blocks))
