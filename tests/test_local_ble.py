"""Offline checks against real Bleak, isolated from the suite's Bleak stubs."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


@unittest.skipUnless(sys.platform == "linux", "BlueZ is specific to Linux")
class LocalBleSubprocessTests(unittest.TestCase):
    def test_real_bluez_backend_offline(self) -> None:
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve())],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    import asyncio
    import importlib.util
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    import bleak
    from bleak.backends.bluezdbus import client as bluez
    from bleak.backends.service import BleakGATTServiceCollection
    from bleak.exc import BleakError
    from dbus_fast import Message, MessageType

    SOURCE = (
        Path(__file__).resolve().parents[1]
        / "custom_components/dji_power_ble/local_ble.py"
    )
    spec = importlib.util.spec_from_file_location("local_ble_offline", SOURCE)
    assert spec is not None and spec.loader is not None
    local = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(local)

    ADAPTER = "11:22:33:44:55:66"
    OTHER_ADAPTER = "22:33:44:55:66:77"
    STATION = "AA:BB:CC:DD:EE:FF"
    ADAPTER_PATH = "/org/bluez/hci0"
    DEVICE_PATH = f"{ADAPTER_PATH}/dev_AA_BB_CC_DD_EE_FF"
    ADAPTER_INTERFACE = "org.bluez.Adapter1"
    DEVICE_INTERFACE = "org.bluez.Device1"

    class Manager:
        def __init__(self, connected=False):
            self._properties = {
                ADAPTER_PATH: {
                    ADAPTER_INTERFACE: {
                        "Address": ADAPTER,
                        "Powered": True,
                        "Alias": "Local controller",
                    }
                },
                DEVICE_PATH: {
                    DEVICE_INTERFACE: {
                        "Address": STATION,
                        "Adapter": ADAPTER_PATH,
                        "Connected": connected,
                        "Name": "Station",
                    }
                },
            }
            self.watchers = {}
            self.next_watcher = 0

        def is_connected(self, path):
            return self._properties[path][DEVICE_INTERFACE]["Connected"]

        def add_device_watcher(self, path, on_connected_changed, on_value_changed):
            self.next_watcher += 1
            self.watchers[self.next_watcher] = (
                path,
                on_connected_changed,
                on_value_changed,
            )
            return self.next_watcher

        def remove_device_watcher(self, watcher):
            self.watchers.pop(watcher, None)

        def set_connected(self, connected):
            self._properties[DEVICE_PATH][DEVICE_INTERFACE]["Connected"] = connected
            for _, callback, _ in list(self.watchers.values()):
                callback(connected)

    class Bus:
        def __init__(self, manager, before_connect=None):
            self.manager = manager
            self.before_connect = before_connect
            self.messages = []
            self.closed = False

        async def connect(self):
            if self.before_connect:
                self.before_connect()
            return self

        async def call(self, message):
            if self.closed:
                raise OSError("D-Bus connection closed")
            self.messages.append(message)
            if message.member == "Connect":
                self.manager.set_connected(True)
            elif message.member == "Disconnect":
                self.manager.set_connected(False)
            return Message(message_type=MessageType.METHOD_RETURN, reply_serial=1)

        async def send(self, message):
            await self.call(message)

        def disconnect(self):
            self.closed = True

        async def wait_for_disconnect(self):
            assert self.closed

    class RealLocalBleTests(unittest.IsolatedAsyncioTestCase):
        async def asyncSetUp(self):
            self.manager = Manager()
            self.callbacks = []
            self.client = None
            self.bus = Bus(self.manager)
            self.patches = [
                patch.object(local, "_manager", AsyncMock(return_value=self.manager)),
                patch.object(
                    bluez,
                    "get_global_bluez_manager",
                    AsyncMock(return_value=self.manager),
                ),
                patch.object(bluez, "MessageBus", return_value=self.bus),
                patch.object(bluez.BlueZFeatures, "checked_bluez_version", True),
                patch.object(bluez.BlueZFeatures, "supported_version", True),
            ]
            for patcher in self.patches:
                patcher.start()
                self.addCleanup(patcher.stop)

            async def services(backend, **kwargs):
                backend.services = BleakGATTServiceCollection()
                await asyncio.sleep(0)
                return backend.services

            patcher = patch.object(
                local._local_backend_type(), "_get_services", services
            )
            patcher.start()
            self.addCleanup(patcher.stop)

        async def asyncTearDown(self):
            if self.client is not None and self.client.is_connected:
                await self.client.disconnect()
            await asyncio.sleep(0)

        async def connect(self, *, retained=False):
            self.manager.set_connected(retained)
            device = await local.async_local_device(ADAPTER, STATION)
            self.assertIsNotNone(device)
            self.client = local.LocalBleakClient(
                device, disconnected_callback=self.callbacks.append
            )
            await self.client.connect()
            return self.client

        def members(self):
            return [message.member for message in self.bus.messages]

        async def test_adapter_list_excludes_offline_and_noncentral_controllers(self):
            self.manager._properties["/org/bluez/hci1"] = {
                ADAPTER_INTERFACE: {"Address": OTHER_ADAPTER, "Powered": False}
            }
            self.manager._properties["/org/bluez/hci2"] = {
                ADAPTER_INTERFACE: {
                    "Address": "33:44:55:66:77:88",
                    "Powered": True,
                    "Roles": ["peripheral"],
                }
            }
            self.assertEqual(
                await local.async_local_adapters(),
                {ADAPTER: f"Local controller (hci0, {ADAPTER})"},
            )

        async def test_connected_station_resolves_without_any_advertisement(self):
            self.manager.set_connected(True)
            device = await local.async_local_device(ADAPTER.lower(), STATION.lower())
            self.assertEqual(device.address, STATION)
            self.assertEqual(device.details["path"], DEVICE_PATH)
            self.assertTrue(device.details["props"]["Connected"])

        async def test_missing_or_powered_off_adapter_never_falls_back(self):
            self.assertIsNone(await local.async_local_device(OTHER_ADAPTER, STATION))
            self.manager._properties[ADAPTER_PATH][ADAPTER_INTERFACE]["Powered"] = False
            self.assertIsNone(await local.async_local_device(ADAPTER, STATION))

        async def test_station_on_another_controller_is_not_returned(self):
            self.manager._properties[DEVICE_PATH][DEVICE_INTERFACE]["Adapter"] = (
                "/org/bluez/hci1"
            )
            self.assertIsNone(await local.async_local_device(ADAPTER, STATION))

        async def test_adapter_identity_is_rechecked_before_connect(self):
            device = await local.async_local_device(ADAPTER, STATION)
            self.client = local.LocalBleakClient(device)
            self.manager._properties[ADAPTER_PATH][ADAPTER_INTERFACE]["Address"] = (
                OTHER_ADAPTER
            )
            with self.assertRaisesRegex(BleakError, "unavailable"):
                await self.client.connect()
            self.assertEqual(self.members(), [])

        async def test_adapter_replacement_during_connect_releases_wrong_link(self):
            def replace_adapter():
                self.manager._properties[ADAPTER_PATH][ADAPTER_INTERFACE]["Address"] = (
                    OTHER_ADAPTER
                )

            self.bus.before_connect = replace_adapter
            with self.assertRaisesRegex(BleakError, "device changed"):
                await self.connect()
            self.assertFalse(self.client.is_connected)
            self.assertFalse(self.client.connected_before_attach)
            self.assertFalse(self.manager.is_connected(DEVICE_PATH))
            self.assertEqual(self.manager.watchers, {})
            self.assertEqual(self.members(), ["Connect", "Disconnect"])

        async def test_missing_station_is_rechecked_before_connect(self):
            device = await local.async_local_device(ADAPTER, STATION)
            self.client = local.LocalBleakClient(device)
            del self.manager._properties[DEVICE_PATH]
            with self.assertRaisesRegex(BleakError, "unavailable"):
                await self.client.connect()
            self.assertEqual(self.members(), [])

        async def test_real_bleak_adopts_existing_link_without_connect_request(self):
            original_client = bleak.BleakClient
            client = await self.connect(retained=True)
            self.assertTrue(client.connected_before_attach)
            self.assertTrue(client.is_connected)
            self.assertEqual(self.members(), [])
            self.assertEqual(len(self.manager.watchers), 1)
            self.assertIs(bleak.BleakClient, original_client)

        async def test_new_connection_is_not_marked_as_retained(self):
            client = await self.connect()
            self.assertFalse(client.connected_before_attach)
            self.assertEqual(self.members(), ["Connect"])

        async def test_disconnect_race_does_not_reuse_previous_authentication(self):
            self.bus.before_connect = lambda: self.manager.set_connected(False)
            client = await self.connect(retained=True)
            self.assertFalse(client.connected_before_attach)
            self.assertEqual(self.members(), ["Connect"])

        async def test_notify_and_write_keep_public_client_contract(self):
            client = await self.connect()
            received = []
            characteristic = SimpleNamespace(
                obj=(f"{DEVICE_PATH}/service001/char002", {}),
                handle=2,
                uuid="0000c305-0000-1000-8000-00805f9b34fb",
            )
            await client.start_notify(
                characteristic, lambda *args: received.append(args)
            )
            client._backend._notification_callbacks[characteristic.obj[0]](
                bytearray(b"data")
            )
            await client.write_gatt_char(characteristic, b"request", response=True)
            self.assertEqual(received, [(characteristic, bytearray(b"data"))])
            self.assertEqual(self.members(), ["Connect", "StartNotify", "WriteValue"])

        async def test_detach_finishes_real_monitor_without_disconnecting_station(self):
            client = await self.connect(retained=True)
            monitor = client._backend._retention_monitor_task
            self.assertFalse(monitor.done())
            await client.detach()
            self.assertTrue(self.bus.closed)
            self.assertFalse(client.is_connected)
            self.assertTrue(self.manager.is_connected(DEVICE_PATH))
            self.assertTrue(monitor.done())
            self.assertEqual(self.manager.watchers, {})
            self.assertEqual(self.callbacks, [])
            self.assertNotIn("Disconnect", self.members())
            await client.disconnect()
            self.assertNotIn("Disconnect", self.members())

        async def test_detach_before_monitor_first_runs_is_also_safe(self):
            async def services(backend, **kwargs):
                backend.services = BleakGATTServiceCollection()
                return backend.services

            with patch.object(local._local_backend_type(), "_get_services", services):
                client = await self.connect(retained=True)
                self.assertIsNone(client._backend._retention_monitor_task)
                await client.detach()
            self.assertTrue(self.manager.is_connected(DEVICE_PATH))
            self.assertNotIn("Disconnect", self.members())

        async def test_ordinary_disconnect_releases_the_station(self):
            client = await self.connect(retained=True)
            await client.disconnect()
            self.assertEqual(self.members(), ["Disconnect"])
            self.assertFalse(self.manager.is_connected(DEVICE_PATH))
            self.assertTrue(self.bus.closed)
            self.assertEqual(self.callbacks, [client])

        async def test_local_drain_error_does_not_undo_committed_detachment(self):
            client = await self.connect(retained=True)
            self.bus.wait_for_disconnect = AsyncMock(side_effect=TimeoutError)
            with self.assertLogs(local.__name__, level="DEBUG"):
                await client.detach()
            self.assertTrue(self.manager.is_connected(DEVICE_PATH))
            self.assertFalse(client.is_connected)
            self.assertTrue(self.bus.closed)
            self.assertNotIn("Disconnect", self.members())

        async def test_cancelled_connect_still_releases_link_and_watchers(self):
            entered = asyncio.Event()

            async def stalled_services(backend, **kwargs):
                entered.set()
                await asyncio.Event().wait()

            device = await local.async_local_device(ADAPTER, STATION)
            self.client = local.LocalBleakClient(device)
            with patch.object(
                local._local_backend_type(), "_get_services", stalled_services
            ):
                task = asyncio.create_task(self.client.connect())
                await entered.wait()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertFalse(self.manager.is_connected(DEVICE_PATH))
            self.assertFalse(self.client.connected_before_attach)
            self.assertEqual(self.manager.watchers, {})
            self.assertEqual(self.members(), ["Connect", "Disconnect"])

        async def test_real_monitor_still_disconnects_on_unplanned_cancellation(self):
            client = await self.connect(retained=True)
            monitor = client._backend._retention_monitor_task
            monitor.cancel()
            await monitor
            self.assertEqual(self.members(), ["Disconnect"])
            self.assertFalse(self.manager.is_connected(DEVICE_PATH))
            self.assertTrue(self.bus.closed)

        async def test_unexpected_link_loss_closes_bus_before_owner_callback(self):
            client = await self.connect(retained=True)
            observed = []
            client._disconnected_callback = lambda _: observed.append(self.bus.closed)
            self.manager.set_connected(False)
            self.assertEqual(observed, [True])
            self.assertFalse(client.is_connected)
            self.assertFalse(client.connected_before_attach)
            self.assertEqual(self.manager.watchers, {})

        async def test_unknown_monitor_shape_refuses_detachment(self):
            client = await self.connect(retained=True)
            event = client._backend._disconnect_monitor_event
            client._backend._disconnect_monitor_event = object()
            try:
                with self.assertRaisesRegex(BleakError, "cannot safely retain"):
                    await client.detach()
                self.assertTrue(client.is_connected)
                self.assertFalse(self.bus.closed)
            finally:
                client._backend._disconnect_monitor_event = event

        async def test_unknown_bleak_major_is_rejected_without_patching_bleak(self):
            original_client = bleak.BleakClient
            with (
                patch.object(local, "_backend_type", None),
                patch.object(local, "version", return_value="4.0.0"),
                self.assertRaisesRegex(BleakError, "version"),
            ):
                local._local_backend_type()
            self.assertIs(bleak.BleakClient, original_client)

        async def test_non_linux_has_no_local_choices(self):
            with patch.object(local.sys, "platform", "darwin"):
                self.assertEqual(await local.async_local_adapters(), {})
                self.assertIsNone(await local.async_local_device(ADAPTER, STATION))

    unittest.main(argv=[sys.argv[0]], defaultTest="RealLocalBleTests", verbosity=2)
