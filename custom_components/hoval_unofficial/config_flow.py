"""Config Flow: UI-based setup with name, IP address, port and device ID."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components import modbus
from homeassistant.const import (
    CONF_HOST,
    CONF_NAME,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    CONF_TIMEOUT,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult, section
from homeassistant.helpers import selector
from homeassistant.helpers.translation import async_get_translations
from homeassistant.helpers.update_coordinator import UpdateFailed

from modbus_connection import ModbusError, ModbusExceptionError, ModbusTcpParams

from .const import (
    CONF_BUFFER_MODULE,
    CONF_DEVICE_ID,
    CONF_DHW_PRESENT,
    CONF_HEAT_PUMP_CONTROLLER,
    CONF_EXPERIMENTAL_CONSTANT_CONTROL,
    CONF_EXPERIMENTAL_COOLING,
    CONF_EXPERIMENTAL_FLOW_SETPOINT_RAISE,
    CONF_EXPERIMENTAL_ROOM_INFLUENCE,
    CONTROL_STRATEGY_CONSTANT,
    CONTROL_STRATEGY_MAP,
    CONTROL_STRATEGY_UNKNOWN,
    CONTROL_STRATEGY_WRITABLE,
    CURRENT_TEMP_SOURCE_ENTITY,
    CURRENT_TEMP_SOURCE_ENTITY_WRITE,
    CURRENT_TEMP_SOURCE_MODBUS,
    CURRENT_TEMP_SOURCE_NONE,
    DEFAULT_CURRENT_TEMP_SOURCE,
    DEFAULT_DEVICE_ID,
    DEFAULT_FLOW_SETPOINT_RAISE_MAX,
    DEFAULT_FLOW_SETPOINT_RAISE_MIN,
    DEFAULT_NAME,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TIMEOUT,
    CONF_MESSAGE_SPACING,
    DEFAULT_MESSAGE_SPACING,
    MAX_MESSAGE_SPACING,
    DOMAIN,
    FLOW_SETPOINT_RAISE_REGISTERS,
    FLOW_SETPOINT_RAISE_STRATEGIES,
    HEATING_CIRCUIT_1,
    HEATING_CIRCUIT_2,
    HEATING_CIRCUIT_3,
    HEATING_CIRCUITS,
    HEAT_PUMP_CONTROLLER_WFA,
    HEAT_PUMP_CONTROLLERS,
    HeatingCircuit,
    MAX_SCAN_INTERVAL,
    MAX_TIMEOUT,
    MIN_SCAN_INTERVAL,
    MIN_TIMEOUT,
    WITH_INFLUENCE_TO_WITHOUT,
    WITHOUT_INFLUENCE_TO_WITH,
    circuit_present,
    circuit_present_key,
    control_strategy_key,
    cooling_enabled,
    dhw_present,
    present_circuits,
    current_temp_entity_key,
    current_temp_source_key,
    flow_setpoint_raise_bound_keys,
    flow_setpoint_raise_bounds,
    heat_pump_controller,
    meters_present,
    previous_strategy_key,
    room_influence_enabled,
)
from . import (
    async_read_buffers,
    async_read_circuit_options,
    async_read_dhw_bottom_sensor,
    async_suggest_heat_pump_controller,
    update_meter_sensors,
)
from .coordinator import HovalModbusCoordinator, read_register, timeout_supported
from .raise_control import async_sync_flow_setpoint_raise
from .room_temp import has_room_sensor, parse_room_temperature

_LOGGER = logging.getLogger(__name__)

FIELD_SOURCE = "current_temp_source"
FIELD_RAISE_MIN = "flow_setpoint_raise_min"
FIELD_RAISE_MAX = "flow_setpoint_raise_max"
FIELD_ENTITY = "current_temp_entity_id"
FIELD_ROOM_INFLUENCE = "room_influence"
FIELD_ACCEPT_RISKS = "accept_risks"

SCAN_INTERVAL_VALIDATOR = vol.All(
    vol.Coerce(int), vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL)
)
TIMEOUT_VALIDATOR = vol.All(
    vol.Coerce(float), vol.Range(min=MIN_TIMEOUT, max=MAX_TIMEOUT)
)
MESSAGE_SPACING_VALIDATOR = vol.All(
    vol.Coerce(int), vol.Range(min=0, max=MAX_MESSAGE_SPACING)
)


def _number_box(minimum: int, maximum: int) -> selector.NumberSelector:
    """A plain input field; a small range would otherwise show a slider."""
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=minimum, max=maximum, step=1, mode=selector.NumberSelectorMode.BOX
        )
    )


PORT_SELECTOR = _number_box(1, 65535)
# Modbus addresses 1-247; 0 is accepted since some gateways answer on it.
DEVICE_ID_SELECTOR = _number_box(0, 247)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME, default=DEFAULT_NAME): str,
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): PORT_SELECTOR,
        vol.Required(CONF_DEVICE_ID, default=DEFAULT_DEVICE_ID): DEVICE_ID_SELECTOR,
    }
)

# Polling interval, timeout and gap: stored in the options, edited only in
# the reconfigure step's collapsed section.
SECTION_ADVANCED = "advanced"
CONNECTION_OPTIONS = (CONF_SCAN_INTERVAL, CONF_TIMEOUT, CONF_MESSAGE_SPACING)


def _connection(user_input: dict[str, Any]) -> dict[str, Any]:
    """Name, host, port and device ID, with the numbers as int."""
    return {
        CONF_NAME: user_input[CONF_NAME],
        CONF_HOST: user_input[CONF_HOST],
        CONF_PORT: int(user_input[CONF_PORT]),
        CONF_DEVICE_ID: int(user_input[CONF_DEVICE_ID]),
    }


def _reconfigure_schema() -> vol.Schema:
    advanced: dict[Any, Any] = {
        vol.Optional(CONF_SCAN_INTERVAL): SCAN_INTERVAL_VALIDATOR,
    }
    # Only offered where Home Assistant's Modbus connection takes a request
    # timeout; otherwise its default applies.
    # HA-COMPAT(2026.10): always offer the field.
    if timeout_supported():
        advanced[vol.Optional(CONF_TIMEOUT)] = TIMEOUT_VALIDATOR
    advanced[vol.Optional(CONF_MESSAGE_SPACING)] = MESSAGE_SPACING_VALIDATOR
    return vol.Schema(
        {
            vol.Required(CONF_NAME): str,
            vol.Required(CONF_HOST): str,
            vol.Required(CONF_PORT): PORT_SELECTOR,
            vol.Required(CONF_DEVICE_ID): DEVICE_ID_SELECTOR,
            vol.Required(SECTION_ADVANCED): section(
                vol.Schema(advanced), {"collapsed": True}
            ),
        }
    )


CONTROLLER_SELECTOR = selector.SelectSelector(
    selector.SelectSelectorConfig(
        options=HEAT_PUMP_CONTROLLERS,
        mode=selector.SelectSelectorMode.LIST,
        translation_key=CONF_HEAT_PUMP_CONTROLLER,
    )
)


async def suggest_controller(
    hass: HomeAssistant, host: str, port: int, device_id: int
) -> str:
    """The heat pump's controller suggested by its meters; WFA if they
    can't be read."""
    try:
        async with modbus.async_get_temporary_unit(
            hass, ModbusTcpParams(host=host, port=port), device_id
        ) as unit:
            return await async_suggest_heat_pump_controller(
                lambda reg: read_register(unit, reg)
            )
    except ModbusError as err:
        _LOGGER.debug("Reading the energy meters failed: %s", err)
        return HEAT_PUMP_CONTROLLER_WFA


