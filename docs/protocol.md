# BLE Protocol Reference

This reference documents the local BLE behavior implemented in
[`duml.py`](../custom_components/dji_power_ble/duml.py) and
[`device.py`](../custom_components/dji_power_ble/device.py). Multi-byte integers are
little-endian unless noted otherwise. The protocol is unofficial and may vary by model
or firmware version.

## Advertising and GATT

DJI Power stations advertise with manufacturer ID `0x08AA`. Manufacturer data contains
a model code, a status byte whose bit 4 indicates bound state, and optionally the BLE
MAC address.

| Code | Model |
| --- | --- |
| `0x91` | DJI Power 1000 |
| `0x97` | DJI Power 1000 V2 |
| `0x98` | DJI Power 1000 Mini |
| `0x94` | DJI Power 2000 |

| Purpose | UUID |
| --- | --- |
| Service | `0000a002-0000-1000-8000-00805f9b34fb` |
| Write | `0000c304-0000-1000-8000-00805f9b34fb` |
| Notify | `0000c305-0000-1000-8000-00805f9b34fb` |

The station accepts one BLE central at a time. Notifications can split frames at any
byte boundary; `DumlStream` buffers chunks and resynchronizes on a valid `0x55` frame.

## DUML v1 frame

```text
55 | length/version | CRC8 | sender | receiver | sequence | attributes |
command set | command ID | payload | CRC16
```

| Offset | Width | Field | Notes |
| --- | --- | --- | --- |
| `0` | 1 | Start | Always `0x55` |
| `1` | 1 | Length low | Low eight bits of total frame length |
| `2` | 1 | Version/length high | Version in bits 7–2; length high bits in 1–0 |
| `3` | 1 | CRC8 | Covers bytes `0..2` |
| `4` | 1 | Sender | Device type and index |
| `5` | 1 | Receiver | Device type and index |
| `6` | 2 | Sequence | Matches requests with responses |
| `8` | 1 | Attributes | Bit 7 marks a response; low nibble selects encryption |
| `9` | 1 | Command set | `0x5A` for DJI Power |
| `10` | 1 | Command ID | Operation within the command set |
| `11` | variable | Payload | Command-specific bytes |
| final 2 | 2 | CRC16 | Covers all preceding frame bytes |

Total length is a 10-bit value and includes the complete frame. CRC8 uses reflected
polynomial `0x8C` with initial value `0x77`. CRC16 uses reflected polynomial `0x8408`
with initial value `0x3692` and is stored little-endian.

The integration sends requests from `0x02` to `0xAB`. Responses set bit 7 and retain
the request sequence. Transport attributes depend on the model:

| Model | Requests | Responses | Pushes | Payload encoding |
| --- | --- | --- | --- | --- |
| Original Power 1000 | `0x26` | `0x86` | `0x06` | AES-256-CBC with PKCS#7 padding |
| Power 1000 V2, Mini, 2000 | `0x20` | `0x80` | `0x00` | Plaintext in the implemented path |

Power 1000 uses a fixed transport key and IV, separate from the account's `pair_key`.
Encryption covers the command payload, including authentication, configuration, and
telemetry. Frame lengths and CRC16 cover the encrypted bytes. The client selects this
profile before its first request and decrypts responses before parsing command fields.
Other encryption types are rejected without interpreting ciphertext as status or state.

## Authentication

The implemented `0x5A` command path requires application-layer authentication on every
connection. These steps describe the decoded payloads:

1. Subscribe to notifications.
2. Send command `0x6A`, operation `0x00` (`startBind`).
3. Receive a success byte and four-byte challenge.
4. Send operation `0x01`, echoing the challenge with the 32-character `pair_key`.
5. Continue only after a zero status response.

On the original Power 1000, the five-byte challenge becomes a 16-byte encrypted
payload with attributes `0x86`. Its first wire byte is ciphertext, so it is not an
authentication status. This transport behavior is verified in firmware `01.00.15.00`
through `01.00.18.00`; physical-device validation of this implementation is pending.

The integration authenticates an already-bound station. Account and token setup are
optional ways to retrieve its existing pair key; they are not part of runtime BLE
traffic.

## Implemented commands

| Command | Direction | Purpose |
| --- | --- | --- |
| `0x60` | Request/response | Keyed configuration GET |
| `0x61` | Station push | Battery and power report |
| `0x62` | Station snapshot | Keyed configuration update/readback |
| `0x63` | Request/response | Keyed configuration SET |
| `0x66` | Station push | HMS data retained raw for diagnostics |
| `0x6A` | Request/response | Local authentication |

## Keyed configuration

Commands `0x60`, `0x62`, and `0x63` share a keyed configuration format. Snapshot and SET
payloads begin with a 16-byte header:

| Offset | Width | Value |
| --- | --- | --- |
| `0` | 2 | Record slot, currently zero |
| `2` | 2 | Marker `0x0010` |
| `4` | 8 | Unix timestamp in milliseconds |
| `12` | 4 | Zero padding |

Each following record is `key:u8`, marker `0x10`, `length:u16`, and `value[length]`.
The integration reads module sweeps `0x01` and `0x04` and decodes these stable fields:

| Key | Meaning | Exposed values |
| --- | --- | --- |
| `0x00` | Base information | Primary and secondary firmware |
| `0x02` | Network state | Cloud connected |
| `0x05` | Charge limits | Recharge and discharge limits |
| `0x06` | Energy storage | Energy reserve |
| `0x0C` | Display | Display timeout |
| `0x0D` | Power switch | AC output state |
| `0x15` | Timezone | UTC offset in minutes |

Unmapped keyed values are retained as `key_XX` hexadecimal diagnostic state.

## Telemetry report

Command `0x61` starts with the same 16-byte header and then uses nested records of
`tag:u16`, `length:u16`, and `value[length]`.

| Tag | Meaning |
| --- | --- |
| `0x3020` | Battery percentage, remaining time, and optional temperature |
| `0x3030` | Total input/output and nested interface groups |
| `0x3031` | Interface container |
| `0x3032` | Power, AC, USB, SDC, 12 V, or XT60 group |
| `0x3034` | Individual interface record |
| `0x3035` → `0x3036` | Input voltage when present |

An interface record identifies its group, one-based port sequence, type, switch state,
output watts, and input watts. The known types are power, AC, USB-A, USB-C, SDC, SDC
Lite, 12 V, and XT60. The codec exposes both aggregate and per-port values.

## Writes and acknowledgement

AC output writes use keys `0x0D` and `0x0E`. Charge-limit writes use key `0x05`, a
six-value structure in which the integration changes only the recharge and discharge
fields and preserves the other values from the latest read.

A `0x63` response contains a four-byte status for each requested key. Every key must be
present with status zero. An acknowledgement means the command was accepted, not that
the new state is already observable, so the client polls keyed configuration until the
requested values appear or the operation times out.

## Known limits

- HMS `0x66` contents are not decoded because non-empty records have not been validated.
- SDC voltage fields are not exposed without accessory-specific validation.
- Cell-level BMS values are not present on the known app-facing BLE command path.
- Writes remain experimental on models without model-specific hardware tests.

Treat the `pair_key` as a password. Do not publish credentials, device identifiers,
diagnostics containing private data, or raw captures.
