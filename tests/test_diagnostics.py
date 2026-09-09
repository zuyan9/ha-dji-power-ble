"""Offline checks that expansion-pack diagnostics retain no identifying data."""

from __future__ import annotations

import copy
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

COMPONENT = Path(__file__).parents[1] / "custom_components" / "dji_power_ble"
PACKAGE = "_dji_power_diagnostics_tests"
REDACTED = "**REDACTED**"


def _module(name: str, **attributes) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    return module


def _redact_data(data, keys):
    """Provide HA's recursive dict/list redaction contract without importing HA."""
    if isinstance(data, dict):
        return {
            key: REDACTED if key in keys else _redact_data(value, keys)
            for key, value in data.items()
        }
    if isinstance(data, list):
        return [_redact_data(value, keys) for value in data]
    return data


def _load_diagnostics() -> types.ModuleType:
    modules = {
        PACKAGE: _module(PACKAGE, __path__=[str(COMPONENT)]),
        "homeassistant": _module("homeassistant"),
        "homeassistant.components": _module("homeassistant.components"),
        "homeassistant.components.diagnostics": _module(
            "homeassistant.components.diagnostics", async_redact_data=_redact_data
        ),
        "homeassistant.config_entries": _module(
            "homeassistant.config_entries", ConfigEntry=object
        ),
        "homeassistant.const": _module(
            "homeassistant.const", CONF_ADDRESS="address", CONF_NAME="name"
        ),
        "homeassistant.core": _module("homeassistant.core", HomeAssistant=object),
    }
    with patch.dict(sys.modules, modules):
        spec = importlib.util.spec_from_file_location(
            f"{PACKAGE}.diagnostics", COMPONENT / "diagnostics.py"
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    return module


diagnostics = _load_diagnostics()


class ExpansionBatteryDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_redacts_pack_identity_and_raw_records_without_mutating_state(
        self,
    ) -> None:
        packs = [
            {
                "seq": 1,
                "serial_number": "TEST-PACK-ONE",
                "battery_percent": 72.5,
                "cycle_count": 18,
                "rated_capacity_wh": 2048,
                "temperature": 25,
                "firmware": "01.00.00.00",
            },
            {
                "seq": 2,
                "serial_number": "TEST-PACK-TWO",
                "battery_percent": 0,
                "cycle_count": 0,
                "rated_capacity_wh": 2048,
                "temperature": None,
                "firmware": None,
            },
        ]
        state = {
            "serial_number": "TEST-STATION",
            "key_01": b"TEST-PACK-ONE\x00TEST-PACK-TWO".hex(),
            "key_0e": b"TEST-STATION".hex(),
            "expansion_batteries": packs,
            "battery_percent": 80,
        }
        entry = types.SimpleNamespace(
            entry_id="station",
            data={
                "address": "AA:BB:CC:DD:EE:FF",
                "pair_key": "test-pair-key",
                "serial_number": "TEST-STATION",
                "model": "DJI Power 2000",
            },
            options={"update_interval": 5},
        )
        coordinator = types.SimpleNamespace(
            data=state, device=types.SimpleNamespace(is_connected=True)
        )
        hass = types.SimpleNamespace(
            data={"dji_power_ble": {entry.entry_id: coordinator}}
        )
        original_state = copy.deepcopy(state)
        original_config = dict(entry.data)

        result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)

        self.assertEqual(
            result["config_entry"],
            {
                "address": REDACTED,
                "pair_key": REDACTED,
                "serial_number": REDACTED,
                "model": "DJI Power 2000",
            },
        )
        self.assertEqual(result["options"], {"update_interval": 5})
        self.assertTrue(result["connected"])
        self.assertEqual(
            result["state"],
            {
                "serial_number": REDACTED,
                "key_01": REDACTED,
                "key_0e": REDACTED,
                "expansion_batteries": [
                    pack | {"serial_number": REDACTED} for pack in packs
                ],
                "battery_percent": 80,
            },
        )
        self.assertEqual(state, original_state)
        self.assertEqual(entry.data, original_config)

    async def test_missing_state_remains_exportable_while_disconnected(self) -> None:
        entry = types.SimpleNamespace(entry_id="station", data={}, options={})
        coordinator = types.SimpleNamespace(
            data=None, device=types.SimpleNamespace(is_connected=False)
        )
        hass = types.SimpleNamespace(
            data={"dji_power_ble": {entry.entry_id: coordinator}}
        )

        result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)

        self.assertEqual(result["state"], {})
        self.assertFalse(result["connected"])
