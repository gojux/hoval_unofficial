"""Minimal Modbus TCP server standing in for the heat pump controller in E2E tests.

All registers read as zero, which is a valid state for every entity the
integration creates.

Like some controllers do for registers they don't have, the server can
leave reads unanswered: writing a first and last address to SILENT_FIRST /
SILENT_LAST makes it never answer a read overlapping that range (0 / 0:
none).
"""
import asyncio

from pymodbus.datastore import ModbusDeviceContext, ModbusSequentialDataBlock, ModbusServerContext
from pymodbus.server import StartAsyncTcpServer

REGISTER_COUNT = 65536
UNIT_ID = 1
SILENT_FIRST = 65000
SILENT_LAST = 65001
READ_FUNCTION_CODES = (3, 4)


class DeviceContext(ModbusDeviceContext):
    """Device context leaving reads of the silent range unanswered."""

    async def async_getValues(self, func_code, address, count=1):
        if func_code in READ_FUNCTION_CODES:
            first, last = self.getValues(3, SILENT_FIRST, 2)
            if first and address <= last and address + count - 1 >= first:
                await asyncio.Event().wait()
        return await super().async_getValues(func_code, address, count)


async def main() -> None:
    device = DeviceContext(
        di=ModbusSequentialDataBlock(0, [0] * REGISTER_COUNT),
        co=ModbusSequentialDataBlock(0, [0] * REGISTER_COUNT),
        hr=ModbusSequentialDataBlock(0, [0] * REGISTER_COUNT),
        ir=ModbusSequentialDataBlock(0, [0] * REGISTER_COUNT),
    )
    context = ModbusServerContext(devices={UNIT_ID: device}, single=False)
    await StartAsyncTcpServer(context=context, address=("0.0.0.0", 502))


if __name__ == "__main__":
    asyncio.run(main())
