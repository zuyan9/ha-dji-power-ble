"""Offline checks for throttled telemetry and immediate confirmed controls."""

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
CONTROL_WRITES = (
    ("set_ac", (True,), {}, {"ac_enabled": False}, {"ac_enabled": True}),
    (
        "set_sdc", (5, 1, False), {},
        {"power_switches": [{"type": 5, "seq": 1, "sw": 1}]},
        {"power_switches": [{"type": 5, "seq": 1, "sw": 2}]},
    ),
    (
        "set_car_charger", (5, 1, 4), {"recharge_power_w": 450},
        {"car_chargers": [{"interface_type": 5, "seq": 1, "type": 4,
                            "p_from_car_v": 400}]},
        {"car_chargers": [{"interface_type": 5, "seq": 1, "type": 4,
                            "p_from_car_v": 450}]},
    ),
    (
        "set_charge_limits",
        (),
        {"discharge_limit": 10, "recharge_limit": 80},
        {"discharge_limit": 5, "recharge_limit": 100},
        {"discharge_limit": 10, "recharge_limit": 80},
    ),
    (
        "set_discharge_power",
        (422,),
        {},
        {"discharge_power_w": 93},
        {"discharge_power_w": 422},
    ),
    (
        "set_charge_power",
        (1000,),
        {},
        {"charge_power_w": 600},
        {"charge_power_w": 1000},
    ),
    (
        "set_power_adjustment",
        ("Manual",),
        {},
        {"power_adjustment": "Automatic"},
        {"power_adjustment": "Manual"},
    ),
    (
        "set_time_periods",
        ([{"type": "off_peak", "start": "01:00", "end": "05:00"}],),
        {},
        {"time_periods": []},
        {"time_periods": [
            {"type": "off_peak", "start": "01:00", "end": "05:00"}
        ]},
    ),
)


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
            keep_connection=False,
        )
        self.coordinator = coordinator_module.DjiPowerCoordinator(
            self.hass, self.entry, self.device
        )

    async def test_shutdown_retains_only_opted_in_session_before_core_stopping(self):
        for keep in (False, True):
            with self.subTest(keep_connection=keep):
                self.device.keep_connection = keep
                self.device.disconnect.reset_mock()
                coordinator = coordinator_module.DjiPowerCoordinator(
                    self.hass, self.entry, self.device
                )
                self.hass.is_stopping = False
                coordinator._handle_state({"battery_percent": 60})
                self.loop.time.return_value = 101.0
                coordinator._handle_state({"battery_percent": 61})
                await coordinator.async_shutdown()
                expected = {"keep_connection": True} if keep else {}
                self.device.disconnect.assert_awaited_once_with(**expected)
                self.assertIsNone(coordinator._pending_data)
                coordinator._handle_disconnect(None)
                self.hass.config_entries.async_schedule_reload.assert_not_called()
                with self.assertRaisesRegex(Exception, "shutting down"):
                    await coordinator._async_update_data()
                self.device.connect.assert_not_awaited()
                await coordinator.async_disconnect()
                self.device.disconnect.assert_awaited_once()

    async def test_ordinary_unload_closes_even_when_retention_enabled(self):
        self.device.keep_connection = True
        await self.coordinator.async_disconnect()
        await self.coordinator.async_shutdown()
        self.device.disconnect.assert_awaited_once_with()

    async def test_late_callbacks_cannot_publish_after_shutdown(self):
        self.coordinator._handle_state({"battery_percent": 60})
        self.loop.time.return_value = 101.0
        self.coordinator._handle_state({"battery_percent": 61})
        flush = self.loop.call_later.call_args.args[1]
        await self.coordinator.async_shutdown()
        flush()
        self.coordinator._handle_state({"battery_percent": 62})
        self.coordinator._publish({"battery_percent": 63})
        self.assertEqual(self.coordinator.data, {"battery_percent": 60})

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

    def test_zero_interval_publishes_rapid_state_changes_without_a_timer(self):
        self.entry.options["update_interval"] = 0
        coordinator = coordinator_module.DjiPowerCoordinator(
            self.hass, self.entry, self.device
        )
        with patch.object(
            coordinator, "async_set_updated_data",
            wraps=coordinator.async_set_updated_data,
        ) as publish:
            for now, battery in ((100.0, 60), (100.0, 61), (100.01, 62)):
                self.loop.time.return_value = now

                coordinator._handle_state({"battery_percent": battery})

                self.assertEqual(coordinator.data, {"battery_percent": battery})
                self.assertTrue(coordinator.last_update_success)
            self.assertEqual(publish.call_count, 3)
        self.assertIsNone(coordinator._push_timer)
        self.assertIsNone(coordinator._pending_data)
        self.loop.call_later.assert_not_called()
        self.device.connect.assert_not_awaited()
        self.device.disconnect.assert_not_awaited()
        self.hass.config_entries.async_schedule_reload.assert_not_called()

    def test_live_interval_changes_reschedule_or_publish_pending_snapshot(self):
        for interval, now, delay in (
            (2, 101.0, 1.0), (10, 102.0, 8.0), (1, 103.0, None), (0, 101.0, None)
        ):
            with self.subTest(interval=interval):
                self.loop.reset_mock()
                self.loop.time.return_value = 100.0
                self.entry.options = {"update_interval": 5}
                coordinator = coordinator_module.DjiPowerCoordinator(
                    self.hass, self.entry, self.device
                )
                coordinator._handle_state({"battery_percent": 60})
                self.loop.time.return_value = 101.0
                coordinator._handle_state({"battery_percent": 61})
                timer = self.loop.call_later.return_value
                self.loop.time.return_value = now
                self.entry.options = {
                    "update_interval": interval, "keep_connection": True
                }

                coordinator.async_apply_options()

                timer.cancel.assert_called_once_with()
                self.assertEqual(coordinator._publish_interval, interval)
                self.assertTrue(self.device.keep_connection)
                if delay is None:
                    self.assertEqual(coordinator.data, {"battery_percent": 61})
                    self.assertIsNone(coordinator._pending_data)
                    self.assertIsNone(coordinator._push_timer)
                    self.loop.call_later.assert_called_once()
                else:
                    self.assertEqual(coordinator.data, {"battery_percent": 60})
                    self.assertEqual(self.loop.call_later.call_args.args[0], delay)
                    self.assertEqual(self.loop.call_later.call_count, 2)
                self.device.disconnect.assert_not_awaited()
                self.device.connect.assert_not_awaited()
                self.hass.config_entries.async_schedule_reload.assert_not_called()

    def test_live_change_from_zero_restores_throttling_without_reconnecting(self):
        self.entry.options["update_interval"] = 0
        self.coordinator.async_apply_options()
        self.coordinator._handle_state({"battery_percent": 60})
        self.loop.time.return_value = 101.0
        self.coordinator._handle_state({"battery_percent": 61})
        self.entry.options["update_interval"] = 5

        self.coordinator.async_apply_options()

        self.loop.call_later.assert_not_called()
        self.loop.time.return_value = 102.0
        self.coordinator._handle_state({"battery_percent": 62})
        self.assertEqual(self.loop.call_later.call_args.args[0], 4.0)
        self.loop.time.return_value = 103.0
        self.coordinator._handle_state({"battery_percent": 63})
        self.assertEqual(self.coordinator.data, {"battery_percent": 61})
        self.loop.call_later.assert_called_once()
        self.loop.time.return_value = 106.0
        self.loop.call_later.call_args.args[1]()
        self.assertEqual(self.coordinator.data, {"battery_percent": 63})
        self.assertIsNone(self.coordinator._pending_data)
        self.assertIsNone(self.coordinator._push_timer)
        self.device.connect.assert_not_awaited()
        self.device.disconnect.assert_not_awaited()
        self.hass.config_entries.async_schedule_reload.assert_not_called()

    def test_retention_toggle_applies_without_disturbing_pending_publication(self):
        self.coordinator._handle_state({"battery_percent": 60})
        self.loop.time.return_value = 101.0
        self.coordinator._handle_state({"battery_percent": 61})
        timer = self.loop.call_later.return_value
        for keep in (True, False):
            with self.subTest(keep=keep):
                self.entry.options["keep_connection"] = keep

                self.coordinator.async_apply_options()

                self.assertIs(self.device.keep_connection, keep)
                self.assertIs(self.coordinator._push_timer, timer)
                timer.cancel.assert_not_called()
                self.device.disconnect.assert_not_awaited()
                self.device.connect.assert_not_awaited()
        self.loop.call_later.assert_called_once()

    def test_old_timer_cannot_publish_early_after_interval_is_lengthened(self):
        self.coordinator._handle_state({"battery_percent": 60})
        self.loop.time.return_value = 101.0
        self.coordinator._handle_state({"battery_percent": 61})
        old_flush = self.loop.call_later.call_args.args[1]
        self.entry.options["update_interval"] = 10
        self.loop.time.return_value = 102.0
        self.coordinator.async_apply_options()

        self.loop.time.return_value = 105.0
        old_flush()

        self.assertEqual(self.coordinator.data, {"battery_percent": 60})
        self.assertEqual(self.loop.call_later.call_args.args[0], 5.0)
        self.loop.time.return_value = 110.0
        self.loop.call_later.call_args.args[1]()
        self.assertEqual(self.coordinator.data, {"battery_percent": 61})

    async def test_release_transfers_live_device_without_old_callbacks_or_timers(self):
        self.device.is_connected = True
        old_state = self.device.add_state_listener.call_args.args[0]
        old_disconnect = self.device.add_disconnect_listener.call_args.args[0]
        unsubscribe_state = self.device.add_state_listener.return_value
        unsubscribe_disconnect = self.device.add_disconnect_listener.return_value
        old_state({"battery_percent": 60})
        self.loop.time.return_value = 101.0
        old_state({"battery_percent": 61})
        timer = self.loop.call_later.return_value
        flush = self.loop.call_later.call_args.args[1]

        self.coordinator.async_release_device()
        self.coordinator.async_release_device()

        unsubscribe_state.assert_called_once_with()
        unsubscribe_disconnect.assert_called_once_with()
        timer.cancel.assert_called_once_with()
        self.assertIsNone(self.coordinator._pending_data)
        self.assertIsNone(self.coordinator._push_timer)
        self.assertTrue(self.device.is_connected)
        old_state({"battery_percent": 62})
        old_disconnect(None)
        flush()
        self.assertEqual(self.coordinator.data, {"battery_percent": 60})
        replacement = coordinator_module.DjiPowerCoordinator(
            self.hass, self.entry, self.device
        )
        replacement._handle_state({"battery_percent": 63})
        await self.coordinator.async_disconnect()
        await self.coordinator.async_shutdown()
        self.device.disconnect.assert_not_awaited()
        self.assertEqual(replacement.data, {"battery_percent": 63})
        self.hass.config_entries.async_schedule_reload.assert_not_called()

    def test_released_coordinator_ignores_late_option_updates(self):
        self.coordinator.async_release_device()
        self.entry.options = {"update_interval": 1, "keep_connection": True}

        self.coordinator.async_apply_options()

        self.assertEqual(self.coordinator._publish_interval, 5)
        self.assertFalse(self.device.keep_connection)
        self.loop.call_later.assert_not_called()
        self.device.disconnect.assert_not_awaited()

    def test_disconnect_during_setup_does_not_schedule_reload(self) -> None:
        self.entry.state = "setup_in_progress"

        self.coordinator._handle_disconnect(None)

        self.assertFalse(self.coordinator.last_update_success)
        self.assertEqual(
            str(self.coordinator.last_exception), "Bluetooth connection lost"
        )
        self.hass.config_entries.async_schedule_reload.assert_not_called()

    def _prepare_throttled_control(self, state: dict[str, object]) -> tuple:
        self.loop.reset_mock()
        self.loop.time.return_value = 100.0
        self.entry.options["update_interval"] = 60
        self.device.data = {"battery_percent": 60, **state}
        self.coordinator = coordinator_module.DjiPowerCoordinator(
            self.hass, self.entry, self.device
        )
        self.coordinator._handle_state(dict(self.device.data))
        self.loop.time.return_value = 101.0
        self.coordinator._handle_state({"battery_percent": 61, **state})
        self.loop.call_later.assert_called_once()
        delay, flush_pending = self.loop.call_later.call_args.args
        self.assertEqual(delay, 59)
        return self.loop.call_later.return_value, flush_pending

    async def test_all_controls_publish_immediately_and_clear_pending_telemetry(
        self,
    ) -> None:
        for method, args, kwargs, previous, requested in CONTROL_WRITES:
            with self.subTest(control=method):
                timer, flush_pending = self._prepare_throttled_control(previous)
                confirmed = {"battery_percent": 62, **requested}
                with patch.object(
                    self.coordinator,
                    "async_set_updated_data",
                    wraps=self.coordinator.async_set_updated_data,
                ) as publish:

                    async def confirm(
                        *args,
                        confirmed=confirmed,
                        publish=publish,
                        **kwargs,
                    ):
                        publish.assert_not_called()
                        self.device.data = dict(confirmed)

                    setter = AsyncMock(side_effect=confirm)
                    setattr(self.device, method, setter)

                    await getattr(self.coordinator, f"async_{method}")(*args, **kwargs)

                    setter.assert_awaited_once_with(*args, **kwargs)
                    publish.assert_called_once_with(confirmed)
                    self.assertEqual(self.coordinator.data, confirmed)
                    self.assertIsNot(self.coordinator.data, self.device.data)
                    self.assertEqual(self.loop.time(), 101.0)
                    timer.cancel.assert_called_once_with()
                    self.assertIsNone(self.coordinator._pending_data)
                    self.assertIsNone(self.coordinator._push_timer)
                    # A cancelled callback may already be queued by the event loop.
                    self.loop.time.return_value = 160.0
                    flush_pending()
                    publish.assert_called_once_with(confirmed)
                    self.assertEqual(self.coordinator.data, confirmed)

    async def test_failed_controls_do_not_publish_requested_values(self) -> None:
        for method, args, kwargs, previous, _requested in CONTROL_WRITES:
            with self.subTest(control=method):
                timer, flush_pending = self._prepare_throttled_control(previous)
                error = DjiPowerError("station did not confirm the requested value")
                setter = AsyncMock(side_effect=error)
                setattr(self.device, method, setter)
                with patch.object(
                    self.coordinator,
                    "async_set_updated_data",
                    wraps=self.coordinator.async_set_updated_data,
                ) as publish:
                    with self.assertRaisesRegex(
                        coordinator_module.HomeAssistantError,
                        "station did not confirm the requested value",
                    ) as raised:
                        await getattr(self.coordinator, f"async_{method}")(
                            *args, **kwargs
                        )

                    self.assertIs(raised.exception.__cause__, error)
                    setter.assert_awaited_once_with(*args, **kwargs)
                    publish.assert_not_called()
                    self.assertEqual(
                        self.coordinator.data, {"battery_percent": 60, **previous}
                    )
                    timer.cancel.assert_not_called()
                    self.loop.time.return_value = 160.0
                    flush_pending()
                    publish.assert_called_once_with(
                        {"battery_percent": 61, **previous}
                    )
                    self.assertEqual(
                        self.coordinator.data, {"battery_percent": 61, **previous}
                    )

    async def test_authentication_error_retains_the_actual_failure_reason(self) -> None:
        error = DjiPowerAuthenticationError(
            "station returned an invalid auth challenge"
        )
        self.device.connect.side_effect = error

        with self.assertRaisesRegex(
            coordinator_module.UpdateFailed, "invalid auth challenge"
        ):
            await self.coordinator._async_update_data()
