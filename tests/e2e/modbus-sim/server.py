"""Minimal Modbus TCP server standing in for the heat pump controller in E2E tests.

All registers read as zero, which is a valid state for every entity the
integration creates.
"""
import asyncio

from pymodbus.datastore import ModbusDeviceContext, ModbusSequentialDataBlock, ModbusServerContext
from pymodbus.server import StartAsyncTcpServer

REGISTER_COUNT = 65536
UNIT_ID = 1


async def main() -> None:
    device = ModbusDeviceContext(
        di=ModbusSequentialDataBlock(0, [0] * REGISTER_COUNT),
        co=ModbusSequentialDataBlock(0, [0] * REGISTER_COUNT),
        hr=ModbusSequentialDataBlock(0, [0] * REGISTER_COUNT),
        ir=ModbusSequentialDataBlock(0, [0] * REGISTER_COUNT),
    )
    context = ModbusServerContext(devices={UNIT_ID: device}, single=False)
    await StartAsyncTcpServer(context=context, address=("0.0.0.0", 502))


if __name__ == "__main__":
    asyncio.run(main())
