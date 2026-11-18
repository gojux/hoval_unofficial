# Register map and verification status

All registers the integration uses, checked against Hoval's Modbus
datapoint list (V2-11-12, gateway software TTE-GW >= V2.38) and Hoval's
Modbus guideline. All registers belong to the heat generator module (WEZ,
UnitId 1) unless noted otherwise, are read as holding registers (function
code 3) at the address given in the datapoint list, and are written with
function code 6 (single register). 32-bit values occupy two registers, the
high word first.

## Columns

- **Datapoint (Hoval):** the name in the German sheet of the datapoint
  list. The list's English sheet has the same addresses, but its names are
  partly untranslated.
- **Access:** as stated in the datapoint list. `R` = read only, `R/W` =
  readable and writable. Every register in the list is readable; there are
  no write-only registers.
- **Verified:** whether the register has been checked on a real
  installation the way the integration uses it (reading it and, where the
  integration writes it, writing it).
  `y` = checked and works, `–` = not checked yet, `n` = checked and doesn't
  work as documented.

Only one installation (TopTronic E with a heat pump) has been used for
verification so far. Reports from other installations, especially ones that
cool, have a buffer module (PS) or heating circuits on an extension module
(HKWW), are welcome.

## Sensors

| Address | Key | Datapoint (Hoval) | Type | Scale | Access | Verified |
|---|---|---|---|---|---|---|
| 1477 | `outdoor_temp` | AF1 Aussenfühler 1 | S16 | 0.1 | R | y |
| 1505 | `heat_quantity_heat_generator` | Wärmemenge Heizen (Wärmeerz.) | U32 | 0.001 MWh | R | y |
| 1507 | `operating_hours_heat_generator` | Betriebsstunden Wärmeerzeuger | U32 | 1 h | R | y |
| 1513 | `flow_temp_hc1` | Vorlauf-Ist HK1 | S16 | 0.1 | R | y |
| 1518 | `switching_cycles_heat_generator` | Schaltzyklen Wärmeerzeuger | U32 | 1 | R | y |
| 1534 | `controller_error_code` | Fehlercode vom Automaten | U8 | enum | R | y |
| 1539 | `heat_pump_status_code` | WEZ-Status | U8 | enum | R | y |
| 18724 | `heat_generator_target_temp` | Wärmeerzeuger-Soll | S16 | 0.1 | R | y |
| 18725 | `heat_generator_actual_temp` | Wärmeerzeuger-Ist | S16 | 0.1 | R | y |
| 18726 | `modulation` | Modulation | U8 | 1 % | R | y |
| 18727 | `operating_hours_heat_generator_above_50` | Betriebsstunden Wärmeerzeuger >50% | U32 | 1 h | R | y |
| 18729 | `switching_cycles_heat_generator_above_50` | Schaltzyklen Wärmeerzeuger >50% | U32 | 1 | R | y |
| 18731 | `current_power_output` | Aktuelle Leistung Heizen | U32 | 0.1 kW | R | y |
| 18733 | `cooling_energy` | Kältemenge Kühlen | U32 | 0.001 MWh | R | – |
| 18735 | `current_cooling_power` | Aktuelle Leistung Kühlen | U32 | 0.1 kW | R | – |
| 18742 | `return_temp_heat_generator` | Rücklauftemperatur Wärmeerzeuger | S16 | 0.1 | R | y |
| 19562 | `supply_target_temp_hc1` | Vorlauf-Soll HK1 | S16 | 0.1 | R | y |
| 19658 | `mixing_valve_hc1` | Mischer HK1 | S8 | 1 % | R | y |
| 25611 | `electrical_power_input_heat_generator` | Aktuelle elektr. Leistungsaufnahme WEZ | S16 | 0.01 kW | R | y |
| 25613 | `total_electrical_energy_heat_generator` | Elektrische Gesamtenergie WEZ | U32 | 0.001 MWh | R | y |
| 27484 | `heat_quantity_heating` | Wärmemenge Heizen (FA) | U32 | 0.001 MWh | R | y |
| 27488 | `heat_quantity_hot_water` | Wärmemenge WW | U32 | 0.001 MWh | R | y |
| 27490 | `cop` | Coefficient of Performance | U8 | 0.1 | R | y |
| 27493 | `buffer_temp` | WPF Wärmepumpenpufferfühler (only with a buffer sensor) | S32 | 0.1 | R | y |
| 27495 | `heat_pump_circulation_pump_speed` | Drehzahl CP | S32 | 1 % | R | y |
| 27537 | `smart_grid_status` | Status Smart Grid | U8 | enum | R | y |
| 31707 | `heat_pump_flow_rate` | Volumenstrom gemittelt | S32 | 0.1 L/min | R | y |

## Heating circuits

Addresses for heating circuits 1 / 2 / 3.

