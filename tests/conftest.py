"""Fixtures: a fake integration "fakedev" with one config entry per device.

That is the Shelly-like case: every device has its own config entry, so the old
device's entry stays set up (offline) while the new one is reloaded.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import patch

from homeassistant.components.device_automation import DEVICE_TRIGGER_BASE_SCHEMA
from homeassistant.components.lovelace import LOVELACE_DATA
from homeassistant.components.sensor import SensorEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry, ConfigFlow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.setup import async_setup_component
from homeassistant.util.yaml import load_yaml
import pytest
from pytest_homeassistant.common import (
    MockConfigEntry,
    MockModule,
    MockPlatform,
    mock_config_flow,
    mock_integration,
    mock_platform,
)
import voluptuous as vol

from custom_components.device_replacement.const import DOMAIN

FAKE = "fakedev"


@dataclass
class FakeDevice:
    """A device of the fake integration and what its entities report."""

    serial: str
    name: str
    entities: list[dict[str, Any]]
    online: bool = True
    values: dict[str, Any] = field(default_factory=dict)
    main_device_id: str | None = None


DEVICES: dict[str, FakeDevice] = {}


def _info(dev: FakeDevice, spec: dict[str, Any] | None = None) -> DeviceInfo:
    """Main device, or the sub-device a spec names with "sub" (like a Shelly channel)."""
    if spec and (sub := spec.get("sub")):
        return DeviceInfo(
            identifiers={(FAKE, f"{dev.serial}-{sub}")},
            name=spec.get("sub_name", sub),
            manufacturer="Fake",
            via_device_id=dev.main_device_id,
        )
    return DeviceInfo(identifiers={(FAKE, dev.serial)}, name=dev.name, manufacturer="Fake")


class FakeSensor(SensorEntity):
    """Sensor of a fake device."""

    _attr_has_entity_name = True

    def __init__(self, dev: FakeDevice, spec: dict[str, Any]) -> None:
        """Initialise from a spec."""
        self._dev, self._key = dev, spec["key"]
        self._attr_unique_id = f"{dev.serial}_{spec['key']}"
        self._attr_name = spec.get("name")
        self._attr_device_class = spec.get("device_class")
        self._attr_native_unit_of_measurement = spec.get("unit")
        self._attr_state_class = spec.get("state_class")
        self._attr_translation_key = spec.get("translation_key")
        self._attr_device_info = _info(dev, spec)

    @property
    def available(self) -> bool:
        """Return if the device is online."""
        return self._dev.online

    @property
    def native_value(self) -> Any:
        """Return the value."""
        return self._dev.values.get(self._key)


class FakeSwitch(SwitchEntity):
    """Switch of a fake device."""

    _attr_has_entity_name = True

    def __init__(self, dev: FakeDevice, spec: dict[str, Any]) -> None:
        """Initialise from a spec."""
        self._dev, self._key = dev, spec["key"]
        self._attr_unique_id = f"{dev.serial}_{spec['key']}"
        self._attr_name = spec.get("name")
        self._attr_device_info = _info(dev, spec)

    @property
    def available(self) -> bool:
        """Return if the device is online."""
        return self._dev.online

    @property
    def is_on(self) -> bool:
        """Return the state."""
        return bool(self._dev.values.get(self._key))


def _platform(domain: str, cls: type) -> MockPlatform:
    async def setup(
        hass: HomeAssistant, entry: ConfigEntry, add: AddConfigEntryEntitiesCallback
    ) -> None:
        dev = DEVICES[entry.data["serial"]]
        # Sub-devices point at the main device by id, as Shelly does.
        dev.main_device_id = (
            dr.async_get(hass)
            .async_get_or_create(
                config_entry_id=entry.entry_id,
                identifiers={(FAKE, dev.serial)},
                name=dev.name,
                manufacturer="Fake",
            )
            .id
        )
        add([cls(dev, s) for s in dev.entities if s["domain"] == domain])

    return MockPlatform(async_setup_entry=setup)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load custom_components/device_replacement."""


@pytest.fixture(autouse=True)
def fast_timeouts() -> Iterable[None]:
    """Do not wait for HA's 30 s disabled_by reload; reload directly."""
    with (
        patch("custom_components.device_replacement.executor.RELOAD_FALLBACK_AFTER", 0.2),
        patch("custom_components.device_replacement.executor.RELOAD_TIMEOUT", 5.0),
        patch("custom_components.device_replacement.executor.UNLOAD_TIMEOUT", 5.0),
    ):
        yield


class FakeDeviceTrigger:
    """device_trigger platform: only old1 has a button."""

    TRIGGER_SCHEMA = DEVICE_TRIGGER_BASE_SCHEMA.extend(
        {vol.Required("type"): str, vol.Optional("subtype"): str}
    )

    def __init__(self, hass: HomeAssistant) -> None:
        """Remember hass to look up devices."""
        self.hass = hass

    async def async_get_triggers(self, hass: HomeAssistant, device_id: str) -> list[dict]:
        """Return the triggers of a device."""
        device = dr.async_get(hass).async_get(device_id)
        if device is None or (FAKE, "old1") not in device.identifiers:
            return []
        return [
            {
                "platform": "device",
                "domain": FAKE,
                "device_id": device_id,
                "type": "button_short_press",
                "subtype": "button_1",
            }
        ]

    async def async_validate_trigger_config(self, hass: HomeAssistant, config: dict) -> dict:
        """Validate a trigger."""
        return self.TRIGGER_SCHEMA(config)

    async def async_attach_trigger(self, hass, config, action, trigger_info):
        """Attach a trigger that never fires."""
        return lambda: None


class FakeFlow(ConfigFlow):
    """Config flow of the fake integration (never run)."""

    VERSION = 1


