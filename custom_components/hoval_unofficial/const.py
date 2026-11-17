"""Constants for the Hoval Heat Pump Integration (unofficial)."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from homeassistant.components.water_heater import STATE_ECO
from homeassistant.components.climate import HVACMode, PRESET_ECO
from homeassistant.const import STATE_OFF

DOMAIN = "hoval_unofficial"

CONF_DEVICE_ID = "device_id"

DEFAULT_NAME = "Heat Pump"
DEFAULT_PORT = 502
DEFAULT_DEVICE_ID = 1
# The Hoval gateway stops answering on a connection that has been idle for
# about 20-30 seconds; a shorter interval keeps the connection alive.
DEFAULT_SCAN_INTERVAL = 20  # seconds
# Modbus request timeout, in seconds. A short timeout lets the retries
# (coordinator.py) get past a dead connection quickly; the gateway answers
# well within it.
DEFAULT_TIMEOUT = 3.0
MIN_TIMEOUT = 1.0
MAX_TIMEOUT = 60.0

RegisterType = Literal["holding", "input"]
DataType = Literal["int16", "uint16", "int32", "uint32", "float32"]


@dataclass(frozen=True)
class RegisterDef:
    """Describes a single Modbus register."""

    key: str                     # unique internal key
    name: str                    # display name in HA
    address: int                 # Modbus register address (0-based)
    register_type: RegisterType = "holding"  # "holding" or "input"
    data_type: DataType = "int16"
    scale: float = 1.0           # raw_value * scale = displayed value
    unit: str | None = None
    device_class: str | None = None
    state_class: str | None = "measurement"
    icon: str | None = None
    writable: bool = False       # only relevant for number/switch
    min_value: float | None = None
    max_value: float | None = None
    step: float | None = None
    precision: int | None = None  # suggested number of displayed decimals
    enabled_default: bool = True

    @property
    def register_count(self) -> int:
        """Number of 16-bit registers this value occupies."""
        return 2 if self.data_type in ("int32", "uint32", "float32") else 1


# --------------------------------------------------------------------------
# HOVAL SENSOR REGISTERS
#
# Real register map confirmed for a Hoval heat pump controller. If you're
# adapting this to a different Hoval model, double-check these against
# your own controller's Modbus documentation -- addresses can still vary
# by firmware/model.
# --------------------------------------------------------------------------

SENSOR_REGISTERS: list[RegisterDef] = [
    # --- Key temperatures (most commonly relevant at a glance) ---
    RegisterDef(
        key="outdoor_temp",
        name="Outdoor temperature",
        address=1477,
        data_type="int16",
        scale=0.1,
        unit="°C",
        device_class="temperature",
    ),
    RegisterDef(
        key="heat_generator_actual_temp",
        name="Heat generator actual temperature",
        address=18725,  # same register as 1525
        data_type="int16",
        scale=0.1,
        precision=1,
        unit="°C",
        device_class="temperature",
    ),
    RegisterDef(
        key="heat_generator_target_temp",
        name="Heat generator target temperature",
        address=18724,  # same register as 1531
        data_type="int16",
        scale=0.1,
        unit="°C",
        device_class="temperature",
    ),
    RegisterDef(
        key="return_temp_heat_generator",
        name="Return temperature heat generator",
        address=18742,  # same register as 1535
        data_type="int16",
        scale=0.1,
        precision=1,
        unit="°C",
        device_class="temperature",
    ),
    RegisterDef(
        key="buffer_temp",
        name="Buffer temperature",
        address=27493,
        data_type="int32",
        scale=0.1,
        unit="°C",
        device_class="temperature",
    ),

    # --- Efficiency / live power ---
    RegisterDef(
        key="cop",
        name="Coefficient of Performance (COP)",
        address=27490,
        data_type="uint16",
        scale=0.1,
        precision=1,
    ),
    RegisterDef(
        key="current_power_output",
        name="Current power output",
        address=18731,
        data_type="uint32",
        scale=0.1,
        precision=1,
        unit="kW",
        device_class="power",
    ),
    RegisterDef(
        key="current_cooling_power",
        name="Current cooling power",
        address=18735,
        data_type="uint32",
        scale=0.1,
        precision=1,
        unit="kW",
        device_class="power",
        enabled_default=False,
    ),
    RegisterDef(
        key="electrical_power_input_heat_generator",
        name="Current electrical power input heat generator",
        address=25611,
        data_type="int16",
        scale=0.01,
        precision=2,
        unit="kW",
        device_class="power",
    ),
    RegisterDef(
        key="modulation",
        name="Modulation",
        address=18726,
        data_type="uint16",
        precision=0,
        unit="%",
    ),
    # Registers of the heat pump's controller (WFA). The heat generator's own
    # flow rate and pump speed registers (18741, 18740) read 0 on a heat pump.
    RegisterDef(
        key="heat_pump_flow_rate",
        name="Heat pump flow rate",
        address=31707,  # averaged
        data_type="int32",
        scale=0.1,
        precision=1,
        unit="L/min",
        device_class="volume_flow_rate",
        enabled_default=False,
    ),
    RegisterDef(
        key="heat_pump_circulation_pump_speed",
        name="Heat pump circulation pump speed",
        address=27495,
        data_type="int32",
        precision=0,
        unit="%",
        enabled_default=False,
    ),

    # --- Heating circuit 1 diagnostics ---
    RegisterDef(
        key="flow_temp_hc1",
        name="Current flow temperature HC1",
        address=1513,
        data_type="int16",
        scale=0.1,
        unit="°C",
        device_class="temperature",
    ),
    RegisterDef(
        key="supply_target_temp_hc1",
        name="Supply target temperature HC1",
        address=19562,
        data_type="int16",
        scale=0.1,
        precision=1,
        unit="°C",
        device_class="temperature",
    ),
    RegisterDef(
        key="mixing_valve_hc1",
        name="Mixing valve HC1",
        address=19658,
        data_type="int16",
        precision=0,
        unit="%",
    ),

    # --- Cumulative energy meters ---
    RegisterDef(
        key="heat_quantity_heat_generator",
        name="Heat quantity heat generator",
        address=1505,
        data_type="uint32",
        scale=0.001,
        precision=3,
        unit="MWh",
        state_class="total_increasing",
        device_class="energy",
    ),
    RegisterDef(
        key="heat_quantity_heating",
        name="Heat quantity heating",
        address=27484,
        data_type="uint32",
        scale=0.001,
        precision=3,
        unit="MWh",
        state_class="total_increasing",
        device_class="energy",
    ),
    RegisterDef(
        key="heat_quantity_hot_water",
        name="Heat quantity hot water",
        address=27488,
        data_type="uint32",
        scale=0.001,
        precision=3,
        unit="MWh",
        state_class="total_increasing",
        device_class="energy",
    ),
    RegisterDef(
        key="cooling_energy",
        name="Cooling energy",
        address=18733,
        data_type="uint32",
        scale=0.001,
        precision=3,
        unit="MWh",
        state_class="total_increasing",
        device_class="energy",
        enabled_default=False,
    ),
    RegisterDef(
        key="total_electrical_energy_heat_generator",
        name="Total electrical energy heat generator",
        address=25613,
        data_type="uint32",
        scale=0.001,
        precision=3,
        unit="MWh",
        state_class="total_increasing",
        device_class="energy",
    ),

    # --- Usage counters (operating hours / switching cycles) ---
    RegisterDef(
        key="switching_cycles_heat_generator",
        name="Switching cycles heat generator",
        address=1518,
        data_type="uint32",
        precision=0,
        state_class="total_increasing",
    ),
    RegisterDef(
        key="switching_cycles_heat_generator_above_50",
        name="Switching cycles heat generator >50%",
        address=18729,
        data_type="uint32",
        precision=0,
        state_class="total_increasing",
    ),
    RegisterDef(
        key="operating_hours_heat_generator",
        name="Operating hours heat generator",
        address=1507,  # same as 1516
        data_type="uint32",
        precision=0,
        unit="h",
        state_class="total_increasing",
        device_class="duration",
    ),
    RegisterDef(
        key="operating_hours_heat_generator_above_50",
        name="Operating hours heat generator >50%",
        address=18727,
        data_type="uint32",
        precision=0,
        unit="h",
        state_class="total_increasing",
        device_class="duration",
    ),

    # --- Diagnostics / status codes (least everyday-relevant) ---
    RegisterDef(
        key="heat_generator_status_code",
        name="Heat generator status code",
        address=1539,  # same register as 18723 FA status
        data_type="uint16",
        enabled_default=False,
    ),
]

# Writable registers (e.g. setpoints) beyond the DHW/heating-circuit
# setpoints already handled by water_heater.py/climate.py, exposed as
# "number" entities. Assembled at the end of this file, once the Smart Grid
# offset registers below are defined.
NUMBER_REGISTERS: list[RegisterDef] = []

# --------------------------------------------------------------------------
# DHW (Domestic Hot Water) REGISTERS
#
# Used by the "water_heater" entity in water_heater.py. Again placeholder
# addresses -- adjust to your heat pump's actual Modbus map.
# --------------------------------------------------------------------------

DHW_CURRENT_TEMP_REGISTER = RegisterDef(
    key="dhw_current_temp",
    name="DHW Temperature",
    address=1500,
    data_type="int16",
    scale=0.1,
    unit="°C",
    device_class="temperature",
)

DHW_NORMAL_TARGET_TEMP_REGISTER = RegisterDef(
    key="dhw_normal_target_temp",
    name="DHW Normal Target Temperature",
    address=1497,
    data_type="int16",
    scale=0.1,
    unit="°C",
    device_class="temperature",
    writable=True,
    min_value=10.0,
    max_value=70.0,
    step=1.0,
)

DHW_ECO_TARGET_TEMP_REGISTER = RegisterDef(
    key="dhw_eco_target_temp",
    name="DHW Eco Target Temperature",
    address=1498,
    data_type="int16",
    scale=1.0,
    unit="°C",
    device_class="temperature",
    writable=True,
    min_value=10.0,
    max_value=70.0,
    step=1.0,
)

# We have to use a different read register for actual target temperature because
# it can be different from constant target temperature when set to other mode than contanst.
DHW_ACTUAL_TARGET_TEMP_REGISTER = RegisterDef(
    key="dhw_actual_target_temp",
    name="DHW Actual Target Temperature",
    address=1499,
    data_type="int16",
    scale=0.1,
    unit="°C",
    device_class="temperature",
)

DHW_MODE_REGISTER = RegisterDef(
    key="dhw_mode",
    name="DHW Mode",
    address=1496,
    data_type="uint16",
    scale=1.0,
    writable=True,
)

# All DHW registers, polled together with SENSOR_REGISTERS/NUMBER_REGISTERS
# by the coordinator.
DHW_REGISTERS: list[RegisterDef] = [
    DHW_CURRENT_TEMP_REGISTER,
    DHW_NORMAL_TARGET_TEMP_REGISTER,
    DHW_ECO_TARGET_TEMP_REGISTER,
    DHW_ACTUAL_TARGET_TEMP_REGISTER,
    DHW_MODE_REGISTER,
]

# Raw values expected in DHW_MODE_REGISTER. Adjust these to match your heat
# pump's actual Modbus map (e.g. some controllers use 0/1/2, others use bit
# flags or different codes entirely).
DHW_MODE_OFF = 0
DHW_MODE_CONSTANT = 4
DHW_MODE_WEEK1 = 1
DHW_MODE_WEEK2 = 2
DHW_MODE_ECO = 6

DHW_STATE_CONSTANT = "constant"
DHW_STATE_WEEK1 = "week_1"
DHW_STATE_WEEK2 = "week_2"


# DHW operating mode <-> Home Assistant water_heater operation mode mapping.
# "Aus" -> STATE_OFF, "Ein" -> STATE_PERFORMANCE (forced/continuous heating),
# "Zeitsteuerung" -> STATE_ECO (efficient, schedule-driven operation).
# These are standard HA water_heater states, so the frontend shows sensible
# translated labels and icons out of the box.
DHW_MODE_TO_OPERATION: dict[int, str] = {
    DHW_MODE_OFF: STATE_OFF,
    DHW_MODE_CONSTANT: DHW_STATE_CONSTANT,
    DHW_MODE_WEEK1: DHW_STATE_WEEK1,
    DHW_MODE_WEEK2: DHW_STATE_WEEK2,
    DHW_MODE_ECO: STATE_ECO,
}
OPERATION_TO_DHW_MODE: dict[str, int] = {
    operation: raw_value for raw_value, operation in DHW_MODE_TO_OPERATION.items()
}

# Room climate
CLIMATE_MODE_OFF = 0
CLIMATE_MODE_CONSTANT = 4
CLIMATE_MODE_WEEK1 = 1
CLIMATE_MODE_WEEK2 = 2
CLIMATE_MODE_ECO = 5

# HVACMode is a closed enum (unlike water_heater's free-form operation_list),
# so WEEK1/WEEK2/ECO can't each get their own hvac_mode -- they all collapse
# into HVACMode.AUTO ("the controller decides based on a schedule/program").
# Which specific program is active (week1 / week2 / eco) is exposed
# separately via preset_mode (see CLIMATE_PRESET_* below), the same way a
# real thermostat's "Auto" mode can still have "Eco"/"Away" presets.
CLIMATE_MODE_TO_HVAC: dict[int, str] = {
    CLIMATE_MODE_OFF: HVACMode.OFF,
    CLIMATE_MODE_CONSTANT: HVACMode.HEAT,
    CLIMATE_MODE_WEEK1: HVACMode.AUTO,
    CLIMATE_MODE_WEEK2: HVACMode.AUTO,
    CLIMATE_MODE_ECO: HVACMode.AUTO,
}
# Note: HVAC_TO_CLIMATE_MODE is intentionally NOT built by inverting
# CLIMATE_MODE_TO_HVAC anymore (that would be lossy: 3 raw codes map to the
# single HVACMode.AUTO, so inverting the dict would silently drop 2 of the
# 3). async_set_hvac_mode only ever needs to send OFF or CONSTANT directly;
# selecting a specific AUTO variant (week1/week2/eco) goes through
# async_set_preset_mode instead -- see climate.py.
HVAC_TO_CLIMATE_MODE: dict[str, int] = {
    HVACMode.OFF: CLIMATE_MODE_OFF,
    HVACMode.HEAT: CLIMATE_MODE_CONSTANT,
    HVACMode.AUTO: CLIMATE_MODE_WEEK1,  # default AUTO variant if none was selected yet
}

# Preset modes, only meaningful/selectable while hvac_mode == AUTO. PRESET_ECO
# is HA's own built-in preset constant (gets a translated label + icon for
# free); "week1"/"week2" are specific to this controller and need their own
# translation (see strings.json / translations/de.json / icons.json).
CLIMATE_PRESET_WEEK1 = "week1"
CLIMATE_PRESET_WEEK2 = "week2"

CLIMATE_MODE_TO_PRESET: dict[int, str] = {
    CLIMATE_MODE_WEEK1: CLIMATE_PRESET_WEEK1,
    CLIMATE_MODE_WEEK2: CLIMATE_PRESET_WEEK2,
    CLIMATE_MODE_ECO: PRESET_ECO,
}
PRESET_TO_CLIMATE_MODE: dict[str, int] = {
    preset: raw_value for raw_value, preset in CLIMATE_MODE_TO_PRESET.items()
}
# Presets are only ever shown while hvac_mode == HVACMode.AUTO (see
# HovalRoomClimate.preset_modes in climate.py, which returns this list only
# in that case, and None otherwise). Since presets are never selectable
# outside of AUTO, there's no need for a "None" placeholder option --
# whenever the picker is shown at all, a real program (week1/week2/eco) is
# always active and selectable.
CLIMATE_PRESET_OPTIONS: list[str] = list(CLIMATE_MODE_TO_PRESET.values())

# Smart Grid offset (in Kelvin), applied on top of a heating circuit's own
# setpoint while Smart Grid is in preferred operation -- see
# SMART_GRID_MODE_REGISTER further below. Exposed as a writable "number"
# entity per circuit (see HeatingCircuit.smart_grid_offset_reg,
# NUMBER_REGISTERS), disabled by default.
SMART_GRID_OFFSET_HC1_REGISTER = RegisterDef(
    key="smart_grid_offset_hc1",
    name="Smart Grid Offset Heating Circuit 1",
    address=27528,
    data_type="int16",
    scale=0.1,
    unit="K",
    writable=True,
    min_value=0,
    max_value=12,
    step=0.5,
    enabled_default=False,
)
SMART_GRID_OFFSET_HC2_REGISTER = RegisterDef(
    key="smart_grid_offset_hc2",
    name="Smart Grid Offset Heating Circuit 2",
    address=27529,
    data_type="int16",
    scale=0.1,
    unit="K",
    writable=True,
    min_value=0,
    max_value=12,
    step=0.5,
    enabled_default=False,
)
SMART_GRID_OFFSET_HC3_REGISTER = RegisterDef(
    key="smart_grid_offset_hc3",
    name="Smart Grid Offset Heating Circuit 3",
    address=27530,
    data_type="int16",
    scale=0.1,
    unit="K",
    writable=True,
    min_value=0,
    max_value=12,
    step=0.5,
    enabled_default=False,
)
SMART_GRID_OFFSET_COOLING_HC1_REGISTER = RegisterDef(
    key="smart_grid_offset_cooling_hc1",
    name="Smart Grid Offset Cooling Heating Circuit 1",
    address=27531,
    data_type="int16",
    scale=0.1,
    unit="K",
    writable=True,
    min_value=-60,
    max_value=0,
    step=0.5,
    enabled_default=False,
)
SMART_GRID_OFFSET_COOLING_HC2_REGISTER = RegisterDef(
    key="smart_grid_offset_cooling_hc2",
    name="Smart Grid Offset Cooling Heating Circuit 2",
    address=27532,
    data_type="int16",
    scale=0.1,
    unit="K",
    writable=True,
    min_value=-60,
    max_value=0,
    step=0.5,
    enabled_default=False,
)
SMART_GRID_OFFSET_COOLING_HC3_REGISTER = RegisterDef(
    key="smart_grid_offset_cooling_hc3",
    name="Smart Grid Offset Cooling Heating Circuit 3",
    address=27533,
    data_type="int16",
    scale=0.1,
    unit="K",
    writable=True,
    min_value=-60,
    max_value=0,
    step=0.5,
    enabled_default=False,
)

# Control strategy of a heating circuit (how the controller determines the
# flow setpoint). Exposed as a disabled-by-default diagnostic enum sensor
# per circuit, see HeatingCircuit.control_strategy_reg.
CONTROL_STRATEGY_HC1_REGISTER = RegisterDef(
    key="control_strategy_hc1",
    name="Control Strategy Heating Circuit 1",
    address=27510,
    data_type="uint16",
    enabled_default=False,
)
CONTROL_STRATEGY_HC2_REGISTER = RegisterDef(
    key="control_strategy_hc2",
    name="Control Strategy Heating Circuit 2",
    address=27511,
    data_type="uint16",
    enabled_default=False,
)
CONTROL_STRATEGY_HC3_REGISTER = RegisterDef(
    key="control_strategy_hc3",
    name="Control Strategy Heating Circuit 3",
    address=27512,
    data_type="uint16",
    enabled_default=False,
)

CONTROL_STRATEGY_UNKNOWN = "unknown"

CONTROL_STRATEGY_WEATHER_COMPENSATED = 0
CONTROL_STRATEGY_ROOM_INFLUENCE = 1
# The only strategies the integration may write, and the only ones under
# which it sends a room temperature to the controller.
CONTROL_STRATEGY_WRITABLE: tuple[int, ...] = (
    CONTROL_STRATEGY_WEATHER_COMPENSATED,
    CONTROL_STRATEGY_ROOM_INFLUENCE,
)
# Strategies with constant flow cooling; only these offer cooling.
CONTROL_STRATEGIES_COOLING: tuple[int, ...] = (4, 5)

# Range of the cooling flow setpoint offered to the user, in °C. The lower
# bound is raised to the circuit's minimum cooling flow temperature, which the
# installer sets to prevent condensation. Above the upper bound, the flow is
# about as warm as the room and doesn't cool any more.
COOLING_FLOW_MIN = 15.0
COOLING_FLOW_MAX = 25.0
COOLING_FLOW_DEFAULT = 18.0

# Raw register value -> enum sensor state. Unlisted values fall back to
# CONTROL_STRATEGY_UNKNOWN, since HA rejects enum states outside the options.
CONTROL_STRATEGY_MAP: dict[int, str] = {
    0: "weather_compensated",
    1: "weather_compensated_room_influence",
    2: "room_control",
    3: "constant",
    4: "weather_compensated_cooling_constant",
    5: "weather_compensated_room_influence_cooling_constant",
}
CONTROL_STRATEGY_OPTIONS: list[str] = [
    *CONTROL_STRATEGY_MAP.values(),
    CONTROL_STRATEGY_UNKNOWN,
]

# State of a heating circuit's control, e.g. whether Smart Grid preferred
# operation currently acts on it. Exposed as a disabled-by-default
# diagnostic enum sensor per circuit, see HeatingCircuit.status_reg.
HEATING_CIRCUIT_STATUS_UNKNOWN = "unknown"

# Raw register value -> enum sensor state, as listed in Hoval's Modbus
# guideline. Unlisted values fall back to HEATING_CIRCUIT_STATUS_UNKNOWN.
HEATING_CIRCUIT_STATUS_MAP: dict[int, str] = {
    0: "switched_off",
    1: "normal_heating",
    2: "comfort_heating",
    3: "eco_heating",
    4: "frost_protection",
    5: "forced_consumption",
    6: "forced_reduction",
    7: "holiday",
    8: "party",
    9: "normal_cooling",
    10: "comfort_cooling",
    11: "eco_cooling",
    12: "fault",
    13: "manual",
    14: "cooling_protection",
    15: "party_cooling",
    16: "screed_drying_heating_up",
    17: "screed_drying_steady",
    18: "screed_drying_cooling_down",
    19: "screed_drying_final",
    22: "cooling_constant_demand",
    23: "heating_constant_demand",
    24: "away_heating",
    25: "away_cooling",
    26: "smart_grid_preferred_heating",
    27: "smart_grid_preferred_cooling",
}
HEATING_CIRCUIT_STATUS_OPTIONS: list[str] = [
    *HEATING_CIRCUIT_STATUS_MAP.values(),
    HEATING_CIRCUIT_STATUS_UNKNOWN,
]


@dataclass
class HeatingCircuit:
    key: str
    name: str
    mode_reg: RegisterDef
    read_setpoint_reg: RegisterDef
    write_normal_setpoint_reg: RegisterDef
    write_eco_setpoint_reg: RegisterDef
    enabled_default: bool = True
    # Register for the physical room temperature sensor of this circuit, if
    # one is wired up. Read via CURRENT_TEMP_SOURCE_MODBUS (see climate.py) --
    # None means the circuit has no such sensor (or its address isn't known
    # yet), in which case selecting that source simply yields no value.
    room_actual_temp_reg: RegisterDef | None = None
    # This circuit's Smart Grid offset register (see
    # SMART_GRID_OFFSET_HC1_REGISTER above) -- used by climate.py to
    # compensate target_temperature writes for an active offset, the same
    # way HovalWaterHeater does for DHW.
    smart_grid_offset_reg: RegisterDef | None = None
    # This circuit's control strategy register (see
    # CONTROL_STRATEGY_HC1_REGISTER above), read by the diagnostic
    # control strategy sensor in sensor.py and by room_temp.py.
    control_strategy_reg: RegisterDef | None = None
    # This circuit's control status (see HEATING_CIRCUIT_STATUS_MAP), read
    # by the diagnostic heating circuit status sensor in sensor.py.
    status_reg: RegisterDef | None = None
    # Constant flow setpoint for cooling; 0 means not cooling. Writing a
    # temperature turns cooling on (see climate.py). None means cooling isn't
    # supported for this circuit.
    cooling_flow_setpoint_reg: RegisterDef | None = None
    # Minimum cooling flow temperature, set by the installer.
    cooling_flow_min_reg: RegisterDef | None = None
    # Constant flow setpoint for heating. With a cooling strategy (4/5) it
    # must be 0 for the circuit to cool; heating then stays weather
    # compensated and doesn't use it, so it is set to 0 when cooling starts
    # and left there.
    heating_constant_flow_reg: RegisterDef | None = None


HEATING_CIRCUIT_1 = HeatingCircuit(
    key="heating_circuit_1",
    name="Heating circuit 1",
    mode_reg=RegisterDef(
        key="hc1_mode",
        name="HC1 Mode",
        address=1478,
    ),
    read_setpoint_reg=RegisterDef(
        key="read_setpoint_hc1",
        name="Current setpoint HC1",
        address=1493,
        scale=0.1,
    ),
    write_normal_setpoint_reg=RegisterDef(
        key="write_normal_setpoint_hc1",
        name="Normal Setpoint HC1",
        address=1481,
        scale=0.1,
        step=0.5,
        min_value=10,
        max_value=30,
    ),
    write_eco_setpoint_reg=RegisterDef(
        key="write_eco_setpoint_hc1",
        name="Eco Setpoint HC1",
        address=1482,
        scale=0.1,
        step=0.5,
        min_value=5,
        max_value=20,
    ),
    room_actual_temp_reg=RegisterDef(
        key="current_room_temp_hc1",
        name="Current room temperature HC1",
        address=1510,
        scale=0.1,
    ),
    smart_grid_offset_reg=SMART_GRID_OFFSET_HC1_REGISTER,
    control_strategy_reg=CONTROL_STRATEGY_HC1_REGISTER,
    status_reg=RegisterDef(
        key="status_hc1",
        name="Status heating circuit 1",
        address=1501,
        data_type="uint16",
        enabled_default=False,
    ),
    cooling_flow_setpoint_reg=RegisterDef(
        key="cooling_flow_setpoint_hc1",
        name="Cooling flow setpoint HC1",
        address=19482,
        scale=0.1,
    ),
    cooling_flow_min_reg=RegisterDef(
        key="cooling_flow_min_hc1",
        name="Minimum cooling flow temperature HC1",
        address=29435,
        scale=0.1,
    ),
    heating_constant_flow_reg=RegisterDef(
        key="heating_constant_flow_hc1",
        name="Constant heating flow setpoint HC1",
        address=1490,
        scale=0.1,
    ),
    enabled_default=True,
)
HEATING_CIRCUIT_2 = HeatingCircuit(
    key="heating_circuit_2",
    name="Heating circuit 2",
    mode_reg=RegisterDef(
        key="hc2_mode",
        name="HC2 Mode",
        address=1479,
    ),
    read_setpoint_reg=RegisterDef(
        key="read_setpoint_hc2",
        name="Current setpoint HC2",
        address=1494,
        scale=0.1,
    ),
    write_normal_setpoint_reg=RegisterDef(
        key="write_normal_setpoint_hc2",
        name="Normal Setpoint HC2",
        address=1483,
        scale=0.1,
        step=0.5,
        min_value=10,
        max_value=30,
    ),
    write_eco_setpoint_reg=RegisterDef(
        key="write_eco_setpoint_hc2",
        name="Eco Setpoint HC2",
        address=1484,
        scale=0.1,
        step=0.5,
        min_value=5,
        max_value=20,
    ),
    room_actual_temp_reg=RegisterDef(
        key="current_room_temp_hc2",
        name="Current room temperature HC2",
        address=1511,
        scale=0.1,
    ),
    smart_grid_offset_reg=SMART_GRID_OFFSET_HC2_REGISTER,
    control_strategy_reg=CONTROL_STRATEGY_HC2_REGISTER,
    status_reg=RegisterDef(
        key="status_hc2",
        name="Status heating circuit 2",
        address=1502,
        data_type="uint16",
        enabled_default=False,
    ),
    cooling_flow_setpoint_reg=RegisterDef(
        key="cooling_flow_setpoint_hc2",
        name="Cooling flow setpoint HC2",
        address=23755,
        scale=0.1,
    ),
    cooling_flow_min_reg=RegisterDef(
        key="cooling_flow_min_hc2",
        name="Minimum cooling flow temperature HC2",
        address=29436,
        scale=0.1,
    ),
    heating_constant_flow_reg=RegisterDef(
        key="heating_constant_flow_hc2",
        name="Constant heating flow setpoint HC2",
        address=1491,
        scale=0.1,
    ),
    enabled_default=False,
)
HEATING_CIRCUIT_3 = HeatingCircuit(
    key="heating_circuit_3",
    name="Heating circuit 3",
    mode_reg=RegisterDef(
        key="hc3_mode",
        name="HC3 Mode",
        address=1480,
    ),
    read_setpoint_reg=RegisterDef(
        key="read_setpoint_hc3",
        name="Current setpoint HC3",
        address=1495,
        scale=0.1,
    ),
    write_normal_setpoint_reg=RegisterDef(
        key="write_normal_setpoint_hc3",
        name="Normal Setpoint HC3",
        address=1485,
        scale=0.1,
        step=0.5,
        min_value=10,
        max_value=30,
    ),
    write_eco_setpoint_reg=RegisterDef(
        key="write_eco_setpoint_hc3",
        name="Eco Setpoint HC3",
        address=1486,
        scale=0.1,
        step=0.5,
        min_value=5,
        max_value=20,
    ),
    room_actual_temp_reg=RegisterDef(
        key="current_room_temp_hc3",
        name="Current room temperature HC3",
        address=1512,
        scale=0.1,
    ),
    smart_grid_offset_reg=SMART_GRID_OFFSET_HC3_REGISTER,
    control_strategy_reg=CONTROL_STRATEGY_HC3_REGISTER,
    status_reg=RegisterDef(
        key="status_hc3",
        name="Status heating circuit 3",
        address=1503,
        data_type="uint16",
        enabled_default=False,
    ),
    cooling_flow_setpoint_reg=RegisterDef(
        key="cooling_flow_setpoint_hc3",
        name="Cooling flow setpoint HC3",
        address=23756,
        scale=0.1,
    ),
    cooling_flow_min_reg=RegisterDef(
        key="cooling_flow_min_hc3",
        name="Minimum cooling flow temperature HC3",
        address=29437,
        scale=0.1,
    ),
    heating_constant_flow_reg=RegisterDef(
        key="heating_constant_flow_hc3",
        name="Constant heating flow setpoint HC3",
        address=1492,
        scale=0.1,
    ),
    enabled_default=False,
)

HEATING_CIRCUITS = [HEATING_CIRCUIT_1, HEATING_CIRCUIT_2, HEATING_CIRCUIT_3]

HEATING_CIRCUIT_REGISTERS = [
    reg
    for hc in HEATING_CIRCUITS
    for reg in (
        hc.mode_reg,
        hc.read_setpoint_reg,
        hc.write_normal_setpoint_reg,
        hc.write_eco_setpoint_reg,
    )
] + [
    hc.room_actual_temp_reg for hc in HEATING_CIRCUITS if hc.room_actual_temp_reg is not None
] + [
    hc.control_strategy_reg for hc in HEATING_CIRCUITS if hc.control_strategy_reg is not None
] + [
    hc.status_reg for hc in HEATING_CIRCUITS if hc.status_reg is not None
] + [
    hc.cooling_flow_setpoint_reg
    for hc in HEATING_CIRCUITS
    if hc.cooling_flow_setpoint_reg is not None
]

# --------------------------------------------------------------------------
# ROOM CURRENT TEMPERATURE SOURCE (per heating circuit, configurable via the
# options flow -- see config_flow.py / climate.py)
#
# Lets the user pick, per circuit, where `current_temperature` comes from:
#   - "none":   don't show a current temperature at all
#   - "modbus": read it from the heat pump's own room sensor register
#               (HeatingCircuit.room_actual_temp_reg) -- only meaningful if
#               that register is actually configured (not None) and the
#               circuit has a physical sensor wired up
#   - "entity": mirror an existing Home Assistant entity's state (e.g. a
#               separate smart thermostat's temperature sensor)
#   - "entity_write": same as "entity", and also send the value to the
#               controller's room register (see room_temp.py)
# --------------------------------------------------------------------------

CURRENT_TEMP_SOURCE_NONE = "none"
CURRENT_TEMP_SOURCE_MODBUS = "modbus"
CURRENT_TEMP_SOURCE_ENTITY = "entity"
CURRENT_TEMP_SOURCE_ENTITY_WRITE = "entity_write"
DEFAULT_CURRENT_TEMP_SOURCE = CURRENT_TEMP_SOURCE_NONE


def current_temp_source_key(hc: HeatingCircuit) -> str:
    """Options-flow storage key for the chosen current-temperature source."""
    return f"{hc.key}_current_temp_source"


def current_temp_entity_key(hc: HeatingCircuit) -> str:
    """Options-flow storage key for the chosen HA source entity_id."""
    return f"{hc.key}_current_temp_entity_id"


def cooling_supported_key(hc: HeatingCircuit) -> str:
    """Options storage key for whether the circuit can cool, read from its
    control strategy when the integration is set up or the circuit is saved
    in the options."""
    return f"{hc.key}_cooling_supported"


def cooling_flow_min_key(hc: HeatingCircuit) -> str:
    """Options storage key for the circuit's minimum cooling flow
    temperature, read together with cooling_supported_key."""
    return f"{hc.key}_cooling_flow_min"


def cooling_flow_range(hc: HeatingCircuit, options: Mapping[str, Any]) -> tuple[float, float]:
    """The cooling flow setpoint range offered for a circuit."""
    minimum = max(COOLING_FLOW_MIN, options.get(cooling_flow_min_key(hc)) or 0)
    return minimum, max(COOLING_FLOW_MAX, minimum)


def previous_strategy_key(hc: HeatingCircuit) -> str:
    """Options-flow storage key for the control strategy to restore, set only
    while the integration itself has changed the circuit's strategy."""
    return f"{hc.key}_previous_control_strategy"


