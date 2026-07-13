"""Capture-derived tests for the HA-independent DJI protocol codec."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

MODULE_PATH = (
    Path(__file__).parents[1] / "custom_components" / "dji_power_ble" / "duml.py"
)
SPEC = importlib.util.spec_from_file_location("dji_power_test_duml", MODULE_PATH)
assert SPEC and SPEC.loader
duml = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = duml
SPEC.loader.exec_module(duml)


def record(tag: int, value: bytes) -> bytes:
    return tag.to_bytes(2, "little") + len(value).to_bytes(2, "little") + value


def interface(
    sequence: int,
    interface_type: int,
    output_w: int,
    input_w: int,
    *,
    input_voltage_mv: int | None = None,
) -> bytes:
    value = bytes((sequence, interface_type, 0))
    value += output_w.to_bytes(2, "little")
    value += input_w.to_bytes(2, "little")
    value += b"\x00"
    if input_voltage_mv is not None:
        voltage = b"\x01" + input_voltage_mv.to_bytes(2, "little") + b"\x00" * 6
        value += record(0x3035, record(0x3036, voltage))
    return record(0x3034, value)


def group(group_type: int, *interfaces: bytes) -> bytes:
    wrapper = record(0x3033, b"".join(interfaces))
    return record(0x3032, bytes((group_type,)) + wrapper)


CAPTURED_KEYED_CONFIG = bytes.fromhex(
    "02000000000010005bf2f2779e0100000000000000103500434e00000f010130312e3030"
    "2e313130300000000000000130332e30332e303030300000000000000004000000000084"
    "03000000000210010001051018006400000046000000640000000f000000000000000000"
    "00000610040000025000071016000002002003000000000000c800000000000000050104"
    "09100400000200000a1000000b1000000c100d000000000000000000c0a80000000d1007"
    "0014100300020102151002005cfe"
)

CAPTURED_REPORT = bytes.fromhex(
    "0100100059f2f2779e010000000000004030090007003035303031330010302600"
    "0000343931353037622d616435382d346164372d626539322d3264323138306430"
    "333862640020300c005c123417025c120000ce0901303025000100000031301d00"
    "3230190003333014003430100001040001000000003830000035300000"
)


class DumlFrameTests(unittest.TestCase):
    def test_encode_matches_captured_auth_request(self) -> None:
        packet = duml.DumlPacket(0x02, 0xAB, 0x2711, 0x20, 0x5A, 0x6A, b"\x00")
        self.assertEqual(packet.encode().hex(), "550e046602ab1127205a6a00ce38")

    def test_stream_reassembles_fragments_and_recovers_after_noise(self) -> None:
        first = duml.DumlPacket(2, 0xAB, 1, 0x20, 0x5A, 0x6A, b"\x00").encode()
        second = duml.DumlPacket(0xAB, 2, 2, 0, 0x5A, 0x61, b"payload").encode()
        stream = duml.DumlStream()

        self.assertEqual(stream.feed(b"noise" + first[:5]), [])
        packets = stream.feed(first[5:] + b"bad" + second)

        self.assertEqual([packet.sequence for packet in packets], [1, 2])

    def test_pair_key_requires_ascii_hex(self) -> None:
        self.assertEqual(duml.normalize_pair_key("AA" * 16), b"aa" * 16)
        with self.assertRaises(duml.ProtocolError):
            duml.normalize_pair_key("z" * 32)


class KeyedConfigTests(unittest.TestCase):
    def test_captured_config_decodes_known_fields(self) -> None:
        parsed = duml.parse_telemetry(CAPTURED_KEYED_CONFIG)

        self.assertNotIn("country_code", parsed)
        self.assertEqual(parsed["firmware"], "01.00.1100")
        self.assertEqual(parsed["firmware_secondary"], "03.03.0000")
        self.assertTrue(parsed["cloud_connected"])
        self.assertEqual(parsed["recharge_limit"], 100)
        self.assertEqual(parsed["discharge_limit"], 0)
        self.assertEqual(parsed["energy_reserve"], 80)
        self.assertEqual(parsed["display_timeout_s"], 0)
        self.assertEqual(parsed["timezone_offset_min"], -420)
        self.assertFalse(parsed["ac_enabled"])

    def test_header_uses_real_u64_millisecond_timestamp(self) -> None:
        self.assertEqual(
            duml.build_keyed_header(1000).hex(),
            "00001000e80300000000000000000000",
        )

    def test_set_ack_validates_each_requested_key(self) -> None:
        ack = duml.build_keyed_set_payload(
            [(0x0D, b"\x00" * 4), (0x0E, b"\x00" * 4)], timestamp_ms=1
        )
        duml.parse_set_ack(ack, (0x0D, 0x0E))
        with self.assertRaisesRegex(duml.ProtocolError, "omitted key"):
            duml.parse_set_ack(ack, (0x05,))

    def test_charge_limit_builder_preserves_non_user_fields(self) -> None:
        current = "6400000046000000640000000f0000000000000000000000"
        payload = duml.build_charge_limits_set_payload(
            current, 15, 70, timestamp_ms=1000
        )
        values = duml.parse_keyed_values(payload)
        self.assertEqual(
            values[0x05].hex(),
            "6400000046000000460000000f000000000000000f000000",
        )


class ReportTests(unittest.TestCase):
    def test_live_capture_decodes_battery_and_usb_c(self) -> None:
        parsed = duml.parse_report(CAPTURED_REPORT)

        self.assertEqual(parsed["battery_percent"], 47)
        self.assertEqual(parsed["runtime_min"], 5940)
        self.assertEqual(parsed["temperature"], 25.1)
        self.assertEqual(parsed["usb_c_output_w"], 1)
        self.assertEqual(parsed["usb_c_1_output_w"], 1)
        self.assertEqual(parsed["interfaces"][0]["type_name"], "usb_c")

    def test_nested_groups_preserve_ports_and_input_voltage(self) -> None:
        interfaces = record(
            0x3031,
            group(1, interface(1, 1, 0, 23))
            + group(2, interface(1, 2, 19, 0))
            + group(
                3,
                interface(1, 3, 2, 0),
                interface(2, 3, 3, 0),
                interface(1, 4, 5, 0),
            )
            + group(4, interface(1, 5, 7, 11, input_voltage_mv=51234)),
        )
        payload = record(0x3030, bytes.fromhex("31002f00") + interfaces)

        parsed = duml.parse_report(payload)

        self.assertEqual(parsed["ac_output_w"], 19)
        self.assertEqual(parsed["dc_output_w"], 10)
        self.assertEqual(parsed["usb_a_1_output_w"], 2)
        self.assertEqual(parsed["usb_a_2_output_w"], 3)
        self.assertEqual(parsed["sdc_input_w"], 11)
        self.assertEqual(parsed["interfaces"][-1]["input_voltage_v"], 51.234)

    def test_input_power_drives_charging_when_time_type_is_not_zero(self) -> None:
        battery = bytes.fromhex("c819990b02c8190000bc0c00")
        payload = record(0x3020, battery) + record(0x3030, bytes.fromhex("00007c01"))

        parsed = duml.parse_report(payload)

        self.assertEqual(parsed["battery_time_type"], 2)
        self.assertTrue(parsed["charging"])


class AdvertisementTests(unittest.TestCase):
    def test_decodes_all_known_model_codes_and_bound_bit(self) -> None:
        info = duml.parse_manufacturer_data(bytes.fromhex("aa08971110aabbccddeeff"))
        self.assertEqual(info.model, "DJI Power 1000 V2")
        self.assertTrue(info.bound)
        self.assertEqual(info.mac, "AA:BB:CC:DD:EE:FF")

        for code in duml.MODEL_NAMES:
            self.assertEqual(
                duml.parse_manufacturer_data(bytes((code, 0x01))).model,
                duml.MODEL_NAMES[code],
            )


if __name__ == "__main__":
    unittest.main()
