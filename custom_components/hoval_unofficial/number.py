"""Number entities for writable Modbus registers (e.g. setpoints)."""
from __future__ import annotations

from homeassistant.components.number import NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, NUMBER_REGISTERS, RegisterDef
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
