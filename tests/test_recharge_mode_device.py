"""Synthetic recharge speed transactions; no hardware acceptance is implied.

The speed list is the one a Power 1000 Mini reported (500 W and 1000 W).
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from tests.test_accessory_device import AccessoryClient
from tests.test_device import FakeBleDevice, device_module, duml

# The last byte of each speed is its switch: 1 on the selected one, 2 otherwise.
SLOW = bytes.fromhex("1f10060001f4010000011f10060002e803000002")
FAST = bytes.fromhex("1f10060001f4010000021f10060002e803000001")
READ = (duml.GET_COMMAND, bytes.fromhex("001e10"))
CLIENT_RULES = bytes.fromhex("0c00") + b"1e00efffff3f"


class RechargeModeDeviceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.device = device_module.DjiPowerDevice(
            FakeBleDevice(), "ab" * 16, name="Station", model="DJI Power 1000 Mini"
        )
        self.client = AccessoryClient(self.device)
        self.client.values = {0x1E: SLOW}
        self.device._client = self.client
        self.device._write_characteristic = object()
        retry = patch.object(device_module, "READBACK_RETRY_INTERVAL", 0)
        retry.start()
        self.addCleanup(retry.stop)

    def commands(self):
        return [command for command, _ in self.client.requests]

    async def test_selection_uses_fresh_get_set_ack_and_targeted_readback(self):
        for watts, expected in ((1000, FAST), (500, SLOW)):
            with self.subTest(watts=watts):
                self.client.requests.clear()
                await self.device.set_recharge_mode(watts)
                read, write, verify = self.client.requests
                self.assertEqual(read, READ)
                self.assertEqual(verify, READ)
                self.assertEqual(write[0], duml.SET_COMMAND)
                self.assertEqual(
                    duml.parse_keyed_values(write[1]),
                    {0x1E: expected, 0x0E: CLIENT_RULES},
                )
                self.assertEqual(self.device.data["key_1e"], expected.hex())

    async def test_selected_speed_is_not_written_again(self):
        await self.device.set_recharge_mode(500)
        self.assertEqual(self.client.requests, [READ])

    async def test_unoffered_speed_is_refused_before_writing(self):
        with self.assertRaisesRegex(device_module.DjiPowerError, "not offered"):
            await self.device.set_recharge_mode(750)
        self.assertEqual(self.client.requests, [READ])

    async def test_missing_or_ambiguous_list_is_refused_before_writing(self):
        for name, value, status in (
            ("no such key", None, 2),
            ("one speed", SLOW[:10], 0),
            ("none selected", SLOW[:9] + b"\x02" + SLOW[10:], 0),
            ("two selected", SLOW[:19] + b"\x01", 0),
            ("unknown child tag", SLOW.replace(b"\x1f\x10", b"\x1e\x00"), 0),
        ):
            with self.subTest(name):
                self.client.requests.clear()
                self.client.values = {} if value is None else {0x1E: value}
                self.client.get_status = status.to_bytes(4, "little")
                with self.assertRaisesRegex(
                    device_module.DjiPowerError, "not offered"
                ):
                    await self.device.set_recharge_mode(1000)
                self.assertEqual(self.client.requests, [READ])

    async def test_rejected_write_fails_without_readback(self):
        for ack in (
            {0x1E: (1).to_bytes(4, "little"), 0x0E: bytes(4)},
            {0x1E: bytes(4), 0x0E: (1).to_bytes(4, "little")},
            {0x1E: bytes(4)},
        ):
            with self.subTest(ack=ack):
                self.client.requests.clear()
                self.client.ack_override = ack
                with self.assertRaises(device_module.DjiPowerError):
                    await self.device.set_recharge_mode(1000)
                self.assertEqual(
                    self.commands(), [duml.GET_COMMAND, duml.SET_COMMAND]
                )
                self.assertEqual(self.device.data["key_1e"], SLOW.hex())

    async def test_unconfirmed_selection_fails_after_bounded_readback(self):
        self.client.apply_set = False
        with self.assertRaisesRegex(device_module.DjiPowerError, "did not report"):
            await self.device.set_recharge_mode(1000)
        self.assertEqual(
            self.commands(),
            [duml.GET_COMMAND, duml.SET_COMMAND]
            + [duml.GET_COMMAND] * (device_module.READBACK_RETRIES + 1),
        )
        self.assertEqual(self.device.data["key_1e"], SLOW.hex())


if __name__ == "__main__":
    unittest.main()
