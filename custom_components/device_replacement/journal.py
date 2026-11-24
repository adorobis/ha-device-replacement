"""Persistent journal of replacements: what was changed, for audit and undo."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util.ulid import ulid_now

from .const import STORAGE_KEY, STORAGE_VERSION

MAX_RECORDS = 50


class Journal:
    """The records of all replacements, newest last."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialise the journal."""
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self.records: list[dict[str, Any]] = []

    async def async_load(self) -> None:
        """Load the records from storage."""
        data = await self._store.async_load()
        self.records = (data or {}).get("records", [])

    def new_record(self, **fields: Any) -> dict[str, Any]:
        """Start a record and return it; it is saved by async_save."""
        record: dict[str, Any] = {
            "id": ulid_now(),
            "created_at": dt_util.utcnow().isoformat(),
            "status": "running",
            "steps": [],
            "migrated": [],
            "warnings": [],
            "sum_adjustments": [],
            **fields,
        }
        self.records.append(record)
        del self.records[:-MAX_RECORDS]
        return record

    def get(self, record_id: str) -> dict[str, Any] | None:
        """Return a record by id."""
        return next((r for r in self.records if r["id"] == record_id), None)

    @staticmethod
    def step(record: dict[str, Any], name: str, **details: Any) -> None:
        """Append a finished step to a record."""
        record["steps"].append({"step": name, "at": dt_util.utcnow().isoformat(), **details})

    async def async_save(self) -> None:
        """Write the records to storage now."""
        await self._store.async_save({"records": self.records})
