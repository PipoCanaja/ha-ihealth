"""Device-side model of the iHealth AM4 tracker, for tests.

Behaves like AaInsSet expects: classic BleCommProtocol frames with the 0xAA
family byte, IdentifyIns auth with getKey("AM4"), and bulk transfers that
only go on once the app has echoed each start/data/finish message.
"""
from __future__ import annotations

import os

from am4 import protocol as P
from fake_kn550bt import _classic_frames


def activity_block(y, mo, d, step_len, points):
    """[yy mm dd steplen 0 0] + 8-byte points + 4 trailing bytes (rejectBuffer)."""
    b = bytes([y, mo, d, step_len, 0, 0])
    for hh, mi, steps, cal in points:
        b += bytes([hh, mi]) + steps.to_bytes(4, "big") + cal.to_bytes(2, "big")
    return b


def sleep_block(y, mo, d, hh, mi, ss, levels):
    return bytes([y, mo, d, hh, mi, ss, 0, 0]) + bytes(levels)


def workout_record(y, mo, d, hh, mi, minutes, steps, km_int, km_dec, cal):
    r = bytes([1, y, mo, d, hh, mi]) + minutes.to_bytes(2, "big") + steps.to_bytes(4, "big")
    r += bytes([km_int, km_dec]) + cal.to_bytes(2, "big")
    return r + bytes(21 - len(r))


def sleep_stage_record(y, mo, d, hh, mi, minutes, eff10, is50):
    r = bytes([2, y, mo, d, hh, mi]) + minutes.to_bytes(2, "big") + eff10.to_bytes(2, "big")
    r += bytes([is50])
    return r + bytes(21 - len(r))


