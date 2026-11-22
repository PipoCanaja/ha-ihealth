"""Async client test against the fake device through a fake bleak client."""
from __future__ import annotations

import asyncio

import po3m.client as client_mod
from fake_po3m import FakePO3M


class FakeBleakClient:
    def __init__(self, dev: FakePO3M) -> None:
        self.dev = dev
        self.is_connected = True
        self.cb = None

    async def start_notify(self, uuid, cb):
        self.cb = cb

    async def write_gatt_char(self, uuid, data, response):
        self.dev.write(bytes(data))
        loop = asyncio.get_running_loop()
        for i, chunk in enumerate(self.dev.out):
            loop.call_later(0.01 * (i + 1), self.cb, None, bytearray(chunk))
        self.dev.out = []

    async def disconnect(self):
        self.is_connected = False


class Dev:
    address = "AA:BB:CC:DD:EE:FF"
    name = "Pulse Oximeter"


def test_full_session(monkeypatch):
    dev = FakePO3M()

    async def fake_connect(cls, ble_device, name, **kwargs):
        return FakeBleakClient(dev)

    monkeypatch.setattr(client_mod, "establish_connection", fake_connect)
    monkeypatch.setattr(client_mod, "RESULT_SILENCE", 0.2)

    async def run():
        live = []
        async with client_mod.Po3mClient(Dev()) as po3:
            battery = await po3.get_battery()
            await po3.set_time()
            info = await po3.get_device_info()
            result = await po3.measure(on_live=live.append, timeout=5)
            history = await po3.get_history()
        return battery, info, live, result, history

    battery, info, live, result, history = asyncio.run(run())
    assert battery["battery"] == 87
    assert info["model"] == "PO3M"
    assert len(live) == 3
    assert (result.spo2, result.pulse_rate) == (98, 64)
    assert len(history) == 2
    assert dev.received[:2] == [0xFA, 0xFC]
