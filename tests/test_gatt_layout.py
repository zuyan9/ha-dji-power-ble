"""Synthetic discovered services exercise routing without claiming hardware proof."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock

from tests.test_device import (
    ALTERNATE_GATT_LAYOUT,
    DEFAULT_GATT_LAYOUT,
    BleakError,
    FakeBleDevice,
    FakeGattService,
    FakeGattServices,
    StationClient,
    device_module,
    duml,
)


class GattStationClient(StationClient):
    """Require every request to use this connection's discovered write handle."""

    def __init__(self, device, *services, selected=DEFAULT_GATT_LAYOUT):
        super().__init__(device, encrypted=device.model == "DJI Power 1000")
        self.services = FakeGattServices(*services)
        service = self.services.get_service(selected[0])
        self.notify = service.get_characteristic(selected[1]) if service else None
        self.write = service.get_characteristic(selected[2]) if service else None
        self.start_notify = AsyncMock(side_effect=self.subscribe)
        self.clear_cache = AsyncMock()
        self.disconnect = AsyncMock(side_effect=self.close)
        self.writes = []

    async def subscribe(self, characteristic, callback):
        assert characteristic is self.notify
        assert callback == self.device._on_notify
        self.device._report_event.set()

    async def close(self):
        self.is_connected = False

    async def write_gatt_char(self, characteristic, value, *, response):
        assert characteristic is self.write
        assert response is True
        self.writes.append(characteristic)
        await super().write_gatt_char(characteristic, value, response=response)


class GattLayoutTests(unittest.IsolatedAsyncioTestCase):
    def make_device(self, model="DJI Power 2000"):
        device = device_module.DjiPowerDevice(
            FakeBleDevice(), "ab" * 16, name="Synthetic station", model=model
        )
        self.addAsyncCleanup(device.disconnect)
        return device

    async def test_both_layouts_route_authentication_gets_and_sets(self):
        for layout in (DEFAULT_GATT_LAYOUT, ALTERNATE_GATT_LAYOUT):
            for model in ("DJI Power 2000", "DJI Power 1000"):
                with self.subTest(layout=layout[0], model=model):
                    device = self.make_device(model)
                    self.assertIsNone(device._write_characteristic)
                    client = GattStationClient(
                        device, FakeGattService(*layout), selected=layout
                    )
                    device._establish = AsyncMock(return_value=client)

                    await device.connect()
                    await device._set(duml.build_ac_set_payload(True), (0x0D, 0x0E))

                    client.start_notify.assert_awaited_once_with(
                        client.notify, device._on_notify
                    )
                    commands = [command for command, _ in client.requests]
                    self.assertEqual(commands[:2], [duml.AUTH_COMMAND] * 2)
                    self.assertTrue(commands[2:-1])
                    self.assertEqual(set(commands[2:-1]), {duml.GET_COMMAND})
                    self.assertEqual(commands[-1], duml.SET_COMMAND)
                    self.assertEqual(len(client.writes), len(commands))
                    self.assertIs(device._write_characteristic, client.write)
                    client.clear_cache.assert_not_awaited()
                    await device.disconnect()
                    self.assertIsNone(device._write_characteristic)

    async def test_prefers_complete_default_and_skips_incomplete_default(self):
        for complete in (True, False):
            with self.subTest(default_complete=complete):
                device = self.make_device()
                default = FakeGattService(*(
                    DEFAULT_GATT_LAYOUT if complete else DEFAULT_GATT_LAYOUT[:2]
                ))
                selected = DEFAULT_GATT_LAYOUT if complete else ALTERNATE_GATT_LAYOUT
                # Discovery order must not change the preferred layout.
                client = GattStationClient(
                    device, FakeGattService(*ALTERNATE_GATT_LAYOUT), default,
                    selected=selected,
                )
                device._establish = AsyncMock(return_value=client)

                await device.connect()

                client.start_notify.assert_awaited_once_with(
                    client.notify, device._on_notify
                )
                self.assertIs(device._write_characteristic, client.write)
                client.clear_cache.assert_not_awaited()
                await device.disconnect()

    async def test_missing_or_split_pairs_retry_cache_without_authenticating(self):
        unknown = "00001234-0000-1000-8000-00805f9b34fb"
        for services in (
            (),
            (FakeGattService(*DEFAULT_GATT_LAYOUT[:2]),),
            (
                FakeGattService(*DEFAULT_GATT_LAYOUT[:2]),
                FakeGattService(ALTERNATE_GATT_LAYOUT[0], ALTERNATE_GATT_LAYOUT[2]),
            ),
            (
                FakeGattService(*DEFAULT_GATT_LAYOUT[:2]),
                FakeGattService(unknown, DEFAULT_GATT_LAYOUT[2]),
            ),
            (FakeGattService(unknown, *ALTERNATE_GATT_LAYOUT[1:]),),
        ):
            with self.subTest(services=[s.uuid for s in services]):
                device = self.make_device()
                first = GattStationClient(device, *services)
                second = GattStationClient(device, *services)
                device._establish = AsyncMock(side_effect=[first, second])

                with self.assertRaises(BleakError):
                    await device.connect()

                first.clear_cache.assert_awaited_once()
                self.assertEqual(device._establish.await_count, 2)
                for client in (first, second):
                    client.start_notify.assert_not_awaited()
                    client.disconnect.assert_awaited_once()
                    self.assertEqual(client.writes, [])
                self.assertIsNone(device._client)
                self.assertIsNone(device._write_characteristic)

    async def test_cache_recovery_selects_the_rediscovered_alternate_service(self):
        device = self.make_device()
        first = GattStationClient(device, FakeGattService(*DEFAULT_GATT_LAYOUT[:2]))
        second = GattStationClient(
            device, FakeGattService(*ALTERNATE_GATT_LAYOUT, handle=20),
            selected=ALTERNATE_GATT_LAYOUT,
        )
        device._establish = AsyncMock(side_effect=[first, second])

        await device.connect()

        first.clear_cache.assert_awaited_once()
        first.disconnect.assert_awaited_once()
        self.assertEqual(first.writes, [])
        self.assertTrue(second.writes)
        self.assertIs(device._write_characteristic, second.write)

    async def test_reconnect_discards_old_handles_and_reselects_layout(self):
        for layout in (DEFAULT_GATT_LAYOUT, ALTERNATE_GATT_LAYOUT):
            with self.subTest(reconnected_layout=layout[0]):
                device = self.make_device()
                first = GattStationClient(
                    device, FakeGattService(*DEFAULT_GATT_LAYOUT)
                )
                second = GattStationClient(
                    device, FakeGattService(*layout, handle=40), selected=layout
                )
                device._establish = AsyncMock(side_effect=[first, second])
                await device.connect()
                first.is_connected = False
                device._on_disconnect(first)
                self.assertIsNone(device._write_characteristic)
                await device.disconnect()

                await device.connect()
                await device._set(duml.build_ac_set_payload(True), (0x0D, 0x0E))

                self.assertIs(device._write_characteristic, second.write)
                self.assertIsNot(first.write, second.write)
                self.assertTrue(second.writes)
                await device.disconnect()

    async def test_connected_client_without_selection_cannot_send_requests(self):
        device = self.make_device()
        client = GattStationClient(device, FakeGattService(*DEFAULT_GATT_LAYOUT))
        device._client = client

        with self.assertRaises(device_module.DjiPowerError):
            await device._request(duml.AUTH_COMMAND, b"\x00")

        self.assertEqual(client.writes, [])
        self.assertEqual(device._pending, {})
