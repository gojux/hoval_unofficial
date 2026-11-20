"""Number entities for writable Modbus registers (e.g. setpoints)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberMode,
    RestoreNumber,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONTROL_STRATEGIES_COOLING,
    COOLING_FLOW_DEFAULT,
    DOMAIN,
    FLOW_SETPOINT_RAISE_REGISTERS,
    HEATING_CIRCUITS,
    NUMBER_REGISTERS,
    HeatingCircuit,
    RegisterDef,
    cooling_enabled,
    cooling_flow_range,
    SMART_GRID_BUFFER_OFFSET_REGISTERS,
    SMART_GRID_COOLING_OFFSET_REGISTERS,
    SMART_GRID_HEATING_OFFSET_REGISTERS,
    SMART_GRID_OFFSET_DHW_REGISTER,
    dhw_present,
    present_circuits,
    buffer_module_translation,
    found_buffer_modules,
    cooling_supported_key,
    flow_setpoint_raise_bounds,
    flow_setpoint_raise_offered,
)
from .constant_control import ConstantFlowControl
from .coordinator import HovalModbusCoordinator

_LOGGER = logging.getLogger(__name__)

# A written value is shown right away and kept until the controller reports
# it, at most this long (see HovalNumber).
CHANGE_CONFIRMATION_TIMEOUT = timedelta(seconds=90)
ATTR_CHANGE_PENDING = "change_pending"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: HovalModbusCoordinator = hass.data[DOMAIN][entry.entry_id]

    options = entry.options
    # Registers only offered under conditions, see below.
    circuits = present_circuits(options)
    conditional = [
        *FLOW_SETPOINT_RAISE_REGISTERS,
        *SMART_GRID_HEATING_OFFSET_REGISTERS.values(),
        *SMART_GRID_COOLING_OFFSET_REGISTERS.values(),
        *(reg for regs in SMART_GRID_BUFFER_OFFSET_REGISTERS.values() for reg, _ in regs),
        SMART_GRID_OFFSET_DHW_REGISTER,
    ]
    entities = [
        HovalNumber(coordinator, entry, reg)
        for reg in NUMBER_REGISTERS
        if reg.writable and reg not in conditional
    ]
    entities.extend(
        HovalNumber(coordinator, entry, SMART_GRID_HEATING_OFFSET_REGISTERS[hc.key])
        for hc in circuits
    )
    if dhw_present(options):
        entities.append(HovalNumber(coordinator, entry, SMART_GRID_OFFSET_DHW_REGISTER))
    entities.extend(
        HovalNumber(
            coordinator,
            entry,
            reg,
            translation=buffer_module_translation(options, unit_id, key),
        )
        for unit_id in found_buffer_modules(options)
        for reg, key in SMART_GRID_BUFFER_OFFSET_REGISTERS[unit_id]
    )
    entities.extend(
        HovalNumber(coordinator, entry, SMART_GRID_COOLING_OFFSET_REGISTERS[hc.key])
        for hc in circuits
    )
    # The raise is an experimental function, offered per circuit, within the
    # circuit's bounds; enabled like the circuit's other entities.
    entities.extend(
        HovalNumber(
            coordinator,
            entry,
            reg,
            enabled=True,
            bounds=flow_setpoint_raise_bounds(options, hc),
            raise_circuit=hc,
        )
        for hc, reg in zip(HEATING_CIRCUITS, FLOW_SETPOINT_RAISE_REGISTERS)
        if flow_setpoint_raise_offered(options, hc)
    )
    for control in coordinator.constant_controls.values():
        entities.append(HovalConstantFlowNumber(coordinator, entry, control, False))
        if control.cooling_offered:
            entities.append(HovalConstantFlowNumber(coordinator, entry, control, True))
    entities.extend(
        HovalCoolingFlowSetpointNumber(coordinator, entry, hc)
        for hc in circuits
        if cooling_enabled(entry.options)
        and entry.options.get(cooling_supported_key(hc))
    )
    coordinator.add_entities(async_add_entities, entities)


class HovalNumber(CoordinatorEntity[HovalModbusCoordinator], NumberEntity):
    """A writable Modbus value (e.g. a setpoint), exposed as a HA number entity.

    A value written from Home Assistant is shown right away, before the
    controller reports it on the next poll; the change_pending attribute is
    true until then. If the controller doesn't report it within
    CHANGE_CONFIRMATION_TIMEOUT, the value it reports is shown again.
    """

    _attr_has_entity_name = True
    # An input box instead of a slider: offsets need exact values, and every
    # slider release would write to the controller right away.
    _attr_mode = NumberMode.BOX

    def __init__(
        self,
        coordinator: HovalModbusCoordinator,
        entry: ConfigEntry,
        reg: RegisterDef,
        enabled: bool | None = None,
        bounds: tuple[float, float] | None = None,
        translation: tuple[str, dict[str, str]] | None = None,
        raise_circuit: HeatingCircuit | None = None,
    ) -> None:
        super().__init__(coordinator, context=[reg.key])
        self._reg = reg
        # The circuit of a flow setpoint raise, held while it cools (see
        # CoolingRaiseGuard).
        self._raise_circuit = raise_circuit
        self._pending: float | None = None
        self._pending_since: datetime | None = None
        # Half a step of the register: values closer than this are the same
        # once written and read back.
        self._epsilon = (reg.scale or 1) / 2
        self._attr_unique_id = f"{entry.entry_id}_{reg.key}"
        # The register's key, or a translation key with placeholders.
        if translation is None:
            self._attr_translation_key = reg.key
        else:
            self._attr_translation_key, self._attr_translation_placeholders = translation
        self._attr_native_unit_of_measurement = reg.unit
        self._attr_native_min_value = 0 if reg.min_value is None else reg.min_value
        self._attr_native_max_value = 100 if reg.max_value is None else reg.max_value
        if bounds is not None:
            self._attr_native_min_value, self._attr_native_max_value = bounds
        self._attr_native_step = 1 if reg.step is None else reg.step
        self._attr_entity_registry_enabled_default = (
            reg.enabled_default if enabled is None else enabled
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Hoval (Modbus)",
            model="Heat Pump Controller",
        )

    @property
    def native_value(self) -> float | None:
        self._expire_pending()
        if self._pending is not None:
            return self._pending
        return self.coordinator.data.get(self._reg.key)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {ATTR_CHANGE_PENDING: self._pending is not None}

    @callback
    def _handle_coordinator_update(self) -> None:
        reported = self.coordinator.data.get(self._reg.key)
        if (
            self._pending is not None
            and reported is not None
            and abs(reported - self._pending) < self._epsilon
        ):
            self._pending = None
            self._pending_since = None
        self._expire_pending()
        super()._handle_coordinator_update()

    def _expire_pending(self) -> None:
        if (
            self._pending is not None
            and self._pending_since is not None
            and dt_util.utcnow() - self._pending_since > CHANGE_CONFIRMATION_TIMEOUT
        ):
            _LOGGER.warning(
                "%s: the controller didn't confirm %s within %s, showing the "
                "value it reports instead",
                self._reg.key,
                self._pending,
                CHANGE_CONFIRMATION_TIMEOUT,
            )
            self._pending = None
            self._pending_since = None

    async def async_set_native_value(self, value: float) -> None:
        guard = self.coordinator.raise_guard
        if (
            self._raise_circuit is not None
            and guard is not None
            and guard.cooling(self._raise_circuit)
        ):
            raise ServiceValidationError(
                f"{self._raise_circuit.key} cools: its flow setpoint raise stays "
                "at the installer's value until it heats again."
            )
        self._pending = value
        self._pending_since = dt_util.utcnow()
        self.async_write_ha_state()
        try:
            await self.coordinator.async_write_value(self._reg, value)
        except UpdateFailed:
            self._pending = None
            self._pending_since = None
            self.async_write_ha_state()
            raise


class HovalCoolingFlowSetpointNumber(
    CoordinatorEntity[HovalModbusCoordinator], RestoreNumber
):
    """The flow temperature a heating circuit cools with.

    The controller's cooling flow setpoint register holds 0 while the circuit
    doesn't cool, so the chosen value is kept by Home Assistant and written
    when the circuit is set to cool (see climate.py), or right away while it
    already cools.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "cooling_flow_setpoint"
    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_native_step = 1

    def __init__(
        self,
        coordinator: HovalModbusCoordinator,
        entry: ConfigEntry,
        heating_circuit: HeatingCircuit,
    ) -> None:
        reg = heating_circuit.cooling_flow_setpoint_reg
        super().__init__(coordinator, context=[reg.key])
        self._hc = heating_circuit
        self._reg = reg
        self._attr_unique_id = f"{entry.entry_id}_{reg.key}"
        self._attr_translation_placeholders = {
            "circuit": heating_circuit.key.removeprefix("heating_circuit_")
        }
        self._attr_native_min_value, self._attr_native_max_value = (
            cooling_flow_range(heating_circuit, entry.options)
        )
        self._attr_native_value = self._clamp(COOLING_FLOW_DEFAULT)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Hoval (Modbus)",
            model="Heat Pump Controller",
        )

    def _clamp(self, value: float) -> float:
        return min(max(value, self._attr_native_min_value), self._attr_native_max_value)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_number_data()) is not None:
            if last.native_value is not None:
                self._attr_native_value = self._clamp(last.native_value)
        self.coordinator.cooling_flow_setpoints[self._hc.key] = self._attr_native_value

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        self.coordinator.cooling_flow_setpoints[self._hc.key] = value
        self.async_write_ha_state()

        if not self.coordinator.data.get(self._reg.key):
            return
        strategy = int(
            await self.coordinator.async_read_value(self._hc.control_strategy_reg)
        )
        if strategy not in CONTROL_STRATEGIES_COOLING:
            raise ServiceValidationError(
                f"{self._hc.key} no longer has a control strategy with cooling "
                f"({strategy}); save the heating circuit in the integration's "
                "options again."
            )
        await self.coordinator.async_write_value(self._reg, value)


