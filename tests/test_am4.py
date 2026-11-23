"""AM4: key, decoders, session and client against a fake tracker."""
from __future__ import annotations

import asyncio
from datetime import datetime

import am4.client as client_mod
from am4 import protocol as P
from fake_am4 import FakeAM4, activity_block, sleep_block, sleep_stage_record, workout_record


def test_key_matches_docs():
    # docs/device_keys.md, row AM4
    assert P.KA_AM4.hex() == "995a085c06eaa5dcf2f4da075df4804b"


def test_firmware_number():
    assert P.firmware_number("1.3.8") == 138
    assert P.firmware_number("1.4.0") == 140
    assert P.firmware_number("") is None
    assert P.firmware_number("v1") is None


def test_decode_state_battery_rules():
    assert P.decode_state(bytes([1, 7]), 140) == P.State("activity", 80)   # fw > 137: +1
    assert P.decode_state(bytes([1, 9]), 140) == P.State("activity", 100)
    assert P.decode_state(bytes([0, 7]), 130) == P.State("sleep", 70)
    assert P.decode_state(bytes([2, 9]), 130) == P.State("flight", 100)
    assert P.decode_state(bytes([3, 12]), 140) == P.State("driving", None)
    assert P.decode_state(b"\x01", 140) is None


def test_decode_realtime():
    data = (123456).to_bytes(4, "big") + (321).to_bytes(2, "big")
    assert P.decode_realtime(data) == P.Realtime(123456, 321)


def test_decode_activity():
    blk = activity_block(26, 11, 23, 72, [(8, 0, 100, 5), (8, 5, 250, 12), (8, 10, 251, 12)])
    pts = P.decode_activity([blk])
    assert [p.time for p in pts] == [datetime(2026, 11, 23, 8, m) for m in (0, 5, 10)]
    assert [p.steps for p in pts] == [100, 250, 251]
    assert pts[0].step_length == 72 and pts[1].calories == 12
    assert P.decode_activity([blk[:-3]])[-1].steps == 250   # partial point ignored


def test_decode_sleep():
    (s,) = P.decode_sleep([sleep_block(26, 11, 22, 23, 30, 0, [0, 1, 1, 2, 2, 2, 1, 0])])
    assert s.start == datetime(2026, 11, 22, 23, 30)
    assert s.end == datetime(2026, 11, 23, 0, 10)
    assert (s.minutes(0), s.minutes(1), s.minutes(2)) == (10, 15, 15)


def test_decode_stage():
    data = workout_record(26, 11, 23, 9, 45, 30, 4200, 3, 2, 210)
    data += sleep_stage_record(26, 11, 23, 7, 0, 450, 912, 1)
    w, s = P.decode_stage(data + b"\x00" * 5)   # trailing garbage ignored
    assert (w.type, w.time, w.duration) == ("workout", datetime(2026, 11, 23, 9, 45), 30)
    assert w.values == {"steps": 4200, "distance": 3.2, "calories": 210}
    assert s.type == "sleep" and s.duration == 450
    assert s.values == {"efficiency": 91.2, "is_50min": 1}


def test_sync_time_payload():
    p = P.sync_time_payload(datetime(2026, 11, 22, 21, 5, 9))   # a Sunday
    assert p == bytes([0xAA, 0xA4, 26, 11, 22, 21, 5, 9, 1, 0, 0, 0, 0])
    assert P.sync_time_payload(datetime(2026, 11, 23, 0, 0, 0))[8] == 2   # Monday


def _pump(sess, dev):
    while dev.out:
        sess.feed(dev.out.pop(0))


def test_session_auth_and_bulk_echo():
    blk = activity_block(26, 11, 23, 70, [(h, 0, h * 100, h) for h in range(24)])
    dev = FakeAM4(activity=[blk], chunk=40)
    msgs = []
    sess = P.Am4Session(writer=dev.write, on_message=lambda c, d: msgs.append(c))
    sess.start_auth(); _pump(sess, dev)
    assert sess.authenticated and dev.authenticated
    assert dev.acks > 0   # FB is fragmented: transport ACKs with the AA byte
    sess.sync_activity(); _pump(sess, dev)
    assert P.CMD_ACTIVITY_FINISH in msgs
    # every start/data/finish message was echoed, in order
    assert dev.echoes[0] == P.CMD_ACTIVITY_START and dev.echoes[-1] == P.CMD_ACTIVITY_FINISH
    assert sess.blocks[P.CMD_ACTIVITY_START] == [blk]
    assert len(P.decode_activity(sess.blocks[P.CMD_ACTIVITY_START])) == 24


