"""Synthetic accessory transactions; no hardware acceptance is implied."""

from __future__ import annotations

import asyncio
import struct
import unittest
from unittest.mock import AsyncMock, patch

from tests.test_device import FakeBleDevice, StationClient, device_module, duml


def charger_value(*, seq=1, watts=400, mode=2, enabled=1):
    """Independently encode a reported 1.8 kW charger, including its bounds."""
    row = struct.pack(
        "<5B15I", 5, seq, 4, enabled, mode,
        600, 100, watts, 600, 100, 300,
        1400, 1150, 1250, 1440, 1100, 1350, 1400, 1150, 1350,
    )
    return struct.pack("<HH", 0x1012, len(row)) + row


SWITCHES = b"".join(
    struct.pack("<HHBBB", 0x1014, 3, kind, seq, state)
    for kind, seq, state in ((2, 1, 1), (5, 1, 1), (6, 2, 2))
)
# AC plus USB-A1, USB-A2, USB-C1 and USB-C2, as the app's USB sheet addresses them.
USB_SWITCHES = b"".join(
    struct.pack("<HHBBB", 0x1014, 3, kind, seq, state)
    for kind, seq, state in ((2, 1, 1), (3, 1, 1), (3, 2, 2), (4, 1, 1), (4, 2, 1))
)


class AccessoryClient(StationClient):
    """Use real packet framing around synthetic accessory property snapshots."""

    def __init__(self, device):
        super().__init__(device, encrypted=device.model == "DJI Power 1000")
        self.values = {0x0A: charger_value(), 0x0D: SWITCHES}
        self.ack_override = None
        self.apply_set = True
        self.did_set = False
        self.readback = None

    async def write_gatt_char(self, uuid, value, *, response):  # noqa: ARG002
        packet = duml.DumlPacket.decode(value)
        self.wire_requests.append(packet)
        payload = self.device._decode_payload(packet)
        self.requests.append((packet.command_id, payload))
        if packet.command_id == duml.GET_COMMAND:
            key = payload[1]
            current = self.values.get(key)
            if self.did_set and self.readback is not None:
                current = self.readback(key, current)
            entries = [] if current is None else [(key, current)]
            reply = bytes(4) + duml.build_keyed_set_payload(entries, timestamp_ms=1)
        elif packet.command_id == duml.SET_COMMAND:
            requested = duml.parse_keyed_values(payload)
            ack = {key: bytes(4) for key in requested}
            if self.ack_override is not None:
                ack = self.ack_override
            self.did_set = True
            if self.apply_set and all(ack.get(k) == bytes(4) for k in requested):
                self.values.update(requested)
            reply = duml.build_keyed_set_payload(ack.items(), timestamp_ms=1)
        else:
            raise AssertionError(f"unexpected command {packet.command_id}")
        self.send(packet.command_id, reply, sequence=packet.sequence, flags=0x80)


class AccessoryDeviceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.reset_device()
        retry = patch.object(device_module, "READBACK_RETRY_INTERVAL", 0)
        retry.start()
        self.addCleanup(retry.stop)

    def reset_device(self, model="DJI Power 2000"):
        self.device = device_module.DjiPowerDevice(
            FakeBleDevice(), "ab" * 16, name="Station", model=model
        )
        self.client = AccessoryClient(self.device)
        self.device._client = self.client
        self.device._write_characteristic = object()

    def car_row(self):
        return self.device.data["car_chargers"][0]

    async def test_controls_use_fresh_get_set_ack_and_immediate_targeted_readback(self):
        for kwargs, field, expected in (
            ({"enabled": False}, "sw", 2),
            ({"mode": 1}, "mode", 1),
            ({"recharge_power_w": 450}, "p_from_car_v", 450),
            ({"minimum_voltage_v": 12.75}, "v_from_car_v", 1275),
        ):
            with self.subTest(kwargs=kwargs):
                self.reset_device()
                with patch.object(device_module.asyncio, "sleep", AsyncMock()) as sleep:
                    await self.device.set_car_charger(5, 1, 4, **kwargs)
                sleep.assert_not_awaited()
                self.assertEqual(self.car_row()[field], expected)
                self.assertEqual(
                    [command for command, _ in self.client.requests],
                    [duml.GET_COMMAND, duml.SET_COMMAND, duml.GET_COMMAND],
                )
                self.assertEqual(self.client.requests[0][1], b"\x00\x0a\x10")
                self.assertEqual(self.client.requests[2][1], b"\x00\x0a\x10")

    async def test_all_supported_transports_handle_accessory_controls(self):
        for model in ("DJI Power 1000", "DJI Power 1000 V2", "DJI Power 2000"):
            with self.subTest(model=model):
                self.reset_device(model)
                await self.device.set_car_charger(5, 1, 4, recharge_power_w=475)
                await self.device.set_sdc(6, 2, True)
                self.assertEqual(self.car_row()["p_from_car_v"], 475)
                self.assertTrue(all(
                    packet.encryption_type == (6 if model == "DJI Power 1000" else 0)
                    for packet in self.client.wire_requests
                ))

    async def test_other_models_make_no_optional_reads_or_writes(self):
        for model in ("DJI Power 500", "DJI Power"):
            with self.subTest(model=model):
                self.reset_device(model)
                await self.device._refresh_accessory_config()
                with self.assertRaises(device_module.DjiPowerError):
                    await self.device.set_car_charger(5, 1, 4, enabled=True)
                with self.assertRaises(device_module.DjiPowerError):
                    await self.device.set_sdc(5, 1, True)
                with self.assertRaises(device_module.DjiPowerError):
                    await self.device.set_usb(3, 1, True)
                self.assertEqual(self.client.requests, [])

    async def test_usb_model_reads_only_switches_and_rejects_sdc_controls(self):
        self.reset_device("DJI Power 1000 Mini")
        await self.device._refresh_accessory_config()
        self.assertEqual(self.client.requests, [(duml.GET_COMMAND, b"\x00\x0d\x10")])
        self.assertNotIn("car_chargers", self.device.data)
        self.client.requests.clear()
        with self.assertRaises(device_module.DjiPowerError):
            await self.device.set_car_charger(5, 1, 4, enabled=True)
        with self.assertRaises(device_module.DjiPowerError):
            await self.device.set_sdc(5, 1, True)
        self.assertEqual(self.client.requests, [])

    async def test_sdc_models_reject_usb_controls_without_requests(self):
        for model in ("DJI Power 1000", "DJI Power 1000 V2", "DJI Power 2000"):
            with self.subTest(model=model):
                self.reset_device(model)
                self.client.values[0x0D] = USB_SWITCHES
                with self.assertRaises(device_module.DjiPowerError):
                    await self.device.set_usb(3, 1, False)
                self.assertEqual(self.client.requests, [])

    async def test_sdc_and_ac_updates_preserve_other_switches(self):
        await self.device.set_sdc(5, 1, False)
        self.assertTrue(self.device.data["ac_enabled"])
        await self.device.set_ac(False)
        self.assertEqual(
            self.device.data["power_switches"],
            [
                {"type": 2, "seq": 1, "sw": 2},
                {"type": 5, "seq": 1, "sw": 2},
                {"type": 6, "seq": 2, "sw": 2},
            ],
        )
        self.assertEqual(
            [command for command, _ in self.client.requests],
            [duml.GET_COMMAND, duml.SET_COMMAND, duml.GET_COMMAND] * 2,
        )

    async def test_write_uses_latest_bounds_and_mode_instead_of_cached_state(self):
        await self.device._refresh_accessory_config()
        for fresh in (charger_value(mode=3), charger_value(enabled=2), b"", b"bad"):
            with self.subTest(fresh=fresh[:5]):
                self.client.values[0x0A] = fresh
                self.client.requests.clear()
                with self.assertRaises(device_module.DjiPowerError):
                    await self.device.set_car_charger(5, 1, 4, recharge_power_w=450)
                self.assertEqual(
                    self.client.requests, [(duml.GET_COMMAND, b"\x00\x0a\x10")]
                )

    async def test_missing_requested_port_never_sends_a_write(self):
        for method, args, kwargs in (
            (self.device.set_sdc, (5, 2, True), {}),
            (self.device.set_car_charger, (5, 2, 4), {"enabled": True}),
        ):
            with self.subTest(method=method.__name__):
                self.client.requests.clear()
                with self.assertRaises(device_module.DjiPowerError):
                    await method(*args, **kwargs)
                self.assertEqual(len(self.client.requests), 1)

    async def test_replacement_charger_on_same_port_cannot_receive_old_control(self):
        await self.device._refresh_accessory_config()
        replacement = bytearray(charger_value())
        replacement[6] = 3  # Same port, a different supported accessory type.
        self.client.values[0x0A] = bytes(replacement)
        self.client.requests.clear()
        with self.assertRaisesRegex(device_module.DjiPowerError, "absent"):
            await self.device.set_car_charger(5, 1, 4, enabled=False)
        self.assertEqual(self.client.requests, [(duml.GET_COMMAND, b"\x00\x0a\x10")])
        self.assertEqual(self.car_row()["type"], 3)

    async def test_replacement_on_same_port_cannot_satisfy_write_confirmation(self):
        def replace(key, current):
            replacement = bytearray(current)
            replacement[6] = 3
            return bytes(replacement)

        self.client.readback = replace
        with self.assertRaisesRegex(device_module.DjiPowerError, "no longer available"):
            await self.device.set_car_charger(5, 1, 4, recharge_power_w=450)
        self.assertEqual(self.car_row()["type"], 3)
        self.assertEqual(self.car_row()["p_from_car_v"], 450)

    async def test_mode_change_rechecks_enabled_state_before_writing(self):
        await self.device._refresh_accessory_config()
        self.client.values[0x0A] = charger_value(enabled=2)
        self.client.requests.clear()
        with self.assertRaisesRegex(device_module.DjiPowerError, "enable the car"):
            await self.device.set_car_charger(5, 1, 4, mode=1)
        self.assertEqual(self.client.requests, [(duml.GET_COMMAND, b"\x00\x0a\x10")])

    async def test_missing_or_rejected_ack_cannot_confirm_control(self):
        for kind in ("car", "sdc"):
            for ack in (
                {}, {0x0A: b"\x01\x00\x00\x00"},
                {0x0A: bytes(4)}, {0x0D: bytes(4)},
            ):
                with self.subTest(kind=kind, ack=ack):
                    self.reset_device()
                    self.client.ack_override = ack
                    with self.assertRaises(device_module.DjiPowerError):
                        if kind == "car":
                            await self.device.set_car_charger(5, 1, 4, enabled=False)
                        else:
                            await self.device.set_sdc(5, 1, False)
                    self.assertEqual(len(self.client.requests), 2)

    async def test_success_ack_with_ineffective_setting_exhausts_readback_retries(self):
        self.client.apply_set = False
        with self.assertRaisesRegex(device_module.DjiPowerError, "did not report"):
            await self.device.set_car_charger(5, 1, 4, recharge_power_w=500)
        self.assertEqual(self.car_row()["p_from_car_v"], 400)
        self.assertEqual(len(self.client.requests), device_module.READBACK_RETRIES + 3)

    async def test_delayed_readback_retries_after_immediate_first_read(self):
        calls = 0

        def lagged(key, value):
            nonlocal calls
            calls += 1
            return charger_value() if calls == 1 else value

        self.client.readback = lagged
        with patch.object(device_module.asyncio, "sleep", AsyncMock()) as sleep:
            await self.device.set_car_charger(5, 1, 4, recharge_power_w=500)
        self.assertEqual(calls, 2)
        sleep.assert_awaited_once_with(0)

    async def test_fresh_omission_or_malformed_readback_invalidates_cached_rows(self):
        for result in (None, b"bad", b""):
            with self.subTest(result=result):
                self.reset_device()
                await self.device._refresh_accessory_config()
                self.client.readback = lambda key, value, result=result: result
                # Even an unchanged request must not succeed from a cached row.
                with self.assertRaises(device_module.DjiPowerError):
                    await self.device.set_car_charger(5, 1, 4, recharge_power_w=400)
                self.assertEqual(
                    self.device.data["car_chargers"], [] if result == b"" else None
                )

    async def test_missing_switch_readback_clears_state_without_affecting_charger(self):
        await self.device._refresh_accessory_config()
        self.client.readback = lambda key, value: None if key == 0x0D else value
        with self.assertRaises(device_module.DjiPowerError):
            await self.device.set_sdc(5, 1, False)
        self.assertIsNone(self.device.data["power_switches"])
        self.assertIsNone(self.device.data["ac_enabled"])
        self.assertIsNone(self.device.data["key_0d"])
        self.assertEqual(self.car_row()["sw"], 1)

    async def test_optional_failures_are_isolated_and_clear_previous_capabilities(self):
        await self.device._refresh_accessory_config()
        self.client.values.pop(0x0A)
        await self.device._refresh_accessory_config()
        self.assertIsNone(self.device.data["car_chargers"])
        self.assertIsNone(self.device.data["key_0a"])
        self.assertTrue(self.device.data["power_switches"])
        self.client.values[0x0A] = charger_value(seq=2)
        await self.device._refresh_accessory_config()
        self.assertEqual(self.car_row()["seq"], 2)

    async def test_disconnect_during_read_does_not_republish_cached_availability(self):
        states = []
        self.device.add_state_listener(states.append)
        with patch.object(
            self.device, "_read_config",
            side_effect=device_module.DjiPowerDisconnectedError("disconnected"),
        ), self.assertRaises(device_module.DjiPowerDisconnectedError):
            await self.device._refresh_accessory_config()
        self.assertEqual(states, [])

    async def test_stalled_optional_read_is_bounded_and_does_not_hold_the_lock(self):
        async def hang(key):
            await asyncio.Future()

        with patch.object(device_module, "DEFAULT_REQUEST_TIMEOUT", 0.01), patch.object(
            self.device, "_read_config", side_effect=hang
        ):
            async with asyncio.timeout(1), self.device._operation_lock:
                await self.device._refresh_accessory_config()
        self.assertFalse(self.device._operation_lock.locked())
        self.assertIsNone(self.device.data.get("car_chargers"))
        self.assertIsNone(self.device.data.get("power_switches"))

    async def test_queued_edits_re_read_and_preserve_the_first_confirmed_setting(self):
        reading = asyncio.Event()
        release = asyncio.Event()
        send = self.client.write_gatt_char

        async def hold_first_readback(*args, **kwargs):
            if len(self.client.requests) == 2:
                reading.set()
                await release.wait()
            await send(*args, **kwargs)

        self.client.write_gatt_char = hold_first_readback
        first = asyncio.create_task(
            self.device.set_car_charger(5, 1, 4, recharge_power_w=500)
        )
        await reading.wait()
        second = asyncio.create_task(
            self.device.set_car_charger(5, 1, 4, minimum_voltage_v=12.75)
        )
        await asyncio.sleep(0)
        self.assertFalse(second.done())
        self.assertEqual(len(self.client.requests), 2)
        release.set()
        await asyncio.gather(first, second)
        self.assertEqual(self.car_row()["p_from_car_v"], 500)
        self.assertEqual(self.car_row()["v_from_car_v"], 1275)

    async def test_background_discovery_precedes_sleep_and_refreshes_each_interval(
        self,
    ):
        calls = []

        async def accessory():
            self.assertTrue(self.device._operation_lock.locked())
            calls.append("accessories")
            if calls.count("accessories") == 2:
                self.client.is_connected = False

        async def packs():
            self.assertTrue(self.device._operation_lock.locked())
            calls.append("packs")

        async def sleep(interval):
            self.assertFalse(self.device._operation_lock.locked())
            self.assertEqual(interval, 30)
            calls.append("sleep")

        with (
            patch.object(self.device, "_refresh_accessory_config", accessory),
            patch.object(self.device, "_read_expansion_batteries", packs),
            patch.object(device_module.asyncio, "sleep", sleep),
        ):
            await self.device._refresh_expansion_loop()
        self.assertEqual(calls, ["accessories", "sleep", "packs", "accessories"])

    def test_malformed_push_invalidates_accessories_and_clears_stale_controls(self):
        self.device.data.update(car_chargers=[{}], power_switches=[{}], key_0a="old")
        self.client.send(duml.TELEMETRY_COMMAND, b"\x0a\x10\x41\x00\x01")
        self.assertIsNone(self.device.data["car_chargers"])
        self.assertIsNone(self.device.data["power_switches"])
        self.assertIsNone(self.device.data["key_0a"])


