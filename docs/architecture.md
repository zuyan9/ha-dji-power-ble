# Architecture

This document describes the behavior implemented by the integration. It is not a
complete DJI protocol specification, and details may differ between station models or
firmware versions. See the [BLE protocol reference](protocol.md) for wire-level details.

## System overview

Setup and normal operation have separate data paths:

```text
DJI account or token (optional, setup only)
                  │
Bluetooth discovery ──> config flow ──> config entry
                                           │
                                           ▼
HA Bluetooth ──> persistent BLE client ──> DJI Power station
      ▲                    │                         │
      │                    ▼                         │
entities <── coordinator <── decoded state <── notifications
```

The optional cloud flow retrieves the station's local `pair_key`. The password and
member token are discarded after setup. Runtime monitoring and control are local BLE;
only the address, pair key, and device metadata remain in the Home Assistant config
entry.

## Integration layers

- [`config_flow.py`](../custom_components/dji_power_ble/config_flow.py) handles Bluetooth
  discovery and the account, token, and manual credential paths.
- [`__init__.py`](../custom_components/dji_power_ble/__init__.py) resolves a recent
  connectable advertisement, creates the client, and manages config-entry loading.
- [`device.py`](../custom_components/dji_power_ble/device.py) owns the authenticated GATT
  connection, request routing, notification handling, writes, and readback checks.
- [`duml.py`](../custom_components/dji_power_ble/duml.py) is the Home Assistant-independent
  protocol boundary: framing, checksums, stream reassembly, Power 1000 payload encryption,
  payload parsing, and SET construction.
- [`coordinator.py`](../custom_components/dji_power_ble/coordinator.py) adapts device pushes
  to Home Assistant. Entity platforms expose the resulting state.

This split between `duml.py`, `device.py` and `coordinator.py` and persistent-device approach
were informed by the excellent [`rabits/ha-ef-ble`](https://github.com/rabits/ha-ef-ble)
project. Keeping the codec independent makes captured payloads and fragmented frames testable
without Home Assistant or Bluetooth hardware.

## Runtime behavior

Home Assistant discovers a supported advertisement and resolves its current connectable
address before opening GATT. The station accepts one BLE central at a time, so it may be
unavailable while DJI Home is connected.

The client keeps one authenticated connection open, routes responses by request sequence,
and merges periodic notifications into the latest state. The codec reassembles fragmented
notifications and rejects invalid checksums before any state reaches the coordinator.

On connection, the integration authenticates with the saved local pair key, reads the
initial keyed configuration, and then consumes battery and interface-power pushes. It
authenticates an already-bound station; initial DJI binding and optional cloud key
retrieval are setup concerns, not part of normal runtime. See the
[BLE protocol reference](protocol.md) for GATT UUIDs, framing, commands, and payload
layouts.

On Power 1000, Power 1000 V2, Power 2000, and Power Auro 2000 Elite, the same
connection also reads expansion pack state every 30 seconds, serialized with writes.
Each discovered pack becomes a separate Home Assistant device linked to the station.
Pack serial numbers identify devices and entities independently of connection order.
Missing packs retain their registry entries and history with unavailable sensors,
including after a reload.

After connection setup, every known station model gets a bounded background GET
of the app's 30 settings keys. Success and missing-key replies are accepted;
oversized replies fall back to one key per request. The probe has a 45-second total
limit, with eight seconds per request, and optional failures leave normal telemetry
running. Diagnostics record the returned key IDs and whether discovery completed.

Car-charger settings, switch rows, accessory information, reserve, and station rules
refresh every 30 seconds on every known model, including the Mini. Stations that
returned the Eco record during discovery also refresh it every 30 seconds. Station
rules also arrive in settings reports, which are the only source on some stations,
so a GET reply without them keeps the last reported rules. Pack polling
remains limited to models with expansion batteries. Entity platforms add controls
when their records and feature-specific gates become usable. Reserve uses its
availability flag; schedules require a valid list, Eco availability and rule 5.
The Energy Saver mode needs Eco availability and rule 5; grid-tied modes
additionally need rule 6 and an earlier DJI Home grid-tied setup. Scheduled Periods
controls need that mode to be active. Auto Resume and the meter phase need rule 6
and an active grid-tied mode, plus rule 17 or a linked meter respectively. Existing
TOU power controls require rule 6 and an active TOU setup.
AC and car outlets need valid switch rows; SDC and USB also require rule 11.

Missing or invalid snapshots make affected controls unavailable without removing
their entities. Later valid snapshots restore them. Existing entity IDs are retained.
Transport and pack support still depend on the identified station model.

## Writes and consistency

AC output uses keyed SET entries `0x0D` and `0x0E`. Charge limits use fresh bounds from
`0x05` and preserve the four bound fields. Their SET includes `0x05`, `0x0E`, and a
fresh `0x06` record when present, with its stored reserve adjusted to the new limits.
Every SET must return a zero status for every requested key. The client then
polls configuration until the requested state is observed, preventing a successful GATT
write from being mistaken for an applied setting. Combined limit and reserve changes
require matching fresh readback of both records.

Backup reserve writes use keys `0x06` and `0x0E` and change only the requested switch
or level after a fresh `0x06` read.

Energy Saver writes use keys `0x18` and `0x0E`. Each reads a fresh `0x18` record and
fresh station rules, then changes only the requested field; Scheduled Periods and
Time of Use selections also require fresh, non-empty price periods from `0x16`.

Port switches read the switch list inside the operation lock and use the latest
station rules.
Rule 21 selects a full-list SET; otherwise only the addressed row is sent. Row
bodies and extension bytes are preserved. Car-charger controls use keys `0x0A` and `0x0E`, preserve the full list of
chargers, and validate the selected setting against fresh reported bounds and the
modes that use it; in Auto, the latest station rules decide. These
paths require a fresh matching row after the keyed acknowledgement. Optional reads
and writes share the operation lock, including the confirmation period.

Writes are serialized with an operation lock. See the support table in the
[README](../README.md#device-support).

## Updates and recovery

The station normally pushes telemetry more often than Home Assistant needs to publish
state. The coordinator retains the newest snapshot and coalesces updates to the configured
1–60 second interval (five seconds by default). This limits recorder churn without losing
the latest values.

Connection establishment has a 30-second deadline and individual requests have an
eight-second timeout covering the write and response. If subscription exposes an
incomplete GATT service cache, the client clears the cache, reconnects, and retries once.
An unexpected disconnect marks the coordinator unavailable and schedules a config-entry
reload. If the station is absent during setup, Home Assistant retries after a matching
advertisement reappears.

## Security and diagnostics

The original Power 1000 uses payload encryption with a fixed transport key; the other
implemented model paths use plaintext. The transport key does not replace the station's
pair-key authentication. Treat captures as sensitive even when payloads are encrypted.
Never publish pair keys, DJI account tokens, passwords, serial numbers, BLE addresses,
or raw captures containing them. Downloaded diagnostics redact the name, address, pair key,
and serial number; account passwords and member tokens are transient and are not stored.
Expansion-pack serial numbers and the raw keyed records that carry pack, parallel-device,
or accessory serial numbers are also redacted. Accessory serial numbers in telemetry
reports are discarded during decoding. Eco records keep only their length and
numeric settings, without the linked meter's identifier and brand. Unknown keyed
records and records with identifiers are redacted; understood numeric settings
remain available for diagnosis.
