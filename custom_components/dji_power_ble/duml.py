"""DJI Power BLE protocol codec.

This module intentionally has no Home Assistant or Bleak dependencies.  It is the
testable protocol boundary used by both the integration and the research tools.
"""

from __future__ import annotations

import contextlib
import dataclasses
import time
from collections.abc import Iterable
from decimal import Decimal, InvalidOperation

SERVICE_UUID = "0000a002-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "0000c304-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000c305-0000-1000-8000-00805f9b34fb"

APP_SOURCE = 0x02
POWER_DESTINATION = 0xAB
POWER_COMMAND_SET = 0x5A

DUML_ENCRYPTION_MASK = 0x0F
POWER_1000_ENCRYPTION_TYPE = 0x06

# Fixed original Power 1000 transport parameters, independent of the pair key.
_POWER_1000_TRANSPORT_KEY = bytes.fromhex(
    "c2ffbc72909d78a160082c3c256e28365899c3d374fc3e6078ea7fbf533fdf29"
)
_POWER_1000_TRANSPORT_IV = bytes.fromhex("bf85b37e2b370a4ab754768d04e6e3d6")

REPORT_COMMAND = 0x61
GET_COMMAND = 0x60
TELEMETRY_COMMAND = 0x62
SET_COMMAND = 0x63
HMS_COMMAND = 0x66
AUTH_COMMAND = 0x6A

START_BIND = 0x00
CHECK_SECRET_KEY = 0x01

EXPANSION_BATTERIES_KEY = 0x01
CHARGE_LIMIT_KEY = 0x05
ENERGY_STORAGE_KEY = 0x06
CAR_CHARGERS_KEY = 0x0A
POWER_SWITCH_KEY = 0x0D
RULES_KEY = 0x0E
TIME_PERIODS_KEY = 0x16
ECO_MODE_KEY = 0x18

TIME_PERIOD_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

_SET_STATE_RULES = (RULES_KEY, bytes.fromhex("0a00") + b"1800efffff")


class ProtocolError(ValueError):
    """A DJI Power payload or DUML frame is malformed."""


def encrypt_power_1000_payload(payload: bytes) -> bytes:
    """Wrap an original Power 1000 payload with AES-256-CBC and PKCS7."""
    if not payload:
        raise ProtocolError("cannot encrypt an empty payload")

    # Keep raw frame decoding available without the optional runtime dependency.
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    padder = padding.PKCS7(algorithms.AES.block_size).padder()
    padded = padder.update(payload) + padder.finalize()
    encryptor = Cipher(
        algorithms.AES(_POWER_1000_TRANSPORT_KEY), modes.CBC(_POWER_1000_TRANSPORT_IV)
    ).encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def decrypt_power_1000_payload(payload: bytes) -> bytes:
    """Unwrap an original Power 1000 payload, validating every padding byte."""
    if not payload or len(payload) % 16:
        raise ProtocolError("encrypted payload must contain complete AES blocks")

    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    decryptor = Cipher(
        algorithms.AES(_POWER_1000_TRANSPORT_KEY), modes.CBC(_POWER_1000_TRANSPORT_IV)
    ).decryptor()
    padded = decryptor.update(payload) + decryptor.finalize()
    unpadder = padding.PKCS7(algorithms.AES.block_size).unpadder()
    try:
        return unpadder.update(padded) + unpadder.finalize()
    except ValueError as error:
        raise ProtocolError("invalid encrypted payload padding") from error


def crc8(data: bytes, initial: int = 0x77) -> int:
    """Return the DUML header CRC8."""
    value = initial
    for byte in data:
        value ^= byte
        for _ in range(8):
            value = (value >> 1) ^ 0x8C if value & 1 else value >> 1
    return value


def crc16(data: bytes, initial: int = 0x3692) -> int:
    """Return the DUML frame CRC16."""
    value = initial
    for byte in data:
        value ^= byte
        for _ in range(8):
            value = (value >> 1) ^ 0x8408 if value & 1 else value >> 1
    return value


@dataclasses.dataclass(frozen=True, slots=True)
class DumlPacket:
    """One DUML v1 frame."""

    source: int
    destination: int
    sequence: int
    flags: int
    command_set: int
    command_id: int
    payload: bytes = b""
    version: int = 1

    @property
    def encryption_type(self) -> int:
        """Return the payload encryption type without modifying the wire frame."""
        return self.flags & DUML_ENCRYPTION_MASK

    @property
    def is_response(self) -> bool:
        """Return whether the response-handler bit is set."""
        return bool(self.flags & 0x80)

    def encode(self) -> bytes:
        """Encode and checksum this frame."""
        for name in ("source", "destination", "flags", "command_set", "command_id"):
            if not 0 <= getattr(self, name) <= 0xFF:
                raise ProtocolError(f"{name} must fit in 8 bits")
        if not 0 <= self.sequence <= 0xFFFF:
            raise ProtocolError("sequence must fit in 16 bits")
        if not 0 <= self.version <= 0x3F:
            raise ProtocolError("version must fit in 6 bits")

        body = bytes((self.source, self.destination))
        body += self.sequence.to_bytes(2, "little")
        body += bytes((self.flags, self.command_set, self.command_id))
        body += self.payload
        length = len(body) + 6
        if length > 0x03FF:
            raise ProtocolError("packet exceeds the DUML 10-bit length field")
        prefix = bytes((0x55, length & 0xFF, (self.version << 2) | (length >> 8)))
        packet = prefix + bytes((crc8(prefix),)) + body
        return packet + crc16(packet).to_bytes(2, "little")

    @classmethod
    def decode(cls, packet: bytes) -> DumlPacket:
        """Validate and decode one complete frame."""
        if len(packet) < 13:
            raise ProtocolError("packet is too short")
        if packet[0] != 0x55:
            raise ProtocolError("invalid DUML magic byte")
        declared_length = packet[1] | ((packet[2] & 0x03) << 8)
        if declared_length != len(packet):
            raise ProtocolError("declared length mismatch")
        if packet[3] != crc8(packet[:3]):
            raise ProtocolError("bad header CRC8")
        if packet[-2:] != crc16(packet[:-2]).to_bytes(2, "little"):
            raise ProtocolError("bad packet CRC16")
        return cls(
            source=packet[4],
            destination=packet[5],
            sequence=int.from_bytes(packet[6:8], "little"),
            flags=packet[8],
            command_set=packet[9],
            command_id=packet[10],
            payload=packet[11:-2],
            version=packet[2] >> 2,
        )


