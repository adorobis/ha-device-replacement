"""Repairs issues for what a replacement could not carry over."""

from __future__ import annotations

from collections import defaultdict
import hashlib
from typing import Any

from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN

_KIND_NAMES = {
    "automation": "automation",
    "script": "script",
    "scene": "scene",
    "group": "group",
    "dashboard": "dashboard",
}


def _issue_id(record_id: str, *parts: str | None) -> str:
    digest = hashlib.sha1("|".join(p or "" for p in parts).encode()).hexdigest()[:12]
    return f"{record_id}_{digest}"


@callback
def async_create_replacement_issues(
    hass: HomeAssistant,
    *,
    record_id: str,
    old_device_name: str,
    new_device_name: str,
    unmapped_in_use: list[Any],
    unsupported: list[Any],
    not_rewritten: list[Any],
) -> list[str]:
    """Create one issue per problem and return their ids."""
    issues: list[str] = []

    def create(issue_id: str, key: str, placeholders: dict[str, str]) -> None:
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=True,
            is_persistent=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key=key,
            translation_placeholders=placeholders,
            data={"record_id": record_id},
        )
        issues.append(issue_id)

    by_entity: dict[str, list[Any]] = defaultdict(list)
    for ref in unmapped_in_use:
        by_entity[ref.target].append(ref)
    for entity_id, refs in sorted(by_entity.items()):
        create(
            _issue_id(record_id, "unmapped", entity_id),
            "unmapped_entity_in_use",
            {
                "entity_id": entity_id,
                "old_device": old_device_name,
                "new_device": new_device_name,
                "used_in": ", ".join(f"{_KIND_NAMES.get(r.kind, r.kind)} {r.name}" for r in refs),
            },
        )
    for use in unsupported:
        create(
            _issue_id(record_id, "device_automation", use.automation, use.type, use.subtype),
            "device_automation_unsupported",
            {
                "automation": use.name,
                "section": use.section,
                "type": use.type + (f" ({use.subtype})" if use.subtype else ""),
                "old_device": old_device_name,
                "new_device": new_device_name,
            },
        )
    for ref in not_rewritten:
        create(
            _issue_id(record_id, "reference", ref.kind, ref.item),
            "device_reference_not_rewritten",
            {
                "kind": _KIND_NAMES.get(ref.kind, ref.kind),
                "name": ref.name,
                "old_device": old_device_name,
                "new_device": new_device_name,
            },
        )
    return issues


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    """Let the user confirm they have fixed the problem."""
    return ConfirmRepairFlow()
