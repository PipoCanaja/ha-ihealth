"""
iHealth KN-550BT (blood-pressure monitor) BLE protocol, pure Python.

Reconstructed from the decompiled iHealth app:
  com.ihealth.communication.control.Bp550BTControl
  com.ihealth.communication.ins.{A1InSet_KN550BT, A1InsSetCommon}
  com.ihealth.communication.ins.{IdentifyIns, IdentifyIns2, XXTEA, XXTEA2}
  com.ihealth.communication.base.protocol.BleCommProtocol      ("classic")
  com.ihealth.communication.base.protocol2.BleCommProtocol2    ("stream")
  com.ihealth.communication.manager.iHealthDevicesManager      (profile choice)

The KN-550BT measures on its own and keeps the readings in memory. The app
only downloads that memory, so this is what we do too: no live measurement.

Three BLE profiles exist, chosen by the advertised service:

| Service                      | Transport  | Auth          | Key             |
|------------------------------|------------|---------------|-----------------|
| com.jiuan.BPAV21             | classic    | IdentifyIns   | KN-550BT_BPAV21 |
| com.jiuan.BPAV10 (+ name)    | classic    | IdentifyIns   | KN-550BT        |
| com.jiuan.dev   (+ name)     | stream     | IdentifyIns2  | fixed key "n"   |

Every command starts with the device-type byte 0xA1.
"""
from __future__ import annotations

import logging
import os
import struct
from dataclasses import dataclass
from datetime import datetime, timedelta

try:
    from ..po3classic.protocol import (
        _F,
        Po3Error,
        _c,
        _nibble_swap,
        _Transport,
        _xxtea_encrypt,
    )
except ImportError:  # tests import the sub-packages as top-level modules
    from po3classic.protocol import (  # type: ignore[no-redef]
        _F,
        Po3Error,
        _c,
        _nibble_swap,
        _Transport,
        _xxtea_encrypt,
    )

_LOGGER = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# GATT
# --------------------------------------------------------------------------
# "com.jiuan.BPAV21" / "rec.jiuan.BPAV21" (app writes) / "sed.jiuan.BPAV21" (notify)
SERVICE_AV21 = "636f6d2e-6a69-7561-6e2e-425041563231"
TX_AV21 = "7265632e-6a69-7561-6e2e-425041563231"
RX_AV21 = "7365642e-6a69-7561-6e2e-425041563231"
# "com.jiuan.BPAV10", shared with KD-926 / KD-723 (told apart by name)
SERVICE_AV10 = "636f6d2e-6a69-7561-6e2e-425041563130"
TX_AV10 = "7265632e-6a69-7561-6e2e-425041563130"
RX_AV10 = "7365642e-6a69-7561-6e2e-425041563130"
# "com.jiuan.dev", shared with BG5S (told apart by name "KN-550BT")
SERVICE_DEV = "636f6d2e-6a69-7561-6e2e-646576000000"
TX_DEV = "7265632e-6a69-7561-6e2e-646576000000"
RX_DEV = "7365642e-6a69-7561-6e2e-646576000000"

# Device Information (read before auth by the app: protocol string + firmware)
CHAR_FIRMWARE = "00002a26-0000-1000-8000-00805f9b34fb"
CHAR_PROTOCOL = "00002a30-0000-1000-8000-00805f9b34fb"
CHAR_PROTOCOL_ALT = "0000ff01-0000-1000-8000-00805f9b34fb"

LOCAL_NAME = "KN-550BT"

PROTO_BPAV21 = "com.jiuan.BPAV21"
PROTO_BPAV22 = "com.jiuan.BPAV22"
PROTO_BPAV10 = "com.jiuan.BPAV10"
PROTO_BPV23 = "com.jiuan.BPV23"

TX_HEAD = 0xB0
RX_HEAD = 0xA0
A1 = 0xA1  # A1InsSetCommon.deviceType