class DumlStream:
    """Incrementally reassemble DUML frames from fragmented notifications."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def clear(self) -> None:
        """Discard buffered partial data."""
        self._buffer.clear()

    def feed(self, data: bytes | bytearray) -> list[DumlPacket]:
        """Append one GATT chunk and return all newly completed valid frames."""
        self._buffer.extend(data)
        return extract_packets(self._buffer)


def extract_packets(buffer: bytearray) -> list[DumlPacket]:
    """Pull complete valid frames from a mutable reassembly buffer."""
    packets: list[DumlPacket] = []
    while buffer:
        start = buffer.find(0x55)
        if start < 0:
            buffer.clear()
            break
        if start:
            del buffer[:start]
        if len(buffer) < 4:
            break

        length = buffer[1] | ((buffer[2] & 0x03) << 8)
        if length < 13 or length > 0x03FF:
            del buffer[0]
            continue
        if len(buffer) < length:
            break

        frame = bytes(buffer[:length])
        try:
            packet = DumlPacket.decode(frame)
        except ProtocolError:
            # A false 0x55 inside corrupt data must not consume a potentially valid
            # frame following it.
            del buffer[0]
            continue
        del buffer[:length]
        packets.append(packet)
    return packets


def normalize_pair_key(value: str | bytes) -> bytes:
    """Validate the 32-character ASCII-hex local credential."""
    try:
        key = value.strip().encode("ascii") if isinstance(value, str) else value.strip()
        if len(key) != 32:
            raise ValueError
        int(key, 16)
    except (UnicodeEncodeError, ValueError) as error:
        raise ProtocolError("pair key must be exactly 32 hex characters") from error
    return key.lower()


@dataclasses.dataclass(frozen=True, slots=True)
class Tlv:
    """One nested 16-bit-tag DJI record."""

    tag: int
    value: bytes


def parse_tlvs(data: bytes, *, strict: bool = False) -> list[Tlv]:
    """Parse sequential ``tag:u16, length:u16, value`` records."""
    records: list[Tlv] = []
    offset = 0
    while offset + 4 <= len(data):
        tag = int.from_bytes(data[offset : offset + 2], "little")
        length = int.from_bytes(data[offset + 2 : offset + 4], "little")
        end = offset + 4 + length
        if end > len(data):
            if strict:
                raise ProtocolError(f"TLV 0x{tag:04x} exceeds its container")
            break
        records.append(Tlv(tag, data[offset + 4 : end]))
        offset = end
    if strict and offset != len(data):
        raise ProtocolError("trailing bytes in TLV container")
    return records


def _records(records: Iterable[Tlv], tag: int) -> list[bytes]:
    return [record.value for record in records if record.tag == tag]


INTERFACE_TYPE_NAMES = {
    0: "unknown",
    1: "power",
    2: "ac",
    3: "usb_a",
    4: "usb_c",
    5: "sdc",
    6: "sdc_lite",
    7: "cigarette",
    8: "xt60",
}

GROUP_TYPE_NAMES = {
    0: "unknown",
    1: "power",
    2: "ac",
    3: "usb",
    4: "sdc",
    5: "cigarette",
    6: "xt60",
}


def _report_records(payload: bytes) -> list[Tlv]:
    # Report pushes have a 16-byte keyed header. Accept a bare TLV tree as well,
    # which keeps the parser useful in capture tooling and focused tests.
    body = (
        payload[16:] if len(payload) >= 20 and payload[2:4] == b"\x10\x00" else payload
    )
    return parse_tlvs(body)


def _parse_input_voltage(port: bytes) -> float | None:
    """Decode the firmware-proven 0x3035 -> 0x3036 input-voltage path."""
    if len(port) < 8:
        return None
    for container in _records(parse_tlvs(port[8:]), 0x3035):
        for record in _records(parse_tlvs(container), 0x3036):
            if len(record) >= 3 and record[0] == 1:
                return int.from_bytes(record[1:3], "little") / 1000
    return None


def parse_report(payload: bytes) -> dict[str, object]:
    """Decode a firmware-proven ``0x5a/0x61`` battery and power push."""
    data: dict[str, object] = {}
    top = _report_records(payload)

    battery_values = _records(top, 0x3020)
    if battery_values:
        battery = battery_values[0]
        if len(battery) >= 9:
            data.update(
                battery_percent=int.from_bytes(battery[0:2], "little") / 100,
                runtime_min=int.from_bytes(battery[2:4], "little"),
                battery_time_type=battery[4],
                primary_battery_percent=int.from_bytes(battery[5:7], "little") / 100,
                primary_runtime_min=int.from_bytes(battery[7:9], "little"),
            )
        # dy301 emits only the first 9 bytes; dy302+ emits temperature as well.
        if len(battery) >= 11:
            data["temperature"] = int.from_bytes(battery[9:11], "little") / 100

    power_values = _records(top, 0x3030)
    if not power_values or len(power_values[0]) < 4:
        return data

    power = power_values[0]
    data["output_w"] = int.from_bytes(power[0:2], "little")
    data["input_w"] = int.from_bytes(power[2:4], "little")
    # time_type remained 2 in a live capture while the station was charging at
    # 380-399 W. Total input power matches the app and cloud charging state.
    data["charging"] = bool(data["input_w"])

    interfaces: list[dict[str, object]] = []
    group_output: dict[int, int] = {}
    group_input: dict[int, int] = {}
    interface_output: dict[str, int] = {}
    interface_input: dict[str, int] = {}

    for interfaces_container in _records(parse_tlvs(power[4:]), 0x3031):
        for group in _records(parse_tlvs(interfaces_container), 0x3032):
            if not group:
                continue
            group_type = group[0]
            wrappers = _records(parse_tlvs(group[1:]), 0x3033)
            for wrapper in wrappers:
                for port in _records(parse_tlvs(wrapper), 0x3034):
                    if len(port) < 7:
                        continue
                    interface_type = port[1]
                    type_name = INTERFACE_TYPE_NAMES.get(interface_type, "unknown")
                    output_w = int.from_bytes(port[3:5], "little")
                    input_w = int.from_bytes(port[5:7], "little")
                    item: dict[str, object] = {
                        "group_type": group_type,
                        "group_name": GROUP_TYPE_NAMES.get(group_type, "unknown"),
                        "seq": port[0],
                        "type": interface_type,
                        "type_name": type_name,
                        "switch_state": port[2],
                        "enabled": port[2] == 1 if port[2] in (1, 2) else None,
                        "output_w": output_w,
                        "input_w": input_w,
                    }
                    if (input_voltage := _parse_input_voltage(port)) is not None:
                        item["input_voltage_v"] = input_voltage
                    interfaces.append(item)
                    group_output[group_type] = (
                        group_output.get(group_type, 0) + output_w
                    )
                    group_input[group_type] = group_input.get(group_type, 0) + input_w
                    interface_output[type_name] = (
                        interface_output.get(type_name, 0) + output_w
                    )
                    interface_input[type_name] = (
                        interface_input.get(type_name, 0) + input_w
                    )
                    data[f"{type_name}_{port[0]}_output_w"] = output_w
                    data[f"{type_name}_{port[0]}_input_w"] = input_w

    data["interfaces"] = interfaces
    data["ac_output_w"] = group_output.get(2, 0)
    data["dc_output_w"] = group_output.get(3, 0)
    data["charge_input_w"] = group_input.get(1, 0)
    for name in ("usb_a", "usb_c", "sdc", "sdc_lite", "cigarette", "xt60"):
        data[f"{name}_output_w"] = interface_output.get(name, 0)
        data[f"{name}_input_w"] = interface_input.get(name, 0)
    for name in ("usb_a", "usb_c", "sdc", "sdc_lite"):
        for sequence in (1, 2):
            data.setdefault(f"{name}_{sequence}_output_w", 0)
            data.setdefault(f"{name}_{sequence}_input_w", 0)
    return data


def _keyed_body(payload: bytes) -> bytes:
    """Return the TLV portion of a 0x60/0x62/0x63 payload."""
    # Check the shared header first: its timestamp can contain the GET marker.
    if len(payload) >= 16 and payload[2:4] == b"\x10\x00":
        return payload[16:]
    if len(payload) >= 20 and payload[6:8] == b"\x10\x00":
        return payload[20:]  # 0x60 response: status + shared 16-byte header
    return payload


def parse_keyed_values(payload: bytes) -> dict[int, bytes]:
    """Parse keyed ``key:u8, marker=0x10, length:u16, value`` records."""
    values: dict[int, bytes] = {}
    body = _keyed_body(payload)
    offset = 0
    while offset + 4 <= len(body):
        key = body[offset]
        marker = body[offset + 1]
        length = int.from_bytes(body[offset + 2 : offset + 4], "little")
        end = offset + 4 + length
        if marker != 0x10 or end > len(body):
            raise ProtocolError("malformed keyed-config TLV")
        values[key] = body[offset + 4 : end]
        offset = end
    if offset != len(body):
        raise ProtocolError("trailing bytes in keyed-config payload")
    return values


def _ascii_field(value: bytes) -> str | None:
    try:
        decoded = value.split(b"\x00", 1)[0].decode("ascii")
    except UnicodeDecodeError:
        return None
    return decoded or None


def _parse_expansion_batteries(value: bytes) -> list[dict[str, object]]:
    """Decode an authoritative expansion-pack list, rejecting ambiguous identity."""
    records = parse_tlvs(value, strict=True)
    rows = _records(records, 0x100F)
    if records and not rows:
        raise ProtocolError("expansion-pack list has no recognized records")

    packs: list[dict[str, object]] = []
    serials: set[str] = set()
    sequences: set[int] = set()
    for row in rows:
        if len(row) < 31:
            raise ProtocolError("expansion-pack record is too short")
        capacity = int.from_bytes(row[7:11], "little")
        if not capacity:
            # The station can include inactive slots with no rated capacity.
            continue
        serial = _ascii_field(row[15:31])
        if serial is None or not serial.strip() or not serial.isprintable():
            raise ProtocolError("expansion-pack record has no valid serial number")
        sequence = row[0]
        if serial in serials or sequence in sequences:
            raise ProtocolError("expansion-pack list has duplicate identities")
        serials.add(serial)
        sequences.add(sequence)

        percentage = int.from_bytes(row[1:3], "little") / 100
        temperature = None
        # Extended app records carry a signed temperature and a validity status:
        # 0 is unknown; 1, 2 and 3 indicate normal, high and low temperature.
        if len(row) >= 34 and row[33] in (1, 2, 3):
            temperature = int.from_bytes(row[31:33], "little", signed=True) / 100
        firmware = _ascii_field(row[34:50]) if len(row) >= 50 else None
        if firmware is not None and (
            not firmware.strip() or not firmware.isprintable()
        ):
            firmware = None
        packs.append(
            {
                "seq": sequence,
                "serial_number": serial,
                "battery_percent": percentage if percentage <= 100 else None,
                "cycle_count": int.from_bytes(row[11:15], "little"),
                "rated_capacity_wh": capacity,
                "temperature": temperature,
                "firmware": firmware,
            }
        )
    return packs


def _parse_power_adjustment(value: bytes | bytearray) -> str:
    """Read power adjustment within an existing grid-tied Time of Use setup."""
    if len(value) < 86:
        raise ProtocolError("eco-mode state must contain at least 86 bytes")
    if value[1] != 3 or value[16] != 3:
        raise ProtocolError("station is not in grid-tied Time of Use mode")
    if value[17] not in (1, 2):
        raise ProtocolError("station reported an unknown power-adjustment mode")
    return "Automatic" if value[17] == 1 else "Manual"


def _parse_manual_discharge_power(value: bytes | bytearray) -> tuple[int, int, int]:
    """Read discharge watts when Time of Use power adjustment is manual."""
    if _parse_power_adjustment(value) != "Manual":
        raise ProtocolError("station is not using manual power adjustment")
    maximum = int.from_bytes(value[30:34], "little")
    minimum = int.from_bytes(value[34:38], "little")
    watts = int.from_bytes(value[38:42], "little")
    if not minimum <= watts <= maximum:
        raise ProtocolError("station reported invalid discharge-power bounds or value")
    return minimum, maximum, watts


def _parse_manual_charge_power(value: bytes | bytearray) -> tuple[int, int, int]:
    """Read charge watts when Time of Use power adjustment is manual."""
    if _parse_power_adjustment(value) != "Manual":
        raise ProtocolError("station is not using manual power adjustment")
    maximum = int.from_bytes(value[18:22], "little")
    minimum = int.from_bytes(value[22:26], "little")
    watts = int.from_bytes(value[26:30], "little")
    if not minimum <= watts <= maximum:
        raise ProtocolError("station reported invalid charge-power bounds or value")
    return minimum, maximum, watts


def _time_period_minutes(value: object) -> int:
    """Validate a wall-clock time with minute precision."""
    if (
        not isinstance(value, str)
        or len(value) != 5
        or not value.isascii()
        or value[2] != ":"
        or not value[:2].isdigit()
        or not value[3:].isdigit()
    ):
        raise ProtocolError("period times must use HH:MM")
    hour, minute = int(value[:2]), int(value[3:])
    if hour > 23 or minute > 59:
        raise ProtocolError("period times must be between 00:00 and 23:59")
    return hour * 60 + minute


def normalize_time_periods(periods: object) -> list[dict[str, object]]:
    """Validate and canonically order recurring peak and off-peak periods.

    Days refer to each period's start day. An end earlier than its start extends
    into the following day, including the Sunday-to-Monday boundary.
    """
    if not isinstance(periods, list):
        raise ProtocolError("periods must be a list")

    canonical: list[tuple[tuple[int, int, int, int], dict[str, object]]] = []
    counts = {"peak": 0, "off_peak": 0}
    intervals: list[tuple[int, int]] = []
    week_minutes = 7 * 24 * 60
    for period in periods:
        if not isinstance(period, dict):
            raise ProtocolError("each period must be an object")
        if set(period) - {"type", "days", "start", "end"}:
            raise ProtocolError("period contains unknown fields")
        kind = period.get("type")
        if kind not in ("peak", "off_peak"):
            raise ProtocolError("period type must be peak or off_peak")
        counts[kind] += 1
        if counts[kind] > 8:
            raise ProtocolError("at most eight periods of each type are allowed")
        days = period.get("days", list(TIME_PERIOD_DAYS))
        if not isinstance(days, list) or not days:
            raise ProtocolError("period days must be a nonempty list")
        if any(day not in TIME_PERIOD_DAYS for day in days):
            raise ProtocolError(
                "period days must use mon, tue, wed, thu, fri, sat, sun"
            )
        if len(set(days)) != len(days):
            raise ProtocolError("period days must not contain duplicates")
        start = _time_period_minutes(period.get("start"))
        end = _time_period_minutes(period.get("end"))
        if start == end:
            raise ProtocolError("period start and end must differ")
        canonical_days = [day for day in TIME_PERIOD_DAYS if day in days]
        mask = sum(1 << TIME_PERIOD_DAYS.index(day) for day in canonical_days)
        canonical.append(
            (
                (1 if kind == "peak" else 2, start, end, mask),
                {
                    "type": kind,
                    "days": canonical_days,
                    "start": period["start"],
                    "end": period["end"],
                },
            )
        )
        for day in canonical_days:
            base = TIME_PERIOD_DAYS.index(day) * 24 * 60
            stop = base + end + (24 * 60 if end < start else 0)
            if stop > week_minutes:
                intervals.append((base + start, week_minutes))
                intervals.append((0, stop - week_minutes))
            else:
                intervals.append((base + start, stop))

    intervals.sort()
    for previous, current in zip(intervals, intervals[1:], strict=False):
        if current[0] < previous[1]:
            raise ProtocolError("periods must not overlap")
    return [period for _, period in sorted(canonical, key=lambda item: item[0])]


def parse_time_periods(value: bytes) -> list[dict[str, object]]:
    """Decode the tariff list with the app's nested-record framing."""
    periods: list[dict[str, object]] = []
    for record in parse_tlvs(value, strict=True):
        # The app binds the row schema through the outer key and ignores the
        # nested tag on reads. Writes use 0x0016; readback tags can differ.
        row = record.value
        if len(row) != 10:
            raise ProtocolError("time-period records must contain exactly ten bytes")
        if row[0] not in (1, 2) or row[1] not in (1, 2):
            raise ProtocolError("time-period record has an unknown type or repetition")
        mask = int.from_bytes(row[2:6], "little")
        if row[1] == 1:
            # Everyday repetition does not consult the weekday mask in the app.
            days = list(TIME_PERIOD_DAYS)
        else:
            if not 1 <= mask <= 0x7F:
                raise ProtocolError("time-period record has an invalid weekday mask")
            days = [
                day for index, day in enumerate(TIME_PERIOD_DAYS) if mask >> index & 1
            ]
        periods.append(
            {
                "type": "peak" if row[0] == 1 else "off_peak",
                "days": days,
                "start": f"{row[6]:02d}:{row[7]:02d}",
                "end": f"{row[8]:02d}:{row[9]:02d}",
            }
        )
    return normalize_time_periods(periods)


