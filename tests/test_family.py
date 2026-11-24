"""Devices with sub-devices (a Shelly 2.5 replaced by a Plus 2PM): one replacement."""

from __future__ import annotations

from typing import Any

from homeassistant import loader
from homeassistant.components.lovelace import LOVELACE_DATA
from homeassistant.config_entries import ConfigEntryDisabler
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant.common import MockConfigEntry

from custom_components.device_replacement.const import DOMAIN

from .conftest import FAKE, FakeDevice, add_device

pytestmark = pytest.mark.parametrize("expected_lingering_timers", [True])

CH0, CH1 = {"sub": "ch0"}, {"sub": "ch1"}

OLD_25 = [
    {"domain": "sensor", "key": "temperature", "name": "Device temperature",
     "device_class": "temperature", "unit": "°C", "state_class": "measurement"},
    {"domain": "sensor", "key": "rssi", "name": "Signal", "device_class": "signal_strength",
     "unit": "dBm", "state_class": "measurement"},
    {"domain": "switch", "key": "relay0", "name": None, **CH0, "sub_name": "kotlownia old"},
    {"domain": "sensor", "key": "power0", "name": "Power", "device_class": "power", "unit": "W",
     "state_class": "measurement", **CH0, "sub_name": "kotlownia old"},
    {"domain": "sensor", "key": "energy0", "name": "Energy", "device_class": "energy",
     "unit": "kWh", "state_class": "total_increasing", **CH0, "sub_name": "kotlownia old"},
    {"domain": "switch", "key": "relay1", "name": None, **CH1, "sub_name": "schowek old"},
    {"domain": "sensor", "key": "power1", "name": "Power", "device_class": "power", "unit": "W",
     "state_class": "measurement", **CH1, "sub_name": "schowek old"},
    {"domain": "sensor", "key": "energy1", "name": "Energy", "device_class": "energy",
     "unit": "kWh", "state_class": "total_increasing", **CH1, "sub_name": "schowek old"},
    # On the 2.5 the button sits on the channel; on the Plus on the main device.
    {"domain": "sensor", "key": "button", "name": "Button", **CH1, "sub_name": "schowek old"},
]  # fmt: skip

NEW_PLUS = [
    {"domain": "sensor", "key": "rssi", "name": "Signal", "device_class": "signal_strength",
     "unit": "dBm", "state_class": "measurement"},
    {"domain": "sensor", "key": "button", "name": "Button"},
    {"domain": "switch", "key": "switch0", "name": None, **CH0, "sub_name": "kotlownia"},
    {"domain": "sensor", "key": "power0", "name": "Power", "device_class": "power", "unit": "W",
     "state_class": "measurement", **CH0, "sub_name": "kotlownia"},
    {"domain": "sensor", "key": "energy0", "name": "Energy", "device_class": "energy",
     "unit": "Wh", "state_class": "total_increasing", **CH0, "sub_name": "kotlownia"},
    {"domain": "sensor", "key": "returned0", "name": "Energy returned", "device_class": "energy",
     "unit": "kWh", "state_class": "total_increasing", **CH0, "sub_name": "kotlownia"},
    {"domain": "switch", "key": "switch1", "name": None, **CH1, "sub_name": "schowek"},
    {"domain": "sensor", "key": "power1", "name": "Power", "device_class": "power", "unit": "W",
     "state_class": "measurement", **CH1, "sub_name": "schowek"},
    {"domain": "sensor", "key": "energy1", "name": "Energy", "device_class": "energy",
     "unit": "Wh", "state_class": "total_increasing", **CH1, "sub_name": "schowek"},
    {"domain": "sensor", "key": "returned1", "name": "Energy returned", "device_class": "energy",
     "unit": "kWh", "state_class": "total_increasing", **CH1, "sub_name": "schowek"},
]  # fmt: skip

