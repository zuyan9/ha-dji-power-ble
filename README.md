# DJI Power (local BLE) for Home Assistant

Local Bluetooth monitoring and control for DJI Power stations. Runtime traffic goes
directly between Home Assistant and the station; DJI cloud access is optional and is
used only during setup to retrieve the station's local credential.

> Unofficial community project. Not affiliated with or endorsed by DJI.

## Device support

The **DJI Power 1000 V2** is capture-verified and hardware-tested. The shared protocol
and advertisement model codes are also implemented for these stations, but they still
need hardware validation:

- DJI Power 1000
- DJI Power 1000 Mini
- DJI Power 2000

Please treat writes on those three models as experimental until there are model-specific
captures and live tests.

## Features

- Live battery level, remaining time, temperature, and charging state
- Total input/output and AC, USB-A, USB-C, SDC, SDC Lite, 12 V, and XT60 power
- Individual USB-A and USB-C port power
- AC output control
- Discharge and recharge limit controls
- Firmware, timezone, display, reserve, and cloud-status diagnostics
- Automatic discovery, reconnect-on-advertisement, and sanitized HA diagnostics

The integration keeps one authenticated BLE connection open and consumes the station's
roughly 1 Hz telemetry pushes. It does not reconnect and re-authenticate for every
sample. Pushes are coalesced into five-second Home Assistant updates by default to
avoid needless recorder churn while retaining the latest station values. The publish
interval is configurable from 1 to 60 seconds in the integration's options.

## Requirements

- Home Assistant with Bluetooth, either through a local adapter or an ESPHome Bluetooth
  proxy close to the station
- The station's 32-character local `pair_key`

A DJI Power station accepts only one BLE central at a time. While this integration is
loaded, DJI Home cannot connect to the same station. Temporarily disable or unload the
HA config entry when you need to use the phone app.

## Installation

With HACS:

1. Open HACS → Custom repositories.
2. Add `https://github.com/zuyan9/ha-dji-power-ble` as an Integration.
3. Install **DJI Power (local BLE)** and restart Home Assistant.

For a manual installation, copy `custom_components/dji_power_ble` into
`config/custom_components/` and restart Home Assistant.

## Setup

Add **DJI Power** from Settings → Devices & services. A nearby station should also be
discovered automatically. The setup flow offers three credential paths:

1. **DJI account** — sign in and solve DJI's image captcha. The integration reads the
   station's `pair_key`, then discards the password and member token.
2. **Existing DJI member token** — use an `x-member-token` once to fetch the key.
3. **Manual pair key** — enter the BLE address and 32-character key directly.

Only the pair key and device metadata are stored. Normal operation is local BLE.

## Architecture

The station exposes service `a002`, write characteristic `c304`, and notify
characteristic `c305`. It carries plaintext DJI DUML v1 frames with CRC8/CRC16 checks,
then authenticates each connection with `0x5a/0x6a` and the local pair key.

The implementation separates three concerns:

- `duml.py` is an HA-independent frame, stream, keyed-config, report, and advertisement
  codec.
- `device.py` owns the persistent GATT session, authentication, sequence-matched
  requests, push state, SET acknowledgement validation, and write readback.
- The coordinator and entity platforms only adapt that device state to Home Assistant.

This split and persistent-device approach were informed by the excellent
[`rabits/ha-ef-ble`](https://github.com/rabits/ha-ef-ble) project, while the DJI byte
layouts come from firmware analysis and live captures in the sibling
`dji-power-firmware-research` workspace.

## Development

The protocol tests are deterministic and require neither Home Assistant nor Bluetooth:

```bash
python3 -m unittest discover -v
uvx ruff check custom_components tests
```

The suite includes captured keyed config and `0x61` report payloads, nested interface
trees, DUML fragmentation/recovery, advertisement model data, the 64-bit keyed timestamp,
charge-limit preservation, and per-key SET acknowledgements.

## Known gaps

- Power 1000, Power 1000 Mini, and Power 2000 need live validation.
- `0x66` HMS fault records have only been captured empty, so raw HMS bytes remain
  diagnostics-only.
- SDC output-voltage data is a subtype-dependent union and is not exposed until captures
  with real accessories establish safe entity mappings.
- Cell-level BMS values are not present on the known app-facing BLE command path.
- DJI can change the optional account-login endpoints at any time.

## License

No license is set yet. Until one is added, all rights are reserved by the author.
