"""Constants for the iHealth integration."""
from __future__ import annotations

DOMAIN = "ihealth"
MANUFACTURER = "iHealth"

PO3M_SERVICE_UUID = "0000fc83-0000-1000-8000-00805f9b34fb"
PO3M_LOCAL_NAME = "Pulse Oximeter"
PO3_CLASSIC_SERVICE_UUID = "636f6d2e-6a69-7561-6e2e-504f56313100"
PO3M_MODEL = "PO3M"

# Do not reconnect sooner than this after a session ended (seconds)
SESSION_COOLDOWN = 20
# Max duration of the live stream for one measurement (seconds)
MEASURE_TIMEOUT = 120

EVENT_HISTORY_RECORD = f"{DOMAIN}_history_record"
