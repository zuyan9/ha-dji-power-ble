"""DJI DUML framing + DJI Power local-auth helpers (ported from src/dji_power_ble.py)."""
from __future__ import annotations

import dataclasses
import re
import time

SERVICE_UUID = "0000a002-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "0000c304-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000c305-0000-1000-8000-00805f9b34fb"

APP_SOURCE = 0x02
POWER_DESTINATION = 0xAB
POWER_COMMAND_SET = 0x5A
AUTH_COMMAND = 0x6A
TELEMETRY_COMMAND = 0x62
SET_COMMAND = 0x63
START_BIND = 0x00
CHECK_SECRET_KEY = 0x01

# Keyed-config field ids (see docs/telemetry-decode.md). The app's recovered
# protocol descriptor calls these `chargeLimit`, `powerSwitch`, and `rules`.
CHARGE_LIMIT_KEY = 0x05
POWER_SWITCH_KEY = 0x0D
# AC-toggle captures include this fixed Nva.rules payload alongside powerSwitch.
# The string is hardcoded ASCII text-hex in libapp.so (pp+0x27580); its decoded
# rule bytes are `18 00 ef ff ff`. It is not a session token.
_SET_STATE_RULES = (0x0E, bytes.fromhex("0a00") + b"1800efffff")
_SET_HEADER_SUFFIX = bytes.fromhex("9e01000000000000")


class ProtocolError(ValueError):
    """Malformed DUML packet."""


def crc8(data: bytes, initial: int = 0x77) -> int:
    value = initial
    for byte in data:
        value ^= byte
        for _ in range(8):
            value = (value >> 1) ^ 0x8C if value & 1 else value >> 1
    return value


def crc16(data: bytes, initial: int = 0x3692) -> int:
    value = initial
    for byte in data:
        value ^= byte
        for _ in range(8):
            value = (value >> 1) ^ 0x8408 if value & 1 else value >> 1
    return value


@dataclasses.dataclass(frozen=True)
class DumlPacket:
    source: int
    destination: int
    sequence: int
    flags: int
    command_set: int
    command_id: int
    payload: bytes
    version: int = 1

    def encode(self) -> bytes:
        body = bytes((self.source, self.destination))
        body += self.sequence.to_bytes(2, "little")
        body += bytes((self.flags, self.command_set, self.command_id))
        body += self.payload
        length = 4 + len(body) + 2
        if length > 0x03FF:
            raise ProtocolError("packet exceeds the DUML 10-bit length field")
        prefix = bytes((0x55, length & 0xFF, (self.version << 2) | (length >> 8)))
        packet = prefix + bytes((crc8(prefix),)) + body
        return packet + crc16(packet).to_bytes(2, "little")

    @classmethod
    def decode(cls, packet: bytes) -> "DumlPacket":
        if len(packet) < 13 or packet[0] != 0x55:
            raise ProtocolError("invalid DUML packet")
        declared_length = packet[1] | ((packet[2] & 0x03) << 8)
        if declared_length != len(packet):
            raise ProtocolError("declared length mismatch")
        if packet[3] != crc8(packet[:3]):
            raise ProtocolError("bad header CRC8")
        if packet[-2:] != crc16(packet[:-2]).to_bytes(2, "little"):
            raise ProtocolError("bad packet CRC16")
        return cls(
            source=packet[4], destination=packet[5],
            sequence=int.from_bytes(packet[6:8], "little"),
            flags=packet[8], command_set=packet[9], command_id=packet[10],
            payload=packet[11:-2], version=packet[2] >> 2,
        )


def normalize_pair_key(value: str) -> bytes:
    key = value.strip().encode("ascii")
    if len(key) != 32:
        raise ProtocolError("pair key must be exactly 32 hex characters")
    int(value.strip(), 16)  # validate hex
    return key


def extract_packets(buffer: bytearray) -> list[DumlPacket]:
    """Pull complete DUML packets out of a reassembly buffer (notifications fragment)."""
    out: list[DumlPacket] = []
    while True:
        start = buffer.find(0x55)
        if start == -1:
            buffer.clear(); break
        if start:
            del buffer[:start]
        if len(buffer) < 4:
            break
        length = buffer[1] | ((buffer[2] & 0x03) << 8)
        if length < 13 or length > 0x03FF:
            del buffer[:1]; continue
        if len(buffer) < length:
            break
        frame = bytes(buffer[:length])
        del buffer[:length]
        try:
            out.append(DumlPacket.decode(frame))
        except ProtocolError:
            pass
    return out


