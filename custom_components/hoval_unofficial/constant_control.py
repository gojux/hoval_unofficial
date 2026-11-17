"""Constant control (control strategy 3) of a heating circuit.

The controller decides between heating, off and cooling by its two constant
flow setpoints: heating while only the heating setpoint is above 0, cooling
while only the cooling setpoint is, off while both are 0. A select sets the
mode, two number entities hold the setpoints. A setpoint is written only
while its mode is active; otherwise it is only kept.

Switching the mode first sets the other setpoint to 0, checks that it reads
0, waits SWITCH_PAUSE_SECONDS and then writes the setpoint of the new mode,
so both are never above 0 at the same time.

Created only while constant control is turned on and the circuit runs on
strategy 3 (see constant_control_enabled).
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.update_coordinator import UpdateFailed

from .const import (
    CONSTANT_COOLING_DEFAULT,
    CONSTANT_HEATING_DEFAULT,
    CONSTANT_MODE_COOL,
    CONSTANT_MODE_HEAT,
    CONSTANT_MODE_OFF,
    CONTROL_STRATEGY_CONSTANT,
    DOMAIN,
    HeatingCircuit,
    RegisterDef,
    constant_heating_range,
    cooling_enabled,
    cooling_flow_range,
)
from .coordinator import HovalModbusCoordinator

_LOGGER = logging.getLogger(__name__)

SWITCH_PAUSE_SECONDS = 10
# Half the registers' resolution of 0.1 °C.
EPSILON = 0.05


def _clamp(value: float, bounds: tuple[float, float]) -> float:
    return min(max(value, bounds[0]), bounds[1])


class ConstantFlowControl:
    """Mode and flow setpoints of one heating circuit on constant control."""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: HovalModbusCoordinator,
        entry: ConfigEntry,
        heating_circuit: HeatingCircuit,
    ) -> None:
        self._hass = hass
        self._coordinator = coordinator
        self._entry = entry
        self.hc = heating_circuit
        self._heating_reg = heating_circuit.heating_constant_flow_reg
        self._cooling_reg = heating_circuit.cooling_flow_setpoint_reg
        self.keys = [self._heating_reg.key, self._cooling_reg.key]
        self.cooling_offered = cooling_enabled(entry.options)
        self.heating_range = constant_heating_range(heating_circuit, entry.options)
        self.cooling_range = cooling_flow_range(heating_circuit, entry.options)
        self.heating = _clamp(CONSTANT_HEATING_DEFAULT, self.heating_range)
        self.cooling = _clamp(CONSTANT_COOLING_DEFAULT, self.cooling_range)
        # The mode being switched to, shown until the switch is done.
        self.pending_mode: str | None = None
        self._lock = asyncio.Lock()
        self._remove_listener: CALLBACK_TYPE | None = None
        self._update_callbacks: list[Callable[[], None]] = []
        self._warned_invalid = False

    @property
    def modes(self) -> list[str]:
        modes = [CONSTANT_MODE_HEAT, CONSTANT_MODE_OFF]
        if self.cooling_offered:
            modes.append(CONSTANT_MODE_COOL)
        return modes

    @callback
    def async_start(self) -> None:
        # Added before the platforms, so the entities see adopted values.
        self._remove_listener = self._coordinator.async_add_listener(
            self._handle_update, context=self.keys
        )

    @callback
    def async_stop(self) -> None:
        if self._remove_listener is not None:
            self._remove_listener()
            self._remove_listener = None

    @callback
    def async_add_update_callback(self, update: Callable[[], None]) -> CALLBACK_TYPE:
        """Calls `update` whenever the mode or a setpoint changes."""
        self._update_callbacks.append(update)
        return lambda: self._update_callbacks.remove(update)

    @callback
    def _notify(self) -> None:
        for update in list(self._update_callbacks):
            update()

    def mode(self) -> str | None:
        """The mode the controller is in, from the polled setpoints; None if
        unknown or if both setpoints are above 0."""
        data = self._coordinator.data or {}
        heating = data.get(self._heating_reg.key)
        cooling = data.get(self._cooling_reg.key)
        if heating is None or cooling is None:
            return None
        if heating > 0 and cooling > 0:
            return None
        if heating > 0:
            return CONSTANT_MODE_HEAT
        if cooling > 0:
            return CONSTANT_MODE_COOL
        return CONSTANT_MODE_OFF

    @callback
    def _handle_update(self) -> None:
        # While writing, the polled values can be from before the write.
        if self._lock.locked():
            return
        data = self._coordinator.data or {}
        heating = data.get(self._heating_reg.key)
        cooling = data.get(self._cooling_reg.key)
        if heating and cooling and heating > 0 and cooling > 0:
            if not self._warned_invalid:
                _LOGGER.warning(
                    "%s: both constant flow setpoints are above 0 (heating %s, "
                    "cooling %s); choose a mode to correct this",
                    self.hc.key,
                    heating,
                    cooling,
                )
                self._warned_invalid = True
            return
        self._warned_invalid = False
        # A setpoint changed elsewhere, e.g. on the operating module.
        mode = self.mode()
        if mode == CONSTANT_MODE_HEAT and abs(heating - self.heating) >= EPSILON:
            self.heating = round(heating, 1)
        elif mode == CONSTANT_MODE_COOL and abs(cooling - self.cooling) >= EPSILON:
            self.cooling = round(cooling, 1)

    def restore_heating(self, value: float) -> None:
        """Takes a value kept by Home Assistant, unless the controller heats
        with its own."""
        if self.mode() != CONSTANT_MODE_HEAT:
            self.heating = _clamp(value, self.heating_range)

    def restore_cooling(self, value: float) -> None:
        if self.mode() != CONSTANT_MODE_COOL:
            self.cooling = _clamp(value, self.cooling_range)

    async def _async_check_strategy(self) -> None:
        strategy = int(
            await self._coordinator.async_read_value(self.hc.control_strategy_reg)
        )
        if strategy != CONTROL_STRATEGY_CONSTANT:
            raise ServiceValidationError(
                f"{self.hc.key} no longer runs on constant control ({strategy}); "
                "save the heating circuit in the integration's options again."
            )

    async def async_set_mode(self, mode: str) -> None:
        """Checks the request and switches the mode in the background, since
        switching takes a while."""
        if mode not in self.modes:
            raise ServiceValidationError(f"{self.hc.key}: mode {mode} isn't offered")
        await self._async_check_strategy()
        self.pending_mode = mode
        self._notify()
        self._entry.async_create_background_task(
            self._hass,
            self._async_switch(mode),
            f"{DOMAIN} constant control {self.hc.key}",
        )

    async def _async_switch(self, mode: str) -> None:
        try:
            async with self._lock:
                if mode == CONSTANT_MODE_OFF:
                    await self._async_write(self._heating_reg, 0)
                    await self._async_write(self._cooling_reg, 0)
                else:
                    if mode == CONSTANT_MODE_HEAT:
                        zero_reg, value_reg = self._cooling_reg, self._heating_reg
                    else:
                        zero_reg, value_reg = self._heating_reg, self._cooling_reg
                    await self._async_write(zero_reg, 0)
                    if await self._coordinator.async_read_value(zero_reg) != 0:
                        raise UpdateFailed(f"{zero_reg.key} doesn't read 0 after writing 0")
                    await asyncio.sleep(SWITCH_PAUSE_SECONDS)
                    # The setpoint as it is now, it may have changed meanwhile.
                    value = self.heating if mode == CONSTANT_MODE_HEAT else self.cooling
                    await self._async_write(value_reg, value)
        except UpdateFailed as err:
            _LOGGER.warning("%s: switching to %s failed: %s", self.hc.key, mode, err)
        finally:
            # Refreshed before the pending mode is dropped, so the mode shown
            # doesn't fall back to the one from before the switch.
            await self._coordinator.async_refresh()
            if self.pending_mode == mode:
                self.pending_mode = None
            self._notify()

    async def _async_write(self, reg: RegisterDef, value: float) -> None:
        await self._coordinator.async_write_value(reg, value, refresh=False)

    async def async_set_heating(self, value: float) -> None:
        self.heating = value
        self._notify()
        await self._async_write_active(CONSTANT_MODE_HEAT, self._heating_reg, value)

    async def async_set_cooling(self, value: float) -> None:
        self.cooling = value
        self._notify()
        await self._async_write_active(CONSTANT_MODE_COOL, self._cooling_reg, value)

    def _may_write_active(self, mode: str) -> bool:
        """Whether `mode` is active and no switch is pending. The pending
        mode is kept until the switch's refresh is done, so it also covers
        the polled values from before the switch."""
        return self.pending_mode is None and self.mode() == mode

    async def _async_write_active(
        self, mode: str, reg: RegisterDef, value: float
    ) -> None:
        """Writes the setpoint right away while its mode is active. During a
        switch, the switch writes the setpoint as it is then."""
        if self._lock.locked() or not self._may_write_active(mode):
            return
        await self._async_check_strategy()
        async with self._lock:
            # Checked again: a switch may have started while the strategy
            # was read, and writing now would undo it.
            if not self._may_write_active(mode):
                return
            await self._async_write(reg, value)
        await self._coordinator.async_request_refresh()
