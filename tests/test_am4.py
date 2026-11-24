"""AM4: key, decoders, session and client against a fake tracker."""
from __future__ import annotations

import asyncio

import pytest
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
    assert dev.dropped == 0   # echoes carry the sequence number the tracker expects
    assert len(P.decode_activity(sess.blocks[P.CMD_ACTIVITY_START])) == 24


# ---------------------------------------------------------------- async client
class FakeChar:
    properties = ["write-without-response", "notify"]   # as on the real AM4? (logged)


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
        self.responses = set()

    async def read_gatt_char(self, uuid):
        assert uuid == P.CHAR_FIRMWARE
        return self.firmware

    async def start_notify(self, uuid, cb):
        assert uuid == P.RX_UUID
        self.cb = cb

    async def write_gatt_char(self, uuid, data, response):
        assert uuid == P.TX_UUID
        self.responses.add(response)
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
    monkeypatch.setattr(client_mod, "AUTH_TIMEOUT", 0.3)
    monkeypatch.setattr(client_mod, "SETTLE_DELAY", 0.01)
    monkeypatch.setattr(client_mod, "QUIET_SETTLE", 0.05)
    monkeypatch.setattr(client_mod, "CLOSE_GRACE", 0.2)

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


def test_echo_sequence_follows_device_real_capture():
    """Frames captured from a real AM4 (fw 137): the AD echo must use seq 0x25
    (last fragment 0x22 + 3), as BleCommProtocol does; 0x19 was ignored."""
    out = []
    sess = P.Am4Session(writer=out.append)
    sess.blocks[P.CMD_ACTIVITY_START] = []
    for hexframe in (
        "a00e0018aaab19081b4c08060000000003",
        "a011441aaaad171900002477019e171e00002478",
        "a011431caa77019e172300002477019e172900d3",
        "a011421eaa002477019e172e00002477019e17da",
        "a0114120aa3300002477019e1738000024770163",
        "a0044022aa9eaa",
    ):
        sess.feed(bytes.fromhex(hexframe))
    assert out[0].hex() == "b003a019aa63"           # ACK of AB, as logged
    assert out[1].hex() == "b004001baaab70"         # AB echo, seq 0x19 + 2
    assert out[-2].hex() == "b003a023aa6d"          # ACK of the last fragment
    assert out[-1][:4].hex() == "b0040025"          # AD echo, seq 0x22 + 3
    (blk,) = sess.blocks[P.CMD_ACTIVITY_START] or [bytes(sess._current[P.CMD_ACTIVITY_START])]
    pts = P.decode_activity([blk])
    assert pts[0].time == datetime(2025, 8, 27, 23, 25)
    assert (pts[0].steps, pts[0].calories, pts[0].step_length) == (9335, 414, 76)
    assert len(pts) == 7


def test_client_gives_up_quickly_on_unanswered_auth(monkeypatch):
    """Real tracker: once the link is stuck, retries on it never work; the
    client must fail fast (Am4AuthTimeout) so the coordinator reconnects."""
    import pytest

    dev = FakeAM4(ignore_fa=10)
    with pytest.raises(client_mod.Am4AuthTimeout):
        _run(monkeypatch, dev, history=False)
    assert dev.received.count(P.CMD_AUTH_FA) == 1      # resends are de-duplicated


def test_lost_echo_is_sent_again(monkeypatch):
    """Real capture: some writes are lost; the app must resend unacknowledged
    frames (BleCommProtocol timer) and never store a re-sent message twice."""
    blk = activity_block(26, 11, 23, 70, [(h, m, h * 60 + m, h) for h in range(6) for m in (0, 20, 40)])
    dev = FakeAM4(activity=[blk], chunk=40, lose_echoes={1, 3})
    monkeypatch.setattr(P._Transport, "RESEND_FIRST", 0.05)
    monkeypatch.setattr(P._Transport, "RESEND_EVERY", 0.05)
    c, out = _run(monkeypatch, dev)
    assert dev.dropped == 0
    assert len(out["activity"]) == 18                  # nothing lost, nothing doubled
    assert [p.steps for p in out["activity"]] == sorted(p.steps for p in out["activity"])