# --------------------------------------------------------------------------
# HEAT PUMP STATUS REGISTER
#
# Raw numeric status code from the controller, translated into German status
# text via HEAT_PUMP_STATUS_MAP. Used by the dedicated "status" sensor
# (device_class ENUM) in sensor.py. Placeholder address -- adjust to your
# Modbus map.
# --------------------------------------------------------------------------

HEAT_PUMP_STATUS_REGISTER = RegisterDef(
    key="heat_pump_status_code",
    name="Heat Pump Status",
    address=1539,
    data_type="uint16",
    scale=1.0,
)

# Error code of the heat pump's controller (WFA). Shown as an enum sensor in
# sensor.py, since 255 means "no error" and the other codes are warning
# numbers (W..), not quantities.
CONTROLLER_ERROR_REGISTER = RegisterDef(
    key="controller_error_code",
    name="Error code from controller",
    address=1534,
    data_type="uint16",
)

# Polled together with SENSOR_REGISTERS/NUMBER_REGISTERS/DHW_REGISTERS by the
# coordinator, but kept as its own list since these need special handling
# (code -> text translation) instead of the generic numeric sensor.
STATUS_REGISTERS: list[RegisterDef] = [
    HEAT_PUMP_STATUS_REGISTER,
    CONTROLLER_ERROR_REGISTER,
]

