"""
iHealth PO3 (classic "POV11" profile) BLE protocol, pure Python.

Reconstructed from the decompiled iHealth app:
  com.ihealth.communication.ins.{AcInsSet, IdentifyIns, POMethod, XXTEA}
  com.ihealth.communication.base.protocol.BleCommProtocol
  com.ihealth.communication.base.ble.BleUnPackageData

This is the OLDER oximeter profile (single read/write/notify characteristic),
distinct from the newer PO3M "POV12" profile in po3m_protocol.py.

Auth key material was recovered from libiHealth.so: getKey("PO3").
"""
from __future__ import annotations

import os
import struct
from dataclasses import dataclass, field
from datetime import datetime

# --------------------------------------------------------------------------
# GATT  (service 128-bit "com.jiuan.POV11", one write+notify characteristic)
# --------------------------------------------------------------------------
SERVICE_UUID = "636f6d2e-6a69-7561-6e2e-504f56313100"
CHAR_UUID = "7274782e-6a69-7561-6e2e-504f56313100"  # "rtx.jiuan.POV11" write+notify

TX_HEAD = 0xB0
RX_HEAD = 0xA0
AC = 0xAC  # device family prefix for every command

# Command bytes (second byte, after AC)
CMD_SET_TIME = 0xA2
CMD_START_MEASURE = 0xA5
CMD_MEASURE_ACK = 0xA6   # device -> app readiness; app replies AC A6
CMD_LIVE = 0xA7          # device -> app live frame; result = last after 1.2 s
CMD_MEM_COUNT = 0xA9     # app asks memory; device replies with count
CMD_MEM_MORE = 0xAA      # app pulls next
CMD_MEM_HDR = 0xAB       # device -> app, record header chunk
CMD_MEM_BODY = 0xAC   # device -> app, record body chunk (same value as AC prefix)
CMD_MEM_END = 0xAD       # device -> app, end of memory
CMD_BATTERY = 0xC1
CMD_DEVICE_INFO = 0xC6
CMD_NOTIFY_INFO = 0xC7
CMD_GET_TIME = 0xC8
CMD_AUTH_FA = 0xFA       # app -> device: FA + c(R1)
CMD_AUTH_FB = 0xFB       # device -> app: 48-byte challenge
CMD_AUTH_FC = 0xFC       # app -> device: FC + response(16)
CMD_AUTH_OK = 0xFD
CMD_AUTH_FAIL = 0xFE

M32 = 0xFFFFFFFF

# getKey("PO3") from libiHealth.so, then Ka = XXTEA(d(key), d(F)).
_KEY_PO3 = bytes.fromhex("bf4b051142d270a3932d2daacca9bd1e")
_F = bytes([67, 104, 47, 72, 81, 52, 76, 122, 73, 116, 89, 84, 52, 50, 115, 61])


class Po3Error(Exception):
    pass


