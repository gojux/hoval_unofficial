"""Works out the flow setpoint raise of heating circuit 1 on every poll and,
while the automatic adjustment is on, writes it to the controller.

Created only while heating circuit 1's raise is offered and the heat pump
has a buffer sensor (see flow_setpoint_raise_learning). The suggestion sensor shows its result, the
switch turns the automatic adjustment on and off.
"""
from __future__ import annotations

import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    FLOW_SETPOINT_RAISE_REGISTERS,
    HEAT_PUMP_STATUS_HEATING,
    HEAT_PUMP_STATUS_REGISTER,
    HEATING_CIRCUIT_1,
    HEATING_CIRCUITS,
    HeatingCircuit,
    RegisterDef,
    circuit_cooling,
    circuit_present,
    flow_setpoint_raise_bounds,
    flow_setpoint_raise_offered,
    flow_setpoint_raise_previous_key,
    heat_pump_status,
)
from .coordinator import HovalModbusCoordinator
from .flow_setpoint_raise import (
    AutoRaise,
    FlowSetpointRaiseAdvisor,
    Sample,
    Suggestion,
)

_LOGGER = logging.getLogger(__name__)

RAISE_REGISTER = FLOW_SETPOINT_RAISE_REGISTERS[0]

# Register keys the suggestion is worked out from, in the order of Sample's
# fields first. Listed as the listener's context so they are polled even while
# their own entities are disabled.
SAMPLE_KEYS = (
    "heat_generator_actual_temp",
    "heat_generator_target_temp",
    "flow_temp_hc1",
    "supply_target_temp_hc1",
    "buffer_temp",
    "mixing_valve_hc1",
)
POLLED_KEYS = (
    *SAMPLE_KEYS,
    "outdoor_temp",
    "modulation",
    HEAT_PUMP_STATUS_REGISTER.key,
    RAISE_REGISTER.key,
)

_STORE_VERSION = 1
_STORE_SAVE_DELAY = 300  # seconds


def suggestion_store(hass: HomeAssistant, entry_id: str) -> Store:
    """Where what the suggestion has learned is kept."""
    return Store(hass, _STORE_VERSION, f"{DOMAIN}.flow_setpoint_raise_{entry_id}")


