# Hoval Heat Pump Integration (unofficial)

![Hoval Heat Pump Integration (unofficial)](images/logo.png)

[GitHub repository](https://github.com/gojux/hoval_unofficial) — please
report issues or feature requests there.

Custom component for Home Assistant that connects to a Hoval heat pump
controller via **Modbus TCP** and exposes it as regular Home Assistant
entities — sensors, a domestic hot water (DHW) control, and climate
entities for up to 3 room heating circuits.

Setup is done entirely through the Home Assistant UI — no YAML required.

## Disclaimer

This is an **unofficial**, community-developed integration and is **not
affiliated with, endorsed by, or supported by Hoval**. "Hoval" and any
related trademarks are the property of their respective owner.

This software is provided **"as is", without warranty of any kind**, and
is used **entirely at your own risk**. The author(s) assume **no
liability** for any damage, data loss, equipment malfunction, or other
harm resulting from the use of this integration, including but not
limited to unintended interaction with your heat pump's Modbus
interface.

For architecture details, implementation notes, and instructions on
adapting this integration to a different heat pump model, see
[DEVELOPER.md](DEVELOPER.md).

## Requirements

This integration requires `pymodbus` version `3.11.0` or newer (`3.10.0`
had known issues and is excluded), which Home Assistant installs
automatically. If you're on an older, manually managed Python
environment, make sure `pymodbus>=3.11.0` is installed.

## Installation

### Via HACS

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=gojux&repository=hoval_unofficial&category=integration)