EXPECTED = {
    "sensor.kotlownia_old_signal": "sensor.kotlownia_signal",
    "sensor.schowek_old_button": "sensor.kotlownia_button",
    "switch.kotlownia_old": "switch.kotlownia",
    "sensor.kotlownia_old_power": "sensor.kotlownia_power",
    "sensor.kotlownia_old_energy": "sensor.kotlownia_energy",
    "switch.schowek_old": "switch.schowek",
    "sensor.schowek_old_power": "sensor.schowek_power",
    "sensor.schowek_old_energy": "sensor.schowek_energy",
}


def _dev(hass: HomeAssistant, serial: str, sub: str | None = None) -> str:
    entry = next(e for e in hass.config_entries.async_entries(FAKE) if e.data["serial"] == serial)
    identifier = (FAKE, f"{serial}-{sub}" if sub else serial)
    device = dr.async_get(hass).async_get_device_by_identifier(identifier, entry.entry_id)
    assert device is not None
    return device.id


@pytest.fixture
async def shellys(hass: HomeAssistant, fakedev: None) -> tuple[MockConfigEntry, MockConfigEntry]:
    """A Shelly-like 2.5 (offline) and Plus 2PM, each a main device with two channels."""
    assert await async_setup_component(hass, "homeassistant", {})
    entry = MockConfigEntry(domain=DOMAIN, title="Device Replacement")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    old = await add_device(
        hass,
        FakeDevice(
            "old25",
            "Kotłownia old",
            OLD_25,
            online=False,
            values={"relay0": True, "power0": 40, "energy0": 10.0, "temperature": 45},
        ),
    )
    new = await add_device(
        hass,
        FakeDevice(
            "plus",
            "Kotłownia",
            NEW_PLUS,
            values={"switch0": True, "power0": 41, "energy0": 5.0, "rssi": -60},
        ),
    )
    return old, new


async def _call(hass: HomeAssistant, service: str, **data: Any) -> dict[str, Any]:
    return await hass.services.async_call(
        DOMAIN,
        service,
        {"old_device_id": _dev(hass, "old25"), "new_device_id": _dev(hass, "plus"), **data},
        blocking=True,
        return_response=True,
    )


async def test_family_plan(hass: HomeAssistant, shellys) -> None:
    """Channels pair by name, entities map across the family, even to another device."""
    plan = await _call(hass, "plan")
    assert {d["old_name"]: d["new_name"] for d in plan["devices"]} == {
        "Kotłownia old": "Kotłownia",
        "kotlownia old": "kotlownia",
        "schowek old": "schowek",
    }
    assert {p["old"]: p["new"] for p in plan["pairs"]} == EXPECTED
    assert [d["entity_id"] for d in plan["unmatched_old"]] == [
        "sensor.kotlownia_old_device_temperature"
    ]
    assert plan["can_execute"], plan["device_checks"]

    # Without sub-devices only the main devices take part.
    plan = await _call(hass, "plan", include_sub_devices=False)
    assert [d["old_name"] for d in plan["devices"]] == ["Kotłownia old"]
    assert {p["old"] for p in plan["pairs"]} == {"sensor.kotlownia_old_signal"}

    # The pairing can be overridden.
    swapped = {_dev(hass, "old25", "ch0"): _dev(hass, "plus", "ch1")}
    plan = await _call(hass, "plan", device_mapping=swapped)
    pairs = {p["old"]: p["new"] for p in plan["pairs"]}
    assert pairs["switch.kotlownia_old"] == "switch.schowek"


