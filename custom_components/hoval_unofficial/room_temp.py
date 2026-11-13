"""Sends a Home Assistant temperature entity to a heating circuit's room register."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    CONF_HOST,
    CONF_PORT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTemperature,
)
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, State, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import (
    EventStateChangedData,
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import TemperatureConverter

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException

from .const import (
    CONF_DEVICE_ID,
    CONTROL_STRATEGY_ROOM_INFLUENCE,
    CONTROL_STRATEGY_WRITABLE,
    CURRENT_TEMP_SOURCE_ENTITY_WRITE,
    DOMAIN,
    HEATING_CIRCUITS,
    HeatingCircuit,
    current_temp_source_key,
    previous_strategy_key,
)
from .coordinator import HovalModbusCoordinator, read_register, write_register

_LOGGER = logging.getLogger(__name__)

WRITE_INTERVAL = timedelta(minutes=10)
MIN_SECONDS_BETWEEN_WRITES = 60
FALLBACK_DELAY_SECONDS = 5 * 60
SOURCE_UNAVAILABLE_ISSUE_DELAY_SECONDS = 30 * 60
# Many wireless sensors keep their last value instead of becoming unavailable.
SOURCE_STALE_AFTER = timedelta(hours=2)

# Range of the controller's room register that counts as a sensor reading.
ROOM_TEMP_MIN = -20.0
ROOM_TEMP_MAX = 50.0
# Range of a Home Assistant temperature that is sent to the controller.
SOURCE_TEMP_MIN = 5.0
SOURCE_TEMP_MAX = 40.0

# The gateway can refuse a connection opened right after another one was
# closed, which is exactly the situation when an entry is removed.
REMOVE_ATTEMPTS = 3
REMOVE_RETRY_SECONDS = 3


def has_room_sensor(room_temperature: float) -> bool:
    """Whether a room register value comes from a sensor (0 means no sensor)."""
    return room_temperature != 0 and ROOM_TEMP_MIN <= room_temperature <= ROOM_TEMP_MAX


def parse_room_temperature(state: State | None) -> float | None:
    """Returns a state's temperature in °C, or None if it isn't usable."""
    if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
        return None
    try:
        value = float(state.state)
    except (TypeError, ValueError):
        return None

    unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT, UnitOfTemperature.CELSIUS)
    if unit not in TemperatureConverter.VALID_UNITS:
        return None
    value = TemperatureConverter.convert(value, unit, UnitOfTemperature.CELSIUS)
    return round(value, 1) if SOURCE_TEMP_MIN <= value <= SOURCE_TEMP_MAX else None


def _source_unavailable_issue_id(entry: ConfigEntry, hc: HeatingCircuit) -> str:
    return f"room_temp_source_unavailable_{entry.entry_id}_{hc.key}"


class RoomTempWriter:
    """Keeps one heating circuit's room register in sync with a HA entity.

    Writes on every change of at least the register's resolution, at most
    once per MIN_SECONDS_BETWEEN_WRITES (a change within that time is written
    when it has passed), and in any case every WRITE_INTERVAL. The control
    strategy is read before each write: nothing is sent unless it is one of
    CONTROL_STRATEGY_WRITABLE.

    If the entity has no usable value for FALLBACK_DELAY_SECONDS, or hasn't
    reported for SOURCE_STALE_AFTER, the circuit's room setpoint is sent
    instead while the strategy includes room influence. A deviation of 0 means
    no correction, so the controller then acts as if it were weather
    compensated only. The controller itself keeps the last value it received.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: HovalModbusCoordinator,
        entry: ConfigEntry,
        heating_circuit: HeatingCircuit,
        entity_id: str,
    ) -> None:
        self._hass = hass
        self._coordinator = coordinator
        self._entry = entry
        self._hc = heating_circuit
        self._entity_id = entity_id
        self._lock = asyncio.Lock()
        self._unsubscribers: list[CALLBACK_TYPE] = []
        self._cancel_deferred_write: CALLBACK_TYPE | None = None
        self._cancel_fallback_timer: CALLBACK_TYPE | None = None
        self._cancel_unavailable_timer: CALLBACK_TYPE | None = None
        self._stopped = True
        self._last_attempt = 0.0
        self._last_target: float | None = None
        self._warned = False
        self._issue_raised = False
        self.fallback_active = False
        self.last_written_value: float | None = None
        self.last_written_at: datetime | None = None
        self.blocked_strategy: int | None = None

    @callback
    def async_start(self) -> None:
        self._stopped = False
        self._unsubscribers = [
            async_track_state_change_event(
                self._hass, [self._entity_id], self._handle_state_change
            ),
            async_track_time_interval(
                self._hass, self._handle_interval, WRITE_INTERVAL
            ),
            # Keeps the room setpoint polled, which the fallback sends.
            self._coordinator.async_add_listener(
                self._handle_coordinator_update,
                context=[self._hc.read_setpoint_reg.key],
            ),
        ]
        self._evaluate(force=True)

    @callback
    def async_stop(self) -> None:
        self._stopped = True
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers = []
        self._cancel_deferred()
        self._source_usable()

    @callback
    def _handle_state_change(self, event: Event[EventStateChangedData]) -> None:
        self._evaluate(force=False)

    @callback
    def _handle_interval(self, now: datetime) -> None:
        self._evaluate(force=True)

    @callback
    def _handle_coordinator_update(self) -> None:
        # A changed setpoint has to reach the controller right away, or it
        # sees a deviation that the fallback is meant to prevent.
        if self.fallback_active and self._setpoint() != self._last_target:
            self._evaluate(force=True)

    @callback
    def _handle_deferred(self, now: datetime) -> None:
        self._cancel_deferred_write = None
        self._evaluate(force=False)

    @callback
    def _cancel_deferred(self) -> None:
        if self._cancel_deferred_write is not None:
            self._cancel_deferred_write()
            self._cancel_deferred_write = None

    def _setpoint(self) -> float | None:
        value = self._coordinator.data.get(self._hc.read_setpoint_reg.key)
        if value is None or not SOURCE_TEMP_MIN <= value <= SOURCE_TEMP_MAX:
            return None
        return round(value, 1)

    def _source_value(self) -> float | None:
        state = self._hass.states.get(self._entity_id)
        if state is None or dt_util.utcnow() - state.last_reported > SOURCE_STALE_AFTER:
            return None
        return parse_room_temperature(state)

    @callback
    def _evaluate(self, force: bool) -> None:
        value = self._source_value()
        if value is None:
            self._source_unusable()
            if not self.fallback_active:
                return
            value = self._setpoint()
            if value is None:
                return
        else:
            if self.fallback_active:
                force = True
            self._source_usable()

        if not force:
            if value == self.last_written_value:
                self._cancel_deferred()
                return
            remaining = MIN_SECONDS_BETWEEN_WRITES - (
                time.monotonic() - self._last_attempt
            )
            if remaining > 0:
                if self._cancel_deferred_write is None:
                    self._cancel_deferred_write = async_call_later(
                        self._hass, remaining, self._handle_deferred
                    )
                return

        self._cancel_deferred()
        self._last_attempt = time.monotonic()
        self._last_target = value
        self._entry.async_create_background_task(
            self._hass,
            self._async_write(value, self.fallback_active),
            f"{DOMAIN} room temperature {self._hc.key}",
        )

    async def _async_write(self, value: float, fallback: bool) -> None:
        async with self._lock:
            if self._stopped:
                return
            try:
                strategy = int(
                    await self._coordinator.async_read_value(
                        self._hc.control_strategy_reg
                    )
                )
                if strategy not in CONTROL_STRATEGY_WRITABLE:
                    if self.blocked_strategy != strategy:
                        _LOGGER.warning(
                            "%s: not sending the room temperature, control "
                            "strategy is %s",
                            self._hc.key,
                            strategy,
                        )
                    self.blocked_strategy = strategy
                    return
                self.blocked_strategy = None
                # Without room influence the value has no effect anyway.
                if fallback and strategy != CONTROL_STRATEGY_ROOM_INFLUENCE:
                    return
                await self._coordinator.async_write_value(
                    self._hc.room_actual_temp_reg, value, refresh=False
                )
            except UpdateFailed as err:
                # Stopping closes the connection under a request in progress.
                if self._stopped:
                    return
                if not self._warned:
                    _LOGGER.warning(
                        "%s: could not send the room temperature: %s",
                        self._hc.key,
                        err,
                    )
                    self._warned = True
                if self._cancel_deferred_write is None:
                    self._cancel_deferred_write = async_call_later(
                        self._hass, MIN_SECONDS_BETWEEN_WRITES, self._handle_deferred
                    )
                return

            self._warned = False
            self.last_written_value = value
            self.last_written_at = dt_util.utcnow()

    @callback
    def _source_unusable(self) -> None:
        if not self.fallback_active and self._cancel_fallback_timer is None:
            self._cancel_fallback_timer = async_call_later(
                self._hass, FALLBACK_DELAY_SECONDS, self._start_fallback
            )
        if not self._issue_raised and self._cancel_unavailable_timer is None:
            self._cancel_unavailable_timer = async_call_later(
                self._hass, SOURCE_UNAVAILABLE_ISSUE_DELAY_SECONDS, self._raise_issue
            )

    @callback
    def _source_usable(self) -> None:
        if self._cancel_fallback_timer is not None:
            self._cancel_fallback_timer()
            self._cancel_fallback_timer = None
        if self.fallback_active:
            _LOGGER.info(
                "%s: %s is usable again, sending its temperature",
                self._hc.key,
                self._entity_id,
            )
            self.fallback_active = False
        if self._cancel_unavailable_timer is not None:
            self._cancel_unavailable_timer()
            self._cancel_unavailable_timer = None
        if self._issue_raised:
            ir.async_delete_issue(
                self._hass, DOMAIN, _source_unavailable_issue_id(self._entry, self._hc)
            )
            self._issue_raised = False

    @callback
    def _start_fallback(self, now: datetime) -> None:
        self._cancel_fallback_timer = None
        _LOGGER.warning(
            "%s: %s has no usable temperature, sending the room setpoint instead",
            self._hc.key,
            self._entity_id,
        )
        self.fallback_active = True
        self._evaluate(force=True)

    @callback
    def _raise_issue(self, now: datetime) -> None:
        self._cancel_unavailable_timer = None
        self._issue_raised = True
        ir.async_create_issue(
            self._hass,
            DOMAIN,
            _source_unavailable_issue_id(self._entry, self._hc),
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="room_temp_source_unavailable",
            translation_placeholders={
                "circuit": self._hc.key.removeprefix("heating_circuit_"),
                "entity_id": self._entity_id,
            },
        )

    def as_diagnostics(self) -> dict[str, Any]:
        return {
            "fallback_active": self.fallback_active,
            "last_written_value": self.last_written_value,
            "last_written_at": (
                self.last_written_at.isoformat() if self.last_written_at else None
            ),
            "blocked_by_control_strategy": self.blocked_strategy,
        }


