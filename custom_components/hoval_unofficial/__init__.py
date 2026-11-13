"""Hoval Heat Pump Integration (unofficial)."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT, CONF_SCAN_INTERVAL, Platform
from homeassistant.core import HomeAssistant

from .const import (
    CONF_DEVICE_ID,
    CURRENT_TEMP_SOURCE_ENTITY_WRITE,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    HEATING_CIRCUITS,
    current_temp_entity_key,
    current_temp_source_key,
)
from .coordinator import HovalModbusCoordinator
from .room_temp import RoomTempWriter, async_cleanup_removed_entry

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.WATER_HEATER,
    Platform.CLIMATE,
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
        hass, name, host, port, device_id, scan_interval
    )
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    for hc in HEATING_CIRCUITS:
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
        await coordinator.async_close()
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Resets what the integration changed on the controller."""
    await async_cleanup_removed_entry(entry)