# --------------------------------------------------------------------------
# Old XXTEA (big-endian whole-array block, n = len/4)
# --------------------------------------------------------------------------
def _xxtea_encrypt(data: bytes, key: bytes) -> bytes:
    v = list(struct.unpack(">%dI" % (len(data) // 4), data))
    k = list(struct.unpack(">4I", key))
    n = len(v)
    if n < 2:
        return bytes(data)
    i = n - 1
    z = v[i]
    s = 0
    for _ in range(52 // n + 6):
        s = (s - 0x61C88647) & M32
        e = (s >> 2) & 3
        p = 0
        while p < i:
            y = v[p + 1]
            z = ((((((z >> 5) ^ (y << 2)) + ((y >> 3) ^ (z << 4))) & M32)
                  ^ (((y ^ s) + (z ^ k[(p & 3) ^ e])) & M32)) + v[p]) & M32
            v[p] = z
            p += 1
        y = v[0]
        z = ((((((z >> 5) ^ (y << 2)) + ((y >> 3) ^ (z << 4))) & M32)
              ^ (((y ^ s) + (z ^ k[e ^ (p & 3)])) & M32)) + v[i]) & M32
        v[i] = z
    return struct.pack(">%dI" % n, *v)


def _nibble_swap(a: bytes) -> bytes:
    return bytes(((b & 0xF0) >> 4) | ((b & 0x0F) << 4) for b in a)


def _c(a: bytes) -> bytes:
    """IdentifyIns.c(): reverse each of the four 4-byte words."""
    o = bytearray(16)
    for i in range(4):
        o[i] = a[3 - i]
        o[i + 4] = a[7 - i]
        o[i + 8] = a[11 - i]
        o[i + 12] = a[15 - i]
    return bytes(o)


_KA = _xxtea_encrypt(_nibble_swap(_KEY_PO3), _nibble_swap(_F))  # session base key


def _auth_response(fb48: bytes) -> bytes:
    """IdentifyIns.deciphering(): 48-byte FB -> 16-byte FC payload."""
    device_id, _r1p, third = fb48[0:16], fb48[16:32], fb48[32:48]
    enc_key = _xxtea_encrypt(_c(device_id), _KA)
    return _c(_xxtea_encrypt(_c(third), enc_key))


# --------------------------------------------------------------------------
# Decoded messages
# --------------------------------------------------------------------------
@dataclass
class LiveData:
    spo2: int
    pulse_rate: int
    pulse_strength: int
    perfusion_index: float | None
    pulse_wave: list[int] = field(default_factory=list)


@dataclass
class HistoryRecord:
    timestamp: datetime | None
    spo2: int
    pulse_rate: int
    perfusion_index: float


def decode_live(data: bytes, last_pi: float | None = None) -> LiveData | None:
    """A7: [spo2][hr][strength][AC lo hi][DC lo hi][w0..][w1..][w2..]"""
    if len(data) < 13:
        return None
    spo2, hr, strength = data[0], data[1], data[2]
    if spo2 > 100 or hr > 250:
        return None
    ac = data[3] | data[4] << 8
    dc = data[5] | data[6] << 8
    pi = round(ac / dc * 1000) / 10 if (spo2 and hr and dc) else 0.0
    if not 0.2 <= pi <= 20.0:
        pi = last_pi
    wave = [data[7 + 2 * i] | data[8 + 2 * i] << 8 for i in range(3)]
    return LiveData(spo2, hr, strength, pi, wave)


def decode_battery(data: bytes) -> int:
    return data[0]


def decode_time(data: bytes) -> datetime | None:
    """C8: [yy][mm][dd][hh][mi][ss], yy since 2000 (month low nibble per app)."""
    try:
        return datetime(2000 + data[0], data[1] & 0x0F, data[2], data[3], data[4], data[5])
    except (ValueError, IndexError):
        return None


def set_time_payload(now: datetime | None = None) -> bytes:
    now = now or datetime.now()
    if now.year < 2023:
        now = datetime(2023, 1, 1)
    return bytes([AC, CMD_SET_TIME, now.year - 2000, now.month, now.day,
                  now.hour, now.minute, now.second])


# --------------------------------------------------------------------------
# Transport: B0/A0 framing, per-fragment ACK, reassembly (BleCommProtocol)
# --------------------------------------------------------------------------
class _Transport:
    """Faithful port of BleCommProtocol (TX) + BleUnPackageData (RX)."""

    def __init__(self) -> None:
        self.seq = 1
        self._rx = bytearray()
        self._frag: dict[int, bytes] = {}   # idx -> data
        self._frag_cmd = 0
        self._frag_count = 0
        self.out: list[bytes] = []

    # ---- TX ----
    @staticmethod
    def _checksum(frame: bytes, end: int) -> int:
        return sum(frame[2:end]) & 0xFF

    def send_payload(self, payload: bytes) -> None:
        """payload[0] is AC; splits into 14-byte fragments like the app does."""
        first, rest = payload[0], payload[1:]   # app polls the AC byte first
        if len(payload) <= 15:
            n = len(payload)
            f = bytearray(n + 5)
            f[0] = TX_HEAD
            f[1] = n + 2
            f[2] = 0
            f[3] = self.seq & 0xFF
            f[4] = first
            f[5:5 + len(rest)] = rest
            f[n + 4] = self._checksum(f, n + 4)
            self.seq = (self.seq + 2) & 0xFF
            self.out.append(bytes(f))
            return
        # multi-fragment (BleCommProtocol.d): sizes 14.. + remainder, AC repeated
        size = len(rest)
        full = size // 14
        sizes = [14] * full + [size % 14]
        count = full + 1
        pos = 0
        for i6, seg in enumerate(sizes):
            f = bytearray(seg + 6)
            f[0] = TX_HEAD
            f[1] = seg + 3
            f[2] = ((full << 4) + full) - i6      # fragment index field
            f[3] = self.seq & 0xFF
            f[4] = first                          # AC repeated in every fragment
            f[5:5 + seg] = rest[pos:pos + seg]
            pos += seg
            f[seg + 5] = self._checksum(f, seg + 5)
            self.seq = (self.seq + 2) & 0xFF
            self.out.append(bytes(f))

    def _send_ack(self, low: int, seq_val: int) -> None:
        b, b2, e = low & 0xFF, seq_val & 0xFF, AC
        self.out.append(bytes([TX_HEAD, 3, b, b2, e, (b + b2 + e) & 0xFF]))

    # ---- RX ----
    def feed(self, chunk: bytes):
        self._rx += chunk
        results = []
        while True:
            while self._rx and self._rx[0] != RX_HEAD:
                self._rx.pop(0)
            if len(self._rx) < 6:
                break
            total = (self._rx[1] & 0xFF) + 3
            if total < 6 or len(self._rx) < total:
                break
            frame = bytes(self._rx[:total])
            del self._rx[:total]
            r = self._handle_frame(frame)
            if r is not None:
                results.append(r)
        return results

    def _handle_frame(self, f: bytes):
        if (sum(f[2:-1]) & 0xFF) != f[-1]:
            raise Po3Error(f"checksum error: {f.hex()}")
        if len(f) == 6:          # pure ACK from device
            return None
        field2 = f[2] & 0xFF
        seq = f[3] & 0xFF
        # app acknowledges every non-0xF0 frame so the device sends the next one
        if field2 != 0xF0:
            self._send_ack((field2 & 0x0F) + 0xA0, (seq + 1) & 0xFF)

        if field2 in (0x00, 0xF0):               # single packet
            return (f[5] & 0xFF, bytes(f[6:-1]))

        count = (field2 >> 4) + 1                 # multi-fragment
        idx = field2 & 0x0F
        if idx == count - 1:                      # header fragment
            self._frag.clear()
            self._frag_cmd = f[5] & 0xFF
            self._frag_count = count
            self._frag[idx] = bytes(f[6:-1])      # drop AC + sub-cmd
        else:
            self._frag[idx] = bytes(f[5:-1])      # drop AC only
        if len(self._frag) == self._frag_count:
            data = b"".join(self._frag[i] for i in sorted(self._frag, reverse=True))
            self._frag.clear()
            return (self._frag_cmd, data)
        return None


# --------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------
class Po3Session:
    def __init__(self, writer=None, on_message=None) -> None:
        self.writer = writer
        self.on_message = on_message
        self.t = _Transport()
        self.authenticated = False
        self.r1 = b""

    def _flush(self) -> None:
        for frame in self.t.out:
            if self.writer:
                self.writer(frame)
        self.t.out = []

    def _send(self, payload: bytes) -> None:
        self.t.send_payload(payload)
        self._flush()

    def pop_writes(self) -> list[bytes]:
        w, self.t.out = self.t.out, []
        return w

    # ---- auth ----
    def start_auth(self) -> None:
        self.r1 = bytes(b & 0x7F for b in os.urandom(16))  # Java abs() of random
        self.authenticated = False
        self._send(bytes([AC, CMD_AUTH_FA]) + _c(self.r1))

    # ---- commands ----
    def get_battery(self):
        self._send(bytes([AC, CMD_BATTERY]))

    def set_time(self, now=None):
        self._send(set_time_payload(now))

    def get_time(self):
        self._send(bytes([AC, CMD_GET_TIME]))

    def get_device_info(self):
        self._send(bytes([AC, CMD_DEVICE_INFO]))

    def start_measure(self):
        self._send(bytes([AC, CMD_START_MEASURE]))

    def get_history(self):
        self._send(bytes([AC, CMD_MEM_COUNT]))

    # ---- RX ----
    def feed(self, chunk: bytes) -> None:
        for cmd, data in self.t.feed(chunk):
            self._dispatch(cmd, data)
        self._flush()

    def _dispatch(self, cmd: int, data: bytes) -> None:
        if cmd == CMD_AUTH_FB:
            if len(data) >= 48:
                self._send(bytes([AC, CMD_AUTH_FC]) + _auth_response(data[:48]))
            return
        if cmd == CMD_AUTH_OK:
            self.authenticated = True
        elif cmd == CMD_AUTH_FAIL:
            self.authenticated = False
        elif cmd == CMD_MEASURE_ACK:  # device ready -> echo AC A6
            self._send(bytes([AC, CMD_MEASURE_ACK]))
        if self.on_message:
            self.on_message(cmd, data)
