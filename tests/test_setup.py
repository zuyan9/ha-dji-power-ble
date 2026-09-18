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
            SELECT="select",
            SENSOR="sensor",
            SWITCH="switch",
        ),
    ),
    "homeassistant.core": _module(
        "homeassistant.core", HomeAssistant=object,
        HassJob=lambda target: types.SimpleNamespace(target=target),
    ),
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
        self.device = types.SimpleNamespace(
            is_connected=True,
            model="DJI Power",
            address="E4:B0:63:11:22:33",
            can_retain_connection=False,
            matches_connection=Mock(return_value=True),
            disconnect=AsyncMock(),
        )
        self.coordinator = types.SimpleNamespace(
            async_config_entry_first_refresh=AsyncMock(), async_disconnect=AsyncMock(),
            async_shutdown=AsyncMock(),
            async_release_device=Mock(return_value=self.device),
            async_apply_options=Mock(),
            device=self.device,
        )
        self.entry = types.SimpleNamespace(
            entry_id="entry-1",
            title="Power station",
            data={"address": "e4:b0:63:11:22:33", "pair_key": "ab" * 16},
            options={},
            disabled_by=None,
            add_update_listener=Mock(return_value=Mock()),
            async_on_unload=Mock(),
        )
        self.created_tasks = []

        def create_task(coroutine, *args, **kwargs):
            task = asyncio.create_task(coroutine)
            self.created_tasks.append(task)
            return task

        self.hass = types.SimpleNamespace(
            data={},
            loop=types.SimpleNamespace(
                time=Mock(return_value=100.0), call_later=Mock(return_value=Mock())
            ),
            async_create_task=Mock(side_effect=create_task),
            async_add_shutdown_job=Mock(return_value=Mock()),
            config_entries=types.SimpleNamespace(
                async_forward_entry_setups=AsyncMock(),
                async_unload_platforms=AsyncMock(return_value=True),
                async_schedule_reload=Mock(),
                async_update_entry=Mock(),
            ),
        )
        bluetooth.async_last_service_info = Mock(return_value=self.service_info)
        bluetooth.async_address_present = Mock(return_value=True)
        bluetooth.async_ble_device_from_address = Mock(return_value=self.ble_device)
        bluetooth.async_register_callback = Mock(return_value=Mock())
        def make_device(*args, **kwargs):
            self.device.model = kwargs["model"]
            return self.device

        self.enterContext(
            patch.object(integration, "DjiPowerDevice", side_effect=make_device)
        )
        self.enterContext(
            patch.object(
                integration, "DjiPowerCoordinator", return_value=self.coordinator
            )
        )

    async def asyncTearDown(self) -> None:
        for task in self.created_tasks:
            if not task.done():
                task.cancel()
        if self.created_tasks:
            await asyncio.gather(*self.created_tasks, return_exceptions=True)

    async def test_actions_registered_without_loaded_entries(self):
        register = Mock()
        with patch.dict(sys.modules, {
            PACKAGE: integration,
            f"{PACKAGE}.services": _module(
                f"{PACKAGE}.services", async_setup_services=register
            ),
        }):
            self.assertTrue(await integration.async_setup(self.hass, {}))
        register.assert_called_once_with(self.hass)
        self.assertEqual(self.hass.data, {})

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

    def _local_device_lookup(self, result):
        lookup = AsyncMock(return_value=result)
        self.slot_manager = types.SimpleNamespace(
            async_allocate_connection_slot=Mock(return_value=True),
            async_release_connection_slot=Mock(),
        )
        self.enterContext(patch.dict(sys.modules, {
            PACKAGE: integration,
            f"{PACKAGE}.local_ble": _module(
                f"{PACKAGE}.local_ble", async_local_device=lookup
            ),
            "habluetooth": _module(
                "habluetooth", get_manager=lambda: self.slot_manager
            ),
        }))
        self.entry.options = {"connection_source": "AA:BB:CC:DD:EE:01"}
        return lookup

    async def test_retained_local_device_needs_no_advertisement_or_proxy_lookup(self):
        lookup = self._local_device_lookup(self.ble_device)
        self.entry.data["model"] = "DJI Power 1000"
        self.entry.options["keep_connection"] = True
        bluetooth.async_last_service_info.return_value = None
        bluetooth.async_address_present.return_value = False

        self.assertTrue(await integration.async_setup_entry(self.hass, self.entry))

        lookup.assert_awaited_once_with("AA:BB:CC:DD:EE:01", "E4:B0:63:11:22:33")
        bluetooth.async_ble_device_from_address.assert_not_called()
        bluetooth.async_address_present.assert_not_called()
        self.assertEqual(integration.DjiPowerDevice.call_args.kwargs["local_adapter"],
                         "AA:BB:CC:DD:EE:01")
        self.assertTrue(integration.DjiPowerDevice.call_args.kwargs["keep_connection"])
        self.assertIs(
            integration.DjiPowerDevice.call_args.kwargs["allocate_connection_slot"],
            self.slot_manager.async_allocate_connection_slot,
        )

    async def test_missing_selected_adapter_never_falls_back(self):
        self._local_device_lookup(None)

        with self.assertRaisesRegex(ConfigEntryNotReady, "selected local"):
            await integration.async_setup_entry(self.hass, self.entry)

        bluetooth.async_ble_device_from_address.assert_not_called()
        integration.DjiPowerDevice.assert_not_called()
        self.hass.async_add_shutdown_job.assert_not_called()
        callback = bluetooth.async_register_callback.call_args.args[1]
        for source, timestamp in (("PROXY", 101), ("AA:BB:CC:DD:EE:02", 101),
                                  ("AA:BB:CC:DD:EE:01", 99)):
            callback(types.SimpleNamespace(time=timestamp, source=source), None)
        self.hass.config_entries.async_schedule_reload.assert_not_called()
        callback(types.SimpleNamespace(time=101, source="aa:bb:cc:dd:ee:01"), None)
        self.hass.config_entries.async_schedule_reload.assert_called_once()

    async def test_local_adapter_query_failure_is_retryable_without_fallback(self):
        lookup = self._local_device_lookup(None)
        lookup.side_effect = RuntimeError("D-Bus unavailable")
        with self.assertRaisesRegex(ConfigEntryNotReady, "adapter unavailable"):
            await integration.async_setup_entry(self.hass, self.entry)
        bluetooth.async_ble_device_from_address.assert_not_called()
        self.hass.async_add_shutdown_job.assert_not_called()

    async def test_automatic_retention_rejected_before_connect_for_every_model(self):
        for model in ("DJI Power 1000", "DJI Power 1000 V2", "DJI Power 1000 Mini",
                      "DJI Power 2000", "DJI Power"):
            with self.subTest(model=model):
                self.entry.options = {"connection_source": "automatic",
                                      "keep_connection": True}
                self.entry.data["model"] = model
                with self.assertRaisesRegex(ConfigEntryNotReady, "requires"):
                    await integration.async_setup_entry(self.hass, self.entry)
                integration.DjiPowerDevice.assert_not_called()

    async def test_local_retention_available_for_other_models(self):
        self._local_device_lookup(self.ble_device)
        self.entry.options["keep_connection"] = True
        bluetooth.async_last_service_info.return_value = None
        for model in ("DJI Power 1000 V2", "DJI Power 1000 Mini", "DJI Power 2000",
                      "DJI Power"):
            with self.subTest(model=model):
                self.entry.data["model"] = model

                self.assertTrue(
                    await integration.async_setup_entry(self.hass, self.entry)
                )

                options = integration.DjiPowerDevice.call_args.kwargs
                self.assertEqual(options["model"], model)
                self.assertTrue(options["keep_connection"])
                self.assertEqual(options["local_adapter"], "AA:BB:CC:DD:EE:01")
                self.assertTrue(
                    await integration.async_unload_entry(self.hass, self.entry)
                )
        bluetooth.async_ble_device_from_address.assert_not_called()

    async def test_model_is_saved_for_restart_without_advertising(self):
        with patch.object(integration, "_model_from_discovery",
                          return_value="DJI Power 1000"):
            await integration.async_setup_entry(self.hass, self.entry)
        self.hass.config_entries.async_update_entry.assert_called_once_with(
            self.entry, data={**self.entry.data, "model": "DJI Power 1000"}
        )

    async def test_core_shutdown_uses_early_job_and_ordinary_unload_removes_it(self):
        await integration.async_setup_entry(self.hass, self.entry)
        job = self.hass.async_add_shutdown_job.call_args.args[0]
        await job.target()
        self.coordinator.async_shutdown.assert_awaited_once_with()
        self.coordinator.async_disconnect.assert_not_awaited()

        self.assertTrue(await integration.async_unload_entry(self.hass, self.entry))
        self.hass.async_add_shutdown_job.return_value.assert_called_once_with()
        self.coordinator.async_disconnect.assert_awaited_once_with()
        self.assertFalse(self.hass.data[integration._SHUTDOWN_CALLBACKS_KEY])

    async def test_failed_unload_keeps_shutdown_job_and_connection(self):
        await integration.async_setup_entry(self.hass, self.entry)
        self.hass.config_entries.async_unload_platforms.return_value = False
        self.assertFalse(await integration.async_unload_entry(self.hass, self.entry))
        self.hass.async_add_shutdown_job.return_value.assert_not_called()
        self.coordinator.async_disconnect.assert_not_awaited()

    async def test_shutdown_registration_failure_closes_initialized_client(self):
        self.hass.async_add_shutdown_job.side_effect = RuntimeError("shutdown started")
        with self.assertRaisesRegex(RuntimeError, "shutdown started"):
            await integration.async_setup_entry(self.hass, self.entry)
        self.coordinator.async_disconnect.assert_awaited_once_with()
        self.hass.config_entries.async_unload_platforms.assert_awaited_once()
        self.assertNotIn(self.entry.entry_id, self.hass.data[integration.DOMAIN])

    async def _loaded_retention_entry(self):
        lookup = self._local_device_lookup(self.ble_device)
        self.entry.options["keep_connection"] = True
        self.entry.data["model"] = "DJI Power 1000"
        self.device.can_retain_connection = True
        await integration.async_setup_entry(self.hass, self.entry)
        return lookup

    async def test_reload_reuses_live_device_without_bluetooth_lookup(self):
        lookup = await self._loaded_retention_entry()
        self.assertTrue(await integration.async_unload_entry(self.hass, self.entry))
        self.coordinator.async_release_device.assert_called_once_with()
        self.coordinator.async_disconnect.assert_not_awaited()
        self.device.disconnect.assert_not_awaited()
        self.assertIn(
            self.entry.entry_id, self.hass.data[integration._RETAINED_CONNECTIONS_KEY]
        )
        integration.DjiPowerDevice.reset_mock()
        integration.DjiPowerCoordinator.reset_mock()
        lookup.reset_mock()
        bluetooth.async_last_service_info.reset_mock()
        bluetooth.async_address_present.reset_mock()
        bluetooth.async_ble_device_from_address.reset_mock()

        self.assertTrue(await integration.async_setup_entry(self.hass, self.entry))

        integration.DjiPowerDevice.assert_not_called()
        integration.DjiPowerCoordinator.assert_called_once_with(
            self.hass, self.entry, self.device
        )
        lookup.assert_not_awaited()
        bluetooth.async_last_service_info.assert_not_called()
        bluetooth.async_address_present.assert_not_called()
        bluetooth.async_ble_device_from_address.assert_not_called()
        self.assertFalse(self.hass.data[integration._RETAINED_CONNECTIONS_KEY])
        self.hass.loop.call_later.return_value.cancel.assert_called_once_with()
        self.device.disconnect.assert_not_awaited()

    async def test_handoff_timer_closes_an_unclaimed_connection(self):
        await self._loaded_retention_entry()
        await integration.async_unload_entry(self.hass, self.entry)
        timer_call = self.hass.loop.call_later.call_args
        self.assertEqual(timer_call.args[0], integration._RELOAD_HANDOFF_TIMEOUT)

        timer_call.args[1](*timer_call.args[2:])
        await asyncio.gather(*self.created_tasks)

        self.assertFalse(self.hass.data[integration._RETAINED_CONNECTIONS_KEY])
        self.device.disconnect.assert_awaited_once_with()
        self.coordinator.async_shutdown.assert_not_awaited()

    async def test_stale_expiry_callback_cannot_close_a_claimed_connection(self):
        await self._loaded_retention_entry()
        await integration.async_unload_entry(self.hass, self.entry)
        timer_call = self.hass.loop.call_later.call_args
        await integration.async_setup_entry(self.hass, self.entry)

        timer_call.args[1](*timer_call.args[2:])
        await asyncio.sleep(0)

        self.device.disconnect.assert_not_awaited()
        self.assertEqual(self.created_tasks, [])
        self.assertIs(
            self.hass.data[integration.DOMAIN][self.entry.entry_id], self.coordinator
        )

    async def test_setup_waits_for_expired_connection_close_before_new_attach(self):
        lookup = await self._loaded_retention_entry()
        await integration.async_unload_entry(self.hass, self.entry)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def disconnect(*args, **kwargs):
            entered.set()
            await release.wait()

        self.device.disconnect.side_effect = disconnect
        timer_call = self.hass.loop.call_later.call_args
        timer_call.args[1](*timer_call.args[2:])
        await entered.wait()
        lookup.reset_mock()
        integration.DjiPowerDevice.reset_mock()
        setup = asyncio.create_task(
            integration.async_setup_entry(self.hass, self.entry)
        )
        self.created_tasks.append(setup)
        await asyncio.sleep(0)
        self.assertFalse(setup.done())
        lookup.assert_not_awaited()
        integration.DjiPowerDevice.assert_not_called()

        release.set()
        self.assertTrue(await setup)

        self.device.disconnect.assert_awaited_once_with()
        integration.DjiPowerDevice.assert_called_once()
        lookup.assert_awaited_once()
        self.assertFalse(self.hass.data[integration._RETAINED_CONNECTIONS_KEY])

    async def test_remove_waits_for_existing_expiry_close_without_duplicate_disconnect(
        self,
    ):
        await self._loaded_retention_entry()
        await integration.async_unload_entry(self.hass, self.entry)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def disconnect(*args, **kwargs):
            entered.set()
            await release.wait()

        self.device.disconnect.side_effect = disconnect
        timer_call = self.hass.loop.call_later.call_args
        timer_call.args[1](*timer_call.args[2:])
        await entered.wait()
        remove = asyncio.create_task(
            integration.async_remove_entry(self.hass, self.entry)
        )
        self.created_tasks.append(remove)
        await asyncio.sleep(0)
        self.assertFalse(remove.done())
        self.device.disconnect.assert_awaited_once_with()

        release.set()
        await remove

        self.device.disconnect.assert_awaited_once_with()
        self.assertFalse(self.hass.data[integration._RETAINED_CONNECTIONS_KEY])

    async def test_shutdown_while_parked_preserves_the_link(self):
        await self._loaded_retention_entry()
        await integration.async_unload_entry(self.hass, self.entry)
        shutdown = self.hass.async_add_shutdown_job.call_args.args[0]

        await shutdown.target()

        self.device.disconnect.assert_awaited_once_with(keep_connection=True)
        self.assertFalse(self.hass.data[integration._RETAINED_CONNECTIONS_KEY])
        self.hass.loop.call_later.return_value.cancel.assert_called_once_with()

    async def test_cancelling_setup_does_not_cancel_expired_connection_cleanup(self):
        await self._loaded_retention_entry()
        await integration.async_unload_entry(self.hass, self.entry)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def disconnect(*args, **kwargs):
            entered.set()
            await release.wait()

        self.device.disconnect.side_effect = disconnect
        timer_call = self.hass.loop.call_later.call_args
        timer_call.args[1](*timer_call.args[2:])
        await entered.wait()
        cleanup = self.created_tasks[-1]
        setup = asyncio.create_task(
            integration.async_setup_entry(self.hass, self.entry)
        )
        self.created_tasks.append(setup)
        await asyncio.sleep(0)

        setup.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await setup

        self.assertFalse(cleanup.done())
        release.set()
        await cleanup
        self.device.disconnect.assert_awaited_once_with()
        self.assertFalse(self.hass.data[integration._RETAINED_CONNECTIONS_KEY])

    async def test_handoff_timer_registration_failure_closes_and_removes_shutdown_job(
        self,
    ):
        await self._loaded_retention_entry()
        cancel = Mock()
        self.hass.async_add_shutdown_job.return_value = cancel
        self.hass.loop.call_later.side_effect = RuntimeError("timer unavailable")

        with self.assertRaisesRegex(RuntimeError, "timer unavailable"):
            await integration.async_unload_entry(self.hass, self.entry)

        cancel.assert_called_once_with()
        self.device.disconnect.assert_awaited_once_with()
        self.assertFalse(self.hass.data.get(integration._RETAINED_CONNECTIONS_KEY))

    async def test_removing_parked_entry_closes_connection_and_cancels_timer(self):
        await self._loaded_retention_entry()
        await integration.async_unload_entry(self.hass, self.entry)

        await integration.async_remove_entry(self.hass, self.entry)

        self.assertFalse(self.hass.data[integration._RETAINED_CONNECTIONS_KEY])
        self.device.disconnect.assert_awaited_once_with()
        self.hass.loop.call_later.return_value.cancel.assert_called_once_with()

    async def test_failed_unload_never_parks_or_releases_the_active_owner(self):
        await self._loaded_retention_entry()
        self.hass.config_entries.async_unload_platforms.return_value = False

        self.assertFalse(await integration.async_unload_entry(self.hass, self.entry))

        self.assertIs(
            self.hass.data[integration.DOMAIN][self.entry.entry_id], self.coordinator
        )
        self.assertFalse(self.hass.data.get(integration._RETAINED_CONNECTIONS_KEY))
        self.coordinator.async_release_device.assert_not_called()
        self.coordinator.async_disconnect.assert_not_awaited()
        self.device.disconnect.assert_not_awaited()
        self.hass.loop.call_later.assert_not_called()

    async def test_disabled_entry_does_not_retain_a_live_connection(self):
        await self._loaded_retention_entry()
        self.entry.disabled_by = "user"

        self.assertTrue(await integration.async_unload_entry(self.hass, self.entry))

        self.coordinator.async_disconnect.assert_awaited_once_with()
        self.coordinator.async_release_device.assert_not_called()
        self.assertFalse(self.hass.data.get(integration._RETAINED_CONNECTIONS_KEY))

    async def test_disabling_retention_closes_on_unload(self):
        await self._loaded_retention_entry()
        self.entry.options["keep_connection"] = False

        await integration.async_unload_entry(self.hass, self.entry)

        self.coordinator.async_disconnect.assert_awaited_once_with()
        self.coordinator.async_release_device.assert_not_called()
        self.assertFalse(self.hass.data.get(integration._RETAINED_CONNECTIONS_KEY))

    async def test_incompatible_credentials_or_adapter_close_on_unload(self):
        await self._loaded_retention_entry()
        self.entry.options["connection_source"] = "AA:BB:CC:DD:EE:02"
        self.entry.data["pair_key"] = "cd" * 16
        self.device.matches_connection.return_value = False

        await integration.async_unload_entry(self.hass, self.entry)

        self.coordinator.async_disconnect.assert_awaited_once_with()
        self.coordinator.async_release_device.assert_not_called()
        self.device.matches_connection.assert_called_with(
            self.entry.data["address"],
            "cd" * 16,
            local_adapter="AA:BB:CC:DD:EE:02",
            model="DJI Power 1000",
        )

    async def test_unauthenticated_or_lost_connection_is_not_parked(self):
        await self._loaded_retention_entry()
        self.device.can_retain_connection = False

        await integration.async_unload_entry(self.hass, self.entry)

        self.coordinator.async_disconnect.assert_awaited_once_with()
        self.coordinator.async_release_device.assert_not_called()
        self.assertFalse(self.hass.data.get(integration._RETAINED_CONNECTIONS_KEY))

    async def test_changed_options_after_parking_discard_old_connection(self):
        lookup = await self._loaded_retention_entry()
        await integration.async_unload_entry(self.hass, self.entry)
        self.device.matches_connection.return_value = False
        self.entry.options["connection_source"] = "AA:BB:CC:DD:EE:02"
        integration.DjiPowerDevice.reset_mock()
        lookup.reset_mock()

        await integration.async_setup_entry(self.hass, self.entry)

        self.device.disconnect.assert_awaited_once_with()
        integration.DjiPowerDevice.assert_called_once()
        lookup.assert_awaited_once_with("AA:BB:CC:DD:EE:02", "E4:B0:63:11:22:33")
        self.assertFalse(self.hass.data[integration._RETAINED_CONNECTIONS_KEY])

    async def test_setup_failure_after_claim_closes_instead_of_reparking(self):
        await self._loaded_retention_entry()
        await integration.async_unload_entry(self.hass, self.entry)
        self.coordinator.async_config_entry_first_refresh.side_effect = RuntimeError(
            "new coordinator setup failed"
        )

        with self.assertRaisesRegex(RuntimeError, "new coordinator setup"):
            await integration.async_setup_entry(self.hass, self.entry)

        self.coordinator.async_disconnect.assert_awaited_once_with()
        self.assertFalse(self.hass.data[integration._RETAINED_CONNECTIONS_KEY])
        self.assertNotIn(self.entry.entry_id, self.hass.data[integration.DOMAIN])

    async def test_options_listener_is_registered_for_unload_cleanup(self):
        await integration.async_setup_entry(self.hass, self.entry)

        self.entry.add_update_listener.assert_called_once_with(
            integration._async_options_updated
        )
        self.entry.async_on_unload.assert_called_once_with(
            self.entry.add_update_listener.return_value
        )

    async def test_interval_option_applies_live_without_reload(self):
        await integration.async_setup_entry(self.hass, self.entry)
        self.entry.options["update_interval"] = 45

        await integration._async_options_updated(self.hass, self.entry)

        self.coordinator.async_apply_options.assert_called_once_with()
        self.hass.config_entries.async_schedule_reload.assert_not_called()
        self.coordinator.async_disconnect.assert_not_awaited()

    async def test_retention_toggle_applies_live_then_closes_on_unload(self):
        await self._loaded_retention_entry()
        self.entry.options["keep_connection"] = False

        await integration._async_options_updated(self.hass, self.entry)

        self.coordinator.async_apply_options.assert_called_once_with()
        self.hass.config_entries.async_schedule_reload.assert_not_called()
        self.coordinator.async_disconnect.assert_not_awaited()
        await integration.async_unload_entry(self.hass, self.entry)
        self.coordinator.async_disconnect.assert_awaited_once_with()
        self.assertFalse(self.hass.data.get(integration._RETAINED_CONNECTIONS_KEY))

    async def test_connection_option_change_schedules_reload(self):
        await self._loaded_retention_entry()
        self.entry.options["connection_source"] = "AA:BB:CC:DD:EE:02"
        self.device.matches_connection.return_value = False

        await integration._async_options_updated(self.hass, self.entry)

        self.hass.config_entries.async_schedule_reload.assert_called_once_with(
            self.entry.entry_id
        )
        self.coordinator.async_apply_options.assert_not_called()

    async def test_options_update_without_loaded_entry_is_a_noop(self):
        await integration._async_options_updated(self.hass, self.entry)

        self.hass.config_entries.async_schedule_reload.assert_not_called()
        self.coordinator.async_apply_options.assert_not_called()


if __name__ == "__main__":
    unittest.main()
