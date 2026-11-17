"""DataUpdateCoordinator: handles the Modbus connection, polling and writes."""
from __future__ import annotations

import asyncio
import logging
import struct
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from datetime import timedelta
from typing import Any

from homeassistant.components import modbus
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from modbus_connection import (
    ModbusConnectionError,
    ModbusDesyncError,
    ModbusError,
    ModbusExceptionError,
    ModbusTcpParams,
    ModbusTimeoutError,
    ModbusUnit,
)

from .const import (
    DataType,
    RegisterDef,
    RegisterType,
    SENSOR_REGISTERS,
    NUMBER_REGISTERS,
    DHW_REGISTERS,
    STATUS_REGISTERS,
    HEATING_CIRCUIT_REGISTERS,
    SMART_GRID_REGISTERS,
)

_LOGGER = logging.getLogger(__name__)

ALL_REGISTERS: list[RegisterDef] = (
    SENSOR_REGISTERS
    + NUMBER_REGISTERS
    + DHW_REGISTERS
    + STATUS_REGISTERS
    + HEATING_CIRCUIT_REGISTERS
    + SMART_GRID_REGISTERS
)

# Modbus allows at most 125 registers per read request.
MAX_BLOCK_REGISTERS = 125

# Repeating a request after a transient error. The gateway stops answering
# on a connection that has been idle for about 20-30 seconds and drops it
# only a few requests later; just a new connection gets answers again.
# - No answer in time or a late answer to an earlier request
#   (FAILED_ANSWER_ERRORS) uses up one of REQUEST_ATTEMPTS. The connection
#   is dropped right away, so the next try opens a new one instead of
#   waiting on a connection that may be dead. The connection is shared with
#   other integrations using the same gateway; theirs reconnect as well.
# - A dropped connection (ModbusConnectionError) doesn't use up an attempt,
#   so the request is still repeated on the new connection; at most
#   MAX_LOST_CONNECTIONS times.
# The controller's own error responses (ModbusExceptionError) are final and
# not repeated.
FAILED_ANSWER_ERRORS = (ModbusTimeoutError, ModbusDesyncError)
REQUEST_ATTEMPTS = 3
MAX_LOST_CONNECTIONS = 2
RETRY_DELAY = 1.0  # seconds


@dataclass(frozen=True)
class ReadBlock:
    """A run of adjacent registers that is read with a single request."""

    register_type: RegisterType
    address: int
    count: int
    registers: tuple[RegisterDef, ...]


def build_read_blocks(registers: list[RegisterDef]) -> list[ReadBlock]:
    """Groups registers with adjacent or overlapping addresses into blocks.

    Blocks never span a gap between registers, so they only cover addresses
    that are part of the register map.
    """
    blocks: list[ReadBlock] = []
    group: list[RegisterDef] = []
    group_end = 0

    def flush() -> None:
        if group:
            blocks.append(
                ReadBlock(
                    register_type=group[0].register_type,
                    address=group[0].address,
                    count=group_end - group[0].address,
                    registers=tuple(group),
                )
            )

    for reg in sorted(registers, key=lambda r: (r.register_type, r.address)):
        end = reg.address + reg.register_count
        if (
            group
            and reg.register_type == group[0].register_type
            and reg.address <= group_end
            and max(end, group_end) - group[0].address <= MAX_BLOCK_REGISTERS
        ):
            group.append(reg)
            group_end = max(group_end, end)
        else:
            flush()
            group = [reg]
            group_end = end
    flush()
    return blocks


def decode_value(registers: list[int], data_type: DataType) -> float:
    """Converts raw 16-bit registers into a numeric value (big-endian)."""
    if data_type == "int16":
        value = registers[0]
        return value - 65536 if value > 32767 else value
    if data_type == "uint16":
        return registers[0]
    if data_type in ("int32", "uint32"):
        raw = (registers[0] << 16) + registers[1]
        if data_type == "int32" and raw > 0x7FFFFFFF:
            raw -= 0x100000000
        return raw
    if data_type == "float32":
        packed = struct.pack(">HH", registers[0], registers[1])
        return struct.unpack(">f", packed)[0]
    raise ValueError(f"Unknown data_type: {data_type}")


