"""Water heater entity for the domestic hot water (DHW) control."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.water_heater import (
    ATTR_TEMPERATURE,
    STATE_ECO,
    WaterHeaterEntity,
    WaterHeaterEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import (
    DHW_CURRENT_TEMP_REGISTER,
    DHW_ECO_TARGET_TEMP_REGISTER,
    DHW_MODE_REGISTER,
    DHW_MODE_TO_OPERATION,
    DHW_NORMAL_TARGET_TEMP_REGISTER,
    DHW_ACTUAL_TARGET_TEMP_REGISTER,
    DHW_STATE_CONSTANT,
    DOMAIN,
    OPERATION_TO_DHW_MODE,
    RegisterDef,
)
from .coordinator import HovalModbusCoordinator

_LOGGER = logging.getLogger(__name__)

# Custom state attribute exposing whether a mode change has been sent to
# the controller but not yet confirmed by re-reading its mode register --
# same optimistic-update pattern as HovalRoomClimate in climate.py.
ATTR_MODE_CHANGE_PENDING = "mode_change_pending"

# Some Hoval controllers take a while to actually apply a mode change
# internally and reflect it back in the mode register -- independent of
# the Modbus polling interval, since it's the controller itself that's
# slow, not how often we ask it. If a pending change still isn't
# confirmed after this long, give up waiting and show the real (still
# old) device state instead of an optimistic value that might never
# arrive -- e.g. if the write silently failed.
MODE_CHANGE_CONFIRMATION_TIMEOUT = timedelta(seconds=90)

# Same optimistic-update mechanism as mode above, applied to
# target_temperature -- see ATTR_TEMPERATURE_CHANGE_PENDING and
# _effective_target_temperature() below.
ATTR_TEMPERATURE_CHANGE_PENDING = "temperature_change_pending"
TEMPERATURE_CHANGE_CONFIRMATION_TIMEOUT = timedelta(seconds=90)
# Half the DHW_ACTUAL_TARGET_TEMP_REGISTER scale (0.1) -- two floats within
# this distance are considered "the same value" once decoded from the
# register, so rounding during encode/decode doesn't prevent confirmation
# from matching.
TEMPERATURE_CONFIRMATION_EPSILON = 0.05


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: HovalModbusCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([HovalWaterHeater(coordinator, entry)])


class HovalWaterHeater(
    CoordinatorEntity[HovalModbusCoordinator], WaterHeaterEntity
):
    """Water heater entity representing the domestic hot water control.

    Maps the controller's five operating modes onto Home Assistant's
    water_heater operation modes (see DHW_MODE_TO_OPERATION in const.py):
      - "Off"      -> STATE_OFF
      - "Constant" -> "constant"   (manual, continuous heating)
      - "Week 1"   -> "week_1"     (schedule program 1)
      - "Week 2"   -> "week_2"     (schedule program 2)
      - "Eco"      -> STATE_ECO    (efficient, schedule-driven operation)

    "Constant" and "Eco" each have their own target-temperature setpoint
    register on the controller (DHW_NORMAL_TARGET_TEMP_REGISTER /
    DHW_ECO_TARGET_TEMP_REGISTER) -- see
    _target_temp_register_for_current_mode() below, which picks the right
    one to read bounds from and write to. "Off" and the two schedule
    programs (week 1/2) have no directly settable temperature.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "dhw"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_supported_features = (
        WaterHeaterEntityFeature.TARGET_TEMPERATURE
        | WaterHeaterEntityFeature.OPERATION_MODE
    )
    _attr_operation_list = list(OPERATION_TO_DHW_MODE.keys())

    def __init__(
        self, coordinator: HovalModbusCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(
            coordinator,
            context=[
                DHW_CURRENT_TEMP_REGISTER.key,
                DHW_ACTUAL_TARGET_TEMP_REGISTER.key,
                DHW_MODE_REGISTER.key,
                # DHW_NORMAL_TARGET_TEMP_REGISTER / DHW_ECO_TARGET_TEMP_REGISTER
                # intentionally excluded: both are write-only (used in
                # async_set_temperature), never read for display --
                # DHW_ACTUAL_TARGET_TEMP_REGISTER is the read-back register
                # shown as target_temperature regardless of which of the
                # two setpoints is currently in effect.
            ],
        )
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_dhw_water_heater"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Hoval (Modbus)",
            model="Heat Pump Controller",
        )

        # Optimistic-update state -- same mechanism as
        # HovalRoomClimate._pending_mode_value in climate.py: the raw mode
        # value we WROTE (not yet confirmed by re-reading the register) is
        # stashed here so current_operation can reflect it immediately,
        # instead of the entity appearing "stuck" on the old value for as
        # long as the controller takes to actually apply the change.
        self._pending_raw_mode: int | None = None
        self._pending_mode_since: datetime | None = None

        # Same mechanism as _pending_raw_mode above, applied to
        # target_temperature: the value we WROTE (not yet confirmed by
        # re-reading DHW_ACTUAL_TARGET_TEMP_REGISTER) is stashed here so
        # target_temperature can reflect it immediately.
        self._pending_temperature: float | None = None
        self._pending_temperature_since: datetime | None = None

    def _target_temp_register_for_current_mode(self) -> RegisterDef | None:
        """Which write-only setpoint register applies to the current
        operating mode, if any. "Off" and the two schedule programs
        (week 1/2) have no directly settable temperature and return None
        -- callers must handle that (see async_set_temperature())."""
        if self.current_operation == DHW_STATE_CONSTANT:
            return DHW_NORMAL_TARGET_TEMP_REGISTER
        if self.current_operation == STATE_ECO:
            return DHW_ECO_TARGET_TEMP_REGISTER
        return None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Confirm (clear) pending optimistic mode/temperature changes
        once the real device data matches what was written."""
        raw_mode = self.coordinator.data.get(DHW_MODE_REGISTER.key)
        if (
            raw_mode is not None
            and self._pending_raw_mode is not None
            and int(raw_mode) == self._pending_raw_mode
        ):
            self._pending_raw_mode = None
            self._pending_mode_since = None

        raw_setpoint = self.coordinator.data.get(DHW_ACTUAL_TARGET_TEMP_REGISTER.key)
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
        showing a value the controller never actually confirmed."""
        if (
            self._pending_raw_mode is not None
            and self._pending_mode_since is not None
            and dt_util.utcnow() - self._pending_mode_since
            > MODE_CHANGE_CONFIRMATION_TIMEOUT
        ):
            _LOGGER.warning(
                "DHW: mode change to raw value %s was not confirmed by the "
                "controller within %s, giving up and showing its actual "
                "reported state instead",
                self._pending_raw_mode,
                MODE_CHANGE_CONFIRMATION_TIMEOUT,
            )
            self._pending_raw_mode = None
            self._pending_mode_since = None

    def _effective_raw_mode(self) -> int | None:
        """The raw mode value to base current_operation on: the pending
        (optimistic) value if a change is still awaiting confirmation,
        otherwise whatever the controller last reported."""
        self._expire_stale_pending_mode()
        if self._pending_raw_mode is not None:
            return self._pending_raw_mode
        raw_mode = self.coordinator.data.get(DHW_MODE_REGISTER.key)
        return int(raw_mode) if raw_mode is not None else None

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
                "DHW: temperature change to %s was not confirmed by the "
                "controller within %s, giving up and showing its actual "
                "reported state instead",
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
        return self.coordinator.data.get(DHW_ACTUAL_TARGET_TEMP_REGISTER.key)

    @property
    def current_temperature(self) -> float | None:
        """Current measured DHW tank temperature."""
        return self.coordinator.data.get(DHW_CURRENT_TEMP_REGISTER.key)

    @property
    def target_temperature(self) -> float | None:
        """Configured DHW target temperature -- the optimistic pending
        value right after a change, until the controller confirms it."""
        return self._effective_target_temperature()

    @property
    def min_temp(self) -> float:
        """Lower bound for the setpoint register active in the current
        mode (constant vs. eco). While the mode has no settable
        temperature at all (Off / week 1 / week 2), collapses to the
        current target_temperature -- i.e. min == max == the displayed
        value (see max_temp) -- rather than hiding TARGET_TEMPERATURE
        from supported_features entirely.

        That alternative was tried first and reverted: hiding the
        feature also hides target_temperature from being *displayed* at
        all (HA's WaterHeaterEntity only includes the temperature
        attribute when WaterHeaterEntityFeature.TARGET_TEMPERATURE is
        set), so the controller's actual current setpoint became
        invisible in those modes -- worse than the problem it was meant
        to solve. Collapsing the range instead keeps the value visible
        and gives the frontend stepper/slider nowhere to actually move
        to, while async_set_temperature() still raises
        ServiceValidationError as a backstop for calls made directly via
        automations/the API.
        """
        reg = self._target_temp_register_for_current_mode()
        if reg is not None and reg.min_value is not None:
            return reg.min_value
        current = self.target_temperature
        if current is not None:
            return current
        return DHW_NORMAL_TARGET_TEMP_REGISTER.min_value or 30.0

    @property
    def max_temp(self) -> float:
        """Upper bound -- see min_temp for the collapsed-range rationale."""
        reg = self._target_temp_register_for_current_mode()
        if reg is not None and reg.max_value is not None:
            return reg.max_value
        current = self.target_temperature
        if current is not None:
            return current
        return DHW_NORMAL_TARGET_TEMP_REGISTER.max_value or 65.0

    @property
    def target_temperature_step(self) -> float | None:
        """Step size -- see min_temp for the collapsed-range rationale."""
        reg = self._target_temp_register_for_current_mode()
        if reg is not None and reg.step is not None:
            return reg.step
        return DHW_NORMAL_TARGET_TEMP_REGISTER.step or 0.5

    @property
    def current_operation(self) -> str | None:
        """Current operating mode -- the optimistic pending value right
        after a change, until the controller confirms it (or the change
        times out unconfirmed), then the real, translated register value."""
        raw_mode = self._effective_raw_mode()
        if raw_mode is None:
            return None
        return DHW_MODE_TO_OPERATION.get(raw_mode)

    @property
    def available(self) -> bool:
        """Entity is only available once the coordinator has valid data."""
        return (
            super().available
            and self.coordinator.data.get(DHW_MODE_REGISTER.key) is not None
        )

    @property
    def extra_state_attributes(self) -> dict[str, bool]:
        """Expose whether a mode or temperature change is still awaiting
        confirmation from the controller -- e.g. for a dashboard card to
        show a "syncing..." indicator."""
        return {
            ATTR_MODE_CHANGE_PENDING: self._pending_raw_mode is not None,
            ATTR_TEMPERATURE_CHANGE_PENDING: self._pending_temperature is not None,
        }

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Write a new target temperature to whichever setpoint register
        applies to the current mode, showing it optimistically right away
        (see the pending-temperature mechanism above).

        Raises ServiceValidationError if the current mode has no directly
        settable temperature (Off, week 1/2 schedule programs). Raising
        (rather than silently no-oping) also causes the frontend to
        reject/revert its own optimistic display of the attempted value,
        instead of leaving the UI showing a temperature that was never
        actually accepted by the controller.
        """
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return

        target_reg = self._target_temp_register_for_current_mode()
        if target_reg is None:
            _LOGGER.warning(
                "DHW: cannot set target temperature while in operation "
                "mode %r (only supported in %r or %r)",
                self.current_operation,
                DHW_STATE_CONSTANT,
                STATE_ECO,
            )
            raise ServiceValidationError(
                f"Cannot set a target DHW temperature while in operation "
                f"mode {self.current_operation!r} -- only supported in "
                f"{DHW_STATE_CONSTANT!r} or {STATE_ECO!r}."
            )

        self._pending_temperature = temperature
        self._pending_temperature_since = dt_util.utcnow()
        self.async_write_ha_state()
        await self.coordinator.async_write_value(target_reg, temperature)

    async def async_set_operation_mode(self, operation_mode: str) -> None:
        """Write a new operating mode to the controller, showing it
        optimistically right away (see the pending-mode mechanism above)."""
        raw_mode = OPERATION_TO_DHW_MODE.get(operation_mode)
        if raw_mode is None:
            raise ValueError(f"Unsupported operation mode: {operation_mode}")
        self._pending_raw_mode = raw_mode
        self._pending_mode_since = dt_util.utcnow()
        self.async_write_ha_state()
        await self.coordinator.async_write_value(DHW_MODE_REGISTER, raw_mode)