class FakeAM4:
    def __init__(self, battery_level=7, mode=1, steps=4321, calories=187,
                 activity=(), sleep=(), stage=b"", user_id=123456, chunk=40, ignore_fa=0, lose_echoes=(), mute_after_echoes=None,
                 resume_on_auth=False):
        self.battery_level = battery_level
        self.mode = mode
        self.steps = steps
        self.calories = calories
        self.activity = list(activity)   # blocks (without the 4 trailing bytes)
        self.sleep = list(sleep)
        self.stage = stage
        self.user_id = user_id
        self.chunk = chunk               # bytes per data message
        self.ignore_fa = ignore_fa       # FA requests ignored after connecting
        self.lose_echoes = set(lose_echoes)  # indexes of echo frames lost on the air
        self._echo_count = 0
        self.mute_after_echoes = mute_after_echoes  # stop answering (stuck tracker)
        self.resume_on_auth = resume_on_auth  # resume a transfer cut in a previous session
        self._seen_seqs: list[int] = []
        self.duplicates = 0
        self.device_id = os.urandom(16)
        self.seed = os.urandom(16)
        self.seq = 2
        self.out: list[bytes] = []
        self.received: list[int] = []
        self.acks = 0
        self.echoes: list[int] = []
        self.authenticated = False
        self.clock: bytes | None = None
        self._fb = {}; self._fc = 0; self._fn = 0
        self._queue: list[tuple[int, bytes]] = []   # bulk messages still to send
        self._waiting: int | None = None             # echo the tracker waits for
        self._last_tx_seq = 0
        self.dropped = 0                              # echoes ignored (bad seq)

    # ---- device -> app ----
    def tx(self, cmd, data=b""):
        fr, self.seq = _classic_frames(bytes([P.AA, cmd]) + data, self.seq)
        self._last_tx_seq = fr[-1][3]
        for f in fr:
            self.out += [f[i:i + 20] for i in range(0, len(f), 20)]

    def _next_bulk(self):
        if self._queue:
            cmd, data = self._queue.pop(0)
            self._waiting = cmd
            self.tx(cmd, data)
        else:
            self._waiting = None

    def _load_bulk(self, start, data_cmd, finish, blocks, start_payload=None):
        q = []
        for blk in blocks:
            head = blk[:self.chunk] if start_payload is None else start_payload
            rest = blk[self.chunk:] if start_payload is None else blk
            q.append((start, head + b"\x00\x00\x00\x09"))
            for i in range(0, len(rest), self.chunk):
                q.append((data_cmd, rest[i:i + self.chunk]))
        q.append((finish, b""))
        self._queue = q
        self._next_bulk()

    # ---- app -> device ----
    def write(self, frame: bytes):
        assert frame[0] == P.TX_HEAD, frame.hex()
        assert sum(frame[2:-1]) & 0xFF == frame[-1]
        assert frame[4] == P.AA, "every frame carries the AA family byte"
        if len(frame) == 6:
            self.acks += 1
            return
        is_echo = self._waiting is not None and len(frame) == 7 and frame[5] == self._waiting
        if self.mute_after_echoes is not None and self._echo_count >= self.mute_after_echoes:
            return
        if is_echo:
            idx, self._echo_count = self._echo_count, self._echo_count + 1
            if idx in self.lose_echoes:
                return                       # lost: no ACK, the app must send it again
        # transport ACK of every command frame: seq + 1
        ack = bytes([P.RX_HEAD, 3, 0xA0 + (frame[2] & 0x0F), (frame[3] + 1) & 0xFF, P.AA])
        self.out.append(ack + bytes([sum(ack[2:]) & 0xFF]))
        if frame[3] in self._seen_seqs:      # a re-send we already got
            self.duplicates += 1
            return
        self._seen_seqs = (self._seen_seqs + [frame[3]])[-8:]
        # like the real tracker: the reply numbering follows ours (seq + 3)
        self.seq = (frame[3] + 3) & 0xFF
        if self._waiting is not None and frame[3] != (self._last_tx_seq + 3) & 0xFF:
            # real AM4: an echo numbered out of sequence is ignored mid-transfer
            self.dropped += 1
            return
        f2 = frame[2]
        if f2 in (0, 0xF0):
            self._cmd(frame[5], bytes(frame[6:-1]))
            return
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
        if self._waiting is not None and cmd == self._waiting:
            self.echoes.append(cmd)
            self._next_bulk()
            return
        self.received.append(cmd)
        if self._waiting is not None and cmd not in (P.CMD_AUTH_FA, P.CMD_AUTH_FC):
            self.ignored_during_transfer = getattr(self, "ignored_during_transfer", 0) + 1
            return
        if cmd == P.CMD_AUTH_FA:
            if self.ignore_fa:
                self.ignore_fa -= 1
                return
            self.tx(P.CMD_AUTH_FB, self.device_id + os.urandom(16) + self.seed)
        elif cmd == P.CMD_AUTH_FC:
            ok = data == P.classic_auth_response(self.device_id + bytes(16) + self.seed, P.KA_AM4)
            self.authenticated = ok
            self.tx(P.CMD_AUTH_OK if ok else P.CMD_AUTH_FAIL)
            if ok and self.resume_on_auth:
                # seen on the real AM4: the unfinished activity transfer goes on
                self._load_bulk(P.CMD_ACTIVITY_START, P.CMD_ACTIVITY_DATA,
                                P.CMD_ACTIVITY_FINISH, self.activity[-1:])
        elif not self.authenticated:
            return
        elif cmd == P.CMD_QUERY_STATE:
            self.tx(cmd, bytes([self.mode, self.battery_level]))
        elif cmd == P.CMD_SYNC_TIME:
            self.clock = data
            self.tx(cmd)
        elif cmd == P.CMD_REALTIME:
            self.tx(cmd, self.steps.to_bytes(4, "big") + self.calories.to_bytes(2, "big"))
        elif cmd == P.CMD_USER_ID_GET:
            self.tx(cmd, self.user_id.to_bytes(4, "big"))
        elif cmd == P.CMD_ACTIVITY_SYNC:
            self._load_bulk(P.CMD_ACTIVITY_START, P.CMD_ACTIVITY_DATA, P.CMD_ACTIVITY_FINISH,
                            self.activity)
        elif cmd == P.CMD_SLEEP_SYNC:
            self._load_bulk(P.CMD_SLEEP_START, P.CMD_SLEEP_DATA, P.CMD_SLEEP_FINISH, self.sleep)
        elif cmd == P.CMD_STAGE_SYNC:
            blocks = [self.stage] if self.stage else []
            # 0B carries a header the app discards; all records come in 0C
            self._load_bulk(P.CMD_STAGE_START, P.CMD_STAGE_DATA, P.CMD_STAGE_FINISH,
                            blocks, start_payload=b"\x01\x02")