Requires [HACS](https://hacs.xyz) to be installed. Clicking the button
above adds this repository to HACS on your Home Assistant instance;
confirm the dialog there, then install the integration from HACS and
restart Home Assistant.

### Manual

1. Copy `custom_components/hoval_unofficial` into the
   `config/custom_components/` directory of your Home Assistant instance.
2. Restart Home Assistant.

### Setup

1. **Settings → Devices & Services → Add Integration** → search for
   "Hoval Heat Pump Integration (unofficial)".
2. Enter in the dialog:
   - **Name** for this heat pump (used as the device name in HA)
   - **IP address** of the controller
   - **Port** (default: 502)
   - **Modbus device ID** (default: 1)
   - optional: polling interval in seconds

Home Assistant tests the connection directly during setup. If it fails,
an error message is shown in the dialog.

You can change the IP address, port, name, or device ID later via the
entry's three-dot menu → **Reconfigure**.

## What you get

- **Sensors** — outdoor temperature, flow/return temperatures, heat
  quantities, operating hours, COP, and a translated status sensor
  (shows the controller's current status as readable text in your HA
  language, e.g. "Heating mode" / "Defrost" / "Frost protection").
- **Domestic hot water (DHW)** — a `water_heater` entity showing tank
  temperature, target temperature, and operating mode.
- **Room heating circuits (HC1/HC2/HC3)** — `climate` entities for up to
  three heating circuits. Only HC1 is shown by default; if you have
  additional circuits, enable HC2/HC3 under **Settings → Devices &
  Services → this integration → Entities** (they're created but hidden
  by default, since not everyone has more than one circuit).

## Using the heating circuits (climate entities)

Each circuit has 5 operating modes, mapped onto Home Assistant's climate
controls as follows:

| Controller mode | Home Assistant |
|---|---|
| Off | **Off** |
| Constant | **Heat** |
| Week 1 / Week 2 / Eco | **Auto**, with the specific program selected via the **preset** dropdown |

- **Setting a target temperature** only works in **Heat** ("Constant")
  or in **Auto** with the **Eco** preset selected — Week 1/Week 2 are
  driven by the controller's own schedule and can't be overridden
  directly. In those modes the temperature control still shows the
  current setpoint, just without the ability to change it.
- **Switching to Auto** remembers whichever program (Week 1/Week 2/Eco)
  was last active, so you don't need to re-select it every time.
- After changing a mode or temperature, it can take up to ~30 seconds
  for the Hoval controller to actually apply the change — the display
  updates immediately to show what you requested, and a
  `mode_change_pending` / `temperature_change_pending` attribute (visible
  under "Attributes" in the entity's more-info dialog) shows `true` while
  waiting for the controller to confirm it.
- **After changing the mode** (e.g. switching between Constant and Eco),
  the displayed target temperature can take **a few minutes** to catch up
  with the new mode's actual setpoint — longer than the ~30 seconds
  above. This is because the target temperature shown always reflects
  whichever setpoint the controller currently considers "active", and it
  can take the controller a little while internally to switch over to
  the new mode's setpoint after you change modes. This isn't something
  the integration can speed up or track precisely, since it's a side
  effect of the mode change rather than a change you request directly.

### Room temperature source (per circuit)

By default, a heating circuit's current room temperature isn't shown
(only the target temperature). You can change this per circuit under
the entry's three-dot menu → **Configure**:

- **Don't show a current temperature** (default)
- **Read from Heat Pump** — uses the controller's own room sensor, if
  one is installed for that circuit
- **Use a Home Assistant entity** — pick any existing temperature sensor
  (e.g. a separate smart thermostat in that room) to use instead

## Using domestic hot water (water_heater entity)

Same 5 modes as the heating circuits (Off / Constant / Week 1 / Week 2 /
Eco), with **Constant** and **Eco** each having their own target
temperature. Setting a temperature only works in those two modes, same
restriction and reasoning as for the heating circuits above — including
the note above that after switching between Constant and Eco, the
displayed target temperature can take a few minutes to catch up with the
new mode's actual setpoint.

## Options

Available under the entry's three-dot menu → **Configure**:

- **Polling interval** (seconds) — how often the integration reads data
  from the controller.
- **Room temperature source** — see above, configurable per heating
  circuit.

Changing any option takes effect immediately, without needing to remove
and re-add the integration.

## Troubleshooting

**Some text still shows in English after switching Home Assistant to a
different language, or vice versa.** Custom integration translations are
cached by Home Assistant. Try, in order:

1. **Settings → System → Restart** (not just reloading the integration).
2. A hard refresh in your browser (Ctrl/Cmd+Shift+R), or fully closing
   and reopening the mobile app.

**Connection fails during setup.** Double-check the IP address, port,
and that the controller's Modbus interface is enabled and reachable from
your Home Assistant instance (same network / no firewall blocking the
port).

**Connection fails, or works intermittently, when something else is also
talking Modbus to the heat pump** (e.g. you also have the heat pump set
up via Home Assistant's built-in `modbus:` YAML integration, or any other
tool/script polling it). **The Hoval controller's Modbus server only
accepts a single client connection at a time** — a second connection
attempt will fail as long as the first one is still open. Symptoms
include this integration failing to connect, or connections dropping and
reconnecting unpredictably.

The fix is to put a **Modbus TCP proxy** in front of the controller, so
multiple consumers can share a single real connection to it:
[ha-modbusproxy](https://github.com/TCzerny/ha-modbusproxy). Point the
proxy at the heat pump's real IP address, then point *both* this
integration *and* any YAML-based `modbus:` config (if you still need it)
at the proxy's IP address/port instead of the heat pump directly.

**Security note:** since the proxy forwards whatever it receives to the
heat pump's Modbus interface, restrict access to it with firewall rules
(e.g. only allow your Home Assistant server's IP) so it doesn't become an
open, unauthenticated gateway to your heat pump's Modbus interface for
anyone else on your network.

## Known limitations

- **Cooling** (active cooling mode) isn't currently implemented. The
  author doesn't have a cooling-capable installation to test against —
  contributions welcome if you do.
- **Smart Grid control** (e.g. SG-Ready style external control inputs)
  isn't currently exposed by this integration.

## Acknowledgements

- **Christian Kropf** — for providing the initial YAML Modbus
  configuration this integration was built from.
- **Max Stoll** — for support and help, especially regarding the heat
  pump and Modbus specifics.

This integration was developed with AI assistance.

## License

[MIT](LICENSE)
