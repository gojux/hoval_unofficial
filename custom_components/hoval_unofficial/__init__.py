"""Hoval Heat Pump Integration (unofficial)."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_HOST,
    CONF_NAME,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    CONF_TIMEOUT,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed

from .const import (
    CIRCUIT_FLOW_TEMP_REGISTERS,
    COOLING_SENSOR_KEYS,
    CONF_DHW_PRESENT,
    DHW_CURRENT_TEMP_REGISTER,
    HEATING_CIRCUIT_1,
    circuit_present_key,
    present_circuits,
    BUFFER_MODULE_TEMP_REGISTERS,
    BUFFER_SENSOR_REGISTER,
    CONF_BUFFER_MODULE,
    CONF_BUFFER_SENSOR,
    CONF_DEVICE_ID,
    CONTROL_STRATEGIES_COOLING,
    CONTROL_STRATEGY_CONSTANT,
    CURRENT_TEMP_SOURCE_ENTITY_WRITE,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TIMEOUT,
    DOMAIN,
    HEATING_CIRCUITS,
    HeatingCircuit,
    RegisterDef,
    buffer_module_key,
    buffer_module_temp_plausible,
    constant_control_enabled,
    cooling_enabled,
    constant_control_key,
    control_strategy_key,
    cooling_flow_min_key,
    cooling_supported_key,
    current_temp_entity_key,
    current_temp_source_key,
    flow_max_key,
    flow_setpoint_raise_learning,
)
from .constant_control import ConstantFlowControl
from .coordinator import HovalModbusCoordinator
from .raise_control import (
    FlowSetpointRaiseControl,
    async_sync_flow_setpoint_raise,
    suggestion_store,
)
from .room_temp import RoomTempWriter, async_cleanup_removed_entry

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.WATER_HEATER,
    Platform.CLIMATE,
    Platform.SWITCH,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Sets up the integration from a config entry."""
    name = entry.data.get(CONF_NAME, entry.title)
    host = entry.data[CONF_HOST]
    port = entry.data[CONF_PORT]
    device_id = entry.data[CONF_DEVICE_ID]
    scan_interval = entry.options.get(
        CONF_SCAN_INTERVAL, entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    )

    coordinator = HovalModbusCoordinator(
        hass,
        entry,
        name,
        host,
        port,
        device_id,
        scan_interval,
        entry.options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT),
    )
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    # Before the update listener is added, so storing these doesn't reload.
    circuit_options = await _async_detect_present(hass, coordinator, entry)
    present = present_circuits({**entry.options, **circuit_options})
    circuit_options.update(
        await async_read_circuit_options(
            coordinator,
            [
                hc
                for hc in present
                if cooling_supported_key(hc) not in entry.options
                or control_strategy_key(hc) not in entry.options
            ],
        )
    )
    if CONF_BUFFER_SENSOR not in entry.options or any(
        buffer_module_key(unit_id) not in entry.options
        for unit_id in BUFFER_MODULE_TEMP_REGISTERS
    ):
        try:
            circuit_options.update(await async_read_buffers(coordinator))
        except UpdateFailed:
            pass
    options = {**entry.options, **circuit_options}
    # Remembers or writes back raises, e.g. when a strategy read above no
    # longer allows one; tried again on the next start if this fails.
    try:
        await async_sync_flow_setpoint_raise(coordinator, options)
    except UpdateFailed as err:
        _LOGGER.warning("Could not update the flow setpoint raise: %s", err)
    if options != entry.options:
        hass.config_entries.async_update_entry(entry, options=options)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    if flow_setpoint_raise_learning(entry.options):
        control = FlowSetpointRaiseControl(hass, coordinator, entry)
        await control.async_start()
        coordinator.flow_setpoint_raise = control

    for hc in present_circuits(entry.options):
        if constant_control_enabled(entry.options, hc):
            constant = ConstantFlowControl(hass, coordinator, entry, hc)
            constant.async_start()
            coordinator.constant_controls[hc.key] = constant

    if cooling_enabled(entry.options):
        _enable_cooling_sensors(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _remove_stale_entities(hass, coordinator, entry)
    # The first refresh only read the registers of the entities enabled by
    # default; now the entities and functions have told what they need.
    await coordinator.async_refresh()

    for hc in present_circuits(entry.options):
        entity_id = entry.options.get(current_temp_entity_key(hc))
        if (
            entry.options.get(current_temp_source_key(hc))
            == CURRENT_TEMP_SOURCE_ENTITY_WRITE
            and entity_id
        ):
            writer = RoomTempWriter(hass, coordinator, entry, hc, entity_id)
            coordinator.room_temp_writers[hc.key] = writer
            writer.async_start()

    return True


async def async_read_circuit_options(
    coordinator: HovalModbusCoordinator, circuits: list[HeatingCircuit]
) -> dict[str, Any]:
    """Reads what the circuits' control strategies allow, as options to
    store: cooling, constant control, and the flow temperature limits.

    A circuit whose values can't be read is left out, so it is read again on
    the next start.
    """
    options: dict[str, Any] = {}
    for hc in circuits:
        try:
            strategy = int(await coordinator.async_read_value(hc.control_strategy_reg))
            minimum = await coordinator.async_read_value(hc.cooling_flow_min_reg)
            maximum = await coordinator.async_read_value(hc.flow_max_reg)
        except UpdateFailed:
            continue
        options[cooling_supported_key(hc)] = strategy in CONTROL_STRATEGIES_COOLING
        options[cooling_flow_min_key(hc)] = round(minimum, 1)
        options[control_strategy_key(hc)] = strategy
        options[constant_control_key(hc)] = strategy == CONTROL_STRATEGY_CONSTANT
        options[flow_max_key(hc)] = round(maximum, 1)
    return options


def _enable_cooling_sensors(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Enables the cooling sensors once cooling is turned on, unless the
    user disabled them; created before, they keep being disabled otherwise."""
    registry = er.async_get(hass)
    for key in COOLING_SENSOR_KEYS:
        entity_id = registry.async_get_entity_id(
            Platform.SENSOR, DOMAIN, f"{entry.entry_id}_{key}"
        )
        if entity_id is None:
            continue
        reg_entry = registry.async_get(entity_id)
        if reg_entry and reg_entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION:
            registry.async_update_entity(entity_id, disabled_by=None)


def _remove_stale_entities(
    hass: HomeAssistant, coordinator: HovalModbusCoordinator, entry: ConfigEntry
) -> None:
    """Removes the entry's disabled entities the integration no longer
    creates, e.g. of a heating circuit that isn't there. Home Assistant
    can't tell whether a disabled entity is still provided, so it doesn't
    offer to delete them. Enabled ones are kept, with their settings, in case
    they come back."""
    registry = er.async_get(hass)
    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if (
            reg_entry.disabled
            and reg_entry.unique_id not in coordinator.provided_unique_ids
        ):
            registry.async_remove(reg_entry.entity_id)


async def _async_detect_present(
    hass: HomeAssistant, coordinator: HovalModbusCoordinator, entry: ConfigEntry
) -> dict[str, bool]:
    """Whether heating circuits 2/3 and domestic hot water are there, for
    those not stored yet: a circuit with an entity enabled before, or whose
    flow temperature isn't 0; domestic hot water always for an entry set up
    before (one that already has options), otherwise if its temperature
    isn't 0. One whose register can't be read is left out, so it is detected
    again on the next start."""
    registry = er.async_get(hass)
    enabled = [
        reg_entry.unique_id
        for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id)
        if not reg_entry.disabled
    ]

    async def nonzero(reg: RegisterDef) -> bool | None:
        try:
            return bool(await coordinator.async_read_value_if_present(reg))
        except UpdateFailed:
            return None

    options: dict[str, bool] = {}
    for number, hc in enumerate(HEATING_CIRCUITS, start=1):
        if hc is HEATING_CIRCUIT_1 or circuit_present_key(hc) in entry.options:
            continue
        if any(
            unique_id.endswith((f"_hc{number}", f"_{hc.key}")) for unique_id in enabled
        ):
            options[circuit_present_key(hc)] = True
        elif (present := await nonzero(CIRCUIT_FLOW_TEMP_REGISTERS[hc.key])) is not None:
            options[circuit_present_key(hc)] = present
    if CONF_DHW_PRESENT not in entry.options:
        if entry.options:
            options[CONF_DHW_PRESENT] = True
        elif (present := await nonzero(DHW_CURRENT_TEMP_REGISTER)) is not None:
            options[CONF_DHW_PRESENT] = present
    return options


async def async_read_buffers(coordinator: HovalModbusCoordinator) -> dict[str, bool]:
    """Which buffer modules (PS) the heat pump has, and whether it has a
    buffer sensor (WPF), as options to store. Raises UpdateFailed."""
    options: dict[str, bool] = {}
    for unit_id, reg in BUFFER_MODULE_TEMP_REGISTERS.items():
        options[buffer_module_key(unit_id)] = buffer_module_temp_plausible(
            await coordinator.async_read_value_if_present(reg)
        )
    options[CONF_BUFFER_MODULE] = any(options.values())
    options[CONF_BUFFER_SENSOR] = bool(
        await coordinator.async_read_value_if_present(BUFFER_SENSOR_REGISTER)
    )
    return options


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when options change (e.g. polling interval, or a
    heating circuit's current-temperature source)."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unloads the integration."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: HovalModbusCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        for writer in coordinator.room_temp_writers.values():
            writer.async_stop()
        for constant in coordinator.constant_controls.values():
            constant.async_stop()
        if coordinator.flow_setpoint_raise is not None:
            await coordinator.flow_setpoint_raise.async_stop()
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Resets what the integration changed on the controller and removes
    what it stored."""
    await async_cleanup_removed_entry(hass, entry)
    await suggestion_store(hass, entry.entry_id).async_remove()
