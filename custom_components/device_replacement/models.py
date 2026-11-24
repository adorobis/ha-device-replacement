"""Data model of a device replacement plan."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .references import DeviceAutomationUse, Reference


class Level(StrEnum):
    """Severity of a check."""

    INFO = "info"
    WARNING = "warning"
    BLOCKING = "blocking"


@dataclass(frozen=True, slots=True)
class Check:
    """Result of one compatibility check."""

    level: Level
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EntityDescriptor:
    """What the planner needs to know about one entity."""

    entity_id: str
    registry_id: str
    domain: str
    platform: str
    unique_id: str
    config_entry_id: str | None
    device_id: str | None
    translation_key: str | None
    name: str | None
    device_class: str | None
    unit: str | None
    state_class: str | None
    entity_category: str | None
    supported_features: int
    options: tuple[str, ...] | None
    disabled_by: str | None
    loaded: bool
    available: bool


@dataclass(slots=True)
class EntityPair:
    """An old entity and the new entity that takes its place."""

    old: EntityDescriptor
    new: EntityDescriptor
    score: int
    source: str  # "auto" or "user"
    checks: list[Check] = field(default_factory=list)

    @property
    def blocking(self) -> bool:
        """Return True when a check blocks this pair."""
        return any(c.level is Level.BLOCKING for c in self.checks)


@dataclass(slots=True)
class Plan:
    """The full plan for replacing one device with another."""

    old_device_id: str
    new_device_id: str
    old_device_name: str
    new_device_name: str
    pairs: list[EntityPair]
    unmatched_old: list[EntityDescriptor]
    unmatched_new: list[EntityDescriptor]
    device_checks: list[Check]
    references: list[Reference] = field(default_factory=list)
    device_automations: list[DeviceAutomationUse] = field(default_factory=list)
    # Devices of both families, (id, name), main device first.
    old_devices: list[tuple[str, str]] = field(default_factory=list)
    new_devices: list[tuple[str, str]] = field(default_factory=list)
    # Old device id → paired new device id (None: no counterpart).
    device_pairs: dict[str, str | None] = field(default_factory=dict)

    @property
    def blocking(self) -> list[Check]:
        """Return every blocking check of the plan."""
        checks = [c for c in self.device_checks if c.level is Level.BLOCKING]
        for pair in self.pairs:
            checks.extend(c for c in pair.checks if c.level is Level.BLOCKING)
        return checks

    @property
    def can_execute(self) -> bool:
        """Return True when nothing blocks the plan."""
        return bool(self.pairs) and not self.blocking

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable summary, used as service response."""

        def ent(d: EntityDescriptor) -> dict[str, Any]:
            return {
                "entity_id": d.entity_id,
                "name": d.name,
                "device_class": d.device_class,
                "unit": d.unit,
                "state_class": d.state_class,
            }

        def chk(c: Check) -> dict[str, Any]:
            return {k: v for k, v in asdict(c).items() if v or k == "level"}

        return {
            "old_device": {"id": self.old_device_id, "name": self.old_device_name},
            "new_device": {"id": self.new_device_id, "name": self.new_device_name},
            "can_execute": self.can_execute,
            "devices": [
                {
                    "old": old_id,
                    "old_name": old_name,
                    "new": self.device_pairs.get(old_id),
                    "new_name": dict(self.new_devices).get(self.device_pairs.get(old_id) or ""),
                }
                for old_id, old_name in self.old_devices
            ],
            "pairs": [
                {
                    "old": p.old.entity_id,
                    "new": p.new.entity_id,
                    "score": p.score,
                    "source": p.source,
                    "checks": [chk(c) for c in p.checks],
                }
                for p in self.pairs
            ],
            "unmatched_old": [ent(d) for d in self.unmatched_old],
            "unmatched_new": [ent(d) for d in self.unmatched_new],
            "device_checks": [chk(c) for c in self.device_checks],
            "references": [r.as_dict() for r in self.references],
            "device_automations": [u.as_dict() for u in self.device_automations],
        }
