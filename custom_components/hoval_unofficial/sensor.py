"""Sensor entities for the Heat Pump Modbus integration."""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    BUFFER_MODULE_SENSOR_REGISTERS,
    BUFFER_MODULE_TEMP_REGISTERS,
    BUFFER_SENSOR_REGISTER,
    CONF_BUFFER_SENSOR,
    CIRCUIT_SENSOR_KEYS,
    COOLING_SENSOR_KEYS,
    CONTROLLER_ERROR_MAP,
    CONTROLLER_ERROR_OPTIONS,
    CONTROLLER_ERROR_REGISTER,
    CONTROLLER_ERROR_UNKNOWN,
    CONTROL_STRATEGY_MAP,
    CONTROL_STRATEGY_OPTIONS,
    CONTROL_STRATEGY_UNKNOWN,
    DOMAIN,
    HEATING_CIRCUIT_STATUS_MAP,
    HEATING_CIRCUIT_STATUS_OPTIONS,
    HEATING_CIRCUIT_STATUS_UNKNOWN,
    HEATING_CIRCUITS,
    HeatingCircuit,
    SENSOR_REGISTERS,
    RegisterDef,
    HEAT_PUMP_STATUS_MAP,
    HEAT_PUMP_STATUS_OPTIONS,
    HEAT_PUMP_STATUS_REGISTER,
    HEAT_PUMP_STATUS_UNKNOWN,
    SMART_GRID_STATUS_MAP,
    SMART_GRID_STATUS_OPTIONS,
    SMART_GRID_STATUS_REGISTER,
    SMART_GRID_STATUS_UNKNOWN,
    buffer_module_translation,
    buffer_sensor_found_key,
    found_buffer_modules,
    cooling_enabled,
    present_circuits,
)
from .coordinator import HovalModbusCoordinator
from .raise_control import POLLED_KEYS, FlowSetpointRaiseControl


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: HovalModbusCoordinator = hass.data[DOMAIN][entry.entry_id]
    circuits = present_circuits(entry.options)
    # The sensors of circuits that aren't there.
    absent = {
        key
        for hc in HEATING_CIRCUITS
        if hc not in circuits
        for key in CIRCUIT_SENSOR_KEYS[hc.key]
    }
    buffer_sensor = bool(entry.options.get(CONF_BUFFER_SENSOR))
    if not buffer_sensor:
        absent.add(BUFFER_SENSOR_REGISTER.key)

    cooling = cooling_enabled(entry.options)
    entities: list[SensorEntity] = [
        HovalSensor(
            coordinator,
            entry,
            reg,
            enabled=(
                True
                if (cooling and reg.key in COOLING_SENSOR_KEYS)
                or reg is BUFFER_SENSOR_REGISTER
                else None
            ),
        )
        for reg in SENSOR_REGISTERS
        if reg.key not in absent
    ]
    # All sensors of a found module; a buffer sensor without a plausible
    # value is offered disabled, so it can still be enabled.
    entities.extend(
        HovalSensor(
            coordinator,
            entry,
            reg,
            enabled=(
                reg not in BUFFER_MODULE_TEMP_REGISTERS[unit_id]
                or bool(entry.options.get(buffer_sensor_found_key(reg)))
            ),
            translation=buffer_module_translation(entry.options, unit_id, key),
        )
        for unit_id in found_buffer_modules(entry.options)
        for reg, key in BUFFER_MODULE_SENSOR_REGISTERS[unit_id]
    )
    entities.append(HovalStatusSensor(coordinator, entry))
    entities.append(HovalControllerErrorSensor(coordinator, entry))
    entities.append(HovalSmartGridStatusSensor(coordinator, entry))
    entities.extend(
        HovalControlStrategySensor(coordinator, entry, hc) for hc in circuits
    )
    entities.extend(
        HovalHeatingCircuitStatusSensor(coordinator, entry, hc) for hc in circuits
    )
    if coordinator.flow_setpoint_raise is not None:
        entities.append(
            HovalFlowSetpointRaiseSuggestionSensor(
                coordinator, entry, coordinator.flow_setpoint_raise
            )
        )
    coordinator.add_entities(async_add_entities, entities)


