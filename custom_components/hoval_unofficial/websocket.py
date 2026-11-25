"""WebSocket command for the system diagram card and the dashboard (frontend/).

The card asks which components the integration found and which entity shows
which value; it reads the values from the entities' states itself. Entities
are looked up by unique ID, so renamed entities keep working, and disabled
ones are left out. The dashboard strategy also gets all enabled entities of
the entry by key.
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .const import (
    BUFFER_MODULE_ROLES,
    BUFFER_MODULE_SENSOR_REGISTERS,
    CONF_BUFFER_SENSOR,
    DHW_BOTTOM_TEMP_REGISTER,
    DOMAIN,
    HEATING_CIRCUITS,
    SMART_GRID_OFFSET_BUFFER_142_COOLING_REGISTER,
    SMART_GRID_OFFSET_BUFFER_142_HEATING_REGISTER,
    SMART_GRID_OFFSET_BUFFER_COOLING_REGISTER,
    SMART_GRID_OFFSET_BUFFER_HEATING_REGISTER,
    SMART_GRID_OFFSET_DHW_REGISTER,
    SMART_GRID_STATUS_REGISTER,
    dhw_bottom_sensor,
    dhw_present,
    found_buffer_modules,
    meters_present,
    present_circuits,
)

WS_TYPE_SCHEMA = f"{DOMAIN}/schema"

# Smart Grid offsets of a buffer module, by UnitId: (heating, cooling).
_BUFFER_OFFSETS = {
    142: (
        SMART_GRID_OFFSET_BUFFER_142_HEATING_REGISTER,
        SMART_GRID_OFFSET_BUFFER_142_COOLING_REGISTER,
    ),
    143: (
        SMART_GRID_OFFSET_BUFFER_HEATING_REGISTER,
        SMART_GRID_OFFSET_BUFFER_COOLING_REGISTER,
    ),
}


@callback
def async_register_websocket(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_schema)


def _loaded_entries(hass: HomeAssistant) -> list[ConfigEntry]:
    return [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
    ]


@websocket_api.websocket_command(
    {vol.Required("type"): WS_TYPE_SCHEMA, vol.Optional("entry_id"): str}
)
@callback
def ws_schema(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """The components of an entry (the given one, or the first loaded one)
    and their entities."""
    entries = _loaded_entries(hass)
    if "entry_id" in msg:
        entries = [entry for entry in entries if entry.entry_id == msg["entry_id"]]
    if not entries:
        connection.send_error(msg["id"], "not_found", "No loaded Hoval entry found")
        return
    connection.send_result(msg["id"], build_schema(hass, entries[0]))


def build_schema(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """What the card shows for an entry: the heat pump, the buffer(s), hot
    water and the heating circuits, each with the entity IDs of its values
    (None for an entity that doesn't exist or is disabled). "entities" maps
    the key of every enabled, visible entity of the entry to its entity ID."""
    registry = er.async_get(hass)
    options = entry.options

    def entity(platform: Platform, key: str) -> str | None:
        entity_id = registry.async_get_entity_id(
            platform, DOMAIN, f"{entry.entry_id}_{key}"
        )
        if entity_id is None:
            return None
        reg_entry = registry.async_get(entity_id)
        if reg_entry is None or reg_entry.disabled:
            return None
        return entity_id

    def sensor(key: str) -> str | None:
        return entity(Platform.SENSOR, key)

    def number(key: str) -> str | None:
        return entity(Platform.NUMBER, key)

    heat_pump = {
        "status": sensor("heat_pump_status"),
        "temperature": sensor("heat_generator_actual_temp"),
        "target": sensor("heat_generator_target_temp"),
        "return": sensor("return_temp_heat_generator"),
        "power": sensor("current_power_output"),
        "cooling_power": sensor("current_cooling_power"),
        "modulation": sensor("modulation"),
        "electrical_power": sensor("electrical_power_input_heat_generator"),
        "cop": sensor("cop"),
        "outdoor": sensor("outdoor_temp"),
        "starts": sensor("switching_cycles_heat_generator"),
    }

    buffers: list[dict[str, Any]] = []
    modules = found_buffer_modules(options)
    for unit_id in modules:
        (top, _), (bottom, _), (setpoint, _) = BUFFER_MODULE_SENSOR_REGISTERS[unit_id]
        heating_offset, cooling_offset = _BUFFER_OFFSETS[unit_id]
        buffers.append(
            {
                # With both modules, 143 is the heating and 142 the cooling buffer.
                "role": BUFFER_MODULE_ROLES[unit_id] if len(modules) > 1 else "buffer",
                "top": sensor(top.key),
                "bottom": sensor(bottom.key),
                "middle": None,
                "setpoint": sensor(setpoint.key),
                "sg_offset_heating": number(heating_offset.key),
                "sg_offset_cooling": number(cooling_offset.key),
            }
        )
    if not buffers and options.get(CONF_BUFFER_SENSOR):
        buffers.append(
            {
                "role": "buffer",
                "top": None,
                "bottom": None,
                "middle": sensor("buffer_temp"),
                "setpoint": None,
                "sg_offset_heating": None,
                "sg_offset_cooling": None,
            }
        )
    # A buffer without any value isn't drawn.
    buffers = [b for b in buffers if b["top"] or b["bottom"] or b["middle"]]

    hot_water = None
    if dhw_present(options):
        hot_water = {
            "water_heater": entity(Platform.WATER_HEATER, "dhw_water_heater"),
            "bottom": (
                sensor(DHW_BOTTOM_TEMP_REGISTER.key) if dhw_bottom_sensor(options) else None
            ),
            "sg_offset": number(SMART_GRID_OFFSET_DHW_REGISTER.key),
        }

    circuits = []
    for number_, hc in enumerate(HEATING_CIRCUITS, start=1):
        if hc not in present_circuits(options):
            continue
        circuits.append(
            {
                "number": number_,
                "climate": entity(Platform.CLIMATE, hc.key),
                "flow": sensor(f"flow_temp_hc{number_}"),
                "flow_target": sensor(f"supply_target_temp_hc{number_}"),
                "status": sensor(hc.status_reg.key),
                "sg_offset_heating": number(hc.smart_grid_offset_reg.key),
                "sg_offset_cooling": number(f"smart_grid_offset_cooling_hc{number_}"),
            }
        )

    prefix = f"{entry.entry_id}_"
    return {
        "entry_id": entry.entry_id,
        "title": entry.title,
        "heat_pump": heat_pump,
        "buffers": buffers,
        "hot_water": hot_water,
        "circuits": circuits,
        "smart_grid_status": sensor(SMART_GRID_STATUS_REGISTER.key),
        # Whether the heat pump has energy meters (not with the ECR461).
        "meters": meters_present(options),
        "entities": {
            reg_entry.unique_id.removeprefix(prefix): reg_entry.entity_id
            for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id)
            if not reg_entry.disabled
            and not reg_entry.hidden
            and reg_entry.unique_id.startswith(prefix)
        },
    }
