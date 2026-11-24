"""Tests of the matcher, the compatibility checks and the sum correction maths."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from homeassistant.core import State

from custom_components.device_replacement.compat import check_pair
from custom_components.device_replacement.matcher import match, score
from custom_components.device_replacement.models import EntityDescriptor, Level
from custom_components.device_replacement.statistics import period_start, sum_jump

BASE = EntityDescriptor(
    entity_id="sensor.x",
    registry_id="r",
    domain="sensor",
    platform="p",
    unique_id="u",
    config_entry_id=None,
    device_id=None,
    translation_key=None,
    name=None,
    device_class=None,
    unit=None,
    state_class=None,
    entity_category=None,
    supported_features=0,
    options=None,
    disabled_by=None,
    loaded=True,
    available=True,
)


def d(entity_id: str, **kw) -> EntityDescriptor:
    """Return a descriptor."""
    return replace(BASE, entity_id=entity_id, domain=entity_id.split(".")[0], **kw)


def test_match_prefers_device_class_and_name() -> None:
    """Power maps to power, energy to energy; unrelated entities stay unmatched."""
    old = [
        d("sensor.old_power", device_class="power", unit="W", name="Power", unique_id="a_power"),
        d("sensor.old_energy", device_class="energy", unit="kWh", name="Energy",
          state_class="total_increasing", unique_id="a_energy"),
        d("sensor.old_temp", device_class="temperature", unit="°C", name="Temperature"),
        d("switch.old_relay", name="Relay"),
    ]  # fmt: skip
    new = [
        d("sensor.new_power", device_class="power", unit="kW", name="Power", unique_id="b_apower"),
        d("sensor.new_energy", device_class="energy", unit="Wh", name="Energy",
          state_class="total_increasing", unique_id="b_aenergy"),
        d("sensor.new_voltage", device_class="voltage", unit="V", name="Voltage"),
        d("switch.new_switch", name="Relay"),
    ]  # fmt: skip
    pairs = {o.entity_id: n.entity_id for o, n, _ in match(old, new, 40, "Old", "New")}
    assert pairs == {
        "sensor.old_power": "sensor.new_power",
        "sensor.old_energy": "sensor.new_energy",
        "switch.old_relay": "switch.new_switch",
    }


def test_score_domain_mismatch_is_zero() -> None:
    """Entities of different domains never match."""
    assert score(d("sensor.a", name="Relay"), d("switch.b", name="Relay")) == 0


def test_translation_key_wins() -> None:
    """The same translation_key outweighs a different name."""
    old = d("sensor.a", translation_key="rssi", name="Signal")
    good = d("sensor.b", translation_key="rssi", name="WiFi signal strength")
    bad = d("sensor.c", name="Signal")
    assert score(old, good) > score(old, bad)


def test_unit_checks() -> None:
    """Convertible units inform, incompatible units block."""
    old = d("sensor.a", device_class="power", unit="W")
    codes = {
        c.code: c.level for c in check_pair(old, d("sensor.b", device_class="power", unit="kW"))
    }
    assert codes == {"unit_converted": Level.INFO}
    codes = {
        c.code: c.level for c in check_pair(old, d("sensor.b", device_class="current", unit="A"))
    }
    assert codes["unit_incompatible"] is Level.BLOCKING
    assert codes["device_class_changed"] is Level.WARNING


def test_state_class_checks() -> None:
    """Measurement to total blocks; a meter swap is noted."""
    old = d("sensor.a", state_class="measurement", device_class="energy", unit="kWh")
    new = d("sensor.b", state_class="total_increasing", device_class="energy", unit="kWh")
    assert [c.code for c in check_pair(old, new) if c.level is Level.BLOCKING] == [
        "state_class_changed"
    ]
    assert [c.code for c in check_pair(new, new)] == ["meter_swap"]


def test_features_and_options() -> None:
    """Lost features and options are warnings."""
    old = d("select.a", supported_features=0b11, options=("a", "b", "c"))
    new = d("select.b", supported_features=0b01, options=("a", "b"))
    checks = {c.code: c for c in check_pair(old, new)}
    assert checks["features_missing"].details == {"missing_supported_features": 0b10}
    assert checks["options_missing"].details == {"missing": ["c"]}


def _state(value: str, unit: str, **attrs) -> State:
    return State("sensor.a", value, {"unit_of_measurement": unit, **attrs})


def test_sum_jump_reset_and_difference() -> None:
    """A reset adds the whole new reading, a higher reading the difference."""
    kw = {"state_class": "total_increasing", "device_class": "energy", "stat_unit": "kWh"}
    # New meter at 12 Wh after the old one at 1234.5 kWh: reset, 0.012 kWh added.
    assert sum_jump(previous=1234.5, first=_state("12", "Wh"), **kw) == 0.012
    # New (used) meter at 2000 kWh: no reset, 765.5 kWh added.
    assert sum_jump(previous=1234.5, first=_state("2000", "kWh"), **kw) == 765.5
    # Slightly lower reading is a dip, not a reset: a negative amount is added.
    assert sum_jump(previous=1000, first=_state("950", "kWh"), **kw) == -50
    assert sum_jump(previous=1000, first=_state("unknown", "kWh"), **kw) is None
    total = {**kw, "state_class": "total"}
    assert sum_jump(previous=10, first=_state("4", "kWh"), **total) == -6
    assert sum_jump(previous=10, first=_state("4", "kWh", last_reset="2026-01-01"), **total) == 4


def test_period_start() -> None:
    """Statistics periods are 5 minutes long."""
    assert period_start(datetime(2026, 1, 1, 10, 7, 31, 5, tzinfo=UTC)) == datetime(
        2026, 1, 1, 10, 5, tzinfo=UTC
    )
