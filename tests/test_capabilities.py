"""Offline station capability predicates shared by discovery and writes."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_duml import SYNTHETIC_ECO_MODE, duml

PACKAGE = "_dji_power_capability_tests"
COMPONENT = Path(__file__).parents[1] / "custom_components" / "dji_power_ble"
spec = importlib.util.spec_from_file_location(
    f"{PACKAGE}.features", COMPONENT / "features.py"
)
assert spec and spec.loader
features = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {
    PACKAGE: types.ModuleType(PACKAGE),
    f"{PACKAGE}.duml": duml,
}):
    spec.loader.exec_module(features)


class PortCapabilityTests(unittest.TestCase):
    def test_ac_and_car_rows_do_not_require_station_rule_11(self):
        rows = [{"type": kind, "seq": 0, "sw": 2} for kind in range(2, 8)]
        for rules in (None, [], [21], [11], [11, 21]):
            with self.subTest(rules=rules):
                expected = range(2, 8) if rules and 11 in rules else (2, 7)
                self.assertEqual(
                    set(features.eligible_port_switches({
                        "power_switches": rows, "station_rules": rules,
                    })),
                    {(kind, 0) for kind in expected},
                )

    def test_derived_cached_flag_cannot_override_missing_rules(self):
        data = {
            "power_switches": [{"type": 5, "seq": 1, "sw": 1}],
            "port_switches_offered": True,
        }
        self.assertEqual(features.eligible_port_switches(data), {})
        data["station_rules"] = [11]
        data["port_switches_offered"] = False
        self.assertEqual(set(features.eligible_port_switches(data)), {(5, 1)})

    def test_unknown_invalid_and_ambiguous_rows_are_never_offered(self):
        valid = {"type": 5, "seq": 1, "sw": 1}
        for invalid in (
            None, {}, {"type": 99, "seq": 1, "sw": 1},
            {"type": 5, "seq": True, "sw": 1},
            {"type": 5, "seq": 256, "sw": 1},
            {"type": 5, "seq": 1, "sw": True},
            {"type": 5, "seq": 1, "sw": 0},
        ):
            with self.subTest(row=invalid):
                self.assertEqual(features.eligible_port_switches({
                    "station_rules": [11], "power_switches": [invalid],
                }), {})
        for other in (valid, valid | {"sw": 0}):
            self.assertEqual(features.eligible_port_switches({
                "station_rules": [11], "power_switches": [valid, other],
            }), {})

    def test_missing_or_malformed_rules_never_offer_gated_ports(self):
        for rules in (None, "11", [True], [11, "21"], [-1, 11]):
            with self.subTest(rules=rules):
                self.assertFalse(features.station_rule_enabled({
                    "station_rules": rules,
                }, 11))


class FeatureCapabilityTests(unittest.TestCase):
    def test_known_models_can_discover_all_features_but_generic_model_cannot(self):
        for feature in features.ModelFeature:
            for model in duml.MODEL_NAMES.values():
                self.assertTrue(features.supports_feature(model, feature))
            self.assertFalse(features.supports_feature("DJI Power", feature))

    def test_schedule_requires_reported_list_available_eco_and_rule_5(self):
        data = {"time_periods": [], "eco_available": True, "station_rules": [5]}
        feature = features.ModelFeature.TARIFF_SCHEDULE
        self.assertTrue(features.feature_available(data, feature))
        for change in (
            {"time_periods": None}, {"eco_available": False}, {"station_rules": []},
        ):
            with self.subTest(change=change):
                self.assertFalse(features.feature_available(data | change, feature))

    def test_tou_requires_active_record_and_both_rules(self):
        data = {
            "eco_available": True, "station_rules": [5, 6],
            "power_adjustment": "Manual",
        }
        feature = features.ModelFeature.TOU_POWER_CONTROL
        self.assertTrue(features.feature_available(data, feature))
        self.assertTrue(features.feature_available(data | {
            "power_adjustment": "Automatic",
        }, feature))
        for change in (
            {"eco_available": False}, {"station_rules": [5]},
            {"station_rules": [6]}, {"power_adjustment": None},
        ):
            with self.subTest(change=change):
                self.assertFalse(features.feature_available(data | change, feature))

    def test_eco_availability_clears_on_short_record_and_survives_unrelated_data(self):
        for value, expected in (
            (b"\x01" + SYNTHETIC_ECO_MODE[1:], True),
            (b"\x00" + SYNTHETIC_ECO_MODE[1:], False),
            (b"", None), (b"\x01", None),
        ):
            with self.subTest(length=len(value), expected=expected):
                self.assertIs(duml.parse_telemetry(
                    duml.build_keyed_set_payload([(0x18, value)])
                )["eco_available"], expected)
        self.assertNotIn("eco_available", duml.parse_telemetry(
            duml.build_keyed_set_payload([(0x02, b"\x00")])
        ))
