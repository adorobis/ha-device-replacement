"""Devices with sub-devices.

Integrations split one physical device into a main device and sub-devices linked
with via_device (a Shelly 2.5 is a main device plus one device per channel). The
replacement treats such a family as one: the entities of all its devices are mapped
together, because the new device may put an entity on another sub-device than the
old one did (the Shelly Plus 2PM has its button event on the main device, the 2.5 on
the channel). The sub-devices are paired so that device settings and device
references go to the matching new sub-device.
"""

from __future__ import annotations

from collections import Counter

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.util import slugify

# More sub-devices than this looks like a hub or a bridge, not a device.
MAX_FAMILY_SIZE = 12


def device_name(device: dr.DeviceEntry) -> str:
    """Return the name a user sees for a device."""
    return device.name_by_user or device.name or device.id


def family(
    hass: HomeAssistant, device_id: str, include_sub_devices: bool = True
) -> list[dr.DeviceEntry]:
    """Return the device and, unless excluded, its sub-devices; the device first."""
    dreg = dr.async_get(hass)
    root = dreg.async_get(device_id)
    if root is None:
        return []
    result = [root]
    if not include_sub_devices:
        return result
    children: dict[str, list[dr.DeviceEntry]] = {}
    for device in dreg.devices.values():
        if device.via_device_id:
            children.setdefault(device.via_device_id, []).append(device)
    queue = [root.id]
    seen = {root.id}
    while queue:
        parent = queue.pop(0)
        for child in sorted(children.get(parent, []), key=device_name):
            if child.id not in seen:
                seen.add(child.id)
                result.append(child)
                queue.append(child.id)
    return result


def tokens(text: str | None) -> set[str]:
    """Return the words of a name, without accents (Kotłownia → kotlownia)."""
    if not text:
        return set()
    return {t for t in slugify(text).split("_") if t}


def _distinctive(devices: list[dr.DeviceEntry]) -> dict[str, set[str]]:
    """Return each device's name words without the words all of them share."""
    words = {d.id: tokens(device_name(d)) for d in devices}
    if len(devices) > 1:
        common = set.intersection(*words.values())
        words = {k: (v - common) or v for k, v in words.items()}
    return words


def name_tokens_by_device(devices: list[dr.DeviceEntry]) -> dict[str, set[str]]:
    """Return the full name words of every device, to strip them from entity names."""
    return {d.id: tokens(device_name(d)) for d in devices}


def pair_devices(old: list[dr.DeviceEntry], new: list[dr.DeviceEntry]) -> dict[str, str | None]:
    """Pair the old family's devices with the new family's.

    The main devices pair with each other. Sub-devices pair by the words of their
    names that set them apart within their family ("schowek" in "schowek old"), then
    the rest in order of name.
    """
    if not old or not new:
        return {}
    pairs: dict[str, str | None] = {old[0].id: new[0].id}
    old_rest, new_rest = old[1:], new[1:]
    old_words, new_words = _distinctive(old), _distinctive(new)
    candidates = sorted(
        (
            (len(old_words[o.id] & new_words[n.id]) / len(old_words[o.id] | new_words[n.id]),
             device_name(o), device_name(n), o.id, n.id)
            for o in old_rest
            for n in new_rest
            if old_words[o.id] | new_words[n.id]
        ),
        key=lambda c: (-c[0], c[1], c[2]),
    )  # fmt: skip
    used_new: set[str] = set()
    for similarity, _, _, old_id, new_id in candidates:
        if similarity == 0:
            break
        if old_id in pairs or new_id in used_new:
            continue
        pairs[old_id] = new_id
        used_new.add(new_id)
    left_old = [o for o in old_rest if o.id not in pairs]
    left_new = [n for n in new_rest if n.id not in used_new]
    for o, n in zip(left_old, left_new, strict=False):
        pairs[o.id] = n.id
    for o in left_old[len(left_new) :]:
        pairs[o.id] = None
    return pairs


def family_counts(devices: list[dr.DeviceEntry]) -> Counter[str]:
    """Return how many sub-devices each device in the list has (for labels)."""
    counts: Counter[str] = Counter()
    for device in devices:
        if device.via_device_id:
            counts[device.via_device_id] += 1
    return counts
