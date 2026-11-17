"""End-to-end check: onboard Home Assistant, add the integration, verify its entities.

Uses only the Python standard library and talks to a Home Assistant instance
started via tests/e2e/docker-compose.yml on localhost:8123, and directly to
the Modbus simulator to set up and verify register values.
"""
import json
import os
import socket
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HA_URL = f"http://localhost:{os.environ.get('HA_PORT', '8123')}"
CLIENT_ID = f"{HA_URL}/"
TEST_USER = {
    "name": "E2E Test",
    "username": "e2e-test",
    "password": "e2e-test-password",
    "language": "en",
}
ENTRY_NAME = "E2E Heat Pump"
MODBUS_HOST = "modbus-sim"
MODBUS_PORT = 502
MODBUS_DEVICE_ID = 1
STARTUP_TIMEOUT_SECONDS = 300
STATE_TIMEOUT_SECONDS = 120
MODBUS_SIM_ADDRESS = ("localhost", int(os.environ.get("MODBUS_SIM_PORT", "15020")))
ROOM_REGISTERS = {1: 1510, 2: 1511, 3: 1512}
STRATEGY_REGISTERS = {1: 27510, 2: 27511, 3: 27512}
SOURCE_ENTITY = "sensor.e2e_room_temperature"
# The writer sends a change at most once per minute.
WRITE_TIMEOUT_SECONDS = 90
SETPOINT_REGISTERS = {1: 1493, 2: 1494, 3: 1495}
MODE_REGISTER_HC1 = 1478
FLOW_TEMP_REGISTERS = {2: 1514, 3: 1515}
DHW_TEMP = 1500
COOLING_FLOW_SETPOINT_HC1 = 19482
COOLING_FLOW_MIN_HC1 = 29435
HEATING_CONSTANT_FLOW_HC1 = 1490
FLOW_SETPOINT_RAISE_HC1 = 30684
FLOW_SETPOINT_RAISE_HC2 = 30685
COOLING_FLOW_SETPOINT_HC3 = 23756
FLOW_MAX_HC1 = 30690
# Buffer temperature of buffer module PS UnitId 142.
BUFFER_MODULE_TEMP = 17324
# Low word of the buffer sensor (WPF, 27493/27494).
BUFFER_SENSOR = 27494
# The room setpoint is sent after 5 minutes without a usable value.
FALLBACK_TIMEOUT_SECONDS = 6 * 60

EXPECTED_ENABLED = [
    ("sensor.", "outdoor_temperature"),
    ("sensor.", "_status"),
    ("water_heater.", None),
    ("climate.", "heating_circuit_1"),
]
EXPECTED_DISABLED_SUBSTRINGS = [
    "smart_grid",
    "cooling",
    "control_strategy",
    "flow_rate",
    "pump_speed",
    "setpoint_raise",
]


def request(method, path, body=None, form=None, token=None):
    headers = {}
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"{HA_URL}{path}", data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = resp.read()
        return json.loads(payload) if payload else None


def wait_for_home_assistant() -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            request("GET", "/api/onboarding")
            return
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(5)
    raise SystemExit("Home Assistant did not become reachable in time")


def onboard() -> str:
    result = request(
        "POST",
        "/api/onboarding/users",
        body={"client_id": CLIENT_ID, **TEST_USER},
    )
    tokens = request(
        "POST",
        "/auth/token",
        form={
            "grant_type": "authorization_code",
            "code": result["auth_code"],
            "client_id": CLIENT_ID,
        },
    )
    return tokens["access_token"]


def add_integration(token: str) -> str:
    flow = request(
        "POST",
        "/api/config/config_entries/flow",
        body={"handler": "hoval_unofficial", "show_advanced_options": False},
        token=token,
    )
    result = request(
        "POST",
        f"/api/config/config_entries/flow/{flow['flow_id']}",
        body={
            "name": ENTRY_NAME,
            "host": MODBUS_HOST,
            "port": MODBUS_PORT,
            "device_id": MODBUS_DEVICE_ID,
            "scan_interval": 30,
        },
        token=token,
    )
    if result.get("type") != "create_entry":
        raise SystemExit(f"Config flow did not create an entry: {result}")
    return result["result"]["entry_id"]


def check_diagnostics(token: str, entry_id: str) -> None:
    raw = request("GET", f"/api/diagnostics/config_entry/{entry_id}", token=token)
    text = json.dumps(raw)
    if MODBUS_HOST in text:
        raise SystemExit("Diagnostics leak the Modbus host")
    if "register_values" not in text or "**REDACTED**" not in text:
        raise SystemExit("Diagnostics download is missing expected content")
    print("OK: diagnostics download works and redacts the host")


def wait_for_states(token: str) -> list[dict]:
    deadline = time.monotonic() + STATE_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        states = request("GET", "/api/states", token=token)
        ids = [s["entity_id"] for s in states]
        if any(i.startswith("water_heater.") for i in ids):
            return states
        time.sleep(3)
    raise SystemExit("Integration entities did not appear in time")


def check(states: list[dict]) -> None:
    by_id = {s["entity_id"]: s for s in states}
    failures = []

    for prefix, fragment in EXPECTED_ENABLED:
        matches = [
            s for eid, s in by_id.items()
            if eid.startswith(prefix) and (fragment is None or fragment in eid)
        ]
        if not matches:
            failures.append(f"missing entity {prefix}*{fragment or ''}*")
            continue
        for s in matches:
            if s["state"] in ("unavailable", "unknown"):
                failures.append(f"{s['entity_id']} is {s['state']}")

    for fragment in EXPECTED_DISABLED_SUBSTRINGS:
        present = [eid for eid in by_id if fragment in eid]
        if present:
            failures.append(f"entities that should be disabled by default are present: {present}")

    if failures:
        print("FAILED:")
        for f in failures:
            print(f"  - {f}")
        raise SystemExit(1)

    print("OK: integration set up, enabled entities available, disabled-by-default entities absent")
    for eid in sorted(by_id):
        if eid.startswith(("sensor.", "water_heater.", "climate.")) and ENTRY_NAME.lower().replace(" ", "_") in eid:
            print(f"  {eid} = {by_id[eid]['state']}")


