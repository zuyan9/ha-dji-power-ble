"""Offline checks for throttled state delivery across BLE disconnections."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

COMPONENT = Path(__file__).parents[1] / "custom_components" / "dji_power_ble"
PACKAGE = "_dji_power_coordinator_tests"
LOADED = object()


class DjiPowerError(Exception):
    """Test protocol failure."""


class DjiPowerAuthenticationError(DjiPowerError):
    """Test authentication failure."""


class _DataUpdateCoordinator:
    def __class_getitem__(cls, item):
        return cls

    def __init__(self, hass, logger, *, name) -> None:
        self.hass = hass
        self.data = None
        self.last_update_success = False
        self.last_exception = None

    def async_set_updated_data(self, data) -> None:
        self.data = data
        self.last_update_success = True
        self.last_exception = None

    def async_set_update_error(self, error) -> None:
        self.last_update_success = False
        self.last_exception = error


def _module(name: str, **attributes) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    return module


def _load_coordinator() -> types.ModuleType:
    modules = {
        PACKAGE: _module(PACKAGE, __path__=[str(COMPONENT)]),
        f"{PACKAGE}.device": _module(
            f"{PACKAGE}.device",
            DjiPowerDevice=object,
            DjiPowerError=DjiPowerError,
            DjiPowerAuthenticationError=DjiPowerAuthenticationError,
        ),
        "homeassistant": _module("homeassistant"),
        "homeassistant.config_entries": _module(
            "homeassistant.config_entries",
            ConfigEntry=object,
            ConfigEntryState=types.SimpleNamespace(LOADED=LOADED),
        ),
        "homeassistant.core": _module(
            "homeassistant.core", HomeAssistant=object, callback=lambda method: method
        ),
        "homeassistant.exceptions": _module(
            "homeassistant.exceptions", HomeAssistantError=Exception
        ),
        "homeassistant.helpers": _module("homeassistant.helpers"),
        "homeassistant.helpers.update_coordinator": _module(
            "homeassistant.helpers.update_coordinator",
            DataUpdateCoordinator=_DataUpdateCoordinator,
            UpdateFailed=type("UpdateFailed", (Exception,), {}),
        ),
        "bleak.exc": _module(
            "bleak.exc", BleakError=type("BleakError", (Exception,), {})
        ),
    }
    with patch.dict(sys.modules, modules):
        spec = importlib.util.spec_from_file_location(
            f"{PACKAGE}.coordinator", COMPONENT / "coordinator.py"
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    return module


coordinator_module = _load_coordinator()


class CoordinatorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.loop = Mock()
        self.loop.time.return_value = 100.0
        self.hass = types.SimpleNamespace(
            loop=self.loop,
            config_entries=types.SimpleNamespace(async_schedule_reload=Mock()),
        )
        self.entry = types.SimpleNamespace(
            entry_id="station", options={"update_interval": 5}, state=LOADED
        )
        self.device = types.SimpleNamespace(
            address="AA:BB:CC:DD:EE:FF",
            add_state_listener=Mock(),
            add_disconnect_listener=Mock(),
            connect=AsyncMock(),
            disconnect=AsyncMock(),
        )
        self.coordinator = coordinator_module.DjiPowerCoordinator(
            self.hass, self.entry, self.device
        )

    def test_pending_snapshot_cannot_restore_availability_after_disconnect(
        self,
    ) -> None:
        self.coordinator._handle_state({"battery_percent": 60})
        self.assertTrue(self.coordinator.last_update_success)
        self.loop.time.return_value = 101.0
        self.coordinator._handle_state({"battery_percent": 61})
        timer = self.loop.call_later.return_value
        flush_pending = self.loop.call_later.call_args.args[1]

        self.coordinator._handle_disconnect(RuntimeError("test BLE disconnect"))

        timer.cancel.assert_called_once_with()
        self.assertFalse(self.coordinator.last_update_success)
        self.assertEqual(
            str(self.coordinator.last_exception), "test BLE disconnect"
        )
        self.hass.config_entries.async_schedule_reload.assert_called_once_with("station")
        # Also exercise a callback already queued by the loop when cancellation runs.
        flush_pending()
        self.assertFalse(self.coordinator.last_update_success)
        self.assertEqual(self.coordinator.data, {"battery_percent": 60})

    def test_connected_updates_publish_latest_throttled_snapshot(self) -> None:
        self.coordinator._handle_state({"battery_percent": 60})
        self.loop.time.return_value = 101.0
        self.coordinator._handle_state({"battery_percent": 61})
        self.loop.time.return_value = 102.0
        self.coordinator._handle_state({"battery_percent": 62})

        self.loop.call_later.assert_called_once()
        self.assertEqual(self.coordinator.data, {"battery_percent": 60})
        self.loop.time.return_value = 105.0
        self.loop.call_later.call_args.args[1]()

        self.assertEqual(self.coordinator.data, {"battery_percent": 62})
        self.assertTrue(self.coordinator.last_update_success)
        self.hass.config_entries.async_schedule_reload.assert_not_called()

    def test_disconnect_during_setup_does_not_schedule_reload(self) -> None:
        self.entry.state = "setup_in_progress"

        self.coordinator._handle_disconnect(None)

        self.assertFalse(self.coordinator.last_update_success)
        self.assertEqual(
            str(self.coordinator.last_exception), "Bluetooth connection lost"
        )
        self.hass.config_entries.async_schedule_reload.assert_not_called()

    async def test_authentication_error_retains_the_actual_failure_reason(self) -> None:
        error = DjiPowerAuthenticationError(
            "station returned an invalid auth challenge"
        )
        self.device.connect.side_effect = error

        with self.assertRaisesRegex(
            coordinator_module.UpdateFailed, "invalid auth challenge"
        ):
            await self.coordinator._async_update_data()
