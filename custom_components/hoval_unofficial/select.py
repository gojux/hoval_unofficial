"""Select entities for Modbus registers with a fixed set of raw values."""
from __future__ import annotations

from typing import Any

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
from .constant_control import ConstantFlowControl
from .coordinator import HovalModbusCoordinator

ATTR_CHANGE_PENDING = "change_pending"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: HovalModbusCoordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator.add_entities(
        async_add_entities,
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
            *(
                HovalConstantModeSelect(coordinator, entry, control)
                for control in coordinator.constant_controls.values()
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


class HovalConstantModeSelect(CoordinatorEntity[HovalModbusCoordinator], SelectEntity):
    """Heating / off / cooling of a heating circuit on constant control.

    The mode chosen is shown right away; switching takes a while (see
    constant_control.py), and the change_pending attribute is true until it
    is done.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "constant_mode"

    def __init__(
        self,
        coordinator: HovalModbusCoordinator,
        entry: ConfigEntry,
        control: ConstantFlowControl,
    ) -> None:
        super().__init__(coordinator, context=control.keys)
        self._control = control
        self._attr_options = control.modes
        self._attr_unique_id = f"{entry.entry_id}_constant_mode_{control.hc.key}"
        self._attr_translation_placeholders = {
            "circuit": control.hc.key.removeprefix("heating_circuit_")
        }
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Hoval (Modbus)",
            model="Heat Pump Controller",
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self._control.async_add_update_callback(self.async_write_ha_state)
        )

    @property
    def current_option(self) -> str | None:
        mode = self._control.pending_mode or self._control.mode()
        return mode if mode in self._attr_options else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {ATTR_CHANGE_PENDING: self._control.pending_mode is not None}

    async def async_select_option(self, option: str) -> None:
        await self._control.async_set_mode(option)
