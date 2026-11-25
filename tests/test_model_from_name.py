"""Model detection from the BLE name (scanners that drop manufacturer data)."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

MODULE_PATH = (
    Path(__file__).parents[1] / "custom_components" / "dji_power_ble" / "duml.py"
)
SPEC = importlib.util.spec_from_file_location("dji_power_test_name", MODULE_PATH)
assert SPEC and SPEC.loader
duml = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = duml
SPEC.loader.exec_module(duml)


class ModelFromNameTests(unittest.TestCase):
    def test_model_from_name(self) -> None:
        self.assertEqual(
            duml.model_from_name("Power1000Mini-1104WD"), "DJI Power 1000 Mini"
        )
        self.assertIsNone(duml.model_from_name("Power2000-ABC"))
        self.assertIsNone(duml.model_from_name(None))


if __name__ == "__main__":
    unittest.main()