CONTROLLER_ERROR_NONE = "no_error"
CONTROLLER_ERROR_UNKNOWN = "unknown_error"

# Raw error code -> enum sensor state, as listed in Hoval's Modbus guideline.
# Unlisted codes fall back to CONTROLLER_ERROR_UNKNOWN; the raw code is
# always exposed as an attribute.
CONTROLLER_ERROR_MAP: dict[int, str] = {
    255: CONTROLLER_ERROR_NONE,
    74: "w11",
    76: "w13",
    80: "w17",
    81: "w18",
    82: "w19",
    83: "w20",
    84: "w21",
    85: "w22",
    86: "w23",
    87: "w24",
    88: "w25",
    89: "w26",
    90: "w27",
    100: "w37",
    101: "w38",
    102: "w39",
    103: "w40",
    104: "w41",
    106: "w43",
    121: "w58",
    122: "w59",
    126: "w63",
}
CONTROLLER_ERROR_OPTIONS: list[str] = [
    *CONTROLLER_ERROR_MAP.values(),
    CONTROLLER_ERROR_UNKNOWN,
]

# Raw status code -> stable, machine-readable key (English, snake_case).
# This dict is the single source of truth for which codes exist; the actual
# displayed text lives in strings.json / translations/de.json.
HEAT_PUMP_STATUS_MAP: dict[int, str] = {
    0: "switched_off",
    1: "heating_mode",
    2: "active_cooling",
    3: "lock",
    4: "hot_water",
    5: "frost_protection",
    6: "hp_temp_too_low",
    7: "hp_flow_too_high",
    8: "defrost",
    9: "passive_cooling",
    11: "hd_fault",
    12: "low_pressure_fault",
    16: "restart_delay",
    17: "energy_ore_block",
    18: "primary_lead_time",
    19: "primary_run_on_time",
    44: "mop",
    49: "unsuccessful_defrost",
    51: "condenser_pump_lead_time",
    55: "inverter_modbus_fault",
    72: "groundwater_frost_protection",
    73: "flow_rate_wq_gw_circuit",
    77: "compressor_limitation",
    97: "preheat_compressor_oil",
    98: "cold_start",
    99: "machine_not_configured",
}

