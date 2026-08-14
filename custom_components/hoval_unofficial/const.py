"""Constants for the Hoval Heat Pump Integration (unofficial)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from homeassistant.components.water_heater import STATE_ECO
from homeassistant.components.climate import HVACMode, PRESET_ECO
from homeassistant.const import STATE_OFF

DOMAIN = "hoval_unofficial"

CONF_DEVICE_ID = "device_id"

DEFAULT_NAME = "Heat Pump"
DEFAULT_PORT = 502
DEFAULT_DEVICE_ID = 1
DEFAULT_SCAN_INTERVAL = 30  # seconds

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
        unit="%",
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
        state_class="total_increasing",
    ),
    RegisterDef(
        key="switching_cycles_heat_generator_above_50",
        name="Switching cycles heat generator >50%",
        address=18729,
        data_type="uint32",
        state_class="total_increasing",
    ),
    RegisterDef(
        key="operating_hours_heat_generator",
        name="Operating hours heat generator",
        address=1507,  # same as 1516
        data_type="uint32",
        unit="h",
        state_class="total_increasing",
        device_class="duration",
    ),
    RegisterDef(
        key="operating_hours_heat_generator_above_50",
        name="Operating hours heat generator >50%",
        address=18727,
        data_type="uint32",
        unit="h",
        state_class="total_increasing",
        device_class="duration",
    ),

    # --- Diagnostics / status & error codes (least everyday-relevant) ---
    RegisterDef(
        key="controller_error_code",
        name="Error code from controller",
        address=1534,
        data_type="uint16",
    ),
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
# "number" entities. Empty for now -- add a RegisterDef here (with
# writable=True) if your controller has additional writable values not
# covered elsewhere.
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
DHW_STATE_WEEK1 = "week 1"
DHW_STATE_WEEK2 = "week 2"


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
# --------------------------------------------------------------------------

CURRENT_TEMP_SOURCE_NONE = "none"
CURRENT_TEMP_SOURCE_MODBUS = "modbus"
CURRENT_TEMP_SOURCE_ENTITY = "entity"
CURRENT_TEMP_SOURCE_OPTIONS: list[str] = [
    CURRENT_TEMP_SOURCE_NONE,
    CURRENT_TEMP_SOURCE_MODBUS,
    CURRENT_TEMP_SOURCE_ENTITY,
]
DEFAULT_CURRENT_TEMP_SOURCE = CURRENT_TEMP_SOURCE_NONE


def current_temp_source_key(hc: HeatingCircuit) -> str:
    """Options-flow storage key for the chosen current-temperature source."""
    return f"{hc.key}_current_temp_source"


def current_temp_entity_key(hc: HeatingCircuit) -> str:
    """Options-flow storage key for the chosen HA source entity_id."""
    return f"{hc.key}_current_temp_entity_id"


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

# Polled together with SENSOR_REGISTERS/NUMBER_REGISTERS/DHW_REGISTERS by the
# coordinator, but kept as its own list since it needs special handling
# (code -> text translation) instead of the generic numeric sensor.
STATUS_REGISTERS: list[RegisterDef] = [HEAT_PUMP_STATUS_REGISTER]

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