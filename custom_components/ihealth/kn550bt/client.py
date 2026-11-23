"""Async BLE client for the iHealth KN-550BT blood-pressure monitor.

A session is: connect -> read protocol string / firmware (Device Information)
-> authenticate -> battery -> offline memory -> set clock -> disconnect.

The memory is never cleared (the app only does so on an explicit "delete"),
so the phone app still sees every reading.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from bleak.backends.device import BLEDevice
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from .protocol import (
    CHAR_FIRMWARE,
    CHAR_PROTOCOL,
    CHAR_PROTOCOL_ALT,
    CMD_AUTH_FAIL,
    CMD_AUTH_OK,
    CMD_BATTERY,
    CMD_ERROR,
    CMD_FUNCTION_INFO,
    CMD_GET_TIME,
    CMD_OFFLINE_DATA,
    CMD_OFFLINE_DATA_NEW,
    CMD_OFFLINE_NUM,
    ERRORS,
    PROTO_BPAV10,
    PROTO_BPAV21,
    RX_AV10,
    RX_AV21,
    RX_DEV,
    SERVICE_AV10,
    SERVICE_AV21,
    SERVICE_DEV,
    TX_AV10,
    TX_AV21,
    TX_DEV,
    BpRecord,
    Kn550Error,
    Kn550Session,
    decode_battery,
    decode_records,
    decode_time,
    firmware_number,
)

_LOGGER = logging.getLogger(__name__)

AUTH_TIMEOUT = 6.0
CMD_TIMEOUT = 4.0
MAX_HISTORY_PAGES = 64

# service -> (write char, notify char, stream transport, default protocol string)
PROFILES = {
    SERVICE_AV21: (TX_AV21, RX_AV21, False, PROTO_BPAV21),
    SERVICE_AV10: (TX_AV10, RX_AV10, False, PROTO_BPAV10),
    SERVICE_DEV: (TX_DEV, RX_DEV, True, None),
}


class Kn550Disconnected(Kn550Error):
    pass


class Kn550DeviceError(Kn550Error):
    """The monitor answered with an error frame (0x38)."""

    def __init__(self, code: int) -> None:
        self.code = code
        super().__init__(f"device error {code}: {ERRORS.get(code, 'unknown')}")


def _clean(raw: bytes | bytearray | None) -> str | None:
    if not raw:
        return None
    text = bytes(raw).split(b"\0", 1)[0].decode("utf-8", "replace").strip()
    return text or None


class Kn550Client:
    def __init__(self, ble_device: BLEDevice) -> None:
        self._ble_device = ble_device
        self._client: BleakClientWithServiceCache | None = None
        self._session: Kn550Session | None = None
        self._tx: str | None = None
        self._rx: str | None = None
        self._write_response = False
        self._write_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._writer_task: asyncio.Task | None = None
        self._waiters: dict[int, asyncio.Future] = {}
        self._auth_event = asyncio.Event()
        self._auth_ok = False
        self._disconnected = asyncio.Event()
        self.protocol: str | None = None
        self.firmware: str | None = None
        self.stream = False

    @property
    def connected(self) -> bool:
        return self._client is not None and not self._disconnected.is_set()

    @property
    def _addr(self) -> str:
        return self._ble_device.address

    async def __aenter__(self) -> "Kn550Client":
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
        default_proto = self._pick_profile()
        await self._read_device_information()
        self.protocol = self.protocol or default_proto
        _LOGGER.debug(
            "%s: profile %s, protocol %s, firmware %s",
            self._addr, "stream" if self.stream else "classic", self.protocol, self.firmware,
        )
        self._session = Kn550Session(
            stream=self.stream,
            protocol=self.protocol,
            writer=self._schedule_write,
            on_message=self._on_message,
        )
        await self._client.start_notify(self._rx, self._on_notify)
        self._writer_task = asyncio.get_running_loop().create_task(self._writer_loop())
        await asyncio.sleep(0.3)  # let the device settle after the CCCD write
        self._auth_event.clear()
        self._auth_ok = False
        self._session.start_auth()
        try:
            await asyncio.wait_for(self._auth_event.wait(), AUTH_TIMEOUT)
        except TimeoutError:
            raise Kn550Error("no answer to authentication") from None
        if self._disconnected.is_set():
            raise Kn550Disconnected("device disconnected during authentication")
        if not self._auth_ok:
            raise Kn550Error("KN-550BT refused authentication")
        _LOGGER.debug("%s: authenticated", self._addr)

    def _pick_profile(self) -> str | None:
        services = self._client.services
        for uuid, (tx, rx, stream, proto) in PROFILES.items():
            if services.get_service(uuid) is None:
                continue
            self._tx, self._rx, self.stream = tx, rx, stream
            char = services.get_characteristic(tx)
            props = getattr(char, "properties", None) or ()
            self._write_response = "write-without-response" not in props
            return proto
        raise Kn550Error("no KN-550BT service found on the device")

    async def _read_device_information(self) -> None:
        for uuid in (CHAR_PROTOCOL, CHAR_PROTOCOL_ALT):
            if self.protocol:
                break
            self.protocol = await self._read(uuid)
        self.firmware = await self._read(CHAR_FIRMWARE)

    async def _read(self, uuid: str) -> str | None:
        if self._client.services.get_characteristic(uuid) is None:
            return None
        try:
            return _clean(await self._client.read_gatt_char(uuid))
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("%s: cannot read %s: %s", self._addr, uuid, err)
            return None

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
        for fut in set(self._waiters.values()):
            if not fut.done():
                fut.set_exception(Kn550Disconnected("device disconnected"))
        self._waiters.clear()

    # ------------------------------------------------------------ low level
    def _on_notify(self, _char, data: bytearray) -> None:
        _LOGGER.debug("%s: notify %s", self._addr, bytes(data).hex())
        try:
            self._session.feed(bytes(data))
        except Kn550Error as err:
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
                await self._client.write_gatt_char(
                    self._tx, frame, response=self._write_response
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("%s: write failed: %s", self._addr, err)

    def _on_message(self, cmd: int, data: bytes) -> None:
        if cmd == CMD_AUTH_OK:
            self._auth_ok = True
            self._auth_event.set()
            return
        if cmd == CMD_AUTH_FAIL:
            self._auth_ok = False
            self._auth_event.set()
            return
        fut = self._waiters.get(cmd)  # CMD_ERROR is registered by _request too
        if fut and not fut.done():
            fut.set_result((cmd, data))
        elif cmd == CMD_ERROR:
            _LOGGER.debug("%s: unsolicited device error %s", self._addr, data.hex())

    async def _request(self, send, *replies: int, timeout: float = CMD_TIMEOUT) -> tuple[int, bytes]:
        if not self.connected:
            raise Kn550Disconnected("not connected")
        fut = asyncio.get_running_loop().create_future()
        for c in (*replies, CMD_ERROR):
            self._waiters[c] = fut
        try:
            send()
            cmd, data = await asyncio.wait_for(fut, timeout)
        finally:
            for c in (*replies, CMD_ERROR):
                if self._waiters.get(c) is fut:
                    del self._waiters[c]
        if cmd == CMD_ERROR:
            raise Kn550DeviceError(data[0] if data else -1)
        return cmd, data

    # ------------------------------------------------------------ commands
    @property
    def _fw(self) -> int | None:
        return firmware_number(self.firmware)

    def battery_supported(self) -> bool:
        """Bp550BTControl.getBattery(): refused on some stream firmwares."""
        if not self.stream or self._fw is None:
            return True
        if self._fw == 200:
            return False
        return int(self.firmware.split(".")[2][0]) % 2 == 1

    async def get_battery(self) -> int | None:
        if not self.battery_supported():
            return None
        try:
            _, data = await self._request(self._session.get_battery, CMD_BATTERY)
        except TimeoutError:
            return None
        return decode_battery(data) if data else None

    async def set_time(self) -> None:
        """getFunctionInfo() carries the current time: this sets the clock."""
        try:
            await self._request(self._session.set_time, CMD_FUNCTION_INFO)
        except TimeoutError:
            _LOGGER.debug("%s: no answer to set time", self._addr)

    async def get_time(self) -> datetime | None:
        if not self.stream or (self._fw or 0) <= 200:
            return None  # Bp550BTControl.getTime(): "Unsupported" otherwise
        try:
            _, data = await self._request(self._session.get_time, CMD_GET_TIME)
        except TimeoutError:
            return None
        return decode_time(data)

    async def get_record_count(self) -> int:
        _, data = await self._request(self._session.get_offline_num, CMD_OFFLINE_NUM)
        return data[1] if len(data) > 1 else 0

    async def get_history(self) -> list[BpRecord]:
        """Download the whole memory (oldest first, as stored)."""
        count = await self.get_record_count()
        _LOGGER.debug("%s: %d record(s) in memory", self._addr, count)
        if not count:
            return []
        new_format = self.protocol == PROTO_BPAV21
        try:
            raw = await self._download(new_format)
        except TimeoutError:
            # unknown protocol string: try the other record format once
            _LOGGER.debug("%s: no answer to 0x%02X, trying the other format",
                          self._addr, CMD_OFFLINE_DATA_NEW if new_format else CMD_OFFLINE_DATA)
            new_format = not new_format
            raw = await self._download(new_format)
        calibration = new_format and (self._fw or 0) > 200
        records = decode_records(raw, new_format, calibration)
        _LOGGER.debug("%s: decoded %d record(s)", self._addr, len(records))
        return records

    async def _download(self, new_format: bool) -> bytes:
        reply = CMD_OFFLINE_DATA_NEW if new_format else CMD_OFFLINE_DATA
        raw = bytearray()
        for _ in range(MAX_HISTORY_PAGES):
            _, data = await self._request(
                lambda: self._session.get_offline_data(new_format), reply
            )
            raw += data[2:]
            if len(data) < 2 or data[1] == 0:
                break  # data[1] != 0: more pages follow
        return bytes(raw)
