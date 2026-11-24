"""The meter sum stays continuous across the swap (recorder + statistics)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import datetime, timedelta
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder import Recorder
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant.common import async_fire_time_changed
from pytest_homeassistant.components.recorder.common import (
    async_wait_recording_done,
    do_adhoc_statistics,
    statistics_during_period,
)

from custom_components.device_replacement.const import DOMAIN

from .conftest import DEVICES
from .test_execute import _device_id

pytestmark = pytest.mark.parametrize("expected_lingering_timers", [True])

ENERGY = "sensor.old_plug_energy"


@pytest.fixture
def mock_recorder_before_hass(async_setup_recorder_instance) -> None:
    """Set up the recorder before hass, as the recorder fixtures require."""


@pytest.fixture(autouse=True)
def no_real_sleep() -> Iterator[None]:
    """The clock is frozen: let the executor's waits only yield to the loop."""

    async def _yield(_delay: float) -> None:
        await asyncio.sleep(0)

    with patch("custom_components.device_replacement.executor._sleep", _yield):
        yield


START = datetime(2026, 11, 24, 10, 0, 0, tzinfo=dt_util.UTC)


async def _set(hass: HomeAssistant, serial: str, key: str, value: float) -> None:
    DEVICES[serial].values[key] = value
    entity_ids = [
        s.entity_id for s in hass.states.async_all() if s.entity_id.endswith(("_energy",))
    ]
    await hass.services.async_call(
        "homeassistant", "update_entity", {"entity_id": entity_ids}, blocking=True
    )
    await async_wait_recording_done(hass)


async def _compile(hass: HomeAssistant, start: datetime) -> None:
    do_adhoc_statistics(hass, start=start)
    await async_wait_recording_done(hass)


def _sums(hass: HomeAssistant) -> list[tuple[str, float]]:
    stats = statistics_during_period(
        hass, START, None, {ENERGY}, "5minute", None, {"sum", "state"}
    )[ENERGY]
    return [
        (dt_util.utc_from_timestamp(r["start"]).strftime("%H:%M"), round(r["sum"], 6))
        for r in stats
    ]


@pytest.mark.freeze_time("2026-11-24 09:58:00+00:00")
async def test_meter_sum_is_corrected(
    recorder_mock: Recorder, hass: HomeAssistant, freezer: FrozenDateTimeFactory, devices
) -> None:
    """Old meter 1234.5 → 1234.6 kWh, new meter 12 → 112 Wh: 0.2 kWh consumed."""
    # The devices were set up at 09:58 with the old meter at 1234.5 kWh.
    freezer.move_to(START + timedelta(seconds=10))
    await _compile(hass, START - timedelta(minutes=5))
    freezer.move_to(START + timedelta(minutes=2))
    await _set(hass, "old1", "energy", 1234.6)
    freezer.move_to(START + timedelta(minutes=5, seconds=10))
    await _compile(hass, START)
    assert _sums(hass) == [("10:00", 0.1)]

    # The old plug dies, the new one (12 Wh on its counter) takes over.
    freezer.move_to(START + timedelta(minutes=6))
    DEVICES["old1"].online = False
    await _set(hass, "old1", "energy", 1234.6)
    freezer.move_to(START + timedelta(minutes=6, seconds=30))
    record = await hass.services.async_call(
        DOMAIN,
        "execute",
        {"old_device_id": _device_id(hass, "old1"), "new_device_id": _device_id(hass, "new1")},
        blocking=True,
        return_response=True,
    )
    await async_wait_recording_done(hass)
    assert record["status"] == "done", record
    [item] = record["sum_adjustments"]
    assert item["state"] == "scheduled"
    assert item["previous"] == 1234.6
    assert item["adjustment"] == pytest.approx(-0.012)
    assert hass.states.get(ENERGY).attributes["unit_of_measurement"] == "kWh"

    freezer.move_to(START + timedelta(minutes=8))
    await _set(hass, "new1", "aenergy", 112.0)
    freezer.move_to(START + timedelta(minutes=10, seconds=10))
    await _compile(hass, START + timedelta(minutes=5))
    # The recorder counts the new meter's 12 Wh as consumption.
    assert _sums(hass) == [("10:00", 0.1), ("10:05", 0.212)]

    freezer.move_to(START + timedelta(minutes=12))
    async_fire_time_changed(hass)
    await async_wait_recording_done(hass)
    await async_wait_recording_done(hass)
    assert _sums(hass) == [("10:00", 0.1), ("10:05", 0.2)]
    assert item["state"] == "applied"