_CAR_CHARGER_FIELDS = (
    "p_from_car",
    "p_to_car",
    "v_from_car",
    "v_to_car",
    "v_auto",
)


def parse_car_chargers(value: bytes) -> list[dict[str, int]]:
    """Decode complete accessory rows, retaining unknown enum values."""
    chargers: list[dict[str, int]] = []
    identities: set[tuple[int, int]] = set()
    for record in parse_tlvs(value, strict=True):
        row = record.value
        if len(row) < 65:
            raise ProtocolError("car-charger records must contain at least 65 bytes")
        identity = (row[0], row[1])
        if identity in identities:
            raise ProtocolError("duplicate car-charger interface and sequence")
        identities.add(identity)
        charger = dict(
            zip(("interface_type", "seq", "type", "sw", "mode"), row[:5], strict=True)
        )
        for index, field in enumerate(_CAR_CHARGER_FIELDS):
            for part_index, part in enumerate(("up", "low", "v")):
                offset = 5 + index * 12 + part_index * 4
                charger[f"{field}_{part}"] = int.from_bytes(
                    row[offset : offset + 4], "little"
                )
        chargers.append(charger)
    return chargers


def parse_power_switches(value: bytes) -> list[dict[str, int]]:
    """Decode the reported port switches without assuming their row order."""
    switches: list[dict[str, int]] = []
    identities: set[tuple[int, int]] = set()
    for record in parse_tlvs(value, strict=True):
        row = record.value
        if len(row) < 3:
            raise ProtocolError("power-switch record must contain at least three bytes")
        identity = (row[0], row[1])
        if identity in identities:
            raise ProtocolError("duplicate power-switch type and sequence")
        identities.add(identity)
        switches.append({"type": row[0], "seq": row[1], "sw": row[2]})
    return switches


