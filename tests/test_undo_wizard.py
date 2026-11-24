"""Undo a replacement, and replace or undo through the UI wizard."""

from __future__ import annotations

from pathlib import Path

from homeassistant.components.lovelace import LOVELACE_DATA
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
import pytest

from custom_components.device_replacement.const import DOMAIN, TOMBSTONE_SUFFIX

from .conftest import DEVICES
from .test_execute import _call, _device_id, _go_offline

pytestmark = pytest.mark.parametrize("expected_lingering_timers", [True])

OLD_UIDS = {
    "sensor.old_plug_power": "old1_power",
    "sensor.old_plug_energy": "old1_energy",
    "switch.old_plug_relay": "old1_relay",
}


async def _undo(hass: HomeAssistant, **data) -> dict:
    return await hass.services.async_call(DOMAIN, "undo", data, blocking=True, return_response=True)


async def test_undo(hass: HomeAssistant, setup_config, tmp_path: Path) -> None:
    """Everything goes back: registry, units, device settings, references, issues."""
    ereg, dreg = er.async_get(hass), dr.async_get(hass)
    old_id, new_id = setup_config["old"], _device_id(hass, "new1")
    await _go_offline(hass)
    record = await _call(hass, "execute", adjust_energy=False)
    assert record["status"] == "done"
    assert record["issues"]

    # While the old device is disabled, a reload of its integration (or a restart)
    # creates disabled entries for its entities: they hold the old unique_ids.
    old_entry = next(
        e for e in hass.config_entries.async_entries("fakedev") if e.data["serial"] == "old1"
    )
    await hass.config_entries.async_reload(old_entry.entry_id)
    await hass.async_block_till_done()
    assert ereg.async_get_entity_id("sensor", "fakedev", "old1_power") == (
        "sensor.old_plug_power_2"
    )

    DEVICES["old1"].online = True
    undone = await _undo(hass)
    await hass.async_block_till_done()
    assert undone["status"] == "undone", undone

    for eid, uid in OLD_UIDS.items():
        entry = ereg.async_get(eid)
        assert entry.unique_id == uid
        assert entry.device_id == old_id
        assert entry.disabled_by is None
    assert "unit_of_measurement" not in ereg.async_get("sensor.old_plug_power").options.get(
        "sensor", {}
    )
    assert hass.states.get("sensor.old_plug_power").state == "480"
    # The new device's entities are back under their own ids.
    assert ereg.async_get("sensor.new_plug_power").unique_id == "new1_apower"
    assert hass.states.get("sensor.new_plug_power").state == "0.5"
    assert ereg.async_get("sensor.old_plug_power_2") is None
    assert not [e for e in ereg.entities.values() if e.unique_id.endswith(TOMBSTONE_SUFFIX)]
    assert dreg.async_get(old_id).disabled_by is None

    text = (tmp_path / "automations.yaml").read_text()
    assert old_id in text
    assert new_id not in text
    dashboard = await hass.data[LOVELACE_DATA].dashboards[None].async_load(False)
    assert dashboard["views"][0]["cards"][0]["device_id"] == old_id
    assert not [i for (d, _), i in ir.async_get(hass).issues.items() if d == DOMAIN]

    with pytest.raises(ServiceValidationError, match="can be undone was found"):
        await _undo(hass)
    with pytest.raises(ServiceValidationError, match="already undone"):
        await _undo(hass, record_id=record["id"])


async def test_undo_refused_after_removal(hass: HomeAssistant, devices) -> None:
    """Without the old device there is nothing to go back to."""
    await _go_offline(hass)
    record = await _call(hass, "execute", adjust_energy=False, remove_old_device=True)
    with pytest.raises(ServiceValidationError, match="old device was removed"):
        await _undo(hass, record_id=record["id"])


async def _finish(hass: HomeAssistant, result: dict) -> dict:
    """Let a progress step finish and return the next result."""
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    await hass.async_block_till_done()
    result = await hass.config_entries.options.async_configure(result["flow_id"])
    if result["type"] is FlowResultType.SHOW_PROGRESS_DONE:
        result = await hass.config_entries.options.async_configure(result["flow_id"])
    return result