# Fallback key for status codes not present in HEAT_PUMP_STATUS_MAP (e.g. if
# the controller reports an undocumented code). Must be included in the
# sensor's "options" list, since HA raises an error for ENUM sensor values
# that aren't part of the declared options.
HEAT_PUMP_STATUS_UNKNOWN = "unknown_status"

# All valid states for the "enum" sensor (used as SensorEntity._attr_options).
HEAT_PUMP_STATUS_OPTIONS: list[str] = [
    *HEAT_PUMP_STATUS_MAP.values(),
    HEAT_PUMP_STATUS_UNKNOWN,
]

# --------------------------------------------------------------------------
# SMART GRID REGISTERS
#
# To use Smart Grid, both registers below must be set: first
# SMART_GRID_TRIGGER_REGISTER to "system_bus" (Modbus as the trigger
# source), then SMART_GRID_MODE_REGISTER to "preferred_operation". Disabled
# by default since most installations don't use Smart Grid. Used by the
# "select" entities in select.py.
# --------------------------------------------------------------------------

SMART_GRID_TRIGGER_REGISTER = RegisterDef(
    key="smart_grid_trigger",
    name="Smart Grid Trigger",
    address=27546,
    data_type="uint16",
    writable=True,
    enabled_default=False,
)

SMART_GRID_TRIGGER_OFF = "off"
SMART_GRID_TRIGGER_INPUT_CONTACTS = "input_contacts"
SMART_GRID_TRIGGER_SYSTEM_BUS = "system_bus"
SMART_GRID_TRIGGER_REDUCED_AVAILABLE_POWER = "reduced_available_power"