class HovalConstantFlowNumber(
    CoordinatorEntity[HovalModbusCoordinator], RestoreNumber
):
    """A flow setpoint of a heating circuit on constant control, for heating
    or for cooling.

    Written only while the circuit is in that mode; otherwise only kept, and
    written when the mode is chosen (see constant_control.py). A setpoint
    changed on the controller while its mode is active is taken over.
    """

    _attr_has_entity_name = True
    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_native_step = 0.5
    _attr_mode = NumberMode.BOX

    def __init__(
        self,
        coordinator: HovalModbusCoordinator,
        entry: ConfigEntry,
        control: ConstantFlowControl,
        cooling: bool,
    ) -> None:
        super().__init__(coordinator, context=control.keys)
        self._control = control
        self._cooling = cooling
        name = "constant_cooling_flow" if cooling else "constant_heating_flow"
        self._attr_translation_key = name
        self._attr_unique_id = f"{entry.entry_id}_{name}_{control.hc.key}"
        self._attr_translation_placeholders = {
            "circuit": control.hc.key.removeprefix("heating_circuit_")
        }
        self._attr_native_min_value, self._attr_native_max_value = (
            control.cooling_range if cooling else control.heating_range
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Hoval (Modbus)",
            model="Heat Pump Controller",
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_number_data()) is not None:
            if last.native_value is not None:
                if self._cooling:
                    self._control.restore_cooling(last.native_value)
                else:
                    self._control.restore_heating(last.native_value)
        self.async_on_remove(
            self._control.async_add_update_callback(self.async_write_ha_state)
        )

    @property
    def native_value(self) -> float:
        return self._control.cooling if self._cooling else self._control.heating

    async def async_set_native_value(self, value: float) -> None:
        if self._cooling:
            await self._control.async_set_cooling(value)
        else:
            await self._control.async_set_heating(value)