def parse_telemetry(payload: bytes) -> dict[str, object]:
    """Decode the known fields of a keyed config snapshot/readback."""
    keyed = parse_keyed_values(payload)
    data: dict[str, object] = {
        f"key_{key:02x}": value.hex() for key, value in keyed.items()
    }

    if EXPANSION_BATTERIES_KEY in keyed:
        # A missing key preserves the previous snapshot. An explicit malformed
        # list clears availability without suppressing unrelated station data.
        data["expansion_batteries"] = None
        with contextlib.suppress(ProtocolError):
            data["expansion_batteries"] = _parse_expansion_batteries(
                keyed[EXPANSION_BATTERIES_KEY]
            )

    if len(base := keyed.get(0x00, b"")) >= 40:
        firmware = _ascii_field(base[7:23])
        bms_firmware = _ascii_field(base[24:40])
        if firmware:
            data["firmware"] = firmware
        if bms_firmware:
            data["firmware_secondary"] = bms_firmware

    if network := keyed.get(0x02):
        data["cloud_connected"] = bool(network[0])

    if len(limits := keyed.get(CHARGE_LIMIT_KEY, b"")) == 24:
        values = [int.from_bytes(limits[i : i + 4], "little") for i in range(0, 24, 4)]
        data["recharge_limit"] = values[2]
        data["discharge_limit"] = values[5]

    if len(storage := keyed.get(ENERGY_STORAGE_KEY, b"")) >= 3:
        data["energy_reserve"] = storage[2]

    if len(display := keyed.get(0x0C, b"")) >= 10:
        data["display_timeout_s"] = int.from_bytes(display[0:2], "little")

    if CAR_CHARGERS_KEY in keyed:
        data["car_chargers"] = None
        with contextlib.suppress(ProtocolError):
            data["car_chargers"] = parse_car_chargers(keyed[CAR_CHARGERS_KEY])

    if POWER_SWITCH_KEY in keyed:
        data.update(power_switches=None, ac_enabled=None)
        with contextlib.suppress(ProtocolError):
            switches = parse_power_switches(keyed[POWER_SWITCH_KEY])
            data["power_switches"] = switches
            for switch in switches:
                if (
                    switch["type"] == 2
                    and switch["seq"] == 1
                    and switch["sw"] in (1, 2)
                ):
                    data["ac_enabled"] = switch["sw"] == 1

    if len(timezone := keyed.get(0x15, b"")) == 2:
        data["timezone_offset_min"] = int.from_bytes(timezone, "little", signed=True)

    if TIME_PERIODS_KEY in keyed:
        data["time_periods"] = None
        with contextlib.suppress(ProtocolError):
            data["time_periods"] = parse_time_periods(keyed[TIME_PERIODS_KEY])

    if ECO_MODE_KEY in keyed:
        # An explicit invalid/disabled record must clear previously usable state.
        # Other module snapshots can omit this key and must not clear it.
        data.update(
            power_adjustment=None,
            charge_power_available=False,
            charge_power_min_w=None,
            charge_power_max_w=None,
            charge_power_w=None,
            discharge_power_available=False,
            discharge_power_min_w=None,
            discharge_power_max_w=None,
            discharge_power_w=None,
        )
        with contextlib.suppress(ProtocolError):
            data["power_adjustment"] = _parse_power_adjustment(keyed[ECO_MODE_KEY])
        try:
            minimum, maximum, watts = _parse_manual_charge_power(keyed[ECO_MODE_KEY])
        except ProtocolError:
            pass
        else:
            data.update(
                charge_power_available=True,
                charge_power_min_w=minimum,
                charge_power_max_w=maximum,
                charge_power_w=watts,
            )
        try:
            minimum, maximum, watts = _parse_manual_discharge_power(keyed[ECO_MODE_KEY])
        except ProtocolError:
            pass
        else:
            data.update(
                discharge_power_available=True,
                discharge_power_min_w=minimum,
                discharge_power_max_w=maximum,
                discharge_power_w=watts,
            )
    return data


