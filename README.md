# udi-ecobee-poly

A PG3 NodeServer for Ecobee thermostats that uses **your own ecobee.com account credentials** (username/password) instead of UDI's shared developer API key (which Ecobee has disabled).

This plugin uses the same authentication path as Home Assistant's Ecobee integration via the [`python-ecobee-api`](https://pypi.org/project/python-ecobee-api/) library, which authenticates against Ecobee's consumer login backend rather than the deprecated developer API.

## Why this plugin exists

The original [`udi-poly-ecobee`](https://github.com/UniversalDevicesInc-PG3/udi-poly-ecobee) cloud mode depends on UDI's shared developer API key. **Ecobee has disabled UDI's access to that API**, causing all installs to receive HTTP 500 errors. The plugin's HomeKit fallback requires a `udi-poly-homekit` companion plugin that does not yet exist.

This plugin sidesteps the issue entirely — your account, your tokens, your control.

## Features

- ✅ Authenticate with ecobee.com username/password (no developer API key)
- ✅ Auto-discover thermostats on your account
- ✅ Current temperature, heat/cool setpoints, humidity, mode, fan, HVAC state
- ✅ Remote sensor nodes — auto-discovered, temp / humidity / occupancy per sensor
- ✅ Climate commands: Home / Away / Sleep
- ✅ **Hold Away (Indefinite)** — single command for "leaving the house" automations
- ✅ Configurable hold type (`nextTransition` or `indefinite`)
- ✅ Refresh token cached locally — credentials only used for initial login + rotation
- 🚧 Equipment notifications (planned)
- 🚧 Vacation programs (planned)

## Setup

1. Install from the PG3 Store (or sideload):
   ```
   git clone https://github.com/csteenwyk/udi-ecobee-poly
   cd udi-ecobee-poly
   ./install.sh
   ```

2. Add the NodeServer in PG3 admin.

3. In Custom Parameters, set:
   - `username` — your ecobee.com email
   - `password` — your ecobee.com password
   - `hold_type` (optional) — `nextTransition` (default) or `indefinite`

4. Save. The controller node will authenticate, discover thermostats and remote sensors, and create a node for each.

## Driver reference

Each Ecobee Thermostat node exposes:

| Driver  | Meaning              | UOM |
|---------|----------------------|-----|
| `ST`    | Current temperature  | °F  |
| `CLISPH`| Heat setpoint        | °F  |
| `CLISPC`| Cool setpoint        | °F  |
| `CLIMD` | HVAC mode (0=Off, 1=Heat, 2=Cool, 3=Auto, 4=Aux Heat) | index |
| `CLIFS` | Fan mode (0=Auto, 1=On) | index |
| `CLIHUM`| Current humidity     | %   |
| `CLIHCS`| HVAC current state (0=Idle, 1=Heating, 2=Cooling, 3=Fan Only) | index |
| `GV0`   | Connected            | bool |

## Thermostat commands

- **Set Heat** — set the heat setpoint using configured `hold_type` (cool setpoint preserved)
- **Set Cool** — set the cool setpoint using configured `hold_type` (heat setpoint preserved)
- **Set Mode** — change HVAC mode (Off / Heat / Cool / Auto / Aux Heat)
- **Set Fan**  — change fan mode (Auto / On)
- **Set Climate** — switch to Home / Away / Sleep program using configured `hold_type`
- **Hold Away (Indefinite)** — convenience: always switches to Away with indefinite hold, regardless of `hold_type`. Designed for "leaving the house" automations.
- **Resume Program** — release any active hold and resume the scheduled climate program
- **Query** — force an immediate refresh from ecobee.com

## Remote sensor drivers

| Driver  | Meaning           | UOM |
|---------|-------------------|-----|
| `ST`    | Temperature       | °F  |
| `CLIHUM`| Humidity (if supported) | % |
| `GV0`   | Occupancy (0=Unoccupied, 1=Occupied) | index |

## Troubleshooting

**HTTP 500 / authentication failed**

Your password may be incorrect, or Ecobee may temporarily be rate-limiting you. Wait a few minutes and try again. If the issue persists, log in at [ecobee.com](https://ecobee.com) directly to verify your credentials work.

**Refresh token cache**

The refresh token is stored at `<PG3 plugin profile>/.ecobee_state.json`. If authentication appears stuck, delete this file and restart the nodeserver to force a fresh login.

## License

MIT — see [LICENSE](LICENSE).

## Credits

- [`python-ecobee-api`](https://github.com/nkgilley/python-ecobee-api) — the underlying library
- Home Assistant's [ecobee integration](https://github.com/home-assistant/core/tree/dev/homeassistant/components/ecobee) — auth flow reference
- The original [`udi-poly-ecobee`](https://github.com/UniversalDevicesInc-PG3/udi-poly-ecobee) — node structure and PG3 patterns