class HovalSensor(CoordinatorEntity[HovalModbusCoordinator], SensorEntity):
    """A single Modbus value exposed as a HA sensor."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: HovalModbusCoordinator,
        entry: ConfigEntry,
        reg: RegisterDef,
        enabled: bool | None = None,
        translation: tuple[str, dict[str, str]] | None = None,
    ) -> None:
        super().__init__(coordinator, context=[reg.key])
        self._reg = reg
        self._attr_unique_id = f"{entry.entry_id}_{reg.key}"
        # The register's key, or a translation key with placeholders.
        if translation is None:
            self._attr_translation_key = reg.key
        else:
            self._attr_translation_key, self._attr_translation_placeholders = translation
        self._attr_native_unit_of_measurement = reg.unit
        self._attr_device_class = reg.device_class
        self._attr_state_class = reg.state_class
        self._attr_icon = reg.icon
        if reg.precision is not None:
            self._attr_suggested_display_precision = reg.precision
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


class HovalControllerErrorSensor(
    CoordinatorEntity[HovalModbusCoordinator], SensorEntity
):
    """The controller's error code as a translated enum: "no error" for 255,
    the warning number (e.g. W11) for listed codes, and "unknown error" for
    codes that aren't documented. The raw code is exposed as an attribute."""

    _attr_has_entity_name = True
    _attr_translation_key = CONTROLLER_ERROR_REGISTER.key
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = CONTROLLER_ERROR_OPTIONS

    def __init__(
        self, coordinator: HovalModbusCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, context=[CONTROLLER_ERROR_REGISTER.key])
        self._attr_unique_id = f"{entry.entry_id}_{CONTROLLER_ERROR_REGISTER.key}"
        self._attr_entity_registry_enabled_default = CONTROLLER_ERROR_REGISTER.enabled_default
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Hoval (Modbus)",
            model="Heat Pump Controller",
        )

    @property
    def native_value(self) -> str | None:
        raw_code = self.coordinator.data.get(CONTROLLER_ERROR_REGISTER.key)
        if raw_code is None:
            return None
        return CONTROLLER_ERROR_MAP.get(int(raw_code), CONTROLLER_ERROR_UNKNOWN)

    @property
    def extra_state_attributes(self) -> dict[str, int] | None:
        raw_code = self.coordinator.data.get(CONTROLLER_ERROR_REGISTER.key)
        if raw_code is None:
            return None
        return {"raw_error_code": int(raw_code)}


class HovalSmartGridStatusSensor(
    CoordinatorEntity[HovalModbusCoordinator], SensorEntity
):
    """Smart Grid state reported by the controller, as a translated enum.

    Disabled by default like the other Smart Grid entities. Raw values
    outside SMART_GRID_STATUS_MAP are reported as SMART_GRID_STATUS_UNKNOWN.
    """

    _attr_has_entity_name = True
    _attr_translation_key = SMART_GRID_STATUS_REGISTER.key
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = SMART_GRID_STATUS_OPTIONS

    def __init__(
        self, coordinator: HovalModbusCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, context=[SMART_GRID_STATUS_REGISTER.key])
        self._attr_unique_id = f"{entry.entry_id}_{SMART_GRID_STATUS_REGISTER.key}"
        self._attr_entity_registry_enabled_default = SMART_GRID_STATUS_REGISTER.enabled_default
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Hoval (Modbus)",
            model="Heat Pump Controller",
        )

    @property
    def native_value(self) -> str | None:
        raw_value = self.coordinator.data.get(SMART_GRID_STATUS_REGISTER.key)
        if raw_value is None:
            return None
        return SMART_GRID_STATUS_MAP.get(int(raw_value), SMART_GRID_STATUS_UNKNOWN)


