"""Device-side model of the KN-550BT (classic and stream profiles), for tests."""
from __future__ import annotations

import os

from kn550bt import protocol as P
from po3classic.protocol import _c


def _classic_frames(payload: bytes, seq: int):
    """Device -> app frames of BleCommProtocol (A0 head, 14-byte fragments)."""
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


def _stream_frame(payload: bytes, seq: int):
    n = len(payload)
    f = bytearray(n + 5)
    f[0] = P.RX_HEAD; f[1] = n + 2; f[2] = 0; f[3] = seq
    f[4:4 + n] = payload
    f[n + 4] = sum(f[2:n + 4]) & 0xFF
    return [bytes(f)], (seq + 2) & 0xFF


def rec10(y, mo, d, h, mi, sys_, dia, hr, arr=False, hsd=False):
    return bytes([y | (0x80 if arr else 0), mo | (0x80 if hsd else 0), d, h, mi,
                  sys_ - dia, dia, hr, 0, 0])


def rec11(y, mo, d, h, mi, s, sys_, dia, hr, flags=0x01, scheme=0):
    return bytes([y, mo, d, h, mi, s, sys_ - dia, dia, hr, scheme, flags])


class FakeKN550BT:
    def __init__(self, stream=False, protocol=P.PROTO_BPAV21, records=(), page=5,
                 battery=64):
        self.stream = stream
        self.protocol = protocol
        self.new_format = protocol == P.PROTO_BPAV21
        self.records = list(records)
        self.page = page
        self.battery = battery
        self.device_id = os.urandom(16)
        self.seed = os.urandom(16)
        self.seq = 2
        self.out: list[bytes] = []
        self.received: list[int] = []
        self.acks = 0
        self.authenticated = False
        self._rx = bytearray()
        self._fb = {}; self._fc = 0; self._fn = 0
        self._pos = 0

    # ---- device -> app ----
    def tx(self, cmd, data=b""):
        payload = bytes([P.A1, cmd]) + data
        if self.stream:
            fr, self.seq = _stream_frame(payload, self.seq)
        else:
            fr, self.seq = _classic_frames(payload, self.seq)
        for f in fr:
            self.out += [f[i:i + 20] for i in range(0, len(f), 20)]

    # ---- app -> device ----
    def write(self, chunk: bytes):
        if self.stream:
            self._rx += chunk
            while len(self._rx) >= 2 and len(self._rx) >= self._rx[1] + 3:
                total = self._rx[1] + 3
                f = bytes(self._rx[:total]); del self._rx[:total]
                assert f[0] == P.TX_HEAD and sum(f[2:-1]) & 0xFF == f[-1], f.hex()
                assert f[4] == P.A1
                self._cmd(f[5], f[6:-1])
            return
        frame = chunk
        assert frame[0] == P.TX_HEAD, frame.hex()
        assert sum(frame[2:-1]) & 0xFF == frame[-1]
        if len(frame) == 6:
            assert frame[4] == P.A1, "ACK must carry the A1 family byte"
            self.acks += 1
            return
        assert frame[4] == P.A1
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
        self.received.append(cmd)
        if cmd == P.CMD_AUTH_FA:
            if self.stream:
                r1 = data[:16]
                self._ka = P.xxtea2_encrypt(self.device_id, P.KEY_STREAM)
                r1s = P.xxtea2_encrypt(r1, self._ka)
                self.tx(P.CMD_AUTH_FB, self.seed + r1s + self.device_id)
            else:
                self.tx(P.CMD_AUTH_FB, self.device_id + os.urandom(16) + self.seed)
        elif cmd == P.CMD_AUTH_FC:
            if self.stream:
                ok = data == P.xxtea2_encrypt(self.seed, self._ka)
            else:
                ka = P.classic_ka(self.protocol)
                ok = data == P.classic_auth_response(self.device_id + bytes(16) + self.seed, ka)
            self.authenticated = ok
            self.tx(P.CMD_AUTH_OK if ok else P.CMD_AUTH_FAIL)
        elif not self.authenticated:
            return
        elif cmd == P.CMD_BATTERY:
            self.tx(P.CMD_BATTERY, bytes([self.battery]))
        elif cmd == P.CMD_FUNCTION_INFO:
            self.clock = tuple(data[:6])
            self.tx(P.CMD_FUNCTION_INFO, bytes([0, 0x13, 0, 0, 0, 0, 0x07, 0, 0]))
        elif cmd == P.CMD_OFFLINE_NUM:
            self._pos = 0
            self.tx(P.CMD_OFFLINE_NUM, bytes([1, len(self.records)]))
        elif cmd in (P.CMD_OFFLINE_DATA, P.CMD_OFFLINE_DATA_NEW):
            if (cmd == P.CMD_OFFLINE_DATA_NEW) != self.new_format:
                return  # wrong format for this firmware: no answer
            chunk = self.records[self._pos:self._pos + self.page]
            self._pos += len(chunk)
            more = len(self.records) - self._pos
            self.tx(cmd, bytes([1, min(more, 255)]) + b"".join(chunk))
        elif cmd == P.CMD_GET_TIME:
            self.tx(P.CMD_GET_TIME, bytes([26, 11, 22, 21, 5, 9]))
        elif cmd == P.CMD_OFFLINE_OVER:
            self.records = []
            self.tx(P.CMD_OFFLINE_OVER)
