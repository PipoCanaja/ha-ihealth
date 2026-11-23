"""KN-550BT: keys, XXTEA2 vectors, record decoding, sessions against a fake device."""
from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

import kn550bt.client as client_mod
from kn550bt import protocol as P
from fake_kn550bt import FakeKN550BT, rec10, rec11

h = bytes.fromhex


def test_keys_match_docs_and_cross_check():
    # Ka derived from getKey() exactly as for the PO3 (see docs/device_keys.md)
    assert P.KA_KN550BT.hex() == "37d0db86635359c0e51227b543f39c1b"
    assert P.KA_KN550BT_BPAV21.hex() == "19010796f2231a688b5434628c57eb19"
    # IdentifyIns2's hard-coded key is that same derived Ka: independent check
    assert P.KEY_STREAM == P.KA_KN550BT_BPAV21
    assert P.classic_ka("com.jiuan.BPAV21") == P.KA_KN550BT_BPAV21
    assert P.classic_ka("com.jiuan.BPAV22") == P.KA_KN550BT_BPAV21
    assert P.classic_ka("com.jiuan.BPAV10") == P.KA_KN550BT
    assert P.classic_ka(None) == P.KA_KN550BT


def test_xxtea2_matches_java():
    # Vectors produced by running the app's own XXTEA2.java
    dev_id = h("00112233445566778899aabbccddeeff")
    seed = h("0f1e2d3c4b5a69788796a5b4c3d2e1f0")
    r1 = h("0102030405060708090a0b0c0d0e0f10")
    ka = P.xxtea2_encrypt(dev_id, P.KEY_STREAM)
    assert ka.hex() == "b574e4a093d501ce337825377cb7f627a006b18c"
    r1s = P.xxtea2_encrypt(r1, ka)
    assert r1s.hex() == "0b82d5896bac65f8c541efb94a7b02f4af6525f4"
    assert P.xxtea2_decrypt(r1s, ka) == r1
    resp = P.stream_auth_response(seed + r1s + dev_id, r1)
    assert resp.hex() == "a71763c7cd617330c19dee942051ba552da21701"
    assert P.stream_auth_response(seed + r1s + dev_id, bytes(16)) is None


def test_decode_records_both_formats():
    old = rec10(26, 11, 20, 8, 15, 128, 82, 71, arr=True) + rec10(26, 11, 21, 7, 2, 135, 88, 66)
    r = P.decode_records(old, new_format=False)
    assert r[0] == P.BpRecord(datetime(2026, 11, 20, 8, 15), 128, 82, 71, arrhythmia=True)
    assert (r[1].systolic, r[1].diastolic, r[1].pulse_rate) == (135, 88, 66)

    new = rec11(26, 11, 22, 20, 1, 33, 141, 90, 75, flags=0x91, scheme=2)
    (n,) = P.decode_records(new, new_format=True, calibration_flag=True)
    assert n.timestamp == datetime(2026, 11, 22, 20, 1, 33)
    assert (n.systolic, n.diastolic, n.pulse_rate, n.scheme_id) == (141, 90, 75, 2)
    assert n.arrhythmia and n.body_movement and n.needs_time_fix
    (n0,) = P.decode_records(new, new_format=True, calibration_flag=False)
    assert not n0.needs_time_fix        # fw <= 2.0.0: bit has no meaning


def test_fix_timestamp():
    rec = P.BpRecord(datetime(2000, 1, 1, 0, 10), 120, 80, 60, needs_time_fix=True)
    device_now = datetime(2000, 1, 1, 2, 0)          # clock reset to 2000-01-01
    real_now = datetime(2026, 11, 23, 9, 0)
    assert P.fix_timestamp(rec, device_now, real_now) == datetime(2026, 11, 23, 7, 10)
    assert P.fix_timestamp(rec, None, real_now) == rec.timestamp
    ok = P.BpRecord(datetime(2026, 11, 23, 7, 10), 120, 80, 60)
    assert P.fix_timestamp(ok, device_now, real_now) == ok.timestamp


def test_firmware_number():
    assert P.firmware_number("2.0.1") == 201
    assert P.firmware_number("1.2.9") == 129
    assert P.firmware_number("") is None
    assert P.firmware_number("v2") is None


def _pump(sess, dev):
    while dev.out:
        sess.feed(dev.out.pop(0))


@pytest.mark.parametrize("stream", [False, True])
def test_session_auth(stream):
    dev = FakeKN550BT(stream=stream)
    msgs = []
    sess = P.Kn550Session(stream, P.PROTO_BPAV21, writer=dev.write,
                          on_message=lambda c, d: msgs.append((c, d)))
    sess.start_auth(); _pump(sess, dev)
    assert sess.authenticated and dev.authenticated
    if not stream:
        assert dev.acks > 0   # FB is fragmented: app must ACK with A1


