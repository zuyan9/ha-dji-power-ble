"""Capture-derived and synthetic tests for the HA-independent protocol codec."""

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


def expansion_battery(
    sequence: int = 1,
    *,
    percentage: int = 6250,
    capacity: int = 2048,
    cycles: int = 23,
    serial: bytes = b"SYNTHETIC-PACK01",
    temperature: int | None = None,
    temperature_status: int = 1,
    firmware: bytes = b"01.00.00.00",
) -> bytes:
    """Build a synthetic pack record, independent of physical-device captures."""
    assert len(serial) <= 16 and len(firmware) <= 16
    value = bytes((sequence,)) + percentage.to_bytes(2, "little")
    value += (999).to_bytes(4, "little")
    value += capacity.to_bytes(4, "little") + cycles.to_bytes(4, "little")
    value += serial.ljust(16, b"\x00")
    if temperature is not None:
        value += temperature.to_bytes(2, "little", signed=True)
        value += bytes((temperature_status,))
        value += firmware.ljust(16, b"\x00") + b"\x01"
    return record(0x100F, value)


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

# Synthetic app-schema eco-mode record, not a physical-station capture.
# The available flag is deliberately zero: the app's manual-control gate uses
# mode/grid_mode/chg_mode (3/3/2), not that flag. The discharge range is 10-800 W.
# The charge range is 100-1200 W, with an initial 500 W setpoint.
SYNTHETIC_ECO_MODE = (
    bytes.fromhex(
        "00030100"  # available, mode, peak_out_sw, valley_in_sw
        "640000001400000050000000"  # t_chg_up, t_chg_low, t_chg_v
        "0302"  # grid_mode, chg_mode
        "b004000064000000f4010000"  # m_chg_up, m_chg_low, m_chg_v
        "200300000a0000005d000000"  # m_dchg_up, m_dchg_low, m_dchg_v
    )
    + b"synthetic".ljust(37, b"\x00")  # src_dev_id
    + bytes.fromhex("0200fa00000001")  # tar_phase, ups_pwr_up, auto_sw
)

