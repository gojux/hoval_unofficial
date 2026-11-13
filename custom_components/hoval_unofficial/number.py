"""Number entities for writable Modbus registers (e.g. setpoints)."""
from __future__ import annotations

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    RestoreNumber,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONTROL_STRATEGIES_COOLING,
    COOLING_FLOW_DEFAULT,
    DOMAIN,
    HEATING_CIRCUITS,
    NUMBER_REGISTERS,
    HeatingCircuit,
    RegisterDef,
    cooling_flow_range,
    cooling_supported_key,
)
from .coordinator import HovalModbusCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: HovalModbusCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = [
        HovalNumber(coordinator, entry, reg)
        for reg in NUMBER_REGISTERS
        if reg.writable
    ]
    entities.extend(
        HovalCoolingFlowSetpointNumber(coordinator, entry, hc)
        for hc in HEATING_CIRCUITS
        if hc.cooling_flow_setpoint_reg is not None
        and entry.options.get(cooling_supported_key(hc))
    )
    async_add_entities(entities)


class HovalNumber(CoordinatorEntity[HovalModbusCoordinator], NumberEntity):
    """A writable Modbus value (e.g. a setpoint), exposed as a HA number entity."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: HovalModbusCoordinator,
        entry: ConfigEntry,
        reg: RegisterDef,
    ) -> None:
        super().__init__(coordinator, context=[reg.key])
        self._reg = reg
        self._attr_unique_id = f"{entry.entry_id}_{reg.key}"
        self._attr_translation_key = reg.key
        self._attr_native_unit_of_measurement = reg.unit
        self._attr_native_min_value = reg.min_value or 0
        self._attr_native_max_value = reg.max_value or 100
        self._attr_native_step = reg.step or 1
        self._attr_entity_registry_enabled_default = reg.enabled_default
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Hoval (Modbus)",
            model="Heat Pump Controller",
        )

    @property
    def native_value(self) -> float | None:
        return self.coordinator.data.get(self._reg.key)

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_write_value(self._reg, value)


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