def test_resent_message_not_stored_twice():
    """The tracker re-sends a whole message (new seqs) when our echo is lost."""
    out = []
    sess = P.Am4Session(writer=out.append)
    sess.blocks[P.CMD_ACTIVITY_START] = []
    for hexframe in (
        "a00e0018aaab19081b4c08060000000003",
        "a011441aaaad171900002477019e171e00002478",
        "a011431caa77019e172300002477019e172900d3",
        "a011421eaa002477019e172e00002477019e17da",
        "a0114120aa3300002477019e1738000024770163",
        "a0044022aa9eaa",
        # echo lost -> same AD again, seqs 0x24.. (captured)
        "a0114424aaad171900002477019e171e00002482",
        "a0114326aa77019e172300002477019e172900dd",
        "a0114228aa002477019e172e00002477019e17e4",
        "a011412aaa3300002477019e173800002477016d",
        "a004402caa9eb4",
    ):
        sess.feed(bytes.fromhex(hexframe))
    echoes = [f for f in out if len(f) == 7 and f[5] == P.CMD_ACTIVITY_DATA]
    assert [f[3] for f in echoes] == [0x25, 0x2F]      # echoed both times
    assert len(sess._current[P.CMD_ACTIVITY_START]) == 6 + 56   # stored once


def test_writes_without_response_like_the_app(monkeypatch):
    """Real tracker via ESPHome proxy: acknowledged writes are too slow (the
    tracker re-sends everything), so write-without-response is used whenever
    the characteristic offers it, as the app does."""
    clients = []
    orig = FakeBleakClient.__init__

    def init(self, *a, **k):
        orig(self, *a, **k)
        clients.append(self)

    monkeypatch.setattr(FakeBleakClient, "__init__", init)
    monkeypatch.setattr(FakeChar, "properties", ["write-without-response", "write"])  # real AM4
    _, out = _run(monkeypatch, FakeAM4(), history=False)
    assert out["real"] == P.Realtime(4321, 187)
    assert clients[0].responses == {False}
    monkeypatch.setattr(FakeChar, "properties", ["write", "notify"])
    _run(monkeypatch, FakeAM4(), history=False)
    assert clients[1].responses == {True}


def test_message_repeated_with_same_seqs_is_ignored():
    """Captured 23:16:30: the AD fragments 0x40..0x48 come again with the same
    seqs. BleUnPackageData ACKs them but hands the message over only once."""
    out, msgs = [], []
    sess = P.Am4Session(writer=out.append, on_message=lambda c, d: msgs.append(c))
    sess.blocks[P.CMD_ACTIVITY_START] = []
    for hexframe in (
        "a00e003aaaab19081b4c081c000000003b",
        "a0114440aaad171900002477019e171e0000249e",
        "a0114342aa77019e172300002477019e172900f9",
        "a0114244aa002477019e172e00002477019e1700",
        "a0114146aa3300002477019e1738000024770189",
        "a0044048aa9ed0",
        # same fragments again, same seqs
        "a0114342aa77019e172300002477019e172900f9",
        "a0114244aa002477019e172e00002477019e1700",
        "a0114146aa3300002477019e1738000024770189",
        "a0044048aa9ed0",
    ):
        sess.feed(bytes.fromhex(hexframe))
    assert msgs.count(P.CMD_ACTIVITY_DATA) == 1
    echoes = [f for f in out if len(f) == 7 and f[5] == P.CMD_ACTIVITY_DATA]
    assert [f.hex() for f in echoes] == ["b004004baaada2"]        # as logged, once
    acks = [f for f in out if len(f) == 6]
    assert len(acks) == 1 + 5 + 4                                  # every frame ACKed
    assert len(sess._current[P.CMD_ACTIVITY_START]) == 6 + 56


def test_single_reply_repeated_with_same_seq_is_ignored():
    """Captured: FD and B4 replies sent twice with the same seq."""
    msgs = []
    sess = P.Am4Session(writer=lambda f: None, on_message=lambda c, d: msgs.append(c))
    for hexframe in ("a006001aaab401047d", "a006001aaab401047d"):
        sess.feed(bytes.fromhex(hexframe))
    assert msgs == [P.CMD_QUERY_STATE]


