"""Carry out a replacement plan.

Order (see the spike findings in the README):

1. Disable the old device. Its entities unload, and anything its integration creates
   for it while the swap runs is created disabled.
2. Disable the mapped new entities so they unload.
3. Per pair: rename the new entry to a tombstone unique_id, then move the old entry
   (entity_id, history, settings) onto the new entity's platform, unique_id, config
   entry and device. Moving onto the enabled new device enables the old entry again,
   unless the user had disabled it.
4. Copy the device settings, wait for the new config entry to reload (HA reloads it
   30 s after disabled_by changed; reload directly if it has not happened).
5. Remove the tombstones, purge their temporary history, optionally remove the old
   device, and schedule the meter sum corrections.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.entity import entity_sources
from homeassistant.helpers.event import (
    async_track_point_in_utc_time,
    async_track_state_change_event,
)
from homeassistant.loader import async_get_integration
from homeassistant.util import dt as dt_util

from .compat import SUM_STATE_CLASSES, unit_converter, units_compatible
from .const import (
    DOMAIN,
    RELOAD_FALLBACK_AFTER,
    RELOAD_TIMEOUT,
    TOMBSTONE_SUFFIX,
    UNLOAD_TIMEOUT,
)
from .journal import Journal
from .models import EntityPair, Plan
from .references import Reference, async_find_references, async_rewrite_device_id
from .repairs import async_create_replacement_issues
from .statistics import (
    adjust_sum,
    apply_after,
    async_last_numeric_state,
    async_statistics_unit,
    period_start,
    sum_jump,
    to_float,
)

_LOGGER = logging.getLogger(__name__)

# Blocking checks that force cannot override.
NOT_FORCEABLE = {
    "same_device",
    "domain_mismatch",
    "mapping_unknown_old",
    "mapping_unknown_new",
    "mapping_duplicate_new",
}

_POLL = 0.25
_sleep = asyncio.sleep  # patched in tests that freeze the clock

# Stop waiting for the first reading of a new meter after this long.
FIRST_READING_TIMEOUT = 3600.0


async def _wait_until(predicate: Callable[[], bool], seconds: float) -> bool:
    # Counts sleeps instead of reading a clock, so a frozen clock cannot hang it.
    for _ in range(max(1, round(seconds / _POLL))):
        if predicate():
            return True
        await _sleep(_POLL)
    return predicate()


def _loaded(hass: HomeAssistant, entity_ids: Iterable[str]) -> set[str]:
    sources = entity_sources(hass)
    return {e for e in entity_ids if e in sources}


def _entry_snapshot(entry: er.RegistryEntry) -> dict[str, Any]:
    return {
        "id": entry.id,
        "entity_id": entry.entity_id,
        "platform": entry.platform,
        "unique_id": entry.unique_id,
        "config_entry_id": entry.config_entry_id,
        "config_subentry_id": entry.config_subentry_id,
        "device_id": entry.device_id,
        "disabled_by": entry.disabled_by,
        "options": {k: dict(v) for k, v in entry.options.items()},
    }


def _device_snapshot(device: dr.DeviceEntry) -> dict[str, Any]:
    return {
        "id": device.id,
        "name": device.name,
        "name_by_user": device.name_by_user,
        "area_id": device.area_id,
        "labels": sorted(device.labels),
        "disabled_by": device.disabled_by,
        "config_entries": sorted(device.config_entries),
    }


class Executor:
    """Run one plan and keep its journal record up to date."""

    def __init__(
        self,
        hass: HomeAssistant,
        journal: Journal,
        plan: Plan,
        *,
        keep_units: bool = True,
        adjust_energy: bool = True,
        remove_old_device: bool = False,
        force: bool = False,
    ) -> None:
        """Initialise the executor."""
        self.hass = hass
        self.journal = journal
        self.plan = plan
        self.keep_units = keep_units
        self.adjust_energy = adjust_energy and "recorder" in hass.config.components
        self.remove_old_device = remove_old_device
        self.force = force
        self.ereg = er.async_get(hass)
        self.dreg = dr.async_get(hass)
        self.record: dict[str, Any] = {}

    def validate(self) -> None:
        """Raise when the plan must not run."""
        if not self.plan.pairs:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="nothing_to_migrate"
            )
        blocking = self.plan.blocking
        if self.force:
            blocking = [c for c in blocking if c.code in NOT_FORCEABLE]
        if blocking:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="plan_blocked",
                translation_placeholders={
                    "reasons": "; ".join(f"{c.code}: {c.message}" for c in blocking)
                },
            )

    def _start_record(self, dry_run: bool) -> None:
        old_device = self.dreg.async_get(self.plan.old_device_id)
        new_device = self.dreg.async_get(self.plan.new_device_id)
        assert old_device is not None and new_device is not None
        entries = [
            self.ereg.async_get(eid)
            for p in self.plan.pairs
            for eid in (p.old.entity_id, p.new.entity_id)
        ]
        self.record = self.journal.new_record(
            dry_run=dry_run,
            old_device_id=self.plan.old_device_id,
            new_device_id=self.plan.new_device_id,
            options={
                "keep_units": self.keep_units,
                "adjust_energy": self.adjust_energy,
                "remove_old_device": self.remove_old_device,
                "force": self.force,
            },
            plan=self.plan.as_dict(),
            snapshot={
                "old_device": _device_snapshot(old_device),
                "new_device": _device_snapshot(new_device),
                "entities": [_entry_snapshot(e) for e in entries if e is not None],
            },
        )

    async def async_run(self, dry_run: bool = False) -> dict[str, Any]:
        """Execute the plan and return the journal record."""
        self.validate()
        self._start_record(dry_run)
        if dry_run:
            self.record["status"] = "dry_run"
            await self.journal.async_save()
            return self.record
        await self.journal.async_save()
        try:
            await self._async_run()
        except Exception as err:
            _LOGGER.exception("Replacing %s failed", self.plan.old_device_name)
            self.record["status"] = "failed"
            self.record["error"] = repr(err)
            raise
        else:
            self.record["status"] = "done"
        finally:
            self.record["finished_at"] = dt_util.utcnow().isoformat()
            await self.journal.async_save()
        return self.record

    def _warn(self, message: str) -> None:
        _LOGGER.warning("%s: %s", self.plan.old_device_name, message)
        self.record["warnings"].append(message)

    async def _async_run(self) -> None:
        pairs = self.plan.pairs
        old_ids = [p.old.entity_id for p in pairs]
        new_ids = [p.new.entity_id for p in pairs]
        all_old = old_ids + [d.entity_id for d in self.plan.unmatched_old]

        meters = await self._async_meter_readings(pairs)

        # 1. retire the old device
        old_device = self.dreg.async_get(self.plan.old_device_id)
        assert old_device is not None
        if old_device.disabled_by is None:
            self.dreg.async_update_device(old_device.id, disabled_by=dr.DeviceEntryDisabler.USER)
        if not await _wait_until(lambda: not _loaded(self.hass, all_old), UNLOAD_TIMEOUT):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="old_not_unloaded",
                translation_placeholders={
                    "entities": ", ".join(sorted(_loaded(self.hass, all_old)))
                },
            )
        Journal.step(self.record, "old_device_disabled")

        # 2. unload the new entities
        for eid in new_ids:
            entry = self.ereg.async_get(eid)
            if entry is not None and entry.disabled_by is None:
                self.ereg.async_update_entity(eid, disabled_by=er.RegistryEntryDisabler.USER)
        if not await _wait_until(lambda: not _loaded(self.hass, new_ids), UNLOAD_TIMEOUT):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="new_not_unloaded",
                translation_placeholders={
                    "entities": ", ".join(sorted(_loaded(self.hass, new_ids)))
                },
            )
        Journal.step(self.record, "new_entities_unloaded")

        # 3. swap the registry entries
        config_entry_ids: set[str] = set()
        for pair in pairs:
            self._swap(pair)
            if pair.new.config_entry_id:
                config_entry_ids.add(pair.new.config_entry_id)
        Journal.step(self.record, "entities_migrated", count=len(pairs))

        # 4. device settings, reload
        self._copy_device_settings()
        expected = [
            eid
            for eid in old_ids
            if (entry := self.ereg.async_get(eid)) is not None and not entry.disabled
        ]
        reloaded = await self._async_wait_for_reload(expected, config_entry_ids)

        # 5. clean up
        if reloaded:
            await self._async_remove_tombstones(new_ids)
        else:
            self._warn(
                "The new entities did not come back; the tombstone entries are kept "
                "and Home Assistant should be restarted"
            )
        leftover = await self._async_update_references()
        if self.remove_old_device:
            await self._async_remove_old_device()
        if self.adjust_energy:
            self._schedule_sum_adjustments(meters)
        await self._async_report_issues(leftover)

    async def _async_meter_readings(self, pairs: list[EntityPair]) -> dict[str, tuple[float, str]]:
        """Return the old meters' last readings in their statistics unit."""
        readings: dict[str, tuple[float, str]] = {}
        if not self.adjust_energy:
            return readings
        for pair in pairs:
            if pair.old.state_class not in SUM_STATE_CLASSES:
                continue
            if pair.new.state_class not in SUM_STATE_CLASSES:
                continue
            eid = pair.old.entity_id
            stat_unit = await async_statistics_unit(self.hass, eid)
            last = await async_last_numeric_state(self.hass, eid)
            if stat_unit is None or last is None:
                continue
            value, unit = last
            convert = unit_converter(pair.old.device_class, unit, stat_unit)
            if convert is None:
                self._warn(f"{eid}: cannot convert {unit} to {stat_unit}; sum not corrected")
                continue
            readings[eid] = (convert(value), stat_unit)
        return readings

    def _swap(self, pair: EntityPair) -> None:
        old_eid, new_eid = pair.old.entity_id, pair.new.entity_id
        new_entry = self.ereg.async_get(new_eid)
        assert new_entry is not None
        real_uid = new_entry.unique_id
        common = {
            "new_config_entry_id": new_entry.config_entry_id,
            "new_config_subentry_id": new_entry.config_subentry_id,
            "new_device_id": new_entry.device_id,
        }
        self.ereg.async_update_entity_platform(
            new_eid, new_entry.platform, new_unique_id=real_uid + TOMBSTONE_SUFFIX, **common
        )
        self.ereg.async_update_entity_platform(
            old_eid, new_entry.platform, new_unique_id=real_uid, **common
        )
        item: dict[str, Any] = {
            "entity_id": old_eid,
            "replaced_by": new_eid,
            "old_unique_id": pair.old.unique_id,
            "new_unique_id": real_uid,
            "tombstone": new_eid,
        }
        if (
            self.keep_units
            and pair.old.domain == "sensor"
            and pair.old.unit
            and pair.new.unit
            and pair.old.unit != pair.new.unit
            and units_compatible(pair.new.device_class, pair.new.unit, pair.old.unit)
        ):
            entry = self.ereg.async_get(old_eid)
            assert entry is not None
            sensor_options = dict(entry.options.get("sensor", {}))
            if "unit_of_measurement" not in sensor_options:
                sensor_options["unit_of_measurement"] = pair.old.unit
                self.ereg.async_update_entity_options(old_eid, "sensor", sensor_options)
                item["display_unit"] = pair.old.unit
        self.record["migrated"].append(item)

    def _copy_device_settings(self) -> None:
        old = self.dreg.async_get(self.plan.old_device_id)
        new = self.dreg.async_get(self.plan.new_device_id)
        assert old is not None and new is not None
        changes: dict[str, Any] = {}
        if old.area_id and not new.area_id:
            changes["area_id"] = old.area_id
        if old.name_by_user and not new.name_by_user:
            changes["name_by_user"] = old.name_by_user
        if old.labels - new.labels:
            changes["labels"] = old.labels | new.labels
        if changes:
            self.dreg.async_update_device(new.id, **changes)
        Journal.step(
            self.record,
            "device_settings_copied",
            changes={k: sorted(v) if isinstance(v, set) else v for k, v in changes.items()},
        )

    async def _async_wait_for_reload(self, expected: list[str], config_entry_ids: set[str]) -> bool:
        """Wait until the migrated entities are loaded again."""

        def all_loaded() -> bool:
            return _loaded(self.hass, expected) == set(expected)

        if await _wait_until(all_loaded, RELOAD_FALLBACK_AFTER):
            Journal.step(self.record, "reloaded", by="home_assistant")
            return True
        for entry_id in config_entry_ids:
            entry = self.hass.config_entries.async_get_entry(entry_id)
            if entry is None or not entry.supports_unload:
                continue
            if entry.state is ConfigEntryState.LOADED:
                await self.hass.config_entries.async_reload(entry_id)
        if await _wait_until(all_loaded, RELOAD_TIMEOUT):
            Journal.step(self.record, "reloaded", by="device_replacement")
            return True
        missing = sorted(set(expected) - _loaded(self.hass, expected))
        Journal.step(self.record, "reload_incomplete", missing=missing)
        return False

    async def _async_remove_tombstones(self, tombstones: list[str]) -> None:
        removed = []
        for eid in tombstones:
            entry = self.ereg.async_get(eid)
            if entry is None or not entry.unique_id.endswith(TOMBSTONE_SUFFIX):
                continue
            if eid in entity_sources(self.hass):
                self._warn(f"{eid} is loaded; tombstone kept")
                continue
            self.ereg.async_remove(eid)
            removed.append(eid)
        if removed and "recorder" in self.hass.config.components:
            # The temporary history of the new entities is not needed any more.
            from homeassistant.components.recorder import get_instance  # noqa: PLC0415

            await self.hass.services.async_call(
                "recorder", "purge_entities", {"entity_id": removed, "keep_days": 0}
            )
            get_instance(self.hass).async_clear_statistics(removed)
        Journal.step(self.record, "tombstones_removed", entities=removed)

    async def _async_update_references(self) -> list[Reference]:
        """Point device references at the new device; return what could not be."""
        old, new = self.plan.old_device_id, self.plan.new_device_id
        rewritten = await async_rewrite_device_id(self.hass, old, new)
        leftover = await async_find_references(self.hass, [], [old])
        Journal.step(
            self.record,
            "references_updated",
            rewritten=rewritten,
            not_rewritten=[r.as_dict() for r in leftover],
        )
        for ref in leftover:
            self._warn(f"{ref.kind} {ref.name} still refers to the old device")
        return leftover

    async def _async_report_issues(self, leftover: list[Reference]) -> None:
        unmapped = {d.entity_id for d in self.plan.unmatched_old}
        still_used = await async_find_references(self.hass, unmapped, [])
        self.record["issues"] = async_create_replacement_issues(
            self.hass,
            record_id=self.record["id"],
            old_device_name=self.plan.old_device_name,
            new_device_name=self.plan.new_device_name,
            unmapped_in_use=still_used,
            unsupported=[u for u in self.plan.device_automations if not u.supported],
            not_rewritten=leftover,
        )

    async def _async_remove_old_device(self) -> None:
        device = self.dreg.async_get(self.plan.old_device_id)
        if device is None:
            return
        for entry_id in list(device.config_entries):
            config_entry = self.hass.config_entries.async_get_entry(entry_id)
            if config_entry is None:
                continue
            integration = await async_get_integration(self.hass, config_entry.domain)
            component = await integration.async_get_component()
            remove = getattr(component, "async_remove_config_entry_device", None)
            if remove is None or not await remove(self.hass, config_entry, device):
                self._warn(
                    f"{config_entry.title} does not allow removing {self.plan.old_device_name}"
                )
                continue
            self.dreg.async_update_device(device.id, remove_config_entry_id=entry_id)
        Journal.step(
            self.record,
            "old_device_removed",
            removed=self.dreg.async_get(self.plan.old_device_id) is None,
        )

    def _schedule_sum_adjustments(self, meters: dict[str, tuple[float, str]]) -> None:
        for pair in self.plan.pairs:
            eid = pair.old.entity_id
            if eid not in meters:
                continue
            previous, stat_unit = meters[eid]
            item: dict[str, Any] = {
                "statistic_id": eid,
                "previous": previous,
                "unit": stat_unit,
                "state": "waiting_for_reading",
            }
            self.record["sum_adjustments"].append(item)
            SumAdjuster(
                self.hass, self.journal, item, pair.new.state_class, pair.new.device_class
            ).async_start()