class FlowSetpointRaiseControl:
    """Suggestion and automatic adjustment of the flow setpoint raise."""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: HovalModbusCoordinator,
        entry: ConfigEntry,
    ) -> None:
        self._hass = hass
        self._coordinator = coordinator
        self._entry = entry
        self._store = suggestion_store(hass, entry.entry_id)
        self._advisor = FlowSetpointRaiseAdvisor()
        self._auto_raise = AutoRaise()
        self._remove_listener: CALLBACK_TYPE | None = None
        self._writing = False
        self._write_task: asyncio.Task | None = None
        # Set while the raise is being restored, so nothing is written
        # in between.
        self._paused = False
        self.lower, self.upper = flow_setpoint_raise_bounds(
            entry.options, HEATING_CIRCUIT_1
        )
        self.suggestion: Suggestion | None = None
        # Set by the switch; off until it has been restored.
        self.auto: bool = False

    async def async_start(self) -> None:
        self._advisor = FlowSetpointRaiseAdvisor(await self._store.async_load())
        # Added before the platforms, so the suggestion is up to date when
        # the entities update.
        self._remove_listener = self._coordinator.async_add_listener(
            self._handle_update, context=list(POLLED_KEYS)
        )

    async def async_stop(self) -> None:
        if self._remove_listener is not None:
            self._remove_listener()
            self._remove_listener = None
        await self._store.async_save(self._advisor.to_store())

    @callback
    def _handle_update(self) -> None:
        # After a failed poll the data is the previous poll's; using it again
        # would count it twice.
        if not self._coordinator.last_update_success:
            return
        now = dt_util.utcnow()
        data = self._coordinator.data or {}
        values = [data.get(key) for key in SAMPLE_KEYS]
        sample = None if None in values else Sample(now, *values)
        current = data.get(RAISE_REGISTER.key)
        self.suggestion = self._advisor.update(
            now,
            heat_pump_status(
                self._entry.options, data.get(HEAT_PUMP_STATUS_REGISTER.key)
            )
            == HEAT_PUMP_STATUS_HEATING,
            data.get("outdoor_temp"),
            current,
            data.get("modulation"),
            sample,
        )
        self._store.async_delay_save(self._advisor.to_store, _STORE_SAVE_DELAY)

        if not self.auto or self._writing or self._paused:
            return
        value = self._auto_raise.next_value(
            now, self.suggestion, current, self.lower, self.upper
        )
        if value is not None:
            self._writing = True
            self._write_task = self._entry.async_create_background_task(
                self._hass, self._async_write(current, value), "flow setpoint raise"
            )

    async def async_pause(self) -> None:
        """Stops writing, after a write in progress has finished."""
        self._paused = True
        if self._write_task is not None:
            await asyncio.gather(self._write_task, return_exceptions=True)

    @callback
    def resume(self) -> None:
        self._paused = False

    async def _async_write(self, current: float | None, value: float) -> None:
        _LOGGER.debug(
            "Automatic flow setpoint raise: %s K -> %s K (%s)",
            current,
            value,
            self.suggestion.mode if self.suggestion else None,
        )
        try:
            await self._coordinator.async_write_value(RAISE_REGISTER, value)
        except UpdateFailed as err:
            _LOGGER.warning("Writing the flow setpoint raise failed: %s", err)
        finally:
            self._writing = False

    def attributes(self) -> dict[str, object]:
        return {"minimum": self.lower, "maximum": self.upper}


