"""Compatibility checks between an old entity and the entity replacing it."""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.components.sensor import UNIT_CONVERTERS

from .models import Check, EntityDescriptor, Level

SUM_STATE_CLASSES = {"total", "total_increasing"}


def unit_converter(
    device_class: str | None, from_unit: str | None, to_unit: str | None
) -> Callable[[float], float] | None:
    """Return a function converting from_unit to to_unit, or None if impossible."""
    if from_unit == to_unit:
        return lambda value: value
    if device_class is None or from_unit is None or to_unit is None:
        return None
    converter = UNIT_CONVERTERS.get(device_class)
    if converter is None:
        return None
    if from_unit not in converter.VALID_UNITS or to_unit not in converter.VALID_UNITS:
        return None
    return converter.converter_factory(from_unit, to_unit)


def units_compatible(device_class: str | None, a: str | None, b: str | None) -> bool:
    """Return True when values in unit a can be shown in unit b."""
    return unit_converter(device_class, a, b) is not None


def check_pair(old: EntityDescriptor, new: EntityDescriptor) -> list[Check]:
    """Return the checks for replacing old with new."""
    checks: list[Check] = []

    if old.domain != new.domain:
        checks.append(
            Check(
                Level.BLOCKING,
                "domain_mismatch",
                f"{old.entity_id} is a {old.domain}, {new.entity_id} is a {new.domain}",
            )
        )
        return checks

    if old.device_class != new.device_class:
        checks.append(
            Check(
                Level.WARNING,
                "device_class_changed",
                f"Device class changes from {old.device_class} to {new.device_class}",
                {"old": old.device_class, "new": new.device_class},
            )
        )

    if old.unit != new.unit:
        if old.unit is None or new.unit is None:
            checks.append(
                Check(
                    Level.WARNING,
                    "unit_added_or_removed",
                    f"Unit changes from {old.unit} to {new.unit}",
                    {"old": old.unit, "new": new.unit},
                )
            )
        elif units_compatible(new.device_class, new.unit, old.unit):
            checks.append(
                Check(
                    Level.INFO,
                    "unit_converted",
                    f"New entity reports {new.unit}; it is shown in {old.unit} as before",
                    {"old": old.unit, "new": new.unit},
                )
            )
        else:
            checks.append(
                Check(
                    Level.BLOCKING,
                    "unit_incompatible",
                    f"{new.unit} cannot be converted to {old.unit}; history and "
                    "statistics would mix units",
                    {"old": old.unit, "new": new.unit},
                )
            )

    if old.state_class != new.state_class:
        level = (
            Level.BLOCKING
            if old.state_class is not None
            and new.state_class is not None
            and (old.state_class in SUM_STATE_CLASSES) != (new.state_class in SUM_STATE_CLASSES)
            else Level.WARNING
        )
        checks.append(
            Check(
                level,
                "state_class_changed",
                f"State class changes from {old.state_class} to {new.state_class}; "
                "long-term statistics may stop or change type",
                {"old": old.state_class, "new": new.state_class},
            )
        )
    elif new.state_class in SUM_STATE_CLASSES:
        checks.append(
            Check(
                Level.INFO,
                "meter_swap",
                "Meter reading changes with the device; the statistics sum is corrected "
                "so the swap does not count as consumption",
            )
        )

    missing = old.supported_features & ~new.supported_features
    if missing:
        checks.append(
            Check(
                Level.WARNING,
                "features_missing",
                "The new entity does not support all features of the old one",
                {"missing_supported_features": missing},
            )
        )

    if old.options and new.options is not None:
        lost = sorted(set(old.options) - set(new.options))
        if lost:
            checks.append(
                Check(
                    Level.WARNING,
                    "options_missing",
                    "Some options of the old entity are not offered by the new one",
                    {"missing": lost},
                )
            )

    return checks