def test_interrupted_activity_transfer_keeps_received_points(monkeypatch):
    days = [activity_block(25, 8, d, 76, [(h, 0, h * 10, h) for h in range(24)]) for d in (27, 28, 29)]
    # start(27) + data... : mute in the middle of the second day
    dev = FakeAM4(activity=days, chunk=40, mute_after_echoes=8)
    monkeypatch.setattr(client_mod, "establish_connection",
                        lambda *a, **k: _fake_connect(dev))
    monkeypatch.setattr(client_mod, "CMD_TIMEOUT", 0.3)
    monkeypatch.setattr(client_mod, "BULK_IDLE_TIMEOUT", 0.3)
    monkeypatch.setattr(client_mod, "SETTLE_DELAY", 0.01)

    async def run():
        async with client_mod.Am4Client(Dev()) as c:
            try:
                await c.get_activity()
            except TimeoutError:
                pass
            else:
                raise AssertionError("transfer should stall")
            return c.partial_activity()

    pts = asyncio.run(run())
    days_seen = sorted({p.time.date() for p in pts})
    assert days_seen[0] == datetime(2025, 8, 27).date()
    assert all(p.time.day == 27 for p in pts[:24]) and len(pts) > 24   # day 27 whole + part of 28
    assert len(pts) < 72


async def _fake_connect(dev):
    return FakeBleakClient(dev, b"1.3.7")


# Captured 2026-11-24 00:13:36 (real AM4, fw 137), RX frames only
RACE_CAPTURE = """
a00e5010aa0000000b2b00000000000040 a011520caa160000000000000b1b000000000044
a011510eaa000b200000000000000b2500000064 a00e5010aa0000000b2b00000000000040 a003a014aa5e
a0115516aaad0b300000000000000b350000003d a0115418aa0000000b3a0000000000000c03006a
a011531aaa00000000000c080000000000000c37 a011521caa0d0000000000000c12000000000043
a011511eaa000c170000000000000c1c00000064 a00e5020aa0000000c2100000000000047
a00e5020aa0000000c2100000000000047
a0115522aaad0c260000000000000c2b00000037 a0115424aa0000000c300000000000000c35009f
a0115326aa00000000000c3a0000000000000d76 a0115228aa030000000000000d0800000000003c
a011512aaa000d0d0000000000000d120000005e a00e502caa0000000d170000000000004a
a003a024aa6e
a0115526aaad0d1c0000000000000d2100000029 a0115428aa0000000d260000000000000d2b0091
a011532aaa00000000000d300000000000000d71 a011522caa350000000000000d3a0000000000a4
a011512eaa000e030000000000000e0800000050 a00e5030aa0000000e0d00000000000045
""".split()


def test_new_message_reusing_seqs_is_not_taken_for_a_repeat():
    """The tracker numbered message 0d1c.. from the echo it accepted (0x23 + 3),
    reusing the seqs 0x26..0x2c of message 0c26.. just before. Comparing seqs
    only dropped it (transfer slowed to ~70 s a day); frames must be compared."""
    out, ad = [], []
    sess = P.Am4Session(writer=out.append,
                        on_message=lambda c, d: ad.append(d[:2].hex()) if c == P.CMD_ACTIVITY_DATA else None)
    sess.blocks[P.CMD_ACTIVITY_START] = []
    sess._current[P.CMD_ACTIVITY_START] = bytearray(6)
    for hexframe in RACE_CAPTURE:
        sess.feed(bytes.fromhex(hexframe))
    assert ad == ["0b30", "0c26", "0d1c"]          # each once, none lost
    echoes = [f[3] for f in out if len(f) == 7 and f[5] == P.CMD_ACTIVITY_DATA]
    assert echoes == [0x23, 0x2F, 0x33]              # 0x23 and 0x2f as logged
    # the echo of 0c26 is obsolete once 0d1c arrived: no longer re-sent
    assert 0x2F not in sess.t.pending and 0x33 in sess.t.pending


