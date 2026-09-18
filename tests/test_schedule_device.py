"""Offline schedule transactions; these do not emulate Power 2000 firmware."""

from __future__ import annotations

import asyncio
import struct
import unittest
from unittest.mock import AsyncMock, call, patch

from tests.test_device import FakeBleDevice, StationClient, device_module, duml
from tests.test_duml import SYNTHETIC_ECO_MODE

PEAK = {"type": "peak", "start": "17:00", "end": "20:00"}
OFF_PEAK = {"type": "off_peak", "start": "00:30", "end": "05:30"}
# Independent synthetic readback fixture. The app reads rows by their enclosing
# schema, without depending on each child's tag value.
PEAK_VALUE = struct.pack("<HHBBIBBBB", 0x1017, 10, 1, 1, 127, 17, 0, 20, 0)


class TimePeriodsClient(StationClient):
    """Reply to actual encoded GET/SET packets with synthetic keyed data."""

    def __init__(self, device):
        super().__init__(device, encrypted=False)
        self.values = {0x16: PEAK_VALUE, 0x18: SYNTHETIC_ECO_MODE}
        self.ack = {0x16: bytes(4), 0x0E: bytes(4)}
        self.apply_set = True
        self.did_set = False
        self.omit_after_set = False
        self.invalid_after_set = False

    async def write_gatt_char(self, uuid, value, *, response):  # noqa: ARG002
        request = duml.DumlPacket.decode(value)
        self.requests.append((request.command_id, request.payload))
        if request.command_id == duml.GET_COMMAND:
            key = request.payload[1]
            entries = []
            if key in self.values and not (self.did_set and self.omit_after_set):
                current = self.values[key]
                if key == 0x16 and self.did_set and self.invalid_after_set:
                    current = b"\x16\x00\x0a\x00\x01"
                entries = [(key, current)]
            reply = bytes(4) + duml.build_keyed_set_payload(entries, timestamp_ms=1)
        elif request.command_id == duml.SET_COMMAND:
            requested = duml.parse_keyed_values(request.payload)
            assert set(requested) == {0x16, 0x0E}
            assert requested[0x0E] == b"\x0a\x00" + b"1800efffff"
            self.did_set = True
            if self.apply_set and self.ack == {0x16: bytes(4), 0x0E: bytes(4)}:
                self.values.update(requested)
            reply = duml.build_keyed_set_payload(self.ack.items(), timestamp_ms=1)
        else:
            raise AssertionError(f"unexpected command {request.command_id}")
        self.send(request.command_id, reply, sequence=request.sequence, flags=0x80)


class TimePeriodsDeviceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.device = device_module.DjiPowerDevice(
            FakeBleDevice(), "ab" * 16, name="Station", model="DJI Power 2000"
        )
        self.client = TimePeriodsClient(self.device)
        self.device._client = self.client
        self.device._write_characteristic = object()
        sleeper = patch.object(device_module.asyncio, "sleep", new_callable=AsyncMock)
        self.sleep = sleeper.start()
        self.addCleanup(sleeper.stop)

    def set_mode(self, mode):
        eco = bytearray(SYNTHETIC_ECO_MODE)
        eco[1] = mode
        self.client.values[0x18] = bytes(eco)

    async def test_other_models_reject_writes_before_any_ble_request(self):
        for model in (
            "DJI Power 1000",
            "DJI Power 1000 V2",
            "DJI Power 1000 Mini",
            "DJI Power 500",
            "DJI Power",
            "DJI Power (0xFF)",
        ):
            with self.subTest(model=model):
                self.device.model = model
                with self.assertRaisesRegex(device_module.DjiPowerError, "Power 2000"):
                    await self.device.set_time_periods([OFF_PEAK])
                self.assertEqual(self.client.requests, [])

    async def test_only_supported_model_requests_optional_schedule(self):
        for model in ("DJI Power 2000", "DJI Power 1000 V2", "DJI Power 1000 Mini"):
            with self.subTest(model=model):
                self.device.model = model
                self.client.requests.clear()
                await self.device.refresh_config()
                schedule_get = (duml.GET_COMMAND, b"\x00\x16\x10")
                self.assertEqual(
                    schedule_get in self.client.requests, model == "DJI Power 2000"
                )

    async def test_editor_read_returns_fresh_independent_schedule(self):
        self.device.data["time_periods"] = duml.normalize_time_periods([OFF_PEAK])

        periods = await self.device.get_time_periods()

        self.assertEqual(periods, duml.normalize_time_periods([PEAK]))
        self.assertEqual(
            self.client.requests, [(duml.GET_COMMAND, b"\x00\x16\x10")]
        )
        periods[0]["days"].clear()
        periods[0]["start"] = "01:00"
        self.assertEqual(
            self.device.data["time_periods"], duml.normalize_time_periods([PEAK])
        )

    async def test_editor_read_accepts_explicit_empty_schedule(self):
        self.client.values[0x16] = b""
        self.assertEqual(await self.device.get_time_periods(), [])

    async def test_editor_read_rejects_unsupported_model_before_io(self):
        self.device.model = "DJI Power 1000 V2"
        with self.assertRaisesRegex(device_module.DjiPowerError, "Power 2000"):
            await self.device.get_time_periods()
        self.assertEqual(self.client.requests, [])

    async def test_editor_read_does_not_reuse_missing_or_invalid_schedule(self):
        for value in (None, b"bad"):
            with self.subTest(value=value):
                self.device.data["time_periods"] = duml.normalize_time_periods([PEAK])
                if value is None:
                    self.client.values.pop(0x16, None)
                else:
                    self.client.values[0x16] = value
                with self.assertRaises(device_module.DjiPowerError):
                    await self.device.get_time_periods()
                self.assertIsNone(self.device.data["time_periods"])
                self.assertFalse(self.device._operation_lock.locked())

    async def test_stale_editor_snapshot_rejects_write_without_set(self):
        self.device.data["time_periods"] = []
        with self.assertRaises(device_module.DjiPowerScheduleChangedError):
            await self.device.set_time_periods([OFF_PEAK], expected_periods=[])
        self.assertEqual(
            [cmd for cmd, _ in self.client.requests],
            [duml.GET_COMMAND, duml.GET_COMMAND],
        )
        self.assertFalse(self.client.did_set)
        self.assertEqual(
            self.device.data["time_periods"], duml.normalize_time_periods([PEAK])
        )

    async def test_stale_snapshot_can_retry_already_applied_schedule(self):
        await self.device.set_time_periods([PEAK], expected_periods=[OFF_PEAK])
        self.assertFalse(self.client.did_set)
        self.assertEqual(
            [cmd for cmd, _ in self.client.requests],
            [duml.GET_COMMAND, duml.GET_COMMAND],
        )

    async def test_stale_snapshot_noop_still_checks_empty_schedule_mode(self):
        self.client.values[0x16] = b""
        self.set_mode(2)
        with self.assertRaises(device_module.DjiPowerError):
            await self.device.set_time_periods([], expected_periods=[PEAK])
        self.assertFalse(self.client.did_set)

    async def test_expected_snapshot_normalizes_period_and_day_order(self):
        schedule = [PEAK, OFF_PEAK]
        payload = duml.build_time_periods_set_payload(schedule)
        self.client.values[0x16] = duml.parse_keyed_values(payload)[0x16]
        expected = [
            {**OFF_PEAK, "days": list(reversed(duml.TIME_PERIOD_DAYS))}, PEAK
        ]

        await self.device.set_time_periods([OFF_PEAK], expected_periods=expected)

        self.assertTrue(self.client.did_set)
        self.assertEqual(
            self.device.data["time_periods"], duml.normalize_time_periods([OFF_PEAK])
        )

    async def test_invalid_expected_snapshot_is_rejected_before_reading(self):
        with self.assertRaises(device_module.DjiPowerError):
            await self.device.set_time_periods([OFF_PEAK], expected_periods="invalid")
        self.assertEqual(self.client.requests, [])

    async def test_fresh_state_write_ack_and_immediate_matching_readback(self):
        self.device.data["time_periods"] = duml.normalize_time_periods([OFF_PEAK])
        original_eco = self.client.values[0x18]
        self.device.data["timezone_offset_min"] = 60
        await self.device.set_time_periods([OFF_PEAK])

        self.assertEqual(
            [cmd for cmd, _ in self.client.requests],
            [duml.GET_COMMAND, duml.GET_COMMAND, duml.SET_COMMAND, duml.GET_COMMAND],
        )
        self.assertEqual(
            [body for cmd, body in self.client.requests if cmd == duml.GET_COMMAND],
            [b"\x00\x16\x10", b"\x00\x18\x10", b"\x00\x16\x10"],
        )
        self.assertEqual(
            self.device.data["time_periods"], duml.normalize_time_periods([OFF_PEAK])
        )
        self.assertEqual(self.client.values[0x18], original_eco)
        self.assertEqual(self.device.data["timezone_offset_min"], 60)
        self.sleep.assert_not_awaited()

    async def test_identical_schedule_skips_set_after_fresh_reads(self):
        await self.device.set_time_periods([PEAK])
        self.assertEqual(
            [cmd for cmd, _ in self.client.requests],
            [duml.GET_COMMAND, duml.GET_COMMAND],
        )
        self.assertFalse(self.client.did_set)

    async def test_invalid_request_is_rejected_before_reading(self):
        with self.assertRaises(device_module.DjiPowerError):
            await self.device.set_time_periods([{**OFF_PEAK, "end": "00:30"}])
        self.assertEqual(self.client.requests, [])

    async def test_nonempty_schedule_does_not_require_manual_tou(self):
        for mode in (0, 1, 2, 3):
            with self.subTest(mode=mode):
                self.set_mode(mode)
                self.client.values[0x16] = PEAK_VALUE
                await self.device.set_time_periods([OFF_PEAK])
                self.assertEqual(self.client.values[0x18][1], mode)

    async def test_clearing_requires_fresh_inactive_mode(self):
        self.device.data["key_18"] = bytes(86).hex()
        for mode in (2, 3):
            with self.subTest(mode=mode):
                self.set_mode(mode)
                self.client.requests.clear()
                with self.assertRaises(device_module.DjiPowerError):
                    await self.device.set_time_periods([])
                self.assertEqual(
                    [cmd for cmd, _ in self.client.requests],
                    [duml.GET_COMMAND, duml.GET_COMMAND],
                )
        for mode in (0, 1):
            with self.subTest(mode=mode):
                self.set_mode(mode)
                self.client.values[0x16] = PEAK_VALUE
                await self.device.set_time_periods([])
                self.assertEqual(self.device.data["time_periods"], [])
                self.assertEqual(self.client.values[0x16], b"")

    async def test_missing_or_invalid_fresh_settings_prevent_set(self):
        for key, value in (
            (0x16, None),
            (0x16, b"bad"),
            (0x18, None),
            (0x18, b"\x01\x02"),
            (0x18, bytes([1, 9]) + bytes(84)),
        ):
            with self.subTest(key=key, value=value):
                self.client.values = {0x16: PEAK_VALUE, 0x18: SYNTHETIC_ECO_MODE}
                if value is None:
                    self.client.values.pop(key)
                else:
                    self.client.values[key] = value
                self.client.requests.clear()
                with self.assertRaises(device_module.DjiPowerError):
                    await self.device.set_time_periods([OFF_PEAK])
                self.assertNotIn(
                    duml.SET_COMMAND, [cmd for cmd, _ in self.client.requests]
                )

    async def test_optional_failures_clear_stale_schedule_without_breaking_refresh(
        self,
    ):
        await self.device.refresh_config()
        self.assertIsInstance(self.device.data["time_periods"], list)
        self.client.values.pop(0x16)
        await self.device.refresh_config()
        self.assertIsNone(self.device.data["time_periods"])
        self.assertIsNone(self.device.data["key_16"])
        self.assertTrue(self.device.data["discharge_power_available"])

    async def test_optional_timeout_is_tolerated_and_disconnect_propagates(self):
        original = self.device._read_config
        for error in (
            device_module.DjiPowerError("timeout"),
            device_module.DjiPowerDisconnectedError("disconnected"),
        ):

            async def read(key, error=error):
                if key == 0x16:
                    raise error
                return await original(key)

            with (
                self.subTest(error=error),
                patch.object(self.device, "_read_config", side_effect=read),
            ):
                if isinstance(error, device_module.DjiPowerDisconnectedError):
                    with self.assertRaises(type(error)):
                        await self.device.refresh_config()
                else:
                    await self.device.refresh_config()
                    self.assertIsNone(self.device.data.get("time_periods"))

    async def test_ack_must_include_success_for_schedule_and_rules_keys(self):
        for ack in (
            {},
            {0x18: bytes(4)},
            {0x16: bytes(4)},
            {0x0E: bytes(4)},
            {0x16: b"\x01\x00\x00\x00", 0x0E: bytes(4)},
            {0x16: bytes(4), 0x0E: b"\x01\x00\x00\x00"},
            {0x16: b"\x00", 0x0E: bytes(4)},
        ):
            with self.subTest(ack=ack):
                self.client.ack = ack
                self.client.requests.clear()
                with self.assertRaises(device_module.DjiPowerError):
                    await self.device.set_time_periods([OFF_PEAK])
                self.assertEqual(len(self.client.requests), 3)
                self.assertEqual(
                    self.device.data["time_periods"],
                    duml.normalize_time_periods([PEAK]),
                )

    async def test_missing_or_malformed_readback_cannot_confirm_cached_value(self):
        for field in ("omit_after_set", "invalid_after_set"):
            with self.subTest(field=field):
                self.client.did_set = False
                self.client.omit_after_set = False
                self.client.invalid_after_set = False
                setattr(self.client, field, True)
                self.client.values[0x16] = PEAK_VALUE
                with self.assertRaises(device_module.DjiPowerError):
                    await self.device.set_time_periods([OFF_PEAK])
                self.assertIsNone(self.device.data["time_periods"])

    async def test_stale_readback_retries_then_confirms(self):
        self.client.apply_set = False

        async def settle(delay):
            self.assertEqual(delay, 2)
            self.assertEqual(len(self.client.requests), 4)
            requested = duml.parse_keyed_values(self.client.requests[2][1])
            self.client.values.update(requested)

        self.sleep.side_effect = settle
        await self.device.set_time_periods([OFF_PEAK])
        self.sleep.assert_awaited_once_with(2)
        self.assertEqual(
            self.device.data["time_periods"], duml.normalize_time_periods([OFF_PEAK])
        )

    async def test_ack_without_matching_readback_does_not_publish_requested_state(self):
        self.client.apply_set = False
        with self.assertRaisesRegex(device_module.DjiPowerError, "did not report"):
            await self.device.set_time_periods([OFF_PEAK])
        self.assertEqual(self.sleep.await_args_list, [call(2)] * 8)
        self.assertEqual(
            self.device.data["time_periods"], duml.normalize_time_periods([PEAK])
        )

    async def test_cancelled_write_releases_operation_lock_and_pending_request(self):
        reading = asyncio.Event()
        original = self.client.write_gatt_char

        async def hold(*args, **kwargs):
            if self.client.did_set:
                reading.set()
                await asyncio.Event().wait()
            await original(*args, **kwargs)

        self.client.write_gatt_char = hold
        task = asyncio.create_task(self.device.set_time_periods([OFF_PEAK]))
        await reading.wait()
        self.assertTrue(self.device._operation_lock.locked())
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.device._operation_lock.locked())
        self.assertEqual(self.device._pending, {})

    async def test_queued_schedule_waits_for_first_confirmation(self):
        reading = asyncio.Event()
        release = asyncio.Event()
        queued = asyncio.Event()
        original = self.client.write_gatt_char

        async def hold_first_readback(*args, **kwargs):
            if len(self.client.requests) == 3:
                reading.set()
                await release.wait()
            await original(*args, **kwargs)

        async def replace_again():
            queued.set()
            await self.device.set_time_periods([PEAK])

        self.client.write_gatt_char = hold_first_readback
        first = asyncio.create_task(self.device.set_time_periods([OFF_PEAK]))
        await reading.wait()
        second = asyncio.create_task(replace_again())
        await queued.wait()
        self.assertFalse(first.done())
        self.assertFalse(second.done())
        self.assertEqual(len(self.client.requests), 3)
        release.set()
        await asyncio.gather(first, second)
        self.assertEqual(
            [cmd for cmd, _ in self.client.requests],
            [duml.GET_COMMAND, duml.GET_COMMAND, duml.SET_COMMAND, duml.GET_COMMAND]
            * 2,
        )
        self.assertEqual(
            self.device.data["time_periods"], duml.normalize_time_periods([PEAK])
        )
        self.sleep.assert_not_awaited()

    async def test_editor_read_waits_for_pending_write_confirmation(self):
        reading = asyncio.Event()
        release = asyncio.Event()
        queued = asyncio.Event()
        original = self.client.write_gatt_char

        async def hold_first_readback(*args, **kwargs):
            if len(self.client.requests) == 3:
                reading.set()
                await release.wait()
            await original(*args, **kwargs)

        async def read_for_editor():
            queued.set()
            return await self.device.get_time_periods()

        self.client.write_gatt_char = hold_first_readback
        writer = asyncio.create_task(self.device.set_time_periods([OFF_PEAK]))
        await reading.wait()
        reader = asyncio.create_task(read_for_editor())
        await queued.wait()
        self.assertFalse(reader.done())
        self.assertEqual(len(self.client.requests), 3)
        release.set()
        await writer
        self.assertEqual(await reader, duml.normalize_time_periods([OFF_PEAK]))
        self.assertEqual(len(self.client.requests), 5)

    async def test_queued_editor_write_cannot_overwrite_first_write(self):
        reading = asyncio.Event()
        release = asyncio.Event()
        queued = asyncio.Event()
        original = self.client.write_gatt_char

        async def hold_first_readback(*args, **kwargs):
            if len(self.client.requests) == 3:
                reading.set()
                await release.wait()
            await original(*args, **kwargs)

        async def stale_replace():
            queued.set()
            await self.device.set_time_periods([PEAK], expected_periods=[PEAK])

        self.client.write_gatt_char = hold_first_readback
        first = asyncio.create_task(self.device.set_time_periods([OFF_PEAK]))
        await reading.wait()
        second = asyncio.create_task(stale_replace())
        await queued.wait()
        self.assertFalse(second.done())
        release.set()
        await first
        with self.assertRaises(device_module.DjiPowerScheduleChangedError):
            await second
        self.assertEqual(
            sum(cmd == duml.SET_COMMAND for cmd, _ in self.client.requests), 1
        )
        self.assertEqual(
            self.device.data["time_periods"], duml.normalize_time_periods([OFF_PEAK])
        )

    async def test_expected_snapshot_is_copied_before_waiting_for_lock(self):
        expected = [dict(PEAK)]
        queued = asyncio.Event()

        async def replace():
            queued.set()
            await self.device.set_time_periods([OFF_PEAK], expected_periods=expected)

        async with self.device._operation_lock:
            task = asyncio.create_task(replace())
            await queued.wait()
            expected.clear()
        await task
        self.assertTrue(self.client.did_set)

    async def test_push_absence_preserves_schedule_but_invalid_data_clears_it(self):
        await self.device.refresh_config()
        self.client.send(
            duml.TELEMETRY_COMMAND, duml.build_keyed_set_payload([(0x15, bytes(2))])
        )
        self.assertIsInstance(self.device.data["time_periods"], list)
        for value in (b"bad", b""):
            self.client.send(
                duml.TELEMETRY_COMMAND, duml.build_keyed_set_payload([(0x16, value)])
            )
            self.assertEqual(self.device.data["time_periods"], None if value else [])
        self.client.send(duml.TELEMETRY_COMMAND, b"\x16\x10\x0a\x00\x01")
        self.assertIsNone(self.device.data["time_periods"])
