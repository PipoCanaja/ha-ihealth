"""
iHealth PO3M (Air) - BLE protocol, pure Python (no BLE dependency).

Reconstructed from the decompiled iHealth app (com.ihealth.sdk.command.po3m,
AuthCommand, ProtocolWrapper, ProtocolParser, XXTEA2).

Requires: cryptography (already shipped with Home Assistant).
"""
from __future__ import annotations

import os
import struct
import zlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# --------------------------------------------------------------------------
# GATT
# --------------------------------------------------------------------------
SERVICE_UUID = "0000fc83-0000-1000-8000-00805f9b34fb"
WRITE_CHAR_UUID = "7265632e-6a69-7561-6e2e-504f56313200"   # "rec.jiuan.POV12"
NOTIFY_CHAR_UUID = "7365642e-6a69-7561-6e2e-504f56313200"  # "sed.jiuan.POV12"

# --------------------------------------------------------------------------
# Constants found in the app
# --------------------------------------------------------------------------
APP_KEY = bytes([0x16, 0x52, 0x12, 0xA2, 0xBD, 0x0B, 0x09, 0x3F,
                 0x2D, 0x1E, 0x96, 0x8D, 0x99, 0xD4, 0x3C, 0xBF])
PRIME_P = b"com.jiuan.v1.0.0"

TX_HEAD = 0xB0
RX_HEAD = 0xA0

# Command IDs
CMD_AUTH_R1 = 0xFA        # app -> dev : FA + R1(16)            (clear)
CMD_AUTH_CHALLENGE = 0xFB  # dev -> app : AES-CBC(Key1)          (64 bytes)
CMD_AUTH_R2 = 0xFC        # app -> dev : FC + R2'(20)           (AES-CTR)
CMD_AUTH_OK = 0xFD
CMD_AUTH_FAIL = 0xFE
CMD_GET_TIME = 0x20
CMD_SET_TIME = 0x21
CMD_GET_BATTERY = 0x22
CMD_NOTIFY_BATTERY = 0x24  # unsolicited from device
CMD_GET_DEVICE_INFO = 0x25
CMD_MEMORY_COUNT = 0x40
CMD_MEMORY_READ = 0x41
CMD_MEMORY_DELETE = 0x42
CMD_START_MEASURE = 0x50
CMD_LIVE_DATA = 0x52

M32 = 0xFFFFFFFF
DELTA = 0x9E3779B9


class Po3mError(Exception):
    pass


# --------------------------------------------------------------------------
# Primitives
# --------------------------------------------------------------------------
def crc32(data: bytes) -> int:
    """The app's bit-reflected CRC (poly 0x04C11DB7) == standard CRC-32."""
    return zlib.crc32(data) & M32


def _u32s(data: bytes, with_len: bool) -> list[int]:
    n = (len(data) + 3) // 4
    v = list(struct.unpack(f"<{n}I", data.ljust(n * 4, b"\0")))
    if with_len:
        v.append(len(data))
    return v


def _xxtea_key(key: bytes) -> list[int]:
    return list(struct.unpack("<4I", key[:16].ljust(16, b"\0")))


def _mx(s: int, y: int, z: int, p: int, e: int, k: list[int]) -> int:
    return ((((z >> 5) ^ (y << 2)) + ((y >> 3) ^ (z << 4)))
            ^ ((s ^ y) + (k[(p & 3) ^ e] ^ z))) & M32