# Raw register value <-> select entity option key.
SMART_GRID_TRIGGER_MAP: dict[int, str] = {
    0: SMART_GRID_TRIGGER_OFF,
    1: SMART_GRID_TRIGGER_INPUT_CONTACTS,
    2: SMART_GRID_TRIGGER_SYSTEM_BUS,
    3: SMART_GRID_TRIGGER_REDUCED_AVAILABLE_POWER,
}
SMART_GRID_TRIGGER_OPTION_TO_VALUE: dict[str, int] = {
    option: value for value, option in SMART_GRID_TRIGGER_MAP.items()
}
SMART_GRID_TRIGGER_OPTIONS: list[str] = list(SMART_GRID_TRIGGER_MAP.values())

SMART_GRID_MODE_REGISTER = RegisterDef(
    key="smart_grid_mode",
    name="Smart Grid Mode",
    address=27545,
    data_type="uint16",
    writable=True,
    enabled_default=False,
)

SMART_GRID_MODE_NORMAL_OPERATION = "normal_operation"
SMART_GRID_MODE_PREFERRED_OPERATION = "preferred_operation"
SMART_GRID_MODE_LOCKED = "locked"
SMART_GRID_MODE_FORCED_CONSUMPTION = "forced_consumption"

# Raw register value <-> select entity option key.
SMART_GRID_MODE_MAP: dict[int, str] = {
    0: SMART_GRID_MODE_NORMAL_OPERATION,
    1: SMART_GRID_MODE_PREFERRED_OPERATION,
    2: SMART_GRID_MODE_LOCKED,
    3: SMART_GRID_MODE_FORCED_CONSUMPTION,
}
SMART_GRID_MODE_OPTION_TO_VALUE: dict[str, int] = {
    option: value for value, option in SMART_GRID_MODE_MAP.items()
}
SMART_GRID_MODE_OPTIONS: list[str] = list(SMART_GRID_MODE_MAP.values())

