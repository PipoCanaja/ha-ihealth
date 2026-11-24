"""
iHealth AM4 (activity and sleep tracker) BLE protocol, pure Python.

Reconstructed from the decompiled iHealth app:
  com.ihealth.communication.control.{Am4Control, AmControlBase, AmProfile}
  com.ihealth.communication.ins.{AaInsSet, IdentifyIns, XXTEA}
  com.ihealth.communication.base.protocol.BleCommProtocol      ("classic")
  com.ihealth.communication.base.ble.BleConfig                 (UUIDs)
  com.ihealth.business.device.am.viewmodel.AM4ViewModel        (app sequence)

Same classic transport and IdentifyIns authentication as the PO3, with the
device-type byte 0xAA and the key getKey("AM4").

Bulk transfers (activity, sleep, stage reports) are sent by the tracker as a
series of messages: start / data... / finish. The app answers every one of
them by echoing ``AA <cmd>`` (on top of the transport ACK), otherwise the
tracker stops. All of them are accumulated here and decoded on "finish".
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta

try:
    from ..kn550bt.protocol import _derive_ka, classic_auth_response
    from ..po3classic.protocol import Po3Error, _c, _Transport
except ImportError:  # tests import the sub-packages as top-level modules
    from kn550bt.protocol import _derive_ka, classic_auth_response  # type: ignore[no-redef]
    from po3classic.protocol import Po3Error, _c, _Transport  # type: ignore[no-redef]

_LOGGER = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# GATT ("com.jiuan.AMV12"; "rec." = app writes, "sed." = notifications)
# --------------------------------------------------------------------------
SERVICE_UUID = "636f6d2e-6a69-7561-6e2e-414d56313200"
TX_UUID = "7265632e-6a69-7561-6e2e-414d56313200"
RX_UUID = "7365642e-6a69-7561-6e2e-414d56313200"
CHAR_FIRMWARE = "00002a26-0000-1000-8000-00805f9b34fb"

TX_HEAD = 0xB0
RX_HEAD = 0xA0
AA = 0xAA  # AaInsSet.G: device-type byte of the AM family

# Commands (second byte, after AA). Replies use the same byte unless noted.
CMD_HOUR_MODE_GET = 0x01
CMD_HOUR_MODE_SET = 0x02
CMD_SWIM_GET = 0x05
CMD_RANDOM = 0x09
CMD_STAGE_SYNC = 0x0A        # reply: 0B start, 0C data, 0D finish
CMD_STAGE_START = 0x0B
CMD_STAGE_DATA = 0x0C
CMD_STAGE_FINISH = 0x0D
CMD_RESET = 0xA1             # erases data, settings and user id (see Am4Settings)
CMD_USER_ID_GET = 0xA2
CMD_USER_ID_SET = 0xA3       # only to put back the id read before a reset
CMD_SYNC_TIME = 0xA4
CMD_USER_INFO_SET = 0xA5
CMD_ALARM_NUM = 0xA6         # reply: [count][id...]
CMD_ALARM_GET = 0xA7         # [id] -> [id hh mm repeat weekbits on]
CMD_ALARM_SET = 0xA8         # same 6 bytes
CMD_REMINDER_SET = 0xAA      # same 3 bytes as B3
CMD_REMINDER_GET = 0xB3      # reply: [hh mm on] (or minutes u16 when > 59)
CMD_ACTIVITY_SYNC = 0xAC     # reply: AB start, AD data, AE finish
CMD_ACTIVITY_START = 0xAB
CMD_ACTIVITY_DATA = 0xAD
CMD_ACTIVITY_FINISH = 0xAE
CMD_SLEEP_SYNC = 0xB0        # reply: AF start, B1 data, B2 finish
CMD_SLEEP_START = 0xAF
CMD_SLEEP_DATA = 0xB1
CMD_SLEEP_FINISH = 0xB2
CMD_QUERY_STATE = 0xB4       # reply: [mode][battery level 0..9]
CMD_USER_INFO_GET = 0xB6
CMD_BMR_SET = 0xB7           # [bmr u16]: no read command
CMD_PICTURE_GET = 0x10
CMD_PICTURE_SET = 0x11
CMD_REALTIME = 0xBF          # reply: [steps u32][calories u16] (today)
CMD_AUTH_FA = 0xFA
CMD_AUTH_FB = 0xFB
CMD_AUTH_FC = 0xFC
CMD_AUTH_OK = 0xFD
CMD_AUTH_FAIL = 0xFE

# start -> (data, finish) of each bulk transfer, and which list it fills
_BULK = {
    CMD_ACTIVITY_START: (CMD_ACTIVITY_DATA, CMD_ACTIVITY_FINISH),
    CMD_SLEEP_START: (CMD_SLEEP_DATA, CMD_SLEEP_FINISH),
    CMD_STAGE_START: (CMD_STAGE_DATA, CMD_STAGE_FINISH),
}
BULK_FINISH = {v[1]: k for k, v in _BULK.items()}
BULK_DATA = {v[0]: k for k, v in _BULK.items()}

# AmProfile.AM_DEVICE_MODE_*
MODES = {0: "sleep", 1: "activity", 2: "flight", 3: "driving"}
# AMSleepDataParsingUtil: one level every 5 minutes
SLEEP_LEVELS = {0: "awake", 1: "light", 2: "deep"}
SLEEP_STEP = timedelta(minutes=5)

# AaInsSet firmware thresholds (version with the dots removed: "1.3.8" -> 138)
FW_STAGE_DATA = 134          # L: stage reports are empty before this
FW_BATTERY_PLUS_ONE = 137    # battery level counts from 0 above this
FW_USER_INFO_SWIM = 138      # AaInsSet.M: A5 / B6 carry 2 more bytes (swim target)
USER_INFO_LEN = 19           # A5 / B6 payload before that

_KEY_AM4 = bytes.fromhex("1967a5449c30c3bd20dec808f8ea4d34")  # getKey("AM4")
KA_AM4 = _derive_ka(_KEY_AM4)


class Am4Error(Exception):
    pass


# --------------------------------------------------------------------------
# Decoders
# --------------------------------------------------------------------------
def firmware_number(version: str | None) -> int | None:
    """AaInsSet: Integer.parseInt(firmware.replace(".", ""))."""
    if not version:
        return None
    digits = version.strip().replace(".", "")
    return int(digits) if digits.isdigit() else None


def _dt(y, mo, d, h=0, mi=0, s=0) -> datetime | None:
    try:
        return datetime(2000 + y, mo, d, h, mi, s)
    except ValueError:
        return None


def _u16(b: bytes, i: int) -> int:
    return b[i] << 8 | b[i + 1]


def _u32(b: bytes, i: int) -> int:
    return b[i] << 24 | b[i + 1] << 16 | b[i + 2] << 8 | b[i + 3]


@dataclass(frozen=True, slots=True)
class State:
    mode: str | None
    battery: int | None   # percent


def decode_state(data: bytes, fw: int | None) -> State | None:
    """B4: [mode][battery 0..9] -> AaInsSet case 23."""
    if len(data) < 2:
        return None
    mode, level = data[0], data[1]
    if level > 9:
        battery = None
    elif fw is not None and fw > FW_BATTERY_PLUS_ONE:
        battery = min((level + 1) * 10, 100)
    else:
        battery = 100 if level == 9 else level * 10
    return State(MODES.get(mode), battery)


@dataclass(frozen=True, slots=True)
class Realtime:
    steps: int
    calories: int  # kcal burnt by activity (the app adds the BMR share itself)


def decode_realtime(data: bytes) -> Realtime | None:
    """BF (AM4): [steps u32 BE][calories u16 BE]."""
    if len(data) < 6:
        return None
    return Realtime(_u32(data, 0), _u16(data, 4))


@dataclass(frozen=True, slots=True)
class ActivityPoint:
    """One 5-minute point. Steps and calories are cumulative for the day."""

    time: datetime
    steps: int
    calories: int
    step_length: int  # cm


def decode_activity(blocks: list[bytes]) -> list[ActivityPoint]:
    """AaInsSet.i(): per day [yy mm dd steplen ? ?] then 8-byte points
    [hh mi steps u32 calories u16] (AM4 layout)."""
    points: list[ActivityPoint] = []
    for b in blocks:
        if len(b) < 6:
            continue
        y, mo, d, step_len = b[0], b[1], b[2], b[3]
        for i in range(6, len(b) - 7, 8):
            t = _dt(y, mo, d, b[i], b[i + 1])
            if t is None:
                continue
            points.append(ActivityPoint(t, _u32(b, i + 2), _u16(b, i + 6), step_len))
    return points


@dataclass(frozen=True, slots=True)
class SleepSession:
    start: datetime
    levels: list[int] = field(default_factory=list)  # one per 5 minutes

    @property
    def end(self) -> datetime:
        return self.start + SLEEP_STEP * len(self.levels)

    def minutes(self, level: int) -> int:
        return 5 * sum(1 for x in self.levels if x == level)


def decode_sleep(blocks: list[bytes]) -> list[SleepSession]:
    """AaInsSet.j(): [yy mm dd hh mi ss ? ?] then one level byte per 5 min."""
    out: list[SleepSession] = []
    for b in blocks:
        if len(b) < 8:
            continue
        start = _dt(*b[0:6])
        if start is None or len(b) == 8:
            continue
        out.append(SleepSession(start, list(b[8:])))
    return out


STAGE_RECORD = 21  # AM4 (17 on the AM3S)


@dataclass(frozen=True, slots=True)
class StageRecord:
    """A report the tracker computed itself (AaInsSet.k())."""

    type: str                      # workout | swim | sleep | summary
    time: datetime | None          # end of the stage (day for "summary")
    duration: int | None = None    # minutes
    values: dict = field(default_factory=dict)


def decode_stage(data: bytes) -> list[StageRecord]:
    out: list[StageRecord] = []
    for pos in range(0, len(data) - STAGE_RECORD + 1, STAGE_RECORD):
        r = data[pos:pos + STAGE_RECORD]
        kind = r[0]
        if kind == 0:  # swim
            out.append(StageRecord("swim", _dt(*r[1:7]), _u16(r, 7), {
                "turns": r[9], "pool_length": r[10], "stroke": r[11],
                "strokes": _u16(r, 12), "calories": _u16(r, 14),
                "cut_in": _u16(r, 16), "cut_out": _u16(r, 18), "process_flag": r[20],
            }))
        elif kind == 1:  # workout (AM4: 4-byte step count)
            out.append(StageRecord("workout", _dt(*r[1:6]), _u16(r, 6), {
                "steps": _u32(r, 8),
                "distance": float(f"{r[12]}.{r[13]}"),   # km, as the app builds it
                "calories": _u16(r, 14),
            }))
        elif kind == 2:  # sleep summary
            out.append(StageRecord("sleep", _dt(*r[1:6]), _u16(r, 6), {
                "efficiency": _u16(r, 8) / 10, "is_50min": r[10],
            }))
        elif kind == 3:  # daily summary ("page view")
            out.append(StageRecord("summary", _dt(*r[1:4]), None, {
                "steps": _u16(r, 4), "distance": _u16(r, 6), "calories": _u16(r, 8),
                "target": _u16(r, 10), "swim": _u16(r, 12),
            }))
    return out


# --------------------------------------------------------------------------
# Settings kept across a reset (A1)
# --------------------------------------------------------------------------
@dataclass(slots=True)
class Am4Settings:
    """What A1 erases, read back from the tracker just before it.

    The phone app recognises its tracker only by the user id stored in it
    (AM4ViewModel.b(): getUserId() compared with the account's id), and its
    pairing (AMSendRandomActivity) is: sendRandom, sync, A3 user id, B7 + A5
    profile, 02 hour mode, then the cloud binding. The cloud side is not
    touched by A1 sent from here, so writing the same values back is enough
    for the app to take the tracker as still paired.

    Raw reply bytes are kept: each set command takes the same layout as the
    matching get reply.
    """

    user_id: int
    user_info: bytes                     # B6 reply = A5 payload
    hour_mode: int | None = None         # 01 reply
    reminder: bytes | None = None        # B3 reply = AA payload
    alarms: list[bytes] = field(default_factory=list)  # A7 replies = A8 payloads
    picture: int | None = None           # 10 reply

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "user_info": self.user_info.hex(),
            "hour_mode": self.hour_mode,
            "reminder": self.reminder.hex() if self.reminder is not None else None,
            "alarms": [a.hex() for a in self.alarms],
            "picture": self.picture,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Am4Settings":
        return cls(
            user_id=int(d["user_id"]),
            user_info=bytes.fromhex(d["user_info"]),
            hour_mode=d.get("hour_mode"),
            reminder=bytes.fromhex(d["reminder"]) if d.get("reminder") else None,
            alarms=[bytes.fromhex(a) for a in d.get("alarms", [])],
            picture=d.get("picture"),
        )


def user_info_payload(raw: bytes, fw: int | None) -> bytes:
    """A5 from a B6 reply: [age steplen height gender kg kg/10 unit
    goal u32, goal/2 u32, goal/4 u32] (+ swim target hh mm from fw 138)."""
    n = USER_INFO_LEN + (2 if fw is not None and fw >= FW_USER_INFO_SWIM else 0)
    if len(raw) < n:
        raise Am4Error(f"user info too short: {raw.hex()}")
    return bytes([AA, CMD_USER_INFO_SET]) + raw[:n]


def bmr_from_user_info(raw: bytes) -> int:
    """AmControlBase.a(): revised Harris-Benedict, minus 2.

    Decompiled as is, the activity-level factor always ends up as 1.0.
    gender 0 = male. BigDecimal ROUND_HALF_UP.
    """
    age, height, gender = raw[0], raw[2], raw[3]
    weight = raw[4] + raw[5] / 10
    if gender == 0:
        v = 88.362 + 13.397 * weight + 4.799 * height - 5.677 * age
    else:
        v = 447.593 + 9.247 * weight + 3.098 * height - 4.33 * age
    v = abs(v)
    return int(v + 0.5) - 2


def sync_time_payload(now: datetime | None = None) -> bytes:
    """a4Ins(): [yy mm dd hh mi ss weekday 0 0 0 0], weekday 1 = Sunday."""
    now = now or datetime.now()
    weekday = now.isoweekday() % 7 + 1   # Calendar.DAY_OF_WEEK
    return bytes([AA, CMD_SYNC_TIME, now.year - 2000, now.month, now.day,
                  now.hour, now.minute, now.second, weekday, 0, 0, 0, 0])


# --------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------
class Am4Session:
    def __init__(self, writer=None, on_message=None) -> None:
        self.writer = writer
        self.on_message = on_message
        self.t = _Transport(family=AA, follow_seq=True, retransmit=True)
        self.authenticated = False
        self.r1 = b""
        # bulk transfers: start cmd -> finished blocks; current block per start
        self.blocks: dict[int, list[bytes]] = {k: [] for k in _BULK}
        self._current: dict[int, bytearray | None] = {k: None for k in _BULK}
        # last bulk message and the seq of our echo, to spot a re-sent message
        self._last_bulk: tuple[int, bytes] | None = None
        self._last_echo_seq: int | None = None
        self._echo_seqs: set[int] = set()   # our echoes not yet acknowledged

    def _flush(self) -> None:
        for frame in self.t.out:
            _LOGGER.debug("TX %s", frame.hex())
            if self.writer:
                self.writer(frame)
        self.t.out = []

    def _send(self, payload: bytes) -> None:
        self.t.send_payload(payload)
        self._flush()

    def _cmd(self, cmd: int) -> None:
        self._send(bytes([AA, cmd]))

    def retransmit(self) -> int:
        """Send again unacknowledged frames (call every ~0.25 s); returns how
        many were given up on."""
        dropped = self.t.resend_due()
        for frame in dropped:
            _LOGGER.debug("no ACK for %s, given up", frame.hex())
        self._flush()
        return len(dropped)

    def _echo_bulk(self, cmd: int, data: bytes) -> bool:
        """Echo a bulk message; False if it is a re-send of the previous one.

        When our echo is lost the tracker sends the whole message again (new
        sequence numbers). It must be echoed again but not stored twice.
        """
        key = (cmd, bytes(data))
        repeat = (
            key == self._last_bulk
            and self._last_echo_seq is not None
            and self._last_echo_seq in self.t.pending
        )
        if repeat:
            _LOGGER.debug("0x%02X sent again by the tracker: not stored twice", cmd)
        # A new message means the tracker accepted an echo of the previous one:
        # echoes still waiting for an ACK are obsolete. Re-sending them (always
        # "AA <cmd>") could be taken for the echo of the message now arriving.
        # For a repeat, the new echo replaces the previous one as well.
        for seq in self._echo_seqs:
            self.t.pending.pop(seq, None)
        self._last_bulk = key
        self._last_echo_seq = self.t.seq
        self._echo_seqs = {self.t.seq}
        self._cmd(cmd)
        return not repeat

    # ---- auth ----
    def start_auth(self) -> None:
        # IdentifyIns.identify(): random bytes, negatives made positive
        self.r1 = bytes((256 - b) & 0xFF if b >= 128 else b for b in os.urandom(16))
        self.authenticated = False
        self._send(bytes([AA, CMD_AUTH_FA]) + _c(self.r1))

    # ---- commands ----
    def query_state(self) -> None:
        self._cmd(CMD_QUERY_STATE)

    def get_user_id(self) -> None:
        self._cmd(CMD_USER_ID_GET)

    def sync_time(self, now: datetime | None = None) -> None:
        self._send(sync_time_payload(now))

    def get_realtime(self) -> None:
        self._cmd(CMD_REALTIME)

    def command(self, cmd: int, data: bytes = b"") -> None:
        """Any other command: AA <cmd> <data>."""
        self._send(bytes([AA, cmd]) + bytes(data))

    def reset(self) -> None:
        """a1Ins(): the 4 bytes after A1 are always FF (the id argument is unused)."""
        self.command(CMD_RESET, b"\xff\xff\xff\xff")

    def _start_bulk(self, cmd: int, start: int) -> None:
        self.blocks[start] = []
        self._current[start] = None
        self._cmd(cmd)

    def sync_activity(self) -> None:
        self._start_bulk(CMD_ACTIVITY_SYNC, CMD_ACTIVITY_START)

    def sync_sleep(self) -> None:
        self._start_bulk(CMD_SLEEP_SYNC, CMD_SLEEP_START)

    def sync_stage(self) -> None:
        self._start_bulk(CMD_STAGE_SYNC, CMD_STAGE_START)

    # ---- RX ----
    def feed(self, chunk: bytes) -> None:
        _LOGGER.debug("RX %s", chunk.hex())
        try:
            messages = self.t.feed(chunk)
        except Po3Error as err:
            raise Am4Error(str(err)) from err
        finally:
            self._flush()  # transport ACKs first, as BleCommProtocol does
        for cmd, data in messages:
            _LOGGER.debug("RX cmd 0x%02X data %s", cmd, data.hex())
            self._dispatch(cmd, data)
        self._flush()

    def _end_block(self, start: int) -> None:
        cur = self._current[start]
        if cur is not None:
            self.blocks[start].append(bytes(cur))
        self._current[start] = None

    def _dispatch(self, cmd: int, data: bytes) -> None:
        if cmd == CMD_AUTH_FB:
            if len(data) >= 48:
                self._send(bytes([AA, CMD_AUTH_FC]) + classic_auth_response(data[:48], KA_AM4))
            return
        if cmd == CMD_AUTH_OK:
            self.authenticated = True
        elif cmd == CMD_AUTH_FAIL:
            self.authenticated = False
        elif cmd in _BULK:
            if not self._echo_bulk(cmd, data):  # echo, the tracker waits for it
                return
            self._end_block(cmd)
            if cmd in (CMD_ACTIVITY_START, CMD_SLEEP_START) and len(data) >= 3:
                _LOGGER.debug("0x%02X: day 20%02d-%02d-%02d", cmd, data[0], data[1], data[2])
            if cmd == CMD_STAGE_START:
                self._current[cmd] = bytearray()         # start payload unused
            else:
                self._current[cmd] = bytearray(data[:-4])  # rejectBuffer()
        elif cmd in BULK_DATA:
            if not self._echo_bulk(cmd, data):
                return
            start = BULK_DATA[cmd]
            if self._current[start] is None:
                self._current[start] = bytearray()
            self._current[start] += data
        elif cmd in BULK_FINISH:
            self._echo_bulk(cmd, data)
            self._end_block(BULK_FINISH[cmd])
        if self.on_message:
            self.on_message(cmd, data)
