# Device Replacement for Home Assistant

Replace a broken device with a new one **in place**: the old device's entities keep
their entity IDs, history, long-term statistics, names, icons, areas and display units,
and move to the new device. Automations, scripts, dashboards and the Energy dashboard
keep working without edits.

> Status: **0.1.0, Phases 1 and 3 (engine, actions, references, Repairs)**. No UI
> wizard or undo yet. Tested on
> Home Assistant 2026.11 with MQTT devices and in tests with one config entry per
> device (Shelly-like).

## How it works

Home Assistant keys history, statistics and references by `entity_id`. Instead of
renaming the new entities (the recorder refuses to merge history into an `entity_id`
that already has some), the old entity's registry entry is kept and re-pointed at the
new entity's integration, unique ID, config entry and device with
`entity_registry.async_update_entity_platform`. When the new device's integration
reloads, it finds that entry and the live entity appears under the old `entity_id`.

Order of operations:

1. **Disable the old device.** Its entities unload. Anything its integration re-creates
   during the swap is created disabled, so no `*_2` duplicates appear.
2. **Disable the mapped new entities** so they unload.
3. **Per pair:** rename the new entry to a *tombstone* unique ID, then move the old entry
   onto the new unique ID, config entry and device. Moving it onto the (enabled) new
   device enables it again, unless the user had disabled it on purpose.
   If the new entity reports a different but convertible unit (kW instead of W), the old
   unit is kept as the display unit, so states and numeric automations don't change.
4. **Copy the device settings** (area, custom name, labels) and wait for the reload.
   HA reloads the config entry 30 s after `disabled_by` changed; if that doesn't
   happen, the entry is reloaded directly.
5. **Clean up:** remove the tombstones and purge their temporary history and
   statistics. Optionally remove the old device. Correct the statistics sum of meters.
6. **References:** point device IDs at the new device and report what is left (below).

### References

Mapped entities keep their `entity_id`, so nothing that refers to them changes. The
old device's **device ID** is replaced with the new one in `automations.yaml`,
`scripts.yaml`, `scenes.yaml` and in dashboards stored in the UI (device triggers,
conditions and actions, `target: device_id`, device cards). YAML you wrote yourself
(packages, YAML dashboards) is not touched.

The plan shows, and after the replacement **Repairs** lists:

| Issue | When |
|---|---|
| Entity not carried over | An unmapped old entity is still used in an automation, script, scene, group or dashboard |
| Device trigger not offered | An automation uses an integration-specific device trigger, condition or action (a button's "short press") that the new device does not offer |
| Still refers to the old device | A device ID could not be rewritten because the config is YAML |

Each issue is fixed by you; confirming it in Repairs removes it. Entity-based device
triggers ("switch turned on") need nothing: they refer to the entity's registry ID,
which is kept.

### Why tombstones

Never remove the new entity's registry entry before the reload. MQTT, for example,
clears the retained discovery topic of a removed entity, and the resulting empty payload
deletes whichever registry entry then owns the unique ID — the migrated one. Renaming the
entry and removing it after the reload avoids that.

### Meter sums

When the new meter takes over, the recorder sees its first reading after the old meter's
last one: a reset (the whole new reading is added) or a jump (the difference is added,
which may be negative). Either would show up as consumption in the Energy dashboard. Once
the 5-minute period holding the first new reading is compiled, the sum is shifted back by
exactly that amount (`recorder.async_adjust_statistics`). The correction is journaled and
resumed after a restart.

## Actions

| Action | What it does |
|---|---|
| `device_replacement.plan` | Returns the proposed mapping and checks; changes nothing |
| `device_replacement.execute` | Runs the replacement and returns its journal record |
| `device_replacement.journal` | Returns the records of past replacements |

```yaml
action: device_replacement.plan
data:
  old_device_id: 0123456789abcdef0123456789abcdef
  new_device_id: fedcba9876543210fedcba9876543210
  mapping:                        # optional; overrides the automatic match
    sensor.old_plug_rssi: null    # leave unmapped
response_variable: plan
```

`execute` takes the same fields plus `keep_units` (default on), `adjust_energy` (on),
`remove_old_device` (off), `force` (off) and `dry_run` (off).

### Checks

| Code | Level |
|---|---|
| `old_device_online` | blocking (overridable with `force`, for integrations that never mark a dead device unavailable) |
| `domain_mismatch`, `mapping_unknown_*`, `mapping_duplicate_new`, `same_device` | blocking, not overridable |
| `unit_incompatible` (e.g. W → A) | blocking (overridable) |
| `state_class_changed` between a measurement and a meter | blocking (overridable) |
| `device_class_changed`, `features_missing`, `options_missing`, `unit_added_or_removed`, `new_device_unavailable`, `restart_required` | warning |
| `unit_converted`, `meter_swap` | info |

## Not done yet

- Undo from the journal.
- UI wizard.
- References in helpers that keep their own entity lists (template, utility meter,
  statistics sensors) are not scanned; mapped entities are unaffected since their
  `entity_id` stays.

## Development

```bash
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -r requirements_test.txt ruff==0.14.4
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```