def test_client_waits_for_a_transfer_resumed_after_auth(monkeypatch):
    """Captured 24/11 00:55: a transfer cut in the previous session resumed on
    its own after FD; the BF sent meanwhile got no answer. The client must let
    it end before sending commands."""
    blk = activity_block(26, 11, 23, 70, [(h, 0, h * 10, h) for h in range(24)])
    dev = FakeAM4(activity=[blk], resume_on_auth=True)
    c, out = _run(monkeypatch, dev, history=False)
    assert out["real"] == P.Realtime(4321, 187)
    assert getattr(dev, "ignored_during_transfer", 0) == 0


# ---------------------------------------------------------------- reset
def test_bmr_and_user_info_payload():
    info = bytes([45, 76, 172, 0, 78, 5, 0]) + (10000).to_bytes(4, "big") * 3
    # male, 172 cm, 78.5 kg, 45 y: 88.362 + 1051.66 + 825.43 - 255.47 = 1709.99
    assert P.bmr_from_user_info(info) == 1710 - 2
    female = bytes([30, 70, 165, 1, 60, 0, 0]) + bytes(12)
    # 447.593 + 554.82 + 511.17 - 129.9 = 1383.68
    assert P.bmr_from_user_info(female) == 1384 - 2
    assert P.user_info_payload(info, 137) == bytes([P.AA, P.CMD_USER_INFO_SET]) + info
    with_swim = info + bytes([0, 30])
    assert P.user_info_payload(with_swim, 137)[2:] == info        # fw 137: 19 bytes
    assert P.user_info_payload(with_swim, 138)[2:] == with_swim   # fw 138: + swim target
    s = P.Am4Settings(1, info, 1, b"\x01\x00\x01", [bytes(6)], 3)
    assert P.Am4Settings.from_dict(s.to_dict()) == s


def _run_reset(monkeypatch, dev, firmware=b"137"):
    async def fake_connect(cls, ble_device, name, **kwargs):
        return FakeBleakClient(dev, firmware)

    monkeypatch.setattr(client_mod, "establish_connection", fake_connect)
    for name, value in (("CMD_TIMEOUT", 0.3), ("BULK_IDLE_TIMEOUT", 0.3), ("AUTH_TIMEOUT", 0.3),
                        ("SETTLE_DELAY", 0.01), ("QUIET_SETTLE", 0.05), ("CLOSE_GRACE", 0.2),
                        ("RESET_TIMEOUT", 0.3)):
        monkeypatch.setattr(client_mod, name, value)

    async def run():
        async with client_mod.Am4Client(Dev()) as c:
            await c.get_activity()
            kept = await c.read_settings()
            await c.reset()
            await c.restore_settings(kept)
            return kept, await c.get_activity()

    return asyncio.run(run())


def test_reset_keeps_pairing_and_settings(monkeypatch):
    days = [activity_block(26, 11, 23, 76, [(7, 0, 50, 2)])]
    dev = FakeAM4(activity=days, steps=900)
    before = (dev.user_id, dev.user_info, dev.hour_mode, dev.reminder, dict(dev.alarms), dev.picture)
    kept, after = _run_reset(monkeypatch, dev)
    assert dev.resets == 1
    assert after == []                                   # history gone
    assert (dev.user_id, dev.user_info, dev.hour_mode, dev.reminder,
            dev.alarms, dev.picture) == before           # all written back as read
    assert dev.bmr == P.bmr_from_user_info(dev.user_info)
    assert dev.clock is not None and dev.clock[0] >= 26  # not left in 2000
    # pairing order of the app: A3 user id before the profile, B7 before A5
    r = dev.received
    i = r.index(P.CMD_RESET)
    assert r.index(P.CMD_USER_ID_SET) > i
    assert r.index(P.CMD_USER_ID_SET) < r.index(P.CMD_BMR_SET) < r.index(P.CMD_USER_INFO_SET)
    assert r[-2:] == [P.CMD_USER_ID_GET, P.CMD_ACTIVITY_SYNC]   # id checked after restore


def test_reset_refused_on_unpaired_tracker(monkeypatch):
    dev = FakeAM4(user_id=0)
    with pytest.raises(P.Am4Error):
        _run_reset(monkeypatch, dev)
    assert dev.resets == 0
