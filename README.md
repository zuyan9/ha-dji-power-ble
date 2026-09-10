# DJI Power (local BLE) for Home Assistant

Local Bluetooth monitoring and control for DJI Power stations. Runtime traffic goes
directly between Home Assistant and the device; DJI cloud access is optional and is
used only during setup to retrieve the device's local credential.

> Unofficial community project. Not affiliated with or endorsed by DJI.

## Device support

| Model | Status |
| --- | --- |
| DJI Power 1000 V2 | Hardware-tested |
| DJI Power 1000 Mini | [Partially hardware-tested](https://github.com/zuyan9/ha-dji-power-ble/issues/4) |
| DJI Power 1000 | [Hardware Tested](https://github.com/zuyan9/ha-dji-power-ble/issues/10)|
| DJI Power 2000 | Implemented, needs model-specific testing |

## Features

- Live battery level, remaining time, temperature, and charging state
- Total input/output and AC, USB-A, USB-C, SDC, SDC Lite, 12 V, and XT60 power
- Individual USB-A and USB-C port power
- AC output control
- Discharge and recharge limit controls
- Firmware, timezone, display, reserve, and cloud-status diagnostics
- Automatic discovery, reconnect-on-advertisement, and sanitized HA diagnostics
- Persistent authenticated BLE connection with live telemetry pushes

## Requirements

- DJI Power Station set up and linked to the DJI account in the DJI Home app
- Home Assistant with Bluetooth, either through a local adapter or a Bluetooth proxy
- [HACS](https://hacs.xyz/) installed (recommended method)

A DJI Power station accepts only one BLE central at a time. While this integration is
loaded, DJI Home cannot connect to the same station via Bluetooth. Temporarily disable
or unload the HA config entry when you need to use the DJI Home app.

## Installation

### HACS

[![Open your Home Assistant instance and open this repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=zuyan9&repository=ha-dji-power-ble&category=integration)

Click the badge above, or add the repository manually:

1. Open HACS → Custom repositories.
2. Add `https://github.com/zuyan9/ha-dji-power-ble` as an Integration.
3. Install **DJI Power (local BLE)** and restart Home Assistant.

### Manual

Copy `custom_components/dji_power_ble` into `config/custom_components/` and
restart Home Assistant.

## Setup

Add **DJI Power** from Settings → Devices & services. A nearby station should also be
discovered automatically. The setup flow offers three credential paths:

1. **DJI account** — sign in and solve DJI's image captcha. The integration reads the
   station's `pair_key`, then discards the password and member token.
2. **Existing DJI member token** — use an `x-member-token` once to fetch the key.
3. **Manual pair key** — enter the BLE address and 32-character key directly.

Only the pair key and device metadata are stored. Normal operation is local BLE.

## Technical documentation

- [Architecture](docs/architecture.md)
- [BLE protocol reference](docs/protocol.md)
- [Troubleshooting and capture tools](docs/troubleshooting.md)

## Development

The protocol tests are deterministic and require neither Home Assistant nor Bluetooth:

```bash
python3 -m pip install "cryptography>=44.0.0"
python3 -m unittest discover -v
uvx ruff check custom_components tests scripts
```

The suite includes captured keyed config and `0x61` report payloads, nested interface
trees, Power 1000 encryption vectors, DUML fragmentation/recovery, advertisement model
data, the 64-bit keyed timestamp, charge-limit preservation, and per-key SET
acknowledgements.

## Known gaps

- `0x66` HMS fault records have only been captured empty, so raw HMS bytes remain
  diagnostics-only.
- SDC output-voltage data is a subtype-dependent union and is not exposed until captures
  with real accessories establish safe entity mappings.
- Cell-level BMS values are not present on the known app-facing BLE command path.
- DJI can change the optional account-login endpoints at any time.