| Addresses | Purpose | Datapoint (Hoval) | Type | Scale | Access | Verified HC1 | Verified HC2/HC3 |
|---|---|---|---|---|---|---|---|
| 1478 / 1479 / 1480 | Operating mode | Betriebswahl Heizung | LIST | – | R/W | y | – |
| 1481 / 1483 / 1485 | Normal room setpoint | Normal-Raumtemperatur Heizbetrieb | S16 | 0.1 | R/W | y | – |
| 1482 / 1484 / 1486 | Eco room setpoint | Spar-Raumtemperatur Heizbetrieb | S16 | 0.1 | R/W | y | – |
| 1490 / 1491 / 1492 | Constant flow setpoint heating | Vorlauf Soll Konstantanf. Heizen | S16 | 0.1 | R/W | –¹ | – |
| 1493 / 1494 / 1495 | Current room setpoint | Raum-Soll | S16 | 0.1 | R | y | – |
| 1501 / 1502 / 1503 | Status of the circuit's control | Status Heizkreisregelung | U8 | enum | R | y | – |
| 1510 / 1511 / 1512 | Room temperature (read and sent) | Raum-Ist | S16 | 0.1 | R/W | y | – |
| 19482 / 23755 / 23756 | Constant flow setpoint cooling | Vorlauf Soll Konstantanf. Kühlen | S16 | 0.1 | R/W | – | – |
| 27510 / 27511 / 27512 | Control strategy | Regelstrategie | U8 | enum | R/W | y | – |
| 27528 / 27529 / 27530 | Smart Grid offset heating | SmartGrid Offset Raum-Sollw. Heizen | S16 | 0.1 K | R/W | –¹ | – |
| 27531 / 27532 / 27533 | Smart Grid offset cooling | SmartGrid Offset Raum-Sollw. Kühlen | S16 | 0.1 K | R/W | – | – |
| 29435 / 29436 / 29437 | Minimum cooling flow temperature | Vorlaufminimaltemp. Kühlen | S16 | 0.1 | R/W | y | – |
| 30684 / 30685 / 30686 | Flow setpoint raise (`flow_setpoint_raise_hc1`–`3`) | WEZ Überhöhung/Abschlag VL-Sollwert | S16 | 0.1 K | R/W | y | – |
| 30690 / 30691 / 30692 | Maximum flow temperature (upper bound of the constant heating flow setpoint) | Vorlauf Maximaltemp. | S16 | 0.1 | R/W | y² | – |

¹ Reading checked, writing not yet.
² Not written by the integration.

## Domestic hot water

| Address | Key | Datapoint (Hoval) | Type | Scale | Access | Verified |
|---|---|---|---|---|---|---|
| 1496 | `dhw_mode` | Betriebswahl Warmwasser | LIST | – | R/W | y |
| 1497 | `dhw_normal_target_temp` | Normal-Warmwassertemperatur | S16 | 0.1 | R/W | y |
| 1498 | `dhw_eco_target_temp` | Spar-Warmwassertemperatur | U8 | 1 | R/W | y |
| 1499 | `dhw_actual_target_temp` | Warmwasser-Soll | S16 | 0.1 | R | y |
| 1500 | `dhw_current_temp` | Warmwasser-Ist SF | S16 | 0.1 | R | y |
| 27509 | `smart_grid_offset_dhw` | Smart-Grid (Offset) WW-Sollwert | S16 | 0.1 K | R/W | y |

## Smart Grid

| Address | Key | Datapoint (Hoval) | Type | Scale | Access | Verified |
|---|---|---|---|---|---|---|
| 27545 | `smart_grid_mode` | Smart Grid über Systembus | U8 | enum | R/W | y |
| 27546 | `smart_grid_trigger` | Auslöser Smart Grid Funktion | U8 | enum | R/W | y |
| 28837 | `smart_grid_offset_buffer_142_heating` | Smart-Grid (Offset) Puffer-Soll.Heizen (PS UnitId 142) | S16 | 0.1 K | R/W | – |
| 28838 | `smart_grid_offset_buffer_142_cooling` | Smart-Grid (Offset) Puffer-Soll.Kühlen (PS UnitId 142) | S16 | 0.1 K | R/W | – |
| 28839 | `smart_grid_offset_buffer_heating` | Smart-Grid (Offset) Puffer-Soll.Heizen (PS UnitId 143) | S16 | 0.1 K | R/W | n³ |
| 28840 | `smart_grid_offset_buffer_cooling` | Smart-Grid (Offset) Puffer-Soll.Kühlen (PS UnitId 143) | S16 | 0.1 K | R/W | – |