def build_keyed_header(timestamp_ms: int | None = None) -> bytes:
    """Build the shared 16-byte keyed-config header."""
    if timestamp_ms is None:
        timestamp_ms = int(time.time() * 1000)
    if not 0 <= timestamp_ms <= 0xFFFFFFFFFFFFFFFF:
        raise ProtocolError("timestamp must fit in 64 bits")
    return b"\x00\x00\x10\x00" + timestamp_ms.to_bytes(8, "little") + b"\x00" * 4


def build_keyed_set_payload(
    entries: Iterable[tuple[int, bytes]], *, timestamp_ms: int | None = None
) -> bytes:
    """Build a keyed ``0x63`` SET body."""
    payload = build_keyed_header(timestamp_ms)
    for key, value in entries:
        if not 0 <= key <= 0xFF:
            raise ProtocolError("key id must fit in 8 bits")
        if len(value) > 0xFFFF:
            raise ProtocolError("keyed value is too long")
        payload += bytes((key, 0x10)) + len(value).to_bytes(2, "little") + value
    return payload


def build_time_periods_set_payload(
    periods: object, *, timestamp_ms: int | None = None
) -> bytes:
    """Replace the complete tariff list using the app's SET row format."""
    value = bytearray()
    for period in normalize_time_periods(periods):
        days = period["days"]
        mask = sum(
            1 << index for index, day in enumerate(TIME_PERIOD_DAYS) if day in days
        )
        start = _time_period_minutes(period["start"])
        end = _time_period_minutes(period["end"])
        row = bytes((1 if period["type"] == "peak" else 2, 1 if mask == 0x7F else 2))
        row += mask.to_bytes(4, "little")
        row += bytes((*divmod(start, 60), *divmod(end, 60)))
        value += bytes((TIME_PERIODS_KEY, 0, 10, 0)) + row
    return build_keyed_set_payload(
        [(TIME_PERIODS_KEY, bytes(value)), _SET_STATE_RULES], timestamp_ms=timestamp_ms
    )


