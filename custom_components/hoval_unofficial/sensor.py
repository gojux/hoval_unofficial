"""Sensor entities for the Heat Pump Modbus integration."""
from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    SENSOR_REGISTERS,
    RegisterDef,
    HEAT_PUMP_STATUS_MAP,
    HEAT_PUMP_STATUS_OPTIONS,
    HEAT_PUMP_STATUS_REGISTER,
    HEAT_PUMP_STATUS_UNKNOWN,
)
from .coordinator import HovalModbusCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: HovalModbusCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[SensorEntity] = [
        HovalSensor(coordinator, entry, reg) for reg in SENSOR_REGISTERS
    ]
    entities.append(HovalStatusSensor(coordinator, entry))
    async_add_entities(entities)


class HovalSensor(CoordinatorEntity[HovalModbusCoordinator], SensorEntity):
    """A single Modbus value exposed as a HA sensor."""

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
        self._attr_device_class = reg.device_class
        self._attr_state_class = reg.state_class
        self._attr_icon = reg.icon
        if reg.precision is not None:
            self._attr_suggested_display_precision = reg.precision
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

class HovalStatusSensor(
    CoordinatorEntity[HovalModbusCoordinator], SensorEntity
):
    """Translates the heat pump's raw status code into a stable, localized text.

    Uses SensorDeviceClass.ENUM: Home Assistant requires a fixed `options`
    list of every possible state value for this device class, and raises an
    error if the returned value isn't one of them -- that's why
    HEAT_PUMP_STATUS_UNKNOWN is included as a fallback for undocumented
    codes.

    The entity's state is a stable, machine-readable key (e.g. "heating_mode"),
    not display text -- the actual English/German label is resolved by Home
    Assistant via `_attr_translation_key` from strings.json /
    translations/de.json (entity.sensor.hp_status.state.*), matching the
    heat pump's own status text in both languages.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "hp_status"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = HEAT_PUMP_STATUS_OPTIONS

    def __init__(
        self, coordinator: HovalModbusCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, context=[HEAT_PUMP_STATUS_REGISTER.key])
        self._attr_unique_id = f"{entry.entry_id}_heat_pump_status"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Hoval (Modbus)",
            model="Heat Pump Controller",
        )

    @property
    def native_value(self) -> str | None:
        raw_code = self.coordinator.data.get(HEAT_PUMP_STATUS_REGISTER.key)
        if raw_code is None:
            return None
        return HEAT_PUMP_STATUS_MAP.get(int(raw_code), HEAT_PUMP_STATUS_UNKNOWN)

    @property
    def extra_state_attributes(self) -> dict[str, int] | None:
        """Expose the raw numeric status code, e.g. for debugging or
        undocumented codes that fall back to HEAT_PUMP_STATUS_UNKNOWN."""
        raw_code = self.coordinator.data.get(HEAT_PUMP_STATUS_REGISTER.key)
        if raw_code is None:
            return None
        return {"raw_status_code": int(raw_code)}
