"""Suggest which entity of the new device replaces which entity of the old one."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
import re

from .compat import units_compatible
from .family import tokens as _words
from .models import EntityDescriptor

SOLE_OF_DOMAIN_BONUS = 20
# The old entity's device is paired with the new entity's device.
SAME_DEVICE_PAIR_BONUS = 15
# Neither entity has a name of its own: each is its device's main entity.
MAIN_ENTITY_BONUS = 20


def _tokens(text: str | None, drop: set[str]) -> set[str]:
    return _words(text) - drop


def name_tokens(desc: EntityDescriptor, device_tokens: set[str]) -> set[str]:
    """Return the words describing what an entity is, without the device name."""
    tokens = _tokens(desc.name, device_tokens)
    if not tokens:
        tokens = _tokens(desc.entity_id.split(".", 1)[1].replace("_", " "), device_tokens)
    return tokens


def _uid_suffix(unique_id: str) -> str:
    parts = re.split(r"[-_:./ ]+", unique_id.lower())
    return parts[-1] if parts else ""


def score(
    old: EntityDescriptor,
    new: EntityDescriptor,
    old_device_tokens: set[str] | None = None,
    new_device_tokens: set[str] | None = None,
) -> int:
    """Return how likely new replaces old, 0..100. Different domains score 0."""
    if old.domain != new.domain:
        return 0
    points = 0
    if old.translation_key and old.translation_key == new.translation_key:
        points += 40
    if old.device_class or new.device_class:
        points += 25 if old.device_class == new.device_class else -20
    else:
        points += 5
    if old.unit or new.unit:
        if old.unit == new.unit:
            points += 10
        elif units_compatible(new.device_class, new.unit, old.unit):
            points += 8
        else:
            points -= 30
    if old.state_class == new.state_class:
        points += 5
    if old.entity_category == new.entity_category:
        points += 5
    if old.name is None and new.name is None:
        # Both are their device's main entity, shown under the device's own name
        # (the relay of a Shelly channel): strong evidence they correspond.
        points += MAIN_ENTITY_BONUS
    a = name_tokens(old, old_device_tokens or set())
    b = name_tokens(new, new_device_tokens or set())
    if a and b:
        points += round(20 * len(a & b) / len(a | b))
    if _uid_suffix(old.unique_id) == _uid_suffix(new.unique_id):
        points += 10
    return max(0, min(100, points))


def match(
    old_entities: Iterable[EntityDescriptor],
    new_entities: Iterable[EntityDescriptor],
    threshold: int,
    old_device_name: str = "",
    new_device_name: str = "",
    *,
    device_tokens: Mapping[str, set[str]] | None = None,
    device_pairs: Mapping[str, str | None] | None = None,
) -> list[tuple[EntityDescriptor, EntityDescriptor, int]]:
    """Return a one-to-one assignment of old to new entities.

    Greedy on the score: the best pair first, then the best of what is left. Pairs
    below the threshold are not suggested. When both devices have exactly one entity
    of a domain (one switch each), that pair gets a bonus.

    With sub-devices, device_tokens gives the name words of each entity's own device
    (stripped from entity names) and device_pairs the paired devices: entities on
    paired devices get a bonus, so channel 1 maps to channel 1.
    """
    old_entities, new_entities = list(old_entities), list(new_entities)
    old_tokens = _tokens(old_device_name, set())
    new_tokens = _tokens(new_device_name, set())
    old_count = Counter(o.domain for o in old_entities)
    new_count = Counter(n.domain for n in new_entities)

    def points(o: EntityDescriptor, n: EntityDescriptor) -> int:
        if device_tokens is not None:
            base = score(
                o,
                n,
                device_tokens.get(o.device_id or "", set()),
                device_tokens.get(n.device_id or "", set()),
            )
        else:
            base = score(o, n, old_tokens, new_tokens)
        if not base:
            return 0
        if old_count[o.domain] == 1 and new_count[n.domain] == 1:
            base += SOLE_OF_DOMAIN_BONUS
        if device_pairs and o.device_id and device_pairs.get(o.device_id) == n.device_id:
            base += SAME_DEVICE_PAIR_BONUS
        return min(100, base)

    candidates = sorted(
        (
            (points(o, n), o.entity_id, n.entity_id, o, n)
            for o in old_entities
            for n in new_entities
        ),
        key=lambda c: (-c[0], c[1], c[2]),
    )
    used_old: set[str] = set()
    used_new: set[str] = set()
    result = []
    for pair_points, old_id, new_id, o, n in candidates:
        if pair_points < threshold:
            break
        if old_id in used_old or new_id in used_new:
            continue
        used_old.add(old_id)
        used_new.add(new_id)
        result.append((o, n, pair_points))
    return result
