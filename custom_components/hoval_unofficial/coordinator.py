"""DataUpdateCoordinator: handles the Modbus connection, polling and writes."""
from __future__ import annotations

import asyncio
import logging
import struct
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from functools import partial
from datetime import timedelta
from typing import Any

from homeassistant.components import modbus
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
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
    BUFFER_MODULE_SENSOR_REGISTERS,
    SENSOR_REGISTERS,
    NUMBER_REGISTERS,
    DHW_REGISTERS,
    STATUS_REGISTERS,
    HEATING_CIRCUIT_REGISTERS,
    SMART_GRID_REGISTERS,
    SMART_GRID_MODE_MAP,
    SMART_GRID_MODE_PREFERRED_OPERATION,
    SMART_GRID_MODE_REGISTER,
)

_LOGGER = logging.getLogger(__name__)

ALL_REGISTERS: list[RegisterDef] = (
    SENSOR_REGISTERS
    + NUMBER_REGISTERS
    + DHW_REGISTERS
    + STATUS_REGISTERS
    + HEATING_CIRCUIT_REGISTERS
    + SMART_GRID_REGISTERS
    + [reg for regs in BUFFER_MODULE_SENSOR_REGISTERS.values() for reg, _ in regs]
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

# A register the controller answered with an error code for, or didn't
# answer for while it still answered other requests, is left out of the
# block reads and tried again on its own, with a single attempt, at most this
# often.
UNREADABLE_RETRY_SECONDS = 600

# Read after a request that wasn't answered, to tell whether the controller
# still answers at all: outdoor temperature and heat generator status, which
# every controller has. An answer to either counts, an error code as well.
# The second one is asked with a single attempt, for a controller that
# doesn't answer the first one itself.
PROBE_ADDRESSES = (1477, 1539)


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
    unit: ModbusUnit,
    request: Callable[[], Awaitable[T]],
    description: str,
    attempts: int = REQUEST_ATTEMPTS,
) -> T:
    """Runs a request, repeating it after transient errors (see
    FAILED_ANSWER_ERRORS and MAX_LOST_CONNECTIONS above), with `attempts`
    instead of REQUEST_ATTEMPTS if given."""
    failed_answers = 0
    lost_connections = 0
    while True:
        try:
            return await request()
        except FAILED_ANSWER_ERRORS as err:
            failed_answers += 1
            await _drop_connection(unit)
            if failed_answers >= attempts:
                raise
            _LOGGER.debug(
                "%s failed (no answer %s of %s), retrying on a new connection: %s",
                description,
                failed_answers,
                attempts,
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
    unit: ModbusUnit,
    register_type: RegisterType,
    address: int,
    count: int,
    attempts: int = REQUEST_ATTEMPTS,
) -> list[int]:
    """Reads raw registers. Raises ModbusError on failure."""
    if register_type == "holding":
        request = partial(unit.read_holding_registers, address, count)
    else:
        request = partial(unit.read_input_registers, address, count)
    return await _with_retries(
        unit, request, f"Reading {count} registers at {address}", attempts
    )


