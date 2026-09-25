# Developer documentation

This document covers architecture, implementation details, and
non-obvious Home Assistant behavior encountered while building this
integration. For installation and usage as an end user, see
[README.md](README.md).

## Dependencies and architecture notes

> **pymodbus `device_id`:** pymodbus renamed the `slave` parameter to
> `device_id` in version 3.10.0. This integration uses the new
> `device_id` naming throughout (config key, variable names, and the
> `pymodbus` calls in `coordinator.py`). The `manifest.json` requirement
> is pinned to `pymodbus>=3.11.0` rather than `3.10.0`, since `3.10.0`
> had known issues -- `3.11.0` is the first release with the
> `device_id` naming that's actually solid enough to depend on.

> **Modbus architecture:** Home Assistant is working on a new
> shared-connection `modbus_connection` integration for future
> manufacturer-specific integrations (see the [July 2026 developer blog
> post](https://developers.home-assistant.io/blog/2026/07/05/modernizing-modbus)),
> but as of its July 16, 2026 update the approach is explicitly being
> re-evaluated and not yet recommended for new device integrations. This
> integration intentionally keeps using `pymodbus` directly via its own
> `AsyncModbusTcpClient` connection.

## File overview

- `config_flow.py` – UI-based setup (name, IP, port, device ID) + connection test
- `coordinator.py` – central Modbus connection, periodic polling
  (`DataUpdateCoordinator`), read/write logic including encoding/decoding.
  Only polls registers that at least one currently enabled, added-to-hass
  entity actually reads (see "Selective polling" below).
- `const.py` – register map for a real Hoval controller (35 registers total)
- `sensor.py` – creates HA sensors from `SENSOR_REGISTERS`, plus a
  dedicated `HovalStatusSensor` (device_class `enum`) that translates the
  raw numeric status code into a localized status text (English/German)
- `number.py` – creates writable HA entities from `NUMBER_REGISTERS`
- `water_heater.py` – domestic hot water (DHW) control as a `water_heater`
  entity, with custom operation modes (`off` / `constant` / `week 1` /
  `week 2` / `eco`) translated via `strings.json`/`translations/de.json`
- `climate.py` – up to 3 room heating circuits (HC1/HC2/HC3) as `climate`
  entities. HC1 is enabled by default; HC2/HC3 start disabled
  (`enabled_default=False` on `HeatingCircuit`) since not everyone has
  multiple circuits.

## Selective polling (`async_contexts()`)

Every `CoordinatorEntity` subclass passes the register key(s) it reads as
its `context` when subscribing (`super().__init__(coordinator,
context=[...])`). `HovalModbusCoordinator._async_update_data()` then only
polls registers whose key appears in `self.async_contexts()` -- i.e. only
what a currently-enabled, added-to-hass entity actually needs. Disabled
entities (e.g. HC2/HC3 by default) never subscribe, so their registers are
automatically skipped without any manual entity-registry bookkeeping. If a
user later enables a disabled entity, it starts polling on the very next
cycle -- no reload or restart needed. If no context is registered yet
(e.g. the very first refresh in `__init__.py`, which runs before entities
are set up), the coordinator falls back to polling everything, since being
unable to determine what's needed should never mean "fetch nothing".

The registers to poll are grouped into blocks of adjacent or overlapping
addresses (`build_read_blocks()`), and each block is read with a single
request, since the controller's Modbus gateway can drop connections under a
long series of single-register requests. Blocks never span a gap between
defined registers. If a block read returns an error response, its
registers are read one by one so that a single unreadable register doesn't
hide the others.

This also means write-only registers (e.g.
`write_normal_setpoint_reg`/`write_eco_setpoint_reg` per heating circuit,
`DHW_NORMAL_TARGET_TEMP_REGISTER`/`DHW_ECO_TARGET_TEMP_REGISTER`) are
never polled at all -- no entity ever
reads them for display, they're only used inside `async_write_value()`.

## A note on entity naming and `translation_key`

If both `_attr_name` and `_attr_translation_key` are set on an entity, HA
always uses `_attr_name` and never resolves the translation -- checking
`hasattr(self, "_attr_name")` happens *before* the translation lookup in
`Entity._name_internal()`. So don't set `_attr_name` as a "just in case"
fallback alongside `translation_key`; if you want a name to be
translatable, `_attr_translation_key` plus the matching
`entity.<domain>.<key>.name` entry in `strings.json`/`translations/de.json`
must be the *only* source of the name.

## Room temperature source (per heating circuit)

Each heating circuit's `current_temperature` can come from one of three
sources, configurable **individually per circuit** via the integration's
**options** (entry's three-dot menu → "Configure" — not the initial setup
form, so it can be changed anytime without re-adding the integration):

- **None** (default) -- no current temperature is shown.
- **Read from Heat Pump (Modbus)** -- reads `HeatingCircuit.room_actual_temp_reg`
  in `const.py`, a fixed register per circuit (addresses `1510`/`1511`/`1512`
  for HC1/HC2/HC3 on this Hoval controller). If you're adapting this to a
  different installation/model and don't know the address, set it to `None`
  for that circuit -- selecting "Modbus" then simply yields no value.
- **Home Assistant entity** -- mirrors the state of an existing `sensor`
  entity with `device_class: temperature` (e.g. a separate smart
  thermostat's temperature sensor, so you can use a different sensor than
  the heat pump's own one for that room). Updates live via
  `async_track_state_change_event`, independent of the Modbus polling
  interval, so changes show up immediately rather than waiting for the
  next coordinator refresh. Non-numeric or `unavailable`/`unknown` states
  are treated as "no current temperature" rather than raising an error.

Changing any of these options reloads the config entry automatically
(same mechanism as changing `scan_interval`).

## Options flow: sections and the "can't clear an entity" trap

The options form is grouped into four collapsible **sections**
(`homeassistant.data_entry_flow.section`): "General" (polling interval)
and one per heating circuit. Home Assistant nests submitted data by
section (`{"general": {...}, "heating_circuit_1": {...}, ...}`), so
`async_step_init()` flattens it back into the flat key structure the rest
of the integration expects (`const.py`'s `current_temp_source_key()` /
`current_temp_entity_key()`, `__init__.py`, `climate.py`) before saving.

Two related pitfalls worth knowing if you extend this form:

- **Never use `vol.Optional(key, default=...)` for a field the user
  should be able to clear** (e.g. the entity picker's "X" button). With
  `default=`, voluptuous silently re-inserts the default whenever the
  field is missing from the submitted data -- which is exactly what
  happens when a field is cleared, making it impossible to ever actually
  remove a previously selected value. Use
  `description={"suggested_value": ...}` instead, which only pre-fills
  the form without that side effect.
- **`add_suggested_values_to_schema()` does not recurse into
  `section()`-wrapped fields** (see
  [home-assistant/frontend#22419](https://github.com/home-assistant/frontend/issues/22419)).
  For fields inside a section, set
  `description={"suggested_value": ...}` directly on the `vol.Optional(...)`
  marker when building that section's inner schema, rather than relying
  on the helper.

## Room heating circuits: HVAC mode vs. preset mode

The controller has 5 operating modes per circuit: Off / Constant / Week 1 /
Week 2 / Eco. Home Assistant's `HVACMode` is a **closed enum** (unlike
`water_heater`'s free-form `operation_list`), so it can't represent three
distinct "automatic" programs on its own. This integration maps:

- `HVACMode.OFF` / `HVACMode.HEAT` (constant) map 1:1 to the controller's
  Off/Constant modes.
- `HVACMode.AUTO` covers all three schedule-driven modes (Week 1, Week 2,
  Eco). Which one is active is exposed via **`preset_mode`**
  (`"week1"` / `"week2"` / `PRESET_ECO`), the same mechanism a real
  thermostat uses for "Eco"/"Away" presets within its Auto mode.
  Selecting a preset (`climate.set_preset_mode`) writes the corresponding
  raw code directly to the mode register — no need to also call
  `set_hvac_mode` first.

**Preset icons** are defined in `icons.json` under
`entity.climate.<translation_key>.state_attributes.preset_mode` (one
block per circuit, since `translation_key` differs per circuit —
`heating_circuit_1`/`_2`/`_3`). All three preset values (`"week1"` /
`"week2"` / `"eco"`) need an explicit icon here, including `"eco"` even
though `PRESET_ECO` has a built-in icon (`mdi:leaf`) elsewhere in HA
core -- once an entity defines *any* custom `state_attributes.preset_mode`
icon mapping, that mapping takes over entirely for that attribute, with
no automatic fallback to a built-in per-value icon for values you didn't
list; an unlisted value falls through to your own `default` instead
(originally missed for `"eco"` here, which showed the `default`
fallback icon instead of the leaf icon until it was added explicitly).
The `default` entry still covers the case of a genuinely unrecognized
preset value.

**Each heating circuit has two independent target-temperature registers**,
mirroring the DHW setup: `write_normal_setpoint_reg` (used in
`HVACMode.HEAT`, "Constant") and `write_eco_setpoint_reg` (used in
`HVACMode.AUTO` while the `eco` preset is active). Note they typically
have **different allowed ranges** (e.g. `10–30°C` for normal vs.
`5–20°C` for eco on this controller) — `HovalRoomClimate.min_temp`/
`max_temp`/`target_temperature_step` are `@property`s (not static
`_attr_*`), reading from
`_target_setpoint_register_for_current_mode()` so the UI's allowed range
always reflects whichever setpoint is currently active.

**Setting a target temperature only makes sense in `HVACMode.HEAT`
("Constant") or `HVACMode.AUTO` with the `eco` preset active** — Off has
no target to set, and the Week 1/Week 2 schedule presets are
schedule-driven on the controller itself, same as DHW. This is enforced
via a **collapsed `min_temp`/`max_temp` range**, not by hiding the
`TARGET_TEMPERATURE` feature:

- **`min_temp`/`max_temp`/`target_temperature_step` are `@property`s**
  reading from `_target_setpoint_register_for_current_mode()`. While
  that returns a register (Constant/Eco), they reflect its real
  min/max/step. While it returns `None` (Off/Week1/Week2), `min_temp`
  and `max_temp` both collapse to the **current** `target_temperature`
  — i.e. a single-point range — so the frontend's stepper/slider has
  nowhere to actually move to, without hiding the value itself.
- **`async_set_temperature()` still raises `ServiceValidationError`**
  when `_target_setpoint_register_for_current_mode()` returns `None`,
  as a backstop for calls made directly via automations or the API
  (which don't go through the collapsed-range UI control at all).

**A dynamic `supported_features` that drops `TARGET_TEMPERATURE`
entirely was tried first and reverted.** It *did* stop the frontend's
optimistic temperature-drag preview, but `ClimateEntity.state_attributes`
only includes the target temperature when that feature bit is set — so
hiding the feature also hid the controller's actual current setpoint
from being displayed at all in Off/Week1/Week2, which is worse than the
UI quirk it was meant to fix. `target_temperature` itself is unaffected
by any of this and always reflects the controller's last reported
setpoint, regardless of the current mode.

**Presets are only offered while actually in AUTO.** `preset_modes` is a
`@property` (not a static `_attr_preset_modes`), returning
`CLIMATE_PRESET_OPTIONS` (`["week1", "week2", "eco"]`, no "none"
placeholder needed) while `hvac_mode == HVACMode.AUTO`, and `None`
otherwise. `preset_mode` mirrors that: `None` outside of AUTO. This isn't
just cosmetic — Home Assistant's own service-call validation
(`ClimateEntity._valid_mode_or_raise()`) checks the submitted preset
against `self.preset_modes` *before* `async_set_preset_mode()` is even
called, so returning `None` while not in AUTO also makes HA reject preset
changes in that state with a clear `ServiceValidationError`, instead of
`async_set_preset_mode()` silently writing something that wouldn't make
sense to the controller.

**Remembering the last active program (`self._last_preset`):** switching
`HVACMode.OFF -> HVACMode.AUTO` needs to pick *some* program to write to
the mode register, since "Auto" alone doesn't specify week1 vs. week2 vs.
eco. Rather than always defaulting to week1 (which would need a second,
redundant Modbus write if the user actually wants week2/eco), each
`HovalRoomClimate` keeps an in-memory `self._last_preset`, updated in two
places:

- Optimistically in `async_set_preset_mode()`, immediately when the user
  picks a preset (before the write even completes).
- In an overridden `_handle_coordinator_update()`, on every coordinator
  refresh — so it also stays correct if the mode was changed directly at
  the heat pump's own panel, not just through Home Assistant.

`async_set_hvac_mode()` then uses `PRESET_TO_CLIMATE_MODE[self._last_preset]`
when switching to `AUTO`, instead of a hardcoded week1. Two additional
sync points keep it correct across restarts and while the circuit is
currently `Off`:

- **`RestoreEntity`**, restoring from a **custom** `last_auto_program`
  state attribute (`ATTR_LAST_AUTO_PROGRAM`), exposed unconditionally via
  `extra_state_attributes` — **not** from the built-in `preset_mode`
  attribute. This distinction matters: `preset_mode` always reflects the
  *live* value, which is `None` whenever the circuit isn't currently in
  `AUTO` — i.e. almost always, for a restart that happens while the
  circuit is sitting at `Off`/`Constant` (the common case). Restoring
  from `preset_mode` would silently restore nothing in exactly that
  case; the custom attribute is written on every state update regardless
  of the current hvac_mode, so it's still there to restore from.
- **`_handle_coordinator_update()`**: overrides the restored (or default)
  value with the real, current device state as soon as the first Modbus
  poll after startup completes, if the circuit happens to already be in
  `AUTO` -- so the restored value is only ever a bridge for the startup
  gap and the `Off` case, never allowed to go stale once real data is
  available.

## Optimistic mode/preset updates with pending confirmation

Some Hoval controllers take noticeably long (users have observed up to
~30 seconds) to actually apply a mode/preset change internally and
reflect it back in the mode register — independent of the Modbus polling
interval, since it's the *controller* that's slow to react, not how often
Home Assistant asks it. Without any special handling, the climate entity
would appear "stuck" on the old value for that whole time after a change.

`HovalRoomClimate` addresses this with an optimistic-update pattern:

- `async_set_hvac_mode()` / `async_set_preset_mode()` immediately stash
  the raw value they just wrote in `self._pending_mode_value` and call
  `self.async_write_ha_state()` *before* awaiting the actual Modbus
  write — so the entity shows the new mode/preset right away, not just
  after the next successful poll.
- `hvac_mode` / `preset_mode` / `preset_modes` all read through
  `_effective_raw_mode()`, which returns the pending value while one is
  set, falling back to the real polled register value once confirmed (or
  once it times out unconfirmed).
- `_handle_coordinator_update()` clears the pending value as soon as the
  controller's real, polled mode register matches what was written —
  i.e. the change is genuinely confirmed, not just assumed.
- If nothing ever confirms it within `MODE_CHANGE_CONFIRMATION_TIMEOUT`
  (90s by default), the pending value is discarded and a warning is
  logged, so the entity falls back to showing the controller's actual
  reported state rather than an optimistic value that might reflect a
  write that silently failed.
- The `mode_change_pending` extra state attribute (`True`/`False`)
  exposes whether a change is still awaiting confirmation — useful for a
  dashboard card to show a "syncing…" indicator while it's `True`.

`HovalWaterHeater` (`water_heater.py`) uses the exact same pattern for
`current_operation` (`self._pending_raw_mode`, its own
`_handle_coordinator_update()`/`_expire_stale_pending_mode()`/
`_effective_raw_mode()`, and the same `mode_change_pending` attribute) —
duplicated rather than shared between the two files since the underlying
state (an `int` mode register vs. a translated `str` operation) differs
slightly, but the mechanism and timeout are identical.

**`target_temperature` uses the same pattern in both files**, via
`self._pending_temperature` / `_expire_stale_pending_temperature()` /
`_effective_target_temperature()` and the `temperature_change_pending`
extra state attribute — `async_set_temperature()` shows the newly set
temperature immediately, and `_handle_coordinator_update()` confirms it
once the polled register value matches (within
`TEMPERATURE_CONFIRMATION_EPSILON`, `0.05`°C — half the registers' `0.1`
scale, since encode/decode rounding means the confirmed value is rarely
bit-for-bit identical to what was requested).

## DHW: separate setpoints for "constant" and "eco" mode

The controller has **two independent target-temperature registers**:
`DHW_NORMAL_TARGET_TEMP_REGISTER` (used while `current_operation ==
"constant"`) and `DHW_ECO_TARGET_TEMP_REGISTER` (used while
`current_operation == "eco"`). Note they use **different scales**
(`0.1` vs `1.0`) — confirmed against the real controller, not a copy-paste
mistake, so don't "fix" them to match if you're touching this code later.
`HovalWaterHeater._target_temp_register_for_current_mode()`
picks the right one based on the current mode, used both by
`async_set_temperature()` (to know where to write) and by the
`min_temp`/`max_temp`/`target_temperature_step` properties (so the UI's
allowed range reflects whichever setpoint is actually active).

"Off" and the two schedule programs ("week 1"/"week 2") have no directly
settable temperature. Same approach as `climate.py` (see the note
above): `min_temp`/`max_temp` collapse to the current
`target_temperature` in that case, rather than hiding
`WaterHeaterEntityFeature.TARGET_TEMPERATURE` from `supported_features`
(that alternative hides the temperature *display* too, since HA only
includes it in `state_attributes` when the feature bit is set — worse
than the frontend UI quirk it was meant to fix). `async_set_temperature()`
additionally raises `ServiceValidationError` as a backstop for direct
automation/API calls, rather than writing to an undefined register.
`DHW_ACTUAL_TARGET_TEMP_REGISTER` is the single read-back register that
reflects whichever setpoint is currently in effect, regardless of mode,
so `target_temperature`/confirmation logic doesn't need to know which of
the two write registers was last used.

## Adapting the registers to your heat pump

`SENSOR_REGISTERS`, `DHW_*_REGISTER`, `HEAT_PUMP_STATUS_REGISTER`, and
`HEATING_CIRCUIT_1/2/3` in `const.py` contain real addresses for a Hoval
controller. If you're adapting this to a different heat pump, replace the
addresses with your own — see `RegisterDef` in `const.py` for the fields
available on each register:

```python
RegisterDef(
    key="outdoor_temp",              # internal key, must be unique
    name="Outdoor temperature",      # English display name (translation source)
    address=1477,                    # Modbus register address
    register_type="holding",         # "input" or "holding" (defaults to "holding")
    data_type="int16",               # int16 / uint16 / int32 / uint32 / float32
    scale=0.1,                       # raw_value * scale = displayed value
    precision=1,                     # suggested displayed decimals (optional)
    unit="°C",
    state_class="measurement",       # or None if not a continuous measurement
    device_class="temperature",
),
```

Entity names are resolved via `_attr_translation_key` in `sensor.py` /
`water_heater.py` / `climate.py`, using `entity.<domain>.<key>.name` in
`strings.json` (English) and `translations/de.json` (German) — after
adding a register, also add the matching name (and, for `water_heater`/
`climate` state values, `state`/`state_attributes` entries) to both
files and to `translations/en.json` (kept in sync with `strings.json`).

**Custom translation caching note (custom integrations only):** if you
change `strings.json`/`translations/*.json` and the UI still shows the old
(or English) text after a simple integration reload, do a **full Home
Assistant restart** (Settings → System → Restart) and a hard browser
refresh (Ctrl/Cmd+Shift+R). Custom-integration translation catalogs are
cached and often aren't picked up by a reload alone.

Common pitfalls:

- **Input vs. holding registers**: `register_type` defaults to `"holding"`
  in this Hoval map; check the manual if you hit "IllegalAddress" errors.
- **Address offset**: some manuals count from 1, while Modbus itself
  counts from 0 – subtract 1 if needed.
- **Sign/scaling**: temperatures are often `int16 * 0.1`, but sometimes
  unsigned with an offset. Adjust `decode_value()`/`encode_value()` in
  `coordinator.py` if necessary.
- **32-bit values** (e.g. energy counters) occupy two registers and are
  read as `int32`/`uint32`/`float32` – byte order can vary by manufacturer.

Writable values (e.g. a flow temperature setpoint) belong in
`NUMBER_REGISTERS` with `writable=True` and are automatically exposed as
a `number` entity in HA.