def encode_value(value: float, data_type: DataType) -> list[int]:
    """Converts a numeric value into a list of 16-bit registers."""
    if data_type == "int16":
        raw = int(round(value))
        if raw < 0:
            raw += 65536
        return [raw & 0xFFFF]
    if data_type == "uint16":
        return [int(round(value)) & 0xFFFF]
    if data_type in ("int32", "uint32"):
        raw = int(round(value))
        if raw < 0:
            raw += 0x100000000
        return [(raw >> 16) & 0xFFFF, raw & 0xFFFF]
    if data_type == "float32":
        packed = struct.pack(">f", float(value))
        high, low = struct.unpack(">HH", packed)
        return [high, low]
    raise ValueError(f"Unknown data_type: {data_type}")


async def _drop_connection(unit: ModbusUnit) -> None:
    """Drops the connection; the next request opens a new one."""
    try:
        await unit.disconnect()
    except ModbusConnectionError as err:
        # The connection is dropped regardless.
        _LOGGER.debug("Dropping the connection failed: %s", err)


async def _with_retries[T](
    unit: ModbusUnit, request: Callable[[], Awaitable[T]], description: str
) -> T:
    """Runs a request, repeating it after transient errors (see
    FAILED_ANSWER_ERRORS and MAX_LOST_CONNECTIONS above)."""
    failed_answers = 0
    lost_connections = 0
    while True:
        try:
            return await request()
        except FAILED_ANSWER_ERRORS as err:
            failed_answers += 1
            await _drop_connection(unit)
            if failed_answers >= REQUEST_ATTEMPTS:
                raise
            _LOGGER.debug(
                "%s failed (no answer %s of %s), retrying on a new connection: %s",
                description,
                failed_answers,
                REQUEST_ATTEMPTS,
                err,
            )
        except ModbusConnectionError as err:
            lost_connections += 1
            if lost_connections > MAX_LOST_CONNECTIONS:
                raise
            _LOGGER.debug(
                "%s failed (connection lost %s of %s), retrying on a new connection: %s",
                description,
                lost_connections,
                MAX_LOST_CONNECTIONS,
                err,
            )
        await asyncio.sleep(RETRY_DELAY)


async def read_registers(
    unit: ModbusUnit, register_type: RegisterType, address: int, count: int
) -> list[int]:
    """Reads raw registers. Raises ModbusError on failure."""
    if register_type == "holding":
        request = partial(unit.read_holding_registers, address, count)
    else:
        request = partial(unit.read_input_registers, address, count)
    return await _with_retries(unit, request, f"Reading {count} registers at {address}")


async def read_register(unit: ModbusUnit, reg: RegisterDef) -> float:
    """Reads a single register's value. Raises ModbusError on failure."""
    registers = await read_registers(
        unit, reg.register_type, reg.address, reg.register_count
    )
    return decode_value(registers, reg.data_type) * reg.scale


async def write_register(unit: ModbusUnit, reg: RegisterDef, value: float) -> None:
    """Writes a holding register's value. Raises ModbusError on failure.

    Repeating a write is safe: it sets the same value again."""
    registers = encode_value(value / reg.scale, reg.data_type)
    if len(registers) == 1:
        request = partial(unit.write_register, reg.address, registers[0])
    else:
        request = partial(unit.write_registers, reg.address, registers)
    await _with_retries(unit, request, f"Writing {reg.key} at {reg.address}")


# HA-COMPAT(2026.10): always true from 2026.10; remove and call
# require_timeout unconditionally.
def timeout_supported() -> bool:
    """Whether Home Assistant's Modbus connection takes a request timeout."""
    return hasattr(ModbusUnit, "require_timeout")


