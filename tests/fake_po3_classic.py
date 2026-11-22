"""Device-side model of the classic PO3 (POV11), for tests."""
from __future__ import annotations

import os
import struct

from po3classic import protocol as P


def _dev_frames(cmd, data, seq):
    payload = bytes([P.AC, cmd]) + data
    first, rest = payload[0], payload[1:]
    frames = []
    if len(payload) <= 15:
        n = len(payload)
        f = bytearray(n + 5)
        f[0] = P.RX_HEAD; f[1] = n + 2; f[2] = 0; f[3] = seq; f[4] = first
        f[5:5 + len(rest)] = rest
        f[n + 4] = sum(f[2:n + 4]) & 0xFF
        frames.append(bytes(f)); seq = (seq + 2) & 0xFF
    else:
        size = len(rest); full = size // 14
        sizes = [14] * full + [size % 14]; pos = 0
        for i6, seg in enumerate(sizes):
            f = bytearray(seg + 6)
            f[0] = P.RX_HEAD; f[1] = seg + 3
            f[2] = ((full << 4) + full) - i6; f[3] = seq; f[4] = first
            f[5:5 + seg] = rest[pos:pos + seg]; pos += seg
            f[seg + 5] = sum(f[2:seg + 5]) & 0xFF
            frames.append(bytes(f)); seq = (seq + 2) & 0xFF
    return frames, seq


class FakePO3Classic:
    def __init__(self, live=((97, 61), (98, 62), (98, 63))):
        self.device_id = os.urandom(16)
        self.third = os.urandom(16)
        self.live = live
        self.seq = 2
        self.out = []
        self.acks = 0
        self._fb = {}; self._fc = 0; self._fn = 0

    def tx(self, cmd, data=b""):
        fr, self.seq = _dev_frames(cmd, data, self.seq)
        for f in fr:
            self.out += [f[i:i + 20] for i in range(0, len(f), 20)]

    def write(self, frame):
        assert frame[0] == P.TX_HEAD, frame.hex()
        if len(frame) == 6:
            self.acks += 1
            return
        f2 = frame[2]
        if f2 in (0, 0xF0):
            self._cmd(frame[5], bytes(frame[6:-1]))
        else:
            count = (f2 >> 4) + 1; idx = f2 & 0x0F
            if idx == count - 1:
                self._fb = {}; self._fc = frame[5]; self._fn = count
                self._fb[idx] = bytes(frame[6:-1])
            else:
                self._fb[idx] = bytes(frame[5:-1])
            if len(self._fb) == self._fn:
                data = b"".join(self._fb[i] for i in sorted(self._fb, reverse=True))
                self._cmd(self._fc, data)

    def _cmd(self, cmd, data):
        if cmd == P.CMD_AUTH_FA:
            self.tx(P.CMD_AUTH_FB, self.device_id + os.urandom(16) + self.third)
        elif cmd == P.CMD_AUTH_FC:
            exp = P._auth_response(self.device_id + b"\0" * 16 + self.third)
            self.tx(P.CMD_AUTH_OK if data == exp else P.CMD_AUTH_FAIL)
        elif cmd == P.CMD_BATTERY:
            self.tx(P.CMD_BATTERY, bytes([77]))
        elif cmd == P.CMD_GET_TIME:
            self.tx(P.CMD_GET_TIME, bytes([26, 11, 22, 19, 30, 0]))
        elif cmd == P.CMD_START_MEASURE:
            self.tx(P.CMD_MEASURE_ACK)
            for spo2, hr in self.live:
                self.tx(P.CMD_LIVE, bytes([spo2, hr, 9])
                        + struct.pack("<HH", 120, 4000)
                        + struct.pack("<HHH", 900, 1100, 1000))
