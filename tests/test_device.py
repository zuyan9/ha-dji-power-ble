"""Tests for response routing and push publication in the device layer."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, call

ROOT = Path(__file__).parents[1]
COMPONENT = ROOT / "custom_components" / "dji_power_ble"


def _package(name: str, path: Path | None = None) -> types.ModuleType:
    module = types.ModuleType(name)
    if path is not None:
        module.__path__ = [str(path)]
    sys.modules[name] = module
    return module


_package("custom_components", ROOT / "custom_components")
_package("custom_components.dji_power_ble", COMPONENT)

bleak = _package("bleak")
bleak.BleakClient = object
backends = _package("bleak.backends")
backend_device = _package("bleak.backends.device")
backend_device.BLEDevice = object
backends.device = backend_device
bleak.backends = backends
bleak_exc = _package("bleak.exc")


class BleakError(Exception):
    """Test replacement for BleakError."""


bleak_exc.BleakError = BleakError
bleak.exc = bleak_exc
connector = _package("bleak_retry_connector")
connector.BleakClientWithServiceCache = object


async def _unused_establish(*args, **kwargs):  # noqa: ARG001
    raise AssertionError("connection establishment is not used in these unit tests")


connector.establish_connection = _unused_establish


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


duml = _load("custom_components.dji_power_ble.duml", COMPONENT / "duml.py")
device_module = _load("custom_components.dji_power_ble.device", COMPONENT / "device.py")


class FakeBleDevice:
    address = "AA:BB:CC:DD:EE:FF"


class RespondingClient:
    """Return one wrong-sequence push before the matching response."""

    is_connected = True

    def __init__(self, device) -> None:
        self.device = device

    async def write_gatt_char(self, uuid, value, *, response):  # noqa: ARG002
        request = duml.DumlPacket.decode(value)
        unrelated = duml.DumlPacket(
            0xAB,
            0x02,
            request.sequence,
            0x00,
            request.command_set,
            request.command_id,
            b"wrong",
        )
        self.device._handle_packet(unrelated)
        reply = duml.DumlPacket(
            0xAB,
            0x02,
            request.sequence,
            0x80,
            request.command_set,
            request.command_id,
            b"right",
        )
        self.device._handle_packet(reply)


class GetClient:
    """Return empty but valid keyed-GET responses and record requests."""

    is_connected = True

    def __init__(self, device) -> None:
        self.device = device
        self.requests = []

    async def write_gatt_char(self, uuid, value, *, response):  # noqa: ARG002
        request = duml.DumlPacket.decode(value)
        self.requests.append(request)
        reply = duml.DumlPacket(
            0xAB,
            0x02,
            request.sequence,
            0x80,
            request.command_set,
            request.command_id,
            b"\x00" * 4 + duml.build_keyed_header(1),
        )
        self.device._handle_packet(reply)


class StationClient:
    """Exercise full wire framing with fragmented station replies."""

    is_connected = True

    def __init__(self, device, *, encrypted=True, challenge=None, result=b"\x00" * 5):
        self.device = device
        self.encrypted = encrypted
        self.challenge = challenge if challenge is not None else b"\x00\x11\x22\x33\x44"
        self.result = result
        self.requests = []
        self.wire_requests = []

    def send(self, command, payload, *, sequence=0, flags=0):
        if self.encrypted:
            payload = duml.encrypt_power_1000_payload(payload)
            flags |= duml.POWER_1000_ENCRYPTION_TYPE
        wire = duml.DumlPacket(
            0xAB, 0x02, sequence, flags, duml.POWER_COMMAND_SET, command, payload
        ).encode()
        for offset in range(0, len(wire), 7):
            self.device._on_notify(None, bytearray(wire[offset : offset + 7]))

    async def write_gatt_char(self, uuid, value, *, response):  # noqa: ARG002
        request = duml.DumlPacket.decode(value)
        self.wire_requests.append(request)
        assert request.flags == (0x26 if self.encrypted else 0x20)
        payload = (
            duml.decrypt_power_1000_payload(request.payload)
            if self.encrypted
            else request.payload
        )
        self.requests.append((request.command_id, payload))
        if request.command_id == duml.AUTH_COMMAND:
            if payload == b"\x00":
                reply = self.challenge
            else:
                assert payload == b"\x01\x11\x22\x33\x44" + b"ab" * 16 + b"\x00"
                reply = self.result
        elif request.command_id == duml.GET_COMMAND:
            reply = b"\x00" * 4 + duml.build_keyed_set_payload(
                [(0x15, (60).to_bytes(2, "little"))], timestamp_ms=1
            )
        elif request.command_id == duml.SET_COMMAND:
            reply = duml.build_keyed_set_payload(
                [(key, b"\x00" * 4) for key in duml.parse_keyed_values(payload)],
                timestamp_ms=1,
            )
        else:
            raise AssertionError(f"unexpected command {request.command_id}")
        self.send(request.command_id, reply, sequence=request.sequence, flags=0x80)


class DeviceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.device = device_module.DjiPowerDevice(
            FakeBleDevice(), "ab" * 16, name="Test station"
        )

    @staticmethod
    def _auth_response(payload: bytes) -> duml.DumlPacket:
        return duml.DumlPacket(
            0xAB, 0x02, 1, 0x80, duml.POWER_COMMAND_SET, duml.AUTH_COMMAND, payload
        )

    def _station(self, model="DJI Power 1000", **kwargs):
        device = device_module.DjiPowerDevice(
            FakeBleDevice(), "ab" * 16, name="Test station", model=model
        )
        client = StationClient(device, **kwargs)
        device._client = client
        return device, client

    async def test_power_1000_auth_config_set_and_fragmented_pushes(self):
        device, client = self._station()
        with self.assertLogs(device_module.__name__, level="DEBUG") as logs:
            await device._authenticate()
        self.assertEqual([len(p.payload) for p in client.wire_requests], [16, 48])
        self.assertIn("stage=start_bind status=00 payload_length=16", logs.output[0])
        self.assertIn("flags=0x86", logs.output[0])
        self.assertIn("decoded_payload_length=5", logs.output[0])
        self.assertNotIn("11223344", "\n".join(logs.output))
        self.assertNotIn("ab" * 16, "\n".join(logs.output))

        await device.refresh_config()
        self.assertEqual(device.data["timezone_offset_min"], 60)
        await device._set(duml.build_ac_set_payload(True), (0x0D, 0x0E))
        self.assertEqual(
            [p.command_id for p in client.wire_requests],
            [
                duml.AUTH_COMMAND,
                duml.AUTH_COMMAND,
                duml.GET_COMMAND,
                duml.GET_COMMAND,
                duml.SET_COMMAND,
            ],
        )

        battery = (5000).to_bytes(2, "little") + b"\x00" * 7
        report = duml.build_keyed_header(1) + b"\x20\x30\x09\x00" + battery
        client.send(duml.REPORT_COMMAND, report)
        self.assertEqual(device.data["battery_percent"], 50)
        self.assertTrue(device._report_event.is_set())
        client.send(
            duml.TELEMETRY_COMMAND,
            duml.build_keyed_set_payload(
                [(0x15, (120).to_bytes(2, "little"))], timestamp_ms=1
            ),
        )
        self.assertEqual(device.data["timezone_offset_min"], 120)
        client.send(duml.HMS_COMMAND, b"\x00" * 4)
        self.assertEqual(device.data["hms_raw"], "00000000")

    async def test_plaintext_models_keep_plaintext_wire_authentication(self):
        for model in ("DJI Power 1000 V2", "DJI Power 1000 Mini", "DJI Power 2000"):
            with self.subTest(model=model):
                device, client = self._station(model, encrypted=False)
                await device._authenticate()
                self.assertEqual(
                    [len(p.payload) for p in client.wire_requests], [1, 38]
                )
                self.assertEqual(client.requests[0], (duml.AUTH_COMMAND, b"\x00"))

    async def test_encrypted_rejected_challenge_does_not_send_pair_key(self):
        device, client = self._station(challenge=b"\x01" + b"\x00" * 4)
        with (
            self.assertLogs(device_module.__name__, level="DEBUG") as logs,
            self.assertRaisesRegex(
                device_module.DjiPowerAuthenticationError, "invalid auth challenge"
            ),
        ):
            await device._authenticate()
        self.assertEqual(len(client.requests), 1)
        self.assertIn("status=01 payload_length=16", logs.output[0])

    async def test_encrypted_key_rejection_remains_an_authentication_error(self):
        device, client = self._station(result=b"\x02" + b"\x00" * 4)
        with self.assertRaisesRegex(
            device_module.DjiPowerAuthenticationError, "rejected the pair key"
        ):
            await device._authenticate()
        self.assertEqual(len(client.requests), 2)

    async def test_undecodable_auth_is_not_interpreted_as_status(self):
        challenge = duml.encrypt_power_1000_payload(b"\x00\x11\x22\x33\x44")
        for model, flags, payload in (
            ("DJI Power 1000", 0x86, b"\x00"),
            ("DJI Power 1000", 0x86, b"\x00" * 16),
            ("DJI Power 1000", 0x8E, challenge),
            ("DJI Power 1000 Mini", 0x86, challenge),
        ):
            with self.subTest(model=model, flags=flags, length=len(payload)):
                device, _client = self._station(model)
                device._request = AsyncMock(
                    return_value=duml.DumlPacket(
                        0xAB,
                        0x02,
                        1,
                        flags,
                        duml.POWER_COMMAND_SET,
                        duml.AUTH_COMMAND,
                        payload,
                    )
                )
                with (
                    self.assertLogs(device_module.__name__, level="DEBUG") as logs,
                    self.assertRaisesRegex(
                        device_module.DjiPowerError, "cannot decode"
                    ),
                ):
                    await device._authenticate()
                device._request.assert_awaited_once()
                self.assertIn("status=encrypted", logs.output[0])

    async def test_malformed_encrypted_push_does_not_publish_state(self):
        device, _client = self._station()
        changes = []
        device.add_state_listener(changes.append)
        device._handle_packet(
            duml.DumlPacket(
                0xAB,
                0x02,
                1,
                6,
                duml.POWER_COMMAND_SET,
                duml.REPORT_COMMAND,
                b"\x00" * 16,
            )
        )
        self.assertEqual(changes, [])
        self.assertEqual(device.data, {})

    async def test_interrupted_connection_closes_established_client(self):
        for stage in ("subscribe", "authenticate"):
            for failure in ("cancel", "timeout"):
                with self.subTest(stage=stage, failure=failure):
                    started = asyncio.Event()

                    async def interrupt(*args, started=started, failure=failure):
                        started.set()
                        if failure == "timeout":
                            raise TimeoutError
                        await asyncio.Event().wait()

                    client = types.SimpleNamespace(
                        is_connected=True, start_notify=AsyncMock(),
                        disconnect=AsyncMock(),
                    )
                    self.device._establish = AsyncMock(return_value=client)
                    self.device._authenticate = AsyncMock()
                    if stage == "subscribe":
                        client.start_notify = interrupt
                    else:
                        self.device._authenticate = interrupt
                    task = asyncio.create_task(self.device.connect())
                    await started.wait()
                    if failure == "cancel":
                        task.cancel()
                    error = (
                        asyncio.CancelledError if failure == "cancel"
                        else device_module.DjiPowerError
                    )
                    with self.assertRaises(error):
                        await task
                    client.disconnect.assert_awaited_once()
                    self.assertIsNone(self.device._client)

    async def test_cache_retry_closes_failed_clients(self):
        for retry_fails in (False, True):
            with self.subTest(retry_fails=retry_fails):
                first = types.SimpleNamespace(
                    is_connected=True,
                    start_notify=AsyncMock(side_effect=BleakError("bad cache")),
                    clear_cache=AsyncMock(), disconnect=AsyncMock(),
                )

                async def subscribe(*args, retry_fails=retry_fails):
                    if retry_fails:
                        raise BleakError("retry failed")
                    self.device._report_event.set()

                second = types.SimpleNamespace(
                    is_connected=True, start_notify=subscribe, disconnect=AsyncMock()
                )
                self.device._establish = AsyncMock(side_effect=[first, second])
                self.device._authenticate = AsyncMock()
                self.device.refresh_config = AsyncMock()
                if retry_fails:
                    with self.assertRaisesRegex(BleakError, "retry failed"):
                        await self.device.connect()
                    self.assertIsNone(self.device._client)
                    self.device._authenticate.assert_not_awaited()
                else:
                    await self.device.connect()
                    self.assertIs(self.device._client, second)
                    second.disconnect.assert_not_awaited()
                    self.device._authenticate.assert_awaited_once()
                    await self.device.disconnect()
                first.clear_cache.assert_awaited_once()
                first.disconnect.assert_awaited_once()
                second.disconnect.assert_awaited_once()

    async def test_request_timeout_includes_stalled_gatt_write(self):
        async def stalled_write(*args, **kwargs):
            await asyncio.Event().wait()

        self.device._client = types.SimpleNamespace(
            is_connected=True, write_gatt_char=stalled_write
        )
        with self.assertRaisesRegex(device_module.DjiPowerError, "timeout waiting"):
            await asyncio.wait_for(
                self.device._request(duml.AUTH_COMMAND, b"\x00", timeout=0.01), 1
            )
        self.assertEqual(self.device._pending, {})

    async def test_authentication_logs_both_stages_and_sends_existing_credential(
        self,
    ) -> None:
        nonce = b"\x12\x34\x56\x78"
        self.device._request = AsyncMock(
            side_effect=[
                self._auth_response(b"\x00" + nonce),
                self._auth_response(b"\x00" * 5),
            ]
        )

        with self.assertLogs(device_module.__name__, level="DEBUG") as logs:
            await self.device._authenticate()

        self.assertEqual(
            self.device._request.await_args_list,
            [
                call(duml.AUTH_COMMAND, b"\x00"),
                call(duml.AUTH_COMMAND, b"\x01" + nonce + b"ab" * 16 + b"\x00"),
            ],
        )
        self.assertEqual(len(logs.output), 2)
        self.assertIn("stage=start_bind status=00 payload_length=5", logs.output[0])
        self.assertIn("source=0xab destination=0x02 flags=0x80", logs.output[0])
        self.assertIn("stage=check_secret_key status=00", logs.output[1])
        self.assertNotIn(nonce.hex(), "\n".join(logs.output))
        self.assertNotIn("ab" * 16, "\n".join(logs.output))

    async def test_failed_challenges_log_metadata_without_sending_pair_key(
        self,
    ) -> None:
        for payload in (b"", b"\x00", b"\x00" * 4, b"\x01" + b"\x00" * 4, b"\xff"):
            with self.subTest(payload=payload):
                self.device._request = AsyncMock(
                    return_value=self._auth_response(payload)
                )
                with (
                    self.assertLogs(device_module.__name__, level="DEBUG") as logs,
                    self.assertRaisesRegex(
                        device_module.DjiPowerAuthenticationError,
                        "invalid auth challenge",
                    ),
                ):
                    await self.device._authenticate()

                self.device._request.assert_awaited_once_with(
                    duml.AUTH_COMMAND, b"\x00"
                )
                self.assertEqual(len(logs.output), 1)
                self.assertIn(
                    f"status={payload[:1].hex() or 'missing'}", logs.output[0]
                )
                self.assertIn(f"payload_length={len(payload)}", logs.output[0])

    async def test_auth_failure_log_omits_unknown_payload_and_device_identifiers(
        self,
    ) -> None:
        self.device.serial_number = "TEST-SERIAL"
        private_data = b"ab" * 16 + b"-private-response"
        self.device._request = AsyncMock(
            return_value=self._auth_response(b"\xff" + private_data)
        )

        with (
            self.assertLogs(device_module.__name__, level="DEBUG") as logs,
            self.assertRaises(device_module.DjiPowerAuthenticationError),
        ):
            await self.device._authenticate()

        output = "\n".join(logs.output)
        for value in (
            "ab" * 16,
            private_data.decode(),
            private_data.hex(),
            FakeBleDevice.address,
            "Test station",
            self.device.serial_number,
        ):
            self.assertNotIn(value, output)

    async def test_key_check_failure_is_logged_and_still_rejected(self) -> None:
        self.device._request = AsyncMock(
            side_effect=[
                self._auth_response(b"\x00\x12\x34\x56\x78"),
                self._auth_response(b"\x02" + b"\x00" * 4),
            ]
        )

        with (
            self.assertLogs(device_module.__name__, level="DEBUG") as logs,
            self.assertRaises(device_module.DjiPowerAuthenticationError),
        ):
            await self.device._authenticate()

        self.assertEqual(self.device._request.await_count, 2)
        self.assertIn("stage=check_secret_key status=02", logs.output[1])

    async def test_requests_are_matched_by_sequence(self) -> None:
        self.device._client = RespondingClient(self.device)

        response = await self.device._request(duml.AUTH_COMMAND, b"\x00")

        self.assertEqual(response.payload, b"right")
        self.assertEqual(self.device._pending, {})

    async def test_refresh_uses_keyed_get_module_sweeps(self) -> None:
        client = GetClient(self.device)
        self.device._client = client

        await self.device.refresh_config()

        self.assertEqual(
            [(request.command_id, request.payload) for request in client.requests],
            [
                (duml.GET_COMMAND, b"\x00\x01\x10"),
                (duml.GET_COMMAND, b"\x00\x04\x10"),
            ],
        )

    async def test_report_push_merges_state_and_notifies_listener(self) -> None:
        updates = []
        self.device.add_state_listener(updates.append)
        battery = bytes.fromhex("c819990b02c8190000bc0c00")
        payload = (
            duml.build_keyed_header(1)
            + (0x3020).to_bytes(2, "little")
            + len(battery).to_bytes(2, "little")
            + battery
            + (0x3030).to_bytes(2, "little")
            + (4).to_bytes(2, "little")
            + bytes.fromhex("00007c01")
        )
        packet = duml.DumlPacket(
            0xAB, 0x02, 1, 0, duml.POWER_COMMAND_SET, duml.REPORT_COMMAND, payload
        )

        self.device._handle_packet(packet)

        self.assertEqual(self.device.data["battery_percent"], 66)
        self.assertTrue(self.device.data["charging"])
        self.assertEqual(len(updates), 1)


if __name__ == "__main__":
    unittest.main()
