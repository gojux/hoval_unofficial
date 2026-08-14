"""Climate entities for the room heating circuits (HC1/HC2/HC3)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
    PRESET_ECO,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_TEMPERATURE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTemperature,
)
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import (
    HeatingCircuit,
    HEATING_CIRCUITS,
    CLIMATE_MODE_TO_HVAC,
    CLIMATE_MODE_TO_PRESET,
    CLIMATE_PRESET_OPTIONS,
    CLIMATE_PRESET_WEEK1,
    CURRENT_TEMP_SOURCE_ENTITY,
    CURRENT_TEMP_SOURCE_MODBUS,
    DEFAULT_CURRENT_TEMP_SOURCE,
    DOMAIN,
    HVAC_TO_CLIMATE_MODE,
    PRESET_TO_CLIMATE_MODE,
    RegisterDef,
    current_temp_entity_key,
    current_temp_source_key,
)
from .coordinator import HovalModbusCoordinator

_LOGGER = logging.getLogger(__name__)

# Custom state attribute used to persist HovalRoomClimate._last_preset across
# Home Assistant restarts via RestoreEntity. Deliberately NOT the built-in
# ATTR_PRESET_MODE -- that one always reflects the *live* preset_mode
# property, which is None whenever the circuit isn't currently in AUTO
# (i.e. almost always, for most restarts). Writing this unconditionally in
# extra_state_attributes below is what makes the restore actually work for
# the common case of restarting while the circuit is sitting at
# Off/Constant.
ATTR_LAST_AUTO_PROGRAM = "last_auto_program"

# Custom state attribute exposing whether a mode/preset change has been
# sent to the controller but not yet confirmed by re-reading its mode
# register (see the "optimistic update" mechanism below).
ATTR_MODE_CHANGE_PENDING = "mode_change_pending"

# Some Hoval controllers take a while (users have observed ~30s) to
# actually apply a mode change internally and reflect it back in the mode
# register -- independent of the Modbus polling interval, since it's the
# controller itself that's slow, not how often we ask it. If a pending
# change still isn't confirmed after this long, give up waiting and show
# the real (still-old) device state instead of an optimistic value that
# might never arrive -- e.g. if the write silently failed.
MODE_CHANGE_CONFIRMATION_TIMEOUT = timedelta(seconds=90)

# Same optimistic-update mechanism as mode/preset above, applied to
# target_temperature -- see ATTR_TEMPERATURE_CHANGE_PENDING and
# _effective_target_temperature() below.
ATTR_TEMPERATURE_CHANGE_PENDING = "temperature_change_pending"
TEMPERATURE_CHANGE_CONFIRMATION_TIMEOUT = timedelta(seconds=90)
# Half the read_setpoint_reg scale (0.1) -- two floats within this distance
# are considered "the same value" once decoded from the register, so
# rounding during encode/decode doesn't prevent confirmation from matching.
TEMPERATURE_CONFIRMATION_EPSILON = 0.05


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: HovalModbusCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [
        HovalRoomClimate(coordinator, entry, hc)
        for hc in HEATING_CIRCUITS
    ]
    async_add_entities(entities)


class HovalRoomClimate(
    CoordinatorEntity[HovalModbusCoordinator], RestoreEntity, ClimateEntity
):
    """Climate entity representing a room heating circuit (HC1/HC2/HC3).

    Maps the controller's five operating modes onto Home Assistant:
      - "Off"      -> HVACMode.OFF
      - "Constant"  -> HVACMode.HEAT   (manual, constant setpoint)
      - "Week 1" / "Week 2" / "Eco" -> HVACMode.AUTO, with the specific
        program exposed via `preset_mode` ("week1" / "week2" / PRESET_ECO).
        HVACMode itself is a closed enum in HA, so it can't represent three
        distinct "automatic" programs on its own -- preset is the correct
        mechanism for that, the same way a real thermostat's "Auto" mode
        can still offer "Eco"/"Away" presets.

    Presets are only ever offered (`preset_modes` returns non-None) while
    `hvac_mode == HVACMode.AUTO` -- see that property below. There's no
    "None"/placeholder preset: whenever the picker is shown at all, a real
    program is always active and selectable, so no such placeholder is
    needed.
    """

    _attr_has_entity_name = True
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.PRESET_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT, HVACMode.AUTO]

    def __init__(
        self,
        coordinator: HovalModbusCoordinator,
        entry: ConfigEntry,
        heating_circuit: HeatingCircuit,
    ) -> None:
        self._hc = heating_circuit

        # Where current_temperature comes from -- configurable per circuit
        # via the options flow (config_flow.py), stored in entry.options.
        # Re-evaluated on every reload, since changing an option reloads
        # the config entry. entry.data.get(...) as a fallback costs
        # nothing and is harmless even though nothing writes there today.
        source_key = current_temp_source_key(heating_circuit)
        entity_key = current_temp_entity_key(heating_circuit)
        self._current_temp_source = entry.options.get(
            source_key, entry.data.get(source_key, DEFAULT_CURRENT_TEMP_SOURCE)
        )
        self._current_temp_entity_id: str | None = entry.options.get(
            entity_key, entry.data.get(entity_key)
        )

        # The registers this specific entity actually reads, passed as the
        # coordinator-subscription "context" -- see async_contexts() in
        # coordinator.py. mode_reg/read_setpoint_reg are always needed;
        # write_normal_setpoint_reg/write_eco_setpoint_reg are intentionally
        # excluded (write-only, never read for display). room_actual_temp_reg
        # is only included if this circuit's source is actually set to
        # "modbus" AND the register is configured, so switching to
        # "none"/"entity" also stops polling it.
        context = [heating_circuit.mode_reg.key, heating_circuit.read_setpoint_reg.key]
        if (
            self._current_temp_source == CURRENT_TEMP_SOURCE_MODBUS
            and heating_circuit.room_actual_temp_reg is not None
        ):
            context.append(heating_circuit.room_actual_temp_reg.key)

        super().__init__(coordinator, context=context)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{heating_circuit.key}"
        self._attr_translation_key = heating_circuit.key
        self._attr_entity_registry_enabled_default = heating_circuit.enabled_default
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Hoval (Modbus)",
            model="Heat Pump Controller",
        )

        # Only used for CURRENT_TEMP_SOURCE_ENTITY: mirrors the source
        # entity's state, kept up to date via async_track_state_change_event
        # rather than the Modbus polling interval, so changes show up
        # immediately instead of waiting for the next coordinator refresh.
        self._external_current_temp: float | None = None

        # Remembers which AUTO program (week1/week2/eco) was last active,
        # so switching Off -> Auto restores that program instead of always
        # falling back to week1. Kept in sync with the actual device state
        # in _handle_coordinator_update() below (not just with what HA
        # itself last commanded), so it stays correct even if the mode was
        # changed directly at the heat pump's own panel. Restored from HA's
        # own entity state history on startup (see async_added_to_hass()
        # below, using RestoreEntity) -- so it survives a Home Assistant
        # restart even if the circuit is currently Off (in which case the
        # coordinator's raw mode data alone wouldn't tell us which AUTO
        # program was last active).
        self._last_preset: str = CLIMATE_PRESET_WEEK1

        # Optimistic-update state: when a mode/preset change is written,
        # the raw value we WROTE (not yet confirmed by re-reading the
        # register) is stashed here so hvac_mode/preset_mode/preset_modes
        # can reflect it immediately, instead of the entity appearing
        # "stuck" on the old value for as long as the controller takes to
        # actually apply the change (observed: up to ~30s on some Hoval
        # controllers, unrelated to the Modbus polling interval). Cleared
        # by _handle_coordinator_update() once the real device data
        # matches, or after MODE_CHANGE_CONFIRMATION_TIMEOUT if it never
        # does.
        self._pending_mode_value: int | None = None
        self._pending_mode_since: datetime | None = None

        # Same mechanism as _pending_mode_value above, applied to
        # target_temperature: the value we WROTE (not yet confirmed by
        # re-reading read_setpoint_reg) is stashed here so
        # target_temperature can reflect it immediately.
        self._pending_temperature: float | None = None
        self._pending_temperature_since: datetime | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        # Restore the last known preset from Home Assistant's own entity
        # state history (RestoreEntity). IMPORTANT: restore from the custom
        # ATTR_LAST_AUTO_PROGRAM attribute (see extra_state_attributes
        # below), NOT from the built-in `preset_mode` attribute --
        # `preset_mode` always reflects the *live* value, which is None
        # whenever the circuit isn't currently in AUTO. Since most restarts
        # happen while the circuit is sitting at Off/Constant (not actively
        # mid-program), the recorded `preset_mode` would almost always be
        # missing/None and never actually restore anything --
        # ATTR_LAST_AUTO_PROGRAM is written unconditionally, capturing
        # exactly the case a plain `preset_mode` restore can't.
        #
        # _handle_coordinator_update() will override this with the real,
        # current device state as soon as the first Modbus poll after
        # startup completes (if the circuit happens to already be in
        # AUTO) -- this restore is specifically for bridging the gap
        # right at startup, and for the case where the circuit is
        # currently Off, so the coordinator's raw data alone doesn't
        # reveal which AUTO program was last active.
        if (last_state := await self.async_get_last_state()) is not None:
            restored_preset = last_state.attributes.get(ATTR_LAST_AUTO_PROGRAM)
            if restored_preset in PRESET_TO_CLIMATE_MODE:
                self._last_preset = restored_preset

        if (
            self._current_temp_source == CURRENT_TEMP_SOURCE_ENTITY
            and self._current_temp_entity_id
        ):
            self._update_external_current_temp(
                self.hass.states.get(self._current_temp_entity_id)
            )
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass,
                    [self._current_temp_entity_id],
                    self._handle_source_entity_change,
                )
            )

    @callback
    def _handle_source_entity_change(self, event: Event[EventStateChangedData]) -> None:
        self._update_external_current_temp(event.data["new_state"])
        self.async_write_ha_state()

    def _update_external_current_temp(self, state) -> None:
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            self._external_current_temp = None
            return
        try:
            self._external_current_temp = float(state.state)
        except (TypeError, ValueError):
            _LOGGER.warning(
                "%s: source entity %s has a non-numeric state (%r), ignoring",
                self._hc.key,
                self._current_temp_entity_id,
                state.state,
            )
            self._external_current_temp = None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Sync the last-known-preset cache with the actual device state,
        and confirm (clear) pending optimistic mode/preset/temperature
        changes once the real device data matches what was written.

        Runs on every coordinator refresh, not just after HA-initiated
        writes -- so if someone changes the mode directly at the heat
        pump's own panel (not through Home Assistant), _last_preset still
        reflects reality the next time the circuit is switched Off -> Auto.
        """
        raw_mode = self.coordinator.data.get(self._hc.mode_reg.key)
        if raw_mode is not None:
            preset = CLIMATE_MODE_TO_PRESET.get(int(raw_mode))
            if preset is not None:
                self._last_preset = preset

            if (
                self._pending_mode_value is not None
                and int(raw_mode) == self._pending_mode_value
            ):
                # Confirmed: the controller now reports exactly the mode we
                # wrote, so the optimistic value is no longer needed.
                self._pending_mode_value = None
                self._pending_mode_since = None

        raw_setpoint = self.coordinator.data.get(self._hc.read_setpoint_reg.key)
        if (
            self._pending_temperature is not None
            and raw_setpoint is not None
            and abs(raw_setpoint - self._pending_temperature)
            < TEMPERATURE_CONFIRMATION_EPSILON
        ):
            self._pending_temperature = None
            self._pending_temperature_since = None

        self._expire_stale_pending_mode()
        self._expire_stale_pending_temperature()
        super()._handle_coordinator_update()

    def _expire_stale_pending_mode(self) -> None:
        """Give up on an unconfirmed optimistic mode change after
        MODE_CHANGE_CONFIRMATION_TIMEOUT, so the entity doesn't stay stuck
        showing a value the controller never actually confirmed (e.g. if
        the write silently failed) forever."""
        if (
            self._pending_mode_value is not None
            and self._pending_mode_since is not None
            and dt_util.utcnow() - self._pending_mode_since
            > MODE_CHANGE_CONFIRMATION_TIMEOUT
        ):
            _LOGGER.warning(
                "%s: mode change to raw value %s was not confirmed by the "
                "controller within %s, giving up and showing its actual "
                "reported state instead",
                self._hc.key,
                self._pending_mode_value,
                MODE_CHANGE_CONFIRMATION_TIMEOUT,
            )
            self._pending_mode_value = None
            self._pending_mode_since = None

    def _effective_raw_mode(self) -> int | None:
        """The raw mode value to base hvac_mode/preset_mode/preset_modes
        on: the pending (optimistic) value if a change is still awaiting
        confirmation, otherwise whatever the controller last reported."""
        self._expire_stale_pending_mode()
        if self._pending_mode_value is not None:
            return self._pending_mode_value
        raw_mode = self.coordinator.data.get(self._hc.mode_reg.key)
        return int(raw_mode) if raw_mode is not None else None

    async def _async_write_mode(self, raw_mode: int) -> None:
        """Write a raw mode value, showing it optimistically right away."""
        self._pending_mode_value = raw_mode
        self._pending_mode_since = dt_util.utcnow()
        self.async_write_ha_state()
        await self.coordinator.async_write_value(self._hc.mode_reg, raw_mode)

    def _expire_stale_pending_temperature(self) -> None:
        """Give up on an unconfirmed optimistic temperature change after
        TEMPERATURE_CHANGE_CONFIRMATION_TIMEOUT, same rationale as
        _expire_stale_pending_mode() above."""
        if (
            self._pending_temperature is not None
            and self._pending_temperature_since is not None
            and dt_util.utcnow() - self._pending_temperature_since
            > TEMPERATURE_CHANGE_CONFIRMATION_TIMEOUT
        ):
            _LOGGER.warning(
                "%s: temperature change to %s was not confirmed by the "
                "controller within %s, giving up and showing its actual "
                "reported state instead",
                self._hc.key,
                self._pending_temperature,
                TEMPERATURE_CHANGE_CONFIRMATION_TIMEOUT,
            )
            self._pending_temperature = None
            self._pending_temperature_since = None

    def _effective_target_temperature(self) -> float | None:
        """The target temperature to report: the pending (optimistic)
        value if a change is still awaiting confirmation, otherwise
        whatever the controller last reported."""
        self._expire_stale_pending_temperature()
        if self._pending_temperature is not None:
            return self._pending_temperature
        return self.coordinator.data.get(self._hc.read_setpoint_reg.key)

    def _target_setpoint_register_for_current_mode(self) -> RegisterDef | None:
        """Which write-only setpoint register applies to the current mode:
        write_normal_setpoint_reg while HVACMode.HEAT ("Constant"),
        write_eco_setpoint_reg while HVACMode.AUTO with the "eco" preset
        active. None for Off and the Week1/Week2 schedule presets, which
        are schedule-driven on the controller and have no directly
        settable temperature -- callers must handle that (see
        async_set_temperature())."""
        if self.hvac_mode == HVACMode.HEAT:
            return self._hc.write_normal_setpoint_reg
        if self.hvac_mode == HVACMode.AUTO and self.preset_mode == PRESET_ECO:
            return self._hc.write_eco_setpoint_reg
        return None

    @property
    def current_temperature(self) -> float | None:
        """Current room temperature, from whichever source is configured."""
        if self._current_temp_source == CURRENT_TEMP_SOURCE_MODBUS:
            reg = self._hc.room_actual_temp_reg
            if reg is None:
                return None
            return self.coordinator.data.get(reg.key)
        if self._current_temp_source == CURRENT_TEMP_SOURCE_ENTITY:
            return self._external_current_temp
        return None  # CURRENT_TEMP_SOURCE_NONE (or unknown/unset)

    @property
    def target_temperature(self) -> float | None:
        """Configured target temperature -- the optimistic pending value
        right after a change, until the controller confirms it."""
        return self._effective_target_temperature()

    @property
    def min_temp(self) -> float:
        """Lower bound for the setpoint register active in the current
        mode (normal vs. eco). While the mode has no settable temperature
        at all (Off / Week1 / Week2), collapses to the current
        target_temperature -- i.e. min == max == the displayed value (see
        max_temp) -- rather than hiding TARGET_TEMPERATURE from
        supported_features entirely.

        That alternative was tried first and reverted: hiding the feature
        also hides target_temperature from being *displayed* at all (HA's
        ClimateEntity.state_attributes only includes ATTR_TEMPERATURE
        when ClimateEntityFeature.TARGET_TEMPERATURE is set), so the
        controller's actual current setpoint became invisible in those
        modes -- worse than the problem it was meant to solve. Collapsing
        the range instead keeps the value visible and gives the frontend
        stepper/slider nowhere to actually move to, while
        async_set_temperature() still raises ServiceValidationError as a
        backstop for calls made directly via automations/the API.
        """
        reg = self._target_setpoint_register_for_current_mode()
        if reg is not None and reg.min_value is not None:
            return reg.min_value
        current = self.target_temperature
        if current is not None:
            return current
        return self._hc.write_normal_setpoint_reg.min_value or 10.0

    @property
    def max_temp(self) -> float:
        """Upper bound -- see min_temp for the collapsed-range rationale."""
        reg = self._target_setpoint_register_for_current_mode()
        if reg is not None and reg.max_value is not None:
            return reg.max_value
        current = self.target_temperature
        if current is not None:
            return current
        return self._hc.write_normal_setpoint_reg.max_value or 30.0

    @property
    def target_temperature_step(self) -> float:
        """Step size -- see min_temp for the collapsed-range rationale."""
        reg = self._target_setpoint_register_for_current_mode()
        if reg is not None and reg.step is not None:
            return reg.step
        return self._hc.write_normal_setpoint_reg.step or 0.5

    @property
    def hvac_mode(self) -> HVACMode | None:
        """Current operating mode -- the optimistic pending value right
        after a change, until the controller confirms it (or the change
        times out unconfirmed), then the real, translated register value."""
        raw_mode = self._effective_raw_mode()
        if raw_mode is None:
            return None
        return CLIMATE_MODE_TO_HVAC.get(raw_mode)

    @property
    def preset_modes(self) -> list[str] | None:
        """Only offer presets while actually in AUTO.

        Home Assistant's own service-call validation
        (`_valid_mode_or_raise()`) checks the submitted preset against
        this list before `async_set_preset_mode()` is even called, so
        returning `None` here also has the side effect of rejecting any
        attempt to set a preset while not in AUTO with a clear
        `ServiceValidationError`, instead of silently writing something
        that wouldn't make sense to the controller.
        """
        if self.hvac_mode != HVACMode.AUTO:
            return None
        return CLIMATE_PRESET_OPTIONS

    @property
    def preset_mode(self) -> str | None:
        """Which AUTO program is active (week1/week2/eco), or None if not
        currently in AUTO -- there's no "no preset" placeholder anymore
        now that presets are only ever shown while in AUTO. Like
        hvac_mode, reflects the optimistic pending value until confirmed."""
        if self.hvac_mode != HVACMode.AUTO:
            return None
        raw_mode = self._effective_raw_mode()
        if raw_mode is None:
            return None
        return CLIMATE_MODE_TO_PRESET.get(raw_mode)

    @property
    def available(self) -> bool:
        """Entity is only available once the coordinator has valid data."""
        return (
            super().available
            and self.coordinator.data.get(self._hc.mode_reg.key) is not None
        )

    @property
    def extra_state_attributes(self) -> dict[str, str | bool]:
        """Expose _last_preset unconditionally, so RestoreEntity can bring
        it back after a restart even while the circuit is currently
        Off/Constant (see ATTR_LAST_AUTO_PROGRAM and async_added_to_hass()
        for why this can't just use the built-in preset_mode attribute).

        Also exposes whether a mode/preset or temperature change is still
        awaiting confirmation from the controller (see the
        optimistic-update mechanisms above) -- e.g. for a dashboard card
        to show a "syncing..." indicator."""
        return {
            ATTR_LAST_AUTO_PROGRAM: self._last_preset,
            ATTR_MODE_CHANGE_PENDING: self._pending_mode_value is not None,
            ATTR_TEMPERATURE_CHANGE_PENDING: self._pending_temperature is not None,
        }

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Write a new target temperature to whichever setpoint register
        applies to the current mode, showing it optimistically right away
        (see the pending-temperature mechanism above).

        Meaningful in HVACMode.HEAT ("Constant", writes
        write_normal_setpoint_reg) and while in AUTO with the "eco" preset
        active (writes write_eco_setpoint_reg). Off and the Week1/Week2
        schedule presets are schedule-driven on the controller itself --
        raises ServiceValidationError in that case (which also causes the
        frontend to reject/revert its own optimistic display of the
        attempted value, rather than a silent no-op leaving the UI showing
        a temperature that was never actually accepted).
        """
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return

        target_reg = self._target_setpoint_register_for_current_mode()
        if target_reg is None:
            _LOGGER.warning(
                "%s: cannot set target temperature while in mode %r / "
                "preset %r (only supported in %r, or %r with the %r "
                "preset)",
                self._hc.key,
                self.hvac_mode,
                self.preset_mode,
                HVACMode.HEAT,
                HVACMode.AUTO,
                PRESET_ECO,
            )
            raise ServiceValidationError(
                f"Cannot set a target temperature for {self._hc.key} while "
                f"in mode {self.hvac_mode!r} / preset {self.preset_mode!r} "
                f"-- only supported in {HVACMode.HEAT!r} (Constant), or "
                f"{HVACMode.AUTO!r} (Auto) with the {PRESET_ECO!r} preset."
            )

        self._pending_temperature = temperature
        self._pending_temperature_since = dt_util.utcnow()
        self.async_write_ha_state()
        await self.coordinator.async_write_value(target_reg, temperature)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Write a new operating mode (Off / Constant / Auto) to the controller.

        For AUTO, writes whichever program (week1/week2/eco) was last
        active for this circuit -- see self._last_preset -- instead of
        always defaulting to week1. That avoids a redundant second Modbus
        write when switching Off -> Auto and the user actually wants
        week2/eco: previously, HA always sent week1 first, and only sent
        the real desired program if/when the user then picked a different
        preset explicitly.
        """
        if hvac_mode == HVACMode.AUTO:
            raw_mode = PRESET_TO_CLIMATE_MODE.get(
                self._last_preset, HVAC_TO_CLIMATE_MODE[HVACMode.AUTO]
            )
        else:
            raw_mode = HVAC_TO_CLIMATE_MODE.get(hvac_mode)
        if raw_mode is None:
            raise ValueError(f"Unsupported HVAC mode: {hvac_mode}")
        await self._async_write_mode(raw_mode)

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Select a specific AUTO program (week1 / week2 / eco).

        Only ever called by HA with a value from self.preset_modes (see
        that property) -- which is None whenever hvac_mode != AUTO, so
        Home Assistant itself already rejects preset changes outside of
        AUTO with a ServiceValidationError before this runs.
        """
        raw_mode = PRESET_TO_CLIMATE_MODE.get(preset_mode)
        if raw_mode is None:
            raise ValueError(f"Unsupported preset mode: {preset_mode}")
        # Update the cache optimistically (before the write completes /
        # before the next coordinator refresh confirms it), so an
        # immediate Off -> Auto right afterwards already picks this up.
        self._last_preset = preset_mode
        await self._async_write_mode(raw_mode)
