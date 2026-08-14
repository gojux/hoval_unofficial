"""DataUpdateCoordinator: handles the Modbus connection, polling and writes."""
from __future__ import annotations

import logging
import struct
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException

from .const import (
    DataType,
    RegisterDef,
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

        for reg in registers_to_poll:
            try:
                if reg.register_type == "holding":
                    result = await self.client.read_holding_registers(
                        reg.address, count=reg.register_count, device_id=self.device_id
                    )
                else:
                    result = await self.client.read_input_registers(
                        reg.address, count=reg.register_count, device_id=self.device_id
                    )

                if result.isError():
                    _LOGGER.warning(
                        "Error reading %s (address %s): %s", reg.key, reg.address, result
                    )
                    continue

                raw_value = decode_value(result.registers, reg.data_type)
                data[reg.key] = raw_value * reg.scale

            except ModbusException as err:
                raise UpdateFailed(f"Modbus error while reading {reg.key}: {err}") from err

        return data

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
