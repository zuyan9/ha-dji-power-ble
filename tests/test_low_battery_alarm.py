"""Low-battery alarm record (0x21) captured from a Power 1000 Mini."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

MODULE_PATH = (
    Path(__file__).parents[1] / "custom_components" / "dji_power_ble" / "duml.py"
)
SPEC = importlib.util.spec_from_file_location("dji_power_test_alarm", MODULE_PATH)
assert SPEC and SPEC.loader
duml = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = duml
SPEC.loader.exec_module(duml)


def written(payload: bytes) -> str:
    return duml.parse_keyed_values(payload)[duml.LOW_BATTERY_ALARM_KEY].hex()


class LowBatteryAlarmTests(unittest.TestCase):
    def test_parse(self) -> None:
        self.assertIs(duml.parse_low_battery_alarm("010105"), True)
        self.assertIs(duml.parse_low_battery_alarm("010205"), False)
        for bad in (None, "", "zz", "0103", "010305", "01010500"):
            self.assertIsNone(duml.parse_low_battery_alarm(bad), bad)

    def test_build(self) -> None:
        self.assertEqual(written(duml.build_low_battery_alarm_set_payload("010105", False)), "010205")
        self.assertEqual(written(duml.build_low_battery_alarm_set_payload("010205", True)), "010105")
        with self.assertRaises(duml.ProtocolError):
            duml.build_low_battery_alarm_set_payload("", True)


if __name__ == "__main__":
    unittest.main()
