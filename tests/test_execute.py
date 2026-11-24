"""Tests of planning and executing a replacement with the fake integration."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)
import pytest

from custom_components.device_replacement.const import DOMAIN, TOMBSTONE_SUFFIX

from .conftest import DEVICES, _device_id

pytestmark = pytest.mark.parametrize("expected_lingering_timers", [True])


async def _call(hass: HomeAssistant, service: str, **data: Any) -> dict[str, Any]:
    return await hass.services.async_call(
        DOMAIN,
        service,
        {"old_device_id": _device_id(hass, "old1"), "new_device_id": _device_id(hass, "new1"),
         **data},
        blocking=True,
        return_response=True,
    )  # fmt: skip


async def _go_offline(hass: HomeAssistant) -> None:
    DEVICES["old1"].online = False
    await hass.services.async_call(
        "homeassistant", "update_entity",
        {"entity_id": ["sensor.old_plug_power", "sensor.old_plug_energy",
                       "sensor.old_plug_device_temperature", "switch.old_plug_relay"]},
        blocking=True,
    )  # fmt: skip
    await hass.async_block_till_done()


async def test_plan(hass: HomeAssistant, devices) -> None:
    """The plan pairs power, energy and relay and blocks while the old plug is online."""
    plan = await _call(hass, "plan")
    assert {p["old"]: p["new"] for p in plan["pairs"]} == {
        "sensor.old_plug_energy": "sensor.new_plug_energy",
        "sensor.old_plug_power": "sensor.new_plug_power",
        "switch.old_plug_relay": "switch.new_plug_relay",
    }
    assert [d["entity_id"] for d in plan["unmatched_old"]] == ["sensor.old_plug_device_temperature"]
    assert [d["entity_id"] for d in plan["unmatched_new"]] == ["sensor.new_plug_voltage"]
    assert [c["code"] for c in plan["device_checks"]] == ["old_device_online"]
    assert not plan["can_execute"]
    power = next(p for p in plan["pairs"] if p["old"] == "sensor.old_plug_power")
    assert [c["code"] for c in power["checks"]] == ["unit_converted"]

    await _go_offline(hass)
    plan = await _call(hass, "plan")
    assert plan["can_execute"]

    plan = await _call(hass, "plan", mapping={"sensor.nope": None})
    assert [c["code"] for c in plan["device_checks"]] == ["mapping_unknown_old"]
    with pytest.raises(ServiceValidationError, match="mapping_unknown_old"):
        await _call(hass, "execute", mapping={"sensor.nope": None}, force=True)


async def test_execute_blocked_while_online(hass: HomeAssistant, devices) -> None:
    """An online old device blocks unless forced."""
    with pytest.raises(ServiceValidationError, match="old_device_online"):
        await _call(hass, "execute")


async def test_dry_run_changes_nothing(hass: HomeAssistant, devices) -> None:
    """A dry run only writes a journal record."""
    await _go_offline(hass)
    record = await _call(hass, "execute", dry_run=True)
    assert record["status"] == "dry_run"
    entry = er.async_get(hass).async_get("sensor.old_plug_power")
    assert entry.unique_id == "old1_power"


async def test_execute(hass: HomeAssistant, devices) -> None:
    """The old entities move to the new plug with their ids and settings."""
    ereg, dreg = er.async_get(hass), dr.async_get(hass)
    old_id, new_id = _device_id(hass, "old1"), _device_id(hass, "new1")
    boiler = ar.async_get(hass).async_create("Boiler room")
    dreg.async_update_device(old_id, area_id=boiler.id, name_by_user="Pump plug")
    ereg.async_update_entity("sensor.old_plug_power", name="Pump power")
    ereg.async_update_entity("switch.old_plug_relay", icon="mdi:pump")
    registry_id = ereg.async_get("switch.old_plug_relay").id
    await _go_offline(hass)

    record = await _call(hass, "execute", adjust_energy=False)
    await hass.async_block_till_done()

    assert record["status"] == "done", record
    for eid, uid in (
        ("sensor.old_plug_power", "new1_apower"),
        ("sensor.old_plug_energy", "new1_aenergy"),
        ("switch.old_plug_relay", "new1_switch0"),
    ):
        entry = ereg.async_get(eid)
        assert entry.unique_id == uid
        assert entry.device_id == new_id
        assert entry.disabled_by is None
    # Settings, registry id and the old unit are kept.
    assert ereg.async_get("sensor.old_plug_power").name == "Pump power"
    assert ereg.async_get("switch.old_plug_relay").icon == "mdi:pump"
    assert ereg.async_get("switch.old_plug_relay").id == registry_id
    power = hass.states.get("sensor.old_plug_power")
    assert power.state == "500.0"
    assert power.attributes["unit_of_measurement"] == "W"
    assert hass.states.get("switch.old_plug_relay").state == "on"
    # The new entity ids and the tombstones are gone.
    for eid in ("sensor.new_plug_power", "sensor.new_plug_energy", "switch.new_plug_relay"):
        assert ereg.async_get(eid) is None
        assert hass.states.get(eid) is None
    assert not [e for e in ereg.entities.values() if e.unique_id.endswith(TOMBSTONE_SUFFIX)]
    # Unmatched entities stay where they were.
    assert hass.states.get("sensor.new_plug_voltage").state == "230"
    temperature = ereg.async_get("sensor.old_plug_device_temperature")
    assert temperature.device_id == old_id
    assert temperature.disabled_by is er.RegistryEntryDisabler.DEVICE
    # Device settings.
    new_device = dreg.async_get(new_id)
    assert new_device.area_id == boiler.id
    assert new_device.name_by_user == "Pump plug"
    assert dreg.async_get(old_id).disabled_by is dr.DeviceEntryDisabler.USER

    journal = await hass.services.async_call(
        DOMAIN, "journal", {}, blocking=True, return_response=True
    )
    assert journal["records"][-1]["id"] == record["id"]
    assert [m["entity_id"] for m in record["migrated"]] == [
        "sensor.old_plug_energy",
        "sensor.old_plug_power",
        "switch.old_plug_relay",
    ]


async def test_user_disabled_entity_stays_disabled(hass: HomeAssistant, devices) -> None:
    """An entity the user had disabled is migrated but stays disabled."""
    ereg = er.async_get(hass)
    ereg.async_update_entity("sensor.old_plug_power", disabled_by=er.RegistryEntryDisabler.USER)
    await hass.async_block_till_done()
    await _go_offline(hass)
    record = await _call(hass, "execute", adjust_energy=False)
    assert record["status"] == "done"
    entry = ereg.async_get("sensor.old_plug_power")
    assert entry.unique_id == "new1_apower"
    assert entry.disabled_by is er.RegistryEntryDisabler.USER


async def test_mapping_and_remove_old_device(hass: HomeAssistant, devices) -> None:
    """A user mapping overrides the matcher; the old device can be removed."""
    await _go_offline(hass)
    old_id = _device_id(hass, "old1")
    record = await _call(
        hass,
        "execute",
        mapping={"sensor.old_plug_energy": None, "switch.old_plug_relay": "switch.new_plug_relay"},
        auto_match=False,
        remove_old_device=True,
        adjust_energy=False,
    )
    assert [m["entity_id"] for m in record["migrated"]] == ["switch.old_plug_relay"]
    assert dr.async_get(hass).async_get(old_id) is None
    # Removing the device removed its remaining entities too.
    assert er.async_get(hass).async_get("sensor.old_plug_energy") is None
    assert hass.states.get("sensor.new_plug_power") is not None
