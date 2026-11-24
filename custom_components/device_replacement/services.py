"""Actions of Device Replacement."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
import voluptuous as vol

from .const import (
    ATTR_ADJUST_ENERGY,
    ATTR_AUTO_MATCH,
    ATTR_DRY_RUN,
    ATTR_FORCE,
    ATTR_KEEP_UNITS,
    ATTR_MAPPING,
    ATTR_NEW_DEVICE_ID,
    ATTR_OLD_DEVICE_ID,
    ATTR_REMOVE_OLD_DEVICE,
    DOMAIN,
    SERVICE_EXECUTE,
    SERVICE_JOURNAL,
    SERVICE_PLAN,
)
from .executor import Executor
from .planner import async_build_plan

if TYPE_CHECKING:
    from .journal import Journal

_MAPPING = vol.Schema({cv.entity_id: vol.Any(None, cv.entity_id)})

PLAN_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_OLD_DEVICE_ID): cv.string,
        vol.Required(ATTR_NEW_DEVICE_ID): cv.string,
        vol.Optional(ATTR_MAPPING, default={}): _MAPPING,
        vol.Optional(ATTR_AUTO_MATCH, default=True): cv.boolean,
    }
)

EXECUTE_SCHEMA = PLAN_SCHEMA.extend(
    {
        vol.Optional(ATTR_KEEP_UNITS, default=True): cv.boolean,
        vol.Optional(ATTR_ADJUST_ENERGY, default=True): cv.boolean,
        vol.Optional(ATTR_REMOVE_OLD_DEVICE, default=False): cv.boolean,
        vol.Optional(ATTR_FORCE, default=False): cv.boolean,
        vol.Optional(ATTR_DRY_RUN, default=False): cv.boolean,
    }
)


def _journal(hass: HomeAssistant) -> Journal:
    entries = hass.config_entries.async_entries(DOMAIN)
    if not entries or entries[0].state is not ConfigEntryState.LOADED:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="not_loaded")
    return entries[0].runtime_data


async def _plan(hass: HomeAssistant, data: dict[str, Any]):
    return await async_build_plan(
        hass,
        data[ATTR_OLD_DEVICE_ID],
        data[ATTR_NEW_DEVICE_ID],
        data[ATTR_MAPPING],
        data[ATTR_AUTO_MATCH],
    )


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the actions."""

    async def plan(call: ServiceCall) -> ServiceResponse:
        _journal(hass)
        return (await _plan(hass, call.data)).as_dict()

    async def execute(call: ServiceCall) -> ServiceResponse:
        journal = _journal(hass)
        executor = Executor(
            hass,
            journal,
            await _plan(hass, call.data),
            keep_units=call.data[ATTR_KEEP_UNITS],
            adjust_energy=call.data[ATTR_ADJUST_ENERGY],
            remove_old_device=call.data[ATTR_REMOVE_OLD_DEVICE],
            force=call.data[ATTR_FORCE],
        )
        record = await executor.async_run(dry_run=call.data[ATTR_DRY_RUN])
        return record if call.return_response else None

    async def journal(call: ServiceCall) -> ServiceResponse:
        return {"records": _journal(hass).records}

    hass.services.async_register(
        DOMAIN, SERVICE_PLAN, plan, schema=PLAN_SCHEMA, supports_response=SupportsResponse.ONLY
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_EXECUTE,
        execute,
        schema=EXECUTE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_JOURNAL, journal, supports_response=SupportsResponse.ONLY
    )
