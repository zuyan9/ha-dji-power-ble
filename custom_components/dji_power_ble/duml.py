"""DJI Power BLE protocol codec.

This module intentionally has no Home Assistant or Bleak dependencies.  It is the
testable protocol boundary used by both the integration and the research tools.
"""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Iterable

SERVICE_UUID = "0000a002-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "0000c304-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000c305-0000-1000-8000-00805f9b34fb"

APP_SOURCE = 0x02
POWER_DESTINATION = 0xAB
POWER_COMMAND_SET = 0x5A

REPORT_COMMAND = 0x61
GET_COMMAND = 0x60
TELEMETRY_COMMAND = 0x62
SET_COMMAND = 0x63
HMS_COMMAND = 0x66
AUTH_COMMAND = 0x6A

START_BIND = 0x00
CHECK_SECRET_KEY = 0x01

CHARGE_LIMIT_KEY = 0x05
ENERGY_STORAGE_KEY = 0x06
POWER_SWITCH_KEY = 0x0D
RULES_KEY = 0x0E

_SET_STATE_RULES = (RULES_KEY, bytes.fromhex("0a00") + b"1800efffff")


class ProtocolError(ValueError):
    """A DJI Power payload or DUML frame is malformed."""


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
    if len(payload) >= 20 and payload[6:8] == b"\x10\x00":
        return payload[20:]  # 0x60 response: op/pad + shared 16-byte header
    if len(payload) >= 16 and payload[2:4] == b"\x10\x00":
        return payload[16:]
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


def parse_telemetry(payload: bytes) -> dict[str, object]:
    """Decode the known fields of a keyed config snapshot/readback."""
    keyed = parse_keyed_values(payload)
    data: dict[str, object] = {
        f"key_{key:02x}": value.hex() for key, value in keyed.items()
    }

    if len(base := keyed.get(0x00, b"")) >= 40:
        country = _ascii_field(base[0:2])
        firmware = _ascii_field(base[7:23])
        bms_firmware = _ascii_field(base[24:40])
        if country:
            data["country_code"] = country
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

    if (
        len(power_switch := keyed.get(POWER_SWITCH_KEY, b"")) >= 7
        and power_switch[2:4] == b"\x03\x00"
        and power_switch[4:6] == b"\x02\x01"
        and power_switch[6] in (1, 2)
    ):
        data["ac_enabled"] = power_switch[6] == 1

    if len(timezone := keyed.get(0x15, b"")) == 2:
        data["timezone_offset_min"] = int.from_bytes(timezone, "little", signed=True)
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


def build_ac_set_payload(enabled: bool, *, timestamp_ms: int | None = None) -> bytes:
    """Build the live-verified AC main-output SET."""
    state = 0x01 if enabled else 0x02
    value = bytes((0x0D, 0x00, 0x03, 0x00, 0x02, 0x01, state))
    return build_keyed_set_payload(
        [(POWER_SWITCH_KEY, value), _SET_STATE_RULES], timestamp_ms=timestamp_ms
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
