"""Regression tests for publication and disconnect handling."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from enum import Enum
from pathlib import Path
from typing import Generic, TypeVar
from unittest.mock import AsyncMock, Mock, patch

COMPONENT = Path(__file__).parents[1] / "custom_components" / "dji_power_ble"
PACKAGE = "_dji_power_coordinator_test"
T = TypeVar("T")


class ConfigEntryState(Enum):
    LOADED = "loaded"
    SETUP_IN_PROGRESS = "setup_in_progress"


class DataUpdateCoordinator(Generic[T]):
    """Model HA's publication success state without an HA installation."""

    def __init__(self, hass, logger, *, name):
        self.hass = hass
        self.data = None
        self.last_update_success = True

    def async_set_updated_data(self, data):
        self.data = data
        self.last_update_success = True

    def async_set_update_error(self, error):
        self.last_update_success = False


class DjiPowerError(Exception):
    """Test protocol failure."""


class DjiPowerAuthenticationError(DjiPowerError):
    """Test authentication failure."""


class UpdateFailed(Exception):
    """Test HA update failure."""


def _module(name, **attributes):
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    return module


stubs = {
    PACKAGE: _module(PACKAGE, __path__=[str(COMPONENT)]),
    f"{PACKAGE}.device": _module(
        f"{PACKAGE}.device",
        DjiPowerDevice=object,
        DjiPowerError=DjiPowerError,
        DjiPowerAuthenticationError=DjiPowerAuthenticationError,
    ),
    "bleak.exc": _module("bleak.exc", BleakError=type("BleakError", (Exception,), {})),
    "homeassistant.config_entries": _module(
        "homeassistant.config_entries",
        ConfigEntry=object,
        ConfigEntryState=ConfigEntryState,
    ),
    "homeassistant.core": _module(
        "homeassistant.core", HomeAssistant=object, callback=lambda function: function
    ),
    "homeassistant.exceptions": _module(
        "homeassistant.exceptions", HomeAssistantError=Exception
    ),
    "homeassistant.helpers.update_coordinator": _module(
        "homeassistant.helpers.update_coordinator",
        DataUpdateCoordinator=DataUpdateCoordinator,
        UpdateFailed=UpdateFailed,
    ),
}
spec = importlib.util.spec_from_file_location(
    f"{PACKAGE}.coordinator", COMPONENT / "coordinator.py"
)
assert spec and spec.loader
coordinator_module = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, stubs):
    spec.loader.exec_module(coordinator_module)


class CoordinatorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.timer = Mock()
        self.hass = types.SimpleNamespace(
            loop=types.SimpleNamespace(
                time=Mock(return_value=100.0),
                call_later=Mock(return_value=self.timer),
            ),
            config_entries=types.SimpleNamespace(async_schedule_reload=Mock()),
        )
        self.entry = types.SimpleNamespace(
            options={"update_interval": 5},
            entry_id="test-entry",
            state=ConfigEntryState.LOADED,
        )
        self.device = types.SimpleNamespace(
            address="AA:BB:CC:DD:EE:FF",
            add_state_listener=Mock(return_value=Mock()),
            add_disconnect_listener=Mock(return_value=Mock()),
            connect=AsyncMock(),
            disconnect=AsyncMock(),
        )
        self.coordinator = coordinator_module.DjiPowerCoordinator(
            self.hass, self.entry, self.device
        )

    async def test_disconnect_drops_queued_state_and_stays_unavailable(self):
        self.coordinator._handle_state({"battery_percent": 50})
        self.coordinator._handle_state({"battery_percent": 51})

        self.coordinator._handle_disconnect(DjiPowerError("connection lost"))

        self.timer.cancel.assert_called_once_with()
        self.coordinator._flush_pending()
        self.assertFalse(self.coordinator.last_update_success)
        self.assertEqual(self.coordinator.data, {"battery_percent": 50})
        self.hass.config_entries.async_schedule_reload.assert_called_once_with(
            self.entry.entry_id
        )

    async def test_setup_disconnect_does_not_schedule_reload(self):
        self.entry.state = ConfigEntryState.SETUP_IN_PROGRESS

        self.coordinator._handle_disconnect(None)

        self.assertFalse(self.coordinator.last_update_success)
        self.hass.config_entries.async_schedule_reload.assert_not_called()

    async def test_authentication_error_retains_the_actual_failure_reason(self):
        self.device.connect.side_effect = DjiPowerAuthenticationError(
            "station returned an invalid auth challenge"
        )

        with self.assertRaisesRegex(UpdateFailed, "invalid auth challenge"):
            await self.coordinator._async_update_data()


if __name__ == "__main__":
    unittest.main()