class SumAdjuster:
    """Wait for the first reading of the new meter, then correct the sum."""

    def __init__(
        self,
        hass: HomeAssistant,
        journal: Journal,
        item: dict[str, Any],
        state_class: str | None,
        device_class: str | None,
    ) -> None:
        """Initialise the adjuster for one journal item."""
        self.hass = hass
        self.journal = journal
        self.item = item
        self.state_class = state_class
        self.device_class = device_class
        self._unsubs: list[Callable[[], None]] = []

    @callback
    def async_start(self) -> None:
        """Start waiting for the first numeric reading."""
        eid = self.item["statistic_id"]
        state = self.hass.states.get(eid)
        if state is not None and to_float(state.state) is not None:
            self._first_reading(state)
            return
        self._unsubs.append(async_track_state_change_event(self.hass, eid, self._state_changed))
        self._unsubs.append(
            async_track_point_in_utc_time(
                self.hass,
                self._timeout,
                dt_util.utcnow() + timedelta(seconds=FIRST_READING_TIMEOUT),
            )
        )

    def _stop(self) -> None:
        while self._unsubs:
            self._unsubs.pop()()

    @callback
    def _state_changed(self, event: Event[EventStateChangedData]) -> None:
        state = event.data["new_state"]
        if state is not None and to_float(state.state) is not None:
            self._stop()
            self._first_reading(state)

    @callback
    def _timeout(self, _now: datetime) -> None:
        self._stop()
        self.item["state"] = "no_reading"
        self.hass.async_create_task(self.journal.async_save())

    @callback
    def _first_reading(self, state: Any) -> None:
        jump = sum_jump(
            state_class=self.state_class,
            device_class=self.device_class,
            previous=self.item["previous"],
            first=state,
            stat_unit=self.item["unit"],
        )
        if jump is None:
            self.item["state"] = "not_convertible"
            self.hass.async_create_task(self.journal.async_save())
            return
        start = period_start(state.last_updated)
        self.item.update(
            first=state.state,
            first_unit=state.attributes.get("unit_of_measurement"),
            start=start.isoformat(),
            adjustment=-jump,
            apply_after=apply_after(start).isoformat(),
            state="scheduled",
        )
        self.hass.async_create_task(self.journal.async_save())
        schedule_adjustment(self.hass, self.journal, self.item)


@callback
def schedule_adjustment(hass: HomeAssistant, journal: Journal, item: dict[str, Any]) -> None:
    """Apply a scheduled sum adjustment once its period is compiled."""

    @callback
    def _apply(_now: datetime | None = None) -> None:
        if item["adjustment"]:
            adjust_sum(hass, item)
        item["state"] = "applied"
        item["applied_at"] = dt_util.utcnow().isoformat()
        hass.async_create_task(journal.async_save())

    when = dt_util.parse_datetime(item["apply_after"])
    if when is None or when <= dt_util.utcnow():
        _apply()
    else:
        async_track_point_in_utc_time(hass, _apply, when)


@callback
def resume_adjustments(hass: HomeAssistant, journal: Journal) -> None:
    """Resume sum adjustments that were scheduled before a restart."""
    changed = False
    for record in journal.records:
        for item in record.get("sum_adjustments", []):
            if item.get("state") == "scheduled":
                schedule_adjustment(hass, journal, item)
            elif item.get("state") == "waiting_for_reading":
                item["state"] = "interrupted"
                changed = True
    if changed:
        hass.async_create_task(journal.async_save())
