"""Light record (0x1B) captured from a Power 1000 Mini."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

MODULE_PATH = (
    Path(__file__).parents[1] / "custom_components" / "dji_power_ble" / "duml.py"
)
SPEC = importlib.util.spec_from_file_location("dji_power_test_light", MODULE_PATH)
assert SPEC and SPEC.loader
duml = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = duml
SPEC.loader.exec_module(duml)


def written(payload: bytes) -> str:
    return duml.parse_keyed_values(payload)[duml.LIGHT_KEY].hex()


class LightTests(unittest.TestCase):
    def test_parse(self) -> None:
        self.assertEqual(
            duml.parse_light("01640a64"),
            {"mode": 1, "maximum": 100, "minimum": 10, "brightness": 100},
        )
        self.assertEqual(duml.parse_light("03640a64")["mode"], 3)
        self.assertEqual(duml.parse_light("02640a35")["brightness"], 0x35)

    def test_rejects_bad_records(self) -> None:
        for bad in (None, "", "zz", "01640a", "00640a64", "04640a64", "01640a05", "01640a65"):
            self.assertIsNone(duml.parse_light(bad), bad)

    def test_build(self) -> None:
        self.assertEqual(written(duml.build_light_set_payload("01640a64", mode=2)), "02640a64")
        self.assertEqual(written(duml.build_light_set_payload("02640a64", mode=3)), "03640a64")
        self.assertEqual(written(duml.build_light_set_payload("02640a64", brightness=53)), "02640a35")
        for kwargs in ({"mode": 4}, {"brightness": 9}, {"brightness": 101}):
            with self.assertRaises(duml.ProtocolError):
                duml.build_light_set_payload("02640a64", **kwargs)
        with self.assertRaises(duml.ProtocolError):
            duml.build_light_set_payload(None, mode=1)


if __name__ == "__main__":
    unittest.main()
