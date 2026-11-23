"""Tell supported iHealth devices apart from their advertisement."""
from __future__ import annotations

from .const import (
    KIND_KN550BT,
    KIND_PO3,
    KN550BT_LOCAL_NAME,
    KN550BT_MODEL,
    KN550BT_SERVICE_AV10,
    KN550BT_SERVICE_AV21,
    KN550BT_SERVICE_DEV,
    PO3_CLASSIC_SERVICE_UUID,
    PO3M_LOCAL_NAME,
    PO3M_MODEL,
    PO3M_SERVICE_UUID,
)

MODELS = {KIND_PO3: PO3M_MODEL, KIND_KN550BT: KN550BT_MODEL}


def device_kind(info) -> str | None:
    """Return KIND_* for a BluetoothServiceInfo, or None if unsupported.

    Mirrors iHealthDevicesManager: the BPAV10 and "dev" services are shared
    with other models, so for those the name must contain "550".
    """
    uuids = set(info.service_uuids or ())
    name = info.name or ""
    if PO3M_SERVICE_UUID in uuids or PO3_CLASSIC_SERVICE_UUID in uuids or PO3M_LOCAL_NAME in name:
        return KIND_PO3
    if KN550BT_SERVICE_AV21 in uuids or name.startswith(KN550BT_LOCAL_NAME):
        return KIND_KN550BT
    if uuids & {KN550BT_SERVICE_AV10, KN550BT_SERVICE_DEV} and "550" in name:
        return KIND_KN550BT
    return None
