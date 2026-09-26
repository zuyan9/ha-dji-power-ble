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

The client selects one complete layout from the discovered services on each
connection, preferring `a002` when both are present. Notify and write
characteristics must belong to the same service; selection is independent of model.

| Service | Notify | Write |
| --- | --- | --- |
| `a002` | `c305` | `c304` |
| `fff0` | `fff4` | `fff5` |

Each value expands to `0000xxxx-0000-1000-8000-00805f9b34fb`. Both layouts use
the same DUML framing and write-with-response behavior. Missing layouts or failed
notification setup trigger one cache-clear/reconnect attempt before setup fails.

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

If a complete, decoded operation `0x01` response has status `03`, the client
retries the full handshake once on the same connection: it requests a fresh
challenge and echoes that challenge with the existing key. A second rejection,
other status, malformed challenge, or transport error fails setup. The retry
remains within the existing overall connection deadline. Authentication errors
report the returned status without assuming every rejection means a wrong key.

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

When a `0x62` report addressed to the app requests acknowledgement, the integration
replies with payload `01`, the same sequence and command, reversed addresses, and
response attributes `0x80` plus the model's transport encryption type. This confirms
report delivery; setting writes still require their own ACK and fresh readback.

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
GET requests contain operation `0x00` followed by requested key IDs plus `0x1000`,
each encoded as a little-endian uint16. Initial setup requests base information,
network state, charge limits, energy reserve, display, power switches, and timezone
in one GET for keys `0x00`, `0x02`, `0x05`, `0x06`, `0x0C`, `0x0D`, and `0x15`.
Expansion batteries use a separate `0x01` read; Power 2000 also requests `0x18`
and `0x16`. Each key selects one property, not a group or a complete configuration
snapshot. It decodes these fields:

| Key | Meaning | Exposed values |
| --- | --- | --- |
| `0x00` | Base information | Primary and secondary firmware, battery cycle count, maintenance charging |
| `0x01` | Expansion batteries | Per-pack battery percentage, cycles, rated capacity, optional temperature and firmware |
| `0x02` | Network state | Cloud connected |
| `0x04` | Accessories | Type and firmware of each attached accessory; serial numbers are discarded |
| `0x05` | Charge limits | Recharge and discharge limits |
| `0x06` | Energy storage | Backup reserve availability, switch, and level |
| `0x0C` | Display | Display timeout |
| `0x0D` | Power switch | AC output state; Power 1000 Mini USB output states |
| `0x0E` | Rules | Auto car-charger layout (rule 0) |
| `0x15` | Timezone | UTC offset in minutes |
| `0x18` | Eco mode | Power adjustment mode, manual recharge/discharge watts and watt limits |

Raw keyed values are retained internally as `key_XX` hexadecimal state. Downloaded
diagnostics redact records containing known private identifiers, including expansion
packs (`key_01`), parallel devices (`key_03`), and accessories (`key_04`).

### Base information

Key `0x00` is read at connection and pushed by the station when its contents change.
The Power 1000 V2 sends 53 bytes; the original Power 1000 sends the first 47.

| Offset | Width | Field |
| --- | --- | --- |
| `0` | 4 | Region code, ASCII; not exposed |
| `4` | 2 | Device type; not exposed |
| `6` | 1 | Version status; not exposed |
| `7` | 16 | Primary firmware, ASCII |
| `23` | 1 | Mode; not exposed |
| `24` | 16 | Secondary firmware, ASCII |
| `40` | 4 | Capacity in Wh; not exposed |
| `44` | 2 | Built-in battery cycle count |
| `46` | 1 | Grid connection status; not exposed |
| `47` | 4 | Upgrade time; not exposed |
| `51` | 1 | Lock status; not exposed |
| `52` | 1 | Charge type: 0 unknown, 1 normal, 2 maintenance |

A maintenance charge fills the battery to 100% regardless of the recharge limit, which
DJI Home shows as "Battery maintenance in progress". **Battery maintenance charging** is
on for type 2, off for type 1, and unknown otherwise. It is created only for stations
whose record includes the charge type, so the original Power 1000 does not have it.
Type 2 is decoded as DJI Home interprets it; it has not yet been captured from a
station.

### Expansion batteries