class UsbSwitchDeviceTests(unittest.IsolatedAsyncioTestCase):
    """Synthetic Power 1000 Mini USB switch transactions; no hardware implied."""

    def setUp(self):
        self.device = device_module.DjiPowerDevice(
            FakeBleDevice(), "ab" * 16, name="Station", model="DJI Power 1000 Mini"
        )
        self.client = AccessoryClient(self.device)
        self.client.values = {0x0D: USB_SWITCHES}
        self.device._client = self.client
        self.device._write_characteristic = object()
        retry = patch.object(device_module, "READBACK_RETRY_INTERVAL", 0)
        retry.start()
        self.addCleanup(retry.stop)

    def switches(self):
        return {
            (row["type"], row["seq"]): row["sw"]
            for row in self.device.data["power_switches"]
        }

    async def test_each_usb_port_uses_fresh_get_set_ack_and_targeted_readback(self):
        for interface_type, seq in ((3, 1), (3, 2), (4, 1), (4, 2)):
            for enabled in (False, True):
                with self.subTest(port=(interface_type, seq), enabled=enabled):
                    self.client.requests.clear()
                    before = self.switches() if self.device.data else None
                    with patch.object(
                        device_module.asyncio, "sleep", AsyncMock()
                    ) as sleep:
                        await self.device.set_usb(interface_type, seq, enabled)
                    sleep.assert_not_awaited()
                    self.assertEqual(
                        self.switches()[(interface_type, seq)], 1 if enabled else 2
                    )
                    if before is not None:
                        before.pop((interface_type, seq))
                        after = self.switches()
                        after.pop((interface_type, seq))
                        self.assertEqual(after, before)
                    self.assertEqual(
                        [command for command, _ in self.client.requests],
                        [duml.GET_COMMAND, duml.SET_COMMAND, duml.GET_COMMAND],
                    )
                    self.assertEqual(self.client.requests[0][1], b"\x00\x0d\x10")
                    self.assertEqual(self.client.requests[2][1], b"\x00\x0d\x10")
                    written = duml.parse_keyed_values(self.client.requests[1][1])
                    self.assertEqual(set(written), {0x0D, 0x0E})
                    self.assertEqual(
                        written[0x0E], bytes.fromhex("0a00") + b"1800efffff"
                    )

    async def test_usb_write_preserves_ac_and_other_usb_rows(self):
        await self.device.set_usb(3, 1, False)
        self.assertTrue(self.device.data["ac_enabled"])
        self.assertEqual(
            self.device.data["power_switches"],
            [
                {"type": 2, "seq": 1, "sw": 1},
                {"type": 3, "seq": 1, "sw": 2},
                {"type": 3, "seq": 2, "sw": 2},
                {"type": 4, "seq": 1, "sw": 1},
                {"type": 4, "seq": 2, "sw": 1},
            ],
        )

    async def test_unreported_or_non_usb_port_never_sends_a_write(self):
        for interface_type, seq in ((3, 3), (4, 0), (2, 1), (5, 1)):
            with self.subTest(port=(interface_type, seq)):
                self.client.requests.clear()
                with self.assertRaises(device_module.DjiPowerError):
                    await self.device.set_usb(interface_type, seq, True)
                self.assertEqual(
                    self.client.requests, [(duml.GET_COMMAND, b"\x00\x0d\x10")]
                )

    async def test_missing_or_rejected_ack_cannot_confirm_usb_switch(self):
        for ack in ({}, {0x0D: b"\x01\x00\x00\x00"}, {0x0D: bytes(4)}):
            with self.subTest(ack=ack):
                self.client.requests.clear()
                self.client.ack_override = ack
                with self.assertRaises(device_module.DjiPowerError):
                    await self.device.set_usb(4, 2, False)
                self.assertEqual(len(self.client.requests), 2)

    async def test_ineffective_usb_write_exhausts_readback_retries(self):
        self.client.apply_set = False
        with self.assertRaisesRegex(device_module.DjiPowerError, "did not report"):
            await self.device.set_usb(4, 1, False)
        self.assertEqual(self.switches()[(4, 1)], 1)
        self.assertEqual(len(self.client.requests), device_module.READBACK_RETRIES + 3)

    async def test_failed_switch_refresh_clears_usb_and_ac_state(self):
        await self.device._refresh_accessory_config()
        self.assertEqual(len(self.device.data["power_switches"]), 5)
        self.client.values.pop(0x0D)
        await self.device._refresh_accessory_config()
        self.assertIsNone(self.device.data["power_switches"])
        self.assertIsNone(self.device.data["ac_enabled"])
        self.assertIsNone(self.device.data["key_0d"])

    def test_malformed_push_invalidates_usb_and_ac_state(self):
        self.device.data.update(power_switches=[{}], key_0d="old", ac_enabled=True)
        self.client.send(duml.TELEMETRY_COMMAND, b"\x0d\x10\x41\x00\x01")
        self.assertIsNone(self.device.data["power_switches"])
        self.assertIsNone(self.device.data["key_0d"])
        self.assertIsNone(self.device.data["ac_enabled"])
        self.assertNotIn("car_chargers", self.device.data)