³ On an installation without a buffer module (PS), the controller answers
with "illegal data address" (Modbus exception 0x02), see
[Differences from Hoval's documentation](#differences-from-hovals-documentation).
The buffer offsets are therefore only offered while their module is found
(see [Buffer module detection](#buffer-module-detection)); the cooling
offsets of the heating circuits (27531–27533) only at strategy 3, 4 or 5.

## Buffer module detection

Read when the integration starts and when the experimental functions are
opened. A value other than 0 within -20 to 110 °C means the heat pump has a
buffer module (PS); then the flow setpoint raise isn't offered (Hoval), and
the module's buffer sensors and setpoint are shown as sensors. An error
response or 0 means there is none. These registers belong to the buffer
modules PS UnitId 142 and 143, not to the heat generator. The module's
UnitId is part of the entity names only if both modules are found.

| Address | Key | Datapoint (Hoval) | Type | Scale | Access | Verified |
|---|---|---|---|---|---|---|
| 17324 / 17386 | `buffer_module_temp_142` / `_143` | Puffer PF/KPF2 Ist (PS UnitId 142 / 143), also the detection | S16 | 0.1 | R | y (143) |
| 17325 / 17387 | `buffer_module_temp2_142` / `_143` | Puffer PF2/KPF Ist | S16 | 0.1 | R | – |
| 17323 / 17385 | `buffer_module_setpoint_142` / `_143` | Puffer-Soll | S16 | 0.1 | R | – |

## Other registers checked on a real installation

Not used by the integration (yet), but checked on a real installation.

| Address | Datapoint (Hoval) | Type | Scale | Access | Verified |
|---|---|---|---|---|---|
| 23739 | Temperatur Überhöhung/Abschlag WEZ | S16 | 0.1 K | R/W | y (read) |
| 31920 | Drehzahl Ist (compressor speed) | S32 | 0.1 % | R | y |

- **23739** raises the heat generator's setpoint for every demand.
- **31920** matches the modulation (18726), e.g. 300 = 30.0 %.

## Differences from Hoval's documentation

Where the documentation and a real installation disagree, the integration
follows the installation.

- **18724** (heat generator target temperature): the datapoint list gives
  0 decimals, the guideline /10. The raw values are tenths of a degree
  (e.g. 400 = 40.0 °C), as in the guideline.
- **1505**: named "Wärmemenge Heizen" in the list, but holds heating plus
  hot water (the sum of 27484 and 27488).
- **1539**: the list describes it as "WEZ-Status" (burner status). It
  reports the status codes of the heat pump's controller (WFA) that the
  guideline lists for 18723, e.g. 8 = defrost, 51 = condenser pump lead
  time.
- **1534**: 255 means "no error"; the guideline maps the other codes to
  warning numbers (74 = W11 … 126 = W63).
- **1501–1503** (status of the circuit's control): the datapoint list
  documents codes 0–19, 22, 23 and 26; the guideline additionally lists
  24/25 (away, heating/cooling) and 27 (Smart Grid preferred operation,
  cooling). The integration uses the guideline's list.
- **19658** (mixing valve, S8): the gateway sign-extends the value to
  16 bits, so negative positions read correctly as signed 16-bit values.
- **1490–1492** (constant flow setpoint heating): the list gives a range of
  10.0–110.0 °C, but according to Hoval the value must be 0 while a circuit
  cools with control strategy 4 or 5. With constant control (strategy 3),
  1490–1492 and 19482/23755/23756 decide the mode: heating while only the
  heating setpoint is above 0, cooling while only the cooling setpoint is,
  off while both are 0 (Hoval).
- **28839/28840** (Smart Grid offset buffer): the guideline names these
  addresses, while the datapoint list assigns them to buffer module PS
  UnitId 143. On an installation without a PS module (buffer sensor WPF on
  the heat generator), 28839 doesn't exist: the controller answers with
  Modbus exception 0x02. Ranges: list 0–90 K / −30–0 K, guideline 0–30 K. The same
  difference exists for 27509 (list 0–80 K, guideline 0–30 K). The
  integration uses the list's ranges.
- **18740/18741** (speed of the main pump, flow rate): read 0 on a heat
  pump, even while the pumps run. The integration uses the heat pump
  controller's registers 27495 (speed of the circulation pump) and 31707
  (averaged flow rate) instead.
- **31908** ("Drehzahl CP"): the datapoint list gives the unit l/min, but
  the value is the pump speed in tenths of a percent (400 = 40 %, the same
  as 27495).
- **18724** (heat generator target temperature) is rounded down to 0.5 K,
  e.g. a circuit setpoint of 36.3 °C plus 1.0 K gives 37.0 °C.
- **30684** (raise of the flow setpoint, HC1): the datapoint list gives a
  step of 1.0 K, but the controller accepts and applies 0.1 K steps. Only
  the resulting 18724 is rounded down to 0.5 K: with a circuit setpoint of
  36.3 °C, 1.5 K gives 37.5 °C and 1.7 K gives 38.0 °C. A finer raise
  therefore shifts the circuit setpoint at which 18724 moves up a step; on
  average, 18724 is about 0.25 K below circuit setpoint plus raise.
- **HC2/HC3**: the guideline notes that heating circuits 2 and 3 are often
  connected to an HKWW extension module, which uses different addresses.
  The integration uses the heat generator module's circuits 2 and 3.