async def test_connection(
    hass: HomeAssistant, host: str, port: int, device_id: int
) -> bool:
    """Checks whether a Modbus device is reachable with the given settings."""
    try:
        async with modbus.async_get_temporary_unit(
            hass, ModbusTcpParams(host=host, port=port), device_id
        ) as unit:
            # A simple read attempt on register 0 is enough to test
            # connectivity.
            await unit.read_holding_registers(0, 1)
    except ModbusExceptionError:
        # An error code returned by the device (e.g. "IllegalAddress") still
        # counts as "device reachable" -- only an actual communication
        # failure does not.
        return True
    except ModbusError as err:
        _LOGGER.warning("Connection test to %s:%s failed: %s", host, port, err)
        return False
    except Exception:  # pylint: disable=broad-except
        # Not a communication failure; logged in full to find the cause.
        _LOGGER.exception("Connection test to %s:%s failed unexpectedly", host, port)
        return False
    return True


class HovalUnofficialConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config Flow for the Hoval unofficial integration."""

    VERSION = 1
    MINOR_VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._suggested_controller = HEAT_PUMP_CONTROLLER_WFA

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            data = _connection(user_input)
            host = data[CONF_HOST]
            port = data[CONF_PORT]
            device_id = data[CONF_DEVICE_ID]

            # Prevent setting up the same controller twice
            unique_id = f"{host}:{port}:{device_id}"
            await self.async_set_unique_id(unique_id)
            self._abort_if_unique_id_configured()

            connection_ok = await test_connection(self.hass, host, port, device_id)
            if not connection_ok:
                errors["base"] = "cannot_connect"
            else:
                self._data = data
                self._suggested_controller = await suggest_controller(
                    self.hass, host, port, device_id
                )
                return await self.async_step_controller()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_controller(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Asks for the heat pump's controller, which no register tells."""
        if user_input is not None:
            return self.async_create_entry(
                title=self._data[CONF_NAME],
                data=self._data,
                options={CONF_HEAT_PUMP_CONTROLLER: user_input[CONF_HEAT_PUMP_CONTROLLER]},
            )
        return self.async_show_form(
            step_id="controller",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_HEAT_PUMP_CONTROLLER, default=self._suggested_controller
                    ): CONTROLLER_SELECTOR
                }
            ),
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Let the user change the connection after setup: name, IP address,
        port, device ID and, in a collapsed section, polling interval,
        timeout and gap between requests.

        Reachable via the entry's three-dot menu -> "Reconfigure". The
        installation itself is set in the options flow below.
        """
        errors: dict[str, str] = {}
        reconfigure_entry = self._get_reconfigure_entry()

        if user_input is not None:
            connection = _connection(user_input)
            name = connection[CONF_NAME]
            host = connection[CONF_HOST]
            port = connection[CONF_PORT]
            device_id = connection[CONF_DEVICE_ID]

            connection_ok = await test_connection(self.hass, host, port, device_id)
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
                    data = {**reconfigure_entry.data, **connection}
                    # Set at setup by older versions; the options hold it now.
                    data.pop(CONF_SCAN_INTERVAL, None)
                    # An emptied field goes back to its default.
                    options = {
                        key: value
                        for key, value in reconfigure_entry.options.items()
                        if key not in CONNECTION_OPTIONS
                    }
                    options.update(user_input.get(SECTION_ADVANCED, {}))
                    return self.async_update_reload_and_abort(
                        reconfigure_entry,
                        title=name,
                        unique_id=new_unique_id,
                        data=data,
                        options=options,
                    )

        if user_input is None:
            entry_data = reconfigure_entry.data
            entry_options = reconfigure_entry.options
            user_input = {
                **entry_data,
                SECTION_ADVANCED: {
                    CONF_SCAN_INTERVAL: entry_options.get(
                        CONF_SCAN_INTERVAL,
                        entry_data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                    ),
                    CONF_TIMEOUT: entry_options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT),
                    CONF_MESSAGE_SPACING: entry_options.get(
                        CONF_MESSAGE_SPACING, DEFAULT_MESSAGE_SPACING
                    ),
                },
            }
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                _reconfigure_schema(), user_input
            ),
            errors=errors,
        )

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "HovalUnofficialOptionsFlow":
        return HovalUnofficialOptionsFlow()


class HovalUnofficialOptionsFlow(config_entries.OptionsFlow):
    """Allows changing the installation (controller, which heating circuits
    and hot water are there) and, per heating circuit, where its room
    temperature comes from and whether it is sent to the controller. The
    connection is changed in the config flow's reconfigure step.

    Each menu entry is its own form that ends the flow when submitted. The
    forms of the heating circuits depend on what is read from the controller
    when they are opened, and submitting one can write to the controller.
    """

    def __init__(self) -> None:
        self._hc: HeatingCircuit = HEATING_CIRCUIT_1
        self._pending_input: dict[str, Any] = {}
        # Confirmation given for the pending input; cleared whenever the
        # circuit form is shown again.
        self._risks_accepted = False
        # Turning cooling off: the options to save once the circuits that
        # cool have stopped.
        self._pending_options: dict[str, Any] = {}
        self._cooling_circuits: list[HeatingCircuit] = []
        self._room_influence_circuits: list[HeatingCircuit] = []
        self._experimental_functions: list[str] = []

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "general",
                *(hc.key for hc in present_circuits(self.config_entry.options)),
                "experimental",
            ],
        )

    async def async_step_experimental(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Turns experimental functions on, which are then set up where they
        belong, e.g. in a heating circuit's form. Turning one off can need a
        confirmation first (see _async_finish_experimental)."""
        coordinator: HovalModbusCoordinator | None = self.hass.data.get(
            DOMAIN, {}
        ).get(self.config_entry.entry_id)
        if coordinator is None:
            return self.async_abort(reason="not_loaded")
        # With a buffer module, the flow setpoint raise isn't offered (Hoval).
        try:
            buffer_modules = await async_read_buffers(coordinator)
        except UpdateFailed:
            return self.async_abort(reason="cannot_read")
        buffer_module = buffer_modules[CONF_BUFFER_MODULE]
        self._experimental_functions = [
            CONF_EXPERIMENTAL_COOLING,
            CONF_EXPERIMENTAL_ROOM_INFLUENCE,
            *([] if buffer_module else [CONF_EXPERIMENTAL_FLOW_SETPOINT_RAISE]),
            CONF_EXPERIMENTAL_CONSTANT_CONTROL,
        ]

        if user_input is None:
            return self._async_show_experimental()

        old = self.config_entry.options
        options = {**old, **user_input, **buffer_modules}
        if buffer_module:
            options[CONF_EXPERIMENTAL_FLOW_SETPOINT_RAISE] = False

        self._room_influence_circuits = []
        if room_influence_enabled(old) and not room_influence_enabled(options):
            # Sending has to be stopped in the circuit's form first, where
            # leaving room influence without a room temperature is refused.
            sending = [
                hc
                for hc in HEATING_CIRCUITS
                if options.get(current_temp_source_key(hc))
                == CURRENT_TEMP_SOURCE_ENTITY_WRITE
            ]
            if sending:
                return self._async_show_experimental(
                    {"base": "room_influence_sending"}, sending
                )
            # Strategies the integration changed are set back, after a
            # confirmation.
            self._room_influence_circuits = [
                hc for hc in HEATING_CIRCUITS if previous_strategy_key(hc) in options
            ]

        self._cooling_circuits = []
        # Turning cooling off while a circuit cools turns its cooling off
        # first, after a confirmation.
        if cooling_enabled(old) and not cooling_enabled(options):
            try:
                self._cooling_circuits = await self._async_cooling_circuits(
                    coordinator
                )
            except UpdateFailed:
                return self.async_abort(reason="cannot_read")

        self._pending_options = options
        return await self._async_finish_experimental(coordinator)

    @callback
    def _async_show_experimental(
        self,
        errors: dict[str, str] | None = None,
        circuits: list[HeatingCircuit] | None = None,
    ) -> FlowResult:
        return self.async_show_form(
            step_id="experimental",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        key,
                        default=bool(self.config_entry.options.get(key, False)),
                    ): bool
                    for key in self._experimental_functions
                }
            ),
            errors=errors or {},
            description_placeholders={"circuits": _circuit_numbers(circuits or [])},
        )

    async def _async_finish_experimental(
        self, coordinator: HovalModbusCoordinator
    ) -> FlowResult:
        """Asks for the confirmations still needed, one after the other, then
        updates the raises and saves the pending options."""
        if self._room_influence_circuits:
            return await self.async_step_confirm_room_influence_off()
        if self._cooling_circuits:
            return await self.async_step_confirm_cooling_off()
        try:
            await self._async_restore_raise(coordinator, self._pending_options)
        except UpdateFailed as err:
            _LOGGER.warning("Updating the flow setpoint raise failed: %s", err)
            return self._async_show_experimental({"base": "write_failed"})
        return self.async_create_entry(title="", data=self._pending_options)

    async def async_step_confirm_room_influence_off(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Sets back the control strategies the integration changed to switch
        room influence on, and checks them, before room influence is turned
        off."""
        errors: dict[str, str] = {}
        if user_input is not None:
            coordinator = self.hass.data.get(DOMAIN, {}).get(
                self.config_entry.entry_id
            )
            if coordinator is None:
                return self.async_abort(reason="not_loaded")
            try:
                for hc in list(self._room_influence_circuits):
                    previous = self._pending_options[previous_strategy_key(hc)]
                    await coordinator.async_write_value(
                        hc.control_strategy_reg, previous, refresh=False
                    )
                    if (
                        int(await coordinator.async_read_value(hc.control_strategy_reg))
                        != previous
                    ):
                        raise UpdateFailed(
                            f"{hc.key}: the control strategy doesn't read {previous}"
                        )
                    del self._pending_options[previous_strategy_key(hc)]
                    self._pending_options[control_strategy_key(hc)] = previous
                    self._room_influence_circuits.remove(hc)
            except UpdateFailed as err:
                _LOGGER.warning("Turning room influence off failed: %s", err)
                errors["base"] = "write_failed"
            else:
                return await self._async_finish_experimental(coordinator)

        return self.async_show_form(
            step_id="confirm_room_influence_off",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders={
                "circuits": _circuit_numbers(self._room_influence_circuits)
            },
        )

    async def _async_restore_raise(
        self, coordinator: HovalModbusCoordinator, options: dict[str, Any]
    ) -> None:
        """Remembers or writes back the circuits' raises for the options about
        to be saved (see async_sync_flow_setpoint_raise). Raises
        UpdateFailed."""
        await async_sync_flow_setpoint_raise(
            coordinator, options, coordinator.flow_setpoint_raise
        )

    async def _async_cooling_circuits(
        self, coordinator: HovalModbusCoordinator
    ) -> list[HeatingCircuit]:
        """The circuits whose cooling flow setpoint is above 0, i.e. that
        cool. Raises UpdateFailed."""
        return [
            hc
            for hc in present_circuits(self.config_entry.options)
            if await coordinator.async_read_value(hc.cooling_flow_setpoint_reg) > 0
        ]

    async def async_step_confirm_cooling_off(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Turns cooling off on the circuits that cool, checks that it is off,
        and only then saves turning the experimental function off."""
        errors: dict[str, str] = {}
        if user_input is not None:
            coordinator = self.hass.data.get(DOMAIN, {}).get(
                self.config_entry.entry_id
            )
            if coordinator is None:
                return self.async_abort(reason="not_loaded")
            try:
                for hc in self._cooling_circuits:
                    await coordinator.async_write_value(
                        hc.cooling_flow_setpoint_reg, 0, refresh=False
                    )
                if await self._async_cooling_circuits(coordinator):
                    raise UpdateFailed("a cooling flow setpoint doesn't read 0")
            except UpdateFailed as err:
                _LOGGER.warning("Turning cooling off failed: %s", err)
                errors["base"] = "write_failed"
            else:
                self._cooling_circuits = []
                return await self._async_finish_experimental(coordinator)

        return self.async_show_form(
            step_id="confirm_cooling_off",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders={
                "circuits": _circuit_numbers(self._cooling_circuits)
            },
        )

    async def async_step_general(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        placeholders = {"circuits": ""}
        if user_input is not None:
            options = {**self.config_entry.options, **user_input}
            # A circuit that sends a room temperature has to stop first, in
            # its own form.
            sending = [
                hc
                for hc in present_circuits(self.config_entry.options)
                if not circuit_present(options, hc)
                and options.get(current_temp_source_key(hc))
                == CURRENT_TEMP_SOURCE_ENTITY_WRITE
            ]
            if not sending:
                await self._async_detect_dhw_bottom_sensor(options)
                if heat_pump_controller(options) != heat_pump_controller(
                    self.config_entry.options
                ):
                    update_meter_sensors(
                        self.hass, self.config_entry, meters_present(options)
                    )
                return self.async_create_entry(title="", data=options)
            errors["base"] = "room_influence_sending"
            placeholders["circuits"] = _circuit_numbers(sending)

        options = self.config_entry.options
        schema: dict[Any, Any] = {
            vol.Required(
                CONF_HEAT_PUMP_CONTROLLER, default=heat_pump_controller(options)
            ): CONTROLLER_SELECTOR,
        }
        for hc in HEATING_CIRCUITS:
            if hc is not HEATING_CIRCUIT_1:
                schema[
                    vol.Required(
                        circuit_present_key(hc), default=circuit_present(options, hc)
                    )
                ] = bool
        schema[vol.Required(CONF_DHW_PRESENT, default=dhw_present(options))] = bool
        return self.async_show_form(
            step_id="general",
            data_schema=vol.Schema(schema),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def _async_detect_dhw_bottom_sensor(self, options: dict[str, Any]) -> None:
        """Looks for the hot water tank's bottom sensor again, e.g. one fitted
        later. Keeps what is stored if the controller can't be read."""
        coordinator: HovalModbusCoordinator | None = self.hass.data.get(
            DOMAIN, {}
        ).get(self.config_entry.entry_id)
        if coordinator is None or not dhw_present(options):
            return
        try:
            options.update(await async_read_dhw_bottom_sensor(coordinator))
        except UpdateFailed as err:
            _LOGGER.warning("Looking for the hot water bottom sensor failed: %s", err)

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
        options = self.config_entry.options
        current_raise: float | None = None
        try:
            strategy = int(await coordinator.async_read_value(hc.control_strategy_reg))
            room_temperature = await coordinator.async_read_value(
                hc.room_actual_temp_reg
            )
            # The flow setpoint raise, an experimental function, at a
            # weather-compensated strategy: its range is set here.
            offer_raise = (
                bool(options.get(CONF_EXPERIMENTAL_FLOW_SETPOINT_RAISE))
                and strategy in FLOW_SETPOINT_RAISE_STRATEGIES
                and not options.get(CONF_BUFFER_MODULE)
            )
            raise_reg = FLOW_SETPOINT_RAISE_REGISTERS[HEATING_CIRCUITS.index(hc)]
            if offer_raise:
                current_raise = await coordinator.async_read_value(raise_reg)
        except UpdateFailed:
            return self.async_abort(reason="cannot_read")

        current_source = options.get(
            current_temp_source_key(hc), DEFAULT_CURRENT_TEMP_SOURCE
        )
        sending = current_source == CURRENT_TEMP_SOURCE_ENTITY_WRITE
        # While sending, the register holds the integration's own value.
        sensor_found = not sending and has_room_sensor(room_temperature)
        # Sending and switching room influence are an experimental function;
        # without it, strategies 1 and 5 are left as they are, like 0 and 4.
        room_influence_on = room_influence_enabled(options)
        can_send = sending or (
            room_influence_on
            and not sensor_found
            and strategy in CONTROL_STRATEGY_WRITABLE
        )
        offer_room_influence = room_influence_on and strategy in CONTROL_STRATEGY_WRITABLE

        errors: dict[str, str] = {}
        if user_input is not None:
            source = user_input.get(FIELD_SOURCE, CURRENT_TEMP_SOURCE_NONE)
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
            if offer_raise and user_input.get(
                FIELD_RAISE_MIN, DEFAULT_FLOW_SETPOINT_RAISE_MIN
            ) > user_input.get(FIELD_RAISE_MAX, DEFAULT_FLOW_SETPOINT_RAISE_MAX):
                errors["base"] = "raise_bounds"

            room_influence = (
                user_input.get(FIELD_ROOM_INFLUENCE, False)
                if offer_room_influence
                else None
            )
            influence_after = (
                room_influence
                if room_influence is not None
                else room_influence_on and strategy in WITH_INFLUENCE_TO_WITHOUT
            )
            # With room influence, the room register must hold a room
            # temperature: without a sensor and with nothing sent, it holds 0,
            # and how the controller takes 0 is not known.
            if (
                not errors
                and influence_after
                and source != CURRENT_TEMP_SOURCE_ENTITY_WRITE
                and not sensor_found
            ):
                errors["base"] = "room_influence_without_temperature"

            # Only a value that is sent and included in the control strategy
            # lets Home Assistant drive the controller.
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
                    # Saving the circuit is how a control strategy changed
                    # later is found, e.g. for cooling or constant control.
                    options.update(
                        await async_read_circuit_options(coordinator, [hc])
                    )
                    if offer_raise:
                        for field, key in zip(
                            (FIELD_RAISE_MIN, FIELD_RAISE_MAX),
                            flow_setpoint_raise_bound_keys(hc),
                        ):
                            if field in user_input:
                                options[key] = user_input[field]
                    await self._async_restore_raise(coordinator, options)
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

        schema: dict[Any, Any] = {}
        # With constant control there's no climate entity to show a room
        # temperature, and the controller doesn't use one.
        if strategy != CONTROL_STRATEGY_CONSTANT:
            schema[vol.Required(FIELD_SOURCE, default=suggested_source)] = (
                selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=source_options,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                        translation_key="current_temp_source",
                    )
                )
            )
            # Always shown, but only relevant for the entity-based sources.
            schema[vol.Optional(FIELD_ENTITY, **entity_field_kwargs)] = (
                selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain="sensor", device_class="temperature"
                    )
                )
            )
        if offer_room_influence:
            schema[
                vol.Optional(
                    FIELD_ROOM_INFLUENCE,
                    default=previous_input.get(
                        FIELD_ROOM_INFLUENCE,
                        strategy in WITH_INFLUENCE_TO_WITHOUT,
                    ),
                )
            ] = bool
        if offer_raise:
            bound = selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=raise_reg.min_value,
                    max=raise_reg.max_value,
                    step=0.1,
                    unit_of_measurement="K",
                    mode=selector.NumberSelectorMode.BOX,
                )
            )
            for field, current in zip(
                (FIELD_RAISE_MIN, FIELD_RAISE_MAX),
                flow_setpoint_raise_bounds(options, hc),
            ):
                schema[
                    vol.Optional(field, default=previous_input.get(field, current))
                ] = bound

        self._risks_accepted = False
        return self.async_show_form(
            step_id="circuit",
            data_schema=vol.Schema(schema),
            errors=errors,
            description_placeholders={
                "circuit": hc.key.removeprefix("heating_circuit_"),
                "strategy": await self._async_strategy_name(strategy),
                "room_temperature": self._format_temperature(room_temperature),
                "current_raise": (
                    "–"
                    if current_raise is None
                    else self._format_temperature(current_raise)
                ),
            },
        )

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

            if room_influence and strategy in WITHOUT_INFLUENCE_TO_WITH:
                new_strategy = WITHOUT_INFLUENCE_TO_WITH[strategy]
                await coordinator.async_write_value(
                    hc.control_strategy_reg, new_strategy, refresh=False
                )
                options[previous_key] = strategy
            elif room_influence is False and strategy in WITH_INFLUENCE_TO_WITHOUT:
                new_strategy = WITH_INFLUENCE_TO_WITHOUT[strategy]
                await coordinator.async_write_value(
                    hc.control_strategy_reg, new_strategy, refresh=False
                )
        except UpdateFailed:
            if leaving:
                writer.async_start()
            raise

        if new_strategy not in WITH_INFLUENCE_TO_WITHOUT:
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


def _circuit_numbers(circuits: list[HeatingCircuit]) -> str:
    """The circuits' numbers for a message, e.g. "1, 3"."""
    return ", ".join(hc.key.removeprefix("heating_circuit_") for hc in circuits)
