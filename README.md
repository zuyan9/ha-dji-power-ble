# DJI Power (local BLE) — Home Assistant integration

Local **Bluetooth** control and monitoring of the **DJI Power 1000 V2** portable
power station in Home Assistant. Telemetry and writes go straight to the station
over BLE — no DJI cloud at runtime.

> Unofficial / community project. Not affiliated with or endorsed by DJI.

## Features

Entities exposed for a paired station:

- **Switch** — AC output on/off
- **Sensors** — battery %, runtime remaining, input/output power, AC/DC/USB-C/USB-A
  output power, battery temperature, firmware + dongle firmware
- **Numbers** — discharge limit, recharge limit (energy-management charge limits)
- **Binary sensors** — connected, charging

Everything runs over local BLE once set up. The DJI cloud is touched only once, at
setup, and only if you choose the account/token method to fetch the key.

## Requirements

- A DJI Power 1000 V2 station.
- Home Assistant with **Bluetooth** — a built-in adapter or, recommended, an
  **ESPHome Bluetooth proxy placed near the station**. The station's signal must be
  strong and stable enough to hold a connection through authentication.
- The station's 32-character local **`pair_key`** (the integration can fetch this
  for you from your DJI account — see Setup).

> A BLE peripheral serves **one central at a time**. If the DJI Home app on a phone
> is connected to the station, Home Assistant cannot connect. Force-stop the app (or
> keep that phone away) so HA can hold the link.

## Installation

**HACS (custom repository):**
1. HACS → ⋮ → Custom repositories → add `https://github.com/zuyan9/ha-dji-power-ble`,
   category *Integration*.
2. Install **DJI Power (local BLE)**, then restart Home Assistant.

**Manual:** copy `custom_components/dji_power_ble` into your HA `config/custom_components/`
and restart.

## Setup

Add it from **Settings → Devices & Services → Add Integration → "DJI Power"** (a
station advertising nearby is also auto-discovered). Choose how to provide the
`pair_key`:

1. **Fetch from my DJI account** — sign in with the DJI account the station is bound
   to and type the inline image captcha. The integration mints a member token, reads
   the local `pair_key` from the DJI cloud, stores the key, and discards the token.
2. **Paste a DJI member token** — if you already have an `x-member-token`, paste it;
   the key is fetched with no login or captcha.
3. **Enter the pair key manually** — paste the 32-hex key and the BLE address.

After setup, operation is local BLE only.

## How it works

The station speaks a DJI DUML-style protocol over BLE GATT (service `a002`, write
`c304`, notify `c305`). After connecting, the client authenticates with the
`pair_key` (`0x5a/0x6a` challenge–response), reads keyed telemetry (`0x62`) and the
~1 Hz report (`0x61`), and writes settings with keyed `0x63` commands. The link is
unencrypted at the BLE layer; security is the app-layer `pair_key`. Treat the
`pair_key` as a credential.

## Notes & limitations

- Keep a Bluetooth proxy close to the station; weak/edge-of-range signal causes the
  connection to flap and telemetry to stay unavailable.
- AC output voltage is fixed by the hardware model (`DYM1000V2L` = 120 V, `…V2H` =
  220–240 V); it is not reported in BLE telemetry.
- The account-login path reproduces DJI Home's mobile account signing to obtain a
  token. DJI may change this at any time.

## License

No license is set yet. Until one is added, all rights reserved by the author.