async def test_family_execute_and_undo(hass: HomeAssistant, shellys) -> None:
    """Settings and device references follow each channel; undo restores all."""
    ereg, dreg = er.async_get(hass), dr.async_get(hass)
    old_ch0, new_ch0 = _dev(hass, "old25", "ch0"), _dev(hass, "plus", "ch0")
    boiler = ar.async_get(hass).async_create("Boiler room")
    dreg.async_update_device(old_ch0, area_id=boiler.id)
    assert await async_setup_component(hass, "lovelace", {})
    await (
        hass.data[LOVELACE_DATA]
        .dashboards[None]
        .async_save({"views": [{"cards": [{"type": "device", "device_id": old_ch0}]}]})
    )

    new_uids = {new: ereg.async_get(new).unique_id for new in EXPECTED.values()}
    record = await _call(hass, "execute", adjust_energy=False)
    await hass.async_block_till_done()
    assert record["status"] == "done", record
    for old_eid, new_eid in EXPECTED.items():
        assert ereg.async_get(old_eid).unique_id == new_uids[new_eid]
    # The button moved from a channel to the main device.
    assert ereg.async_get("sensor.schowek_old_button").device_id == _dev(hass, "plus")
    assert ereg.async_get("switch.kotlownia_old").device_id == new_ch0
    assert hass.states.get("switch.kotlownia_old").state == "on"
    # Every old device is disabled, the channel's area went to the new channel.
    for sub in (None, "ch0", "ch1"):
        assert dreg.async_get(_dev(hass, "old25", sub)).disabled_by is dr.DeviceEntryDisabler.USER
    assert dreg.async_get(new_ch0).area_id == boiler.id
    dashboard = await hass.data[LOVELACE_DATA].dashboards[None].async_load(False)
    assert dashboard["views"][0]["cards"][0]["device_id"] == new_ch0

    undone = await hass.services.async_call(DOMAIN, "undo", {}, blocking=True, return_response=True)
    await hass.async_block_till_done()
    assert undone["status"] == "undone", undone
    assert ereg.async_get("switch.kotlownia_old").device_id == old_ch0
    assert ereg.async_get("sensor.schowek_old_button").device_id == _dev(hass, "old25", "ch1")
    assert ereg.async_get("switch.kotlownia").device_id == new_ch0
    assert dreg.async_get(new_ch0).area_id is None
    for sub in (None, "ch0", "ch1"):
        assert dreg.async_get(_dev(hass, "old25", sub)).disabled_by is None
    dashboard = await hass.data[LOVELACE_DATA].dashboards[None].async_load(False)
    assert dashboard["views"][0]["cards"][0]["device_id"] == old_ch0


async def test_disabled_old_config_entry(hass: HomeAssistant, shellys) -> None:
    """The old integration entry may be disabled: its devices can still be replaced."""
    old_entry, _ = shellys
    await hass.config_entries.async_set_disabled_by(old_entry.entry_id, ConfigEntryDisabler.USER)
    await hass.async_block_till_done()
    plan = await _call(hass, "plan")
    assert plan["can_execute"], plan["device_checks"]
    record = await _call(hass, "execute", adjust_energy=False)
    assert record["status"] == "done", record
    entry = er.async_get(hass).async_get("switch.kotlownia_old")
    assert entry.disabled_by is None
    assert hass.states.get("switch.kotlownia_old").state == "on"


async def test_wizard_lists_disabled_devices_and_pairs(hass: HomeAssistant, shellys) -> None:
    """The wizard offers disabled devices and a sub-device pairing step."""
    dreg = dr.async_get(hass)
    dreg.async_update_device(_dev(hass, "old25"), disabled_by=dr.DeviceEntryDisabler.USER)
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    flows = hass.config_entries.options

    result = await flows.async_init(entry.entry_id)
    result = await flows.async_configure(result["flow_id"], {"next_step_id": "replace"})
    integrations = result["data_schema"].schema["old_integration"].config["options"]
    assert FAKE in [o["value"] for o in integrations]
    result = await flows.async_configure(
        result["flow_id"], {"old_integration": FAKE, "new_device_id": _dev(hass, "plus")}
    )
    assert result["step_id"] == "old_device"
    labels = [o["label"] for o in result["data_schema"].schema["old_device_id"].config["options"]]
    assert "Kotłownia old (disabled) + 2 sub-devices" in labels
    assert "↳ schowek old" in labels
    result = await flows.async_configure(
        result["flow_id"], {"old_device_id": _dev(hass, "old25"), "include_sub_devices": True}
    )
    assert result["step_id"] == "devices"
    defaults = {str(k): k.default() for k in result["data_schema"].schema}
    assert defaults == {
        "Kotłownia old": _dev(hass, "plus"),
        "kotlownia old": _dev(hass, "plus", "ch0"),
        "schowek old": _dev(hass, "plus", "ch1"),
    }
    result = await flows.async_configure(result["flow_id"], defaults)
    assert result["step_id"] == "mapping"
    mapping = {str(k): k.default() for k in result["data_schema"].schema}
    assert {k: v for k, v in mapping.items() if v != "__none__"} == EXPECTED