def modbus_request(pdu: bytes) -> bytes:
    frame = struct.pack(">HHHB", 1, 0, len(pdu) + 1, MODBUS_DEVICE_ID) + pdu
    with socket.create_connection(MODBUS_SIM_ADDRESS, timeout=10) as sock:
        sock.sendall(frame)
        return sock.recv(256)[7:]


def read_sim(address: int) -> int:
    return struct.unpack(">H", modbus_request(struct.pack(">BHH", 3, address, 1))[2:4])[0]


def write_sim(address: int, value: int) -> None:
    modbus_request(struct.pack(">BHH", 6, address, value))


def expect_sim(address: int, value: int, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if read_sim(address) == value:
            return
        time.sleep(1)
    raise SystemExit(f"Register {address} is {read_sim(address)}, expected {value}")


def set_source_state(token: str, value: str) -> None:
    request(
        "POST",
        f"/api/states/{SOURCE_ENTITY}",
        body={
            "state": value,
            "attributes": {"unit_of_measurement": "°C", "device_class": "temperature"},
        },
        token=token,
    )


def open_options_step(token: str, entry_id: str, step: str) -> dict:
    # Saved options reload the entry, during which the form isn't available.
    deadline = time.monotonic() + STATE_TIMEOUT_SECONDS
    while True:
        menu = request(
            "POST",
            "/api/config/config_entries/options/flow",
            body={"handler": entry_id},
            token=token,
        )
        if menu.get("type") != "menu":
            raise SystemExit(f"Options flow did not open its menu: {menu}")
        form = request(
            "POST",
            f"/api/config/config_entries/options/flow/{menu['flow_id']}",
            body={"next_step_id": step},
            token=token,
        )
        if form.get("reason") != "not_loaded" or time.monotonic() > deadline:
            return form
        time.sleep(1)


def open_circuit_form(token: str, entry_id: str, circuit: int) -> dict:
    form = open_options_step(token, entry_id, f"heating_circuit_{circuit}")
    if form.get("type") != "form" or form.get("step_id") != "circuit":
        raise SystemExit(f"Options flow did not open the circuit form: {form}")
    return form


def open_general_form(token: str, entry_id: str) -> dict:
    form = open_options_step(token, entry_id, "general")
    if form.get("type") != "form" or form.get("step_id") != "general":
        raise SystemExit(f"Options flow did not open the general form: {form}")
    return form


def wait_until_polling(token: str) -> None:
    """Waits until the reloaded entry polls the controller again."""
    deadline = time.monotonic() + STATE_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        states = request("GET", "/api/states", token=token)
        polled = [
            s for s in states
            if s["entity_id"].startswith("sensor.") and "outdoor_temperature" in s["entity_id"]
        ]
        if polled and all(s["state"] not in ("unavailable", "unknown") for s in polled):
            return
        time.sleep(2)
    raise SystemExit("Entities did not become available again after saving options")


def check_timeout_option(token: str, entry_id: str) -> None:
    version = request("GET", "/api/config", token=token)["version"]
    # HA-COMPAT(2026.10): drop the check for a missing field.
    supported = tuple(int(part) for part in version.split(".")[:2]) >= (2026, 10)
    fields = form_fields(open_general_form(token, entry_id))
    if ("timeout" in fields) != supported:
        raise SystemExit(
            f"Timeout option offered: {'timeout' in fields}, expected {supported} "
            f"on Home Assistant {version}"
        )
    if not supported:
        print(f"OK: no timeout option on Home Assistant {version}")
        return

    result = submit_form(
        token, open_general_form(token, entry_id), {"scan_interval": 30, "timeout": 5}
    )
    if result.get("type") != "create_entry":
        raise SystemExit(f"Options flow did not save the timeout: {result}")
    field = form_fields(open_general_form(token, entry_id))["timeout"]
    if (field.get("description") or {}).get("suggested_value") != 5:
        raise SystemExit(f"Timeout was not stored: {field}")
    wait_until_polling(token)

    # An emptied field goes back to DEFAULT_TIMEOUT.
    result = submit_form(token, open_general_form(token, entry_id), {"scan_interval": 30})
    if result.get("type") != "create_entry":
        raise SystemExit(f"Options flow did not save: {result}")
    field = form_fields(open_general_form(token, entry_id))["timeout"]
    if (field.get("description") or {}).get("suggested_value") != 3.0:
        raise SystemExit(f"Timeout did not go back to the default: {field}")
    wait_until_polling(token)
    print(f"OK: timeout option works on Home Assistant {version}")


def form_fields(form: dict) -> dict:
    return {field["name"]: field for field in form["data_schema"]}


def source_options(form: dict) -> list[str]:
    return form_fields(form)["current_temp_source"]["selector"]["select"]["options"]


def submit_form(token: str, form: dict, data: dict) -> dict:
    return request(
        "POST",
        f"/api/config/config_entries/options/flow/{form['flow_id']}",
        body=data,
        token=token,
    )


def accept_risks(token: str, result: dict) -> dict:
    if result.get("step_id") == "confirm_risks":
        return submit_form(token, result, {"accept_risks": True})
    return result


def configure_circuit(token: str, entry_id: str, circuit: int, data: dict) -> None:
    result = submit_form(token, open_circuit_form(token, entry_id, circuit), data)
    result = accept_risks(token, result)
    if result.get("type") != "create_entry":
        raise SystemExit(f"Options flow did not save: {result}")


def writer_status(token: str, entry_id: str, circuit: int) -> dict:
    diagnostics = request(
        "GET", f"/api/diagnostics/config_entry/{entry_id}", token=token
    )
    return diagnostics["data"]["room_temp_writers"][f"heating_circuit_{circuit}"]


def check_sending(token: str, entry_id: str) -> None:
    room, strategy = ROOM_REGISTERS[1], STRATEGY_REGISTERS[1]
    form = open_circuit_form(token, entry_id, 1)
    if "entity_write" in source_options(form) or "room_influence" in form_fields(form):
        raise SystemExit("Room influence is offered before it is turned on")
    set_experimental(token, entry_id, room_influence=True)
    print("OK: sending and room influence are only offered once turned on")

    set_source_state(token, "unavailable")
    form = open_circuit_form(token, entry_id, 1)
    if "entity_write" not in source_options(form):
        raise SystemExit("Sending is not offered without sensor at strategy 0")
    result = submit_form(
        token,
        form,
        {"current_temp_source": "entity_write", "current_temp_entity_id": SOURCE_ENTITY},
    )
    if result.get("errors", {}).get("current_temp_entity_id") != "entity_unavailable":
        raise SystemExit(f"Unusable source entity was not rejected: {result}")
    if read_sim(strategy) != 0:
        raise SystemExit("Strategy was changed although the form was rejected")

    set_source_state(token, "21.5")
    result = submit_form(
        token,
        open_circuit_form(token, entry_id, 1),
        {"current_temp_source": "entity_write", "current_temp_entity_id": SOURCE_ENTITY},
    )
    if result.get("type") != "create_entry":
        raise SystemExit(f"Sending without room influence asked to confirm: {result}")
    expect_sim(room, 215)
    if read_sim(strategy) != 0:
        raise SystemExit("Sending changed the strategy without being asked to")
    print("OK: choosing to send writes the temperature, strategy stays")

    data = {
        "current_temp_source": "entity_write",
        "current_temp_entity_id": SOURCE_ENTITY,
        "room_influence": True,
    }
    confirm = submit_form(token, open_circuit_form(token, entry_id, 1), data)
    if confirm.get("step_id") != "confirm_risks":
        raise SystemExit(f"Room influence on a sent value did not ask to confirm: {confirm}")
    rejected = submit_form(token, confirm, {"accept_risks": False})
    if rejected.get("errors", {}).get("base") != "risks_not_accepted":
        raise SystemExit(f"Continuing without accepting the risks was allowed: {rejected}")
    if read_sim(strategy) != 0:
        raise SystemExit("Strategy was changed before the risks were accepted")
    result = submit_form(token, rejected, {"accept_risks": True})
    if result.get("type") != "create_entry":
        raise SystemExit(f"Accepting the risks did not save: {result}")
    expect_sim(strategy, 1)
    print("OK: room influence on a sent value needs the risks to be accepted")

    confirm = submit_form(token, open_circuit_form(token, entry_id, 1), data)
    if confirm.get("step_id") != "confirm_risks":
        raise SystemExit(f"Saving while room influence stays on did not ask: {confirm}")
    accept_risks(token, confirm)
    print("OK: saving while room influence stays on asks again")

    set_source_state(token, "22.0")
    expect_sim(room, 220, WRITE_TIMEOUT_SECONDS)
    print("OK: a changed temperature is sent")

    write_sim(strategy, 3)
    set_source_state(token, "23.0")
    time.sleep(WRITE_TIMEOUT_SECONDS)
    if read_sim(room) != 220:
        raise SystemExit("Temperature was sent although the strategy is 3")
    if writer_status(token, entry_id, 1)["blocked_by_control_strategy"] != 3:
        raise SystemExit("Diagnostics don't show the blocking strategy")
    print("OK: nothing is sent at another strategy")

    write_sim(strategy, 1)
    check_fallback(token, entry_id)
    configure_circuit(
        token, entry_id, 1, {"current_temp_source": "none", "room_influence": False}
    )
    expect_sim(room, 0)
    expect_sim(strategy, 0)
    print("OK: switching off resets temperature and strategy")


def check_fallback(token: str, entry_id: str) -> None:
    room, setpoint = ROOM_REGISTERS[1], SETPOINT_REGISTERS[1]
    write_sim(setpoint, 215)
    set_source_state(token, "3.0")
    expect_sim(room, 215, FALLBACK_TIMEOUT_SECONDS)
    if not writer_status(token, entry_id, 1)["fallback_active"]:
        raise SystemExit("Diagnostics don't show the fallback")
    wait_for_current_temperature(token, None)
    print("OK: an unusable source sends the room setpoint after 5 minutes")

    write_sim(setpoint, 200)
    expect_sim(room, 200, WRITE_TIMEOUT_SECONDS)
    print("OK: a changed room setpoint is sent during the fallback")

    set_source_state(token, "22.5")
    expect_sim(room, 225)
    if writer_status(token, entry_id, 1)["fallback_active"]:
        raise SystemExit("The fallback didn't end when the source recovered")
    print("OK: a recovered source is sent again right away")
    set_source_state(token, "23.0")


def wait_for_current_temperature(token: str, expected: float | None) -> None:
    deadline = time.monotonic() + STATE_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        states = request("GET", "/api/states", token=token)
        climate = next(
            s for s in states if s["entity_id"].endswith("_heating_circuit_1")
        )
        current = climate["attributes"].get("current_temperature")
        if current == expected:
            return
        time.sleep(3)
    raise SystemExit(f"current_temperature is {current}, expected {expected}")


def check_reading_room_sensor(token: str, entry_id: str) -> None:
    configure_circuit(
        token, entry_id, 1, {"current_temp_source": "modbus", "room_influence": False}
    )
    wait_for_current_temperature(token, None)
    print("OK: register value 0 is shown as no current temperature")

    write_sim(ROOM_REGISTERS[1], 215)
    wait_for_current_temperature(token, 21.5)
    print("OK: a room sensor's value is shown")

    write_sim(ROOM_REGISTERS[1], 0)
    configure_circuit(
        token, entry_id, 1, {"current_temp_source": "none", "room_influence": False}
    )


def check_sensor_detection(token: str, entry_id: str) -> None:
    room, strategy = ROOM_REGISTERS[2], STRATEGY_REGISTERS[2]
    write_sim(room, 200)
    form = open_circuit_form(token, entry_id, 2)
    if "entity_write" in source_options(form):
        raise SystemExit("Sending is offered although a room sensor was found")
    if form_fields(form)["room_influence"].get("default") is not False:
        raise SystemExit("Strategy change is not offered for a found room sensor")
    result = submit_form(
        token, form, {"current_temp_source": "modbus", "room_influence": True}
    )
    if result.get("type") != "create_entry":
        raise SystemExit(f"Options flow did not save: {result}")
    expect_sim(strategy, 1)
    print("OK: a room sensor is detected and strategy 1 is offered")

    write_sim(STRATEGY_REGISTERS[3], 2)
    if "entity_write" in source_options(open_circuit_form(token, entry_id, 3)):
        raise SystemExit("Sending is offered at strategy 2")
    write_sim(STRATEGY_REGISTERS[3], 1)
    form = open_circuit_form(token, entry_id, 3)
    if form_fields(form)["room_influence"].get("default") is not True:
        raise SystemExit("The checkbox doesn't reflect strategy 1")
    if "modbus" not in source_options(form):
        raise SystemExit("Reading the controller's sensor is not offered")
    print("OK: the offers follow the control strategy")

    # Room influence without any room temperature is refused.
    write_sim(STRATEGY_REGISTERS[3], 0)
    form = open_circuit_form(token, entry_id, 3)
    result = submit_form(
        token, form, {"current_temp_source": "none", "room_influence": True}
    )
    if result.get("errors", {}).get("base") != "room_influence_without_temperature":
        raise SystemExit(f"Room influence without a room temperature was accepted: {result}")
    time.sleep(2)
    if read_sim(STRATEGY_REGISTERS[3]) != 0:
        raise SystemExit("Strategy was changed although saving was refused")
    print("OK: room influence without a room temperature is refused")


def call_service(token: str, domain: str, service: str, data: dict) -> None:
    request("POST", f"/api/services/{domain}/{service}", body=data, token=token)


def entity_state(token: str, suffix: str) -> dict:
    states = request("GET", "/api/states", token=token)
    return next(s for s in states if s["entity_id"].endswith(suffix))


def wait_for_attribute(token: str, suffix: str, check) -> dict:
    deadline = time.monotonic() + STATE_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            state = entity_state(token, suffix)
        except StopIteration:
            state = None
        if state is not None and check(state):
            return state
        time.sleep(2)
    raise SystemExit(f"{suffix} did not reach the expected state: {state}")


def check_cooling(token: str, entry_id: str) -> None:
    climate = "_heating_circuit_1"
    number = "_cooling_flow_setpoint_heating_circuit_1"
    if "cool" in entity_state(token, climate)["attributes"]["hvac_modes"]:
        raise SystemExit("Cooling is offered although the strategy has no cooling")

    write_sim(STRATEGY_REGISTERS[1], 4)
    write_sim(COOLING_FLOW_MIN_HC1, 160)
    configure_circuit(token, entry_id, 1, {"current_temp_source": "none"})
    wait_until_polling(token)
    if "cool" in entity_state(token, "climate.e2e_heat_pump_heating_circuit_1")[
        "attributes"
    ]["hvac_modes"]:
        raise SystemExit("Cooling is offered although cooling is turned off")
    states = {s["entity_id"] for s in request("GET", "/api/states", token=token)}
    if any(eid.endswith(number) for eid in states):
        raise SystemExit("The cooling flow setpoint exists although cooling is off")
    print("OK: without the cooling option, strategy 4 is handled like 0")

    set_experimental(token, entry_id, cooling=True)
    wait_for_attribute(token, climate, lambda s: "cool" in s["attributes"]["hvac_modes"])
    wait_for_attribute(
        token,
        "sensor.e2e_heat_pump_current_cooling_power",
        lambda s: s["state"] not in ("unavailable", "unknown"),
    )
    print("OK: turning cooling on enables the cooling sensors")
    state = wait_for_attribute(token, number, lambda s: s["state"] == "18.0")
    if state["attributes"]["min"] != 16 or state["attributes"]["max"] != 25:
        raise SystemExit(f"Cooling flow range is wrong: {state['attributes']}")
    print("OK: saving the circuit finds cooling and the minimum flow temperature")

    configure_circuit(
        token,
        entry_id,
        1,
        {
            "current_temp_source": "entity_write",
            "current_temp_entity_id": SOURCE_ENTITY,
            "room_influence": True,
        },
    )
    expect_sim(STRATEGY_REGISTERS[1], 5)
    expect_sim(ROOM_REGISTERS[1], 230)
    wait_until_polling(token)
    wait_for_attribute(token, climate, lambda s: "cool" in s["attributes"]["hvac_modes"])
    configure_circuit(
        token, entry_id, 1, {"current_temp_source": "none", "room_influence": False}
    )
    expect_sim(STRATEGY_REGISTERS[1], 4)
    expect_sim(ROOM_REGISTERS[1], 0)
    wait_until_polling(token)
    wait_for_attribute(token, climate, lambda s: "cool" in s["attributes"]["hvac_modes"])
    state = wait_for_attribute(token, number, lambda s: s["state"] == "18.0")
    print("OK: room influence switches between strategies 4 and 5")

    number_id = state["entity_id"]
    climate_id = entity_state(token, climate)["entity_id"]
    write_sim(MODE_REGISTER_HC1, 1)
    write_sim(HEATING_CONSTANT_FLOW_HC1, 690)
    call_service(token, "number", "set_value", {"entity_id": number_id, "value": 20})
    time.sleep(3)
    if read_sim(COOLING_FLOW_SETPOINT_HC1) != 0:
        raise SystemExit("The cooling flow setpoint was written while not cooling")

    call_service(
        token, "climate", "set_hvac_mode", {"entity_id": climate_id, "hvac_mode": "cool"}
    )
    expect_sim(COOLING_FLOW_SETPOINT_HC1, 200)
    if read_sim(HEATING_CONSTANT_FLOW_HC1) != 0:
        raise SystemExit("Cooling didn't set the constant heating flow setpoint to 0")
    if read_sim(MODE_REGISTER_HC1) != 1:
        raise SystemExit("Cooling changed the operating mode")
    state = wait_for_attribute(token, climate, lambda s: s["state"] == "cool")
    if "temperature" in state["attributes"]:
        raise SystemExit("A target temperature is shown while cooling")
    print("OK: cool sets heating flow 0 and writes the cooling flow, mode unchanged")

    call_service(token, "number", "set_value", {"entity_id": number_id, "value": 21})
    expect_sim(COOLING_FLOW_SETPOINT_HC1, 210)
    print("OK: a changed flow temperature is sent while cooling")

    call_service(
        token, "climate", "set_hvac_mode", {"entity_id": climate_id, "hvac_mode": "heat"}
    )
    expect_sim(COOLING_FLOW_SETPOINT_HC1, 0)
    expect_sim(MODE_REGISTER_HC1, 4)
    if read_sim(HEATING_CONSTANT_FLOW_HC1) != 0:
        raise SystemExit("Turning cooling off changed the constant heating flow setpoint")
    print("OK: heat turns cooling off, then writes the operating mode")

    write_sim(STRATEGY_REGISTERS[1], 0)
    try:
        call_service(
            token, "climate", "set_hvac_mode", {"entity_id": climate_id, "hvac_mode": "cool"}
        )
    except urllib.error.HTTPError:
        pass
    else:
        raise SystemExit("Cooling was accepted although the strategy has no cooling")
    if read_sim(COOLING_FLOW_SETPOINT_HC1) != 0:
        raise SystemExit("Cooling was written although the strategy has no cooling")
    print("OK: cooling is refused once the strategy no longer includes it")

    # Turning the cooling option off while the circuit cools.
    write_sim(STRATEGY_REGISTERS[1], 4)
    configure_circuit(token, entry_id, 1, {"current_temp_source": "none"})
    wait_until_polling(token)
    write_sim(COOLING_FLOW_SETPOINT_HC1, 200)
    form = open_options_step(token, entry_id, "experimental")
    confirm = submit_form(token, form, {"experimental_cooling": False})
    if confirm.get("step_id") != "confirm_cooling_off":
        raise SystemExit(f"Turning cooling off while cooling was not confirmed: {confirm}")
    if confirm["description_placeholders"].get("circuits") != "1":
        raise SystemExit(f"The cooling circuits are not named: {confirm}")
    if read_sim(COOLING_FLOW_SETPOINT_HC1) != 200:
        raise SystemExit("Cooling was turned off before the confirmation")
    result = submit_form(token, confirm, {})
    if result.get("type") != "create_entry":
        raise SystemExit(f"Confirming did not save: {result}")
    if read_sim(COOLING_FLOW_SETPOINT_HC1) != 0:
        raise SystemExit("Cooling was not turned off before saving")
    wait_until_polling(token)
    wait_for_attribute(
        token, climate, lambda s: "cool" not in s["attributes"]["hvac_modes"]
    )
    print("OK: turning the cooling option off turns cooling off first, once confirmed")
    write_sim(STRATEGY_REGISTERS[1], 0)
    configure_circuit(token, entry_id, 1, {"current_temp_source": "none"})
    wait_until_polling(token)


def check_flow_setpoint_raise(token: str, entry_id: str) -> None:
    bounds = ("flow_setpoint_raise_min", "flow_setpoint_raise_max")
    number = "_pump_flow_setpoint_raise_heating_circuit_1"
    switch = "_automatic_flow_setpoint_raise_heating_circuit_1"
    if set(bounds) & set(form_fields(open_circuit_form(token, entry_id, 1))):
        raise SystemExit("The flow setpoint raise is offered before it is turned on")

    write_sim(FLOW_SETPOINT_RAISE_HC1, 5)
    write_sim(FLOW_SETPOINT_RAISE_HC2, 7)
    write_sim(BUFFER_SENSOR, 452)
    set_experimental(token, entry_id, flow_setpoint_raise=True)
    state = wait_for_attribute(token, number, lambda s: s["state"] == "0.5")
    if state["attributes"]["max"] != 3:
        raise SystemExit(f"The raise doesn't have the default range: {state['attributes']}")
    wait_for_attribute(token, switch, lambda s: s["state"] == "off")
    wait_for_attribute(
        token, "_suggested_flow_setpoint_raise_heating_circuit_1", lambda s: True
    )
    print("OK: once turned on, heating circuit 1 gets the raise and its adjustment")

    # Heating circuit 2's raise, offered at its strategy 1, is now remembered.
    write_sim(FLOW_SETPOINT_RAISE_HC2, 20)
    if not set(bounds) <= set(form_fields(open_circuit_form(token, entry_id, 2))):
        raise SystemExit("Heating circuit 2's raise range is not offered")
    form = open_circuit_form(token, entry_id, 1)
    if form["description_placeholders"].get("current_raise") != "0.5":
        raise SystemExit(f"The current raise is not shown: {form['description_placeholders']}")
    result = submit_form(
        token, form, {"current_temp_source": "none", bounds[0]: 2.0, bounds[1]: 1.0}
    )
    if result.get("errors", {}).get("base") != "raise_bounds":
        raise SystemExit(f"A lowest raise above the highest was accepted: {result}")
    configure_circuit(
        token, entry_id, 1, {"current_temp_source": "none", bounds[0]: 0.0, bounds[1]: 2.0}
    )
    wait_for_attribute(token, number, lambda s: s["attributes"].get("max") == 2)
    state = wait_for_attribute(
        token, switch, lambda s: s["attributes"].get("maximum") == 2.0
    )
    print("OK: the range set in the circuit applies to the raise and its adjustment")

    number_id = entity_state(token, number)["entity_id"]
    call_service(token, "number", "set_value", {"entity_id": number_id, "value": 1.3})
    state = entity_state(token, number)
    if state["state"] != "1.3":
        raise SystemExit(f"The written value is not shown right away: {state}")
    expect_sim(FLOW_SETPOINT_RAISE_HC1, 13)
    wait_for_attribute(
        token, number, lambda s: s["attributes"].get("change_pending") is False
    )
    print("OK: a written raise is shown right away and confirmed by the next poll")

    set_experimental(token, entry_id, flow_setpoint_raise=False)
    expect_sim(FLOW_SETPOINT_RAISE_HC1, 5)
    expect_sim(FLOW_SETPOINT_RAISE_HC2, 7)
    print("OK: turning the raise off restores the raises from before")

    # Without a buffer sensor only the number entity.
    write_sim(BUFFER_SENSOR, 0)
    set_experimental(token, entry_id, flow_setpoint_raise=True)
    wait_for_attribute(token, number, lambda s: s["state"] == "0.5")
    if entity_state(token, switch)["state"] != "unavailable":
        raise SystemExit("The automatic adjustment exists without a buffer sensor")
    print("OK: without a buffer sensor, there is no suggestion or adjustment")
    write_sim(BUFFER_SENSOR, 452)
    set_experimental(token, entry_id, flow_setpoint_raise=True)
    wait_for_attribute(token, switch, lambda s: s["state"] == "off")
    write_sim(FLOW_SETPOINT_RAISE_HC1, 13)

    # Only at strategies 0 and 1: saving the circuit at another one restores.
    write_sim(STRATEGY_REGISTERS[1], 2)
    form = open_circuit_form(token, entry_id, 1)
    if set(bounds) & set(form_fields(form)):
        raise SystemExit("The flow setpoint raise is offered at strategy 2")
    result = submit_form(token, form, {"current_temp_source": "none"})
    if result.get("type") != "create_entry":
        raise SystemExit(f"Options flow did not save: {result}")
    if read_sim(FLOW_SETPOINT_RAISE_HC1) != 5:
        raise SystemExit("The raise was not restored when the strategy changed")
    wait_until_polling(token)
    print("OK: the raise is only offered at strategies 0 and 1")
    write_sim(STRATEGY_REGISTERS[1], 0)
    configure_circuit(token, entry_id, 1, {"current_temp_source": "none"})
    wait_for_attribute(token, number, lambda s: s["state"] == "0.5")
    write_sim(FLOW_SETPOINT_RAISE_HC1, 13)

    # Not with a buffer module.
    write_sim(BUFFER_MODULE_TEMP, 452)
    form = open_options_step(token, entry_id, "experimental")
    if "experimental_flow_setpoint_raise" in form_fields(form):
        raise SystemExit("The flow setpoint raise is offered with a buffer module")
    result = submit_form(token, form, {})
    if result.get("type") != "create_entry":
        raise SystemExit(f"Experimental functions were not saved: {result}")
    if read_sim(FLOW_SETPOINT_RAISE_HC1) != 5:
        raise SystemExit("The raise was not restored when a buffer module was found")
    wait_until_polling(token)
    print("OK: the raise is not offered with a buffer module")
    write_sim(BUFFER_MODULE_TEMP, 0)
    set_experimental(token, entry_id, flow_setpoint_raise=True)
    wait_for_attribute(token, number, lambda s: s["state"] == "0.5")
    write_sim(FLOW_SETPOINT_RAISE_HC1, 13)


def set_experimental(token: str, entry_id: str, **functions: bool) -> None:
    """Turns experimental functions on or off; the others stay as they are."""
    form = open_options_step(token, entry_id, "experimental")
    result = submit_form(
        token, form, {f"experimental_{name}": on for name, on in functions.items()}
    )
    if result.get("type") != "create_entry":
        raise SystemExit(f"Experimental functions were not saved: {result}")
    wait_until_polling(token)


def check_constant_control(token: str, entry_id: str) -> None:
    climate = "climate.e2e_heat_pump_heating_circuit_1"
    select = "select.e2e_heat_pump_constant_control_heating_circuit_1"
    heating = "number.e2e_heat_pump_constant_heating_flow_setpoint_heating_circuit_1"
    cooling = "number.e2e_heat_pump_constant_cooling_flow_setpoint_heating_circuit_1"
    write_sim(STRATEGY_REGISTERS[1], 3)
    write_sim(FLOW_MAX_HC1, 450)
    write_sim(HEATING_CONSTANT_FLOW_HC1, 0)
    write_sim(COOLING_FLOW_SETPOINT_HC1, 0)
    form = open_circuit_form(token, entry_id, 1)
    if "current_temp_source" in form_fields(form):
        raise SystemExit("A room temperature source is offered at strategy 3")
    result = submit_form(token, form, {})
    if result.get("type") != "create_entry":
        raise SystemExit(f"Options flow did not save: {result}")
    wait_until_polling(token)
    wait_for_attribute(token, climate, lambda s: s["state"] == "unavailable")
    states = {s["entity_id"] for s in request("GET", "/api/states", token=token)}
    if select in states:
        raise SystemExit("Constant control is offered before it is turned on")
    print("OK: strategy 3 without constant control has no climate entity")

    set_experimental(token, entry_id, constant_control=True, cooling=False)
    state = wait_for_attribute(token, select, lambda s: s["state"] == "off")
    if state["attributes"]["options"] != ["heat", "off"]:
        raise SystemExit(f"Cooling is offered without its option: {state['attributes']}")
    state = wait_for_attribute(token, heating, lambda s: s["state"] == "35.0")
    if state["attributes"]["min"] != 20 or state["attributes"]["max"] != 45:
        raise SystemExit(f"Heating flow range is wrong: {state['attributes']}")
    wait_for_attribute(token, climate, lambda s: s["state"] == "unavailable")
    states = {s["entity_id"] for s in request("GET", "/api/states", token=token)}
    if cooling in states:
        raise SystemExit("The cooling flow setpoint exists without its option")
    print("OK: constant control replaces the climate entity once turned on")

    set_experimental(token, entry_id, cooling=True)
    state = wait_for_attribute(token, select, lambda s: s["state"] == "off")
    if state["attributes"]["options"] != ["heat", "off", "cool"]:
        raise SystemExit(f"Cooling is not offered: {state['attributes']}")
    wait_for_attribute(token, cooling, lambda s: s["state"] == "20.0")
    print("OK: cooling is offered with its own option")

    call_service(token, "select", "select_option", {"entity_id": select, "option": "heat"})
    state = entity_state(token, select)
    if state["state"] != "heat" or state["attributes"]["change_pending"] is not True:
        raise SystemExit(f"The chosen mode is not shown right away: {state}")
    time.sleep(5)
    if read_sim(HEATING_CONSTANT_FLOW_HC1) != 0:
        raise SystemExit("The heating setpoint was written without a pause")
    expect_sim(HEATING_CONSTANT_FLOW_HC1, 350, timeout=30)
    if read_sim(COOLING_FLOW_SETPOINT_HC1) != 0:
        raise SystemExit("The cooling setpoint is not 0 while heating")
    wait_for_attribute(
        token,
        select,
        lambda s: s["state"] == "heat" and s["attributes"]["change_pending"] is False,
    )
    print("OK: heating writes cooling 0 first and the heating setpoint after a pause")

    call_service(token, "number", "set_value", {"entity_id": heating, "value": 40})
    expect_sim(HEATING_CONSTANT_FLOW_HC1, 400)
    call_service(token, "number", "set_value", {"entity_id": cooling, "value": 21})
    time.sleep(3)
    if read_sim(COOLING_FLOW_SETPOINT_HC1) != 0:
        raise SystemExit("The cooling setpoint was written while heating")
    if entity_state(token, cooling)["state"] != "21.0":
        raise SystemExit("The cooling setpoint was not kept")
    print("OK: only the setpoint of the active mode is written")

    write_sim(HEATING_CONSTANT_FLOW_HC1, 380)
    wait_for_attribute(token, heating, lambda s: s["state"] == "38.0")
    print("OK: a setpoint changed on the controller is taken over")

    call_service(token, "select", "select_option", {"entity_id": select, "option": "cool"})
    expect_sim(HEATING_CONSTANT_FLOW_HC1, 0)
    expect_sim(COOLING_FLOW_SETPOINT_HC1, 210, timeout=30)
    wait_for_attribute(
        token,
        select,
        lambda s: s["state"] == "cool" and s["attributes"]["change_pending"] is False,
    )
    print("OK: cooling writes heating 0 first and the cooling setpoint after a pause")

    write_sim(HEATING_CONSTANT_FLOW_HC1, 300)
    wait_for_attribute(token, select, lambda s: s["state"] == "unknown")
    print("OK: both setpoints above 0 are shown as unknown")

    call_service(token, "select", "select_option", {"entity_id": select, "option": "off"})
    expect_sim(HEATING_CONSTANT_FLOW_HC1, 0)
    expect_sim(COOLING_FLOW_SETPOINT_HC1, 0)
    wait_for_attribute(token, select, lambda s: s["state"] == "off")
    if entity_state(token, heating)["state"] != "38.0":
        raise SystemExit("Off changed the kept heating setpoint")
    print("OK: off writes both setpoints 0 and keeps them")

    write_sim(STRATEGY_REGISTERS[1], 0)
    configure_circuit(token, entry_id, 1, {"current_temp_source": "none"})
    wait_until_polling(token)
    set_experimental(token, entry_id, constant_control=False, cooling=False)
    wait_for_attribute(token, climate, lambda s: s["state"] != "unavailable")


def check_room_influence_off(token: str, entry_id: str) -> None:
    configure_circuit(
        token,
        entry_id,
        1,
        {
            "current_temp_source": "entity_write",
            "current_temp_entity_id": SOURCE_ENTITY,
            "room_influence": True,
        },
    )
    expect_sim(STRATEGY_REGISTERS[1], 1)
    wait_until_polling(token)
    form = open_options_step(token, entry_id, "experimental")
    result = submit_form(token, form, {"experimental_room_influence": False})
    if result.get("errors", {}).get("base") != "room_influence_sending":
        raise SystemExit(f"Room influence was turned off while sending: {result}")
    if result["description_placeholders"].get("circuits") != "1":
        raise SystemExit(f"The sending circuits are not named: {result}")
    print("OK: room influence can't be turned off while a circuit sends")

    configure_circuit(
        token, entry_id, 1, {"current_temp_source": "none", "room_influence": False}
    )
    expect_sim(STRATEGY_REGISTERS[1], 0)
    wait_until_polling(token)
    # Heating circuit 2: room influence switched on by the integration, with
    # the controller's own room sensor.
    if read_sim(STRATEGY_REGISTERS[2]) != 1:
        raise SystemExit("Heating circuit 2 is not on room influence")
    form = open_options_step(token, entry_id, "experimental")
    confirm = submit_form(token, form, {"experimental_room_influence": False})
    if confirm.get("step_id") != "confirm_room_influence_off":
        raise SystemExit(f"Setting the strategy back was not confirmed: {confirm}")
    if confirm["description_placeholders"].get("circuits") != "2":
        raise SystemExit(f"The circuits to set back are not named: {confirm}")
    if read_sim(STRATEGY_REGISTERS[2]) != 1:
        raise SystemExit("The strategy was set back before the confirmation")
    result = submit_form(token, confirm, {})
    if result.get("type") != "create_entry":
        raise SystemExit(f"Confirming did not save: {result}")
    if read_sim(STRATEGY_REGISTERS[2]) != 0:
        raise SystemExit("The strategy was not set back before saving")
    wait_until_polling(token)
    if "room_influence" in form_fields(open_circuit_form(token, entry_id, 2)):
        raise SystemExit("Room influence is offered after turning it off")
    print("OK: turning room influence off sets back the strategies it changed")
    set_experimental(token, entry_id, room_influence=True)


def check_presence(token: str, entry_id: str) -> None:
    form = open_options_step(token, entry_id, "general")
    fields = form_fields(form)
    if fields["heating_circuit_3_present"].get("default") is not True:
        raise SystemExit(f"Heating circuit 3 was not detected: {fields}")
    if fields["dhw_present"].get("default") is not True:
        raise SystemExit(f"Domestic hot water was not detected: {fields}")
    result = submit_form(
        token, form, {"heating_circuit_3_present": False, "dhw_present": False}
    )
    if result.get("type") != "create_entry":
        raise SystemExit(f"General options were not saved: {result}")
    wait_until_polling(token)
    wait_for_attribute(
        token, "climate.e2e_heat_pump_heating_circuit_3", lambda s: s["state"] == "unavailable"
    )
    wait_for_attribute(token, "water_heater.e2e_heat_pump_water_heater", lambda s: s["state"] == "unavailable")
    menu = request(
        "POST", "/api/config/config_entries/options/flow", body={"handler": entry_id}, token=token
    )
    if "heating_circuit_3" in menu["menu_options"]:
        raise SystemExit(f"An absent heating circuit is in the menu: {menu}")
    print("OK: a heating circuit or hot water that isn't there gets no entities")

    set_general = open_options_step(token, entry_id, "general")
    submit_form(
        token, set_general, {"heating_circuit_3_present": True, "dhw_present": True}
    )
    wait_until_polling(token)
    wait_for_attribute(
        token, "climate.e2e_heat_pump_heating_circuit_3", lambda s: s["state"] != "unavailable"
    )
    wait_for_attribute(token, "water_heater.e2e_heat_pump_water_heater", lambda s: s["state"] != "unavailable")


def check_removal(token: str, entry_id: str) -> None:
    # Heating circuit 3 cools at strategy 4; removing leaves cooling on.
    set_experimental(token, entry_id, cooling=True)
    write_sim(STRATEGY_REGISTERS[3], 4)
    configure_circuit(
        token, entry_id, 3, {"current_temp_source": "none", "room_influence": False}
    )
    wait_until_polling(token)
    write_sim(COOLING_FLOW_SETPOINT_HC3, 200)
    write_sim(FLOW_SETPOINT_RAISE_HC2, 25)
    configure_circuit(
        token,
        entry_id,
        1,
        {
            "current_temp_source": "entity_write",
            "current_temp_entity_id": SOURCE_ENTITY,
            "room_influence": True,
        },
    )
    expect_sim(ROOM_REGISTERS[1], 230)
    expect_sim(STRATEGY_REGISTERS[1], 1)
    form = open_circuit_form(token, entry_id, 1)
    result = submit_form(
        token, form, {"current_temp_source": "none", "room_influence": True}
    )
    if result.get("errors", {}).get("base") != "room_influence_without_temperature":
        raise SystemExit(f"Stopping to send with room influence on was accepted: {result}")
    if read_sim(ROOM_REGISTERS[1]) != 230:
        raise SystemExit("The room temperature was reset although saving was refused")
    print("OK: stopping to send while room influence stays on is refused")
    request("DELETE", f"/api/config/config_entries/entry/{entry_id}", token=token)
    expect_sim(ROOM_REGISTERS[1], 0)
    expect_sim(STRATEGY_REGISTERS[1], 0)
    expect_sim(STRATEGY_REGISTERS[2], 0)
    if read_sim(COOLING_FLOW_SETPOINT_HC3) != 200:
        raise SystemExit("Removal turned cooling off")
    if read_sim(STRATEGY_REGISTERS[3]) != 4:
        raise SystemExit("Removal changed a strategy the integration didn't set")
    if read_sim(ROOM_REGISTERS[2]) != 200:
        raise SystemExit("Removal overwrote the value of a physical room sensor")
    expect_sim(FLOW_SETPOINT_RAISE_HC1, 5)
    expect_sim(FLOW_SETPOINT_RAISE_HC2, 7)
    print("OK: removing the integration resets what it changed")


def main() -> None:
    wait_for_home_assistant()
    token = onboard()
    # Heating circuits 2 and 3 and domestic hot water are detected by a
    # temperature other than 0.
    for address in (*FLOW_TEMP_REGISTERS.values(), DHW_TEMP):
        write_sim(address, 350)
    entry_id = add_integration(token)
    states = wait_for_states(token)
    check(states)
    check_diagnostics(token, entry_id)
    check_timeout_option(token, entry_id)
    check_sending(token, entry_id)
    check_reading_room_sensor(token, entry_id)
    check_sensor_detection(token, entry_id)
    check_cooling(token, entry_id)
    check_flow_setpoint_raise(token, entry_id)
    check_constant_control(token, entry_id)
    check_room_influence_off(token, entry_id)
    check_presence(token, entry_id)
    check_removal(token, entry_id)


if __name__ == "__main__":
    sys.exit(main())
