"""Find and update what refers to the replaced device and its entities.

Mapped entities keep their entity_id, so references to them need no change. What
does need attention:

- device_id references to the old device (device triggers, conditions and actions,
  device targets, dashboard cards). They are rewritten to the new device in the files
  the UI manages (automations.yaml, scripts.yaml, scenes.yaml) and in dashboards stored
  in the UI. Anything else is reported.
- integration-specific device automations (a button's "single press" trigger) that
  the new device does not offer.
- unmapped old entities that are still used somewhere.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
import json
import logging
import re
from typing import Any

from homeassistant.config import (
    AUTOMATION_CONFIG_PATH,
    SCENE_CONFIG_PATH,
    SCRIPT_CONFIG_PATH,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

_LOGGER = logging.getLogger(__name__)

DEFAULT_DASHBOARD = "lovelace"

# (config file, domain to reload)
UI_FILES = (
    (AUTOMATION_CONFIG_PATH, "automation"),
    (SCRIPT_CONFIG_PATH, "script"),
    (SCENE_CONFIG_PATH, "scene"),
)


@dataclass(frozen=True, slots=True)
class Reference:
    """One place that refers to an entity or a device."""

    kind: str  # automation, script, scene, group or dashboard
    item: str  # entity_id of the automation/script/..., or the dashboard url path
    name: str
    target: str  # the referenced entity_id or device_id

    def as_dict(self) -> dict[str, str]:
        """Return a JSON-serialisable dict."""
        return {"kind": self.kind, "item": self.item, "name": self.name, "target": self.target}


@dataclass(frozen=True, slots=True)
class DeviceAutomationUse:
    """An integration-specific device trigger, condition or action of the old device."""

    automation: str
    name: str
    section: str  # trigger, condition or action
    domain: str
    type: str
    subtype: str | None
    supported: bool
    # Another device of the new family that offers it, when the paired one does not.
    offered_on: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict."""
        return {
            "automation": self.automation,
            "name": self.name,
            "section": self.section,
            "domain": self.domain,
            "type": self.type,
            "subtype": self.subtype,
            "supported": self.supported,
            "offered_on": self.offered_on,
        }


def _friendly(hass: HomeAssistant, entity_id: str) -> str:
    state = hass.states.get(entity_id)
    return state.name if state is not None else entity_id


def _loaded(hass: HomeAssistant, domain: str) -> bool:
    return domain in hass.config.components


def _config_refs(hass: HomeAssistant, target: str, is_device: bool) -> Iterator[Reference]:
    """Yield the automations, scripts, scenes and groups referring to target."""
    finders: list[tuple[str, Any]] = []
    if _loaded(hass, "automation"):
        from homeassistant.components import automation  # noqa: PLC0415

        finders.append(
            (
                "automation",
                automation.automations_with_device
                if is_device
                else automation.automations_with_entity,
            )
        )
    if _loaded(hass, "script"):
        from homeassistant.components import script  # noqa: PLC0415

        finders.append(
            ("script", script.scripts_with_device if is_device else script.scripts_with_entity)
        )
    if not is_device:
        if _loaded(hass, "scene"):
            from homeassistant.components.homeassistant import scene  # noqa: PLC0415

            finders.append(("scene", scene.scenes_with_entity))
        if _loaded(hass, "group"):
            from homeassistant.components import group  # noqa: PLC0415

            finders.append(("group", group.groups_with_entity))
    for kind, finder in finders:
        for item in finder(hass, target):
            yield Reference(kind, item, _friendly(hass, item), target)


def _dashboards(hass: HomeAssistant) -> dict[str | None, Any]:
    if not _loaded(hass, "lovelace"):
        return {}
    from homeassistant.components.lovelace import LOVELACE_DATA  # noqa: PLC0415

    data = hass.data.get(LOVELACE_DATA)
    return dict(data.dashboards) if data is not None else {}


async def _async_load_dashboard(dashboard: Any) -> dict[str, Any] | None:
    try:
        return await dashboard.async_load(False)
    except HomeAssistantError:  # ConfigNotFound: the dashboard was never saved
        return None


def _pattern(target: str) -> re.Pattern[str]:
    # sensor.power must not match sensor.power_2 or xsensor.power
    return re.compile(rf"(?<![\w.]){re.escape(target)}(?!\w)")


async def async_find_references(
    hass: HomeAssistant, entity_ids: Iterable[str], device_ids: Iterable[str]
) -> list[Reference]:
    """Return every place that refers to one of the entities or devices."""
    refs: list[Reference] = []
    for eid in entity_ids:
        refs.extend(_config_refs(hass, eid, is_device=False))
    for device_id in device_ids:
        refs.extend(_config_refs(hass, device_id, is_device=True))
    targets = [*entity_ids, *device_ids]
    for url_path, dashboard in _dashboards(hass).items():
        config = await _async_load_dashboard(dashboard)
        if config is None:
            continue
        text = json.dumps(config)
        title = (dashboard.config or {}).get("title") if dashboard.config else None
        for target in targets:
            if _pattern(target).search(text):
                refs.append(
                    Reference(
                        "dashboard",
                        url_path or DEFAULT_DASHBOARD,
                        title or url_path or "Overview",
                        target,
                    )
                )
    return sorted(set(refs), key=lambda r: (r.target, r.kind, r.item))


