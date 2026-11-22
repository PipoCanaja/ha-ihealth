"""Make `po3m` importable without Home Assistant; stub bleak if missing."""
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components" / "ihealth"))
sys.path.insert(0, str(Path(__file__).parent))

try:  # pragma: no cover
    import bleak  # noqa: F401
    import bleak_retry_connector  # noqa: F401
except ImportError:
    device = types.ModuleType("bleak.backends.device")
    device.BLEDevice = object
    sys.modules.setdefault("bleak", types.ModuleType("bleak"))
    sys.modules.setdefault("bleak.backends", types.ModuleType("bleak.backends"))
    sys.modules["bleak.backends.device"] = device
    brc = types.ModuleType("bleak_retry_connector")
    brc.BleakClientWithServiceCache = object

    async def establish_connection(*args, **kwargs):  # replaced in tests
        raise NotImplementedError

    brc.establish_connection = establish_connection
    sys.modules["bleak_retry_connector"] = brc