async def test_wizard_replace_and_undo(hass: HomeAssistant, devices) -> None:
    """Pick devices, adjust the mapping, confirm; then undo from the menu."""
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    old_id, new_id = _device_id(hass, "old1"), _device_id(hass, "new1")
    flows = hass.config_entries.options

    result = await flows.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    result = await flows.async_configure(result["flow_id"], {"next_step_id": "undo"})
    assert result == result | {"type": FlowResultType.ABORT, "reason": "nothing_to_undo"}
    assert result["description_placeholders"] == {"recent": "- none yet"}

    result = await flows.async_init(entry.entry_id)
    result = await flows.async_configure(result["flow_id"], {"next_step_id": "replace"})
    assert result["step_id"] == "replace"
    result = await flows.async_configure(
        result["flow_id"], {"old_integration": "fakedev", "new_device_id": new_id}
    )
    assert result["step_id"] == "old_device"
    result = await flows.async_configure(
        result["flow_id"], {"old_device_id": new_id, "include_sub_devices": True}
    )
    assert result["errors"] == {"base": "same_device"}
    result = await flows.async_configure(
        result["flow_id"], {"old_device_id": old_id, "include_sub_devices": True}
    )
    assert result["step_id"] == "mapping"
    schema = {str(k): k.default() for k in result["data_schema"].schema}
    assert schema == {
        "sensor.old_plug_device_temperature": "__none__",
        "sensor.old_plug_energy": "sensor.new_plug_energy",
        "sensor.old_plug_power": "sensor.new_plug_power",
        "switch.old_plug_relay": "switch.new_plug_relay",
    }
    # Two old entities onto one new entity is refused.
    bad = schema | {"sensor.old_plug_energy": "sensor.new_plug_power"}
    result = await flows.async_configure(result["flow_id"], bad)
    assert result["errors"] == {"base": "duplicate_new"}
    # Keep energy on the old device.
    mapping = schema | {"sensor.old_plug_energy": "__none__"}
    result = await flows.async_configure(result["flow_id"], mapping)
    assert result["step_id"] == "confirm"
    summary = result["description_placeholders"]["summary"]
    assert "**sensor.old_plug_power** ← sensor.new_plug_power" in summary
    assert "old_device_online" not in summary  # the message is shown, not the code
    assert "still has available entities" in summary
    assert "force" in {str(k) for k in result["data_schema"].schema}

    options = {"keep_units": True, "adjust_energy": False, "remove_old_device": False}
    result = await flows.async_configure(result["flow_id"], options | {"force": False})
    assert result["errors"] == {"base": "blocked"}
    result = await flows.async_configure(result["flow_id"], options | {"force": True})
    result = await _finish(hass, result)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "replaced"
    assert (
        "sensor.old_plug_power ← sensor.new_plug_power"
        in (result["description_placeholders"]["summary"])
    )
    ereg = er.async_get(hass)
    assert ereg.async_get("sensor.old_plug_power").unique_id == "new1_apower"
    assert ereg.async_get("sensor.old_plug_energy").unique_id == "old1_energy"

    result = await flows.async_init(entry.entry_id)
    result = await flows.async_configure(result["flow_id"], {"next_step_id": "undo"})
    assert result["step_id"] == "undo"
    [option] = result["data_schema"].schema["record_id"].config["options"]
    assert option["label"].endswith("Old Plug → New Plug, 2 entities (latest)")
    record_id = result["data_schema"].schema[next(iter(result["data_schema"].schema))]
    result = await flows.async_configure(
        result["flow_id"], {"record_id": entry.runtime_data.records[-1]["id"]}
    )
    result = await _finish(hass, result)
    assert result["reason"] == "undone", result
    assert ereg.async_get("sensor.old_plug_power").unique_id == "old1_power"
    assert record_id is not None

    # Nothing left to undo: the dialog says why for each recent replacement.
    result = await flows.async_init(entry.entry_id)
    result = await flows.async_configure(result["flow_id"], {"next_step_id": "undo"})
    assert result["reason"] == "nothing_to_undo"
    recent = result["description_placeholders"]["recent"]
    assert recent.startswith("- ")
    assert "Old Plug → New Plug, 2 entities: it was already undone" in recent