async def read_register(
    unit: ModbusUnit, reg: RegisterDef, attempts: int = REQUEST_ATTEMPTS
) -> float:
    """Reads a single register's value. Raises ModbusError on failure."""
    registers = await read_registers(
        unit, reg.register_type, reg.address, reg.register_count, attempts
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
        message_spacing: int = 0,
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
        # Only set when chosen, so a gap set elsewhere for this device ID
        # stays as it is otherwise.
        if message_spacing:
            self.unit.set_message_spacing(message_spacing / 1000)
            entry.async_on_unload(lambda: self.unit.set_message_spacing(0))
        # Heating circuit key -> RoomTempWriter (room_temp.py), for the
        # circuits sending a room temperature to the controller.
        self.room_temp_writers: dict[str, Any] = {}
        # Heating circuit key -> cooling flow setpoint chosen by the user
        # (number.py), written when the circuit is set to cool (climate.py).
        self.cooling_flow_setpoints: dict[str, float] = {}
        # FlowSetpointRaiseControl (raise_control.py) while the flow setpoint
        # raise of heating circuit 1 is set up.
        self.flow_setpoint_raise: Any = None
        # CoolingRaiseGuard (raise_control.py) while any circuit's flow
        # setpoint raise is offered.
        self.raise_guard: Any = None
        # Heating circuit key -> ConstantFlowControl (constant_control.py),
        # for the circuits on constant control while it is turned on.
        self.constant_controls: dict[str, Any] = {}
        # Unique IDs of the entities the platforms created on this start; the
        # disabled entities of this entry not among them are removed.
        self.provided_unique_ids: set[str] = set()
        # Key of each register the controller answered with an error code
        # for -> time.monotonic() of the last attempt to read it.
        self._unreadable: dict[str, float] = {}

    def add_entities(
        self, async_add_entities: AddEntitiesCallback, entities: Iterable[Entity]
    ) -> None:
        """Adds a platform's entities and remembers that they are provided."""
        entities = list(entities)
        self.provided_unique_ids.update(
            entity.unique_id for entity in entities if entity.unique_id
        )
        async_add_entities(entities)

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
        # Fallback: an empty needed_keys (the first refresh in __init__.py,
        # which runs before entities are set up) means the registers of the
        # entities enabled by default. __init__.py refreshes again right
        # after setting up the entities, for the ones enabled otherwise.
        needed_keys: set[str] = set()
        for context in self.async_contexts():
            needed_keys.update(context)

        registers_to_poll = (
            [reg for reg in ALL_REGISTERS if reg.key in needed_keys]
            if needed_keys
            else [reg for reg in ALL_REGISTERS if reg.enabled_default]
        )

        now = time.monotonic()
        readable = [reg for reg in registers_to_poll if reg.key not in self._unreadable]
        retry = [
            reg
            for reg in registers_to_poll
            if reg.key in self._unreadable
            and now - self._unreadable[reg.key] >= UNREADABLE_RETRY_SECONDS
        ]

        try:
            for block in build_read_blocks(readable):
                try:
                    registers = await read_registers(
                        self.unit, block.register_type, block.address, block.count
                    )
                except ModbusExceptionError as err:
                    # The controller answered with an error code, e.g. for a
                    # register it doesn't have.
                    await self._async_read_one_by_one(data, block, err)
                    continue
                except FAILED_ANSWER_ERRORS as err:
                    # No answer, not even on new connections. Some
                    # controllers don't answer at all for a register they
                    # don't have; the poll only fails if they don't answer
                    # other requests either.
                    await self._async_raise_unless_answering(err)
                    await self._async_read_one_by_one(data, block, err)
                    continue

                for reg in block.registers:
                    offset = reg.address - block.address
                    self._store_value(
                        data, reg, registers[offset : offset + reg.register_count]
                    )

            for reg in retry:
                await self._async_read_single(data, reg, attempts=1)
        except ModbusError as err:
            raise UpdateFailed(f"Modbus error while polling: {err}") from err

        return data

    async def _async_read_one_by_one(
        self, data: dict[str, float], block: ReadBlock, err: ModbusError
    ) -> None:
        """Reads a block's registers one by one after reading the block
        failed with `err`, so a single unreadable register doesn't hide the
        others in the block."""
        if len(block.registers) == 1:
            self._mark_unreadable(block.registers[0], err)
            return
        _LOGGER.debug(
            "Block read at address %s (%s registers) failed: %s",
            block.address,
            block.count,
            err,
        )
        for reg in block.registers:
            await self._async_read_single(data, reg)

    async def _async_read_single(
        self, data: dict[str, float], reg: RegisterDef, attempts: int = REQUEST_ATTEMPTS
    ) -> None:
        """Reads one register and keeps track of whether it is readable.
        Raises ModbusError unless the controller answered with an error code
        or still answers other requests."""
        try:
            registers = await read_registers(
                self.unit, reg.register_type, reg.address, reg.register_count, attempts
            )
        except ModbusExceptionError as err:
            self._mark_unreadable(reg, err)
            return
        except FAILED_ANSWER_ERRORS as err:
            await self._async_raise_unless_answering(err)
            self._mark_unreadable(reg, err)
            return
        if self._unreadable.pop(reg.key, None) is not None:
            _LOGGER.info("%s (address %s) is readable again", reg.key, reg.address)
        self._store_value(data, reg, registers)

    async def _async_raise_unless_answering(self, err: ModbusError) -> None:
        """After a request that wasn't answered (`err`), raises `err` unless
        the controller still answers other requests (see PROBE_ADDRESSES)."""
        for attempts, address in enumerate(PROBE_ADDRESSES, start=1):
            try:
                await read_registers(
                    self.unit, "holding", address, 1, REQUEST_ATTEMPTS if attempts == 1 else 1
                )
            except ModbusExceptionError:
                return
            except ModbusError as probe_err:
                _LOGGER.debug("No answer to the probe at %s: %s", address, probe_err)
                continue
            return
        _LOGGER.debug("The controller doesn't answer any request")
        raise err from None

    def _mark_unreadable(self, reg: RegisterDef, err: ModbusError) -> None:
        """Leaves a register out of the block reads (see
        UNREADABLE_RETRY_SECONDS)."""
        if reg.key not in self._unreadable:
            _LOGGER.warning(
                "Error reading %s (address %s): %s; trying again every %s s",
                reg.key,
                reg.address,
                err,
                UNREADABLE_RETRY_SECONDS,
            )
        else:
            _LOGGER.debug("%s still unreadable: %s", reg.key, err)
        self._unreadable[reg.key] = time.monotonic()

    @staticmethod
    def _store_value(data: dict[str, float], reg: RegisterDef, registers: list[int]) -> None:
        data[reg.key] = decode_value(registers, reg.data_type) * reg.scale

    async def async_read_value(self, reg: RegisterDef) -> float:
        """Reads a register directly, independent of what is being polled."""
        try:
            return await read_register(self.unit, reg)
        except ModbusError as err:
            raise UpdateFailed(f"Modbus error while reading {reg.key}: {err}") from err

    async def async_smart_grid_offset(self, offset_reg: RegisterDef) -> float:
        """The Smart Grid offset the controller currently adds to a base
        setpoint, read right away: the register's value while Smart Grid is
        in preferred operation, otherwise 0."""
        raw_mode = await self.async_read_value_if_present(SMART_GRID_MODE_REGISTER)
        if (
            raw_mode is None
            or SMART_GRID_MODE_MAP.get(int(raw_mode))
            != SMART_GRID_MODE_PREFERRED_OPERATION
        ):
            return 0.0
        offset = await self.async_read_value_if_present(offset_reg)
        return offset if offset is not None else 0.0

    async def async_read_value_if_present(
        self,
        reg: RegisterDef,
        unanswered_absent: bool = False,
        found_before: bool | None = None,
    ) -> float | None:
        """Reads a register directly; None if the controller answers that it
        doesn't have it, or, with `unanswered_absent`, doesn't answer for it
        while it still answers other requests (as in polling). Raises
        UpdateFailed on other errors.

        With `unanswered_absent`, `found_before` tells what is stored about
        the register, so that a single failed request doesn't make something
        found before count as missing:
        - True: always asked, with all attempts, also while polling leaves it
          out as unreadable (which a single error response causes).
        - False: asked with a single attempt, as not answering is expected;
          not at all while polling leaves it out.
        - None: asked with all attempts, unless polling leaves it out.
        """
        attempts = REQUEST_ATTEMPTS
        if unanswered_absent and not found_before:
            if reg.key in self._unreadable:
                return None
            if found_before is False:
                attempts = 1
        try:
            return await read_register(self.unit, reg, attempts)
        except ModbusExceptionError:
            return None
        except FAILED_ANSWER_ERRORS as err:
            if not unanswered_absent:
                raise UpdateFailed(f"Modbus error while reading {reg.key}: {err}") from err
            try:
                await self._async_raise_unless_answering(err)
            except ModbusError as probe_err:
                raise UpdateFailed(
                    f"Modbus error while reading {reg.key}: {probe_err}"
                ) from probe_err
            _LOGGER.debug("No answer for %s (address %s)", reg.key, reg.address)
            return None
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
