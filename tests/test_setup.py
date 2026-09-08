"""Regression tests for integration setup."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, call

ROOT = Path(__file__).parents[1]
COMPONENT = ROOT / "custom_components" / "dji_power_ble"


def _module(name: str, **attributes) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    sys.modules[name] = module
    return module


custom_components = _module("custom_components")
custom_components.__path__ = [str(ROOT / "custom_components")]
homeassistant = _module("homeassistant")
components = _module("homeassistant.components")
bluetooth = _module(
    "homeassistant.components.bluetooth",
    BluetoothCallbackMatcher=dict,
    BluetoothChange=object,
    BluetoothScanningMode=types.SimpleNamespace(PASSIVE="passive"),
    BluetoothServiceInfoBleak=object,
)
components.bluetooth = bluetooth
homeassistant.components = components
_module("homeassistant.config_entries", ConfigEntry=object)
_module(
    "homeassistant.const",
    CONF_ADDRESS="address",
    Platform=types.SimpleNamespace(
        BINARY_SENSOR="binary_sensor",
        NUMBER="number",
        SENSOR="sensor",
        SWITCH="switch",
    ),
)
_module("homeassistant.core", HomeAssistant=object)
_module("homeassistant.exceptions", ConfigEntryNotReady=Exception)
_module("custom_components.dji_power_ble.coordinator", DjiPowerCoordinator=object)
_module("custom_components.dji_power_ble.device", DjiPowerDevice=object)

SPEC = importlib.util.spec_from_file_location(
    "custom_components.dji_power_ble",
    COMPONENT / "__init__.py",
    submodule_search_locations=[str(COMPONENT)],
)
assert SPEC and SPEC.loader
integration = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = integration
SPEC.loader.exec_module(integration)


class SetupTests(unittest.IsolatedAsyncioTestCase):
    async def test_lowercase_config_address_uses_uppercase_bluetooth_address(
        self,
    ) -> None:
        configured = "e4:b0:63:11:22:33"
        expected = configured.upper()
        service_info = types.SimpleNamespace(time=99.0, manufacturer_data={})
        ble_device = object()
        device = types.SimpleNamespace(is_connected=True)
        coordinator = types.SimpleNamespace(
            async_config_entry_first_refresh=AsyncMock()
        )
        entry = types.SimpleNamespace(
            entry_id="entry-1",
            title="Power station",
            data={"address": configured, "pair_key": "ab" * 16},
        )
        hass = types.SimpleNamespace(
            data={},
            loop=types.SimpleNamespace(time=Mock(return_value=100.0)),
            config_entries=types.SimpleNamespace(
                async_forward_entry_setups=AsyncMock()
            ),
        )
        bluetooth.async_last_service_info = Mock(return_value=service_info)
        bluetooth.async_address_present = Mock(return_value=True)
        bluetooth.async_ble_device_from_address = Mock(return_value=ble_device)
        integration.DjiPowerDevice = Mock(return_value=device)
        integration.DjiPowerCoordinator = Mock(return_value=coordinator)

        self.assertTrue(await integration.async_setup_entry(hass, entry))

        self.assertEqual(
            bluetooth.async_last_service_info.call_args,
            call(hass, expected, connectable=True),
        )
        self.assertEqual(
            bluetooth.async_ble_device_from_address.call_args,
            call(hass, expected, connectable=True),
        )


if __name__ == "__main__":
    unittest.main()