The client requests key `0x01` with `00 01 10`. Its value contains repeated nested
`0x100F` TLVs, one per slot, decoded with the same layout for Power 1000, Power 1000 V2,
and Power 2000. The original Power 1000 retains its encrypted transport. Polling runs
every 30 seconds on these models; keyed pushes can update the same state immediately,
subject to the configured Home Assistant publication interval.

| Row offset | Width | Field |
| --- | --- | --- |
| `0` | 1 | Slot sequence; gaps are preserved |
| `1` | 2 | Battery percentage × 100 |
| `3` | 4 | Reserved capacity-like field; not exposed |
| `7` | 4 | Rated capacity in Wh; zero indicates an inactive slot |
| `11` | 4 | Cycle count |
| `15` | 16 | Pack serial number, ASCII |
| `31` | 2 | Optional signed temperature × 100 °C |
| `33` | 1 | Temperature status: 0 unknown, 1 normal, 2 high, 3 low |
| `34` | 16 | Optional firmware version, ASCII |

Rows require the 31-byte common prefix. Temperature requires its complete field and a
recognized status of 1–3; firmware requires all 16 bytes. Additional tail bytes are
accepted. An absent temperature field does not create a temperature entity. Rated
capacity is nominal capacity, not remaining energy or battery health.

An explicit empty list marks all packs absent. Unrelated keyed pushes preserve the
last pack list. A failed targeted read, malformed list, or ambiguous pack identity
makes pack sensors unavailable until a valid snapshot arrives. Pack identity uses
serial numbers, so slot changes preserve entity history. The layout is supported by
firmware/app analysis and synthetic tests; populated physical-pack captures remain
needed to validate runtime behavior across models.

## Telemetry report

Command `0x61` starts with the same 16-byte header and then uses nested records of
`tag:u16`, `length:u16`, and `value[length]`.

| Tag | Meaning |
| --- | --- |
| `0x3020` | Battery percentage, duration and time type, and optional temperature |
| `0x3030` | Total input/output and nested interface groups |
| `0x3031` | Interface container |
| `0x3032` | Power, AC, USB, SDC, 12 V, or XT60 group |
| `0x3034` | Individual interface record |
| `0x3035` → `0x3036` | Input voltage when present |
| `0x3038` → `0x3039` → `0x303A` | Attached accessory type and per-input rows when present |

An interface record identifies its group, one-based port sequence, type, switch state,
output watts, and input watts. The known types are power, AC, USB-A, USB-C, SDC, SDC
Lite, 12 V, and XT60. The codec exposes both aggregate and per-port values.

An SDC interface can also carry an accessory record. Its 17-byte head holds the
accessory serial number, which the codec discards, followed by the accessory type:
`1` car power outlet cable, `2` solar panel adapter, `3` 1 kW car charger, `4` 1.8 kW
Solar/Car charger, and `5` PoE cable. Each 13-byte `0x303A` row describes one accessory
input: form (`1` solar, `2` car, `3` grid), output and input watts as u16 LE, then
output and input voltage as u32 LE hundredths of a volt. Rows keep the station's order
and omit inputs that carry no power. The integration exposes recognized forms as
sensors; other rows appear only in diagnostics as part of `accessory_inputs`.

Extended battery records include temperature at `0x3020[9:11]`, encoded as signed
16-bit hundredths of a degree Celsius. Shorter records omit temperature.

### Battery time and charging

The battery record's duration (`0x3020[2:4]`, minutes) and time type
(`0x3020[4]`) control the charging and time entities:

| Time type | Charging | Remaining Time | Recharging Time |
| --- | --- | --- | --- |
| `1` | Charging | Unavailable | Reported duration |
| `0` or `2` | Not charging | Reported duration | Unavailable |
| Other or no battery data | Unknown | Unavailable | Unavailable |

External input power does not determine charging status: input can supply the
station's outputs while the battery is not charging. **Not charging** includes
both discharging and neutral operation. Type `0` is treated like `2` for these
entities; the raw type remains available in diagnostic data.

**Remaining Time** replaces the **Runtime remaining** name while retaining its
existing entity ID and history. Its value is unavailable during recharging;
automations that need the charging estimate should use **Recharging Time**.
Reported durations, including zero and 5,940 minutes (99 hours), are preserved.
A 99-hour reading can be a capped estimate or a fallback and does not identify
a distinct sustaining state. Primary battery runtime is a separate reading.