async def async_cleanup_removed_entry(entry: ConfigEntry) -> None:
    """Undoes what the integration changed on the controller, as far as possible.

    Runs after the entry was unloaded, so it uses its own short-lived
    connection. Failing here must not block removing the entry.
    """
    circuits = [
        hc
        for hc in HEATING_CIRCUITS
        if entry.options.get(current_temp_source_key(hc))
        == CURRENT_TEMP_SOURCE_ENTITY_WRITE
        or previous_strategy_key(hc) in entry.options
    ]
    if not circuits:
        return

    device_id = entry.data[CONF_DEVICE_ID]
    client = AsyncModbusTcpClient(entry.data[CONF_HOST], port=entry.data[CONF_PORT])
    try:
        for attempt in range(1, REMOVE_ATTEMPTS + 1):
            try:
                if not await client.connect():
                    raise ModbusException("connection failed")
                for hc in circuits:
                    await _async_release_circuit(client, device_id, entry, hc)
                return
            except ModbusException as err:
                if attempt == REMOVE_ATTEMPTS:
                    _LOGGER.warning(
                        "Could not reset the room temperature settings on the "
                        "controller: %s",
                        err,
                    )
                    return
                client.close()
                await asyncio.sleep(REMOVE_RETRY_SECONDS)
    finally:
        client.close()


async def _async_release_circuit(
    client: AsyncModbusTcpClient,
    device_id: int,
    entry: ConfigEntry,
    hc: HeatingCircuit,
) -> None:
    strategy = int(await read_register(client, device_id, hc.control_strategy_reg))
    if strategy not in CONTROL_STRATEGY_WRITABLE:
        return
    if (
        entry.options.get(current_temp_source_key(hc))
        == CURRENT_TEMP_SOURCE_ENTITY_WRITE
    ):
        await write_register(client, device_id, hc.room_actual_temp_reg, 0)
    previous = entry.options.get(previous_strategy_key(hc))
    if previous is not None and strategy == CONTROL_STRATEGY_ROOM_INFLUENCE:
        await write_register(client, device_id, hc.control_strategy_reg, previous)