class HovalControlStrategySensor(
    CoordinatorEntity[HovalModbusCoordinator], SensorEntity
):
    """A heating circuit's control strategy, as a translated diagnostic enum.

    Disabled by default. Raw values outside CONTROL_STRATEGY_MAP are
    reported as CONTROL_STRATEGY_UNKNOWN.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "control_strategy"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = CONTROL_STRATEGY_OPTIONS
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: HovalModbusCoordinator,
        entry: ConfigEntry,
        heating_circuit: HeatingCircuit,
    ) -> None:
        reg = heating_circuit.control_strategy_reg
        super().__init__(coordinator, context=[reg.key])
        self._reg = reg
        self._attr_unique_id = f"{entry.entry_id}_{reg.key}"
        # "heating_circuit_1" -> "1", used in the translated entity name.
        self._attr_translation_placeholders = {
            "circuit": heating_circuit.key.removeprefix("heating_circuit_")
        }
        self._attr_entity_registry_enabled_default = reg.enabled_default
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Hoval (Modbus)",
            model="Heat Pump Controller",
        )

    @property
    def native_value(self) -> str | None:
        raw_value = self.coordinator.data.get(self._reg.key)
        if raw_value is None:
            return None
        return CONTROL_STRATEGY_MAP.get(int(raw_value), CONTROL_STRATEGY_UNKNOWN)


class HovalHeatingCircuitStatusSensor(
    CoordinatorEntity[HovalModbusCoordinator], SensorEntity
):
    """A heating circuit's control status (e.g. normal heating, frost
    protection, Smart Grid preferred operation), as a translated diagnostic
    enum. Disabled by default. Raw values outside HEATING_CIRCUIT_STATUS_MAP
    are reported as HEATING_CIRCUIT_STATUS_UNKNOWN; the raw value is always
    exposed as an attribute."""

    _attr_has_entity_name = True
    _attr_translation_key = "heating_circuit_status"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = HEATING_CIRCUIT_STATUS_OPTIONS
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: HovalModbusCoordinator,
        entry: ConfigEntry,
        heating_circuit: HeatingCircuit,
    ) -> None:
        reg = heating_circuit.status_reg
        super().__init__(coordinator, context=[reg.key])
        self._reg = reg
        self._attr_unique_id = f"{entry.entry_id}_{reg.key}"
        self._attr_translation_placeholders = {
            "circuit": heating_circuit.key.removeprefix("heating_circuit_")
        }
        self._attr_entity_registry_enabled_default = reg.enabled_default
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Hoval (Modbus)",
            model="Heat Pump Controller",
        )

    @property
    def native_value(self) -> str | None:
        raw_value = self.coordinator.data.get(self._reg.key)
        if raw_value is None:
            return None
        return HEATING_CIRCUIT_STATUS_MAP.get(
            int(raw_value), HEATING_CIRCUIT_STATUS_UNKNOWN
        )

    @property
    def extra_state_attributes(self) -> dict[str, int] | None:
        raw_value = self.coordinator.data.get(self._reg.key)
        if raw_value is None:
            return None
        return {"raw_status_code": int(raw_value)}


class HovalFlowSetpointRaiseSuggestionSensor(
    CoordinatorEntity[HovalModbusCoordinator], SensorEntity
):
    """The flow setpoint raise of heating circuit 1 the integration would
    choose (see flow_setpoint_raise.py), worked out by
    FlowSetpointRaiseControl on every poll. Written to the controller only
    while the automatic adjustment is on."""

    _attr_has_entity_name = True
    _attr_translation_key = "flow_setpoint_raise_suggestion_hc1"
    _attr_native_unit_of_measurement = "K"
    _attr_state_class = "measurement"
    _attr_suggested_display_precision = 1
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: HovalModbusCoordinator,
        entry: ConfigEntry,
        control: FlowSetpointRaiseControl,
    ) -> None:
        super().__init__(coordinator, context=list(POLLED_KEYS))
        self._control = control
        self._attr_unique_id = f"{entry.entry_id}_flow_setpoint_raise_suggestion_hc1"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Hoval (Modbus)",
            model="Heat Pump Controller",
        )

    @property
    def native_value(self) -> float | None:
        suggestion = self._control.suggestion
        return None if suggestion is None else suggestion.value

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        suggestion = self._control.suggestion
        if suggestion is None:
            return None
        return {"mode": suggestion.mode, **suggestion.attributes}