SMART_GRID_STATUS_REGISTER = RegisterDef(
    key="smart_grid_status",
    name="Smart Grid Status",
    address=27537,
    data_type="uint16",
    enabled_default=False,
)

SMART_GRID_STATUS_INACTIVE = "inactive"
SMART_GRID_STATUS_UNKNOWN = "unknown"

# Raw register value -> enum sensor state. Unlisted values fall back to
# SMART_GRID_STATUS_UNKNOWN, since HA rejects enum states outside the options.
SMART_GRID_STATUS_MAP: dict[int, str] = {
    0: SMART_GRID_MODE_NORMAL_OPERATION,
    1: SMART_GRID_MODE_PREFERRED_OPERATION,
    2: SMART_GRID_MODE_LOCKED,
    3: SMART_GRID_MODE_FORCED_CONSUMPTION,
    255: SMART_GRID_STATUS_INACTIVE,
}
SMART_GRID_STATUS_OPTIONS: list[str] = [
    *SMART_GRID_STATUS_MAP.values(),
    SMART_GRID_STATUS_UNKNOWN,
]

# Polled together with SENSOR_REGISTERS/NUMBER_REGISTERS/DHW_REGISTERS by the
# coordinator, but kept as its own list since these need the raw-value <->
# option-key mapping above instead of a generic numeric value.
SMART_GRID_REGISTERS: list[RegisterDef] = [
    SMART_GRID_TRIGGER_REGISTER,
    SMART_GRID_MODE_REGISTER,
    SMART_GRID_STATUS_REGISTER,
]