REPORT_COMMAND = 0x61

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


def _find_records(payload: bytes, tag: int) -> list[bytes]:
    """Find every `<tag:2 LE><len:2 LE><value>` record in a 0x61 report."""
    records: list[bytes] = []
    needle = tag.to_bytes(2, "little")
    idx = payload.find(needle)
    while idx != -1:
        if idx + 4 > len(payload):
            break
        ln = int.from_bytes(payload[idx + 2:idx + 4], "little")
        if 0 < ln <= 200 and idx + 4 + ln <= len(payload):
            records.append(payload[idx + 4:idx + 4 + ln])
            idx = payload.find(needle, idx + 4 + ln)
            continue
        idx = payload.find(needle, idx + 1)
    return records


def _find_record(payload: bytes, tag: int) -> bytes | None:
    """Find the first `<tag:2 LE><len:2 LE><value>` record in a 0x61 report."""
    records = _find_records(payload, tag)
    return records[0] if records else None


def parse_report(payload: bytes) -> dict[str, object]:
    """Decode the 0x61 1 Hz report (battery + power), verified vs the cloud OSD.

    tag 0x3020 = battery: charge_pct(/100), remain_min, time_type, prim_charge(/100),
                 prim_remain, temp(/100), temp_t.
    tag 0x3030 = power: output_w(u16), input_w(u16), then per-port records.
    """
    out: dict[str, object] = {}
    battery = _find_record(payload, 0x3020)
    if battery and len(battery) >= 11:
        out["battery_percent"] = round(int.from_bytes(battery[0:2], "little") / 100, 1)
        out["runtime_min"] = int.from_bytes(battery[2:4], "little")
        out["temperature"] = round(int.from_bytes(battery[9:11], "little") / 100, 1)
    power = _find_record(payload, 0x3030)
    if power and len(power) >= 4:
        out["output_w"] = int.from_bytes(power[0:2], "little")
        out["input_w"] = int.from_bytes(power[2:4], "little")
        out["charging"] = out["input_w"] > 0
        # 0x3032 group = group_type(1B) + nested 0x3034 interface records.
        # 0x3034 interface = seq(1B) type(1B) sw(1B) output(u16) input(u16).
        # Keep every interface: USB groups contain multiple physical ports.
        interfaces: list[dict[str, object]] = []
        group_output_w: dict[int, int] = {}
        group_input_w: dict[int, int] = {}
        for gval in _find_records(power[4:], 0x3032):
            grp = gval[0] if gval else 0
            for port in _find_records(gval[1:], 0x3034):
                if len(port) >= 7:
                    o = int.from_bytes(port[3:5], "little")
                    in_w = int.from_bytes(port[5:7], "little")
                    interface_type = port[1]
                    interface_name = INTERFACE_TYPE_NAMES.get(interface_type, "unknown")
                    interfaces.append(
                        {
                            "group_type": grp,
                            "group_name": GROUP_TYPE_NAMES.get(grp, "unknown"),
                            "seq": port[0],
                            "type": interface_type,
                            "type_name": interface_name,
                            "sw": port[2],
                            "output_w": o,
                            "input_w": in_w,
                        }
                    )
                    group_output_w[grp] = group_output_w.get(grp, 0) + o
                    group_input_w[grp] = group_input_w.get(grp, 0) + in_w
                    if interface_name in ("usb_a", "usb_c", "sdc", "sdc_lite"):
                        output_key = f"{interface_name}_output_w"
                        input_key = f"{interface_name}_input_w"
                        out[output_key] = int(out.get(output_key, 0)) + o
                        out[input_key] = int(out.get(input_key, 0)) + in_w
                    if interface_name in ("usb_a", "usb_c"):
                        port_output_key = f"{interface_name}_{port[0]}_output_w"
                        out[port_output_key] = int(out.get(port_output_key, 0)) + o
        out["interfaces"] = interfaces
        out["ac_output_w"] = group_output_w.get(2, 0)
        out["dc_output_w"] = group_output_w.get(3, 0)
        out["charge_input_w"] = group_input_w.get(1, 0)
        for interface_name in ("usb_a", "usb_c", "sdc", "sdc_lite"):
            out.setdefault(f"{interface_name}_output_w", 0)
            out.setdefault(f"{interface_name}_input_w", 0)
        for interface_name in ("usb_a", "usb_c"):
            for seq in (1, 2):
                out.setdefault(f"{interface_name}_{seq}_output_w", 0)
    return out


