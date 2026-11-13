"""Config Flow: UI-based setup with name, IP address, port and device ID."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT, CONF_SCAN_INTERVAL
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector
from homeassistant.helpers.translation import async_get_translations
from homeassistant.helpers.update_coordinator import UpdateFailed

from pymodbus.client import AsyncModbusTcpClient

from .const import (
    CONF_DEVICE_ID,
    CONTROL_STRATEGY_MAP,
    CONTROL_STRATEGY_ROOM_INFLUENCE,
    CONTROL_STRATEGY_UNKNOWN,
    CONTROL_STRATEGY_WEATHER_COMPENSATED,
    CONTROL_STRATEGY_WRITABLE,
    CURRENT_TEMP_SOURCE_ENTITY,
    CURRENT_TEMP_SOURCE_ENTITY_WRITE,
    CURRENT_TEMP_SOURCE_MODBUS,
    CURRENT_TEMP_SOURCE_NONE,
    DEFAULT_CURRENT_TEMP_SOURCE,
    DEFAULT_DEVICE_ID,
    DEFAULT_NAME,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    HEATING_CIRCUIT_1,
    HEATING_CIRCUIT_2,
    HEATING_CIRCUIT_3,
    HEATING_CIRCUITS,
    HeatingCircuit,
    current_temp_entity_key,
    current_temp_source_key,
    previous_strategy_key,
)
from . import async_read_cooling_options
from .coordinator import HovalModbusCoordinator
from .room_temp import has_room_sensor, parse_room_temperature

_LOGGER = logging.getLogger(__name__)

FIELD_SOURCE = "current_temp_source"
FIELD_ENTITY = "current_temp_entity_id"
FIELD_ROOM_INFLUENCE = "room_influence"
FIELD_ACCEPT_RISKS = "accept_risks"

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME, default=DEFAULT_NAME): str,
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): vol.Coerce(int),
        vol.Required(CONF_DEVICE_ID, default=DEFAULT_DEVICE_ID): vol.Coerce(int),
        vol.Optional(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): vol.Coerce(int),
    }
)

# Same fields as STEP_USER_DATA_SCHEMA minus scan_interval, which stays the
# sole responsibility of the options flow (HovalUnofficialOptionsFlow) to
# avoid two different places editing the same value.
STEP_RECONFIGURE_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME, default=DEFAULT_NAME): str,
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): vol.Coerce(int),
        vol.Required(CONF_DEVICE_ID, default=DEFAULT_DEVICE_ID): vol.Coerce(int),
    }
)


async def test_connection(host: str, port: int, device_id: int) -> bool:
    """Checks whether a Modbus device is reachable with the given settings."""
    client = AsyncModbusTcpClient(host, port=port)
    try:
        connected = await client.connect()
        if not connected:
            return False
        # A simple read attempt on register 0 is enough to test connectivity.
        # An error code returned by the device (e.g. "IllegalAddress") still
        # counts as "device reachable" -- only an actual communication
        # failure does not.
        result = await client.read_holding_registers(0, count=1, device_id=device_id)
        return result is not None
    except Exception:  # pylint: disable=broad-except
        _LOGGER.exception("Connection test failed")
        return False
    finally:
        client.close()


class HovalUnofficialConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config Flow for the Hoval unofficial integration."""

    VERSION = 1
    MINOR_VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            name = user_input[CONF_NAME]
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]
            device_id = user_input[CONF_DEVICE_ID]

            # Prevent setting up the same controller twice
            unique_id = f"{host}:{port}:{device_id}"
            await self.async_set_unique_id(unique_id)
            self._abort_if_unique_id_configured()

            connection_ok = await test_connection(host, port, device_id)
            if not connection_ok:
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(
                    title=name,
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Let the user change name, IP address, port and device ID after setup.

        Reachable via the entry's three-dot menu -> "Reconfigure". Doesn't
        touch `scan_interval` or the heating-circuit temperature-source
        settings, which are handled by the options flow below.
        """
        errors: dict[str, str] = {}
        reconfigure_entry = self._get_reconfigure_entry()

        if user_input is not None:
            name = user_input[CONF_NAME]
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]
            device_id = user_input[CONF_DEVICE_ID]

            connection_ok = await test_connection(host, port, device_id)
            if not connection_ok:
                errors["base"] = "cannot_connect"
            else:
                # The unique_id is derived from host/port/device_id, so it can
                # change here -- check for conflicts with *other* entries only
                # (the entry being reconfigured itself is excluded).
                new_unique_id = f"{host}:{port}:{device_id}"
                conflict = next(
                    (
                        entry
                        for entry in self._async_current_entries()
                        if entry.entry_id != reconfigure_entry.entry_id
                        and entry.unique_id == new_unique_id
                    ),
                    None,
                )
                if conflict is not None:
                    errors["base"] = "already_configured"
                else:
                    return self.async_update_reload_and_abort(
                        reconfigure_entry,
                        title=name,
                        unique_id=new_unique_id,
                        data={**reconfigure_entry.data, **user_input},
                    )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                STEP_RECONFIGURE_DATA_SCHEMA, reconfigure_entry.data
            ),
            errors=errors,
        )

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "HovalUnofficialOptionsFlow":
        return HovalUnofficialOptionsFlow()


class HovalUnofficialOptionsFlow(config_entries.OptionsFlow):
    """Allows changing the polling interval and, per heating circuit, where
    its room temperature comes from and whether it is sent to the controller.

    Each menu entry is its own form that ends the flow when submitted. The
    forms of the heating circuits depend on what is read from the controller
    when they are opened, and submitting one can write to the controller.
    """

    def __init__(self) -> None:
        self._hc: HeatingCircuit = HEATING_CIRCUIT_1
        self._pending_input: dict[str, Any] = {}
        # Confirmations given for the pending input; cleared whenever the
        # circuit form is shown again.
        self._room_influence_confirmed = False
        self._risks_accepted = False

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=["general", *(hc.key for hc in HEATING_CIRCUITS)],
        )

    async def async_step_general(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            options = dict(self.config_entry.options)
            options.pop(CONF_SCAN_INTERVAL, None)
            options.update(user_input)
            return self.async_create_entry(title="", data=options)

        current_scan_interval = self.config_entry.options.get(
            CONF_SCAN_INTERVAL,
            self.config_entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        )
        return self.async_show_form(
            step_id="general",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_SCAN_INTERVAL,
                        description={"suggested_value": current_scan_interval},
                    ): vol.Coerce(int),
                }
            ),
        )

    async def async_step_heating_circuit_1(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        self._hc = HEATING_CIRCUIT_1
        return await self.async_step_circuit()

    async def async_step_heating_circuit_2(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        self._hc = HEATING_CIRCUIT_2
        return await self.async_step_circuit()

    async def async_step_heating_circuit_3(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        self._hc = HEATING_CIRCUIT_3
        return await self.async_step_circuit()

    async def async_step_circuit(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        coordinator: HovalModbusCoordinator | None = self.hass.data.get(
            DOMAIN, {}
        ).get(self.config_entry.entry_id)
        if coordinator is None:
            return self.async_abort(reason="not_loaded")

        hc = self._hc
        try:
            strategy = int(await coordinator.async_read_value(hc.control_strategy_reg))
            room_temperature = await coordinator.async_read_value(
                hc.room_actual_temp_reg
            )
        except UpdateFailed:
            return self.async_abort(reason="cannot_read")

        options = self.config_entry.options
        current_source = options.get(
            current_temp_source_key(hc), DEFAULT_CURRENT_TEMP_SOURCE
        )
        sending = current_source == CURRENT_TEMP_SOURCE_ENTITY_WRITE
        # While sending, the register holds the integration's own value.
        sensor_found = not sending and has_room_sensor(room_temperature)
        can_send = sending or (
            not sensor_found and strategy in CONTROL_STRATEGY_WRITABLE
        )
        offer_room_influence = strategy in CONTROL_STRATEGY_WRITABLE

        errors: dict[str, str] = {}
        if user_input is not None:
            source = user_input[FIELD_SOURCE]
            entity_id = user_input.get(FIELD_ENTITY)
            temperature = None
            if source == CURRENT_TEMP_SOURCE_ENTITY_WRITE:
                temperature = parse_room_temperature(
                    self.hass.states.get(entity_id) if entity_id else None
                )
                if not entity_id:
                    errors[FIELD_ENTITY] = "entity_required"
                elif temperature is None:
                    errors[FIELD_ENTITY] = "entity_unavailable"
                elif not can_send:
                    errors["base"] = "strategy_not_writable"

            room_influence = (
                user_input.get(FIELD_ROOM_INFLUENCE, False)
                if offer_room_influence
                else None
            )
            # Without a detected sensor or a value being sent, room influence
            # only works with a sensor the integration can't see, e.g. one
            # retrofitted while it was sending.
            if (
                not errors
                and room_influence
                and source != CURRENT_TEMP_SOURCE_ENTITY_WRITE
                and not sensor_found
                and not self._room_influence_confirmed
            ):
                self._pending_input = user_input
                return await self.async_step_confirm_room_influence()

            # Only a value that is sent and included in the control strategy
            # lets Home Assistant drive the controller.
            influence_after = (
                room_influence
                if room_influence is not None
                else strategy == CONTROL_STRATEGY_ROOM_INFLUENCE
            )
            if (
                not errors
                and source == CURRENT_TEMP_SOURCE_ENTITY_WRITE
                and influence_after
                and not self._risks_accepted
            ):
                self._pending_input = user_input
                return await self.async_step_confirm_risks()

            if not errors:
                try:
                    options = await self._async_apply_circuit(
                        coordinator,
                        strategy,
                        temperature,
                        source,
                        entity_id,
                        room_influence,
                    )
                    # Saving the circuit is how cooling added later is found.
                    options.update(
                        await async_read_cooling_options(coordinator, [hc])
                    )
                    return self.async_create_entry(title="", data=options)
                except UpdateFailed as err:
                    _LOGGER.warning("%s: writing failed: %s", hc.key, err)
                    errors["base"] = "write_failed"

        source_options = [
            CURRENT_TEMP_SOURCE_NONE,
            CURRENT_TEMP_SOURCE_MODBUS,
            CURRENT_TEMP_SOURCE_ENTITY,
        ]
        if can_send:
            source_options.append(CURRENT_TEMP_SOURCE_ENTITY_WRITE)

        # An unset source defaults to the sensor found on the controller.
        previous_input = user_input or self._pending_input
        suggested_source = previous_input.get(
            FIELD_SOURCE,
            options.get(
                current_temp_source_key(hc),
                CURRENT_TEMP_SOURCE_MODBUS
                if sensor_found
                else DEFAULT_CURRENT_TEMP_SOURCE,
            ),
        )
        current_entity_id = previous_input.get(
            FIELD_ENTITY, options.get(current_temp_entity_key(hc))
        )
        entity_field_kwargs: dict[str, Any] = {}
        if current_entity_id:
            entity_field_kwargs["description"] = {"suggested_value": current_entity_id}

        schema: dict[Any, Any] = {
            vol.Required(FIELD_SOURCE, default=suggested_source): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=source_options,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                    translation_key="current_temp_source",
                )
            ),
            # Always shown, but only relevant for the entity-based sources.
            vol.Optional(FIELD_ENTITY, **entity_field_kwargs): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain="sensor", device_class="temperature"
                )
            ),
        }
        if offer_room_influence:
            schema[
                vol.Optional(
                    FIELD_ROOM_INFLUENCE,
                    default=previous_input.get(
                        FIELD_ROOM_INFLUENCE,
                        strategy == CONTROL_STRATEGY_ROOM_INFLUENCE,
                    ),
                )
            ] = bool

        self._room_influence_confirmed = False
        self._risks_accepted = False
        return self.async_show_form(
            step_id="circuit",
            data_schema=vol.Schema(schema),
            errors=errors,
            description_placeholders={
                "circuit": hc.key.removeprefix("heating_circuit_"),
                "strategy": await self._async_strategy_name(strategy),
                "room_temperature": self._format_temperature(room_temperature),
            },
        )

    async def async_step_confirm_room_influence(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        return self.async_show_menu(
            step_id="confirm_room_influence",
            menu_options=[
                "confirm_room_influence_yes",
                "confirm_room_influence_no",
            ],
            description_placeholders={
                "circuit": self._hc.key.removeprefix("heating_circuit_"),
            },
        )

    async def async_step_confirm_room_influence_yes(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        self._room_influence_confirmed = True
        return await self.async_step_circuit(self._pending_input)

    async def async_step_confirm_room_influence_no(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        return await self.async_step_circuit()

    async def async_step_confirm_risks(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input.get(FIELD_ACCEPT_RISKS):
                self._risks_accepted = True
                return await self.async_step_circuit(self._pending_input)
            errors["base"] = "risks_not_accepted"

        return self.async_show_form(
            step_id="confirm_risks",
            data_schema=vol.Schema(
                {vol.Required(FIELD_ACCEPT_RISKS, default=False): bool}
            ),
            errors=errors,
            description_placeholders={
                "circuit": self._hc.key.removeprefix("heating_circuit_"),
            },
        )

    async def _async_apply_circuit(
        self,
        coordinator: HovalModbusCoordinator,
        strategy: int,
        temperature: float | None,
        source: str,
        entity_id: str | None,
        room_influence: bool | None,
    ) -> dict[str, Any]:
        """Writes the requested changes to the controller and returns the new
        options. Raises UpdateFailed, leaving the options unchanged."""
        hc = self._hc
        options = dict(self.config_entry.options)
        previous_key = previous_strategy_key(hc)
        writer = coordinator.room_temp_writers.get(hc.key)
        leaving = writer is not None and source != CURRENT_TEMP_SOURCE_ENTITY_WRITE
        new_strategy = strategy

        # Stopped first so that it can't overwrite the value reset below.
        if leaving:
            writer.async_stop()
        try:
            if source == CURRENT_TEMP_SOURCE_ENTITY_WRITE:
                if strategy in CONTROL_STRATEGY_WRITABLE:
                    await coordinator.async_write_value(
                        hc.room_actual_temp_reg, temperature, refresh=False
                    )
            elif leaving and strategy in CONTROL_STRATEGY_WRITABLE:
                await coordinator.async_write_value(
                    hc.room_actual_temp_reg, 0, refresh=False
                )

            if room_influence and strategy == CONTROL_STRATEGY_WEATHER_COMPENSATED:
                await coordinator.async_write_value(
                    hc.control_strategy_reg,
                    CONTROL_STRATEGY_ROOM_INFLUENCE,
                    refresh=False,
                )
                new_strategy = CONTROL_STRATEGY_ROOM_INFLUENCE
                options[previous_key] = strategy
            elif (
                room_influence is False and strategy == CONTROL_STRATEGY_ROOM_INFLUENCE
            ):
                await coordinator.async_write_value(
                    hc.control_strategy_reg,
                    CONTROL_STRATEGY_WEATHER_COMPENSATED,
                    refresh=False,
                )
                new_strategy = CONTROL_STRATEGY_WEATHER_COMPENSATED
        except UpdateFailed:
            if leaving:
                writer.async_start()
            raise

        if new_strategy != CONTROL_STRATEGY_ROOM_INFLUENCE:
            options.pop(previous_key, None)
        options[current_temp_source_key(hc)] = source
        options.pop(current_temp_entity_key(hc), None)
        if entity_id:
            options[current_temp_entity_key(hc)] = entity_id
        return options

    def _format_temperature(self, temperature: float) -> str:
        """The temperature with the decimal separator of the user's language."""
        text = f"{temperature:.1f}"
        return text.replace(".", ",") if self.hass.config.language == "de" else text

    async def _async_strategy_name(self, strategy: int) -> str:
        """The strategy's name in the user's language."""
        translations = await async_get_translations(
            self.hass, self.hass.config.language, "entity", {DOMAIN}
        )
        key = CONTROL_STRATEGY_MAP.get(strategy, CONTROL_STRATEGY_UNKNOWN)
        return translations.get(
            f"component.{DOMAIN}.entity.sensor.control_strategy.state.{key}", key
        )
