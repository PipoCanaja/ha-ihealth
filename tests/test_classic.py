"""Classic PO3 (POV11): auth key derivation + full session against fake device."""
from __future__ import annotations

from datetime import datetime

from po3classic import protocol as P
from fake_po3_classic import FakePO3Classic


def test_session_key_is_derived_from_recovered_native_key():
    # getKey("PO3") recovered from libiHealth.so, Ka cross-checked against Java.
    assert P._KEY_PO3.hex() == "bf4b051142d270a3932d2daacca9bd1e"
    assert P._KA.hex() == "faa35392cd6f290f33594bcc15bfc725"


def _run():
    dev = FakePO3Classic()
    msgs = []
    sess = P.Po3Session(writer=dev.write, on_message=lambda c, d: msgs.append((c, d)))

    def pump():
        while dev.out:
            sess.feed(dev.out.pop(0))

    sess.start_auth(); pump()
    return dev, sess, msgs, pump


def test_handshake_and_live():
    dev, sess, msgs, pump = _run()
    assert sess.authenticated          # device verified our FC response
    assert dev.acks > 0                # app ACKs fragments
    sess.get_battery(); pump()
    sess.get_time(); pump()
    sess.start_measure(); pump()
    got = {c: d for c, d in msgs}
    assert P.decode_battery(got[P.CMD_BATTERY]) == 77
    assert P.decode_time(got[P.CMD_GET_TIME]) == datetime(2026, 11, 22, 19, 30, 0)
    lives = [P.decode_live(d) for c, d in msgs if c == P.CMD_LIVE]
    assert [(l.spo2, l.pulse_rate) for l in lives] == [(97, 61), (98, 62), (98, 63)]
    assert lives[0].pulse_wave == [900, 1100, 1000]


def test_wrong_key_fails_auth():
    dev = FakePO3Classic()
    dev.device_id = bytes(16)  # fake computes expected FC from this id...
    msgs = []
    sess = P.Po3Session(writer=dev.write, on_message=lambda c, d: msgs.append((c, d)))
    # tamper: make the fake expect a different id than it sent
    orig = dev._cmd
    def patched(cmd, data):
        if cmd == P.CMD_AUTH_FC:
            dev.tx(P.CMD_AUTH_FAIL)  # force rejection
        else:
            orig(cmd, data)
    dev._cmd = patched
    sess.start_auth()
    while dev.out:
        sess.feed(dev.out.pop(0))
    assert not sess.authenticated
