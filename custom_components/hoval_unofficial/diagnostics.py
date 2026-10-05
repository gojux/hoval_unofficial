"""Diagnostics download for the Hoval Heat Pump Integration."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .const import DOMAIN, CONF_DEVICE_ID
from .coordinator import HovalModbusCoordinator

REDACTED = "**REDACTED**"
TO_REDACT = {"host", "name", "title"}


def _redact_entity_ids(options: dict[str, Any]) -> dict[str, Any]:
    return {
        key: (REDACTED if key.endswith("entity_id") and value else value)
        for key, value in options.items()
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    integration = await async_get_integration(hass, DOMAIN)
    coordinator: HovalModbusCoordinator = hass.data[DOMAIN][entry.entry_id]

    return {
        "integration_version": integration.version,
        "home_assistant_version": HA_VERSION,
        "config_entry": {
            "title": REDACTED,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": _redact_entity_ids(dict(entry.options)),
        },
        "device_id": entry.data.get(CONF_DEVICE_ID),
        "last_update_success": coordinator.last_update_success,
        "register_values": dict(coordinator.data or {}),
    }
