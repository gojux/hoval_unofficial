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

EXPECTED_ENABLED = [
    ("sensor.", "outdoor_temperature"),
    ("sensor.", "_status"),
    ("water_heater.", None),
    ("climate.", "heating_circuit_1"),
]
EXPECTED_DISABLED_SUBSTRINGS = ["smart_grid", "cooling", "control_strategy"]


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


def open_circuit_form(token: str, entry_id: str, circuit: int) -> dict:
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
            body={"next_step_id": f"heating_circuit_{circuit}"},
            token=token,
        )
        if form.get("reason") != "not_loaded" or time.monotonic() > deadline:
            break
        time.sleep(1)
    if form.get("type") != "form" or form.get("step_id") != "circuit":
        raise SystemExit(f"Options flow did not open the circuit form: {form}")
    return form


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


def configure_circuit(token: str, entry_id: str, circuit: int, data: dict) -> None:
    result = submit_form(token, open_circuit_form(token, entry_id, circuit), data)
    if result.get("type") != "create_entry":
        raise SystemExit(f"Options flow did not save: {result}")


def writer_status(token: str, entry_id: str, circuit: int) -> dict:
    diagnostics = request(
        "GET", f"/api/diagnostics/config_entry/{entry_id}", token=token
    )
    return diagnostics["data"]["room_temp_writers"][f"heating_circuit_{circuit}"]


def check_sending(token: str, entry_id: str) -> None:
    room, strategy = ROOM_REGISTERS[1], STRATEGY_REGISTERS[1]
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
    configure_circuit(
        token,
        entry_id,
        1,
        {"current_temp_source": "entity_write", "current_temp_entity_id": SOURCE_ENTITY},
    )
    expect_sim(room, 215)
    if read_sim(strategy) != 0:
        raise SystemExit("Sending changed the strategy without being asked to")
    print("OK: choosing to send writes the temperature, strategy stays")

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
    expect_sim(strategy, 1)
    print("OK: the checkbox sets strategy 1")

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
    configure_circuit(
        token, entry_id, 1, {"current_temp_source": "none", "room_influence": False}
    )
    expect_sim(room, 0)
    expect_sim(strategy, 0)
    print("OK: switching off resets temperature and strategy")


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

    write_sim(STRATEGY_REGISTERS[3], 3)
    if "entity_write" in source_options(open_circuit_form(token, entry_id, 3)):
        raise SystemExit("Sending is offered at strategy 3")
    write_sim(STRATEGY_REGISTERS[3], 1)
    form = open_circuit_form(token, entry_id, 3)
    if form_fields(form)["room_influence"].get("default") is not True:
        raise SystemExit("The checkbox doesn't reflect strategy 1")
    if "modbus" not in source_options(form):
        raise SystemExit("Reading the controller's sensor is not offered")
    print("OK: the offers follow the control strategy")

    # Room influence without any room temperature needs a confirmation.
    write_sim(STRATEGY_REGISTERS[3], 0)
    form = open_circuit_form(token, entry_id, 3)
    data = {"current_temp_source": "none", "room_influence": True}
    confirm = submit_form(token, form, data)
    if confirm.get("step_id") != "confirm_room_influence":
        raise SystemExit(f"No confirmation was asked for: {confirm}")
    if read_sim(STRATEGY_REGISTERS[3]) != 0:
        raise SystemExit("Strategy was changed before the confirmation")
    back = submit_form(token, confirm, {"next_step_id": "confirm_room_influence_no"})
    if back.get("step_id") != "circuit":
        raise SystemExit(f"Going back did not reopen the form: {back}")
    if form_fields(back)["room_influence"].get("default") is not True:
        raise SystemExit("Going back lost the checkbox")
    confirm = submit_form(token, back, data)
    result = submit_form(token, confirm, {"next_step_id": "confirm_room_influence_yes"})
    if result.get("type") != "create_entry":
        raise SystemExit(f"Confirming did not save: {result}")
    expect_sim(STRATEGY_REGISTERS[3], 1)
    print("OK: room influence without a room temperature needs confirmation")


def check_removal(token: str, entry_id: str) -> None:
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
    request("DELETE", f"/api/config/config_entries/entry/{entry_id}", token=token)
    expect_sim(ROOM_REGISTERS[1], 0)
    expect_sim(STRATEGY_REGISTERS[1], 0)
    expect_sim(STRATEGY_REGISTERS[2], 0)
    expect_sim(STRATEGY_REGISTERS[3], 0)
    if read_sim(ROOM_REGISTERS[2]) != 200:
        raise SystemExit("Removal overwrote the value of a physical room sensor")
    print("OK: removing the integration resets what it changed")


def main() -> None:
    wait_for_home_assistant()
    token = onboard()
    entry_id = add_integration(token)
    states = wait_for_states(token)
    check(states)
    check_diagnostics(token, entry_id)
    check_sending(token, entry_id)
    check_reading_room_sensor(token, entry_id)
    check_sensor_detection(token, entry_id)
    check_removal(token, entry_id)


if __name__ == "__main__":
    sys.exit(main())
