"""Wire-level session reuse and retention lifecycle tests without hardware."""

from __future__ import annotations

import asyncio
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from tests.test_device import (
    DEFAULT_GATT_LAYOUT,
    BleakError,
    FakeBleDevice,
    FakeGattService,
    FakeGattServices,
    StationClient,
    device_module,
    duml,
)

ADAPTER = "AA:BB:CC:DD:EE:01"
LIMITS = b"".join(value.to_bytes(4, "little") for value in (1, 2, 80, 3, 4, 10))
VALID_PROBE = bytes(4) + duml.build_keyed_set_payload(
    [(duml.CHARGE_LIMIT_KEY, LIMITS)], timestamp_ms=1
)


class RetainedStationClient(StationClient):
    """Return a scripted GET response through the model's actual DUML framing."""

    def __init__(self, device, *, retained=True, payload=VALID_PROBE, **reply):
        super().__init__(device, encrypted=device.model == "DJI Power 1000")
        self.connected_before_attach = retained
        self.payload = payload
        self.reply = reply
        self.services = FakeGattServices(FakeGattService(*DEFAULT_GATT_LAYOUT))
        self.start_notify = AsyncMock()
        self.disconnect = AsyncMock()
        self.detach = AsyncMock()

    async def write_gatt_char(self, characteristic, value, *, response):
        packet = duml.DumlPacket.decode(value)
        payload = (
            duml.decrypt_power_1000_payload(packet.payload)
            if self.encrypted else packet.payload
        )
        if packet.command_id != duml.GET_COMMAND or payload != b"\x00\x05\x10":
            await super().write_gatt_char(characteristic, value, response=response)
            return
        self.wire_requests.append(packet)
        self.requests.append((packet.command_id, payload))
        if self.payload is None:
            return
        reply_payload = self.reply.get(
            "ciphertext",
            duml.encrypt_power_1000_payload(self.payload)
            if self.encrypted else self.payload,
        )
        wire = duml.DumlPacket(
            self.reply.get("source", duml.POWER_DESTINATION),
            self.reply.get("destination", duml.APP_SOURCE),
            packet.sequence + self.reply.get("sequence_offset", 0),
            self.reply.get("flags", 0x86 if self.encrypted else 0x80),
            self.reply.get("command_set", duml.POWER_COMMAND_SET),
            self.reply.get("command_id", duml.GET_COMMAND),
            reply_payload,
        ).encode()
        for offset in range(0, len(wire), 7):
            self.device._on_notify(None, bytearray(wire[offset : offset + 7]))


