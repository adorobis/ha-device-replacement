"""Device Replacement: replace a broken device in place, keeping its entities."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .executor import resume_adjustments
from .journal import Journal
from .services import async_setup_services

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type DeviceReplacementConfigEntry = ConfigEntry[Journal]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the actions."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: DeviceReplacementConfigEntry) -> None:
    """Load the journal."""
    journal = Journal(hass)
    await journal.async_load()
    entry.runtime_data = journal
    resume_adjustments(hass, journal)


async def async_unload_entry(hass: HomeAssistant, entry: DeviceReplacementConfigEntry) -> None:
    """Unload the entry; the actions stay registered and report it is not loaded."""
