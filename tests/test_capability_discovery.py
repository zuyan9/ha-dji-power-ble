"""Bounded app-style discovery using synthetic framed BLE replies."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from tests.test_device import (
    FakeBleDevice,
    StationClient,
    device_module,
    duml,
    requested_config_keys,
)


class DiscoveryClient(StationClient):
    def __init__(self, device):
        super().__init__(device, encrypted=False)
        self.values = {0x0D: bytes.fromhex("14100300070102"), 0x16: b""}
        self.status = 2
        self.fallback = False
        self.fail_key = None
        self.block_key = None
        self.blocked = asyncio.Event()
        self.malformed = False
        self.status_only = False

    async def write_gatt_char(self, uuid, value, *, response):  # noqa: ARG002
        packet = duml.DumlPacket.decode(value)
        payload = self.device._decode_payload(packet)
        self.requests.append((packet.command_id, payload))
        assert packet.command_id == duml.GET_COMMAND
        keys = requested_config_keys(payload)
        if keys == [self.block_key]:
            self.blocked.set()
            await asyncio.Event().wait()
        if keys == [self.fail_key]:
            raise device_module.BleakError("synthetic key failure")
        status = self.status if len(keys) > 1 or not self.fallback else 2
        reply = status.to_bytes(4, "little") + duml.build_keyed_set_payload(
            [(key, self.values[key]) for key in keys if key in self.values],
            timestamp_ms=1,
        )
        if self.status_only and len(keys) > 1:
            reply = status.to_bytes(4, "little")
        if self.malformed:
            reply += b"\xff"
        self.send(packet.command_id, reply, sequence=packet.sequence, flags=0x80)


class CapabilityDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.device = device_module.DjiPowerDevice(
            FakeBleDevice(), "ab" * 16, name="Station", model="DJI Power 1000 Mini"
        )
        self.client = DiscoveryClient(self.device)
        self.device._client = self.client
        self.device._write_characteristic = object()

    async def test_complete_success_and_missing_discover_without_writes(self):
        for status in (0, 2):
            with self.subTest(status=status):
                self.client.status = status
                self.client.requests.clear()
                self.device.data.update(
                    energy_reserve_available=True, station_rules=[5, 11],
                    key_0e="pushed", car_auto_threshold=False,
                    port_switches_offered=True,
                    power_adjustment="Manual", eco_available=True,
                )
                await self.device._discover_capabilities()
                self.assertEqual(len(self.client.requests), 1)
                keys = requested_config_keys(self.client.requests[0][1])
                self.assertEqual(tuple(keys), duml.APP_DISCOVERY_KEYS)
                self.assertEqual(len(keys), 30)
                self.assertEqual(self.device.data["discovery_keys"], [0x0D, 0x16])
                self.assertTrue(self.device.data["capability_discovery_complete"])
                self.assertEqual(self.device.data["time_periods"], [])
                self.assertIsNone(self.device.data["eco_available"])
                self.assertIsNone(self.device.data["energy_reserve_available"])
                # Stations such as the V2 report their rules only in pushes.
                self.assertEqual(self.device.data["station_rules"], [5, 11])
                self.assertEqual(self.device.data["key_0e"], "pushed")
                self.assertIs(self.device.data["car_auto_threshold"], False)
                self.assertIs(self.device.data["port_switches_offered"], True)

    async def test_over_statuses_retry_without_publishing_partial_rows(self):
        for status in (1, 3):
            with self.subTest(status=status):
                self.client.status = status
                self.client.fallback = True
                self.client.requests.clear()
                snapshots = []
                unsubscribe = self.device.add_state_listener(snapshots.append)
                try:
                    await self.device._discover_capabilities()
                finally:
                    unsubscribe()
                self.assertEqual(len(self.client.requests), 31)
                singles = [
                    requested_config_keys(p) for _, p in self.client.requests[1:]
                ]
                self.assertEqual(singles, [[key] for key in duml.APP_DISCOVERY_KEYS])
                self.assertTrue(self.device.data["capability_discovery_complete"])
                self.assertEqual(self.device.data["power_switches"][0]["type"], 7)
                self.assertFalse(self.device._pending)

    async def test_invalid_reply_clears_previous_capabilities(self):
        for status, malformed in ((4, False), (2, True)):
            with self.subTest(status=status, malformed=malformed):
                self.device.data.update(
                    power_switches=[{"type": 5, "seq": 1, "sw": 1}],
                    key_0d="old", time_periods=[], station_rules=[11],
                )
                self.client.status, self.client.malformed = status, malformed
                await self.device._discover_capabilities()
                self.assertFalse(self.device.data["capability_discovery_complete"])
                for key in (
                    "power_switches", "key_0d", "time_periods", "station_rules"
                ):
                    self.assertIsNone(self.device.data[key])

    async def test_status_only_over_replies_trigger_single_key_fallback(self):
        self.client.status_only = True
        self.client.fallback = True
        for status, expected_reads in ((1, 31), (3, 31), (4, 1)):
            with self.subTest(status=status):
                self.client.status = status
                self.client.requests.clear()
                await self.device._discover_capabilities()
                self.assertEqual(len(self.client.requests), expected_reads)
                self.assertEqual(
                    self.device.data["capability_discovery_complete"], status != 4
                )

    async def test_single_key_failure_does_not_suppress_later_keys(self):
        self.client.status, self.client.fallback = 3, True
        self.client.fail_key = 0x06
        self.device.data["energy_reserve_available"] = True
        await self.device._discover_capabilities()
        self.assertFalse(self.device.data["capability_discovery_complete"])
        self.assertIsNone(self.device.data["energy_reserve_available"])
        self.assertEqual(self.device.data["discovery_keys"], [0x0D, 0x16])
        self.assertEqual(len(self.client.requests), 31)

    async def test_overall_deadline_clears_unvisited_keys_and_releases_requests(self):
        self.client.status, self.client.fallback = 1, True
        self.client.block_key = 0x05
        self.device.data.update(key_18="old", eco_available=True, station_rules=[5])
        with patch.object(device_module, "CAPABILITY_DISCOVERY_TIMEOUT", 0.02):
            await self.device._discover_capabilities()
        self.assertTrue(self.client.blocked.is_set())
        self.assertFalse(self.device.data["capability_discovery_complete"])
        self.assertIsNone(self.device.data["eco_available"])
        self.assertFalse(self.device._pending)

    async def test_cancelled_discovery_does_not_continue_or_publish(self):
        self.client.status, self.client.fallback = 1, True
        self.client.block_key = 0x00
        snapshots = []
        self.device.add_state_listener(snapshots.append)
        task = asyncio.create_task(self.device._discover_capabilities())
        await self.client.blocked.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(self.client.requests), 2)
        self.assertEqual(snapshots, [])
        self.assertFalse(self.device._pending)

    async def test_generic_model_does_not_probe(self):
        self.device.model = "DJI Power"
        await self.device._discover_capabilities()
        self.assertEqual(self.client.requests, [])