def _walk(node: Any, section: str) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield (section, dict) for every dict in an automation config."""
    if isinstance(node, dict):
        yield section, node
        for key, value in node.items():
            child = section
            if key in ("trigger", "triggers"):
                child = "trigger"
            elif key in ("condition", "conditions") and not isinstance(value, str):
                child = "condition"
            elif key in ("action", "actions", "sequence") and not isinstance(value, str):
                child = "action"
            yield from _walk(value, child)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value, section)


async def async_device_automation_uses(
    hass: HomeAssistant,
    old_device_id: str,
    new_device_id: str | None,
    mapped_registry_ids: dict[str, str],
    new_family_ids: list[str] | None = None,
) -> list[DeviceAutomationUse]:
    """Return the integration-specific device automations of the old device.

    They are checked against the paired new device (None: it has no counterpart).
    When another device of the new family offers the same one, offered_on says which.

    Entity-based ones (they carry the entity's registry id) keep working for mapped
    entities and are skipped; mapped_registry_ids maps registry ids of mapped old
    entities to their entity_id.
    """
    if not _loaded(hass, "automation"):
        return []
    from homeassistant.components import automation  # noqa: PLC0415
    from homeassistant.components.automation import DATA_COMPONENT  # noqa: PLC0415
    from homeassistant.components.device_automation import (  # noqa: PLC0415
        DeviceAutomationType,
        async_get_device_automations,
    )

    uses: list[DeviceAutomationUse] = []
    component = hass.data.get(DATA_COMPONENT)
    if component is None:
        return uses
    candidates = list(dict.fromkeys([*([new_device_id] if new_device_id else []),
                                     *(new_family_ids or [])]))  # fmt: skip
    # section → device id → offered (domain, type, subtype)
    offered: dict[str, dict[str, set[tuple[str, str, str | None]]]] = {}
    for section, kind in (
        ("trigger", DeviceAutomationType.TRIGGER),
        ("condition", DeviceAutomationType.CONDITION),
        ("action", DeviceAutomationType.ACTION),
    ):
        try:
            found = await async_get_device_automations(hass, kind, candidates) if candidates else {}
        except HomeAssistantError:
            found = {}
        offered[section] = {
            device_id: {(a.get("domain"), a.get("type"), a.get("subtype")) for a in items}
            for device_id, items in found.items()
        }
    for automation_id in automation.automations_with_device(hass, old_device_id):
        entity = component.get_entity(automation_id)
        config = getattr(entity, "raw_config", None) if entity is not None else None
        if not config:
            continue
        for section, node in _walk(config, "automation"):
            if node.get("device_id") != old_device_id or "domain" not in node:
                continue
            if "type" not in node or section not in offered:
                continue
            entity_ref = node.get("entity_id")
            if entity_ref and entity_ref in mapped_registry_ids:
                continue  # entity-based, the entity was mapped: keeps working
            key = (node["domain"], node["type"], node.get("subtype"))
            by_device = offered[section]
            supported = (
                not entity_ref
                and new_device_id is not None
                and key in by_device.get(new_device_id, set())
            )
            offered_on = None
            if not supported and not entity_ref:
                offered_on = next(
                    (
                        d
                        for d in candidates
                        if d != new_device_id and key in by_device.get(d, set())
                    ),
                    None,
                )
            uses.append(
                DeviceAutomationUse(
                    automation=automation_id,
                    name=_friendly(hass, automation_id),
                    section=section,
                    domain=node["domain"],
                    type=node["type"],
                    subtype=node.get("subtype"),
                    supported=supported,
                    offered_on=offered_on,
                )
            )
    return uses


def _replace_in_file(path: str, old: str, new: str) -> int:
    try:
        with open(path, encoding="utf-8") as file:
            text = file.read()
    except FileNotFoundError:
        return 0
    count = text.count(old)
    if count:
        with open(path, "w", encoding="utf-8") as file:
            file.write(text.replace(old, new))
    return count


def _replace_in(node: Any, old: str, new: str) -> tuple[Any, int]:
    if isinstance(node, str):
        return (node.replace(old, new), node.count(old)) if old in node else (node, 0)
    if isinstance(node, list):
        out, total = [], 0
        for value in node:
            replaced, count = _replace_in(value, old, new)
            out.append(replaced)
            total += count
        return out, total
    if isinstance(node, dict):
        result, total = {}, 0
        for key, value in node.items():
            replaced, count = _replace_in(value, old, new)
            result[key] = replaced
            total += count
        return result, total
    return node, 0


async def async_rewrite_device_id(
    hass: HomeAssistant, old_device_id: str, new_device_id: str
) -> dict[str, int]:
    """Replace old_device_id with new_device_id where Home Assistant manages the config.

    Returns the number of replacements per file or dashboard.
    """
    counts: dict[str, int] = {}
    for filename, domain in UI_FILES:
        count = await hass.async_add_executor_job(
            _replace_in_file, hass.config.path(filename), old_device_id, new_device_id
        )
        if count:
            counts[filename] = count
            if hass.services.has_service(domain, "reload"):
                await hass.services.async_call(domain, "reload", blocking=True)
    for url_path, dashboard in _dashboards(hass).items():
        if getattr(dashboard, "mode", None) != "storage":
            continue
        config = await _async_load_dashboard(dashboard)
        if config is None:
            continue
        config, count = _replace_in(config, old_device_id, new_device_id)
        if count:
            await dashboard.async_save(config)
            counts[f"dashboard:{url_path or DEFAULT_DASHBOARD}"] = count
    if counts:
        _LOGGER.info("Replaced device %s with %s: %s", old_device_id, new_device_id, counts)
    return counts


def registry_ids(hass: HomeAssistant, entity_ids: Iterable[str]) -> dict[str, str]:
    """Return {registry id: entity_id} for the given entity_ids."""
    ereg = er.async_get(hass)
    return {e.id: e.entity_id for eid in entity_ids if (e := ereg.async_get(eid)) is not None}
