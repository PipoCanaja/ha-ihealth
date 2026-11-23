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
MEASURE_TIMEOUT = 600

EVENT_HISTORY_RECORD = f"{DOMAIN}_history_record"

# KN-550BT blood-pressure monitor
KN550BT_MODEL = "KN-550BT"
KN550BT_LOCAL_NAME = "KN-550BT"
KN550BT_SERVICE_AV21 = "636f6d2e-6a69-7561-6e2e-425041563231"  # com.jiuan.BPAV21
KN550BT_SERVICE_AV10 = "636f6d2e-6a69-7561-6e2e-425041563130"  # com.jiuan.BPAV10 (shared)
KN550BT_SERVICE_DEV = "636f6d2e-6a69-7561-6e2e-646576000000"   # com.jiuan.dev (shared)
# After a successful download, wait this long before the next one: the monitor
# keeps advertising for a while after a measurement (seconds)
KN550BT_SESSION_COOLDOWN = 120

EVENT_BP_RECORD = f"{DOMAIN}_bp_record"

# Config entry data: which kind of device the entry is for
CONF_KIND = "kind"
KIND_PO3 = "po3"
KIND_KN550BT = "kn550bt"
KIND_AM4 = "am4"

# AM4 activity tracker
AM4_MODEL = "AM4"
AM4_SERVICE_UUID = "636f6d2e-6a69-7561-6e2e-414d56313200"  # com.jiuan.AMV12
# Read the tracker at most this often while it is in range (seconds)
AM4_SESSION_INTERVAL = 15 * 60

EVENT_AM4_ACTIVITY = f"{DOMAIN}_am4_activity"
EVENT_AM4_SLEEP = f"{DOMAIN}_am4_sleep"
EVENT_AM4_STAGE = f"{DOMAIN}_am4_stage"
