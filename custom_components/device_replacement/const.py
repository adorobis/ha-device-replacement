"""Constants for Device Replacement."""

from typing import Final

DOMAIN: Final = "device_replacement"

STORAGE_KEY: Final = f"{DOMAIN}.journal"
STORAGE_VERSION: Final = 1

# Suffix for the unique_id of a new entity whose registry entry is retired. The entry
# is renamed instead of removed before the reload, because integrations clean up
# after a removed entry (MQTT clears the retained discovery topic), and that cleanup
# can remove the migrated entry, which by then owns the unique_id.
TOMBSTONE_SUFFIX: Final = "__device_replacement_tombstone"

# Minimum score for an automatically suggested entity pair.
MATCH_THRESHOLD: Final = 40

# How long to wait for entities to unload and to come back after the reload.
UNLOAD_TIMEOUT: Final = 30.0
RELOAD_TIMEOUT: Final = 60.0
# HA reloads a config entry 30 s after disabled_by of one of its entities changed;
# reload directly when that has not happened by then.
RELOAD_FALLBACK_AFTER: Final = 40.0

# The recorder compiles the 5-minute statistics shortly after each period ends.
STATISTICS_PERIOD_MINUTES: Final = 5
SUM_ADJUST_DELAY_AFTER_PERIOD: Final = 90.0

ATTR_OLD_DEVICE_ID: Final = "old_device_id"
ATTR_NEW_DEVICE_ID: Final = "new_device_id"
ATTR_MAPPING: Final = "mapping"
ATTR_AUTO_MATCH: Final = "auto_match"
ATTR_KEEP_UNITS: Final = "keep_units"
ATTR_ADJUST_ENERGY: Final = "adjust_energy"
ATTR_REMOVE_OLD_DEVICE: Final = "remove_old_device"
ATTR_FORCE: Final = "force"
ATTR_DRY_RUN: Final = "dry_run"

SERVICE_PLAN: Final = "plan"
SERVICE_EXECUTE: Final = "execute"
SERVICE_JOURNAL: Final = "journal"
SERVICE_UNDO: Final = "undo"
