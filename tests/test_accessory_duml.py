"""Synthetic accessory codec checks; these do not validate physical hardware."""

from __future__ import annotations

import unittest

from tests.test_duml import duml, record


def car_row(
    *,
    interface_type: int = 5,
    seq: int = 1,
    accessory_type: int = 4,
    sw: int = 1,
    mode: int = 2,
    tail: bytes = b"",
    **values: int,
) -> bytes:
    """Supply independent synthetic bounds including the issue's shown values."""
    row = bytes((interface_type, seq, accessory_type, sw, mode))
    defaults = {
        "p_from_car": (600, 100, 400),
        "p_to_car": (600, 50, 150),
        "v_from_car": (1400, 1150, 1150),
        "v_to_car": (1450, 1200, 1300),
        "v_auto": (1400, 1200, 1250),
    }
    for field, bounds in defaults.items():
        for part, default in zip(("up", "low", "v"), bounds, strict=True):
            row += values.get(f"{field}_{part}", default).to_bytes(4, "little")
    return row + tail


def car_value(**kwargs: int | bytes) -> bytes:
    return record(0x1012, car_row(**kwargs))


def edit_car(current_value: str | bytes | None = None, **kwargs: object) -> bytes:
    if current_value is None:
        current_value = car_value()
    return duml.build_car_charger_set_payload(
        current_value, 5, 1, 4, timestamp_ms=0, **kwargs
    )


class AccessoryParsingTests(unittest.TestCase):
    def test_car_schema_uses_wire_order_and_raw_units(self) -> None:
        values = duml.parse_car_chargers(car_value())[0]
        self.assertEqual(
            {
                name: values[name]
                for name in ("interface_type", "seq", "type", "sw", "mode")
            },
            {"interface_type": 5, "seq": 1, "type": 4, "sw": 1, "mode": 2},
        )
        self.assertEqual(values["p_from_car_up"], 600)
        self.assertEqual(values["p_from_car_low"], 100)
        self.assertEqual(values["p_from_car_v"], 400)
        self.assertEqual(values["p_to_car_v"], 150)
        self.assertEqual(values["v_from_car_v"], 1150)
        self.assertEqual(values["v_to_car_v"], 1300)
        self.assertEqual(values["v_auto_v"], 1250)
        self.assertEqual(len(values), 20)

    def test_empty_lists_are_explicit_snapshots(self) -> None:
        self.assertEqual(duml.parse_car_chargers(b""), [])
        self.assertEqual(duml.parse_power_switches(b""), [])
        parsed = duml.parse_telemetry(record(0x100A, b"") + record(0x100D, b""))
        self.assertEqual(parsed["car_chargers"], [])
        self.assertEqual(parsed["power_switches"], [])
        self.assertIsNone(parsed["ac_enabled"])

    def test_missing_keys_preserve_previous_snapshots(self) -> None:
        parsed = duml.parse_telemetry(record(0x1002, b"\x01"))
        self.assertNotIn("car_chargers", parsed)
        self.assertNotIn("power_switches", parsed)
        self.assertNotIn("ac_enabled", parsed)

    def test_extended_rows_and_unknown_enums_are_read_without_guessing(self) -> None:
        car = car_value(accessory_type=99, mode=99, sw=99, tail=b"future")
        self.assertEqual(duml.parse_car_chargers(car)[0]["type"], 99)
        self.assertEqual(
            duml.parse_power_switches(record(0x1234, bytes((99, 3, 7)) + b"future")),
            [{"type": 99, "seq": 3, "sw": 7}],
        )

    def test_each_truncated_prefix_and_incomplete_tlv_are_rejected(self) -> None:
        for parser, row in (
            (duml.parse_car_chargers, car_row()),
            (duml.parse_power_switches, b"\x05\x01\x01"),
        ):
            for length in range(len(row)):
                with (
                    self.subTest(parser=parser.__name__, length=length),
                    self.assertRaises(duml.ProtocolError),
                ):
                    parser(record(0x1012, row[:length]))
            for malformed in (
                b"\x01",
                b"\x01\x00",
                b"\x01\x00\x01",
                record(7, row)[:-1],
            ):
                with (
                    self.subTest(malformed=malformed.hex()),
                    self.assertRaises(duml.ProtocolError),
                ):
                    parser(malformed)

    def test_duplicate_port_identities_are_rejected(self) -> None:
        for parser, value in (
            (duml.parse_car_chargers, car_value() + car_value(accessory_type=3)),
            (
                duml.parse_power_switches,
                record(0x1014, b"\x05\x01\x01") + record(0x000D, b"\x05\x01\x02"),
            ),
        ):
            with (
                self.subTest(parser=parser.__name__),
                self.assertRaises(duml.ProtocolError),
            ):
                parser(value)

    def test_malformed_list_invalidates_only_its_fields(self) -> None:
        payload = record(0x100A, b"\x00") + record(0x100D, b"\x00")
        parsed = duml.parse_telemetry(payload + record(0x1002, b"\x01"))
        self.assertIsNone(parsed["car_chargers"])
        self.assertIsNone(parsed["power_switches"])
        self.assertIsNone(parsed["ac_enabled"])
        self.assertTrue(parsed["cloud_connected"])

    def test_ac_can_appear_after_an_sdc_row(self) -> None:
        value = record(0x1014, b"\x05\x02\x02") + record(0x000D, b"\x02\x01\x01")
        parsed = duml.parse_telemetry(record(0x100D, value))
        self.assertTrue(parsed["ac_enabled"])
        self.assertEqual(len(parsed["power_switches"]), 2)

    def test_present_switch_snapshot_clears_missing_unknown_or_malformed_ac(
        self,
    ) -> None:
        for value in (
            record(0x1014, b"\x05\x01\x01"),
            record(0x1014, b"\x02\x02\x01"),
            record(0x1014, b"\x02\x01\x03"),
            record(0x1014, b"\x02\x01\x01") + b"\x00",
        ):
            with self.subTest(value=value.hex()):
                parsed = duml.parse_telemetry(record(0x100D, value))
                self.assertIsNone(parsed["ac_enabled"])


