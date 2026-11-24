"""Keep the statistics sum of a meter continuous across the device swap.

When the new meter takes over the entity, the recorder sees its first reading after
the old meter's last one. For total_increasing a drop of more than 10 % is a reset,
so the whole new reading is added to the sum; otherwise the difference is added,
which can be negative. Either way the swap shows up as consumption. Once the
5-minute period holding the first new reading is compiled, the sum is shifted back
by exactly that amount from that period on.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
import math
from typing import Any

from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util

from .compat import unit_converter
from .const import STATISTICS_PERIOD_MINUTES, SUM_ADJUST_DELAY_AFTER_PERIOD

_LOGGER = logging.getLogger(__name__)


def to_float(value: Any) -> float | None:
    """Return value as float, or None when it is not a number."""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(result) else result


async def async_last_numeric_state(
    hass: HomeAssistant, entity_id: str
) -> tuple[float, str | None] | None:
    """Return the last numeric state of entity_id from the recorder, with its unit."""
    from homeassistant.components.recorder import get_instance, history  # noqa: PLC0415

    states = await get_instance(hass).async_add_executor_job(
        history.get_last_state_changes, hass, 20, entity_id
    )
    for state in reversed(states.get(entity_id, [])):
        if (value := to_float(state.state)) is not None:
            return value, state.attributes.get("unit_of_measurement")
    return None


async def async_statistics_unit(hass: HomeAssistant, statistic_id: str) -> str | None:
    """Return the unit the statistics of statistic_id are stored in."""
    from homeassistant.components.recorder import get_instance, statistics  # noqa: PLC0415

    metadata = await get_instance(hass).async_add_executor_job(
        lambda: statistics.get_metadata(hass, statistic_ids={statistic_id})
    )
    if statistic_id not in metadata:
        return None
    return metadata[statistic_id][1]["unit_of_measurement"]


def sum_jump(
    *,
    state_class: str | None,
    device_class: str | None,
    previous: float,
    first: State,
    stat_unit: str | None,
) -> float | None:
    """Return what the recorder adds to the sum for the first reading of the new meter.

    previous is the old meter's last reading in the statistics unit.
    """
    value = to_float(first.state)
    if value is None:
        return None
    convert = unit_converter(device_class, first.attributes.get("unit_of_measurement"), stat_unit)
    if convert is None:
        return None
    value = convert(value)
    if state_class == "total_increasing":
        reset = value < 0.9 * previous
    else:
        # A total sensor resets when its last_reset changes; the new device's
        # last_reset differs from the old device's whenever it reports one.
        reset = first.attributes.get("last_reset") is not None
    return value if reset else value - previous


def period_start(when: datetime) -> datetime:
    """Return the start of the 5-minute statistics period holding when."""
    when = dt_util.as_utc(when)
    return when.replace(
        minute=when.minute - when.minute % STATISTICS_PERIOD_MINUTES, second=0, microsecond=0
    )


def apply_after(start: datetime) -> datetime:
    """Return when the period starting at start has surely been compiled."""
    return start + timedelta(
        minutes=STATISTICS_PERIOD_MINUTES, seconds=SUM_ADJUST_DELAY_AFTER_PERIOD
    )


def adjust_sum(hass: HomeAssistant, item: dict[str, Any]) -> None:
    """Queue the sum adjustment described by a journal item."""
    from homeassistant.components.recorder import get_instance  # noqa: PLC0415

    _LOGGER.info(
        "Adjusting the sum of %s by %s %s from %s",
        item["statistic_id"],
        item["adjustment"],
        item["unit"],
        item["start"],
    )
    get_instance(hass).async_adjust_statistics(
        item["statistic_id"],
        dt_util.parse_datetime(item["start"]),
        item["adjustment"],
        item["unit"],
    )