# ---------------------------------------------------------------- async client
class FakeChar:
    properties = ["write-without-response", "notify"]


class FakeServices:
    chars = {P.TX_UUID, P.RX_UUID, P.CHAR_FIRMWARE}

    def get_service(self, uuid):
        return object() if uuid == P.SERVICE_UUID else None

    def get_characteristic(self, uuid):
        return FakeChar() if uuid in self.chars else None


class FakeBleakClient:
    def __init__(self, dev: FakeAM4, firmware: bytes):
        self.dev = dev
        self.firmware = firmware
        self.is_connected = True
        self.cb = None
        self.services = FakeServices()

    async def read_gatt_char(self, uuid):
        assert uuid == P.CHAR_FIRMWARE
        return self.firmware

    async def start_notify(self, uuid, cb):
        assert uuid == P.RX_UUID
        self.cb = cb

    async def write_gatt_char(self, uuid, data, response):
        assert uuid == P.TX_UUID and response is False
        self.dev.write(bytes(data))
        loop = asyncio.get_running_loop()
        for i, chunk in enumerate(self.dev.out):
            loop.call_later(0.002 * (i + 1), self.cb, None, bytearray(chunk))
        self.dev.out = []

    async def disconnect(self):
        self.is_connected = False


class Dev:
    address = "AA:BB:CC:DD:EE:04"
    name = "AM4"


def _run(monkeypatch, dev, firmware=b"1.4.0\0", history=True):
    async def fake_connect(cls, ble_device, name, **kwargs):
        return FakeBleakClient(dev, firmware)

    monkeypatch.setattr(client_mod, "establish_connection", fake_connect)
    monkeypatch.setattr(client_mod, "CMD_TIMEOUT", 0.3)
    monkeypatch.setattr(client_mod, "BULK_IDLE_TIMEOUT", 0.3)

    async def run():
        async with client_mod.Am4Client(Dev()) as c:
            out = {
                "state": await c.get_state(),
                "user": await c.get_user_id(),
            }
            await c.sync_time()
            out["real"] = await c.get_realtime()
            if history:
                out["stage"] = await c.get_stage()
                out["activity"] = await c.get_activity()
                out["sleep"] = await c.get_sleep()
            return c, out

    return asyncio.run(run())


def test_client_full_session(monkeypatch):
    days = [
        activity_block(26, 11, 22, 70, [(h, m, h * 300 + m, h * 10) for h in range(24) for m in (0, 30)]),
        activity_block(26, 11, 23, 70, [(7, 0, 50, 2), (7, 5, 120, 6)]),
    ]
    sleep = [sleep_block(26, 11, 22, 23, 0, 0, [0, 1, 2, 2, 1, 0] * 10)]
    stage = workout_record(26, 11, 23, 9, 45, 30, 4200, 3, 2, 210)
    dev = FakeAM4(battery_level=4, activity=days, sleep=sleep, stage=stage)
    c, out = _run(monkeypatch, dev)
    assert c.firmware == "1.4.0" and c.fw == 140
    assert out["state"] == P.State("activity", 50)
    assert out["user"] == 123456
    assert out["real"] == P.Realtime(4321, 187)
    assert dev.clock is not None and dev.clock[0] >= 26
    assert [r.type for r in out["stage"]] == ["workout"]
    assert len(out["activity"]) == 48 + 2
    assert out["activity"][-1].time == datetime(2026, 11, 23, 7, 5)
    (s,) = out["sleep"]
    assert s.minutes(2) == 100 and s.end == datetime(2026, 11, 23, 4, 0)
    # never sends commands that change binding / settings / memory
    for forbidden in (P.CMD_RESET, P.CMD_USER_ID_SET, P.CMD_USER_INFO_SET):
        assert forbidden not in dev.received
    assert dev.received[:2] == [P.CMD_AUTH_FA, P.CMD_AUTH_FC]


def test_client_empty_history_and_old_firmware(monkeypatch):
    dev = FakeAM4(stage=workout_record(26, 11, 23, 9, 45, 30, 1, 0, 1, 1))
    c, out = _run(monkeypatch, dev, firmware=b"1.3.0")
    assert out["state"].battery == 70          # fw <= 137: level * 10
    assert out["stage"] == []                  # fw < 134: stage reports ignored
    assert out["activity"] == [] and out["sleep"] == []
