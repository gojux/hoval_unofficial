"""Select entities for Modbus registers with a fixed set of raw values."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    SMART_GRID_CONTROL_MAP,
    SMART_GRID_CONTROL_OPTION_TO_VALUE,
    SMART_GRID_CONTROL_OPTIONS,
    SMART_GRID_CONTROL_REGISTER,
)
from .coordinator import HovalModbusCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: HovalModbusCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([HovalSmartGridControlSelect(coordinator, entry)])


class HovalSmartGridControlSelect(
    CoordinatorEntity[HovalModbusCoordinator], SelectEntity
):
    """Selects which input activates the controller's Smart Grid mode."""

    _attr_has_entity_name = True
    _attr_translation_key = "smart_grid_control"
    _attr_options = SMART_GRID_CONTROL_OPTIONS

    def __init__(
        self, coordinator: HovalModbusCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, context=[SMART_GRID_CONTROL_REGISTER.key])
        self._attr_unique_id = f"{entry.entry_id}_smart_grid_control"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Hoval (Modbus)",
            model="Heat Pump Controller",
        )

    @property
    def current_option(self) -> str | None:
        raw_value = self.coordinator.data.get(SMART_GRID_CONTROL_REGISTER.key)
        if raw_value is None:
            return None
        return SMART_GRID_CONTROL_MAP.get(int(raw_value))

    async def async_select_option(self, option: str) -> None:
        raw_value = SMART_GRID_CONTROL_OPTION_TO_VALUE[option]
        await self.coordinator.async_write_value(SMART_GRID_CONTROL_REGISTER, raw_value)
