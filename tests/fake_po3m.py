"""Device-side model of the PO3M (inverse of the app logic), for tests."""
from __future__ import annotations

import os
import struct

from po3m.protocol import (
    APP_KEY, PRIME_P, RX_HEAD, Cipher, aes_ctr, algorithms, crc32, ctr_blocks, modes,
    xxtea_encrypt,
)


class FakePO3M:
    def __init__(self, live_frames=((97, 62), (98, 63), (98, 64))) -> None:
        self.dev_id = os.urandom(16)
        self.r2 = os.urandom(16)
        self.live_frames = live_frames
        self.key2 = None
        self.tx = 0
        self.out: list[bytes] = []
        self.received: list[int] = []

    def _frame(self, payload: bytes, enc: str) -> None:
        size = len(payload)
        lo, hi = size & 0xFF, size >> 8
        clear = payload + struct.pack("<I", crc32(payload))
        if enc == "ctr":
            start = self.tx
            self.tx += ctr_blocks(size)
            body = aes_ctr(clear, self.key2, self.r2[:12], start)
            cnt = self.tx - 1  # last block counter (the app tries both)
        else:  # cbc (FB challenge)
            e = Cipher(algorithms.AES(self.k1), modes.CBC(self.iv1)).encryptor()
            body, cnt = e.update(clear) + e.finalize(), 0
        f = bytes([RX_HEAD, lo, hi, ((lo ^ hi) & 0xFE) | (enc == "ctr")])
        f += body + struct.pack("<I", cnt)
        f += bytes([sum(f) & 0xFF])
        self.out += [f[i:i + 20] for i in range(0, len(f), 20)]  # MTU 23

    def write(self, f: bytes) -> None:
        size = f[1] | f[2] << 8
        assert sum(f[:-1]) & 0xFF == f[-1], "bad checksum"
        body = f[4:8 + size]
        if f[3] & 1:
            start = struct.unpack_from("<I", f, 8 + size)[0]
            clear = aes_ctr(body, self.key2, self.r2[:12], start)
        else:
            clear = body
        assert crc32(clear[:-4]) == struct.unpack("<I", clear[-4:])[0], "bad TAG"
        cmd, data = clear[0], clear[1:-4]
        self.received.append(cmd)
        if cmd == 0xFA:
            r1 = data
            self.k1, self.iv1 = r1[:8] + PRIME_P[:8], r1[8:] + PRIME_P[8:]
            k = xxtea_encrypt(self.dev_id, APP_KEY)
            self.expected_r2p = xxtea_encrypt(self.r2, k)
            self.key2 = (
                Cipher(algorithms.AES(k[:16]), modes.ECB()).encryptor()
                .update(self.expected_r2p[:16])
            )
            self.tx = struct.unpack("<I", self.r2[12:])[0] + 500
            self._frame(bytes([0xFB]) + self.r2 + xxtea_encrypt(r1, k) + self.dev_id + bytes(7), "cbc")
        elif cmd == 0xFC:
            self._frame(bytes([0xFD if data == self.expected_r2p else 0xFE]), "ctr")
        elif cmd == 0x21:
            self._frame(b"\x21", "ctr")
        elif cmd == 0x22:
            self._frame(bytes([0x22, 0, 87]), "ctr")
        elif cmd == 0x25:
            def s16(t: str) -> bytes:
                return t.encode().ljust(16, b"\0")
            self._frame(
                bytes([0x25, 0, 87, 26, 11, 22, 18, 30, 0]) + s16("com.jiuan.POV12")
                + s16("PO3M") + b"102" + b"100" + s16("iHealth")
                + s16("PO3M") + s16("ABCDEF123456"),
                "ctr",
            )
        elif cmd == 0x50:
            self._frame(b"\x50", "ctr")
            for spo2, pr in self.live_frames:
                self._frame(
                    bytes([0x52, spo2, pr, 9]) + struct.pack("<HH", 123, 4100)
                    + struct.pack("<HHH", 1000, 1200, 1100),
                    "ctr",
                )
        elif cmd == 0x40:
            self._frame(bytes([0x40, 2, 0]), "ctr")
        elif cmd == 0x41:
            def rec(t, o, p, pi):
                return bytes(t) + bytes([o, p, pi, 1, 0, 0])
            self._frame(
                bytes([0x41, 2, 0]) + rec([26, 11, 20, 8, 15, 0], 96, 70, 35)
                + rec([26, 11, 21, 9, 30, 5], 99, 58, 52),
                "ctr",
            )
