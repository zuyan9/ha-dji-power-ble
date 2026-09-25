"""Offline checks that protocol diagnostics redact identifying data."""

from __future__ import annotations

import copy
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_duml import (
    SYNTHETIC_ECO_MODE,
    accessory_input,
    accessory_report,
    duml,
    group,
    interface,
    record,
)

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


class ProtocolDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
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
                "name": "TEST-STATION-NAME",
                "pair_key": "test-pair-key",
                "serial_number": "TEST-STATION",
                "model": "DJI Power 2000",
            },
            options={"update_interval": 5, "connection_source": "AA:BB:CC:DD:EE:01",
                     "keep_connection": True},
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
                "name": REDACTED,
                "pair_key": REDACTED,
                "serial_number": REDACTED,
                "model": "DJI Power 2000",
            },
        )
        self.assertEqual(result["options"], {"update_interval": 5,
                                           "connection_source": REDACTED,
                                           "keep_connection": True})
        self.assertEqual(entry.options["connection_source"], "AA:BB:CC:DD:EE:01")
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

    async def test_redacts_meter_record_and_tail_but_preserves_power_settings(
        self,
    ) -> None:
        eco_mode = bytearray(SYNTHETIC_ECO_MODE)
        eco_mode[42:79] = b"TEST-METER-IDENTIFIER".ljust(37, b"\x00")
        for tail in (b"", b"TEST-EXTENDED-IDENTIFIER"):
            with self.subTest(extended=bool(tail)):
                raw = bytes(eco_mode) + tail
                state = duml.parse_telemetry(
                    duml.build_keyed_set_payload(
                        [(duml.ECO_MODE_KEY, raw)], timestamp_ms=0
                    )
                )
                original_state = copy.deepcopy(state)
                entry = types.SimpleNamespace(entry_id="station", data={}, options={})
                coordinator = types.SimpleNamespace(
                    data=state, device=types.SimpleNamespace(is_connected=True)
                )
                hass = types.SimpleNamespace(
                    data={"dji_power_ble": {entry.entry_id: coordinator}}
                )

                result = await diagnostics.async_get_config_entry_diagnostics(
                    hass, entry
                )

                self.assertEqual(result["state"], state | {"key_18": REDACTED})
                self.assertEqual(result["state"]["power_adjustment"], "Manual")
                self.assertEqual(result["state"]["charge_power_w"], 500)
                self.assertEqual(result["state"]["discharge_power_w"], 93)
                self.assertEqual(state, original_state)
                self.assertEqual(state["key_18"], raw.hex())

    async def test_redacts_parallel_and_accessory_records_but_keeps_inputs(
        self,
    ) -> None:
        serial = b"TEST-ACCESSORY01"
        accessory = accessory_report(4, accessory_input(1, 0, 43, 0, 4012))
        interfaces = record(
            0x3031, group(4, interface(1, 5, 0, 43, accessory=accessory))
        )
        state = duml.parse_report(
            record(0x3030, bytes.fromhex("00002b00") + interfaces)
        )
        state |= {
            "key_03": (b"\x01" + b"TEST-PARALLEL-01" + bytes(16)).hex(),
            "key_04": record(
                0x1011, serial + b"\x04" + b"00.00.03.20".ljust(16, b"\x00")
            ).hex(),
        }
        original_state = copy.deepcopy(state)
        entry = types.SimpleNamespace(entry_id="station", data={}, options={})
        coordinator = types.SimpleNamespace(
            data=state, device=types.SimpleNamespace(is_connected=True)
        )
        hass = types.SimpleNamespace(
            data={"dji_power_ble": {entry.entry_id: coordinator}}
        )

        result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)

        self.assertEqual(
            result["state"], state | {"key_03": REDACTED, "key_04": REDACTED}
        )
        self.assertEqual(result["state"]["interfaces"][0]["accessory_type"], 4)
        self.assertEqual(
            result["state"]["interfaces"][0]["accessory_inputs"][0]["input_w"], 43
        )
        for private in (serial.hex(), b"TEST-PARALLEL-01".hex(), "TEST-ACCESSORY"):
            self.assertNotIn(private, repr(result))
        self.assertEqual(state, original_state)

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