def rules_value(text: bytes) -> bytes:
    return len(text).to_bytes(2, "little") + text


class StationRulesTests(unittest.TestCase):
    def test_rule_count_and_little_endian_mask_are_decoded(self) -> None:
        for text, expected in (
            # The Power 1000 V2 reports its rules with a trailing NUL.
            (b"11000f7001\x00", (17, 0x01700F)),
            (b"1e00efffff3f", (30, 0x3FFFFFEF)),
            (b"0200fe", (2, 0xFE)),
            (b"0100", (1, 0)),
            # DJI Home reads text shorter than a count as no rules.
            (b"", (0, 0)),
            (b"ab\x00", (0, 0)),
        ):
            with self.subTest(text=text):
                self.assertEqual(duml.parse_station_rules(rules_value(text)), expected)

    def test_rule_zero_selects_the_auto_threshold_layout(self) -> None:
        for text, expected in (
            (b"11000f7001\x00", True),
            (b"1e00efffff3f", True),
            (b"0200fe", False),
            (b"0000ff", False),
            (b"0100", False),
            (b"", False),
        ):
            with self.subTest(text=text):
                parsed = duml.parse_telemetry(record(0x100E, rules_value(text)))
                self.assertIs(parsed["car_auto_threshold"], expected)

    def test_malformed_rules_are_unknown_without_hiding_other_state(self) -> None:
        for value in (
            b"",
            b"\x01",
            b"\x05\x00" + b"0200",
            b"\x04\x00" + b"0200fe",
            b"\x05\x00" + b"0200f",
            b"\x06\x00" + b"02zzfe",
            b"\x06\x00" + "0200é".encode(),
        ):
            with self.subTest(value=value):
                with self.assertRaises(duml.ProtocolError):
                    duml.parse_station_rules(value)
                parsed = duml.parse_telemetry(
                    record(0x100E, value) + record(0x1002, b"\x01")
                )
                self.assertIsNone(parsed["car_auto_threshold"])
                self.assertTrue(parsed["cloud_connected"])

    def test_missing_rules_preserve_the_previous_layout(self) -> None:
        self.assertNotIn(
            "car_auto_threshold", duml.parse_telemetry(record(0x1002, b"\x01"))
        )

    def test_numbers_follow_dji_home_mode_layouts(self) -> None:
        for mode, rule, expected in (
            (1, True, ("p_from_car", "p_to_car", "v_auto")),
            (1, False, ("p_from_car", "p_to_car", "v_from_car", "v_to_car")),
            (1, None, ("p_from_car", "p_to_car")),
            (2, True, ("p_from_car", "v_from_car")),
            (2, None, ("p_from_car", "v_from_car")),
            (3, False, ("p_to_car", "v_to_car")),
            (0, True, ()),
            (4, True, ()),
            (True, True, ()),
            (None, True, ()),
            (1, 1, ("p_from_car", "p_to_car")),
        ):
            with self.subTest(mode=mode, rule=rule):
                self.assertEqual(duml.car_charger_numbers(mode, rule), expected)


