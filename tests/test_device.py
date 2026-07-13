"""Tests for response routing and push publication in the device layer."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path

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


class DeviceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.device = device_module.DjiPowerDevice(
            FakeBleDevice(), "ab" * 16, name="Test station"
        )

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