## Writes and acknowledgement

AC output writes use keys `0x0D` and `0x0E`. Key `0x0E` carries the client rules record
that DJI Home 1.6.9 sends with its writes: a u16 LE length followed by the ASCII hex
string `1e00efffff3f`. Charge-limit writes use key `0x05`, a six-value structure in
which the integration changes only the recharge and discharge fields and preserves the
other values from a fresh `0x05` read before writing.

Backup reserve writes use keys `0x06` and `0x0E`. Key `0x06` holds availability
(`1` offered), the switch (`1` on, `2` off), and the level as a u16 LE percentage.
The integration writes only when the setting is offered, changes only the requested
switch or level, and preserves any additional bytes. The station stores the level
without checking it. Like DJI Home, the integration limits it to the discharge limit
plus 5 % through the recharge limit, from a fresh `0x05` read.

Power 2000 manual **Recharge power** and **Discharge power** writes use key `0x18`
(`eco_mode`). The app-derived layout stores each setting as little-endian uint32
values:

| Setting | Maximum offset | Minimum offset | Setpoint offset |
| --- | --- | --- | --- |
| Recharge power (W) | 18 | 22 | 26 |
| Discharge power (W) | 30 | 34 | 38 |

Both controls require grid-tied Time of Use with manual power adjustment:
`mode` (byte 1) = 3, `grid_mode` (byte 16) = 3, and `chg_mode` (byte 17) = 2.
They accept integer watts within their own returned bounds. Recharge power is the
manual Time of Use charging setpoint; it is distinct from the recharge limit (%)
and does not configure a general AC charging-power cap.

The client reads a fresh, complete record of at least 86 bytes and replaces only
bytes 26–29 for recharge or 38–41 for discharge, preserving every other byte,
including the opposite setpoint and any extended tail. Missing, incomplete, or
incompatible records leave both numbers unavailable. Invalid bounds or a current
value outside the bounds disable only the affected number.

The Power 2000 **Power adjustment** selector changes only `chg_mode` (byte 17):
**Automatic** = 1, **Manual** = 2. It requires an existing grid-tied Time of Use
configuration and preserves both watt setpoints and all other settings. Automatic
also requires a linked smart meter (`src_dev_id`); link it in DJI Home first.
Both watt numbers are available only in Manual with valid reported limits.
Initial grid installation, Time of Use selection, and meter linking remain in DJI
Home. These controls do not construct missing configuration.

The Power 2000 **Set electricity price time periods** action replaces the complete
tariff schedule in key `0x16`. Scheduled Periods and Time of Use share this list.
Each 10-byte period contains a type, a recurrence type, a 32-bit weekday mask, and
four bytes for the start/end hours and minutes. The list uses outer key `0x1016`
with individual key `0x0016` records on writes. On reads, each nested record is
decoded using the tariff schema, independent of its child tag. The integration
validates up to eight periods per tariff type, rejects overlaps across midnight
and week boundaries, and uses
the station's timezone without changing it. An explicit empty list is distinct
from an omitted or malformed key.

Schedule writes require fresh schedule and Eco configuration, preserve the selected
mode, and include the same `0x0E` rules record as AC output writes. Both keys require
successful acknowledgements, followed by matching tariff readback. Clearing the
last period is rejected while scheduled or grid operation is active. Schedule
entities, reads, and writes are restricted to Power 2000. The codec and offline
confirmation tests do not establish physical Power 2000 schedule execution.

A `0x63` response contains a four-byte status for each requested key. Every key must be
present with status zero. An acknowledgement means the command was accepted, not that
the new state is already observable, so the client polls keyed configuration until the
requested values appear or the operation times out.

The first confirmation read follows the acknowledgement immediately. If the reported
values do not match, the client makes up to eight further attempts, waiting two
seconds between attempts. Each read also has a transport timeout. AC output is
confirmed with an explicit `0x0D` GET, percentage limits with `0x05`, Power 2000
power controls with `0x18`, and tariff periods with `0x16`. Unrelated expansion-battery
or configuration reads are excluded from write confirmation. Cached values cannot
substitute for missing readback fields.
Writes remain serialized until confirmation completes, and confirmed values are
published immediately regardless of the Home Assistant update interval.

The Power 2000 controls' encoding and mode checks are verified against the app.
Manual discharge control has been reported working on hardware.

