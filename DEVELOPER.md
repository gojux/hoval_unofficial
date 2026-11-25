# Developer documentation

This document covers architecture, implementation details, and
non-obvious Home Assistant behavior encountered while building this
integration. For installation and usage as an end user, see
[README.md](README.md).

## Dependencies and architecture notes

> **Modbus connection:** The integration uses Home Assistant's shared
> Modbus connection (`homeassistant.components.modbus.async_get_unit` /
> `async_get_temporary_unit`, library `modbus-connection` with the
> `tmodbus` backend), hence `dependencies: ["modbus"]` in `manifest.json`
> and a minimum of Home Assistant 2026.9. All integrations talking to the
> same gateway share one connection and their requests are serialized.
> Errors are exceptions: `ModbusExceptionError` means the controller
> answered with an error code (e.g. a register it doesn't have), every
> other `ModbusError` means the request didn't get through. The library
> doesn't repeat requests, so the coordinator does (`_with_retries`), 1 s
> apart: a read or write gets up to three tries that end without an answer
> (timeout or late answer). After each of these it drops the connection
> (`ModbusUnit.disconnect()`), so the next try opens a new one; this also
> reconnects other integrations sharing the gateway. A connection dropped
> by the gateway doesn't count as a try, so the request is repeated on the
> new connection (at most twice). The gateway stops answering on a
> connection that has been idle for about 20-30 seconds and only drops it
> a few requests later; just a new connection gets answers again. The
> default polling interval of 20 seconds (`DEFAULT_SCAN_INTERVAL`) keeps
> the connection busy enough that this doesn't happen; the retries cover
> longer intervals and other interruptions.

<!-- HA-COMPAT(2026.10): describe the timeout as always available. -->
> **Request timeout:** From Home Assistant 2026.10 a unit can ask for a
> request timeout (`ModbusUnit.require_timeout`). The integration detects
> this at runtime (`coordinator.timeout_supported()`) and only then
> offers the timeout in the options, asking for `DEFAULT_TIMEOUT` (3 s)
> unless the user enters another value; on 2026.9 the library default of
> 10 s applies. The connection uses the longest timeout any consumer of
> the gateway asks for.

## Supported Home Assistant versions

- **Minimum:** the integration supports at least the last three Home
  Assistant releases. The minimum is raised only when that brings
  something, e.g. removing compatibility code or an API Home Assistant
  drops -- not automatically with every release.
- **New Home Assistant features** (especially in the Modbus integration
  and `modbus-connection`) are reviewed with every release and, where
  possible, built in backwards compatible: detected at runtime (e.g.
  `hasattr`), not by comparing version numbers. Older versions keep
  their behavior, newer ones use the feature. Options that depend on a
  feature are only shown where it is available.
- **Review per release:** compare `homeassistant/components/modbus/`
  (`connection.py`, `__init__.py`, `manifest.json`) and the
  `modbus-connection` API (`ModbusUnit`, exceptions) with the previous
  release, and check the Home Assistant log of the E2E run for
  deprecation warnings.
- **Tests:** run the E2E tests on the minimum and on the latest release
  (`HA_VERSION=<version>`, see `tests/e2e/docker-compose.yml`), the
  releases in between when something relevant changed there.
- **Marking compatibility code:** every place that exists only for older
  releases -- code, tests and docs -- gets a marker naming the release
  from which it is no longer needed, plus what to do then:
  `# HA-COMPAT(2026.10): always offer the field.` (in Markdown as
  `<!-- HA-COMPAT(2026.10): ... -->`). `python3 tests/check_compat_markers.py`
  lists all markers and fails when one is at or below the minimum in
  `hacs.json`.
- **Raising the minimum:** update `hacs.json`, the README and this file
  together, run `tests/check_compat_markers.py` and clean up every marker
  it reports as due.

## File overview

- `config_flow.py` – UI-based setup (name, IP, port, device ID) + connection test
- `coordinator.py` – central Modbus connection, periodic polling
  (`DataUpdateCoordinator`), read/write logic including encoding/decoding.
  Only polls registers that at least one currently enabled, added-to-hass
  entity actually reads (see "Selective polling" below).
- `const.py` – register map for a real Hoval controller; addresses and
  their verification status are listed in [REGISTERS.md](REGISTERS.md)
- `sensor.py` – creates HA sensors from `SENSOR_REGISTERS` (the buffer
  sensor `buffer_temp` only with `CONF_BUFFER_SENSOR`) and the sensors of
  each found buffer module (`BUFFER_MODULE_SENSOR_REGISTERS`; a buffer
  sensor without a plausible value, `buffer_sensor_found_key`, disabled;
  named by `buffer_module_translation`: as heating / cooling buffer from
  `BUFFER_MODULE_ROLES` only if both modules are found, as the buffer Smart
  Grid offsets in `number.py`), plus
  dedicated enum sensors (device_class `enum`) that translate raw codes
  into localized texts (English/German): `HovalStatusSensor` (status),
  `HovalControllerErrorSensor` (error code, 255 = no error), the Smart
  Grid status sensor and, per heating circuit, the control strategy and
  status sensors (diagnostic, disabled by default), and the suggested flow
  setpoint raise of heating circuit 1 (diagnostic; only while
  `flow_setpoint_raise_learning`, see `raise_control.py`)
- `flow_setpoint_raise.py` – works out the suggested flow setpoint raise
  from the polled values, without Home Assistant dependencies: surplus /
  balance detection, the raise needed in balance, the table of the raise
  per outdoor temperature range, and `AutoRaise`, which decides what the
  automatic adjustment writes (down right away, up in small steps). It only
  measures while the circuit sets the heat generator setpoint (setpoint =
  circuit flow setpoint + raise, rounded down to 0.5 K), otherwise
  `other_demand`
- `raise_control.py` – `FlowSetpointRaiseControl`, created in
  `async_setup_entry` while heating circuit 1's raise is offered
  (`flow_setpoint_raise_offered`: experimental function on, no buffer module,
  strategy 0/1 stored as `control_strategy_key`) and the heat pump has a
  buffer sensor (`CONF_BUFFER_SENSOR`, 27493 != 0), see
  `flow_setpoint_raise_learning`. It
  listens to the coordinator with the registers it needs as context, works
  out the suggestion on every poll, keeps what it learned in a `Store`
  (removed with the entry) and writes the raise while the switch is on.
  For every circuit, `async_sync_flow_setpoint_raise` remembers the raise on
  the controller once the integration provides that circuit's raise
  (`flow_setpoint_raise_offered`, key `flow_setpoint_raise_previous_key`) and
  writes it back (read back, then dropped) once it no longer does. The
  options flow runs it before saving, pausing heating circuit 1's automatic
  adjustment, and refuses to save if it fails; `async_setup_entry` runs it
  too, e.g. for a strategy read at startup. `async_cleanup_removed_entry`
  writes back all remembered raises on removal (only logging a failure);
  cooling and constant control are left as they are.
  `CoolingRaiseGuard`, created while any circuit's raise is offered
  (`coordinator.raise_guard`), listens to the circuits' status and raise
  registers: once a status shows cooling (`circuit_cooling`), it writes the
  remembered raise (`flow_setpoint_raise_previous_key`, 0 without one) and
  keeps it there, pausing heating circuit 1's automatic adjustment; the
  raise number refuses changes meanwhile. Once the circuit heats again, the
  raise from before cooling is written back and the adjustment resumed
- `number.py` – creates writable HA entities from `NUMBER_REGISTERS`; some
  only under conditions: the flow setpoint raises (`flow_setpoint_raise_offered`,
  range from `flow_setpoint_raise_bounds`), the buffer Smart Grid offsets per
  found buffer module (`buffer_module_key`), the Smart Grid offsets per
  present circuit (heating and cooling, any strategy). A
  written value is shown right away until the next poll confirms it
  (`change_pending` attribute, given up after 90 s)
- `switch.py` – the switch for the automatic flow setpoint raise
  (`RestoreEntity`, off when first created)
- `constant_control.py` – `ConstantFlowControl`, one per heating circuit on
  constant control (strategy 3) while that experimental function is on
  (`constant_control_enabled`, strategy read by `async_read_circuit_options`).
  Without that function, a circuit on strategy 3 (`on_constant_control`) gets
  no climate entity, and its options form has no room temperature source.
  Derives the mode from the two constant flow setpoints, keeps both
  setpoints, and switches the mode in a background task: other setpoint 0,
  read back, `SWITCH_PAUSE_SECONDS`, then the new setpoint. The select
  (`HovalConstantModeSelect`) and the two numbers (`HovalConstantFlowNumber`,
  `RestoreNumber`) show its state; climate.py skips these circuits
- `select.py` – `HovalMappedSelect`, a generic writable `select` entity
  used for the two Smart Grid registers (trigger/mode), mapping the raw
  register value to a stable option key (same pattern as
  `HovalStatusSensor`'s status code translation)
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
(the first refresh in `__init__.py`, which runs before entities are set
up), the coordinator polls the registers with `enabled_default`;
`async_setup_entry` refreshes again right after setting up the platforms,
so entities enabled otherwise get their values right away. Nothing set up
at startup relies on the first refresh reading a particular register:
detection reads its registers directly.

The registers to poll are grouped into blocks of adjacent or overlapping
addresses (`build_read_blocks()`), and each block is read with a single
request, since the controller's Modbus gateway can drop connections under a
long series of single-register requests. Blocks never span a gap between
defined registers. If a block read returns an error response, its
registers are read one by one so that a single unreadable register doesn't
hide the others. The same happens if a block isn't answered, not even after
the retries, while the controller still answers a probe request
(`PROBE_ADDRESSES`: outdoor temperature, then heat generator status with a
single attempt, for a controller that doesn't answer the first one itself;
an error response counts): some controllers don't answer at all for a
register they don't have. Without an answer to either probe the poll fails.
Reading the buffer registers for the buffer detection follows the same
rule (`async_read_value_if_present(..., unanswered_absent=True)`): a
register without an answer counts as missing. A register polling already left out
as unreadable isn't asked again there, so the experimental functions open
right away. The optional gap between requests (`CONF_MESSAGE_SPACING`, ms)
is passed to `set_message_spacing` of the shared connection only when set,
and set back to 0 on unload. A register that can't be read either
way is left out of the block reads, logged once as a warning, and tried
again on its own with a single attempt every `UNREADABLE_RETRY_SECONDS`
(600 s); meanwhile its entity shows no value.

This also means registers the integration only writes (e.g.
`write_normal_setpoint_reg`/`write_eco_setpoint_reg` per heating circuit,
`DHW_NORMAL_TARGET_TEMP_REGISTER`/`DHW_ECO_TARGET_TEMP_REGISTER`) are
never polled at all, although the controller allows reading them -- no
entity reads them for display, they're only used inside
`async_write_value()`.

## A note on entity naming and `translation_key`

If both `_attr_name` and `_attr_translation_key` are set on an entity, HA
always uses `_attr_name` and never resolves the translation -- checking
`hasattr(self, "_attr_name")` happens *before* the translation lookup in
`Entity._name_internal()`. So don't set `_attr_name` as a "just in case"
fallback alongside `translation_key`; if you want a name to be
translatable, `_attr_translation_key` plus the matching
`entity.<domain>.<key>.name` entry in `strings.json`/`translations/de.json`
must be the *only* source of the name.

## Cooling (per heating circuit)

Cooling is an experimental function (`CONF_EXPERIMENTAL_COOLING`,
`cooling_enabled`). Without it, nothing cooling-related is written or can be
chosen, and no cooling number is created; cooling the controller does on its
own is still shown: `HovalRoomClimate` polls the cooling flow setpoint at
strategy 4/5 and shows `HVACMode.COOL` while it is above 0 (leaving it writes
0, as usual), and shows the cooling room setpoints of weather-compensated
cooling read-only (see below). Turning it off while a circuit's cooling flow
setpoint is above 0 goes through `async_step_confirm_cooling_off`, which
writes 0, reads it back and only then saves the options.

A circuit can cool if `HeatingCircuit.cooling_flow_setpoint_reg` is set
and its control strategy is 4 or 5
(`CONTROL_STRATEGIES_COOLING`). Whether it is, and the circuit's minimum
cooling flow temperature, are read on the first setup and whenever the circuit
is saved in the options (`async_read_circuit_options` in `__init__.py`,
which also reads constant control and the maximum flow temperature), and
stored in the options -- not polled, since they only change when the installer
changes the controller's configuration.

Cooling has no mode of its own on the controller: a flow setpoint above 0 in
`cooling_flow_setpoint_reg` turns it on, 0 turns it off. `HovalRoomClimate`
shows `HVACMode.COOL` whenever that register is above 0, and writes the value
of `HovalCoolingFlowSetpointNumber` (kept by Home Assistant via
`RestoreNumber`, shared through `coordinator.cooling_flow_setpoints`) when
switched to cool, after re-reading the control strategy. Before that it sets
`heating_constant_flow_reg` to 0 if it isn't: with strategy 4/5 the circuit
only cools while the constant heating flow setpoint is 0. The mode register
is not written when cooling starts (Hoval); switching from cool to another
mode writes the cooling flow setpoint 0 first and then the mode register. Heating is weather
compensated with these strategies and doesn't use that register (only
strategy 3 does, see `constant_control.py`), so it isn't restored when cooling
stops.

Weather-compensated cooling (strategy 0/1/2, `weather_cooling_possible`): the
controller switches between heating and cooling by the outdoor temperature
and then uses the cooling room setpoints
(`write_normal_cooling_setpoint_reg` / `write_eco_cooling_setpoint_reg`,
gateway firmware 2.39+, polled by the climate entity of such a circuit). No register
tells whether an installation does this; `HovalRoomClimate` goes by the
circuit's status (`status_reg`, `HEATING_CIRCUIT_STATUS_COOLING`), polled for
`hvac_action`. While it shows cooling, the mode stays as it is, the target
temperature is the cooling room setpoint of the mode (normal in Constant,
eco with the eco preset, stored value without Smart Grid offset; the
current room setpoint in the week programs or if the gateway hasn't got
the register), and is only written with the experimental function, with the
same optimistic update as heating.

## Heating circuits and hot water present

`circuit_present` / `dhw_present` (options `heating_circuit_N_present`,
`dhw_present`; heating circuit 1 always). Every platform, the options menu,
the circuit options read at startup and the room temperature writers only
handle present ones (`present_circuits`); `async_sync_flow_setpoint_raise`
drops a remembered raise of a circuit that isn't there without writing.
`_async_detect_present` in `__init__.py` fills missing options from the
entity registry (a circuit with an enabled entity) or by reading the flow
temperature / hot water temperature directly (other than 0); hot water is
always present for an entry that already had options (other than the
heat pump controller, which is stored when the entry is created). An
option whose register isn't answered stays missing and is detected on the
next start.

## Heat pump controller (WFA200 / ECR461)

Option `heat_pump_controller` (`heat_pump_controller()`, default `wfa` =
WFA200; the models of each are listed in the README's setup). The
codes in 1539 depend on it: `HEAT_PUMP_STATUS_MAPS` holds one map per
controller, with the same key for the same meaning, so the status sensor,
the system diagram card (`MODE_BY_STATUS`; a key ending in `_fault` shows
the status in red) and the raise learning
(`HEAT_PUMP_STATUS_HEATING`) only look at keys, never at raw codes; use
`heat_pump_status()`. The ECR461 has no energy meters: `meters_present()`
is false and the sensors in `METER_SENSOR_KEYS` are created disabled.
The schema WebSocket command reports it as `meters`; the card then uses
`FOOTER_DEFAULT_WITHOUT_METERS` as the footer's default.
No register tells the controllers apart; `suggest_heat_pump_controller`
suggests ECR461 when the operating hours aren't 0 but heat quantity (1505)
and electrical energy (25613) are. It is asked in the config flow's
`controller` step (read through a temporary Modbus unit), stored at the
first start for older entries, and changed under Options -> General.
`update_meter_sensors` runs only when the value is set or changed:
switching to ECR461 disables the enabled meter sensors, switching to WFA
enables those disabled by the integration that are enabled by default, so
a sensor the user enabled again stays enabled.

Every platform adds its entities through `coordinator.add_entities`, which
remembers their unique IDs; after the platforms are set up,
`_remove_stale_entities` removes the entry's disabled registry entries not
among them (Home Assistant doesn't offer to delete a disabled entity, as it
can't tell whether it is still provided). Enabled ones are kept.

## Room temperature source (per heating circuit)

Sending and switching room influence need the experimental function
`CONF_EXPERIMENTAL_ROOM_INFLUENCE` (`room_influence_enabled`); without it the
circuit form offers neither. Turning it off is refused while a circuit sends
(`room_influence_sending`), and strategies with a `previous_strategy_key`
are set back via `async_step_confirm_room_influence_off`. The experimental
step chains its confirmations in `_async_finish_experimental`: room
influence, then cooling, then the raises, then saving.

Each heating circuit's `current_temperature` can come from one of four
sources, configurable **individually per circuit** via the integration's
**options** (entry's cog icon, "Configure" — not the initial setup
form, so it can be changed anytime without re-adding the integration):

- **None** (default) -- no current temperature is shown.
- **Read from Heat Pump (Modbus)** -- reads `HeatingCircuit.room_actual_temp_reg`
  in `const.py`, a fixed register per circuit (addresses `1510`/`1511`/`1512`
  for HC1/HC2/HC3 on this Hoval controller). If you're adapting this to a
  different installation/model and don't know the address, set it to `None`
  for that circuit -- selecting "Modbus" then simply yields no value. The
  same goes for a register value of 0, which means there's no sensor.
- **Home Assistant entity** -- mirrors the state of an existing `sensor`
  entity with `device_class: temperature` (e.g. a separate smart
  thermostat's temperature sensor, so you can use a different sensor than
  the heat pump's own one for that room). Updates live via
  `async_track_state_change_event`, independent of the Modbus polling
  interval, so changes show up immediately rather than waiting for the
  next coordinator refresh. Non-numeric or `unavailable`/`unknown` states
  are treated as "no current temperature" rather than raising an error.
- **Home Assistant entity, sent to the controller** (`entity_write`) -- same
  display as above, and `RoomTempWriter` (`room_temp.py`) writes the value to
  `room_actual_temp_reg`. It runs independently of the climate entities,
  since HC2/HC3 are disabled by default, and reads the control strategy
  directly before each write, independent of what is being polled. If the
  entity is unusable for 5 minutes or hasn't reported (`last_reported`) for
  2 hours, it sends the room setpoint (`read_setpoint_reg`, kept polled
  through its own coordinator listener) while the strategy includes room
  influence, which neutralizes the room influence until the entity recovers.

The form for a circuit is built from what is read when it opens: a value in
the room register other than 0 counts as a sensor, except while sending, when
the register holds the integration's own value. Only the pairs in
`WITHOUT_INFLUENCE_TO_WITH` (0 <-> 1, 4 <-> 5) are ever written,
independently of the chosen source, and only switching room influence on is remembered
(`previous_strategy_key`), so it can be restored when the entry is removed
(`async_remove_entry`). Saving with room influence on, no sensor detected and
nothing sent is refused (`room_influence_without_temperature`): the room
register would hold 0, and how the controller takes 0 is not known.

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

