"""Offline settings report ACK framing, concurrency, and connection lifecycle."""

from __future__ import annotations

import asyncio
import types
import unittest
from unittest.mock import AsyncMock, patch

from tests.test_device import BleakError, FakeBleDevice, device_module, duml


class SettingsReportAckTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.devices = []

    async def asyncTearDown(self):
        for device in self.devices:
            await device.disconnect()

    def station(self, model="DJI Power 2000"):
        device = device_module.DjiPowerDevice(
            FakeBleDevice(), "ab" * 16, name="Test station", model=model
        )
        client = types.SimpleNamespace(
            is_connected=True,
            write_gatt_char=AsyncMock(),
            disconnect=AsyncMock(),
        )
        device._client = client
        device._write_characteristic = object()
        self.devices.append(device)
        return device, client

    @staticmethod
    def report(*, flags=0x40, destination=2, command_set=0x5A, command_id=0x62):
        payload = duml.build_keyed_set_payload(
            [(0x15, (60).to_bytes(2, "little"))], timestamp_ms=1
        )
        if flags & 7 == duml.POWER_1000_ENCRYPTION_TYPE:
            payload = duml.encrypt_power_1000_payload(payload)
        return duml.DumlPacket(
            0xAB, destination, 0x1234, flags, command_set, command_id, payload
        )

    async def drain(self, device):
        await asyncio.gather(*tuple(device._settings_ack_tasks))
        self.assertFalse(device._settings_ack_tasks)

    async def test_fragmented_reports_ack_with_model_cipher_and_preserve_state(self):
        for model in ("DJI Power 2000", "DJI Power 1000 V2", "DJI Power 1000"):
            for command_type in (0x20, 0x40, 0x60):
                for encrypted_report in (False, True):
                    if encrypted_report and model != "DJI Power 1000":
                        continue
                    with self.subTest(
                        model=model, command_type=command_type,
                        encrypted_report=encrypted_report,
                    ):
                        device, client = self.station(model)
                        flags = command_type | (6 if encrypted_report else 0)
                        report = self.report(flags=flags)
                        wire = report.encode()
                        for offset in range(0, len(wire), 7):
                            device._on_notify(None, bytearray(wire[offset:offset + 7]))
                        await self.drain(device)
                        client.write_gatt_char.assert_awaited_once()
                        args, kwargs = client.write_gatt_char.await_args
                        self.assertIs(args[0], device._write_characteristic)
                        self.assertEqual(kwargs, {"response": True})
                        ack = duml.DumlPacket.decode(args[1])
                        encrypted_ack = model == "DJI Power 1000"
                        self.assertEqual(
                            (ack.source, ack.destination, ack.sequence,
                             ack.flags, ack.command_set, ack.command_id),
                            (2, 0xAB, 0x1234, 0x86 if encrypted_ack else 0x80,
                             0x5A, 0x62),
                        )
                        self.assertEqual(device._decode_payload(ack), b"\x01")
                        self.assertEqual(device.data["timezone_offset_min"], 60)
                        self.assertEqual(device._sequence, 0x1000)
                        self.assertEqual(device._pending, {})

    async def test_only_ack_requested_settings_reports_addressed_to_app(self):
        device, client = self.station()
        cases = (
            {"flags": 0}, {"flags": 6}, {"flags": 0x80}, {"flags": 0xA0},
            {"flags": 0xC0}, {"flags": 0xE0}, {"destination": 3},
            {"command_set": 0}, {"command_id": duml.REPORT_COMMAND},
            {"command_id": duml.HMS_COMMAND}, {"command_id": 0x7F},
        )
        for case in cases:
            with self.subTest(case=case):
                device._handle_packet(self.report(**case))
                await self.drain(device)
                client.write_gatt_char.assert_not_awaited()

    async def test_ack_does_not_wait_for_get_reply_or_operation_lock(self):
        device, client = self.station()
        get_started = asyncio.Event()
        finish_get_write = asyncio.Event()
        writes = []
        get = None

        async def write(_characteristic, value, *, response):
            nonlocal get
            self.assertTrue(response)
            packet = duml.DumlPacket.decode(value)
            writes.append(packet)
            if packet.command_id == duml.GET_COMMAND:
                get = packet
                device._handle_packet(self.report())
                get_started.set()
                await finish_get_write.wait()
            else:
                self.assertTrue(packet.is_response)
                device._handle_packet(duml.DumlPacket(
                    0xAB, 2, get.sequence, 0x80, 0x5A, duml.GET_COMMAND, b"reply"
                ))

        client.write_gatt_char = write
        async with device._operation_lock:
            request = asyncio.create_task(device._request(duml.GET_COMMAND, b"\x00"))
            await get_started.wait()
            await asyncio.sleep(0)
            self.assertEqual(len(writes), 1)  # ACK cannot overlap the GATT write.
            self.assertTrue(device._settings_ack_tasks)
            finish_get_write.set()
            reply = await asyncio.wait_for(request, 1)
            await self.drain(device)
        self.assertEqual(reply.payload, b"reply")
        self.assertEqual([p.command_id for p in writes], [0x60, 0x62])
        self.assertEqual(device._pending, {})

    async def test_disconnect_cancels_inflight_ack(self):
        for unexpected in (False, True):
            with self.subTest(unexpected=unexpected):
                device, client = self.station()
                started = asyncio.Event()

                async def write(*_args, started=started, **_kwargs):
                    started.set()
                    await asyncio.Event().wait()

                client.write_gatt_char = write
                device._handle_packet(self.report())
                tasks = tuple(device._settings_ack_tasks)
                await started.wait()
                if unexpected:
                    device._on_disconnect(client)
                await device.disconnect()
                self.assertTrue(all(task.cancelled() for task in tasks))
                self.assertFalse(device._settings_ack_tasks)
                self.assertFalse(device._write_lock.locked())

    async def test_queued_ack_never_moves_to_replacement_client(self):
        device, client = self.station()
        replacement = types.SimpleNamespace(
            is_connected=True, write_gatt_char=AsyncMock(), disconnect=AsyncMock()
        )
        async with device._write_lock:
            device._handle_packet(self.report())
            await asyncio.sleep(0)
            device._client = replacement
            device._write_characteristic = object()
        await self.drain(device)
        client.write_gatt_char.assert_not_awaited()
        replacement.write_gatt_char.assert_not_awaited()

    async def test_queued_ack_is_cancelled_before_reconnect(self):
        device, client = self.station()
        async with device._write_lock:
            device._handle_packet(self.report())
            tasks = tuple(device._settings_ack_tasks)
            await asyncio.sleep(0)
            await device.disconnect()
        self.assertTrue(all(task.cancelled() for task in tasks))
        self.assertFalse(device._settings_ack_tasks)
        client.write_gatt_char.assert_not_awaited()

    async def test_ack_transport_errors_are_consumed_and_later_reports_work(self):
        device, client = self.station()
        for error in (BleakError("failed"), EOFError(), OSError("closed")):
            with self.subTest(error=type(error).__name__):
                client.write_gatt_char.side_effect = error
                with self.assertLogs(device_module.__name__, level="DEBUG"):
                    device._handle_packet(self.report())
                    await self.drain(device)
                self.assertEqual(device._pending, {})
                self.assertFalse(device._write_lock.locked())
        client.write_gatt_char.reset_mock(side_effect=True)
        device._handle_packet(self.report())
        await self.drain(device)
        client.write_gatt_char.assert_awaited_once()

    async def test_ack_write_timeout_releases_lock(self):
        device, client = self.station()

        async def stalled_write(*_args, **_kwargs):
            await asyncio.Event().wait()

        client.write_gatt_char = stalled_write
        with (
            patch.object(device_module, "DEFAULT_REQUEST_TIMEOUT", 0.01),
            self.assertLogs(device_module.__name__, level="DEBUG"),
        ):
            device._handle_packet(self.report())
            await asyncio.wait_for(self.drain(device), 1)
        self.assertFalse(device._write_lock.locked())

    async def test_no_ack_without_active_selected_connection(self):
        for missing in ("client", "connection", "characteristic", "closing"):
            with self.subTest(missing=missing):
                device, client = self.station()
                if missing == "client":
                    device._client = None
                elif missing == "connection":
                    client.is_connected = False
                elif missing == "characteristic":
                    device._write_characteristic = None
                else:
                    device._disconnecting = True
                device._handle_packet(self.report())
                await self.drain(device)
                client.write_gatt_char.assert_not_awaited()