def build_ac_set_payload(
    enabled: bool,
    *,
    current_value: str | bytes | None = None,
    timestamp_ms: int | None = None,
) -> bytes:
    """Build the AC SET, preserving other reported switches when supplied."""
    if type(enabled) is not bool:
        raise ProtocolError("AC enabled state must be a boolean")
    state = 0x01 if enabled else 0x02
    if current_value is None:
        value = bytes((0x0D, 0x00, 0x03, 0x00, 0x02, 0x01, state))
    else:
        value = _accessory_snapshot(current_value)
        rows = parse_power_switches(value)
        index = next(
            (
                index
                for index, row in enumerate(rows)
                if (row["type"], row["seq"]) == (2, 1)
            ),
            None,
        )
        if index is None or rows[index]["sw"] not in (1, 2):
            raise ProtocolError("current snapshot has no valid AC main-output switch")
        value = _replace_accessory_row(
            value, POWER_SWITCH_KEY, index, 2, bytes((state,))
        )
    return build_keyed_set_payload(
        [(POWER_SWITCH_KEY, value), _SET_STATE_RULES], timestamp_ms=timestamp_ms
    )


def _accessory_snapshot(current_value: str | bytes) -> bytes:
    """Read a complete snapshot without replacing unavailable state with defaults."""
    try:
        if isinstance(current_value, str):
            return bytes.fromhex(current_value)
        if isinstance(current_value, bytes):
            return current_value
    except ValueError as error:
        raise ProtocolError("accessory state is not valid hex") from error
    raise ProtocolError("accessory state must be bytes or a hex string")


def _validate_sdc_identity(interface_type: int, seq: int) -> None:
    if type(interface_type) is not int or interface_type not in (5, 6):
        raise ProtocolError("accessory controls require an SDC or SDC Lite interface")
    if type(seq) is not int or not 0 <= seq <= 255:
        raise ProtocolError("accessory sequence must be a reported byte")


def _replace_accessory_row(
    value: bytes, key: int, index: int, offset: int, replacement: bytes
) -> bytes:
    """Use app SET tags while preserving every row body and its extended tail."""
    output = bytearray()
    for row_index, record in enumerate(parse_tlvs(value, strict=True)):
        row = bytearray(record.value)
        if row_index == index:
            row[offset : offset + len(replacement)] = replacement
        output += key.to_bytes(2, "little") + len(row).to_bytes(2, "little") + row
    return bytes(output)