def test_classic_wrong_key_is_rejected():
    dev = FakeKN550BT(protocol=P.PROTO_BPAV10)   # device expects KN-550BT key
    sess = P.Kn550Session(False, P.PROTO_BPAV21, writer=dev.write)  # we use BPAV21's
    sess.start_auth(); _pump(sess, dev)
    assert not sess.authenticated


# ---------------------------------------------------------------- async client
class FakeChar:
    properties = ["write-without-response", "notify"]


class FakeServices:
    def __init__(self, service, chars):
        self.service, self.chars = service, chars

    def get_service(self, uuid):
        return object() if uuid == self.service else None

    def get_characteristic(self, uuid):
        return FakeChar() if uuid in self.chars else None


class FakeBleakClient:
    def __init__(self, dev: FakeKN550BT, service, tx, rx, dis):
        self.dev = dev
        self.is_connected = True
        self.cb = None
        self.dis = dis
        self.services = FakeServices(service, {tx, rx, *dis})

    async def read_gatt_char(self, uuid):
        return self.dis[uuid]

    async def start_notify(self, uuid, cb):
        self.cb = cb

    async def write_gatt_char(self, uuid, data, response):
        self.dev.write(bytes(data))
        loop = asyncio.get_running_loop()
        for i, chunk in enumerate(self.dev.out):
            loop.call_later(0.002 * (i + 1), self.cb, None, bytearray(chunk))
        self.dev.out = []

    async def disconnect(self):
        self.is_connected = False


class Dev:
    address = "AA:BB:CC:DD:EE:01"
    name = "KN-550BT"


def _run_client(monkeypatch, dev, service, tx, rx, dis):
    async def fake_connect(cls, ble_device, name, **kwargs):
        return FakeBleakClient(dev, service, tx, rx, dis)

    monkeypatch.setattr(client_mod, "establish_connection", fake_connect)
    monkeypatch.setattr(client_mod, "CMD_TIMEOUT", 0.3)

    async def run():
        async with client_mod.Kn550Client(Dev()) as c:
            battery = await c.get_battery()
            history = await c.get_history()
            await c.set_time()
            when = await c.get_time()
            return c, battery, history, when

    return asyncio.run(run())


def test_client_classic_bpav21(monkeypatch):
    recs = [rec11(26, 11, d, 9, 0, 0, 120 + d, 80, 60 + d) for d in range(1, 13)]
    dev = FakeKN550BT(records=recs, page=5)   # 3 pages, multi-fragment frames
    c, battery, history, when = _run_client(
        monkeypatch, dev, P.SERVICE_AV21, P.TX_AV21, P.RX_AV21,
        {P.CHAR_FIRMWARE: b"2.0.3\0"},
    )
    assert c.protocol == P.PROTO_BPAV21 and not c.stream
    assert battery == 64
    assert [r.systolic for r in history] == [120 + d for d in range(1, 13)]
    assert history[-1].timestamp == datetime(2026, 11, 12, 9, 0, 0)
    assert history[0].needs_time_fix is True     # fw > 2.0.0, flag 0x01 set
    assert when is None                          # classic profile: unsupported
    assert P.CMD_OFFLINE_OVER not in dev.received   # memory is never cleared
    assert dev.received[:2] == [P.CMD_AUTH_FA, P.CMD_AUTH_FC]


def test_client_stream_falls_back_to_old_format(monkeypatch):
    recs = [rec10(26, 11, 22, 7, 30, 131, 85, 70), rec10(26, 11, 22, 19, 45, 125, 79, 64)]
    dev = FakeKN550BT(stream=True, protocol="com.jiuan.BPAV10", records=recs, battery=91)
    c, battery, history, when = _run_client(
        monkeypatch, dev, P.SERVICE_DEV, P.TX_DEV, P.RX_DEV,
        # protocol string read from Device Information
        {P.CHAR_PROTOCOL: b"com.jiuan.BPAV10\0\0", P.CHAR_FIRMWARE: b"2.0.1"},
    )
    assert c.stream and c.protocol == "com.jiuan.BPAV10"
    assert battery == 91                         # 2.0.1: odd patch -> supported
    assert [(r.systolic, r.diastolic) for r in history] == [(131, 85), (125, 79)]
    assert when == datetime(2026, 11, 22, 21, 5, 9)
    assert dev.clock[:3] != (0, 0, 0)            # function info carried the time


def test_client_stream_unknown_protocol_tries_both(monkeypatch):
    recs = [rec11(26, 11, 22, 7, 30, 5, 131, 85, 70)]
    dev = FakeKN550BT(stream=True, protocol=P.PROTO_BPAV21, records=recs)
    c, battery, history, _ = _run_client(
        monkeypatch, dev, P.SERVICE_DEV, P.TX_DEV, P.RX_DEV, {P.CHAR_FIRMWARE: b"2.0.0"},
    )
    assert battery is None                       # 2.0.0 on stream: unsupported
    assert len(history) == 1 and history[0].systolic == 131
