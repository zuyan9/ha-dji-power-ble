"""Synthetic schedule validation and protocol tests, independent of hardware."""

from __future__ import annotations

import copy
import unittest

from tests.test_duml import SYNTHETIC_ECO_MODE, duml, record


def period(
    start: str = "00:30",
    end: str = "05:30",
    *,
    kind: str = "off_peak",
    days: list[str] | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {"type": kind, "start": start, "end": end}
    if days is not None:
        value["days"] = days
    return value


class TimePeriodValidationTests(unittest.TestCase):
    def test_everyday_default_and_canonical_order_without_mutation(self) -> None:
        periods = [
            period(),
            period("17:00", "21:00", kind="peak", days=["fri", "mon", "wed"]),
        ]
        original = copy.deepcopy(periods)
        result = duml.normalize_time_periods(periods)

        self.assertEqual(periods, original)
        self.assertEqual(result[0]["days"], ["mon", "wed", "fri"])
        self.assertEqual(result[1]["days"], list(duml.TIME_PERIOD_DAYS))
        self.assertEqual(result, duml.normalize_time_periods(list(reversed(periods))))
        self.assertEqual(result, duml.normalize_time_periods(result))

    def test_empty_schedule_is_valid(self) -> None:
        self.assertEqual(duml.normalize_time_periods([]), [])

    def test_invalid_structure_or_fields_are_rejected(self) -> None:
        invalid = (
            None,
            {},
            "periods",
            [None],
            [{}],
            [period() | {"type": "valley"}],
            [period() | {"type": []}],
            [period() | {"unexpected": 1}],
            [period() | {"days": "mon"}],
            [period() | {"days": []}],
            [period() | {"days": ["Monday"]}],
            [period() | {"days": ["mon", "mon"]}],
            [period() | {"days": [1]}],
            [period() | {"days": [[]]}],
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(duml.ProtocolError):
                duml.normalize_time_periods(value)

    def test_invalid_clock_times_are_rejected(self) -> None:
        for value in (
            None,
            1230,
            "0:30",
            "00:3",
            "00:30:00",
            "00.30",
            "24:00",
            "12:60",
            "-1:00",
            " 0:30",
            "００:３０",
        ):
            for field in ("start", "end"):
                with (
                    self.subTest(field=field, value=value),
                    self.assertRaises(duml.ProtocolError),
                ):
                    duml.normalize_time_periods([period() | {field: value}])

    def test_equal_start_and_end_are_rejected(self) -> None:
        with self.assertRaisesRegex(duml.ProtocolError, "must differ"):
            duml.normalize_time_periods([period("00:00", "00:00")])

    def test_eight_periods_of_each_type_are_allowed(self) -> None:
        periods = [
            period(
                f"{hour:02d}:00",
                f"{hour:02d}:30",
                kind="peak" if hour < 8 else "off_peak",
            )
            for hour in range(16)
        ]
        self.assertEqual(len(duml.normalize_time_periods(periods)), 16)
        for kind in ("peak", "off_peak"):
            with self.subTest(kind=kind), self.assertRaisesRegex(
                duml.ProtocolError, "eight"
            ):
                duml.normalize_time_periods(
                    periods + [period("20:00", "21:00", kind=kind)]
                )

    def test_overlaps_across_types_and_within_a_type_are_rejected(self) -> None:
        for kind in ("peak", "off_peak"):
            for start, end in (("01:00", "02:00"), ("05:00", "06:00")):
                with (
                    self.subTest(kind=kind, start=start),
                    self.assertRaisesRegex(duml.ProtocolError, "overlap"),
                ):
                    duml.normalize_time_periods(
                        [period(), period(start, end, kind=kind, days=["wed"])]
                    )

    def test_adjacent_periods_and_disjoint_weekdays_are_allowed(self) -> None:
        periods = [
            period(days=["mon"]),
            period("05:30", "06:00", kind="peak", days=["mon"]),
            period(days=["tue"]),
        ]
        self.assertEqual(len(duml.normalize_time_periods(periods)), 3)

    def test_overnight_overlap_including_week_boundary_is_rejected(self) -> None:
        for start_day, next_day in (("mon", "tue"), ("sun", "mon")):
            with (
                self.subTest(start_day=start_day),
                self.assertRaisesRegex(duml.ProtocolError, "overlap"),
            ):
                duml.normalize_time_periods(
                    [
                        period("23:00", "02:00", days=[start_day]),
                        period("01:00", "03:00", kind="peak", days=[next_day]),
                    ]
                )

    def test_overnight_adjacency_across_week_boundary_is_allowed(self) -> None:
        periods = [
            period("23:00", "02:00", days=["sun"]),
            period("02:00", "03:00", kind="peak", days=["mon"]),
        ]
        self.assertEqual(len(duml.normalize_time_periods(periods)), 2)


class TimePeriodModeTests(unittest.TestCase):
    def test_editing_accepts_all_known_modes_without_manual_tou_gate(self) -> None:
        for mode in range(4):
            with self.subTest(mode=mode):
                eco = bytearray(SYNTHETIC_ECO_MODE)
                eco[1] = mode
                eco[16:18] = b"\x00\x00"
                duml.validate_time_periods_mode(bytes(eco))
                duml.validate_time_periods_mode(eco.hex())

    def test_clearing_requires_scheduling_disabled(self) -> None:
        for mode in range(4):
            with self.subTest(mode=mode):
                eco = bytearray(SYNTHETIC_ECO_MODE)
                eco[1] = mode
                if mode in (0, 1):
                    duml.validate_time_periods_mode(bytes(eco), clearing=True)
                else:
                    with self.assertRaisesRegex(duml.ProtocolError, "before clearing"):
                        duml.validate_time_periods_mode(bytes(eco), clearing=True)

    def test_missing_truncated_or_unknown_mode_is_rejected(self) -> None:
        unknown = bytearray(SYNTHETIC_ECO_MODE)
        unknown[1] = 255
        for value in ("not hex", b"", SYNTHETIC_ECO_MODE[:85], bytes(unknown)):
            with self.subTest(value=value), self.assertRaises(duml.ProtocolError):
                duml.validate_time_periods_mode(value)


class TimePeriodCodecTests(unittest.TestCase):
    # Synthetic rows with independently specified expected field bytes.
    DAILY_OFF_PEAK = bytes.fromhex("02017f000000001e051e")
    WEEKLY_PEAK = bytes.fromhex("0102450000001100151e")

    def test_set_matches_known_tariff_rows_and_rules_envelope(self) -> None:
        payload = duml.build_time_periods_set_payload(
            [
                period(),
                period("17:00", "21:30", kind="peak", days=["sun", "mon", "wed"]),
            ],
            timestamp_ms=7,
        )
        self.assertEqual(
            payload.hex(),
            "00001000070000000000000000000000"
            "16101c00"
            "16000a000102450000001100151e"
            "16000a0002017f000000001e051e"
            "0e100c000a0031383030656666666666",
        )
        self.assertEqual(
            set(duml.parse_keyed_values(payload)),
            {duml.TIME_PERIODS_KEY, duml.RULES_KEY},
        )

    def test_parse_known_rows_and_round_trip_canonical_schedule(self) -> None:
        value = record(0x0016, self.DAILY_OFF_PEAK) + record(0x0016, self.WEEKLY_PEAK)
        parsed = duml.parse_time_periods(value)
        self.assertEqual(
            parsed,
            [
                period("17:00", "21:30", kind="peak", days=["mon", "wed", "sun"]),
                period(days=list(duml.TIME_PERIOD_DAYS)),
            ],
        )
        encoded = duml.build_time_periods_set_payload(parsed, timestamp_ms=0)
        self.assertEqual(duml.parse_telemetry(encoded)["time_periods"], parsed)

    def test_all_weekdays_encode_with_everyday_repetition_and_mask(self) -> None:
        explicit = duml.build_time_periods_set_payload(
            [period(days=list(reversed(duml.TIME_PERIOD_DAYS)))], timestamp_ms=0
        )
        default = duml.build_time_periods_set_payload([period()], timestamp_ms=0)
        self.assertEqual(explicit, default)
        self.assertEqual(
            duml.parse_keyed_values(explicit)[duml.TIME_PERIODS_KEY],
            record(0x0016, self.DAILY_OFF_PEAK),
        )

    def test_daily_read_ignores_unused_weekday_mask(self) -> None:
        row = bytearray(self.DAILY_OFF_PEAK)
        row[2:6] = b"\x00" * 4
        self.assertEqual(
            duml.parse_time_periods(record(0x0016, bytes(row))),
            [period(days=list(duml.TIME_PERIOD_DAYS))],
        )

    def test_weekly_all_days_read_normalizes_to_everyday(self) -> None:
        row = bytearray(self.DAILY_OFF_PEAK)
        row[1] = 2
        parsed = duml.parse_time_periods(record(0x0016, bytes(row)))
        encoded = duml.build_time_periods_set_payload(parsed, timestamp_ms=0)
        self.assertEqual(
            duml.parse_keyed_values(encoded)[duml.TIME_PERIODS_KEY],
            record(0x0016, self.DAILY_OFF_PEAK),
        )

    def test_inbound_nested_tag_is_not_used_to_select_the_row_schema(self) -> None:
        for tag in (0x0016, 0x1016, 0xABCD):
            with self.subTest(tag=tag):
                self.assertEqual(
                    duml.parse_time_periods(record(tag, self.DAILY_OFF_PEAK)),
                    [period(days=list(duml.TIME_PERIOD_DAYS))],
                )

    def test_empty_list_encodes_and_decodes_explicitly(self) -> None:
        self.assertEqual(duml.parse_time_periods(b""), [])
        payload = duml.build_time_periods_set_payload([], timestamp_ms=0)
        self.assertEqual(
            payload,
            duml.build_keyed_header(0)
            + bytes.fromhex("161000000e100c000a0031383030656666666666"),
        )
        self.assertEqual(duml.parse_keyed_values(payload)[duml.TIME_PERIODS_KEY], b"")
        self.assertEqual(duml.parse_telemetry(payload)["time_periods"], [])

    def test_unknown_type_invalid_clock_and_bad_weekday_masks(self) -> None:
        for index, value in (
            (0, 0),
            (0, 3),
            (1, 0),
            (1, 3),
            (2, 0),
            (2, 128),
            (3, 1),
            (6, 24),
            (7, 60),
            (8, 24),
            (9, 60),
        ):
            row = bytearray(self.WEEKLY_PEAK)
            row[index] = value
            with (
                self.subTest(index=index, value=value),
                self.assertRaises(duml.ProtocolError),
            ):
                duml.parse_time_periods(record(0x0016, bytes(row)))

    def test_truncation_trailing_bytes_and_extended_rows_are_rejected(self) -> None:
        valid = record(0x0016, self.DAILY_OFF_PEAK)
        for value in (
            b"\x16",
            b"\x16\x00\x0a",
            valid[:-1],
            valid + b"\x00",
            record(0x0016, self.DAILY_OFF_PEAK[:-1]),
            record(0x0016, self.DAILY_OFF_PEAK + b"\x00"),
        ):
            with self.subTest(value=value), self.assertRaises(duml.ProtocolError):
                duml.parse_time_periods(value)

    def test_overlapping_readback_is_invalid(self) -> None:
        value = record(0x0016, self.DAILY_OFF_PEAK) * 2
        with self.assertRaisesRegex(duml.ProtocolError, "overlap"):
            duml.parse_time_periods(value)

    def test_missing_key_preserves_and_malformed_key_clears_schedule(self) -> None:
        state = duml.parse_telemetry(
            duml.build_time_periods_set_payload([period()], timestamp_ms=0)
        )
        expected = state["time_periods"]
        partial = duml.build_keyed_set_payload([(0x15, b"\x78\x00")], timestamp_ms=0)
        self.assertNotIn("time_periods", duml.parse_telemetry(partial))
        state.update(duml.parse_telemetry(partial))
        self.assertEqual(state["time_periods"], expected)

        malformed = duml.build_keyed_set_payload(
            [(duml.TIME_PERIODS_KEY, b"\x16"), (0x15, b"\x78\x00")], timestamp_ms=0
        )
        state.update(duml.parse_telemetry(malformed))
        self.assertIsNone(state["time_periods"])
        self.assertEqual(state["timezone_offset_min"], 120)
        state.update(duml.parse_telemetry(duml.build_time_periods_set_payload([])))
        self.assertEqual(state["time_periods"], [])
