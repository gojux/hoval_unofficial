"""DataUpdateCoordinator: handles the Modbus connection, polling and writes."""
from __future__ import annotations

import logging
import struct
from dataclasses import dataclass
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException

from .const import (
    DataType,
    RegisterDef,
    RegisterType,
    SENSOR_REGISTERS,
    NUMBER_REGISTERS,
    DHW_REGISTERS,
    STATUS_REGISTERS,
    HEATING_CIRCUIT_REGISTERS,
)

_LOGGER = logging.getLogger(__name__)

ALL_REGISTERS: list[RegisterDef] = (
    SENSOR_REGISTERS
    + NUMBER_REGISTERS
    + DHW_REGISTERS
    + STATUS_REGISTERS
    + HEATING_CIRCUIT_REGISTERS
)

# Modbus allows at most 125 registers per read request.
MAX_BLOCK_REGISTERS = 125


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


class HovalModbusCoordinator(DataUpdateCoordinator[dict[str, float]]):
    """Periodically reads all configured registers and provides them to the entities."""

    def __init__(
        self,
        hass: HomeAssistant,
        name: str,
        host: str,
        port: int,
        device_id: int,
        scan_interval: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=name,
            update_interval=timedelta(seconds=scan_interval),
        )
        self.device_name = name
        self.host = host
        self.port = port
        self.device_id = device_id
        self.client = AsyncModbusTcpClient(host, port=port)

    async def _ensure_connected(self) -> None:
        if not self.client.connected:
            await self.client.connect()
        if not self.client.connected:
            raise UpdateFailed(f"Could not connect to {self.host}:{self.port}")

    async def _async_update_data(self) -> dict[str, float]:
        await self._ensure_connected()
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
                result = await self._read_registers(
                    block.register_type, block.address, block.count
                )
                if not result.isError():
                    for reg in block.registers:
                        offset = reg.address - block.address
                        self._store_value(
                            data, reg, result.registers[offset : offset + reg.register_count]
                        )
                    continue

                if len(block.registers) > 1:
                    # Read the registers one by one so a single unreadable
                    # register doesn't hide the others in the block.
                    _LOGGER.debug(
                        "Block read at address %s (%s registers) failed: %s",
                        block.address,
                        block.count,
                        result,
                    )
                    for reg in block.registers:
                        single = await self._read_registers(
                            reg.register_type, reg.address, reg.register_count
                        )
                        if single.isError():
                            _LOGGER.warning(
                                "Error reading %s (address %s): %s",
                                reg.key,
                                reg.address,
                                single,
                            )
                            continue
                        self._store_value(data, reg, single.registers)
                else:
                    reg = block.registers[0]
                    _LOGGER.warning(
                        "Error reading %s (address %s): %s", reg.key, reg.address, result
                    )

            except ModbusException as err:
                raise UpdateFailed(
                    f"Modbus error while reading address {block.address}: {err}"
                ) from err

        return data

    async def _read_registers(self, register_type: RegisterType, address: int, count: int):
        if register_type == "holding":
            return await self.client.read_holding_registers(
                address, count=count, device_id=self.device_id
            )
        return await self.client.read_input_registers(
            address, count=count, device_id=self.device_id
        )

    @staticmethod
    def _store_value(data: dict[str, float], reg: RegisterDef, registers: list[int]) -> None:
        data[reg.key] = decode_value(registers, reg.data_type) * reg.scale

    async def async_write_value(self, reg: RegisterDef, value: float) -> None:
        """Writes a value to a holding register and refreshes the data afterwards."""
        await self._ensure_connected()
        raw_value = value / reg.scale
        registers = encode_value(raw_value, reg.data_type)

        try:
            if len(registers) == 1:
                result = await self.client.write_register(
                    reg.address, registers[0], device_id=self.device_id
                )
            else:
                result = await self.client.write_registers(
                    reg.address, registers, device_id=self.device_id
                )

            if result.isError():
                raise UpdateFailed(f"Error writing {reg.key}: {result}")

        except ModbusException as err:
            raise UpdateFailed(f"Modbus error while writing {reg.key}: {err}") from err

        await self.async_request_refresh()

    async def async_close(self) -> None:
        self.client.close()
