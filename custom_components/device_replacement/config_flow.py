"""Config flow of Device Replacement, and the replacement wizard as its options flow.

Settings → Devices & services → Device Replacement → Configure:
replace a device (pick devices → map entities → review → run) or undo a replacement.
"""

from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.selector import (
    BooleanSelector,
    DeviceSelector,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)
from homeassistant.util import dt as dt_util
import voluptuous as vol

from .const import (
    ATTR_ADJUST_ENERGY,
    ATTR_FORCE,
    ATTR_KEEP_UNITS,
    ATTR_NEW_DEVICE_ID,
    ATTR_OLD_DEVICE_ID,
    ATTR_REMOVE_OLD_DEVICE,
    DOMAIN,
)
from .executor import NOT_FORCEABLE, Executor
from .journal import Journal
from .models import Level, Plan
from .planner import async_build_plan
from .undo import Undoer, undo_blocker

NONE = "__none__"
ATTR_RECORD_ID = "record_id"
_ICON = {Level.INFO: "ℹ️", Level.WARNING: "⚠️", Level.BLOCKING: "⛔"}


class DeviceReplacementConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add Device Replacement."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Confirm adding the integration."""
        if user_input is not None:
            return self.async_create_entry(title="Device Replacement", data={})
        return self.async_show_form(step_id="user")

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the replacement wizard."""
        return ReplacementWizard()


def plan_summary(plan: Plan) -> str:
    """Return the plan as markdown for the confirm step."""
    lines = []
    for pair in plan.pairs:
        lines.append(f"- **{pair.old.entity_id}** ← {pair.new.entity_id}")
        lines.extend(f"  - {_ICON[c.level]} {c.message}" for c in pair.checks)
    if plan.unmatched_old:
        lines.append("")
        lines.append("Not carried over:")
        lines.extend(f"- {d.entity_id}" for d in plan.unmatched_old)
    if plan.device_checks:
        lines.append("")
        lines.extend(f"- {_ICON[c.level]} {c.message}" for c in plan.device_checks)
    return "\n".join(lines)


def record_label(record: dict[str, Any]) -> str:
    """Return a one-line description of a replacement."""
    when = dt_util.parse_datetime(record["created_at"])
    stamp = dt_util.as_local(when).strftime("%Y-%m-%d %H:%M") if when else record["created_at"]
    count = len(record.get("migrated", []))
    return (
        f"{stamp}: {record['plan']['old_device']['name']} → "
        f"{record['plan']['new_device']['name']}, {count} "
        f"{'entity' if count == 1 else 'entities'}"
    )


def record_summary(record: dict[str, Any]) -> str:
    """Return the outcome of a replacement or undo as markdown."""
    if record.get("undone_at"):
        lines = [
            f"- {m['entity_id']} is back on {record['plan']['old_device']['name']}"
            for m in record.get("migrated", [])
        ]
    else:
        lines = [f"- {m['entity_id']} ← {m['replaced_by']}" for m in record.get("migrated", [])]
    warnings = record.get("warnings", []) + record.get("undo", {}).get("warnings", [])
    lines.extend(f"- ⚠️ {w}" for w in warnings)
    if record.get("issues") and not record.get("undone_at"):
        lines.append(f"- {len(record['issues'])} issue(s) were added to Repairs")
    return "\n".join(lines) or "-"


class ReplacementWizard(OptionsFlow):
    """Replace a device, or undo a replacement."""

    def __init__(self) -> None:
        """Initialise the wizard."""
        self._old: str = ""
        self._new: str = ""
        self._plan: Plan | None = None
        self._task: asyncio.Task[dict[str, Any]] | None = None
        self._record_id: str | None = None

    @property
    def _journal(self) -> Journal:
        return self.config_entry.runtime_data

    def _device_name(self, device_id: str) -> str:
        device = dr.async_get(self.hass).async_get(device_id)
        if device is None:
            return device_id
        return device.name_by_user or device.name or device_id

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Choose between replacing and undoing."""
        return self.async_show_menu(step_id="init", menu_options=["replace", "undo"])

    # --- replace -----------------------------------------------------------------

    async def async_step_replace(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the old and the new device."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._old = user_input[ATTR_OLD_DEVICE_ID]
            self._new = user_input[ATTR_NEW_DEVICE_ID]
            if self._old == self._new:
                errors["base"] = "same_device"
            else:
                self._plan = await async_build_plan(self.hass, self._old, self._new)
                return await self.async_step_mapping()
        return self.async_show_form(
            step_id="replace",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(
                    {
                        vol.Required(ATTR_OLD_DEVICE_ID): DeviceSelector(),
                        vol.Required(ATTR_NEW_DEVICE_ID): DeviceSelector(),
                    }
                ),
                user_input or {},
            ),
            errors=errors,
        )

    async def async_step_mapping(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the new entity for every old entity."""
        assert self._plan is not None
        plan = self._plan
        errors: dict[str, str] = {}
        if user_input is not None:
            mapping = {k: None if v == NONE else v for k, v in user_input.items()}
            chosen = [v for v in mapping.values() if v]
            if len(chosen) != len(set(chosen)):
                errors["base"] = "duplicate_new"
            elif not chosen:
                errors["base"] = "nothing_mapped"
            else:
                self._plan = await async_build_plan(
                    self.hass, self._old, self._new, mapping, auto_match=False
                )
                return await self.async_step_confirm()

        suggested = {p.old.entity_id: p.new.entity_id for p in plan.pairs}
        old_entities = sorted(
            [p.old for p in plan.pairs] + plan.unmatched_old, key=lambda d: d.entity_id
        )
        new_entities = sorted(
            [p.new for p in plan.pairs] + plan.unmatched_new, key=lambda d: d.entity_id
        )
        schema: dict[Any, Any] = {}
        for old in old_entities:
            options = [SelectOptionDict(value=NONE, label="— don't carry over —")]
            options.extend(
                SelectOptionDict(
                    value=n.entity_id, label=f"{n.name or n.entity_id} ({n.entity_id})"
                )
                for n in new_entities
                if n.domain == old.domain
            )
            schema[vol.Required(old.entity_id, default=suggested.get(old.entity_id, NONE))] = (
                SelectSelector(
                    SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN)
                )
            )
        return self.async_show_form(
            step_id="mapping",
            data_schema=vol.Schema(schema),
            errors=errors,
            description_placeholders={
                "old_device": plan.old_device_name,
                "new_device": plan.new_device_name,
            },
        )

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the checks and the options, then run."""
        assert self._plan is not None
        plan = self._plan
        errors: dict[str, str] = {}
        placeholders = {
            "old_device": plan.old_device_name,
            "new_device": plan.new_device_name,
            "summary": plan_summary(plan),
            "reasons": "",
        }
        if user_input is not None:
            executor = Executor(
                self.hass,
                self._journal,
                plan,
                keep_units=user_input[ATTR_KEEP_UNITS],
                adjust_energy=user_input[ATTR_ADJUST_ENERGY],
                remove_old_device=user_input[ATTR_REMOVE_OLD_DEVICE],
                force=user_input.get(ATTR_FORCE, False),
            )
            try:
                executor.validate()
            except HomeAssistantError as err:
                errors["base"] = "blocked"
                placeholders["reasons"] = str(err)
            else:
                self._task = self.hass.async_create_task(executor.async_run())
                return await self.async_step_execute()

        schema: dict[Any, Any] = {
            vol.Required(ATTR_KEEP_UNITS, default=True): BooleanSelector(),
            vol.Required(ATTR_ADJUST_ENERGY, default=True): BooleanSelector(),
            vol.Required(ATTR_REMOVE_OLD_DEVICE, default=False): BooleanSelector(),
        }
        if any(c.code not in NOT_FORCEABLE for c in plan.blocking):
            schema[vol.Required(ATTR_FORCE, default=False)] = BooleanSelector()
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema(schema),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_execute(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show progress while the replacement runs."""
        assert self._task is not None
        if not self._task.done():
            return self.async_show_progress(
                step_id="execute", progress_action="replacing", progress_task=self._task
            )
        return self.async_show_progress_done(next_step_id="replaced")

    async def async_step_replaced(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the outcome."""
        assert self._task is not None
        try:
            record = self._task.result()
        except Exception as err:
            return self.async_abort(reason="failed", description_placeholders={"error": str(err)})
        return self.async_abort(
            reason="replaced", description_placeholders={"summary": record_summary(record)}
        )

    # --- undo --------------------------------------------------------------------

    async def async_step_undo(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Pick the replacement to undo."""
        done = [r for r in reversed(self._journal.records) if not r.get("dry_run")]
        records = [r for r in done if undo_blocker(self.hass, r) is None]
        if not records:
            recent = "\n".join(
                f"- {record_label(r)}: {undo_blocker(self.hass, r)}" for r in done[:5]
            )
            return self.async_abort(
                reason="nothing_to_undo",
                description_placeholders={"recent": recent or "- none yet"},
            )
        if user_input is not None:
            record = self._journal.get(user_input[ATTR_RECORD_ID])
            assert record is not None
            self._task = self.hass.async_create_task(
                Undoer(self.hass, self._journal, record).async_run()
            )
            return await self.async_step_undo_execute()
        latest = done[0]["id"]
        options = [
            SelectOptionDict(
                value=r["id"],
                label=record_label(r) + (" (latest)" if r["id"] == latest else ""),
            )
            for r in records
        ]
        return self.async_show_form(
            step_id="undo",
            data_schema=vol.Schema(
                {
                    vol.Required(ATTR_RECORD_ID, default=options[0]["value"]): SelectSelector(
                        SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST)
                    )
                }
            ),
        )

    async def async_step_undo_execute(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show progress while the undo runs."""
        assert self._task is not None
        if not self._task.done():
            return self.async_show_progress(
                step_id="undo_execute", progress_action="undoing", progress_task=self._task
            )
        return self.async_show_progress_done(next_step_id="undone")

    async def async_step_undone(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show the outcome of the undo."""
        assert self._task is not None
        try:
            record = self._task.result()
        except Exception as err:
            return self.async_abort(reason="failed", description_placeholders={"error": str(err)})
        return self.async_abort(
            reason="undone", description_placeholders={"summary": record_summary(record)}
        )