class HovalModbusCoordinator(DataUpdateCoordinator[dict[str, float]]):
    """Periodically reads all configured registers and provides them to the entities."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        name: str,
        host: str,
        port: int,
        device_id: int,
        scan_interval: int,
        timeout: float | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=name,
            update_interval=timedelta(seconds=scan_interval),
        )
        self.device_name = name
        self.host = host
        self.port = port
        self.device_id = device_id
        # The connection is shared with other integrations using the same
        # gateway and released by Home Assistant when the entry unloads.
        self.unit = modbus.async_get_unit(
            hass, entry, ModbusTcpParams(host=host, port=port), device_id
        )
        if timeout is not None and timeout_supported():
            self.unit.require_timeout(timeout)
            entry.async_on_unload(lambda: self.unit.require_timeout(None))
        # Heating circuit key -> RoomTempWriter (room_temp.py), for the
        # circuits sending a room temperature to the controller.
        self.room_temp_writers: dict[str, Any] = {}
        # Heating circuit key -> cooling flow setpoint chosen by the user
        # (number.py), written when the circuit is set to cool (climate.py).
        self.cooling_flow_setpoints: dict[str, float] = {}

    async def _async_update_data(self) -> dict[str, float]:
        data: dict[str, float] = {}

        # Only poll registers a currently-subscribed entity actually needs.
        # Each CoordinatorEntity passes its register keys as `context` when
        # subscribing (super().__init__(coordinator, context=[...])), so
        # async_contexts() reflects exactly what's currently enabled and
        # added to hass -- disabled entities never subscribe, so their
        # registers are skipped automatically, and enabling one later picks
        # its registers back up on the next poll without a reload.
        #
        # Fallback: an empty needed_keys (e.g. the very first refresh in
        # __init__.py, which runs before entities are set up) means "poll
        # everything" -- not knowing what's needed should never mean
        # fetching nothing.
        needed_keys: set[str] = set()
        for context in self.async_contexts():
            needed_keys.update(context)

        registers_to_poll = (
            [reg for reg in ALL_REGISTERS if reg.key in needed_keys]
            if needed_keys
            else ALL_REGISTERS
        )

        for block in build_read_blocks(registers_to_poll):
            try:
                try:
                    registers = await read_registers(
                        self.unit, block.register_type, block.address, block.count
                    )
                except ModbusExceptionError as err:
                    # The controller answered with an error code, e.g. for a
                    # register it doesn't have.
                    if len(block.registers) == 1:
                        self._warn_unreadable(block.registers[0], err)
                        continue
                    # Read the registers one by one so a single unreadable
                    # register doesn't hide the others in the block.
                    _LOGGER.debug(
                        "Block read at address %s (%s registers) failed: %s",
                        block.address,
                        block.count,
                        err,
                    )
                    for reg in block.registers:
                        try:
                            single = await read_registers(
                                self.unit,
                                reg.register_type,
                                reg.address,
                                reg.register_count,
                            )
                        except ModbusExceptionError as single_err:
                            self._warn_unreadable(reg, single_err)
                            continue
                        self._store_value(data, reg, single)
                    continue
            except ModbusError as err:
                raise UpdateFailed(
                    f"Modbus error while reading address {block.address}: {err}"
                ) from err

            for reg in block.registers:
                offset = reg.address - block.address
                self._store_value(
                    data, reg, registers[offset : offset + reg.register_count]
                )

        return data

    @staticmethod
    def _warn_unreadable(reg: RegisterDef, err: ModbusExceptionError) -> None:
        _LOGGER.warning("Error reading %s (address %s): %s", reg.key, reg.address, err)

    @staticmethod
    def _store_value(data: dict[str, float], reg: RegisterDef, registers: list[int]) -> None:
        data[reg.key] = decode_value(registers, reg.data_type) * reg.scale

    async def async_read_value(self, reg: RegisterDef) -> float:
        """Reads a register directly, independent of what is being polled."""
        try:
            return await read_register(self.unit, reg)
        except ModbusError as err:
            raise UpdateFailed(f"Modbus error while reading {reg.key}: {err}") from err

    async def async_write_value(
        self, reg: RegisterDef, value: float, refresh: bool = True
    ) -> None:
        """Writes a value to a holding register and, unless told otherwise,
        refreshes the data afterwards."""
        try:
            await write_register(self.unit, reg, value)
        except ModbusError as err:
            raise UpdateFailed(f"Modbus error while writing {reg.key}: {err}") from err

        if refresh:
            await self.async_request_refresh()
