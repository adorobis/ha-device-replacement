"""Build a replacement plan from the registries and the state machine."""

from __future__ import annotations

from collections.abc import Mapping

from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.entity import entity_sources

from .compat import check_pair
from .const import DOMAIN, MATCH_THRESHOLD, TOMBSTONE_SUFFIX
from .family import MAX_FAMILY_SIZE, device_name, family, name_tokens_by_device, pair_devices
from .matcher import match, score
from .models import Check, EntityDescriptor, EntityPair, Level, Plan
from .references import async_device_automation_uses, async_find_references, registry_ids


def describe(hass: HomeAssistant, entry: er.RegistryEntry) -> EntityDescriptor:
    """Return the descriptor of a registry entry."""
    state = hass.states.get(entry.entity_id)
    capabilities = entry.capabilities or {}
    options = capabilities.get("options")
    return EntityDescriptor(
        entity_id=entry.entity_id,
        registry_id=entry.id,
        domain=entry.domain,
        platform=entry.platform,
        unique_id=entry.unique_id,
        config_entry_id=entry.config_entry_id,
        device_id=entry.device_id,
        translation_key=entry.translation_key,
        name=entry.original_name or entry.name,
        device_class=entry.device_class or entry.original_device_class,
        unit=entry.unit_of_measurement,
        state_class=capabilities.get("state_class"),
        entity_category=str(entry.entity_category) if entry.entity_category else None,
        supported_features=entry.supported_features or 0,
        options=tuple(options) if isinstance(options, list) else None,
        disabled_by=str(entry.disabled_by) if entry.disabled_by else None,
        loaded=entry.entity_id in entity_sources(hass),
        available=state is not None and state.state != STATE_UNAVAILABLE,
    )


def device_entities(hass: HomeAssistant, device_id: str) -> list[er.RegistryEntry]:
    """Return the registry entries of a device, tombstones excluded."""
    return [
        e
        for e in er.async_entries_for_device(
            er.async_get(hass), device_id, include_disabled_entities=True
        )
        if not e.unique_id.endswith(TOMBSTONE_SUFFIX)
    ]


def _get_device(hass: HomeAssistant, device_id: str) -> dr.DeviceEntry:
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="device_not_found",
            translation_placeholders={"device_id": device_id},
        )
    return device


def _device_checks(
    old_family: list[dr.DeviceEntry],
    new_family: list[dr.DeviceEntry],
    device_mapping: Mapping[str, str | None],
) -> list[Check]:
    checks: list[Check] = []
    old_ids = {d.id for d in old_family}
    new_ids = {d.id for d in new_family}
    if old_ids & new_ids:
        checks.append(
            Check(Level.BLOCKING, "same_device", "Old and new device are the same device")
        )
    for side, devices in (("old", old_family), ("new", new_family)):
        if len(devices) > MAX_FAMILY_SIZE:
            checks.append(
                Check(
                    Level.BLOCKING,
                    "too_many_sub_devices",
                    f"{device_name(devices[0])} has {len(devices) - 1} sub-devices, like a "
                    "hub; pick the device itself, or leave out the sub-devices",
                    {"side": side, "sub_devices": len(devices) - 1},
                )
            )
    seen: set[str] = set()
    for old_id, new_id in device_mapping.items():
        if old_id not in old_ids:
            checks.append(
                Check(
                    Level.BLOCKING,
                    "device_mapping_unknown_old",
                    f"Device {old_id} is not part of the old device",
                    {"device_id": old_id},
                )
            )
        if new_id is not None and new_id not in new_ids:
            checks.append(
                Check(
                    Level.BLOCKING,
                    "device_mapping_unknown_new",
                    f"Device {new_id} is not part of the new device",
                    {"device_id": new_id},
                )
            )
        if new_id is not None and new_id in seen:
            checks.append(
                Check(
                    Level.BLOCKING,
                    "device_mapping_duplicate_new",
                    f"Device {new_id} is paired more than once",
                    {"device_id": new_id},
                )
            )
        if new_id is not None:
            seen.add(new_id)
    return checks


