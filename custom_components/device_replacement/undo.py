"""Undo a replacement from its journal record.

The migrated registry entries go back to the old device with their old platform,
unique_id and config entry, the way they came: the entries are unloaded, whatever
holds the unique_id they need is renamed to a tombstone first (while the old device
was disabled, its integration may have re-created its entities as `*_2`), the old
device is enabled again, and the tombstones are removed after the reload. The new
device's integration then creates its own entities again under fresh entity_ids.

Device settings copied to the new device are restored, the new device ID is put back
to the old one where it was rewritten, and the Repairs issues of the record are
deleted. Statistics are not touched.

An undo needs the old device: it is refused when the replacement removed it.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
from homeassistant.util import dt as dt_util

from . import executor
from .const import DOMAIN, TOMBSTONE_SUFFIX
from .executor import _loaded, _wait_until
from .journal import Journal
from .references import async_rewrite_device_id

_LOGGER = logging.getLogger(__name__)


def undo_blocker(hass: HomeAssistant, record: dict[str, Any]) -> str | None:
    """Return why a record cannot be undone, or None when it can."""
    if record.get("dry_run"):
        return "it was a dry run"
    if record.get("undone_at"):
        return "it was already undone"
    if record.get("status") != "done":
        return f"it did not finish ({record.get('status')})"
    if record.get("options", {}).get("remove_old_device"):
        return "the old device was removed"
    if dr.async_get(hass).async_get(record["old_device_id"]) is None:
        return "the old device no longer exists"
    return None


def undoable(hass: HomeAssistant, record: dict[str, Any]) -> bool:
    """Return True when a record can be undone."""
    return undo_blocker(hass, record) is None


class Undoer:
    """Reverse one replacement."""

    def __init__(self, hass: HomeAssistant, journal: Journal, record: dict[str, Any]) -> None:
        """Initialise from a journal record."""
        self.hass = hass
        self.journal = journal
        self.record = record
        self.ereg = er.async_get(hass)
        self.dreg = dr.async_get(hass)
        self.steps: list[dict[str, Any]] = []
        self.warnings: list[str] = []

    def _forward_step(self, name: str) -> dict[str, Any]:
        return next((s for s in self.record["steps"] if s["step"] == name), {})

    def _step(self, name: str, **details: Any) -> None:
        self.steps.append({"step": name, "at": dt_util.utcnow().isoformat(), **details})

    def _warn(self, message: str) -> None:
        _LOGGER.warning("Undo %s: %s", self.record["id"], message)
        self.warnings.append(message)

    def validate(self) -> None:
        """Raise when the record cannot be undone."""
        if reason := undo_blocker(self.hass, self.record):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="undo_impossible",
                translation_placeholders={"record_id": self.record["id"], "reason": reason},
            )

    async def async_run(self) -> dict[str, Any]:
        """Undo the replacement and return the updated record."""
        self.validate()
        try:
            await self._async_run()
        finally:
            self.record["undo"] = {"steps": self.steps, "warnings": self.warnings}
            await self.journal.async_save()
        self.record["undone_at"] = dt_util.utcnow().isoformat()
        self.record["status"] = "undone"
        await self.journal.async_save()
        return self.record

    async def _async_run(self) -> None:
        snapshot = self.record["snapshot"]
        before = {e["entity_id"]: e for e in snapshot["entities"]}
        old_device_id = self.record["old_device_id"]
        new_device_id = self.record["new_device_id"]

        # Entries to move back, still as the replacement left them.
        moves: list[tuple[str, dict[str, Any]]] = []
        for item in self.record["migrated"]:
            eid = item["entity_id"]
            entry = self.ereg.async_get(eid)
            original = before.get(eid)
            if entry is None or original is None:
                self._warn(f"{eid} no longer exists; skipped")
                continue
            if entry.unique_id != item["new_unique_id"]:
                self._warn(f"{eid} changed since the replacement; skipped")
                continue
            moves.append((eid, original))

        ids = [eid for eid, _ in moves]
        await self._async_unload(ids)
        tombstones = await self._async_move_back(moves)
        self._restore_devices(old_device_id, new_device_id)
        await self._async_wait_and_clean(moves, tombstones)
        await self._async_restore_references(old_device_id, new_device_id)

    async def _async_unload(self, ids: list[str]) -> None:
        for eid in ids:
            if not self.ereg.async_get(eid).disabled:
                self.ereg.async_update_entity(eid, disabled_by=er.RegistryEntryDisabler.USER)
        if not await _wait_until(lambda: not _loaded(self.hass, ids), executor.UNLOAD_TIMEOUT):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="old_not_unloaded",
                translation_placeholders={"entities": ", ".join(sorted(_loaded(self.hass, ids)))},
            )
        self._step("migrated_entities_unloaded")

    async def _async_move_back(self, moves: list[tuple[str, dict[str, Any]]]) -> list[str]:
        """Move the entries back; tombstone whatever holds their old unique_id."""
        tombstones: list[str] = []
        for eid, original in moves:
            domain = eid.split(".", 1)[0]
            holder = self.ereg.async_get_entity_id(
                domain, original["platform"], original["unique_id"]
            )
            if holder and holder != eid:
                if holder in _loaded(self.hass, [holder]):
                    self.ereg.async_update_entity(holder, disabled_by=er.RegistryEntryDisabler.USER)
                    await _wait_until(
                        lambda h=holder: not _loaded(self.hass, [h]), executor.UNLOAD_TIMEOUT
                    )
                held = self.ereg.async_get(holder)
                self.ereg.async_update_entity_platform(
                    holder,
                    held.platform,
                    new_config_entry_id=held.config_entry_id,
                    new_unique_id=held.unique_id + TOMBSTONE_SUFFIX,
                    new_device_id=held.device_id,
                )
                tombstones.append(holder)
            self.ereg.async_update_entity_platform(
                eid,
                original["platform"],
                new_config_entry_id=original["config_entry_id"],
                new_config_subentry_id=original["config_subentry_id"],
                new_unique_id=original["unique_id"],
                new_device_id=original["device_id"],
            )
            if domain == "sensor":
                self.ereg.async_update_entity_options(
                    eid, "sensor", original["options"].get("sensor")
                )
            disabled_by = original["disabled_by"]
            self.ereg.async_update_entity(
                eid,
                disabled_by=er.RegistryEntryDisabler(disabled_by) if disabled_by else None,
            )
        self._step("entities_moved_back", entities=[eid for eid, _ in moves], tombstones=tombstones)
        return tombstones

    def _restore_devices(self, old_device_id: str, new_device_id: str) -> None:
        """Enable the old devices again and restore the new devices' settings."""
        snapshot = self.record["snapshot"]
        for before in snapshot.get("old_devices") or [snapshot["old_device"]]:
            if self.dreg.async_get(before["id"]) is None:
                continue
            disabled_by = before["disabled_by"]
            self.dreg.async_update_device(
                before["id"],
                disabled_by=dr.DeviceEntryDisabler(disabled_by) if disabled_by else None,
            )
        step = self._forward_step("device_settings_copied")
        # v0.2 records list the changes per new device; v0.1 only for the main one
        changed_by_device = step.get("devices") or {new_device_id: step.get("changes", {})}
        new_before = {d["id"]: d for d in snapshot.get("new_devices") or [snapshot["new_device"]]}
        restored: dict[str, list[str]] = {}
        for device_id, changed in changed_by_device.items():
            before = new_before.get(device_id)
            if before is None or self.dreg.async_get(device_id) is None:
                continue
            restore: dict[str, Any] = {}
            if "area_id" in changed:
                restore["area_id"] = before["area_id"]
            if "name_by_user" in changed:
                restore["name_by_user"] = before["name_by_user"]
            if "labels" in changed:
                restore["labels"] = set(before["labels"])
            if restore:
                self.dreg.async_update_device(device_id, **restore)
                restored[device_id] = sorted(restore)
        self._step("devices_restored", new_devices=restored)

    async def _async_wait_and_clean(
        self, moves: list[tuple[str, dict[str, Any]]], tombstones: list[str]
    ) -> None:
        """Wait until both devices have their entities again, then drop tombstones."""
        before = {e["entity_id"]: e for e in self.record["snapshot"]["entities"]}
        # Only entities that are enabled once the old devices are restored come back;
        # a device that was disabled before the replacement is disabled again.
        expected = [
            eid
            for eid, _ in moves
            if (e := self.ereg.async_get(eid)) is not None and not e.disabled
        ]
        config_entries = {o["config_entry_id"] for _, o in moves if o["config_entry_id"]}
        new_keys: list[tuple[str, str, str]] = []
        for item in self.record["migrated"]:
            new_before = before.get(item["replaced_by"])
            if new_before is None:
                continue
            new_keys.append(
                (item["entity_id"].split(".", 1)[0], new_before["platform"], item["new_unique_id"])
            )
            if new_before["config_entry_id"]:
                config_entries.add(new_before["config_entry_id"])

        def back() -> bool:
            if _loaded(self.hass, expected) != set(expected):
                return False
            new_ids = [self.ereg.async_get_entity_id(*key) for key in new_keys]
            return all(new_ids) and _loaded(self.hass, new_ids) == set(new_ids)

        reloaded = await _wait_until(back, executor.RELOAD_FALLBACK_AFTER)
        if not reloaded:
            for entry_id in config_entries:
                entry = self.hass.config_entries.async_get_entry(entry_id)
                if entry is not None and entry.supports_unload:
                    await self.hass.config_entries.async_reload(entry_id)
            reloaded = await _wait_until(back, executor.RELOAD_TIMEOUT)
        self._step(
            "reloaded",
            complete=reloaded,
            new_entities=[self.ereg.async_get_entity_id(*key) for key in new_keys],
        )
        if reloaded:
            for eid in tombstones:
                if eid not in _loaded(self.hass, [eid]):
                    self.ereg.async_remove(eid)
            self._step("tombstones_removed", entities=tombstones)
        else:
            self._warn("The entities did not all come back; Home Assistant should be restarted")

    async def _async_restore_references(self, old_device_id: str, new_device_id: str) -> None:
        """Put the old device IDs back where they were rewritten; delete the issues."""
        step = self._forward_step("references_updated")
        # v0.2 records list the rewrites per old device; v0.1 only for the main one
        per_device = step.get("devices")
        if per_device is None:
            per_device = {old_device_id: step["rewritten"]} if step.get("rewritten") else {}
        pairs = self.record.get("device_pairs") or {old_device_id: new_device_id}
        restored: dict[str, dict[str, int]] = {}
        for old_id in per_device:
            new_id = pairs.get(old_id)
            if new_id:
                restored[old_id] = await async_rewrite_device_id(self.hass, new_id, old_id)
        if restored:
            self._step("references_restored", devices=restored)
        for issue_id in self.record.get("issues", []):
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        self._step("issues_deleted", count=len(self.record.get("issues", [])))