def _car_charger_number(row: dict[str, int], field: str, value: int) -> None:
    if row["sw"] != 1 or row["mode"] != 2:
        raise ProtocolError("enable the car charger in Recharge mode before editing it")
    minimum, maximum, current = (row[f"{field}_{part}"] for part in ("low", "up", "v"))
    if maximum == 0 or not minimum <= current <= maximum:
        raise ProtocolError("car charger reported invalid control bounds or setpoint")
    if not minimum <= value <= maximum:
        raise ProtocolError("value is outside the car charger's reported bounds")


def build_car_charger_set_payload(
    current_value: str | bytes,
    interface_type: int,
    seq: int,
    accessory_type: int,
    *,
    enabled: bool | None = None,
    mode: int | None = None,
    recharge_power_w: int | None = None,
    minimum_voltage_v: float | None = None,
    timestamp_ms: int | None = None,
) -> bytes:
    """Edit one reported car-charger setting and preserve the complete list."""
    _validate_sdc_identity(interface_type, seq)
    if type(accessory_type) is not int or accessory_type not in (3, 4):
        raise ProtocolError("car-charger controls require a 1 kW or 1.8 kW accessory")
    if (
        sum(
            setting is not None
            for setting in (enabled, mode, recharge_power_w, minimum_voltage_v)
        )
        != 1
    ):
        raise ProtocolError("change exactly one car-charger setting at a time")
    value = _accessory_snapshot(current_value)
    rows = parse_car_chargers(value)
    index = next(
        (
            index
            for index, row in enumerate(rows)
            if (row["interface_type"], row["seq"], row["type"])
            == (interface_type, seq, accessory_type)
        ),
        None,
    )
    if index is None:
        raise ProtocolError("car charger is absent from the current snapshot")
    row = rows[index]
    if row["sw"] not in (1, 2) or row["mode"] not in (1, 2, 3):
        raise ProtocolError("car charger reported an unknown switch state or mode")
    if enabled is not None:
        if type(enabled) is not bool:
            raise ProtocolError("car-charger enabled state must be a boolean")
        offset, replacement = 3, bytes((1 if enabled else 2,))
    elif mode is not None:
        if type(mode) is not int or mode not in (1, 2, 3):
            raise ProtocolError("car-charger mode must be Auto, Recharge, or Charge")
        if row["sw"] != 1:
            raise ProtocolError("enable the car charger before changing its mode")
        offset, replacement = 4, bytes((mode,))
    elif recharge_power_w is not None:
        if type(recharge_power_w) is not int:
            raise ProtocolError("car-recharging power must be a whole number of watts")
        _car_charger_number(row, "p_from_car", recharge_power_w)
        offset, replacement = 13, recharge_power_w.to_bytes(4, "little")
    else:
        if type(minimum_voltage_v) not in (int, float):
            raise ProtocolError("car-recharging voltage must be a finite number")
        try:
            scaled = Decimal(str(minimum_voltage_v)) * 100
            if not scaled.is_finite() or scaled != scaled.to_integral_value():
                raise ProtocolError("car-recharging voltage must use 0.01 V increments")
            voltage = int(scaled)
        except InvalidOperation as error:
            raise ProtocolError("car-recharging voltage is invalid") from error
        _car_charger_number(row, "v_from_car", voltage)
        offset, replacement = 37, voltage.to_bytes(4, "little")
    updated = _replace_accessory_row(
        value, CAR_CHARGERS_KEY, index, offset, replacement
    )
    return build_keyed_set_payload(
        [(CAR_CHARGERS_KEY, updated), _SET_STATE_RULES], timestamp_ms=timestamp_ms
    )


def build_sdc_switch_set_payload(
    current_value: str | bytes,
    interface_type: int,
    seq: int,
    enabled: bool,
    *,
    timestamp_ms: int | None = None,
) -> bytes:
    """Edit an explicitly reported SDC switch without inventing a port row."""
    _validate_sdc_identity(interface_type, seq)
    if type(enabled) is not bool:
        raise ProtocolError("SDC enabled state must be a boolean")
    value = _accessory_snapshot(current_value)
    rows = parse_power_switches(value)
    index = next(
        (
            index
            for index, row in enumerate(rows)
            if (row["type"], row["seq"]) == (interface_type, seq)
        ),
        None,
    )
    if index is None:
        raise ProtocolError("SDC switch is absent from the current snapshot")
    if rows[index]["sw"] not in (1, 2):
        raise ProtocolError("SDC port reported an unknown switch state")
    updated = _replace_accessory_row(
        value, POWER_SWITCH_KEY, index, 2, bytes((1 if enabled else 2,))
    )
    return build_keyed_set_payload(
        [(POWER_SWITCH_KEY, updated), _SET_STATE_RULES], timestamp_ms=timestamp_ms
    )


def build_charge_limits_set_payload(
    current_value: str | bytes,
    discharge_limit: int,
    recharge_limit: int,
    *,
    timestamp_ms: int | None = None,
) -> bytes:
    """Build a charge-limit SET while preserving the four non-user fields."""
    try:
        value = bytearray(
            bytes.fromhex(current_value)
            if isinstance(current_value, str)
            else current_value
        )
    except ValueError as error:
        raise ProtocolError("charge-limit state is not valid hex") from error
    if len(value) != 24:
        raise ProtocolError("charge-limit state must contain six u32 values")
    if not 0 <= discharge_limit <= 15:
        raise ProtocolError("discharge limit must be between 0 and 15 percent")
    if not 70 <= recharge_limit <= 100:
        raise ProtocolError("recharge limit must be between 70 and 100 percent")
    if discharge_limit >= recharge_limit:
        raise ProtocolError("discharge limit must be lower than recharge limit")
    value[8:12] = recharge_limit.to_bytes(4, "little")
    value[20:24] = discharge_limit.to_bytes(4, "little")
    return build_keyed_set_payload(
        [(CHARGE_LIMIT_KEY, bytes(value))], timestamp_ms=timestamp_ms
    )