class CarChargerBuilderTests(unittest.TestCase):
    def test_reported_zero_sequence_is_preserved(self) -> None:
        payload = duml.build_car_charger_set_payload(
            car_value(seq=0), 5, 0, 4, enabled=False, timestamp_ms=0
        )
        row = duml.parse_car_chargers(duml.parse_keyed_values(payload)[0x0A])[0]
        self.assertEqual((row["seq"], row["sw"]), (0, 2))

    def test_each_edit_preserves_other_rows_values_and_extended_tails(self) -> None:
        before = car_row(tail=b"target-tail")
        unknown = car_row(interface_type=99, seq=4, accessory_type=99, tail=b"unknown")
        value = record(0x1012, unknown) + record(0x1012, before)
        edits = (
            ({"enabled": False}, 3, b"\x02"),
            ({"enabled": True}, 3, b"\x01"),
            ({"mode": 1}, 4, b"\x01"),
            ({"mode": 3}, 4, b"\x03"),
            ({"recharge_power_w": 500}, 13, (500).to_bytes(4, "little")),
            ({"minimum_voltage_v": 12.34}, 37, (1234).to_bytes(4, "little")),
        )
        for setting, offset, replacement in edits:
            with self.subTest(setting=setting):
                result = duml.parse_keyed_values(edit_car(value.hex(), **setting))
                self.assertEqual(result[0x0E], bytes.fromhex("0c00") + b"1e00efffff3f")
                records = duml.parse_tlvs(result[0x0A], strict=True)
                self.assertEqual([row.tag for row in records], [0x000A, 0x000A])
                self.assertEqual(records[0].value, unknown)
                expected = bytearray(before)
                expected[offset : offset + len(replacement)] = replacement
                self.assertEqual(records[1].value, expected)
                self.assertEqual(
                    value, record(0x1012, unknown) + record(0x1012, before)
                )

    def test_charge_and_auto_numbers_edit_only_their_setting(self) -> None:
        other = car_row(interface_type=6, seq=2, tail=b"other")
        for mode, setting, offset, raw in (
            (3, {"charge_power_w": 300}, 25, 300),
            (1, {"charge_power_w": 600}, 25, 600),
            (1, {"recharge_power_w": 100}, 13, 100),
            (3, {"charge_voltage_v": 13.45}, 49, 1345),
            (1, {"charge_voltage_v": 14.5, "auto_threshold": False}, 49, 1450),
            (1, {"minimum_voltage_v": 12.5, "auto_threshold": False}, 37, 1250),
            (1, {"auto_voltage_v": 12.6, "auto_threshold": True}, 61, 1260),
        ):
            with self.subTest(mode=mode, setting=setting):
                before = car_row(mode=mode, tail=b"target-tail")
                value = record(0x1012, other) + record(0x1012, before)
                result = duml.parse_keyed_values(edit_car(value, **setting))
                self.assertEqual(result[0x0E], bytes.fromhex("0c00") + b"1e00efffff3f")
                records = duml.parse_tlvs(result[0x0A], strict=True)
                self.assertEqual(records[0].value, other)
                expected = bytearray(before)
                expected[offset : offset + 4] = raw.to_bytes(4, "little")
                self.assertEqual(records[1].value, expected)

    def test_both_known_accessories_and_sdc_interfaces_are_supported(self) -> None:
        for interface_type in (5, 6):
            for accessory_type in (3, 4):
                value = car_value(
                    interface_type=interface_type, accessory_type=accessory_type
                )
                result = duml.build_car_charger_set_payload(
                    value,
                    interface_type,
                    1,
                    accessory_type,
                    enabled=False,
                    timestamp_ms=0,
                )
                self.assertEqual(
                    duml.parse_car_chargers(duml.parse_keyed_values(result)[0x0A])[0][
                        "sw"
                    ],
                    2,
                )

    def test_exactly_one_edit_is_required(self) -> None:
        for settings in (
            {},
            {"enabled": True, "mode": 2},
            {"mode": 2, "recharge_power_w": 400},
            {"recharge_power_w": 400, "minimum_voltage_v": 12.5},
            {"charge_power_w": 300, "auto_voltage_v": 12.5},
            {"recharge_power_w": None},
            {"unknown_w": 400},
            {"enabled": True, "unknown_w": None},
        ):
            with self.subTest(settings=settings), self.assertRaises(duml.ProtocolError):
                edit_car(**settings)
        # Omitted numbers can be passed as None alongside the single change.
        edit_car(enabled=False, charge_power_w=None)

    def test_target_must_be_reported_known_and_unambiguous(self) -> None:
        for value in (
            b"",
            car_value(interface_type=6),
            car_value(seq=2),
            car_value(accessory_type=3),
            car_value(sw=0),
            car_value(mode=0),
            car_value() + car_value(),
            car_value()[:-1],
            "not hex",
        ):
            with self.subTest(value=value), self.assertRaises(duml.ProtocolError):
                edit_car(value, enabled=True)

    def test_numbers_require_an_enabled_mode_that_offers_them(self) -> None:
        powers = ({"recharge_power_w": 500}, {"charge_power_w": 300})
        voltages = (
            {"minimum_voltage_v": 12.5},
            {"charge_voltage_v": 13},
            {"auto_voltage_v": 12.5},
        )
        for setting in powers + voltages:
            for rule in (True, False, None):
                with (
                    self.subTest(setting=setting, rule=rule),
                    self.assertRaisesRegex(duml.ProtocolError, "enable the car"),
                ):
                    edit_car(car_value(sw=2), auto_threshold=rule, **setting)
        for mode, rule, setting in (
            (2, True, {"charge_power_w": 300}),
            (2, False, {"charge_voltage_v": 13}),
            (2, True, {"auto_voltage_v": 12.5}),
            (3, None, {"recharge_power_w": 500}),
            (3, False, {"minimum_voltage_v": 12.5}),
            (3, True, {"auto_voltage_v": 12.5}),
            (1, True, {"minimum_voltage_v": 12.5}),
            (1, True, {"charge_voltage_v": 13}),
            (1, False, {"auto_voltage_v": 12.5}),
            (1, None, {"minimum_voltage_v": 12.5}),
            (1, None, {"charge_voltage_v": 13}),
            (1, None, {"auto_voltage_v": 12.5}),
        ):
            with (
                self.subTest(mode=mode, rule=rule, setting=setting),
                self.assertRaisesRegex(duml.ProtocolError, "mode does not use"),
            ):
                edit_car(car_value(mode=mode), auto_threshold=rule, **setting)

    def test_invalid_argument_types_states_and_precision_are_rejected(self) -> None:
        for settings in (
            {"enabled": 1},
            {"mode": True},
            {"mode": 0},
            {"mode": 4},
            {"recharge_power_w": True},
            {"recharge_power_w": 400.0},
            {"recharge_power_w": "400"},
            {"minimum_voltage_v": True},
            {"minimum_voltage_v": "12.5"},
            {"minimum_voltage_v": 12.345},
            {"minimum_voltage_v": float("inf")},
            {"minimum_voltage_v": float("nan")},
            {"charge_power_w": True},
            {"charge_power_w": 300.0},
            {"charge_voltage_v": "13"},
            {"charge_voltage_v": 13.001},
            {"auto_voltage_v": float("-inf")},
        ):
            with self.subTest(settings=settings), self.assertRaises(duml.ProtocolError):
                edit_car(**settings)

    def test_reported_bounds_and_current_setpoints_are_validated(self) -> None:
        for settings in (
            {"recharge_power_w": 99},
            {"recharge_power_w": 601},
            {"recharge_power_w": -1},
            {"minimum_voltage_v": 11.49},
            {"minimum_voltage_v": 14.01},
        ):
            with self.subTest(settings=settings), self.assertRaises(duml.ProtocolError):
                edit_car(**settings)
        for fields in (
            {"p_from_car_low": 700},
            {"p_from_car_up": 0},
            {"p_from_car_v": 99},
            {"p_from_car_v": 601},
        ):
            with self.subTest(fields=fields), self.assertRaises(duml.ProtocolError):
                edit_car(car_value(**fields), recharge_power_w=400)
        # A malformed unrelated control must not disable this control.
        edit_car(car_value(v_from_car_up=0), recharge_power_w=400)

    def test_charge_and_auto_bounds_are_validated_per_field(self) -> None:
        for mode, settings in (
            (3, {"charge_power_w": 49}),
            (3, {"charge_power_w": 601}),
            (3, {"charge_voltage_v": 11.99}),
            (3, {"charge_voltage_v": 14.51}),
            (1, {"auto_voltage_v": 11.99, "auto_threshold": True}),
            (1, {"auto_voltage_v": 14.01, "auto_threshold": True}),
        ):
            with self.subTest(settings=settings), self.assertRaises(duml.ProtocolError):
                edit_car(car_value(mode=mode), **settings)
        for fields in ({"p_to_car_up": 0}, {"p_to_car_v": 49}, {"p_to_car_low": 700}):
            with self.subTest(fields=fields), self.assertRaises(duml.ProtocolError):
                edit_car(car_value(mode=3, **fields), charge_power_w=300)
        # A malformed recharge control must not disable the charge controls.
        edit_car(car_value(mode=3, p_from_car_up=0), charge_power_w=300)

    def test_reported_boundaries_are_inclusive(self) -> None:
        for mode, settings in (
            (2, {"recharge_power_w": 100}),
            (2, {"recharge_power_w": 600}),
            (2, {"minimum_voltage_v": 11.5}),
            (2, {"minimum_voltage_v": 14}),
            (3, {"charge_power_w": 50}),
            (3, {"charge_voltage_v": 14.5}),
            (1, {"auto_voltage_v": 12, "auto_threshold": True}),
            (1, {"auto_voltage_v": 14, "auto_threshold": True}),
        ):
            with self.subTest(settings=settings):
                edit_car(car_value(mode=mode), **settings)