class RetentionDeviceTests(unittest.IsolatedAsyncioTestCase):
    def station(
        self, *, keep=True, retained=True, payload=VALID_PROBE,
        allocate=None, release=None, model="DJI Power 1000", **reply,
    ):
        device = device_module.DjiPowerDevice(
            FakeBleDevice(), "ab" * 16, name="Test station",
            model=model, local_adapter=ADAPTER, keep_connection=keep,
            allocate_connection_slot=allocate, release_connection_slot=release,
        )
        client = RetainedStationClient(
            device, retained=retained, payload=payload, **reply
        )
        device._establish = AsyncMock(return_value=client)
        device.refresh_config = AsyncMock(side_effect=device._report_event.set)
        device._start_expansion_refresh = Mock()
        self.addAsyncCleanup(device.disconnect)
        return device, client

    def local_client_factory(self, device, client):
        """Restore real local establishment with an isolated fake backend."""
        del device._establish
        factory = Mock(return_value=client)
        module_name = "custom_components.dji_power_ble.local_ble"
        module = types.ModuleType(module_name)
        module.LocalBleakClient = factory
        self.enterContext(patch.dict(sys.modules, {module_name: module}))
        client.connect = AsyncMock()
        return factory

    async def test_retained_authorized_session_skips_authentication(self):
        for model in (*duml.MODEL_NAMES.values(), "DJI Power"):
            with self.subTest(model=model):
                device, client = self.station(model=model)

                await device.connect()

                self.assertEqual(
                    client.requests, [(duml.GET_COMMAND, b"\x00\x05\x10")]
                )
                self.assertEqual(
                    client.wire_requests[0].flags,
                    0x26 if model == "DJI Power 1000" else 0x20,
                )
                self.assertEqual(device.data["recharge_limit"], 80)
                self.assertEqual(device.data["discharge_limit"], 10)
                self.assertTrue(device._authenticated)
                self.assertEqual(device._pending, {})
                client.start_notify.assert_awaited_once()
                device.refresh_config.assert_awaited_once()
                device._start_expansion_refresh.assert_called_once()
                client.disconnect.assert_not_awaited()

                await device.disconnect(keep_connection=True)

                client.detach.assert_awaited_once()
                client.disconnect.assert_not_awaited()

    async def test_unproven_plaintext_session_runs_normal_authentication(self):
        with patch.object(device_module, "RESUME_PROBE_TIMEOUT", 0.01):
            for model in ("DJI Power 1000 V2", "DJI Power 1000 Mini", "DJI Power 2000"):
                for reply in (
                    {"payload": None}, {"payload": bytes(4)}, {"sequence_offset": -1}
                ):
                    with self.subTest(model=model, reply=reply):
                        device, client = self.station(model=model, **reply)

                        await device.connect()

                        self.assertEqual(
                            [command for command, _ in client.requests],
                            [duml.GET_COMMAND, duml.AUTH_COMMAND, duml.AUTH_COMMAND],
                        )
                        self.assertTrue(
                            all(packet.flags == 0x20 for packet in client.wire_requests)
                        )
                        self.assertTrue(device._authenticated)
                        self.assertEqual(device._pending, {})

    async def test_reusing_live_device_keeps_authentication_and_request_sequence(self):
        for model in ("DJI Power 1000", "DJI Power 2000"):
            with self.subTest(model=model):
                device, client = self.station(model=model, retained=False)
                await device.connect()
                self.assertTrue(device.can_retain_connection)
                sequence = device._sequence
                requests = list(client.requests)

                await device.connect()

                self.assertIs(device._client, client)
                self.assertEqual(device._sequence, sequence)
                self.assertEqual(client.requests, requests)
                device._establish.assert_awaited_once()
                client.start_notify.assert_awaited_once()
                client.detach.assert_not_awaited()
                client.disconnect.assert_not_awaited()

                self.assertTrue(await device._resume_session())
                self.assertEqual(client.wire_requests[-1].sequence, sequence + 1)

    def test_connection_identity_rejects_transport_or_credential_changes(self):
        device, _ = self.station()
        settings = {
            "address": device.address.lower(), "pair_key": "AB" * 16,
            "local_adapter": ADAPTER, "model": device.model,
        }
        self.assertTrue(device.matches_connection(**settings))
        for change in (
            {"address": "00:00:00:00:00:00"},
            {"pair_key": "cd" * 16},
            {"pair_key": "invalid"},
            {"local_adapter": "AA:BB:CC:DD:EE:02"},
            {"local_adapter": None},
            {"model": "DJI Power 2000"},
        ):
            with self.subTest(change=next(iter(change))):
                self.assertFalse(device.matches_connection(**{**settings, **change}))

    def test_only_live_authorized_opted_in_session_can_be_handed_off(self):
        device, client = self.station()
        device._client = client
        for keep, authenticated, connected in (
            (True, True, True), (False, True, True),
            (True, False, True), (True, True, False),
        ):
            with self.subTest(
                keep=keep, authenticated=authenticated, connected=connected
            ):
                device.keep_connection = keep
                device._authenticated = authenticated
                client.is_connected = connected
                self.assertEqual(
                    device.can_retain_connection, keep and authenticated and connected
                )

    async def test_unproven_existing_session_runs_normal_authentication(self):
        wrong_key = bytes(4) + duml.build_keyed_set_payload(
            [(0x04, LIMITS)], timestamp_ms=1
        )
        short_value = bytes(4) + duml.build_keyed_set_payload(
            [(duml.CHARGE_LIMIT_KEY, LIMITS[:-1])], timestamp_ms=1
        )
        cases = {
            "no response": {"payload": None},
            "empty ack": {"payload": bytes(4)},
            "negative status": {"payload": b"\x01" + VALID_PROBE[1:]},
            "truncated record": {"payload": VALID_PROBE[:-1]},
            "wrong record": {"payload": wrong_key},
            "short limits": {"payload": short_value},
            "missing get envelope": {"payload": VALID_PROBE[4:]},
            "wrong source": {"source": 0x05},
            "wrong destination": {"destination": 0x03},
            "old sequence": {"sequence_offset": -1},
            "push with same sequence": {"flags": 0x06},
            "wrong command": {"command_id": duml.TELEMETRY_COMMAND},
            "wrong command set": {"command_set": 0x00},
            "invalid encryption": {"ciphertext": b"\x00"},
        }
        with patch.object(device_module, "RESUME_PROBE_TIMEOUT", 0.01):
            for reason, kwargs in cases.items():
                with self.subTest(reason=reason):
                    device, client = self.station(**kwargs)
                    # Existing telemetry must not satisfy a new probe.
                    device.data.update(recharge_limit=80, discharge_limit=10)

                    await device.connect()

                    self.assertEqual(
                        [command for command, _ in client.requests],
                        [duml.GET_COMMAND, duml.AUTH_COMMAND, duml.AUTH_COMMAND],
                    )
                    self.assertEqual(client.requests[1][1], b"\x00")
                    self.assertEqual(
                        client.requests[2][1],
                        b"\x01\x11\x22\x33\x44" + b"ab" * 16 + b"\x00",
                    )
                    self.assertTrue(device._authenticated)
                    self.assertEqual(device._pending, {})
                    device.refresh_config.assert_awaited_once()

    async def test_fresh_or_non_opted_in_connection_always_authenticates(self):
        for keep, retained in ((True, False), (False, True), (False, False)):
            with self.subTest(keep=keep, retained=retained):
                device, client = self.station(keep=keep, retained=retained)

                await device.connect()

                self.assertEqual(
                    [command for command, _ in client.requests],
                    [duml.AUTH_COMMAND, duml.AUTH_COMMAND],
                )
                self.assertTrue(device._authenticated)

    async def test_interrupted_probe_does_not_authenticate(self):
        for failure in ("cancel", "deadline", "disconnect"):
            with self.subTest(failure=failure):
                device, client = self.station()
                started = asyncio.Event()

                async def interrupt(
                    _characteristic, value, *, response, device=device,
                    client=client, failure=failure, started=started,
                ):
                    client.wire_requests.append(duml.DumlPacket.decode(value))
                    started.set()
                    if failure == "disconnect":
                        client.is_connected = False
                        device._on_disconnect(client)
                        return
                    await asyncio.Event().wait()

                client.write_gatt_char = interrupt
                with patch.object(
                    device_module, "DEFAULT_CONNECT_TIMEOUT",
                    0.03 if failure == "deadline" else 1,
                ):
                    task = asyncio.create_task(device.connect())
                    await asyncio.wait_for(started.wait(), 1)
                    if failure == "cancel":
                        task.cancel()
                    error = (
                        asyncio.CancelledError if failure == "cancel"
                        else device_module.DjiPowerDisconnectedError
                        if failure == "disconnect" else device_module.DjiPowerError
                    )
                    with self.assertRaises(error):
                        await task

                self.assertEqual(
                    [packet.command_id for packet in client.wire_requests],
                    [duml.GET_COMMAND],
                )
                self.assertFalse(device._authenticated)
                self.assertIsNone(device._client)
                self.assertIsNone(device._write_characteristic)
                self.assertEqual(device._pending, {})
                device.refresh_config.assert_not_awaited()
                client.detach.assert_not_awaited()
                if failure != "disconnect":
                    client.disconnect.assert_awaited_once()

    async def test_transport_error_with_lost_link_is_not_an_auth_fallback(self):
        device, client = self.station()

        async def failed_write(*_args, **_kwargs):
            client.is_connected = False
            raise BleakError("link lost")

        client.write_gatt_char = AsyncMock(side_effect=failed_write)
        with self.assertRaises(device_module.DjiPowerDisconnectedError):
            await device.connect()

        client.write_gatt_char.assert_awaited_once()
        self.assertEqual(device._pending, {})
        client.disconnect.assert_awaited_once()
        device.refresh_config.assert_not_awaited()

    def test_retention_requires_explicit_local_adapter_for_every_model(self):
        for model in (*duml.MODEL_NAMES.values(), "DJI Power"):
            with (
                self.subTest(model=model),
                self.assertRaisesRegex(ValueError, "selected local adapter"),
            ):
                device_module.DjiPowerDevice(
                    FakeBleDevice(), "ab" * 16, name="Test station",
                    model=model, keep_connection=True,
                )

    async def test_only_authorized_opted_in_session_detaches_at_shutdown(self):
        for configured, requested, authenticated, connected in (
            (True, True, True, True),
            (False, True, True, True),
            (True, False, True, True),
            (True, True, False, True),
            (True, True, True, False),
        ):
            with self.subTest(
                configured=configured, requested=requested,
                authenticated=authenticated, connected=connected,
            ):
                device, client = self.station(keep=configured)
                device._client = client
                device._write_characteristic = object()
                device._authenticated = authenticated
                client.is_connected = connected

                await device.disconnect(keep_connection=requested)

                retain = configured and requested and authenticated and connected
                self.assertEqual(client.detach.await_count, int(retain))
                self.assertEqual(client.disconnect.await_count, int(not retain))
                self.assertIsNone(device._client)
                self.assertIsNone(device._write_characteristic)
                self.assertFalse(device._authenticated)
                self.assertFalse(device._disconnecting)
                # Repeated shutdown must not close an already detached link.
                await device.disconnect(keep_connection=requested)
                self.assertEqual(client.detach.await_count, int(retain))
                self.assertEqual(client.disconnect.await_count, int(not retain))

    async def test_failed_detach_falls_back_to_normal_disconnect(self):
        for error in (BleakError, RuntimeError, AttributeError):
            with self.subTest(error=error.__name__):
                device, client = self.station()
                device._client = client
                device._authenticated = True
                client.detach.side_effect = error("detach unavailable")

                with self.assertLogs(device_module.__name__, level="WARNING"):
                    await device.disconnect(keep_connection=True)

                client.detach.assert_awaited_once()
                client.disconnect.assert_awaited_once()
                self.assertIsNone(device._client)
                self.assertFalse(device._authenticated)
                self.assertFalse(device._disconnecting)

    async def test_detach_cancels_worker_and_resolves_pending_requests(self):
        device, client = self.station()
        device._client = client
        device._write_characteristic = object()
        device._authenticated = True
        started = asyncio.Event()

        async def write_without_reply(*_args, **_kwargs):
            started.set()

        client.write_gatt_char = AsyncMock(side_effect=write_without_reply)
        pending = asyncio.create_task(
            device._request(duml.GET_COMMAND, b"\x00\x01\x10")
        )
        await asyncio.wait_for(started.wait(), 1)
        self.assertEqual(len(device._pending), 1)
        worker = asyncio.create_task(asyncio.Event().wait())
        device._expansion_refresh_task = worker

        await device.disconnect(keep_connection=True)

        with self.assertRaises(device_module.DjiPowerDisconnectedError):
            await pending
        self.assertTrue(worker.cancelled())
        self.assertIsNone(device._expansion_refresh_task)
        self.assertEqual(device._pending, {})
        client.detach.assert_awaited_once()
        client.disconnect.assert_not_awaited()

    async def test_full_local_adapter_does_not_create_client_or_use_other_source(self):
        allocate, release = Mock(return_value=False), Mock()
        device, client = self.station(allocate=allocate, release=release)
        factory = self.local_client_factory(device, client)
        with (
            patch.object(
                device_module, "establish_connection", AsyncMock()
            ) as fallback,
            self.assertRaisesRegex(device_module.DjiPowerError, "No free connection"),
        ):
            await device.connect()

        allocate.assert_called_once_with(device._ble_device)
        factory.assert_not_called()
        fallback.assert_not_awaited()
        release.assert_not_called()
        self.assertFalse(device._slot_allocated)
        self.assertIsNone(device._client)

    async def test_failed_or_cancelled_local_connect_releases_slot(self):
        for failure in ("failure", "cancel"):
            with self.subTest(failure=failure):
                allocate, release = Mock(return_value=True), Mock()
                device, client = self.station(allocate=allocate, release=release)
                factory = self.local_client_factory(device, client)
                started = asyncio.Event()

                async def interrupted_connect(failure=failure, started=started):
                    started.set()
                    if failure == "failure":
                        raise BleakError("cannot connect")
                    await asyncio.Event().wait()

                client.connect.side_effect = interrupted_connect
                task = asyncio.create_task(device.connect())
                await asyncio.wait_for(started.wait(), 1)
                if failure == "cancel":
                    task.cancel()
                error = asyncio.CancelledError if failure == "cancel" else BleakError
                with self.assertRaises(error):
                    await task

                allocate.assert_called_once_with(device._ble_device)
                release.assert_called_once_with(device._ble_device)
                factory.assert_called_once()
                client.disconnect.assert_awaited_once()
                client.detach.assert_not_awaited()
                self.assertFalse(device._slot_allocated)
                self.assertIsNone(device._client)

    async def test_live_retained_link_keeps_slot_but_closed_link_releases_it(self):
        for outcome in ("normal", "unexpected", "retain"):
            with self.subTest(outcome=outcome):
                allocate, release = Mock(return_value=True), Mock()
                device, client = self.station(allocate=allocate, release=release)
                self.local_client_factory(device, client)

                await device.connect()

                allocate.assert_called_once_with(device._ble_device)
                self.assertTrue(device._slot_allocated)
                release.assert_not_called()
                if outcome == "unexpected":
                    client.is_connected = False
                    device._on_disconnect(client)
                await device.disconnect(keep_connection=outcome == "retain")
                # Repeated unload/stop cleanup must not release a detached link.
                await device.disconnect()

                self.assertEqual(release.call_count, int(outcome != "retain"))
                self.assertEqual(client.detach.await_count, int(outcome == "retain"))
                self.assertFalse(device._slot_allocated)
