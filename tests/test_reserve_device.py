"""Synthetic backup-reserve and accessory-list transactions; no hardware implied."""

from __future__ import annotations

import itertools
import struct
import unittest
from unittest.mock import AsyncMock, patch

from tests.test_accessory_device import AccessoryClient
from tests.test_device import FakeBleDevice, device_module, duml

RESERVE_MODELS = ("DJI Power 1000", "DJI Power 1000 V2", "DJI Power 2000")
RESERVE_OFF = bytes.fromhex("01025000")
# Recharge limit 90 %, discharge limit 5 %: DJI Home's reserve range is 10-90 %.
CHARGE_LIMITS = struct.pack("<6I", 100, 70, 90, 15, 0, 5)
ACCESSORIES = struct.pack("<HH", 0x1011, 33) + (
    b"TEST-ACCESSORY01" + b"\x04" + b"00.00.03.20".ljust(16, b"\x00")
)


class BackupReserveDeviceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.reset_device()
        retry = patch.object(device_module, "READBACK_RETRY_INTERVAL", 0)
        retry.start()
        self.addCleanup(retry.stop)

    def reset_device(self, model="DJI Power 1000"):
        self.device = device_module.DjiPowerDevice(
            FakeBleDevice(), "ab" * 16, name="Station", model=model
        )
        self.client = AccessoryClient(self.device)
        self.client.values[duml.ENERGY_STORAGE_KEY] = RESERVE_OFF
        self.client.values[duml.CHARGE_LIMIT_KEY] = CHARGE_LIMITS
        self.device._client = self.client
        self.device._write_characteristic = object()

    def commands(self):
        return [command for command, _ in self.client.requests]

    async def test_write_uses_fresh_get_set_ack_and_readback(self):
        for model, (kwargs, written, expected) in itertools.product(
            RESERVE_MODELS,
            (
                ({"enabled": True}, "01015000", {"energy_reserve_enabled": True}),
                ({"percent": 35}, "01022300", {"energy_reserve": 35}),
                (
                    {"enabled": True, "percent": 90},
                    "01015a00",
                    {"energy_reserve_enabled": True, "energy_reserve": 90},
                ),
            ),
        ):
            with self.subTest(model=model, kwargs=kwargs):
                self.reset_device(model)
                with patch.object(device_module.asyncio, "sleep", AsyncMock()) as sleep:
                    await self.device.set_energy_reserve(**kwargs)

                sleep.assert_not_awaited()
                # A level change first reads the charge limits that bound it.
                reads = [b"\x00\x05\x10"] * ("percent" in kwargs) + [b"\x00\x06\x10"]
                self.assertEqual(
                    self.client.requests,
                    [(duml.GET_COMMAND, payload) for payload in reads]
                    + [(duml.SET_COMMAND, self.client.requests[len(reads)][1])]
                    + [(duml.GET_COMMAND, b"\x00\x06\x10")],
                )
                entries = duml.parse_keyed_values(self.client.requests[len(reads)][1])
                self.assertEqual(
                    list(entries), [duml.ENERGY_STORAGE_KEY, duml.RULES_KEY]
                )
                self.assertEqual(entries[duml.ENERGY_STORAGE_KEY].hex(), written)
                for key, value in expected.items():
                    self.assertEqual(self.device.data[key], value)
                encryption = 6 if model == "DJI Power 1000" else 0
                self.assertTrue(
                    all(
                        packet.encryption_type == encryption
                        for packet in self.client.wire_requests
                    )
                )

    async def test_unvalidated_models_are_rejected_without_requests(self):
        for model in ("DJI Power 1000 Mini", "DJI Power"):
            with self.subTest(model=model):
                self.reset_device(model)
                with self.assertRaises(device_module.DjiPowerError):
                    await self.device.set_energy_reserve(enabled=True)
                self.assertEqual(self.client.requests, [])

    async def test_unoffered_or_invalid_change_never_sends_a_write(self):
        for value, kwargs in (
            (bytes.fromhex("00025000"), {"enabled": True}),
            (bytes.fromhex("01035000"), {"enabled": None, "percent": 40}),
            (RESERVE_OFF, {"percent": 9}),
            (RESERVE_OFF, {"percent": 91}),
        ):
            with self.subTest(value=value.hex(), kwargs=kwargs):
                self.reset_device()
                self.client.values[duml.ENERGY_STORAGE_KEY] = value
                with self.assertRaises(device_module.DjiPowerError):
                    await self.device.set_energy_reserve(**kwargs)
                self.assertNotIn(duml.SET_COMMAND, self.commands())

    async def test_level_uses_fresh_limits_and_requires_them(self):
        self.client.values[duml.CHARGE_LIMIT_KEY] = struct.pack(
            "<6I", 100, 70, 100, 15, 0, 15
        )
        with self.assertRaises(device_module.DjiPowerError):
            await self.device.set_energy_reserve(percent=19)
        await self.device.set_energy_reserve(percent=100)
        self.assertEqual(self.device.data["energy_reserve"], 100)

        self.reset_device()
        self.client.values.pop(duml.CHARGE_LIMIT_KEY)
        with self.assertRaisesRegex(device_module.DjiPowerError, "charge limits"):
            await self.device.set_energy_reserve(percent=50)
        self.assertEqual(self.commands(), [duml.GET_COMMAND])

    async def test_omitted_state_invalidates_controls_without_writing(self):
        self.device.data.update(
            energy_reserve_available=True, energy_reserve_enabled=False
        )
        self.client.values.pop(duml.ENERGY_STORAGE_KEY)
        with self.assertRaises(device_module.DjiPowerError):
            await self.device.set_energy_reserve(enabled=True)
        self.assertEqual(self.commands(), [duml.GET_COMMAND])
        self.assertIsNone(self.device.data["energy_reserve_available"])
        self.assertIsNone(self.device.data["energy_reserve_enabled"])

    async def test_rejected_ack_or_ineffective_write_is_not_confirmed(self):
        self.client.ack_override = {
            duml.ENERGY_STORAGE_KEY: bytes(4), duml.RULES_KEY: b"\x01\x00\x00\x00"
        }
        with self.assertRaises(device_module.DjiPowerError):
            await self.device.set_energy_reserve(enabled=True)
        self.assertIs(self.device.data["energy_reserve_enabled"], False)
        self.assertEqual(self.commands(), [duml.GET_COMMAND, duml.SET_COMMAND])

        self.reset_device()
        self.client.apply_set = False
        with self.assertRaisesRegex(
            device_module.DjiPowerError, "did not report the requested backup reserve"
        ):
            await self.device.set_energy_reserve(percent=20)
        # Charge limits, reserve, then the initial and retried reserve readbacks.
        self.assertEqual(
            self.commands().count(duml.GET_COMMAND),
            device_module.READBACK_RETRIES + 3,
        )
        self.assertEqual(self.device.data["energy_reserve"], 80)

    async def test_refresh_reads_accessory_list_and_reserve_on_sdc_models(self):
        for model in RESERVE_MODELS:
            with self.subTest(model=model):
                self.reset_device(model)
                self.client.values[duml.ACCESSORIES_KEY] = ACCESSORIES
                await self.device._refresh_accessory_config()
                self.assertEqual(
                    [payload for _, payload in self.client.requests],
                    [
                        b"\x00\x0a\x10",
                        b"\x00\x0d\x10",
                        b"\x00\x04\x10",
                        b"\x00\x06\x10",
                    ],
                )
                self.assertEqual(
                    self.device.data["accessories"],
                    [{"type": 4, "firmware": "00.00.03.20"}],
                )
                self.assertIs(self.device.data["energy_reserve_available"], True)
                self.assertIs(self.device.data["energy_reserve_enabled"], False)

    async def test_failed_optional_reads_are_isolated(self):
        self.client.values[duml.ACCESSORIES_KEY] = ACCESSORIES
        await self.device._refresh_accessory_config()
        self.client.values.pop(duml.ACCESSORIES_KEY)
        self.client.values.pop(duml.ENERGY_STORAGE_KEY)
        await self.device._refresh_accessory_config()
        self.assertIsNone(self.device.data["accessories"])
        self.assertIsNone(self.device.data["energy_reserve_enabled"])
        self.assertTrue(self.device.data["car_chargers"])
        self.assertTrue(self.device.data["power_switches"])

    def test_malformed_push_invalidates_accessories_and_reserve(self):
        self.device.data.update(
            accessories=[{"type": 4, "firmware": "00.00.03.20"}],
            key_04="old",
            energy_reserve_available=True,
            energy_reserve_enabled=True,
            key_06="old",
        )
        self.client.send(duml.TELEMETRY_COMMAND, b"\x0a\x10\x41\x00\x01")
        for key in (
            "accessories", "key_04", "energy_reserve_available",
            "energy_reserve_enabled", "key_06",
        ):
            self.assertIsNone(self.device.data[key], key)