# Offset values (in Kelvin) applied on top of the controller's own setpoints
# while Smart Grid is in preferred operation -- see SMART_GRID_MODE_REGISTER
# above. Exposed as writable "number" entities, disabled by default.
# (The heating-circuit offsets are defined earlier, right before the
# HeatingCircuit dataclass, since each circuit references its own.)
SMART_GRID_OFFSET_DHW_REGISTER = RegisterDef(
    key="smart_grid_offset_dhw",
    name="Smart Grid Offset DHW",
    address=27509,
    data_type="int16",
    scale=0.1,
    unit="K",
    writable=True,
    min_value=0,
    max_value=80,
    step=0.5,
    enabled_default=False,
)
SMART_GRID_OFFSET_BUFFER_HEATING_REGISTER = RegisterDef(
    key="smart_grid_offset_buffer_heating",
    name="Smart Grid Offset Buffer (Heating)",
    address=28839,
    data_type="int16",
    scale=0.1,
    unit="K",
    writable=True,
    min_value=0,
    max_value=90,
    step=0.5,
    enabled_default=False,
)
SMART_GRID_OFFSET_BUFFER_COOLING_REGISTER = RegisterDef(
    key="smart_grid_offset_buffer_cooling",
    name="Smart Grid Offset Buffer (Cooling)",
    address=28840,
    data_type="int16",
    scale=0.1,
    unit="K",
    writable=True,
    min_value=-30,
    max_value=0,
    step=0.5,
    enabled_default=False,
)