CMD_BATTERY = 0x20
CMD_FUNCTION_INFO = 0x21     # also sets the device clock (payload = now)
CMD_ERROR = 0x38
CMD_OFFLINE_NUM = 0x40
CMD_OFFLINE_DATA = 0x46      # 10-byte records
CMD_OFFLINE_OVER = 0x47      # "transfer finished": DELETES the memory (app: auto on fw < 2.0.1)
CMD_OFFLINE_DATA_NEW = 0x4A  # 11-byte records (protocol BPAV21)
CMD_GET_DISPLAY = 0x60
CMD_SET_DISPLAY = 0x61
CMD_GET_TIME = 0x62
CMD_AUTH_FA = 0xFA
CMD_AUTH_FB = 0xFB
CMD_AUTH_FC = 0xFC
CMD_AUTH_OK = 0xFD
CMD_AUTH_FAIL = 0xFE

ERRORS = {
    0: "no suitable zero found in 20 s",
    1: "systolic pressure not found",
    2: "diastolic pressure not found",
    3: "inflating too fast",
    4: "inflating too slow",
    5: "pressure above 300 mmHg",
    6: "pressure above 15 mmHg for more than 160 s",
    7: "EEPROM read/write error",
    8: "EEPROM backup error",
    10: "SPAN value error",
    11: "CRC error",
    12: "connection error",
    13: "low battery",
    14: "result above the upper limit",
    16: "result below the lower limit",
    17: "arm moved during the measurement",
    32: "device is measuring",
}


class Kn550Error(Exception):
    pass


# --------------------------------------------------------------------------
# Keys
# --------------------------------------------------------------------------
# getKey(<type>) from libiHealth.so (see docs/device_keys.md). The classic
# IdentifyIns path derives Ka = XXTEA(d(getKey), d(F)), as for the PO3.
_KEY_KN550BT = bytes.fromhex("5835d27714e6e2da4de3795f6565ceb4")
_KEY_KN550BT_BPAV21 = bytes.fromhex("b51dfe0438ba4f897837979124e23fcc")
_KEY_BPWEIXIN = bytes.fromhex("70e3fb742028d21a8ef30d28c1fa7127")


def _derive_ka(native_key: bytes) -> bytes:
    return _xxtea_encrypt(_nibble_swap(native_key), _nibble_swap(_F))


KA_KN550BT = _derive_ka(_KEY_KN550BT)
KA_KN550BT_BPAV21 = _derive_ka(_KEY_KN550BT_BPAV21)
KA_BPWEIXIN = _derive_ka(_KEY_BPWEIXIN)

# IdentifyIns2.n: fixed key used by the stream profile for "KN-550BT" and
# "KN-550BT_BPAV21" alike. (It equals KA_KN550BT_BPAV21, a good cross-check.)
KEY_STREAM = bytes([25, 1, 7, 150, 242, 35, 26, 104, 139, 84, 52, 98, 140, 87, 235, 25])


def classic_ka(protocol: str | None) -> bytes:
    """Key choice of A1InsSetCommon.haveNewData(0xFB)."""
    if protocol in (PROTO_BPAV21, PROTO_BPAV22):
        return KA_KN550BT_BPAV21
    if protocol and PROTO_BPV23 in protocol:
        return KA_BPWEIXIN
    return KA_KN550BT


# --------------------------------------------------------------------------
# XXTEA2 (little-endian, with an appended length word) - IdentifyIns2
# --------------------------------------------------------------------------
_M32 = 0xFFFFFFFF
_DELTA = 0x9E3779B9


def _to_words(data: bytes, with_length: bool) -> list[int]:
    n = (len(data) + 3) // 4
    padded = data + b"\0" * (n * 4 - len(data))
    v = list(struct.unpack("<%dI" % n, padded))
    if with_length:
        v.append(len(data))
    return v


def _key16(key: bytes) -> list[int]:
    key = (key + b"\0" * 16)[:16]
    return list(struct.unpack("<4I", key))