@pytest.fixture
async def fakedev(hass: HomeAssistant) -> AsyncIterator[None]:
    """Register the fake integration."""
    DEVICES.clear()

    async def setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
        await hass.config_entries.async_forward_entry_setups(entry, ["sensor", "switch"])

    async def unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
        await hass.config_entries.async_unload_platforms(entry, ["sensor", "switch"])

    module = MockModule(FAKE, async_setup_entry=setup_entry, async_unload_entry=unload_entry)

    async def remove_device(hass: HomeAssistant, entry: ConfigEntry, device: Any) -> bool:
        return True

    module.async_remove_config_entry_device = remove_device
    mock_integration(hass, module)
    mock_platform(hass, f"{FAKE}.config_flow", None)
    mock_platform(hass, f"{FAKE}.device_trigger", FakeDeviceTrigger(hass))
    mock_platform(hass, f"{FAKE}.sensor", _platform("sensor", FakeSensor))
    mock_platform(hass, f"{FAKE}.switch", _platform("switch", FakeSwitch))
    with mock_config_flow(FAKE, FakeFlow):
        yield


async def add_device(hass: HomeAssistant, dev: FakeDevice) -> MockConfigEntry:
    """Set up one fake device with its own config entry."""
    DEVICES[dev.serial] = dev
    entry = MockConfigEntry(domain=FAKE, title=dev.name, data={"serial": dev.serial})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


OLD_ENTITIES = [
    {"domain": "sensor", "key": "power", "name": "Power", "device_class": "power",
     "unit": "W", "state_class": "measurement"},
    {"domain": "sensor", "key": "energy", "name": "Energy", "device_class": "energy",
     "unit": "kWh", "state_class": "total_increasing"},
    {"domain": "sensor", "key": "temperature", "name": "Device temperature",
     "device_class": "temperature", "unit": "°C", "state_class": "measurement"},
    {"domain": "switch", "key": "relay", "name": "Relay"},
]  # fmt: skip

NEW_ENTITIES = [
    {"domain": "sensor", "key": "apower", "name": "Power", "device_class": "power",
     "unit": "kW", "state_class": "measurement"},
    {"domain": "sensor", "key": "aenergy", "name": "Energy", "device_class": "energy",
     "unit": "Wh", "state_class": "total_increasing"},
    {"domain": "sensor", "key": "voltage", "name": "Voltage", "device_class": "voltage",
     "unit": "V", "state_class": "measurement"},
    {"domain": "switch", "key": "switch0", "name": "Relay"},
]  # fmt: skip


@pytest.fixture
async def devices(hass: HomeAssistant, fakedev: None) -> tuple[MockConfigEntry, MockConfigEntry]:
    """Set up Device Replacement, an old plug (W, kWh) and a new plug (kW, Wh)."""
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "persistent_notification", {})
    entry = MockConfigEntry(domain=DOMAIN, title="Device Replacement")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    old = await add_device(
        hass,
        FakeDevice(
            "old1",
            "Old Plug",
            OLD_ENTITIES,
            values={"power": 480, "energy": 1234.5, "temperature": 40, "relay": True},
        ),
    )
    new = await add_device(
        hass,
        FakeDevice(
            "new1",
            "New Plug",
            NEW_ENTITIES,
            values={"apower": 0.5, "aenergy": 12.0, "voltage": 230, "switch0": True},
        ),
    )
    return old, new


def _automations(old_device: str, relay_registry_id: str) -> str:
    return f"""
- id: relay_on
  alias: Relay on
  triggers:
    - trigger: device
      domain: switch
      device_id: {old_device}
      entity_id: {relay_registry_id}
      type: turned_on
  actions:
    - action: persistent_notification.create
      data: {{message: relay on}}
- id: button
  alias: Button press
  triggers:
    - trigger: device
      domain: fakedev
      device_id: {old_device}
      type: button_short_press
      subtype: button_1
  actions: []
- id: too_hot
  alias: Too hot
  triggers:
    - trigger: numeric_state
      entity_id: sensor.old_plug_device_temperature
      above: 80
  actions: []
"""


@pytest.fixture
async def setup_config(hass: HomeAssistant, devices, tmp_path: Path) -> dict[str, Any]:
    """Automations in automations.yaml, a YAML script and a stored dashboard."""
    hass.config.config_dir = str(tmp_path)
    old = _device_id(hass, "old1")
    relay = er.async_get(hass).async_get("switch.old_plug_relay").id
    (tmp_path / "automations.yaml").write_text(_automations(old, relay))
    (tmp_path / "configuration.yaml").write_text("automation: !include automations.yaml\n")
    assert await async_setup_component(
        hass, "automation", {"automation": load_yaml(str(tmp_path / "automations.yaml"))}
    )
    assert await async_setup_component(
        hass,
        "script",
        {
            "script": {
                "yaml_script": {
                    "alias": "YAML script",
                    "sequence": [
                        {
                            "action": "homeassistant.turn_on",
                            "target": {"device_id": old},
                        }
                    ],
                }
            }
        },
    )
    assert await async_setup_component(hass, "lovelace", {})
    await (
        hass.data[LOVELACE_DATA]
        .dashboards[None]
        .async_save({"views": [{"cards": [{"type": "device", "device_id": old}]}]})
    )
    await hass.async_block_till_done()
    return {"old": old, "relay": relay}


def _device_id(hass: HomeAssistant, serial: str) -> str:
    """Return the device id of a fake device."""
    entry = next(e for e in hass.config_entries.async_entries(FAKE) if e.data["serial"] == serial)
    device = dr.async_get(hass).async_get_device_by_identifier((FAKE, serial), entry.entry_id)
    assert device is not None
    return device.id
