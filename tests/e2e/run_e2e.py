"""End-to-end check: onboard Home Assistant, add the integration, verify its entities.

Uses only the Python standard library and talks to a Home Assistant instance
started via tests/e2e/docker-compose.yml on localhost:8123.
"""
import json
import os
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

EXPECTED_ENABLED = [
    ("sensor.", "outdoor_temperature"),
    ("sensor.", "_status"),
    ("water_heater.", None),
    ("climate.", "heating_circuit_1"),
]
EXPECTED_DISABLED_SUBSTRINGS = ["smart_grid", "cooling"]


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


def add_integration(token: str) -> None:
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


def main() -> None:
    wait_for_home_assistant()
    token = onboard()
    add_integration(token)
    states = wait_for_states(token)
    check(states)


if __name__ == "__main__":
    sys.exit(main())
