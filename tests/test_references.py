"""Device references are rewritten; what cannot be carried over becomes a Repairs issue."""

from __future__ import annotations

from pathlib import Path

from homeassistant.components.lovelace import LOVELACE_DATA
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
import pytest

from custom_components.device_replacement.const import DOMAIN

from .conftest import _device_id
from .test_execute import _call, _go_offline

pytestmark = pytest.mark.parametrize("expected_lingering_timers", [True])


async def test_plan_reports_references(hass: HomeAssistant, setup_config) -> None:
    """The plan lists the unmapped entity in use and the unsupported trigger."""
    await _go_offline(hass)
    plan = await _call(hass, "plan")
    checks = {c["code"]: c for c in plan["device_checks"]}
    assert checks["unmapped_entity_in_use"]["details"] == {
        "entity_id": "sensor.old_plug_device_temperature",
        "used_in": ["automation.too_hot"],
    }
    assert checks["device_automation_unsupported"]["details"]["type"] == "button_short_press"
    assert [u["automation"] for u in plan["device_automations"]] == ["automation.button_press"]
    assert {(r["kind"], r["item"]) for r in plan["references"]} == {
        ("automation", "automation.button_press"),
        ("automation", "automation.relay_on"),
        ("automation", "automation.too_hot"),
        ("dashboard", "lovelace"),
        ("script", "script.yaml_script"),
    }
    assert plan["can_execute"]


async def test_execute_rewrites_and_reports(
    hass: HomeAssistant, setup_config, tmp_path: Path
) -> None:
    """Device IDs are rewritten where HA manages the config; the rest becomes issues."""
    old, new = setup_config["old"], _device_id(hass, "new1")
    await _go_offline(hass)
    record = await _call(hass, "execute", adjust_energy=False)
    await hass.async_block_till_done()
    assert record["status"] == "done", record

    step = next(s for s in record["steps"] if s["step"] == "references_updated")
    assert step["rewritten"] == {"automations.yaml": 2, "dashboard:lovelace": 1}
    assert [r["item"] for r in step["not_rewritten"]] == ["script.yaml_script"]

    text = (tmp_path / "automations.yaml").read_text()
    assert old not in text
    assert text.count(new) == 2
    dashboard = await hass.data[LOVELACE_DATA].dashboards[None].async_load(False)
    assert dashboard["views"][0]["cards"][0]["device_id"] == new

    # The entity-based device trigger follows the relay to the new device.
    hass.states.async_set("switch.old_plug_relay", "off")
    await hass.async_block_till_done()
    hass.states.async_set("switch.old_plug_relay", "on")
    await hass.async_block_till_done()
    assert hass.states.get("automation.relay_on").attributes["last_triggered"] is not None

    issues = {
        i.translation_key: i
        for (domain, _), i in ir.async_get(hass).issues.items()
        if domain == DOMAIN
    }
    assert set(issues) == {
        "unmapped_entity_in_use",
        "device_automation_unsupported",
        "device_reference_not_rewritten",
    }
    assert issues["unmapped_entity_in_use"].translation_placeholders["used_in"] == (
        "automation Too hot"
    )
    assert issues["device_reference_not_rewritten"].translation_placeholders["name"] == (
        "YAML script"
    )
    assert sorted(record["issues"]) == sorted(i.issue_id for i in issues.values())


async def test_issue_can_be_confirmed(hass: HomeAssistant, setup_config, hass_client) -> None:
    """Confirming the repair removes the issue."""
    assert await async_setup_component(hass, "repairs", {})
    await _go_offline(hass)
    record = await _call(hass, "execute", adjust_energy=False)
    issue_id = record["issues"][0]
    client = await hass_client()
    resp = await client.post(
        "/api/repairs/issues/fix", json={"handler": DOMAIN, "issue_id": issue_id}
    )
    flow = await resp.json()
    assert flow["step_id"] == "confirm"
    resp = await client.post(f"/api/repairs/issues/fix/{flow['flow_id']}", json={})
    assert (await resp.json())["type"] == "create_entry"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
