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

## Writes and consistency

AC output uses keyed SET entries `0x0D` and `0x0E`. Charge limits use key `0x05`; the
builder changes only the requested limit fields and preserves the station's other four
values. Every SET must return a zero status for every requested key. The client then
polls configuration until the requested state is observed, preventing a successful GATT
write from being mistaken for an applied setting.

Writes are serialized with an operation lock. They remain experimental on models that
do not yet have model-specific hardware validation; see the support table in the
[README](../README.md#device-support).

## Updates and recovery

The station normally pushes telemetry more often than Home Assistant needs to publish
state. The coordinator retains the newest snapshot and coalesces updates to the configured
1–60 second interval (five seconds by default). This limits recorder churn without losing
the latest values.

Connection establishment has a 30-second deadline and individual requests have an
eight-second response timeout. If notification subscription exposes an incomplete GATT
service cache, the client clears the cache, reconnects, and retries once. An unexpected
disconnect marks the coordinator unavailable and schedules a config-entry reload. If the
station is absent during setup, Home Assistant retries after a matching advertisement
reappears.

## Security and diagnostics

The original Power 1000 uses payload encryption with a fixed transport key; the other
implemented model paths use plaintext. The transport key does not replace the station's
pair-key authentication. Treat captures as sensitive even when payloads are encrypted.
Never publish pair keys, DJI account tokens, passwords, serial numbers, BLE addresses,
or raw captures containing them. Downloaded diagnostics redact the address, pair key, and
serial number; account passwords and member tokens are transient and are not stored.