## SDC and car-charger configuration

Optional SDC controls are enabled for Power 1000, Power 1000 V2, and Power 2000
when their configuration reports supported rows. A telemetry interface alone is
insufficient to create a switch.

Key `0x04` (`accessories`) lists attached accessories in 33-byte rows: a 16-byte
serial number, the accessory type, and a 16-byte ASCII firmware version. The
integration keeps only the type and firmware and matches each row to a reported port
by type and port order. It reads this key with the charger settings every 30 seconds.

Key `0x0A` (`car_charges`) contains nested charger rows with a 65-byte fixed prefix:

| Byte offset | Field | Encoding |
| --- | --- | --- |
| 0 / 1 / 2 | Interface type / sequence / accessory type | u8 each |
| 3 / 4 | Switch / operation mode | u8 each |
| 5 / 9 / 13 | Car-to-station power maximum / minimum / setting | u32 LE, W |
| 17 / 21 / 25 | Station-to-car power maximum / minimum / setting | u32 LE, W |
| 29 / 33 / 37 | Car-to-station voltage maximum / minimum / setting | u32 LE, 0.01 V |
| 41 / 45 / 49 | Station-to-car voltage maximum / minimum / setting | u32 LE, 0.01 V |
| 53 / 57 / 61 | Auto voltage maximum / minimum / setting | u32 LE, 0.01 V |

Switch values are `1` enabled and `2` disabled. Modes are `1` Auto, `2` Recharge
(car to station), and `3` Charge (station to car). Supported charger types are
`3` (1 kW) and `4` (1.8 kW Solar/Car); supported interfaces are `5` (SDC) and
`6` (SDC Lite). The sequence is taken from the reported row, including zero.
The implementation exposes the switch, the mode, and all five settings. A setting
is available and writable only while the charger is enabled in a mode where DJI Home
shows it, and only with valid bounds for that field:

| Mode | Settings |
| --- | --- |
| `2` Recharge | Car-to-station power and voltage |
| `3` Charge | Station-to-car power and voltage |
| `1` Auto, rule 0 set | Both powers and the Auto voltage |
| `1` Auto, rule 0 clear or no rules | Both powers and both direction voltages |

A GET of key `0x0E` (`rules`) returns the station's own rules, which differ from the
client record sent with writes. The value is a u16 LE text length followed by ASCII
hex, which the station can end with NUL. The hex decodes to a u16 LE rule count and
a little-endian mask; rule *n* is set when *n* is below the count and mask bit *n*
is `1`. As in DJI Home, text shorter than four characters means no rules. The
integration reads `0x0E` while a charger is reported. Malformed rules or a failed
read leave the Auto layout unknown, and Auto then offers only the two powers.

Key `0x0D` (`power_sw`) contains nested rows beginning with three bytes:
`type, sequence, switch`. AC is type `2`, sequence `1`; USB-A and USB-C use
types `3` and `4`, and SDC and SDC Lite use types `5` and `6`, each with their
reported one-based sequence. All switch writes retain other rows, including AC,
and modify only the addressed switch byte. USB switches are enabled only on
Power 1000 Mini; SDC controls only on Power 1000, Power 1000 V2, and Power 2000.

SET uses child tags `0x000A` and `0x000D` inside outer properties `0x100A` and
`0x100D`. Readback child tags can differ; parsing follows the enclosing property's
schema while validating every nested length. Row bodies and unknown extended tails
are retained during edits. Truncated rows or duplicate port identities invalidate
the snapshot rather than selecting an ambiguous target.

Both accessory SET paths include the same rules key `0x0E` used for AC output.
Each written key must return zero status. Confirmation uses a fresh targeted GET
(`00 0a 10` or `00 0d 10`) and matches the addressed row, with immediate first
read and the existing retry policy. A successful ACK can precede or accompany an
ineffective setting, so it cannot substitute for matching readback. Readback itself
does not prove physical charging or switching behavior.

## Known limits

- HMS `0x66` reports remain raw diagnostics; active-alarm entities are not implemented.
- SDC voltage fields are not exposed without accessory-specific validation.
- Cell-level BMS values are not present on the known app-facing BLE command path.
- Writes remain experimental on models without model-specific hardware tests.

Treat the `pair_key` as a password. Do not publish credentials, device identifiers,
diagnostics containing private data, or raw captures.
