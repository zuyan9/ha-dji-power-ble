"""Regression tests for integration setup."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, call, patch

COMPONENT = Path(__file__).parents[1] / "custom_components" / "dji_power_ble"
PACKAGE = "_dji_power_setup_test"


class ConfigEntryNotReady(Exception):
    """Test replacement for HA's retryable setup failure."""


def _module(name: str, **attributes) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    return module


bluetooth = _module(
    "homeassistant.components.bluetooth",
    BluetoothCallbackMatcher=dict,
    BluetoothChange=object,
    BluetoothScanningMode=types.SimpleNamespace(PASSIVE="passive"),
    BluetoothServiceInfoBleak=object,
)
stubs = {
    "homeassistant": _module("homeassistant"),
    "homeassistant.components": _module(
        "homeassistant.components", bluetooth=bluetooth
    ),
    "homeassistant.components.bluetooth": bluetooth,
    "homeassistant.config_entries": _module(
        "homeassistant.config_entries", ConfigEntry=object
    ),
    "homeassistant.const": _module(
        "homeassistant.const",
        CONF_ADDRESS="address",
        Platform=types.SimpleNamespace(
            BINARY_SENSOR="binary_sensor",
            NUMBER="number",
            SENSOR="sensor",
            SWITCH="switch",
        ),
    ),
    "homeassistant.core": _module("homeassistant.core", HomeAssistant=object),
    "homeassistant.exceptions": _module(
        "homeassistant.exceptions", ConfigEntryNotReady=ConfigEntryNotReady
    ),
    f"{PACKAGE}.coordinator": _module(
        f"{PACKAGE}.coordinator", DjiPowerCoordinator=object
    ),
    f"{PACKAGE}.device": _module(f"{PACKAGE}.device", DjiPowerDevice=object),
}
SPEC = importlib.util.spec_from_file_location(
    PACKAGE, COMPONENT / "__init__.py", submodule_search_locations=[str(COMPONENT)]
)
assert SPEC and SPEC.loader
integration = importlib.util.module_from_spec(SPEC)
with patch.dict(sys.modules, {**stubs, PACKAGE: integration}):
    SPEC.loader.exec_module(integration)


class SetupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.service_info = types.SimpleNamespace(time=99.0, manufacturer_data={})
        self.ble_device = object()
        self.device = types.SimpleNamespace(is_connected=True)
        self.coordinator = types.SimpleNamespace(
            async_config_entry_first_refresh=AsyncMock(), async_disconnect=AsyncMock()
        )
        self.entry = types.SimpleNamespace(
            entry_id="entry-1",
            title="Power station",
            data={"address": "e4:b0:63:11:22:33", "pair_key": "ab" * 16},
        )
        self.hass = types.SimpleNamespace(
            data={},
            loop=types.SimpleNamespace(time=Mock(return_value=100.0)),
            config_entries=types.SimpleNamespace(
                async_forward_entry_setups=AsyncMock(),
                async_unload_platforms=AsyncMock(return_value=True),
                async_schedule_reload=Mock(),
            ),
        )
        bluetooth.async_last_service_info = Mock(return_value=self.service_info)
        bluetooth.async_address_present = Mock(return_value=True)
        bluetooth.async_ble_device_from_address = Mock(return_value=self.ble_device)
        bluetooth.async_register_callback = Mock(return_value=Mock())
        self.enterContext(
            patch.object(integration, "DjiPowerDevice", return_value=self.device)
        )
        self.enterContext(
            patch.object(
                integration, "DjiPowerCoordinator", return_value=self.coordinator
            )
        )

    async def test_lowercase_config_address_uses_uppercase_bluetooth_address(self):
        expected = self.entry.data["address"].upper()

        self.assertTrue(await integration.async_setup_entry(self.hass, self.entry))

        self.assertEqual(
            bluetooth.async_last_service_info.call_args,
            call(self.hass, expected, connectable=True),
        )
        self.assertEqual(
            bluetooth.async_ble_device_from_address.call_args,
            call(self.hass, expected, connectable=True),
        )
        self.coordinator.async_disconnect.assert_not_awaited()
        self.hass.config_entries.async_unload_platforms.assert_not_awaited()

    async def test_cached_advertisement_waits_for_fresh_reappearance(self):
        cancel = Mock()

        def register(hass, callback, matcher, mode):
            callback(self.service_info, None)
            return cancel

        bluetooth.async_register_callback.side_effect = register
        integration._register_reappear_callback(self.hass, self.entry, "ADDRESS")
        self.hass.config_entries.async_schedule_reload.assert_not_called()
        callback = bluetooth.async_register_callback.call_args.args[1]

        callback(types.SimpleNamespace(time=101.0), None)

        self.hass.config_entries.async_schedule_reload.assert_called_once_with(
            "entry-1"
        )
        cancel.assert_called_once_with()
        self.assertFalse(self.hass.data[integration._REAPPEAR_CALLBACKS_KEY])

    async def test_removing_entry_waiting_for_setup_cancels_watcher(self):
        integration._register_reappear_callback(self.hass, self.entry, "ADDRESS")
        cancel = bluetooth.async_register_callback.return_value

        await integration.async_remove_entry(self.hass, self.entry)

        cancel.assert_called_once_with()
        self.assertFalse(self.hass.data[integration._REAPPEAR_CALLBACKS_KEY])

    async def test_setup_failure_or_cancellation_releases_resources(self):
        for phase in ("refresh", "platforms"):
            for error_type in (
                RuntimeError,
                ConfigEntryNotReady,
                asyncio.CancelledError,
            ):
                with self.subTest(phase=phase, error=error_type.__name__):
                    self.hass.data.clear()
                    refresh = self.coordinator.async_config_entry_first_refresh
                    forward = self.hass.config_entries.async_forward_entry_setups
                    refresh.reset_mock(side_effect=True)
                    forward.reset_mock(side_effect=True)
                    self.coordinator.async_disconnect.reset_mock()
                    self.hass.config_entries.async_unload_platforms.reset_mock()
                    bluetooth.async_register_callback.reset_mock()
                    failing_step = refresh if phase == "refresh" else forward
                    failing_step.side_effect = error_type("setup interrupted")

                    with self.assertRaises(error_type):
                        await integration.async_setup_entry(self.hass, self.entry)

                    self.coordinator.async_disconnect.assert_awaited_once_with()
                    self.assertNotIn(
                        "entry-1", self.hass.data.get(integration.DOMAIN, {})
                    )
                    unload = self.hass.config_entries.async_unload_platforms
                    if phase == "platforms":
                        unload.assert_awaited_once_with(
                            self.entry, integration.PLATFORMS
                        )
                    else:
                        unload.assert_not_awaited()
                    self.assertEqual(
                        bluetooth.async_register_callback.call_count,
                        int(error_type is ConfigEntryNotReady),
                    )

    async def test_disconnect_during_platform_setup_cleans_up_and_retries(self):
        self.device.is_connected = False

        with self.assertRaisesRegex(ConfigEntryNotReady, "disconnected during setup"):
            await integration.async_setup_entry(self.hass, self.entry)

        self.coordinator.async_disconnect.assert_awaited_once_with()
        self.hass.config_entries.async_unload_platforms.assert_awaited_once_with(
            self.entry, integration.PLATFORMS
        )
        self.assertNotIn("entry-1", self.hass.data[integration.DOMAIN])
        bluetooth.async_register_callback.assert_called_once()


if __name__ == "__main__":
    unittest.main()
