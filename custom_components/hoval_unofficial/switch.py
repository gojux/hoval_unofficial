"""Switch entities: the automatic adjustment of the flow setpoint raise."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_ON, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .coordinator import HovalModbusCoordinator
from .raise_control import FlowSetpointRaiseControl


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: HovalModbusCoordinator = hass.data[DOMAIN][entry.entry_id]
    if coordinator.flow_setpoint_raise is not None:
        coordinator.add_entities(
            async_add_entities,
            [HovalFlowSetpointRaiseAutoSwitch(entry, coordinator.flow_setpoint_raise)],
        )


class HovalFlowSetpointRaiseAutoSwitch(SwitchEntity, RestoreEntity):
    """Turns the automatic adjustment of heating circuit 1's flow setpoint
    raise on and off. Off when first created; the state is kept across
    restarts."""

    _attr_has_entity_name = True
    _attr_translation_key = "flow_setpoint_raise_auto_hc1"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, control: FlowSetpointRaiseControl) -> None:
        self._control = control
        self._attr_unique_id = f"{entry.entry_id}_flow_setpoint_raise_auto_hc1"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Hoval (Modbus)",
            model="Heat Pump Controller",
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            self._control.auto = last.state == STATE_ON

    async def async_will_remove_from_hass(self) -> None:
        self._control.auto = False
        await super().async_will_remove_from_hass()

    @property
    def is_on(self) -> bool:
        return self._control.auto

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._control.attributes()

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._control.auto = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._control.auto = False
        self.async_write_ha_state()