async def test_undo_keeps_disabled_old_devices_disabled(hass: HomeAssistant, shellys) -> None:
    """Old devices disabled before the replacement stay disabled; undo does not wait."""
    dreg = dr.async_get(hass)
    for sub in (None, "ch0", "ch1"):
        dreg.async_update_device(_dev(hass, "old25", sub), disabled_by=dr.DeviceEntryDisabler.USER)
    await hass.async_block_till_done()
    record = await _call(hass, "execute", adjust_energy=False)
    assert record["status"] == "done", record
    undone = await hass.services.async_call(DOMAIN, "undo", {}, blocking=True, return_response=True)
    assert undone["undo"]["warnings"] == []
    for sub in (None, "ch0", "ch1"):
        assert dreg.async_get(_dev(hass, "old25", sub)).disabled_by is dr.DeviceEntryDisabler.USER
    entry = er.async_get(hass).async_get("switch.kotlownia_old")
    assert entry.unique_id == "old25_relay0"
    assert entry.disabled_by is er.RegistryEntryDisabler.DEVICE
    assert er.async_get(hass).async_get("switch.kotlownia").unique_id == "plus_switch0"


def _without_removal_hook(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the fake integration behave like Shelly: devices can't be removed alone."""
    module = hass.data[loader.DATA_COMPONENTS][FAKE]
    monkeypatch.delattr(module, "async_remove_config_entry_device")


async def test_remove_old_device_deletes_its_entry(
    hass: HomeAssistant, shellys, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The old integration entry holds only the old devices: it is deleted."""
    old_entry, _ = shellys
    _without_removal_hook(hass, monkeypatch)
    record = await _call(hass, "execute", adjust_energy=False, remove_old_device=True)
    assert record["status"] == "done", record
    assert record["warnings"] == []
    step = next(s for s in record["steps"] if s["step"] == "old_device_removed")
    assert step == step | {"removed": True, "removed_integration_entries": ["Kotłownia old"]}
    assert hass.config_entries.async_get_entry(old_entry.entry_id) is None
    # The migrated entities belong to the new entry and stay.
    assert hass.states.get("switch.kotlownia_old").state == "on"
    assert er.async_get(hass).async_get("sensor.kotlownia_old_device_temperature") is None


async def test_remove_old_device_keeps_shared_entry(
    hass: HomeAssistant, shellys, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An entry that holds other devices too is kept, with a clear warning."""
    old_entry, _ = shellys
    _without_removal_hook(hass, monkeypatch)
    dr.async_get(hass).async_get_or_create(
        config_entry_id=old_entry.entry_id, identifiers={(FAKE, "other")}, name="Garage"
    )
    record = await _call(hass, "execute", adjust_energy=False, remove_old_device=True)
    assert record["status"] == "done", record
    assert record["warnings"] == [
        "The fakedev integration cannot remove schowek old, kotlownia old, Kotłownia old on "
        "its own, and its entry “Kotłownia old” also holds Garage; remove the old device by "
        "hand"
    ]
    assert hass.config_entries.async_get_entry(old_entry.entry_id) is not None
