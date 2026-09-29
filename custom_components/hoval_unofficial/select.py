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
    RegisterDef,
    SMART_GRID_MODE_MAP,
    SMART_GRID_MODE_OPTION_TO_VALUE,
    SMART_GRID_MODE_OPTIONS,
    SMART_GRID_MODE_REGISTER,
    SMART_GRID_TRIGGER_MAP,
    SMART_GRID_TRIGGER_OPTION_TO_VALUE,
    SMART_GRID_TRIGGER_OPTIONS,
    SMART_GRID_TRIGGER_REGISTER,
)
from .coordinator import HovalModbusCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: HovalModbusCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            HovalMappedSelect(
                coordinator,
                entry,
                reg=SMART_GRID_TRIGGER_REGISTER,
                value_map=SMART_GRID_TRIGGER_MAP,
                option_to_value=SMART_GRID_TRIGGER_OPTION_TO_VALUE,
                options=SMART_GRID_TRIGGER_OPTIONS,
            ),
            HovalMappedSelect(
                coordinator,
                entry,
                reg=SMART_GRID_MODE_REGISTER,
                value_map=SMART_GRID_MODE_MAP,
                option_to_value=SMART_GRID_MODE_OPTION_TO_VALUE,
                options=SMART_GRID_MODE_OPTIONS,
            ),
        ]
    )


class HovalMappedSelect(CoordinatorEntity[HovalModbusCoordinator], SelectEntity):
    """A writable Modbus register with a fixed set of raw values, exposed as
    a HA select entity via a raw-value <-> option-key mapping."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: HovalModbusCoordinator,
        entry: ConfigEntry,
        reg: RegisterDef,
        value_map: dict[int, str],
        option_to_value: dict[str, int],
        options: list[str],
    ) -> None:
        super().__init__(coordinator, context=[reg.key])
        self._reg = reg
        self._value_map = value_map
        self._option_to_value = option_to_value
        self._attr_translation_key = reg.key
        self._attr_options = options
        self._attr_unique_id = f"{entry.entry_id}_{reg.key}"
        self._attr_entity_registry_enabled_default = reg.enabled_default
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Hoval (Modbus)",
            model="Heat Pump Controller",
        )

    @property
    def current_option(self) -> str | None:
        raw_value = self.coordinator.data.get(self._reg.key)
        if raw_value is None:
            return None
        return self._value_map.get(int(raw_value))

    async def async_select_option(self, option: str) -> None:
        raw_value = self._option_to_value[option]
        await self.coordinator.async_write_value(self._reg, raw_value)