# Synthetic known-answer vectors: no device nonce, credentials, or capture data.
POWER_1000_AUTH_FRAME = bytes.fromhex(
    "551d04dfab020110865a6a5748f5445a84623c2347b1f9fcbde0f5bb2e"
)
POWER_1000_CIPHER_VECTORS = (
    (bytes.fromhex("0011223344"), "5748f5445a84623c2347b1f9fcbde0f5"),
    (bytes.fromhex("0100000000"), "dce7729163478147e55088f405885904"),
    (
        bytes(range(16)),
        "1d8457d6affce4d410217549ad683a7fa9d9eda83a8a41cec0450d2cfd1d2b3d",
    ),
    (
        bytes(range(38)),
        "1d8457d6affce4d410217549ad683a7f4f0dab801eba83c08939050c1de81f68"
        "8f3d0366a135382f3666b65324c1e0c0",
    ),
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


class Power1000TransportTests(unittest.TestCase):
    def test_cipher_matches_synthetic_known_answers(self) -> None:
        for plaintext, ciphertext_hex in POWER_1000_CIPHER_VECTORS:
            with self.subTest(plaintext_length=len(plaintext), status=plaintext[0]):
                ciphertext = bytes.fromhex(ciphertext_hex)
                self.assertEqual(
                    duml.encrypt_power_1000_payload(plaintext), ciphertext
                )
                self.assertEqual(
                    duml.decrypt_power_1000_payload(ciphertext), plaintext
                )

    def test_auth_frame_preserves_ciphertext_and_wire_metadata(self) -> None:
        packet = duml.DumlPacket.decode(POWER_1000_AUTH_FRAME)

        self.assertEqual(packet.encryption_type, duml.POWER_1000_ENCRYPTION_TYPE)
        self.assertTrue(packet.is_response)
        self.assertEqual(len(packet.payload), 16)
        self.assertEqual(
            duml.decrypt_power_1000_payload(packet.payload),
            bytes.fromhex("0011223344"),
        )
        self.assertEqual(packet.flags, 0x86)
        self.assertEqual(packet.sequence, 4097)
        self.assertEqual(packet.encode(), POWER_1000_AUTH_FRAME)

    def test_rejects_empty_or_partial_ciphertext(self) -> None:
        for payload in (b"", b"\x00", bytes(15), bytes(17)):
            with (
                self.subTest(length=len(payload)),
                self.assertRaisesRegex(duml.ProtocolError, "AES blocks"),
            ):
                duml.decrypt_power_1000_payload(payload)

    def test_rejects_corrupt_padding_before_its_final_byte(self) -> None:
        ciphertext = bytearray.fromhex(POWER_1000_CIPHER_VECTORS[2][1])
        # CBC makes this flip the penultimate padding byte, leaving the final
        # padding-length byte intact. Checking only that final byte would pass.
        ciphertext[14] ^= 1

        with self.assertRaisesRegex(duml.ProtocolError, "padding"):
            duml.decrypt_power_1000_payload(bytes(ciphertext))


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

    def test_timestamp_marker_does_not_change_keyed_header_length(self) -> None:
        # The timestamp has 0x0010 at the offset used by a GET header marker.
        payload = bytes.fromhex("00001000000010009e01000000000000151002003c00")
        for prefix in (b"", *(status.to_bytes(4, "little") for status in range(4))):
            with self.subTest(prefix=prefix.hex()):
                parsed = duml.parse_telemetry(prefix + payload)
                self.assertEqual(parsed["timezone_offset_min"], 60)

    def test_set_ack_validates_each_requested_key(self) -> None:
        ack = duml.build_keyed_set_payload(
            [(0x0D, b"\x00" * 4), (0x0E, b"\x00" * 4)],
            timestamp_ms=0x0000019E00100000,
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


class ExpansionBatteryTests(unittest.TestCase):
    def parse_packs(self, value: bytes) -> dict[str, object]:
        payload = duml.build_keyed_set_payload([(0x01, value)])
        return duml.parse_telemetry(payload)

    def test_legacy_record_decodes_per_pack_fields(self) -> None:
        value = expansion_battery()
        self.assertEqual(len(value), 4 + 31)

        parsed = self.parse_packs(value)

        self.assertEqual(
            parsed["expansion_batteries"],
            [
                {
                    "seq": 1,
                    "serial_number": "SYNTHETIC-PACK01",
                    "battery_percent": 62.5,
                    "cycle_count": 23,
                    "rated_capacity_wh": 2048,
                    "temperature": None,
                    "firmware": None,
                }
            ],
        )
        self.assertEqual(parsed["key_01"], value.hex())

    def test_extended_record_decodes_temperature_and_firmware_with_tail(self) -> None:
        original = expansion_battery(temperature=-1234)
        self.assertEqual(len(original), 4 + 51)
        for tail in (b"", b"\xaa\xbb\xcc"):
            with self.subTest(tail=tail):
                value = record(0x100F, original[4:] + tail)
                pack = self.parse_packs(value)["expansion_batteries"][0]

                self.assertEqual(pack["temperature"], -12.34)
                self.assertEqual(pack["firmware"], "01.00.00.00")
                self.assertEqual(pack["battery_percent"], 62.5)

    def test_optional_fields_require_their_complete_prefix(self) -> None:
        original = expansion_battery(temperature=2510)[4:]
        for length in (31, 32, 33, 34, 35, 49, 50):
            with self.subTest(length=length):
                pack = self.parse_packs(record(0x100F, original[:length]))[
                    "expansion_batteries"
                ][0]

                self.assertEqual(pack["battery_percent"], 62.5)
                self.assertEqual(pack["temperature"], 25.1 if length >= 34 else None)
                self.assertEqual(
                    pack["firmware"], "01.00.00.00" if length >= 50 else None
                )

    def test_ten_packs_preserve_order_and_distinct_values(self) -> None:
        value = b"".join(
            expansion_battery(
                sequence,
                percentage=sequence * 1000,
                cycles=sequence * 17,
                serial=f"SYNTHETIC-PACK{sequence:02}".encode(),
            )
            for sequence in range(1, 11)
        )

        packs = self.parse_packs(value)["expansion_batteries"]

        self.assertEqual([pack["seq"] for pack in packs], list(range(1, 11)))
        self.assertEqual(
            [pack["battery_percent"] for pack in packs], list(range(10, 101, 10))
        )
        self.assertEqual(
            [pack["cycle_count"] for pack in packs], list(range(17, 171, 17))
        )

    def test_charge_percentage_bounds_do_not_invalidate_other_metrics(self) -> None:
        for encoded, expected in ((0, 0), (10000, 100), (10001, None), (65535, None)):
            with self.subTest(encoded=encoded):
                pack = self.parse_packs(expansion_battery(percentage=encoded))[
                    "expansion_batteries"
                ][0]

                self.assertEqual(pack["battery_percent"], expected)
                self.assertEqual(pack["cycle_count"], 23)

    def test_capacity_and_cycle_count_use_unsigned_32_bit_fields(self) -> None:
        pack = self.parse_packs(
            expansion_battery(capacity=2**32 - 1, cycles=2**32 - 1)
        )["expansion_batteries"][0]

        self.assertEqual(pack["rated_capacity_wh"], 2**32 - 1)
        self.assertEqual(pack["cycle_count"], 2**32 - 1)

    def test_temperature_status_does_not_hide_other_metrics(self) -> None:
        for status in (0, 1, 2, 3, 4, 255):
            with self.subTest(status=status):
                pack = self.parse_packs(
                    expansion_battery(temperature=3000, temperature_status=status)
                )["expansion_batteries"][0]

                self.assertEqual(
                    pack["temperature"], 30 if status in (1, 2, 3) else None
                )
                self.assertEqual(pack["firmware"], "01.00.00.00")
                self.assertEqual(pack["battery_percent"], 62.5)

    def test_temperature_uses_signed_16_bit_hundredths(self) -> None:
        for encoded in (-32768, -1, 0, 32767):
            with self.subTest(encoded=encoded):
                pack = self.parse_packs(expansion_battery(temperature=encoded))[
                    "expansion_batteries"
                ][0]
                self.assertEqual(pack["temperature"], encoded / 100)

    def test_empty_list_clears_and_missing_key_preserves_snapshot(self) -> None:
        current = self.parse_packs(expansion_battery())
        partial = duml.parse_telemetry(CAPTURED_KEYED_CONFIG)

        self.assertNotIn("expansion_batteries", partial)
        current.update(partial)
        self.assertEqual(len(current["expansion_batteries"]), 1)
        current.update(self.parse_packs(b""))
        self.assertEqual(current["expansion_batteries"], [])

    def test_inactive_slots_are_ignored_before_identity_checks(self) -> None:
        inactive = expansion_battery(capacity=0, serial=b"")

        self.assertEqual(self.parse_packs(inactive)["expansion_batteries"], [])
        packs = self.parse_packs(inactive + expansion_battery())[
            "expansion_batteries"
        ]
        self.assertEqual(len(packs), 1)

    def test_malformed_list_invalidates_packs_and_keeps_other_keyed_data(self) -> None:
        complete = expansion_battery()
        malformed = (
            complete[:-1],  # A child length exceeds the list boundary.
            complete + b"\x01",  # Incomplete child TLV header.
            complete + record(0x100F, b"\x00" * 30),  # Short second pack.
            record(0x100F, b""),
            record(0x1010, b"unknown list format"),
        )
        for index, value in enumerate(malformed):
            with self.subTest(case=index):
                payload = duml.build_keyed_set_payload([(0x01, value), (0x02, b"\x01")])
                parsed = duml.parse_telemetry(payload)

                self.assertIsNone(parsed["expansion_batteries"])
                self.assertTrue(parsed["cloud_connected"])
                self.assertEqual(parsed["key_01"], value.hex())

    def test_invalid_serial_invalidates_snapshot(self) -> None:
        for serial in (b"", b" " * 16, b"bad\xffserial", b"bad\nserial"):
            with self.subTest(serial=serial):
                self.assertIsNone(
                    self.parse_packs(expansion_battery(serial=serial))[
                        "expansion_batteries"
                    ]
                )

    def test_serial_accepts_null_padded_or_full_width_ascii(self) -> None:
        for serial in (b"SYNTHETIC", b"SYNTHETIC-PACK01"):
            with self.subTest(serial=serial):
                pack = self.parse_packs(expansion_battery(serial=serial))[
                    "expansion_batteries"
                ][0]
                self.assertEqual(pack["serial_number"], serial.decode())

    def test_duplicate_serial_or_sequence_invalidates_snapshot(self) -> None:
        for duplicate in (
            expansion_battery(2),
            expansion_battery(serial=b"SYNTHETIC-PACK02"),
        ):
            with self.subTest(duplicate=duplicate):
                self.assertIsNone(
                    self.parse_packs(expansion_battery() + duplicate)[
                        "expansion_batteries"
                    ]
                )

    def test_unknown_child_tag_is_skipped_alongside_known_pack_records(self) -> None:
        value = record(0x1010, b"future metadata") + expansion_battery()

        self.assertEqual(len(self.parse_packs(value)["expansion_batteries"]), 1)

    def test_invalid_firmware_only_clears_optional_metadata(self) -> None:
        for firmware in (b"", b"bad\xffversion", b"bad\nversion", b" " * 16):
            with self.subTest(firmware=firmware):
                pack = self.parse_packs(
                    expansion_battery(temperature=2000, firmware=firmware)
                )["expansion_batteries"][0]

                self.assertIsNone(pack["firmware"])
                self.assertEqual(pack["temperature"], 20)
                self.assertEqual(pack["battery_percent"], 62.5)


class DischargePowerTests(unittest.TestCase):
    def test_app_schema_decodes_93_and_422_watts_with_device_bounds(self) -> None:
        self.assertEqual(len(SYNTHETIC_ECO_MODE), 86)
        for encoded, watts in (("5d000000", 93), ("a6010000", 422)):
            with self.subTest(watts=watts):
                value = bytearray(SYNTHETIC_ECO_MODE)
                value[38:42] = bytes.fromhex(encoded)
                payload = duml.build_keyed_set_payload([(0x18, bytes(value))])

                parsed = duml.parse_telemetry(payload)

                self.assertTrue(parsed["discharge_power_available"])
                self.assertEqual(parsed["discharge_power_min_w"], 10)
                self.assertEqual(parsed["discharge_power_max_w"], 800)
                self.assertEqual(parsed["discharge_power_w"], watts)
                self.assertEqual(parsed["key_18"], value.hex())

    def test_available_flag_does_not_gate_manual_control(self) -> None:
        for available in (0, 1):
            with self.subTest(available=available):
                value = bytearray(SYNTHETIC_ECO_MODE)
                value[0] = available
                self.assertEqual(
                    duml._parse_manual_discharge_power(bytes(value)), (10, 800, 93)
                )
                payload = duml.build_discharge_power_set_payload(bytes(value), 422)
                self.assertEqual(duml.parse_keyed_values(payload)[0x18][0], available)

    def test_set_changes_only_watts_preserving_complete_record_and_tail(self) -> None:
        current = SYNTHETIC_ECO_MODE + bytes.fromhex("aabbccddeeff")
        for source in (current, current.hex()):
            with self.subTest(source_type=type(source).__name__):
                payload = duml.build_discharge_power_set_payload(
                    source, 422, timestamp_ms=1000
                )
                entries = duml.parse_keyed_values(payload)

                self.assertEqual(set(entries), {0x18})
                self.assertEqual(
                    entries[0x18],
                    current[:38] + bytes.fromhex("a6010000") + current[42:],
                )
                self.assertEqual(payload[:16], duml.build_keyed_header(1000))

    def test_set_accepts_both_device_reported_bounds(self) -> None:
        for watts in (10, 800):
            with self.subTest(watts=watts):
                payload = duml.build_discharge_power_set_payload(
                    SYNTHETIC_ECO_MODE, watts
                )
                self.assertEqual(
                    duml.parse_telemetry(payload)["discharge_power_w"], watts
                )

    def test_discharge_fields_use_full_unsigned_32_bit_values(self) -> None:
        value = bytearray(SYNTHETIC_ECO_MODE)
        value[30:42] = bytes.fromhex("ffffffff0000010001000100")
        self.assertEqual(
            duml._parse_manual_discharge_power(bytes(value)),
            (65536, 4294967295, 65537),
        )
        payload = duml.build_discharge_power_set_payload(bytes(value), 4294967295)
        self.assertEqual(
            duml.parse_keyed_values(payload)[0x18][38:42], bytes.fromhex("ffffffff")
        )

    def test_set_rejects_non_integer_or_out_of_range_watts(self) -> None:
        for watts in (93.0, 93.5, True, False, "93", None, -1, 9, 801, 2**32):
            with (
                self.subTest(watts=watts),
                self.assertRaises(duml.ProtocolError),
            ):
                duml.build_discharge_power_set_payload(SYNTHETIC_ECO_MODE, watts)

    def test_set_rejects_invalid_hex(self) -> None:
        for current in ("zz", "0"):
            with (
                self.subTest(current=current),
                self.assertRaisesRegex(duml.ProtocolError, "valid hex"),
            ):
                duml.build_discharge_power_set_payload(current, 422)

    def test_invalid_or_inactive_state_clears_telemetry_and_blocks_writes(self) -> None:
        invalid = [SYNTHETIC_ECO_MODE[:length] for length in (0, 17, 42, 85)]
        for offset, disabled in ((1, 0), (16, 2), (17, 1)):
            value = bytearray(SYNTHETIC_ECO_MODE)
            value[offset] = disabled
            invalid.append(bytes(value))
        for limits in (
            "090000000a0000000a000000",  # Maximum below minimum.
            "200300000a00000009000000",  # Current below minimum.
            "200300000a00000021030000",  # Current above maximum.
        ):
            value = bytearray(SYNTHETIC_ECO_MODE)
            value[30:42] = bytes.fromhex(limits)
            invalid.append(bytes(value))

        for index, value in enumerate(invalid):
            with self.subTest(case=index):
                payload = duml.build_keyed_set_payload([(0x18, value)])
                parsed = duml.parse_telemetry(payload)

                self.assertFalse(parsed["discharge_power_available"])
                self.assertIsNone(parsed["discharge_power_min_w"])
                self.assertIsNone(parsed["discharge_power_max_w"])
                self.assertIsNone(parsed["discharge_power_w"])
                self.assertEqual(parsed["key_18"], value.hex())
                with self.assertRaises(duml.ProtocolError):
                    duml.build_discharge_power_set_payload(value, 422)

    def test_partial_snapshot_without_eco_mode_keeps_existing_state(self) -> None:
        payload = duml.build_keyed_set_payload([(0x18, SYNTHETIC_ECO_MODE)])
        current = duml.parse_telemetry(payload)
        partial = duml.parse_telemetry(CAPTURED_KEYED_CONFIG)

        self.assertFalse(any(key.startswith("discharge_power_") for key in partial))
        current.update(partial)
        self.assertEqual(current["discharge_power_w"], 93)
        self.assertTrue(current["discharge_power_available"])


class ChargePowerTests(unittest.TestCase):
    def test_app_schema_decodes_charge_watts_and_device_bounds(self) -> None:
        for available in (0, 1):
            with self.subTest(available=available):
                value = bytearray(SYNTHETIC_ECO_MODE)
                value[0] = available
                payload = duml.build_keyed_set_payload([(0x18, bytes(value))])

                parsed = duml.parse_telemetry(payload)

                self.assertTrue(parsed["charge_power_available"])
                self.assertEqual(parsed["charge_power_min_w"], 100)
                self.assertEqual(parsed["charge_power_max_w"], 1200)
                self.assertEqual(parsed["charge_power_w"], 500)
                self.assertEqual(parsed["discharge_power_w"], 93)

    def test_set_preserves_discharge_and_complete_record_except_charge_watts(self):
        current = SYNTHETIC_ECO_MODE + bytes.fromhex("aabbccddeeff")
        for source in (current, current.hex()):
            with self.subTest(source_type=type(source).__name__):
                payload = duml.build_charge_power_set_payload(
                    source, 750, timestamp_ms=1000
                )
                entries = duml.parse_keyed_values(payload)

                self.assertEqual(set(entries), {0x18})
                self.assertEqual(
                    entries[0x18],
                    current[:26] + bytes.fromhex("ee020000") + current[30:],
                )
                self.assertEqual(payload[:16], duml.build_keyed_header(1000))

    def test_charge_set_accepts_reported_bounds_zero_and_full_uint32(self) -> None:
        for minimum, maximum, current in (
            (100, 1200, 500),
            (0, 0, 0),
            (0, 4294967295, 65537),
            (65536, 4294967295, 65537),
        ):
            value = bytearray(SYNTHETIC_ECO_MODE)
            value[18:30] = b"".join(
                watts.to_bytes(4, "little") for watts in (maximum, minimum, current)
            )
            self.assertEqual(
                duml._parse_manual_charge_power(value), (minimum, maximum, current)
            )
            for watts in (minimum, maximum):
                with self.subTest(minimum=minimum, maximum=maximum, watts=watts):
                    payload = duml.build_charge_power_set_payload(bytes(value), watts)
                    self.assertEqual(
                        duml.parse_telemetry(payload)["charge_power_w"], watts
                    )

    def test_charge_set_rejects_non_integer_and_out_of_range_watts(self) -> None:
        for watts in (500.0, 500.5, True, False, "500", None, -1, 99, 1201, 2**32):
            with self.subTest(watts=watts), self.assertRaises(duml.ProtocolError):
                duml.build_charge_power_set_payload(SYNTHETIC_ECO_MODE, watts)

    def test_charge_set_rejects_invalid_hex(self) -> None:
        for current in ("zz", "0"):
            with (
                self.subTest(current=current),
                self.assertRaisesRegex(duml.ProtocolError, "valid hex"),
            ):
                duml.build_charge_power_set_payload(current, 750)

    def test_invalid_or_inactive_eco_mode_clears_charge_and_blocks_write(self) -> None:
        invalid = [SYNTHETIC_ECO_MODE[:length] for length in (0, 17, 30, 85)]
        for offset, disabled in ((1, 0), (16, 2), (17, 0), (17, 1), (17, 3)):
            value = bytearray(SYNTHETIC_ECO_MODE)
            value[offset] = disabled
            invalid.append(bytes(value))

        for index, value in enumerate(invalid):
            with self.subTest(case=index):
                payload = duml.build_keyed_set_payload([(0x18, value)])
                parsed = duml.parse_telemetry(payload)

                self.assertFalse(parsed["charge_power_available"])
                self.assertIsNone(parsed["charge_power_min_w"])
                self.assertIsNone(parsed["charge_power_max_w"])
                self.assertIsNone(parsed["charge_power_w"])
                with self.assertRaises(duml.ProtocolError):
                    duml.build_charge_power_set_payload(value, 750)

    def test_invalid_charge_bounds_do_not_disable_discharge(self) -> None:
        for maximum, minimum, current in (
            (99, 100, 100),
            (1200, 100, 99),
            (1200, 100, 1201),
        ):
            with self.subTest(maximum=maximum, minimum=minimum, current=current):
                value = bytearray(SYNTHETIC_ECO_MODE)
                value[18:30] = b"".join(
                    watts.to_bytes(4, "little") for watts in (maximum, minimum, current)
                )
                payload = duml.build_keyed_set_payload([(0x18, bytes(value))])
                parsed = duml.parse_telemetry(payload)

                self.assertFalse(parsed["charge_power_available"])
                self.assertIsNone(parsed["charge_power_min_w"])
                self.assertIsNone(parsed["charge_power_max_w"])
                self.assertIsNone(parsed["charge_power_w"])
                self.assertTrue(parsed["discharge_power_available"])
                self.assertEqual(parsed["discharge_power_w"], 93)
                with self.assertRaises(duml.ProtocolError):
                    duml.build_charge_power_set_payload(bytes(value), 750)
                changed = duml.build_discharge_power_set_payload(bytes(value), 422)
                self.assertEqual(
                    duml.parse_keyed_values(changed)[0x18][:38], value[:38]
                )

    def test_invalid_discharge_bounds_do_not_disable_charge(self) -> None:
        value = bytearray(SYNTHETIC_ECO_MODE)
        value[38:42] = bytes(4)  # Below the discharge minimum of 10 W.
        payload = duml.build_keyed_set_payload([(0x18, bytes(value))])
        parsed = duml.parse_telemetry(payload)

        self.assertTrue(parsed["charge_power_available"])
        self.assertEqual(parsed["charge_power_w"], 500)
        self.assertFalse(parsed["discharge_power_available"])
        changed = duml.build_charge_power_set_payload(bytes(value), 750)
        self.assertEqual(duml.parse_keyed_values(changed)[0x18][30:], value[30:])

    def test_partial_snapshot_without_eco_mode_preserves_charge_state(self) -> None:
        payload = duml.build_keyed_set_payload([(0x18, SYNTHETIC_ECO_MODE)])
        current = duml.parse_telemetry(payload)
        partial = duml.parse_telemetry(CAPTURED_KEYED_CONFIG)

        self.assertFalse(any(key.startswith("charge_power_") for key in partial))
        current.update(partial)
        self.assertEqual(current["charge_power_w"], 500)
        self.assertTrue(current["charge_power_available"])


class PowerAdjustmentTests(unittest.TestCase):
    def test_automatic_readback_clears_previous_manual_watts(self) -> None:
        payload = duml.build_keyed_set_payload([(0x18, SYNTHETIC_ECO_MODE)])
        current = duml.parse_telemetry(payload)
        self.assertEqual(current["power_adjustment"], "Manual")
        self.assertEqual(current["charge_power_w"], 500)
        self.assertEqual(current["discharge_power_w"], 93)

        value = bytearray(SYNTHETIC_ECO_MODE)
        value[17] = 1
        payload = duml.build_keyed_set_payload([(0x18, bytes(value))])
        current.update(duml.parse_telemetry(payload))

        self.assertEqual(current["power_adjustment"], "Automatic")
        self.assertFalse(current["charge_power_available"])
        self.assertIsNone(current["charge_power_w"])
        self.assertIsNone(current["charge_power_min_w"])
        self.assertIsNone(current["charge_power_max_w"])
        self.assertFalse(current["discharge_power_available"])
        self.assertIsNone(current["discharge_power_w"])
        self.assertIsNone(current["discharge_power_min_w"])
        self.assertIsNone(current["discharge_power_max_w"])

    def test_selector_does_not_require_valid_manual_watts(self) -> None:
        value = bytearray(SYNTHETIC_ECO_MODE)
        value[38:42] = bytes(4)  # Below the reported manual minimum of 10 W.
        for encoded, mode in ((1, "Automatic"), (2, "Manual")):
            with self.subTest(mode=mode):
                value[17] = encoded
                payload = duml.build_keyed_set_payload([(0x18, bytes(value))])
                parsed = duml.parse_telemetry(payload)

                self.assertEqual(parsed["power_adjustment"], mode)
                self.assertFalse(parsed["discharge_power_available"])
                self.assertIsNone(parsed["discharge_power_w"])
                for target in ("Automatic", "Manual"):
                    updated = duml.build_power_adjustment_set_payload(
                        bytes(value), target
                    )
                    self.assertEqual(
                        duml.parse_telemetry(updated)["power_adjustment"], target
                    )

    def test_set_changes_only_adjustment_preserving_watts_and_tail(self) -> None:
        original = SYNTHETIC_ECO_MODE + bytes.fromhex("aabbccddeeff")
        for source_is_hex in (False, True):
            current = original
            for mode, encoded in (("Automatic", 1), ("Manual", 2)):
                with self.subTest(source_is_hex=source_is_hex, mode=mode):
                    source = current.hex() if source_is_hex else current
                    payload = duml.build_power_adjustment_set_payload(
                        source, mode, timestamp_ms=1000
                    )
                    entries = duml.parse_keyed_values(payload)

                    self.assertEqual(set(entries), {0x18})
                    self.assertEqual(
                        entries[0x18], current[:17] + bytes((encoded,)) + current[18:]
                    )
                    self.assertEqual(payload[:16], duml.build_keyed_header(1000))
                    current = entries[0x18]

    def test_automatic_requires_meter_but_manual_does_not(self) -> None:
        value = bytearray(SYNTHETIC_ECO_MODE)
        value[42:79] = bytes(37)
        value[17] = 1
        with self.assertRaisesRegex(duml.ProtocolError, "smart meter"):
            duml.build_power_adjustment_set_payload(bytes(value), "Automatic")

        payload = duml.build_power_adjustment_set_payload(bytes(value), "Manual")
        self.assertEqual(duml.parse_telemetry(payload)["power_adjustment"], "Manual")
        self.assertEqual(duml.parse_keyed_values(payload)[0x18][42:79], bytes(37))

    def test_meter_check_uses_valid_utf8_within_its_fixed_field(self) -> None:
        value = bytearray(SYNTHETIC_ECO_MODE + b"nonempty-extension")
        for meter_id in (bytes(37), b"\xff" + bytes(36)):
            with self.subTest(meter_id=meter_id):
                value[42:79] = meter_id
                with self.assertRaisesRegex(duml.ProtocolError, "smart meter"):
                    duml.build_power_adjustment_set_payload(bytes(value), "Automatic")
        value[42:79] = "synthetic-é".encode().ljust(37, b"\x00")
        payload = duml.build_power_adjustment_set_payload(bytes(value), "Automatic")
        self.assertEqual(duml.parse_keyed_values(payload)[0x18][42:], value[42:])

    def test_set_rejects_unknown_modes_and_malformed_hex(self) -> None:
        for mode in ("manual", "automatic", "Scheduled", "", None, True, 2):
            with self.subTest(mode=mode), self.assertRaises(duml.ProtocolError):
                duml.build_power_adjustment_set_payload(SYNTHETIC_ECO_MODE, mode)
        for current in ("zz", "0"):
            with (
                self.subTest(current=current),
                self.assertRaisesRegex(duml.ProtocolError, "valid hex"),
            ):
                duml.build_power_adjustment_set_payload(current, "Manual")

    def test_invalid_record_clears_selector_and_blocks_both_modes(self) -> None:
        invalid = [SYNTHETIC_ECO_MODE[:length] for length in (0, 17, 42, 85)]
        for offset, unsupported in ((1, 0), (16, 2), (17, 0), (17, 3)):
            value = bytearray(SYNTHETIC_ECO_MODE)
            value[offset] = unsupported
            invalid.append(bytes(value))

        for index, value in enumerate(invalid):
            with self.subTest(case=index):
                payload = duml.build_keyed_set_payload([(0x18, value)])
                parsed = duml.parse_telemetry(payload)

                self.assertIsNone(parsed["power_adjustment"])
                self.assertFalse(parsed["discharge_power_available"])
                self.assertEqual(parsed["key_18"], value.hex())
                for target in ("Automatic", "Manual"):
                    with self.assertRaises(duml.ProtocolError):
                        duml.build_power_adjustment_set_payload(value, target)

    def test_partial_snapshot_without_eco_mode_keeps_adjustment(self) -> None:
        payload = duml.build_keyed_set_payload([(0x18, SYNTHETIC_ECO_MODE)])
        current = duml.parse_telemetry(payload)
        partial = duml.parse_telemetry(CAPTURED_KEYED_CONFIG)

        self.assertNotIn("power_adjustment", partial)
        current.update(partial)
        self.assertEqual(current["power_adjustment"], "Manual")


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