# How much higher than a heating circuit's flow setpoint the heat generator
# is asked to heat (while cooling: how much lower), to make up for the water
# cooling down or mixing on its way to the circuit, e.g. in a buffer. The
# datapoint list gives 1.0 K steps, but the controller applies 0.1 K. The
# heat generator setpoint that results is rounded down to 0.5 K.
FLOW_SETPOINT_RAISE_REGISTERS: list[RegisterDef] = [
    RegisterDef(
        key=f"flow_setpoint_raise_hc{circuit}",
        name=f"Flow setpoint raise heating circuit {circuit}",
        address=address,
        data_type="int16",
        scale=0.1,
        unit="K",
        writable=True,
        min_value=0,
        max_value=10,
        step=0.1,
        enabled_default=False,
    )
    for circuit, address in ((1, 30684), (2, 30685), (3, 30686))
]

NUMBER_REGISTERS.extend(
    [
        *FLOW_SETPOINT_RAISE_REGISTERS,
        SMART_GRID_OFFSET_HC1_REGISTER,
        SMART_GRID_OFFSET_HC2_REGISTER,
        SMART_GRID_OFFSET_HC3_REGISTER,
        SMART_GRID_OFFSET_COOLING_HC1_REGISTER,
        SMART_GRID_OFFSET_COOLING_HC2_REGISTER,
        SMART_GRID_OFFSET_COOLING_HC3_REGISTER,
        SMART_GRID_OFFSET_DHW_REGISTER,
        SMART_GRID_OFFSET_BUFFER_HEATING_REGISTER,
        SMART_GRID_OFFSET_BUFFER_COOLING_REGISTER,
    ]
)