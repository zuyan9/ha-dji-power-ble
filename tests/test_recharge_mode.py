"""Recharge speed record captured from a Power 1000 Mini (500 W / 1000 W)."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

MODULE_PATH = (
    Path(__file__).parents[1] / "custom_components" / "dji_power_ble" / "duml.py"
)
SPEC = importlib.util.spec_from_file_location("dji_power_test_recharge", MODULE_PATH)
assert SPEC and SPEC.loader
duml = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = duml
SPEC.loader.exec_module(duml)

SLOW = "1f10060001f4010000011f10060002e803000002"
FAST = "1f10060001f4010000021f10060002e803000001"


class RechargeModeTests(unittest.TestCase):
    def test_parse_and_switch(self) -> None:
        self.assertEqual(duml.parse_recharge_modes(SLOW), {500: 1, 1000: 2})
        self.assertEqual(duml.parse_recharge_modes(FAST), {500: 2, 1000: 1})
        for current, watts, expected in ((SLOW, 1000, FAST), (FAST, 500, SLOW)):
            payload = duml.build_recharge_mode_set_payload(current, watts)
            written = duml.parse_keyed_values(payload)[duml.RECHARGE_MODE_KEY]
            self.assertEqual(written.hex(), expected)

    def test_rejects_unknown_layout(self) -> None:
        for bad in ("", "zz", SLOW[:20], None):
            self.assertIsNone(duml.parse_recharge_modes(bad))
        with self.assertRaises(duml.ProtocolError):
            duml.build_recharge_mode_set_payload(SLOW, 750)


if __name__ == "__main__":
    unittest.main()