def build_plan(
    hass: HomeAssistant,
    old_device_id: str,
    new_device_id: str,
    mapping: Mapping[str, str | None] | None = None,
    auto_match: bool = True,
    *,
    include_sub_devices: bool = True,
    device_mapping: Mapping[str, str | None] | None = None,
) -> Plan:
    """Return the plan for replacing old_device_id with new_device_id.

    Both devices come with their sub-devices unless include_sub_devices is False.
    mapping maps old entity_ids to new entity_ids; None leaves an old entity
    unmapped. Old entities missing from mapping are matched automatically unless
    auto_match is False. device_mapping overrides the pairing of sub-devices.
    """
    _get_device(hass, old_device_id)
    _get_device(hass, new_device_id)
    old_family = family(hass, old_device_id, include_sub_devices)
    new_family = family(hass, new_device_id, include_sub_devices)
    old_name, new_name = device_name(old_family[0]), device_name(new_family[0])
    device_mapping = dict(device_mapping or {})
    device_checks = _device_checks(old_family, new_family, device_mapping)
    device_pairs = pair_devices(old_family, new_family) | device_mapping

    old = {e.entity_id: describe(hass, e) for d in old_family for e in device_entities(hass, d.id)}
    new = {e.entity_id: describe(hass, e) for d in new_family for e in device_entities(hass, d.id)}
    online = sorted(d.entity_id for d in old.values() if d.loaded and d.available)
    if online:
        device_checks.append(
            Check(
                Level.BLOCKING,
                "old_device_online",
                "The old device still has available entities; replace it only when it "
                "is offline (use force to override)",
                {"entities": online},
            )
        )
    offline_new = sorted(d.entity_id for d in new.values() if d.loaded and not d.available)
    if offline_new:
        device_checks.append(
            Check(
                Level.WARNING,
                "new_device_unavailable",
                "Some entities of the new device are unavailable",
                {"entities": offline_new},
            )
        )

    for config_entry_id in {d.config_entry_id for d in new.values() if d.config_entry_id}:
        config_entry = hass.config_entries.async_get_entry(config_entry_id)
        if config_entry is not None and not config_entry.supports_unload:
            device_checks.append(
                Check(
                    Level.WARNING,
                    "restart_required",
                    f"{config_entry.title} cannot be reloaded; Home Assistant must be "
                    "restarted to finish the replacement",
                    {"config_entry_id": config_entry_id},
                )
            )

    pairs: list[EntityPair] = []
    used_new: set[str] = set()
    mapping = dict(mapping or {})
    for old_id, new_id in mapping.items():
        if old_id not in old:
            device_checks.append(
                Check(
                    Level.BLOCKING,
                    "mapping_unknown_old",
                    f"{old_id} does not belong to {old_name}",
                    {"entity_id": old_id},
                )
            )
            continue
        if new_id is None:
            continue
        if new_id not in new:
            device_checks.append(
                Check(
                    Level.BLOCKING,
                    "mapping_unknown_new",
                    f"{new_id} does not belong to {new_name}",
                    {"entity_id": new_id},
                )
            )
            continue
        if new_id in used_new:
            device_checks.append(
                Check(
                    Level.BLOCKING,
                    "mapping_duplicate_new",
                    f"{new_id} is mapped more than once",
                    {"entity_id": new_id},
                )
            )
            continue
        used_new.add(new_id)
        o, n = old[old_id], new[new_id]
        pairs.append(EntityPair(o, n, score(o, n), "user", check_pair(o, n)))

    if auto_match:
        rest_old = [d for k, d in old.items() if k not in mapping]
        rest_new = [d for k, d in new.items() if k not in used_new]
        words = name_tokens_by_device(old_family + new_family)
        for o, n, points in match(
            rest_old,
            rest_new,
            MATCH_THRESHOLD,
            device_tokens=words,
            device_pairs=device_pairs,
        ):
            used_new.add(n.entity_id)
            pairs.append(EntityPair(o, n, points, "auto", check_pair(o, n)))

    paired_old = {p.old.entity_id for p in pairs}
    pairs.sort(key=lambda p: p.old.entity_id)
    return Plan(
        old_device_id=old_device_id,
        new_device_id=new_device_id,
        old_device_name=old_name,
        new_device_name=new_name,
        pairs=pairs,
        unmatched_old=[d for k, d in sorted(old.items()) if k not in paired_old],
        unmatched_new=[d for k, d in sorted(new.items()) if k not in used_new],
        device_checks=device_checks,
        old_devices=[(d.id, device_name(d)) for d in old_family],
        new_devices=[(d.id, device_name(d)) for d in new_family],
        device_pairs=device_pairs,
    )


async def async_build_plan(
    hass: HomeAssistant,
    old_device_id: str,
    new_device_id: str,
    mapping: Mapping[str, str | None] | None = None,
    auto_match: bool = True,
    *,
    include_sub_devices: bool = True,
    device_mapping: Mapping[str, str | None] | None = None,
) -> Plan:
    """Return the plan, with what refers to the old devices and unmapped entities."""
    plan = build_plan(
        hass,
        old_device_id,
        new_device_id,
        mapping,
        auto_match,
        include_sub_devices=include_sub_devices,
        device_mapping=device_mapping,
    )
    unmapped = [d.entity_id for d in plan.unmatched_old]
    old_ids = [device_id for device_id, _ in plan.old_devices]
    plan.references = await async_find_references(hass, unmapped, old_ids)
    for eid in unmapped:
        used = [r for r in plan.references if r.target == eid]
        if used:
            plan.device_checks.append(
                Check(
                    Level.WARNING,
                    "unmapped_entity_in_use",
                    f"{eid} is not mapped but is used in "
                    + ", ".join(f"{r.kind} {r.name}" for r in used),
                    {"entity_id": eid, "used_in": [r.item for r in used]},
                )
            )
    mapped = registry_ids(hass, [p.old.entity_id for p in plan.pairs])
    new_ids = [device_id for device_id, _ in plan.new_devices]
    new_names = dict(plan.new_devices)
    for old_id, new_id in plan.device_pairs.items():
        plan.device_automations.extend(
            await async_device_automation_uses(hass, old_id, new_id, mapped, new_ids)
        )
    for use in plan.device_automations:
        if not use.supported:
            what = f"{use.name} uses the device {use.section} {use.type}" + (
                f" ({use.subtype})" if use.subtype else ""
            )
            if use.offered_on:
                message = (
                    f"{what}; {new_names.get(use.offered_on, use.offered_on)} offers it, "
                    "pick it there after the replacement"
                )
            else:
                message = f"{what}, which {plan.new_device_name} does not offer"
            plan.device_checks.append(
                Check(Level.WARNING, "device_automation_unsupported", message, use.as_dict())
            )
    return plan
