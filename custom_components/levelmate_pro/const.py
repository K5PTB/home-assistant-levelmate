"""Constants for the LevelMate Pro integration (read-only fork).

The UUIDs, byte layouts and scaling below come from upstream
(timsys512/home-assistant-levelmate), which reverse-engineered them from the
official LogicBlue "LevelMate Pro" Android app (package
com.logicblue.levelmateprov2).

This fork only READS the device. It never writes to any characteristic, so the
write-related UUIDs (password, install orientation, RV width/length, units,
road side, ...) are deliberately not defined here. The device's own settings,
made with the official app, stay exactly as they are.
"""
from __future__ import annotations

DOMAIN = "levelmate_pro"

# --- BLE identifiers ---------------------------------------------------------

SERVICE_UUID = "0000aa10-0000-1000-8000-00805f9b34fb"

# The two leveling measurements (signed int16 LE, 0.25 inch per count). The
# device firmware has already remapped them for the mounting orientation that
# was set with the official app.
CHAR_SIDE = "0000aa13-0000-1000-8000-00805f9b34fb"
CHAR_FRONT = "0000aa14-0000-1000-8000-00805f9b34fb"

# Temperature, float32 LE, degrees Celsius.
CHAR_TEMPERATURE = "0000aa20-0000-1000-8000-00805f9b34fb"

# --- Scaling -----------------------------------------------------------------

INCHES_PER_RAW_COUNT = 0.25  # for CHAR_SIDE / CHAR_FRONT

# --- Connection behaviour ----------------------------------------------------

# The connection switch turns itself off when the switch is on but the device
# has not been connected for this long - e.g. the LevelMate was switched off,
# or it went to sleep after being left alone. That way Home Assistant never
# grabs the device's single BLE connection at the next stop before the phone
# app can.
GRACE_SECONDS = 5 * 60

# Minimum time between connection attempts triggered by advertisements.
RETRY_SECONDS = 10

# --- Config entry keys --------------------------------------------------------

CONF_ADDRESS = "address"