class CoolingRaiseGuard:
    """Puts the installer's raise back while a circuit cools.

    The raise register acts as a reduction while cooling, so a raise set by
    the integration would make the flow colder (condensation). Circuits on
    strategy 0/1 can cool weather compensated: the controller switches by the
    outdoor temperature, seen here in the circuit's status on the next poll.
    Then the raise remembered when the integration took it over
    (flow_setpoint_raise_previous_key) is written back, the automatic
    adjustment paused and the number entity refuses changes; once the circuit
    heats again, the raise from before cooling is written back.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: HovalModbusCoordinator,
        entry: ConfigEntry,
        control: FlowSetpointRaiseControl | None,
    ) -> None:
        self._hass = hass
        self._coordinator = coordinator
        self._entry = entry
        self._control = control
        self._circuits = [
            (hc, reg)
            for hc, reg in zip(HEATING_CIRCUITS, FLOW_SETPOINT_RAISE_REGISTERS)
            if flow_setpoint_raise_offered(entry.options, hc)
        ]
        # Keys of the circuits in cooling operation.
        self._cooling: set[str] = set()
        # Raise of each circuit before it started cooling.
        self._heating_raises: dict[str, float] = {}
        self._busy: set[str] = set()
        self._remove_listener: CALLBACK_TYPE | None = None

    @callback
    def async_start(self) -> None:
        context = [
            key for hc, reg in self._circuits for key in (hc.status_reg.key, reg.key)
        ]
        self._remove_listener = self._coordinator.async_add_listener(
            self._handle_update, context=context
        )

    @callback
    def async_stop(self) -> None:
        if self._remove_listener is not None:
            self._remove_listener()
            self._remove_listener = None

    def cooling(self, hc: HeatingCircuit) -> bool:
        """Whether the circuit's raise is held at the installer's value."""
        return hc.key in self._cooling

    def _installer_raise(self, hc: HeatingCircuit) -> float:
        # 0 if it couldn't be remembered: no reduction at all.
        return self._entry.options.get(flow_setpoint_raise_previous_key(hc), 0.0)

    @callback
    def _handle_update(self) -> None:
        if not self._coordinator.last_update_success:
            return
        data = self._coordinator.data or {}
        for hc, reg in self._circuits:
            status = data.get(hc.status_reg.key)
            if status is None or hc.key in self._busy:
                continue
            cooling = circuit_cooling(status)
            current = data.get(reg.key)
            if cooling and hc.key not in self._cooling:
                self._cooling.add(hc.key)
                if current is not None:
                    self._heating_raises[hc.key] = current
                self._run(hc, self._async_hold(hc, reg, pause=True))
            elif (
                cooling
                and current is not None
                and abs(current - self._installer_raise(hc)) >= reg.scale / 2
            ):
                # Changed while cooling, e.g. at the controller.
                self._run(hc, self._async_hold(hc, reg, pause=False))
            elif not cooling and hc.key in self._cooling:
                self._cooling.discard(hc.key)
                self._run(hc, self._async_release(hc, reg))

    def _run(self, hc: HeatingCircuit, coro) -> None:
        self._busy.add(hc.key)

        async def run() -> None:
            try:
                await coro
            except UpdateFailed as err:
                _LOGGER.warning(
                    "Could not set the flow setpoint raise of %s: %s", hc.key, err
                )
            finally:
                self._busy.discard(hc.key)

        self._entry.async_create_background_task(
            self._hass, run(), f"flow setpoint raise while cooling {hc.key}"
        )

    async def _async_hold(
        self, hc: HeatingCircuit, reg: RegisterDef, pause: bool
    ) -> None:
        if pause and hc is HEATING_CIRCUIT_1 and self._control is not None:
            await self._control.async_pause()
        value = self._installer_raise(hc)
        _LOGGER.info(
            "%s cools: flow setpoint raise set back to %s K", hc.key, value
        )
        await self._coordinator.async_write_value(reg, value, refresh=False)

    async def _async_release(self, hc: HeatingCircuit, reg: RegisterDef) -> None:
        value = self._heating_raises.pop(hc.key, None)
        if value is not None:
            _LOGGER.info("%s heats again: flow setpoint raise %s K", hc.key, value)
            await self._coordinator.async_write_value(reg, value, refresh=False)
        if hc is HEATING_CIRCUIT_1 and self._control is not None:
            self._control.resume()



async def async_sync_flow_setpoint_raise(
    coordinator: HovalModbusCoordinator,
    options: dict,
    control: FlowSetpointRaiseControl | None = None,
) -> None:
    """Brings the remembered raises in line with `options`, the options about
    to be stored.

    For each circuit whose raise the integration now provides, the raise on
    the controller is remembered, unless one is already. For each circuit
    whose raise it no longer provides, the remembered raise is written back,
    checked by reading it, and dropped. While heating circuit 1's is written
    back, its automatic adjustment (`control`) is paused, and resumed if this
    fails.

    Raises UpdateFailed; `options` may then be partly updated.
    """
    for hc, reg in zip(HEATING_CIRCUITS, FLOW_SETPOINT_RAISE_REGISTERS):
        key = flow_setpoint_raise_previous_key(hc)
        # A circuit that isn't there has nothing to write back to.
        if not circuit_present(options, hc):
            options.pop(key, None)
            continue
        if flow_setpoint_raise_offered(options, hc):
            if key not in options:
                options[key] = round(await coordinator.async_read_value(reg), 1)
            continue
        if key not in options:
            continue
        previous = options[key]
        paused = control if hc is HEATING_CIRCUIT_1 else None
        if paused is not None:
            await paused.async_pause()
        try:
            await coordinator.async_write_value(reg, previous, refresh=False)
            reported = await coordinator.async_read_value(reg)
            if abs(reported - previous) >= reg.scale / 2:
                raise UpdateFailed(
                    f"{reg.key} reads {reported} K instead of {previous} K"
                )
        except UpdateFailed:
            if paused is not None:
                paused.resume()
            raise
        del options[key]