class SdcSwitchBuilderTests(unittest.TestCase):
    def test_reported_zero_sequence_is_preserved(self) -> None:
        payload = duml.build_sdc_switch_set_payload(
            record(0x1014, b"\x05\x00\x01"), 5, 0, False, timestamp_ms=0
        )
        row = duml.parse_power_switches(duml.parse_keyed_values(payload)[0x0D])[0]
        self.assertEqual((row["seq"], row["sw"]), (0, 2))

    def test_ac_edit_preserves_shared_sdc_switch_rows(self) -> None:
        rows = [b"\x05\x02\x02tail", b"\x02\x01\x02ac-tail", b"\x06\x01\x01"]
        value = b"".join(record(0x1014, row) for row in rows)
        payload = duml.build_ac_set_payload(True, current_value=value, timestamp_ms=0)
        result = duml.parse_keyed_values(payload)
        self.assertEqual(
            [row.value for row in duml.parse_tlvs(result[0x0D], strict=True)],
            [rows[0], b"\x02\x01\x01ac-tail", rows[2]],
        )
        self.assertEqual(result[0x0E], bytes.fromhex("0c00") + b"1e00efffff3f")

    def test_ac_edit_requires_valid_reported_ac_when_snapshot_supplied(self) -> None:
        for value in (
            b"",
            record(0x1014, b"\x05\x01\x01"),
            record(0x1014, b"\x02\x01\x00"),
            b"\x00",
        ):
            with self.subTest(value=value), self.assertRaises(duml.ProtocolError):
                duml.build_ac_set_payload(True, current_value=value, timestamp_ms=0)

    def test_reported_port_edit_preserves_ac_unknown_rows_and_tails(self) -> None:
        rows = [b"\x02\x01\x01", b"\x05\x02\x02tail", b"\x63\x01\x07future"]
        value = b"".join(record(0x1014, row) for row in rows)
        payload = duml.build_sdc_switch_set_payload(value, 5, 2, True, timestamp_ms=0)
        result = duml.parse_keyed_values(payload)
        self.assertEqual(result[0x0E], bytes.fromhex("0c00") + b"1e00efffff3f")
        records = duml.parse_tlvs(result[0x0D], strict=True)
        self.assertEqual([row.tag for row in records], [0x000D] * 3)
        self.assertEqual(
            [row.value for row in records], [rows[0], b"\x05\x02\x01tail", rows[2]]
        )

    def test_sdc_lite_can_be_disabled(self) -> None:
        payload = duml.build_sdc_switch_set_payload(
            record(0x1014, b"\x06\x01\x01").hex(), 6, 1, False, timestamp_ms=0
        )
        self.assertEqual(
            duml.parse_power_switches(duml.parse_keyed_values(payload)[0x0D])[0]["sw"],
            2,
        )

    def test_missing_unknown_duplicate_and_malformed_switches_fail(self) -> None:
        for value in (
            b"",
            record(0x1014, b"\x02\x01\x01"),
            record(0x1014, b"\x05\x02\x01"),
            record(0x1014, b"\x05\x01\x00"),
            record(0x1014, b"\x05\x01"),
            record(0x1014, b"\x05\x01\x01") * 2,
            "not hex",
        ):
            with self.subTest(value=value), self.assertRaises(duml.ProtocolError):
                duml.build_sdc_switch_set_payload(value, 5, 1, True)

    def test_sdc_identity_and_enabled_types_are_validated(self) -> None:
        value = record(0x1014, b"\x05\x01\x01")
        for interface_type, seq, enabled in (
            (2, 1, True),
            (True, 1, True),
            (5, -1, True),
            (5, 256, True),
            (5, True, True),
            (5, 1, 1),
        ):
            with (
                self.subTest(interface_type=interface_type, seq=seq, enabled=enabled),
                self.assertRaises(duml.ProtocolError),
            ):
                duml.build_sdc_switch_set_payload(value, interface_type, seq, enabled)