def _mx(s, y, z, p, e, k):
    a = (((z >> 5) ^ ((y << 2) & _M32)) + ((y >> 3) ^ ((z << 4) & _M32))) & _M32
    b = ((s ^ y) + (k[(p & 3) ^ e] ^ z)) & _M32
    return a ^ b


def xxtea2_encrypt(data: bytes, key: bytes) -> bytes:
    if not data:
        return data
    v, k = _to_words(data, True), _key16(key)
    n = len(v)
    z, s = v[n - 1], 0
    for _ in range(52 // n + 6):
        s = (s + _DELTA) & _M32
        e = (s >> 2) & 3
        for p in range(n - 1):
            y = v[p + 1]
            v[p] = z = (v[p] + _mx(s, y, z, p, e, k)) & _M32
        y = v[0]
        v[n - 1] = z = (v[n - 1] + _mx(s, y, z, n - 1, e, k)) & _M32
    return struct.pack("<%dI" % n, *v)


def xxtea2_decrypt(data: bytes, key: bytes) -> bytes | None:
    if not data:
        return data
    v, k = _to_words(data, False), _key16(key)
    n = len(v)
    if n >= 2:
        s = ((52 // n + 6) * _DELTA) & _M32
        y = v[0]
        while s:
            e = (s >> 2) & 3
            for p in range(n - 1, 0, -1):
                z = v[p - 1]
                v[p] = y = (v[p] - _mx(s, y, z, p, e, k)) & _M32
            z = v[n - 1]
            v[0] = y = (v[0] - _mx(s, y, z, 0, e, k)) & _M32
            s = (s - _DELTA) & _M32
    length = v[-1]
    total = n * 4
    if length < total - 7 or length > total - 4:
        return None
    return struct.pack("<%dI" % n, *v)[:length]


# --------------------------------------------------------------------------
# Auth responses
# --------------------------------------------------------------------------
def classic_auth_response(fb48: bytes, ka: bytes) -> bytes:
    """IdentifyIns.deciphering(): 48-byte FB -> 16-byte FC payload."""
    device_id, third = fb48[0:16], fb48[32:48]
    enc_key = _xxtea_encrypt(_c(device_id), ka)
    return _c(_xxtea_encrypt(_c(third), enc_key))


def stream_auth_response(fb52: bytes, r1: bytes) -> bytes | None:
    """IdentifyIns2.deciphering(): 52-byte FB -> 20-byte FC payload.

    FB = R2 seed (16) | R1' (20) | ID (16). The device proves itself by sending
    R1 encrypted (R1'); we answer with the R2 seed encrypted. Returns None when
    R1' does not decrypt to our R1 (the app then aborts too).
    """
    seed, r1_stroke, device_id = fb52[0:16], fb52[16:36], fb52[36:52]
    ka = xxtea2_encrypt(device_id, KEY_STREAM)
    if xxtea2_decrypt(r1_stroke, ka) != r1:
        return None
    return xxtea2_encrypt(seed, ka)


# --------------------------------------------------------------------------
# Decoders
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class BpRecord:
    timestamp: datetime | None
    systolic: int
    diastolic: int
    pulse_rate: int
    arrhythmia: bool = False
    hsd: bool = False                 # "hypertension symbol" flag (10-byte format)
    body_movement: bool = False       # 11-byte format only
    # 11-byte format, firmware > 2.0.0: the clock was not trustworthy when this
    # reading was taken (bit 0x01). The app then rebases the timestamp on the
    # device clock: real = record + (phone now - device now). See fix_timestamp.
    needs_time_fix: bool = False
    scheme_id: int | None = None


def _dt(y, mo, d, h, mi, s=0) -> datetime | None:
    try:
        return datetime(2000 + y, mo, d, h, mi, s)
    except ValueError:
        return None


def decode_records(data: bytes, new_format: bool, calibration_flag: bool = False) -> list[BpRecord]:
    """A1InSet_KN550BT.e(): concatenated offline records.

    10-byte (0x46): yy|arr<<7, mm|hsd<<7, dd, hh, mi, sys-dia, dia, hr, -, -
    11-byte (0x4A): yy, mm, dd, hh, mi, ss, sys-dia, dia, hr, scheme, flags
                    flags: 0x80 arrhythmia, 0x10 body movement,
                           0x01 timestamp needs fixing (fw > 2.0.0 only)
    """
    out: list[BpRecord] = []
    size = 11 if new_format else 10
    for i in range(len(data) // size):
        r = data[i * size:(i + 1) * size]
        if new_format:
            dia = r[7]
            flags = r[10]
            out.append(
                BpRecord(
                    timestamp=_dt(r[0], r[1], r[2], r[3], r[4], r[5]),
                    systolic=r[6] + dia,
                    diastolic=dia,
                    pulse_rate=r[8],
                    arrhythmia=bool(flags & 0x80),
                    body_movement=bool(flags & 0x10),
                    needs_time_fix=calibration_flag and bool(flags & 0x01),
                    scheme_id=r[9],
                )
            )
        else:
            dia = r[6]
            out.append(
                BpRecord(
                    timestamp=_dt(r[0] & 0x7F, r[1] & 0x7F, r[2], r[3], r[4]),
                    systolic=r[5] + dia,
                    diastolic=dia,
                    pulse_rate=r[7],
                    arrhythmia=bool(r[0] & 0x80),
                    hsd=bool(r[1] & 0x80),
                )
            )
    return out


def fix_timestamp(
    record: BpRecord, device_now: datetime | None, real_now: datetime
) -> datetime | None:
    """Kn550BtOptimizationBaseViewModel: correct a flagged reading's time.

    ``device_now`` is the monitor clock read at the start of the session
    (0x62), ``real_now`` the local time at that moment (both naive). Without
    the device clock the stored timestamp is returned as is.
    """
    if record.timestamp is None or not record.needs_time_fix or device_now is None:
        return record.timestamp
    offset: timedelta = real_now - device_now
    return record.timestamp + offset


def decode_battery(data: bytes) -> int:
    level = data[0]
    return 100 if level <= 0 or level > 100 else level


def decode_time(data: bytes) -> datetime | None:
    if len(data) < 6:
        return None
    return _dt(*data[:6])


def firmware_number(version: str | None) -> int | None:
    """A1InSet_KN550BT.f(): "2.0.1" -> 201."""
    if not version:
        return None
    parts = version.strip().split(".")
    if len(parts) < 3 or not all(p[:1].isdigit() for p in parts[:3]):
        return None
    try:
        return int(parts[0]) * 100 + int(parts[1]) * 10 + int(parts[2][0])
    except ValueError:
        return None


def function_info_payload(now: datetime | None = None) -> bytes:
    now = now or datetime.now()
    year = now.year - 2000 if now.year >= 2000 else 0
    return bytes([A1, CMD_FUNCTION_INFO, year, now.month, now.day, now.hour, now.minute, now.second])


# --------------------------------------------------------------------------
# Stream transport (BleCommProtocol2 + BleUnPackageData2): no ACKs
# --------------------------------------------------------------------------
class _StreamTransport:
    CHUNK = 20

    def __init__(self) -> None:
        self.seq = 1
        self._rx = bytearray()
        self.out: list[bytes] = []

    def send_payload(self, payload: bytes) -> None:
        n = len(payload)
        f = bytearray(n + 5)
        f[0] = TX_HEAD
        f[1] = (n + 2) & 0xFF
        f[2] = 0
        f[3] = self.seq & 0xFF
        f[4:4 + n] = payload
        f[n + 4] = sum(f[2:n + 4]) & 0xFF
        self.seq = (self.seq + 2) & 0xFF
        self.out += [bytes(f[i:i + self.CHUNK]) for i in range(0, len(f), self.CHUNK)]

    def feed(self, chunk: bytes):
        self._rx += chunk
        results = []
        while True:
            while self._rx and self._rx[0] != RX_HEAD:
                self._rx.pop(0)
            if len(self._rx) < 2:
                break
            total = self._rx[1] + 3
            if len(self._rx) < total:
                break
            frame = bytes(self._rx[:total])
            del self._rx[:total]
            if total <= 6:
                continue
            if (sum(frame[2:-1]) & 0xFF) != frame[-1]:
                raise Kn550Error(f"checksum error: {frame.hex()}")
            results.append((frame[5], bytes(frame[6:-1])))
        return results


# --------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------
class Kn550Session:
    """Protocol state for one connection.

    ``stream`` selects the BleCommProtocol2 / IdentifyIns2 profile ("dev"
    service); otherwise the classic BleCommProtocol / IdentifyIns one.
    """

    def __init__(self, stream: bool, protocol: str | None = None, writer=None, on_message=None) -> None:
        self.stream = stream
        self.protocol = protocol
        self.writer = writer
        self.on_message = on_message
        self.t = _StreamTransport() if stream else _Transport(family=A1)
        self.authenticated = False
        self.r1 = b""

    def _flush(self) -> None:
        for frame in self.t.out:
            _LOGGER.debug("TX %s", frame.hex())
            if self.writer:
                self.writer(frame)
        self.t.out = []

    def _send(self, payload: bytes) -> None:
        self.t.send_payload(payload)
        self._flush()

    # ---- auth ----
    def start_auth(self) -> None:
        # Java: random bytes, negatives replaced by their absolute value
        self.r1 = bytes((256 - b) & 0xFF if b >= 128 else b for b in os.urandom(16))
        self.authenticated = False
        r1 = self.r1 if self.stream else _c(self.r1)
        self._send(bytes([A1, CMD_AUTH_FA]) + r1)

    # ---- commands ----
    def get_battery(self) -> None:
        self._send(bytes([A1, CMD_BATTERY, 0, 0, 0]))

    def set_time(self, now: datetime | None = None) -> None:
        """getFunctionInfo(): returns capabilities and sets the clock."""
        self._send(function_info_payload(now))

    def get_time(self) -> None:
        self._send(bytes([A1, CMD_GET_TIME]))

    def get_offline_num(self) -> None:
        self._send(bytes([A1, CMD_OFFLINE_NUM, 1, 0, 0]))

    def get_offline_data(self, new_format: bool) -> None:
        cmd = CMD_OFFLINE_DATA_NEW if new_format else CMD_OFFLINE_DATA
        self._send(bytes([A1, cmd, 1, 0, 0]))

    def delete_memory(self) -> None:
        """Bp550BTControl.transferFinished(): clears the device memory."""
        self._send(bytes([A1, CMD_OFFLINE_OVER, 0, 0, 0]))

    # ---- RX ----
    def feed(self, chunk: bytes) -> None:
        _LOGGER.debug("RX %s", chunk.hex())
        try:
            messages = self.t.feed(chunk)
        except Po3Error as err:
            raise Kn550Error(str(err)) from err
        finally:
            self._flush()  # classic transport ACKs
        for cmd, data in messages:
            _LOGGER.debug("RX cmd 0x%02X data %s", cmd, data.hex())
            self._dispatch(cmd, data)
        self._flush()

    def _dispatch(self, cmd: int, data: bytes) -> None:
        if cmd == CMD_AUTH_FB:
            if self.stream:
                if len(data) >= 52:
                    resp = stream_auth_response(data[:52], self.r1)
                    if resp is None:
                        _LOGGER.debug("device R1 check failed; answering anyway")
                        ka = xxtea2_encrypt(data[36:52], KEY_STREAM)
                        resp = xxtea2_encrypt(data[0:16], ka)
                    self._send(bytes([A1, CMD_AUTH_FC]) + resp)
            elif len(data) >= 48:
                ka = classic_ka(self.protocol)
                self._send(bytes([A1, CMD_AUTH_FC]) + classic_auth_response(data[:48], ka))
            return
        if cmd == CMD_AUTH_OK:
            self.authenticated = True
        elif cmd == CMD_AUTH_FAIL:
            self.authenticated = False
        if self.on_message:
            self.on_message(cmd, data)