def _eco_mode_bytes(current_value: str | bytes) -> bytearray:
    """Copy the complete readback for a single-field eco-mode edit."""
    try:
        return bytearray(
            bytes.fromhex(current_value)
            if isinstance(current_value, str)
            else current_value
        )
    except ValueError as error:
        raise ProtocolError("eco-mode state is not valid hex") from error


def validate_time_periods_mode(
    current_value: str | bytes, *, clearing: bool = False
) -> None:
    """Require known Eco settings and retain periods while scheduling is active."""
    value = _eco_mode_bytes(current_value)
    if len(value) < 86:
        raise ProtocolError("eco-mode state must contain at least 86 bytes")
    if value[1] not in (0, 1, 2, 3):
        raise ProtocolError("station reported an unknown energy-optimization mode")
    if clearing and value[1] not in (0, 1):
        raise ProtocolError(
            "turn off Scheduled Periods or grid-tied operation in DJI Home "
            "before clearing all periods"
        )


def build_discharge_power_set_payload(
    current_value: str | bytes,
    watts: int,
    *,
    timestamp_ms: int | None = None,
) -> bytes:
    """Change only manual discharge watts in the complete eco-mode readback."""
    if type(watts) is not int:
        raise ProtocolError("discharge power must be a whole number of watts")
    value = _eco_mode_bytes(current_value)
    minimum, maximum, _ = _parse_manual_discharge_power(value)
    if not minimum <= watts <= maximum:
        raise ProtocolError(
            f"discharge power must be between {minimum} and {maximum} watts"
        )
    value[38:42] = watts.to_bytes(4, "little")
    return build_keyed_set_payload(
        [(ECO_MODE_KEY, bytes(value))], timestamp_ms=timestamp_ms
    )


def build_charge_power_set_payload(
    current_value: str | bytes,
    watts: int,
    *,
    timestamp_ms: int | None = None,
) -> bytes:
    """Change only manual charge watts in the complete eco-mode readback."""
    if type(watts) is not int:
        raise ProtocolError("charge power must be a whole number of watts")
    value = _eco_mode_bytes(current_value)
    minimum, maximum, _ = _parse_manual_charge_power(value)
    if not minimum <= watts <= maximum:
        raise ProtocolError(
            f"charge power must be between {minimum} and {maximum} watts"
        )
    value[26:30] = watts.to_bytes(4, "little")
    return build_keyed_set_payload(
        [(ECO_MODE_KEY, bytes(value))], timestamp_ms=timestamp_ms
    )


def build_power_adjustment_set_payload(
    current_value: str | bytes,
    mode: str,
    *,
    timestamp_ms: int | None = None,
) -> bytes:
    """Change only Automatic/Manual adjustment in an existing Time of Use setup."""
    if mode not in ("Manual", "Automatic"):
        raise ProtocolError("power adjustment must be Manual or Automatic")
    value = _eco_mode_bytes(current_value)
    _parse_power_adjustment(value)
    if mode == "Automatic":
        try:
            meter_linked = bool(value[42:79].split(b"\x00", 1)[0].decode("utf-8"))
        except UnicodeDecodeError:
            meter_linked = False
        if not meter_linked:
            raise ProtocolError(
                "link a smart meter in DJI Home before selecting Automatic"
            )
    value[17] = 1 if mode == "Automatic" else 2
    return build_keyed_set_payload(
        [(ECO_MODE_KEY, bytes(value))], timestamp_ms=timestamp_ms
    )


def parse_set_ack(payload: bytes, expected_keys: Iterable[int]) -> None:
    """Validate every per-key status in a ``0x63`` response."""
    statuses = parse_keyed_values(payload)
    for key in expected_keys:
        value = statuses.get(key)
        if value is None:
            raise ProtocolError(f"SET acknowledgement omitted key 0x{key:02x}")
        if len(value) != 4:
            raise ProtocolError(f"SET acknowledgement for key 0x{key:02x} is malformed")
        status = int.from_bytes(value, "little")
        if status:
            raise ProtocolError(f"SET key 0x{key:02x} failed with status {status}")


MODEL_NAMES = {
    0x91: "DJI Power 1000",
    0x97: "DJI Power 1000 V2",
    0x98: "DJI Power 1000 Mini",
    0x94: "DJI Power 2000",
}


@dataclasses.dataclass(frozen=True, slots=True)
class AdvertisementInfo:
    """Decoded DJI manufacturer-specific scan-response data."""

    model_code: int
    model: str
    bound: bool
    mac: str | None


def parse_manufacturer_data(value: bytes) -> AdvertisementInfo:
    """Decode manufacturer data with or without the leading company id."""
    if value[:2] == b"\xaa\x08":
        value = value[2:]
    if len(value) < 2:
        raise ProtocolError("DJI manufacturer data is too short")
    model_code = value[0]
    mac = None
    if len(value) >= 9:
        mac = ":".join(f"{byte:02X}" for byte in value[3:9])
    return AdvertisementInfo(
        model_code=model_code,
        model=MODEL_NAMES.get(model_code, f"DJI Power (0x{model_code:02X})"),
        bound=bool(value[1] & 0x10),
        mac=mac,
    )