def build_keyed_set_payload(entries: list[tuple[int, bytes]]) -> bytes:
    """Build a 0x63 keyed-config SET body: 16-byte header + `<kid> 0x10 <len:2 LE> value`.

    The header is `00 00 10 00 <ts:4 LE> 9e 01 00*6`; the station does not validate
    the timestamp (confirmed: a stale ts was accepted live), so we send wall-clock ms.
    """
    ts = int(time.time() * 1000) & 0xFFFFFFFF
    payload = bytes((0x00, 0x00, 0x10, 0x00)) + ts.to_bytes(4, "little") + _SET_HEADER_SUFFIX
    for kid, value in entries:
        payload += bytes((kid, 0x10)) + len(value).to_bytes(2, "little") + value
    return payload


def build_ac_set_payload(enabled: bool) -> bytes:
    """SET AC output on/off — verified by btsnoop capture + a live Pi A/B test.

    Key 0x0d (`powerSwitch`) carries `0d 00 03 00 02 01 <01=on|02=off>`;
    key 0x0e carries the fixed `rules` value observed in the app's request.
    """
    state = 0x01 if enabled else 0x02
    ac_value = bytes((0x0D, 0x00, 0x03, 0x00, 0x02, 0x01, state))
    return build_keyed_set_payload([(POWER_SWITCH_KEY, ac_value), _SET_STATE_RULES])


def build_charge_limits_set_payload(
    current_value: str | bytes, discharge_limit: int, recharge_limit: int
) -> bytes:
    """SET energy-management limits while preserving the opaque key 0x05 fields.

    Captured DJI Home readbacks and live local writes map the six u32 values as
    `[100, 70, recharge, 15, 0, discharge]`. The four constant-looking values
    are intentionally copied from the current station value rather than guessed.
    """
    try:
        value = bytearray(bytes.fromhex(current_value) if isinstance(current_value, str) else current_value)
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
    return build_keyed_set_payload([(CHARGE_LIMIT_KEY, bytes(value))])


def parse_telemetry(payload: bytes) -> dict[str, object]:
    """Decode keyed telemetry (cmd 0x62/0x60): `<kid> 0x10 <len:2 LE> <value>`."""
    data: dict[str, object] = {}
    i = 0
    while i + 4 <= len(payload):
        # Key 0 is the baseInfo block. Empty keyed values are also valid.
        if payload[i + 1] == 0x10 and payload[i] < 0x40:
            kid = payload[i]
            ln = int.from_bytes(payload[i + 2:i + 4], "little")
            if i + 4 + ln <= len(payload):
                data[f"key_{kid:02x}"] = payload[i + 4:i + 4 + ln].hex()
                i += 4 + ln
                continue
        i += 1
    versions = re.findall(rb"\d{2}\.\d{2}\.\d{4}", payload)
    if versions:
        data["firmware"] = versions[0].decode()
    if len(versions) > 1:
        data["firmware_secondary"] = versions[1].decode()
    # NOTE: the first 2 bytes of baseInfo (key 0x00) are ASCII (e.g. b"CN"), but
    # their meaning is unconfirmed (the app exposes no region/country for the
    # Power station, and it does not track the AC-voltage SKU, which is fixed by
    # the model number, e.g. DYM1000V2L = 120V). A prior `region` sensor parsed
    # this and was dropped as unverified.
    # AC output state: key 0x0d entry `0d 10 <len:2=0007> 14 10 03 00 02 01 <state>`
    # where state 0x01=on, 0x02=off.
    sig = bytes((POWER_SWITCH_KEY, 0x10, 0x07, 0x00))
    idx = payload.find(sig)
    if idx != -1 and idx + 11 <= len(payload):
        value = payload[idx + 4:idx + 11]
        if value[4:6] == b"\x02\x01" and value[6] in (0x01, 0x02):
            data["ac_enabled"] = value[6] == 0x01
    charge_limits = data.get(f"key_{CHARGE_LIMIT_KEY:02x}")
    if isinstance(charge_limits, str):
        value = bytes.fromhex(charge_limits)
        if len(value) == 24:
            values = [int.from_bytes(value[i:i + 4], "little") for i in range(0, 24, 4)]
            data["recharge_limit"] = values[2]
            data["discharge_limit"] = values[5]
    return data