class UsbSwitchBuilderTests(unittest.TestCase):
    ROWS = (
        b"\x02\x01\x01",
        b"\x03\x01\x01",
        b"\x03\x02\x02",
        b"\x04\x01\x01",
        b"\x04\x02\x01tail",
    )

    def value(self) -> bytes:
        return b"".join(record(0x1014, row) for row in self.ROWS)

    def test_each_usb_port_edit_changes_only_its_switch_byte(self) -> None:
        for index, (interface_type, seq) in enumerate(
            ((3, 1), (3, 2), (4, 1), (4, 2)), start=1
        ):
            for enabled in (False, True):
                with self.subTest(port=(interface_type, seq), enabled=enabled):
                    payload = duml.build_usb_switch_set_payload(
                        self.value(), interface_type, seq, enabled, timestamp_ms=0
                    )
                    result = duml.parse_keyed_values(payload)
                    self.assertEqual(
                        result[0x0E], bytes.fromhex("0c00") + b"1e00efffff3f"
                    )
                    records = duml.parse_tlvs(result[0x0D], strict=True)
                    self.assertEqual([row.tag for row in records], [0x000D] * 5)
                    expected = list(self.ROWS)
                    row = bytearray(expected[index])
                    row[2] = 1 if enabled else 2
                    expected[index] = bytes(row)
                    self.assertEqual([row.value for row in records], expected)

    def test_missing_unknown_duplicate_and_malformed_usb_switches_fail(self) -> None:
        for value in (
            b"",
            record(0x1014, b"\x02\x01\x01"),
            record(0x1014, b"\x03\x02\x01"),
            record(0x1014, b"\x03\x01\x00"),
            record(0x1014, b"\x03\x01"),
            record(0x1014, b"\x03\x01\x01") * 2,
            "not hex",
        ):
            with self.subTest(value=value), self.assertRaises(duml.ProtocolError):
                duml.build_usb_switch_set_payload(value, 3, 1, True)

    def test_usb_identity_and_enabled_types_are_validated(self) -> None:
        value = self.value() + record(0x1014, b"\x05\x01\x01")
        for interface_type, seq, enabled in (
            (2, 1, True),
            (5, 1, True),
            (True, 1, True),
            (3, -1, True),
            (3, 256, True),
            (3, True, True),
            (3, 1, 1),
        ):
            with (
                self.subTest(interface_type=interface_type, seq=seq, enabled=enabled),
                self.assertRaises(duml.ProtocolError),
            ):
                duml.build_usb_switch_set_payload(value, interface_type, seq, enabled)

    def test_sdc_builder_cannot_address_usb_rows(self) -> None:
        with self.assertRaises(duml.ProtocolError):
            duml.build_sdc_switch_set_payload(self.value(), 3, 1, False)