def xxtea_encrypt(data: bytes, key: bytes) -> bytes:
    """XXTEA2.encrypt: input length is appended as an extra word (16 -> 20 bytes)."""
    if not data:
        return data
    v, k = _u32s(data, True), _xxtea_key(key)
    n = len(v)
    s, z = 0, v[n - 1]
    for _ in range(6 + 52 // n):
        s = (s + DELTA) & M32
        e = (s >> 2) & 3
        for p in range(n - 1):
            v[p] = (v[p] + _mx(s, v[p + 1], z, p, e, k)) & M32
            z = v[p]
        v[n - 1] = (v[n - 1] + _mx(s, v[0], z, n - 1, e, k)) & M32
        z = v[n - 1]
    return struct.pack(f"<{n}I", *v)


def xxtea_decrypt(data: bytes, key: bytes) -> bytes | None:
    """XXTEA2.decrypt: strips the trailing length word (20 -> 16 bytes)."""
    if not data:
        return data
    v, k = _u32s(data, False), _xxtea_key(key)
    n = len(v)
    s = ((6 + 52 // n) * DELTA) & M32
    y = v[0]
    while s:
        e = (s >> 2) & 3
        for p in range(n - 1, 0, -1):
            v[p] = (v[p] - _mx(s, y, v[p - 1], p, e, k)) & M32
            y = v[p]
        v[0] = (v[0] - _mx(s, y, v[n - 1], 0, e, k)) & M32
        y = v[0]
        s = (s - DELTA) & M32
    total = n * 4
    length = v[-1]
    if length < total - 7 or length > total - 4:
        return None
    return struct.pack(f"<{n}I", *v)[:length]


def _aes_ecb(key: bytes, block: bytes) -> bytes:
    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return enc.update(block) + enc.finalize()


def aes_ctr(data: bytes, key: bytes, nonce12: bytes, start: int) -> bytes:
    """Manual CTR as in AuthCommand.g(): block = nonce(12) || BE32(counter)."""
    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    out = bytearray()
    ctr = start & M32
    for i in range(0, len(data), 16):
        ks = enc.update(nonce12 + struct.pack(">I", ctr))
        chunk = data[i:i + 16]
        out += bytes(a ^ b for a, b in zip(chunk, ks))
        ctr = (ctr + 1) & M32
    return bytes(out)


def aes_cbc_decrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    return dec.update(data) + dec.finalize()


def ctr_blocks(payload_len: int) -> int:
    """Number of AES blocks for cmd+data(+4 bytes TAG)."""
    return (payload_len + 19) // 16


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
    time_calibrated: int


def _ts(b: bytes, off: int = 0) -> datetime | None:
    try:
        y, mo, d, h, mi, s = b[off:off + 6]
        return datetime(2000 + y, mo, d, h, mi, s)
    except (ValueError, IndexError):
        return None


def _cstr(b: bytes, off: int, maxlen: int = 16) -> str:
    raw = b[off:off + maxlen].split(b"\0", 1)[0]
    return raw.decode("utf-8", "replace")


def _ver(b: bytes, off: int) -> str:
    return ".".join(chr(c) for c in b[off:off + 3]) if len(b) >= off + 3 else ""


def decode_live(data: bytes, last_pi: float | None = None) -> LiveData | None:
    """0x52: [spo2][pr][strength][AC lo hi][DC lo hi][wave0 lo hi][wave1..][wave2..]"""
    if len(data) < 9:
        return None
    spo2, pr, strength = data[0], data[1], data[2]
    if spo2 > 100 or pr > 250:
        return None
    ac = data[3] | data[4] << 8
    dc = data[5] | data[6] << 8
    pi = round(ac / dc * 1000) / 10 if (spo2 and pr and dc) else 0.0
    if not 0.2 <= pi <= 20.0:
        pi = last_pi
    wave = [data[7 + 2 * i] | data[8 + 2 * i] << 8
            for i in range(3) if 8 + 2 * i < len(data)]
    return LiveData(spo2, pr, strength, pi, wave)


def decode_battery(data: bytes) -> dict:
    """0x22: [status][level %]"""
    return {"battery_status": data[0], "battery": data[1]}


def decode_device_info(data: bytes) -> dict:
    """0x25: status, battery, time, then (>=94 bytes) 16-byte strings."""
    info = {"battery_status": data[0], "battery": data[1], "time": _ts(data, 2)}
    if len(data) >= 94:
        info.update(
            protocol=_cstr(data, 8), accessory_name=_cstr(data, 24),
            firmware=_ver(data, 40), hardware=_ver(data, 43),
            manufacturer=_cstr(data, 46), model=_cstr(data, 62),
            serial=_cstr(data, 78),
        )
    return info


def decode_history_chunk(data: bytes) -> tuple[bool, list[HistoryRecord]]:
    """0x41: [?][more flag] + n * 12-byte records. Returns (more, records)."""
    more = len(data) >= 2 and data[1] != 0
    body = data[2:]
    recs = []
    for i in range(len(body) // 12):
        r = body[i * 12:(i + 1) * 12]
        recs.append(HistoryRecord(_ts(r), r[6], r[7], round(r[8] / 10, 1), r[9]))
    return more, recs


def set_time_payload(now: datetime | None = None) -> bytes:
    now = now or datetime.now()
    if now.year < 2023:
        now = datetime(2023, 1, 1)
    return bytes([CMD_SET_TIME, now.year - 2000, now.month, now.day,
                  now.hour, now.minute, now.second])


# --------------------------------------------------------------------------
# Session: framing + crypto state
# --------------------------------------------------------------------------
class Po3mSession:
    """
    Feed notifications with `feed()`, get frames to write from `pop_writes()`
    or via the `writer` callback. Decoded (cmd, data) tuples go to `on_message`.
    """

    def __init__(self, writer: Callable[[bytes], None] | None = None,
                 on_message: Callable[[int, bytes], None] | None = None) -> None:
        self.writer = writer
        self.on_message = on_message
        self._rx = bytearray()
        self._pending_writes: list[bytes] = []
        self.r1 = b""
        self.key1 = self.iv1 = b""
        self.key2 = b""
        self.nonce = b""
        self.count_init = self.tx_count = self.rx_count = 0
        self.authenticated = False
        self.device_id = b""

    # ---------------- TX ----------------
    def _emit(self, frame: bytes) -> None:
        if self.writer:
            self.writer(frame)
        else:
            self._pending_writes.append(frame)

    def pop_writes(self) -> list[bytes]:
        w, self._pending_writes = self._pending_writes, []
        return w

    def build_frame(self, payload: bytes) -> bytes:
        """ProtocolWrapper.i(): B0 | len LE16 | hdr | body | CNT LE32 | sum8"""
        size = len(payload)
        lo, hi = size & 0xFF, (size >> 8) & 0xFF
        encrypted = payload[0] not in (CMD_AUTH_R1, CMD_AUTH_CHALLENGE)
        hdr = ((lo ^ hi) & 0xFE) | int(encrypted)
        clear = payload + struct.pack("<I", crc32(payload))
        if encrypted:
            if not self.key2:
                raise Po3mError("session key not derived yet")
            start = (self.tx_count + 1) & M32
            body = aes_ctr(clear, self.key2, self.nonce, start)
            self.tx_count = (start + ctr_blocks(size)) & M32
            cnt = start
        else:
            body, cnt = clear, 0
        frame = bytes([TX_HEAD, lo, hi, hdr]) + body + struct.pack("<I", cnt)
        return frame + bytes([sum(frame) & 0xFF])

    def send(self, payload: bytes) -> bytes:
        frame = self.build_frame(payload)
        self._emit(frame)
        return frame

    def start_auth(self) -> bytes:
        # App: Java Random, negative bytes negated -> 0..127 (0x80 stays 0x80)
        self.r1 = bytes(b & 0x7F for b in os.urandom(16))
        self.key1 = self.r1[:8] + PRIME_P[:8]
        self.iv1 = self.r1[8:16] + PRIME_P[8:16]
        self.authenticated = False
        self.key2 = b""
        return self.send(bytes([CMD_AUTH_R1]) + self.r1)

    # ---------------- RX ----------------
    def feed(self, chunk: bytes) -> None:
        """Feed one notification (frames may be split across notifications)."""
        if not self._rx and (not chunk or chunk[0] != RX_HEAD):
            return  # garbage / not a frame start
        self._rx += chunk
        while len(self._rx) >= 3:
            if self._rx[0] != RX_HEAD:
                self._rx.clear()
                return
            total = (self._rx[1] | self._rx[2] << 8) + 13
            if len(self._rx) < total:
                return
            frame, self._rx = bytes(self._rx[:total]), self._rx[total:]
            self._handle_frame(frame)

    def _handle_frame(self, f: bytes) -> None:
        if (sum(f[:-1]) & 0xFF) != f[-1]:
            raise Po3mError(f"bad checksum: {f.hex()}")
        size = f[1] | f[2] << 8
        is_ctr = bool(f[3] & 1)
        body = f[4:4 + size + 4]
        cnt = struct.unpack_from("<I", f, 4 + size + 4)[0]

        if is_ctr:
            if not self.key2:
                raise Po3mError("encrypted frame before key derivation")
            blocks = ctr_blocks(size)
            # CNT may be first or last block counter: the app tries both.
            for start in ((cnt - blocks + 1) & M32, cnt):
                clear = aes_ctr(body, self.key2, self.nonce, start)
                if crc32(clear[:-4]) == struct.unpack("<I", clear[-4:])[0]:
                    self.rx_count = (start + blocks - 1) & M32
                    self._dispatch(clear[0], clear[1:-4])
                    return
            raise Po3mError(f"CTR decrypt/TAG failed: {f.hex()}")

        # Not CTR: either the FB challenge (AES-CBC with Key1) or clear text
        if self.key1 and len(body) % 16 == 0 and not self.authenticated:
            clear = aes_cbc_decrypt(body, self.key1, self.iv1)
            if clear[0] == CMD_AUTH_CHALLENGE:
                if crc32(clear[:-4]) != struct.unpack("<I", clear[-4:])[0]:
                    raise Po3mError("FB TAG mismatch")
                self._handle_challenge(clear)
                return
        self._dispatch(body[0], body[1:size])

    def _handle_challenge(self, fb: bytes) -> None:
        """AuthCommand.j() + m(): verify device, derive Key2/Nonce/counters, send FC."""
        r2, r1p, dev_id = fb[1:17], fb[17:37], fb[37:53]
        k = xxtea_encrypt(dev_id, APP_KEY)               # 20 bytes
        r1_back = (xxtea_decrypt(r1p, k) or b"")[:16]
        if r1_back != self.r1:
            raise Po3mError("device authentication failed (R1 mismatch)")
        r2p = xxtea_encrypt(r2, k)                         # 20 bytes
        self.key2 = _aes_ecb(k[:16], r2p[:16])
        self.nonce = r2[:12]
        self.count_init = struct.unpack("<I", r2[12:16])[0]
        self.tx_count = self.rx_count = self.count_init
        self.device_id = dev_id
        self.send(bytes([CMD_AUTH_R2]) + r2p)

    def _dispatch(self, cmd: int, data: bytes) -> None:
        if cmd == CMD_AUTH_OK:
            self.authenticated = True
        elif cmd == CMD_AUTH_FAIL:
            self.authenticated = False
        if self.on_message:
            self.on_message(cmd, data)
