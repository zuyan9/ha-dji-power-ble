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
| DJI Power 2000 | [Hardware Tested](https://github.com/zuyan9/ha-dji-power-ble/issues/17) |

## Features

- AC output control; discharge and recharge limit controls
- Battery level, remaining time, charging time, temperature, and charging status
- Input, output, AC, USB, SDC, etc. power readings
- Expansion battery devices with battery level and pack diagnostics
- [SDC and car-charger controls](docs/accessory-controls.md) reported by the station
- [SDC accessory type, firmware, and per-input solar and car readings](docs/accessory-controls.md#accessory-information-and-input-readings)
- Power 1000: [custom backup reserve level](docs/backup-reserve.md) control
- Power 2000: discharge/recharge watts control, and [electricity price periods](docs/time-periods.md)
- Power 1000 Mini: individual [USB-A and USB-C output switches](docs/accessory-controls.md#usb-outputs)
- Firmware, timezone, display, reserve, and cloud-status diagnostics

## Requirements

- DJI Power Station set up and linked to the DJI account in the DJI Home app
- Home Assistant 2025.8 or newer with Bluetooth, local adapter or Bluetooth proxy
- [HACS](https://hacs.xyz/) installed (recommended method)

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

A DJI Power station accepts only one BLE central at a time. Temporarily disable
or unload the HA config entry when you need to use the DJI Home app via BLE in
offline mode. The app still works via WiFi for cloud access.

## Technical documentation

- [Architecture](docs/architecture.md)
- [BLE protocol reference](docs/protocol.md)
- [Troubleshooting and capture tools](docs/troubleshooting.md)

## Development

The protocol tests are deterministic and require neither Home Assistant nor Bluetooth:

```bash
python3 -m pip install "cryptography>=44.0.0" aiohttp voluptuous
python3 -m unittest discover -v
uvx ruff check custom_components tests scripts
```

The suite includes captured keyed config and `0x61` report payloads, nested interface
trees, Power 1000 encryption vectors, DUML fragmentation/recovery, advertisement model
data, the 64-bit keyed timestamp, charge-limit preservation, and per-key SET
acknowledgements.

## Known gaps

- `0x66` HMS alarm reports remain diagnostics-only; no active-alarm entity is
  implemented.
- SDC rows other than recognized accessory inputs, such as drone-battery charging,
  appear only in diagnostics until their meaning is established.
- Cell-level BMS values are not present on the known app-facing BLE command path.
- DJI can change the optional account-login endpoints at any time.
