"""Matching on the registry data of a real Shelly 2.5 → Shelly Plus 2PM swap.

Names, translation keys, classes, units and unique IDs are copied from a live Home
Assistant 2026.11 (the 2.5 renamed "… old" by the user, channels set to lights).
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from custom_components.device_replacement.family import name_tokens_by_device, pair_devices
from custom_components.device_replacement.matcher import match
from custom_components.device_replacement.models import EntityDescriptor

BASE = EntityDescriptor(
    entity_id="",
    registry_id="",
    domain="",
    platform="shelly",
    unique_id="",
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
    loaded=False,
    available=False,
)


def e(entity_id, device, uid, name, dc=None, unit=None, sc=None, tk=None, cat=None):
    """Return a descriptor."""
    return replace(
        BASE,
        entity_id=entity_id,
        registry_id=entity_id,
        domain=entity_id.split(".")[0],
        device_id=device,
        unique_id=uid,
        name=name,
        device_class=dc,
        unit=unit,
        state_class=sc,
        translation_key=tk,
        entity_category=cat,
    )


def dev(device_id, name, name_by_user=None):
    """Return a stand-in for a device registry entry."""
    return SimpleNamespace(id=device_id, name=name, name_by_user=name_by_user)


OLD_DEVICES = [
    dev("o", "Kotłownia", "Kotłownia old"),
    dev("o0", "kotlownia", "kotlownia old"),
    dev("o1", "schowek", "schowek old"),
]
NEW_DEVICES = [dev("n", "Kotłownia"), dev("n0", "kotlownia"), dev("n1", "schowek")]

M, P = "measurement", "total_increasing"
OLD = [
    e("binary_sensor.kotlownia_overheating", "o", "8C-device-overtemp", "Overheating", "problem", tk="overheating", cat="diagnostic"),
    e("binary_sensor.kotlownia_cloud", "o", "8C-cloud", "Cloud", "connectivity", tk="cloud", cat="diagnostic"),
    e("button.kotlownia_reboot", "o", "8C-reboot", "Restart", "restart", cat="config"),
    e("sensor.kotlownia_device_temperature", "o", "8C-device-deviceTemp", "Device temperature", "temperature", "°C", M, "device_temperature", "diagnostic"),
    e("sensor.kotlownia_voltage", "o", "8C-device-voltage", "Voltage", "voltage", "V", M),
    e("sensor.kotlownia_rssi", "o", "8C-rssi", "Signal strength", "signal_strength", "dBm", M, cat="diagnostic"),
    e("sensor.kotlownia_uptime", "o", "8C-uptime", "Uptime", "uptime", cat="diagnostic"),
    e("update.kotlownia_firmware", "o", "8C-fwupdate", "Firmware", "firmware", cat="config"),
    e("update.kotlownia_beta_firmware", "o", "8C-fwupdate_beta", "Beta firmware", "firmware", tk="beta_firmware", cat="config"),
    e("binary_sensor.kotlownia_input", "o0", "8C-relay_0-input", "Input", "power", tk="input"),
    e("binary_sensor.kotlownia_overpowering", "o0", "8C-relay_0-overpower", "Overpowering", "problem", tk="overpowering", cat="diagnostic"),
    e("sensor.kotlownia_power", "o0", "8C-relay_0-power", "Power", "power", "W", M),
    e("sensor.kotlownia_energy", "o0", "8C-relay_0-energy", "Energy", "energy", "kWh", P),
    e("light.kotlownia", "o0", "8C-relay_0", None),
    e("binary_sensor.schowek_overpowering", "o1", "8C-relay_1-overpower", "Overpowering", "problem", tk="overpowering", cat="diagnostic"),
    e("sensor.schowek_power", "o1", "8C-relay_1-power", "Power", "power", "W", M),
    e("sensor.schowek_energy", "o1", "8C-relay_1-energy", "Energy", "energy", "kWh", P),
    e("light.schowek", "o1", "8C-relay_1", None),
    e("event.schowek", "o1", "8C-relay_1-2", "Input ", "button", tk="input"),
]  # fmt: skip

NEW = [
    e("binary_sensor.shellyplus2pm_input_0", "n", "FC-input:0-input", "kotlownia", "power", tk="input"),
    e("binary_sensor.shellyplus2pm_cloud", "n", "FC-cloud-cloud", "Cloud", "connectivity", tk="cloud", cat="diagnostic"),
    e("binary_sensor.shellyplus2pm_restart_required", "n", "FC-sys-restart", "Restart required", "problem", tk="restart_required", cat="diagnostic"),
    e("button.shellyplus2pm_restart", "n", "FC-reboot", "Restart", "restart", cat="config"),
    e("event.kotlownia_schowek", "n", "FC-input:1", "schowek", "button", tk="input"),
    e("sensor.shellyplus2pm_signal_strength", "n", "FC-wifi-rssi", "Signal strength", "signal_strength", "dBm", M, cat="diagnostic"),
    e("sensor.shellyplus2pm_uptime", "n", "FC-sys-uptime", "Uptime", "uptime", cat="diagnostic"),
    e("update.shellyplus2pm_firmware", "n", "FC-sys-fwupdate", "Firmware", "firmware", cat="config"),
    e("update.shellyplus2pm_beta_firmware", "n", "FC-sys-fwupdate_beta", "Beta firmware", "firmware", tk="beta_firmware", cat="config"),
]  # fmt: skip
for ch, device in ((0, "n0"), (1, "n1")):
    k = f"FC-switch:{ch}"
    NEW += [
        e(f"binary_sensor.out{ch}_overheating", device, f"{k}-overtemp", "Overheating", "problem", tk="overheating", cat="diagnostic"),
        e(f"binary_sensor.out{ch}_overpowering", device, f"{k}-overpower", "Overpowering", "problem", tk="overpowering", cat="diagnostic"),
        e(f"binary_sensor.out{ch}_overvoltage", device, f"{k}-overvoltage", "Overvoltage", "problem", tk="overvoltage", cat="diagnostic"),
        e(f"binary_sensor.out{ch}_overcurrent", device, f"{k}-overcurrent", "Overcurrent", "problem", tk="overcurrent", cat="diagnostic"),
        e(f"light.{['kotlownia', 'schowek'][ch]}_2", device, k, None),
        e(f"sensor.out{ch}_power", device, f"{k}-power", "Power", "power", "W", M),
        e(f"sensor.out{ch}_power_factor", device, f"{k}-switch_pf", "Power factor", "power_factor", None, M),
        e(f"sensor.out{ch}_voltage", device, f"{k}-voltage", "Voltage", "voltage", "V", M),
        e(f"sensor.out{ch}_current", device, f"{k}-current", "Current", "current", "A", M),
        e(f"sensor.out{ch}_energy", device, f"{k}-energy", "Energy", "energy", "kWh", P),
        e(f"sensor.out{ch}_energy_returned", device, f"{k}-ret_energy", "Energy returned", "energy", "kWh", P, "energy_returned"),
        e(f"sensor.out{ch}_energy_consumed", device, f"{k}-consumed_energy_switch", "Energy consumed", "energy", "kWh", P, "energy_consumed"),
        e(f"sensor.out{ch}_frequency", device, f"{k}-freq", "Frequency", "frequency", "Hz", M),
        e(f"sensor.out{ch}_temperature", device, f"{k}-temperature", "Temperature", "temperature", "°C", M, cat="diagnostic"),
    ]  # fmt: skip


def test_real_shelly_25_to_plus_2pm() -> None:
    """Channels pair by name; lights, meters and the button land where they belong."""
    pairs = pair_devices(OLD_DEVICES, NEW_DEVICES)
    assert pairs == {"o": "n", "o0": "n0", "o1": "n1"}
    words = name_tokens_by_device(OLD_DEVICES + NEW_DEVICES)
    result = {
        o.entity_id: n.entity_id
        for o, n, _ in match(OLD, NEW, 40, device_tokens=words, device_pairs=pairs)
    }
    # What a user cares about in this swap.
    assert result["light.kotlownia"] == "light.kotlownia_2"
    assert result["light.schowek"] == "light.schowek_2"
    assert result["sensor.kotlownia_power"] == "sensor.out0_power"
    assert result["sensor.kotlownia_energy"] == "sensor.out0_energy"
    assert result["sensor.schowek_power"] == "sensor.out1_power"
    assert result["sensor.schowek_energy"] == "sensor.out1_energy"
    assert result["event.schowek"] == "event.kotlownia_schowek"
    assert result["binary_sensor.kotlownia_input"] == "binary_sensor.shellyplus2pm_input_0"
    assert result["binary_sensor.kotlownia_overpowering"] == "binary_sensor.out0_overpowering"
    assert result["binary_sensor.schowek_overpowering"] == "binary_sensor.out1_overpowering"
    assert result["sensor.kotlownia_rssi"] == "sensor.shellyplus2pm_signal_strength"
    assert result["binary_sensor.kotlownia_cloud"] == "binary_sensor.shellyplus2pm_cloud"
    assert result["button.kotlownia_reboot"] == "button.shellyplus2pm_restart"
    assert result["update.kotlownia_firmware"] == "update.shellyplus2pm_firmware"
    assert result["update.kotlownia_beta_firmware"] == "update.shellyplus2pm_beta_firmware"
    assert result["sensor.kotlownia_uptime"] == "sensor.shellyplus2pm_uptime"
