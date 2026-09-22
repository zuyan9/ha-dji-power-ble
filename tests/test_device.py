"""Tests for response routing and push publication in the device layer."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, call, patch

from tests.test_duml import SYNTHETIC_ECO_MODE, expansion_battery, record

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


DEFAULT_GATT_LAYOUT = (
    "0000a002-0000-1000-8000-00805f9b34fb",
    "0000c305-0000-1000-8000-00805f9b34fb",
    "0000c304-0000-1000-8000-00805f9b34fb",
)
ALTERNATE_GATT_LAYOUT = (
    "0000fff0-0000-1000-8000-00805f9b34fb",
    "0000fff4-0000-1000-8000-00805f9b34fb",
    "0000fff5-0000-1000-8000-00805f9b34fb",
)


class FakeGattService:
    """Keep characteristic lookup scoped to one discovered service."""

    def __init__(self, uuid, *characteristic_uuids, handle=1):
        self.uuid = uuid
        self.characteristics = [
            types.SimpleNamespace(uuid=value, handle=handle + offset)
            for offset, value in enumerate(characteristic_uuids)
        ]

    def get_characteristic(self, uuid):
        return next((c for c in self.characteristics if c.uuid == uuid), None)


class FakeGattServices:
    """Expose the service lookup used by Bleak's service collection."""

    def __init__(self, *services):
        self.services = services

    def get_service(self, uuid):
        return next((s for s in self.services if s.uuid == uuid), None)


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

    def __init__(
        self, device, *, encrypted=True, challenge=None, result=b"\x00" * 5,
        auth_responses=None,
    ):
        self.device = device
        self.encrypted = encrypted
        self.challenge = challenge if challenge is not None else b"\x00\x11\x22\x33\x44"
        self.result = result
        self.auth_responses = (
            iter(auth_responses) if auth_responses is not None else None
        )
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
            if self.auth_responses is not None:
                reply = next(self.auth_responses)
            elif payload == b"\x00":
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


class DischargePowerClient(StationClient):
    """Emulate app-schema configuration, never physical output or real firmware."""

    def __init__(self, device, value=SYNTHETIC_ECO_MODE):
        super().__init__(device, encrypted=False)
        self.value = value
        self.apply_set = True
        self.ack_value = bytes(4)
        self.omit_after_set = False

    async def write_gatt_char(self, uuid, value, *, response):  # noqa: ARG002
        request = duml.DumlPacket.decode(value)
        self.requests.append((request.command_id, request.payload))
        if request.command_id == duml.GET_COMMAND:
            entries = []
            if request.payload == b"\x00\x18\x10" and self.value is not None:
                entries = [(0x18, self.value)]
            reply = bytes(4) + duml.build_keyed_set_payload(entries, timestamp_ms=1)
        elif request.command_id == duml.SET_COMMAND:
            entries = [] if self.ack_value is None else [(0x18, self.ack_value)]
            reply = duml.build_keyed_set_payload(entries, timestamp_ms=1)
            requested = duml.parse_keyed_values(request.payload)
            assert set(requested) == {0x18}
            if self.apply_set and self.ack_value == bytes(4):
                self.value = requested[0x18]
            if self.omit_after_set:
                self.value = None
        else:
            raise AssertionError(f"unexpected command {request.command_id}")
        self.send(request.command_id, reply, sequence=request.sequence, flags=0x80)


class ConfigControlClient(StationClient):
    """Emulate targeted config reads on plaintext and encrypted transports."""

    def __init__(self, device, *, encrypted=False):
        super().__init__(device, encrypted=encrypted)
        self.values = {
            duml.CHARGE_LIMIT_KEY: b"".join(
                value.to_bytes(4, "little") for value in (100, 70, 100, 15, 0, 0)
            ),
            duml.POWER_SWITCH_KEY: bytes.fromhex("0d000300020102"),
        }
        self.ack_value = bytes(4)
        self.apply_set = True
        self.did_set = False
        self.omit_after_set = False

    async def write_gatt_char(self, uuid, value, *, response):  # noqa: ARG002
        request = duml.DumlPacket.decode(value)
        payload = (
            duml.decrypt_power_1000_payload(request.payload)
            if self.encrypted else request.payload
        )
        self.requests.append((request.command_id, payload))
        if request.command_id == duml.GET_COMMAND:
            assert len(payload) == 3 and payload[0] == 0 and payload[2] == 0x10
            key = payload[1]
            assert key in (duml.CHARGE_LIMIT_KEY, duml.POWER_SWITCH_KEY)
            entries = [] if self.did_set and self.omit_after_set else [
                (key, self.values[key])
            ]
            reply = bytes(4) + duml.build_keyed_set_payload(entries, timestamp_ms=1)
        elif request.command_id == duml.SET_COMMAND:
            requested = duml.parse_keyed_values(payload)
            entries = [] if self.ack_value is None else [
                (key, self.ack_value) for key in requested
            ]
            reply = duml.build_keyed_set_payload(entries, timestamp_ms=1)
            if self.apply_set and self.ack_value == bytes(4):
                self.values.update(requested)
            self.did_set = True
        else:
            raise AssertionError(f"unexpected command {request.command_id}")
        self.send(request.command_id, reply, sequence=request.sequence, flags=0x80)


class ConfigControlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.device, self.client = self.make_station()
        self.sleep = patch.object(
            device_module.asyncio, "sleep", new_callable=AsyncMock
        )
        self.sleep_mock = self.sleep.start()
        self.addCleanup(self.sleep.stop)

    @staticmethod
    def make_station(*, encrypted=False):
        device = device_module.DjiPowerDevice(
            FakeBleDevice(), "ab" * 16, name="Synthetic station",
            model="DJI Power 1000" if encrypted else "DJI Power 2000",
        )
        client = ConfigControlClient(device, encrypted=encrypted)
        device._client = client
        device._write_characteristic = object()
        device.data.update(duml.parse_telemetry(
            duml.build_keyed_set_payload(list(client.values.items()), timestamp_ms=1)
        ))
        return device, client

    async def test_all_standard_controls_confirm_immediately_with_targeted_reads(self):
        for encrypted in (False, True):
            for method, kwargs, key, expected in (
                ("set_ac", {"enabled": True}, 0x0D, {"ac_enabled": True}),
                ("set_charge_limits", {"discharge_limit": 5}, 0x05,
                 {"discharge_limit": 5, "recharge_limit": 100}),
                ("set_charge_limits", {"recharge_limit": 80}, 0x05,
                 {"discharge_limit": 0, "recharge_limit": 80}),
            ):
                with self.subTest(encrypted=encrypted, control=kwargs):
                    device, client = self.make_station(encrypted=encrypted)
                    await getattr(device, method)(**kwargs)

                    self.sleep_mock.assert_not_awaited()
                    for field, value in expected.items():
                        self.assertEqual(device.data[field], value)
                    get = (duml.GET_COMMAND, bytes((0, key, 0x10)))
                    reads = [item for item in client.requests
                             if item[0] == duml.GET_COMMAND]
                    self.assertEqual(reads, [get] * 2)
                    self.assertEqual(client.requests[-1], get)

    async def test_charge_limit_write_preserves_fresh_other_fields(self):
        fresh = b"".join(
            value.to_bytes(4, "little") for value in (99, 71, 90, 14, 1, 3)
        )
        self.client.values[0x05] = fresh

        await self.device.set_charge_limits(discharge_limit=5)

        self.assertEqual(self.client.values[0x05][:20], fresh[:20])
        self.assertEqual(self.device.data["recharge_limit"], 90)
        self.assertEqual(self.device.data["discharge_limit"], 5)

    async def test_missing_fresh_limits_never_writes_cached_record(self):
        self.client.values[0x05] = b""

        with self.assertRaisesRegex(device_module.DjiPowerError, "unavailable"):
            await self.device.set_charge_limits(recharge_limit=80)

        self.assertEqual(self.client.requests, [(duml.GET_COMMAND, b"\x00\x05\x10")])

    async def test_missing_readback_cannot_confirm_matching_cached_values(self):
        for method, kwargs in (
            ("set_ac", {"enabled": False}),
            ("set_charge_limits", {"recharge_limit": 100}),
        ):
            with self.subTest(control=method):
                device, client = self.make_station()
                client.omit_after_set = True
                self.sleep_mock.reset_mock()
                with self.assertRaisesRegex(
                    device_module.DjiPowerError,
                    "cannot read" if method == "set_ac" else "did not report",
                ):
                    await getattr(device, method)(**kwargs)
                self.assertEqual(
                    self.sleep_mock.await_args_list,
                    [] if method == "set_ac" else [call(2)] * 8,
                )
                if method == "set_ac":
                    self.assertIsNone(device.data["power_switches"])
                    self.assertIsNone(device.data["ac_enabled"])

    async def test_stale_readback_retries_then_confirms(self):
        self.client.apply_set = False

        async def apply_after_delay(_delay):
            self.assertEqual(len(self.client.requests), 3)
            self.client.values.update(
                duml.parse_keyed_values(self.client.requests[1][1])
            )

        self.sleep_mock.side_effect = apply_after_delay
        await self.device.set_ac(True)

        self.assertTrue(self.device.data["ac_enabled"])
        self.sleep_mock.assert_awaited_once_with(2)

    async def test_unapplied_write_exhausts_retries_without_confirming(self):
        self.client.apply_set = False

        with self.assertRaisesRegex(device_module.DjiPowerError, "did not report"):
            await self.device.set_ac(True)

        self.assertFalse(self.device.data["ac_enabled"])
        self.assertEqual(len(self.client.requests), 11)
        self.assertEqual(self.sleep_mock.await_args_list, [call(2)] * 8)

    async def test_rejected_ack_never_starts_readback(self):
        for method, kwargs in (
            ("set_ac", {"enabled": True}),
            ("set_charge_limits", {"recharge_limit": 80}),
        ):
            for ack in (None, bytes.fromhex("01000000")):
                with self.subTest(control=method, ack=ack):
                    device, client = self.make_station()
                    client.ack_value = ack
                    with self.assertRaises(device_module.DjiPowerError):
                        await getattr(device, method)(**kwargs)
                    self.assertEqual(client.requests[-1][0], duml.SET_COMMAND)
                    self.sleep_mock.assert_not_awaited()
                    self.assertFalse(device.data["ac_enabled"])
                    self.assertEqual(device.data["recharge_limit"], 100)

    async def test_cancelled_confirmation_releases_lock_and_pending_request(self):
        reading = asyncio.Event()
        send = self.client.write_gatt_char

        async def stall_readback(*args, **kwargs):
            if self.client.did_set:
                reading.set()
                await asyncio.Event().wait()
            await send(*args, **kwargs)

        self.client.write_gatt_char = stall_readback
        task = asyncio.create_task(self.device.set_ac(True))
        await reading.wait()
        self.assertTrue(self.device._operation_lock.locked())
        self.assertTrue(self.device._pending)
        self.assertFalse(self.device.data["ac_enabled"])

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertFalse(self.device._operation_lock.locked())
        self.assertEqual(self.device._pending, {})
        self.client.write_gatt_char = send
        await self.device.set_ac(False)
        self.assertFalse(self.device.data["ac_enabled"])


class EcoModeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.device = device_module.DjiPowerDevice(
            FakeBleDevice(), "ab" * 16, name="Test station", model="DJI Power 2000"
        )
        self.client = DischargePowerClient(self.device)
        self.device._client = self.client
        self.device._write_characteristic = object()
        self.sleep = patch.object(
            device_module.asyncio, "sleep", new_callable=AsyncMock
        )
        self.sleep_mock = self.sleep.start()
        self.addCleanup(self.sleep.stop)

    async def test_confirmed_eco_controls_do_not_wait_before_first_readback(self):
        for method, value in (
            (self.device.set_discharge_power, 422),
            (self.device.set_charge_power, 700),
            (self.device.set_power_adjustment, "Automatic"),
        ):
            with self.subTest(control=method.__name__):
                await method(value)
                self.sleep_mock.assert_not_awaited()

    async def test_stale_first_readback_retries_until_station_applies_write(self):
        self.client.apply_set = False

        async def apply_after_delay(delay):
            self.assertEqual(delay, 2)
            # SET was followed by a fresh GET before any retry delay.
            self.assertEqual(
                [command for command, _ in self.client.requests],
                [duml.GET_COMMAND, duml.SET_COMMAND, duml.GET_COMMAND],
            )
            requested = duml.parse_keyed_values(self.client.requests[1][1])
            self.client.value = requested[0x18]

        self.sleep_mock.side_effect = apply_after_delay
        await self.device.set_discharge_power(422)

        self.sleep_mock.assert_awaited_once_with(2)
        self.assertEqual(self.device.data["discharge_power_w"], 422)

    async def test_last_retry_retains_sixteen_seconds_of_settling_time(self):
        self.client.apply_set = False

        async def apply_on_last_retry(_delay):
            if self.sleep_mock.await_count == 8:
                requested = duml.parse_keyed_values(self.client.requests[1][1])
                self.client.value = requested[0x18]

        self.sleep_mock.side_effect = apply_on_last_retry
        await self.device.set_discharge_power(422)

        self.assertEqual(self.sleep_mock.await_args_list, [call(2)] * 8)
        self.assertEqual(len(self.client.requests), 11)

    async def test_rapid_watt_changes_confirm_in_order_without_fixed_delays(self):
        await asyncio.gather(
            *(self.device.set_discharge_power(watts) for watts in (94, 95, 96))
        )

        self.sleep_mock.assert_not_awaited()
        self.assertEqual(self.device.data["discharge_power_w"], 96)
        self.assertEqual(
            [command for command, _ in self.client.requests],
            [duml.GET_COMMAND, duml.SET_COMMAND, duml.GET_COMMAND] * 3,
        )

    async def test_queued_mode_change_waits_for_watt_confirmation(self):
        reading = asyncio.Event()
        release = asyncio.Event()
        queued = asyncio.Event()
        send = self.client.write_gatt_char

        async def hold_first_readback(*args, **kwargs):
            if len(self.client.requests) == 2:
                reading.set()
                await release.wait()
            await send(*args, **kwargs)

        async def change_mode():
            queued.set()
            await self.device.set_power_adjustment("Automatic")

        self.client.write_gatt_char = hold_first_readback
        first = asyncio.create_task(self.device.set_discharge_power(422))
        await reading.wait()
        second = asyncio.create_task(change_mode())
        await queued.wait()

        self.assertFalse(first.done())
        self.assertFalse(second.done())
        self.assertEqual(len(self.client.requests), 2)
        self.assertEqual(self.device.data["discharge_power_w"], 93)
        release.set()
        await asyncio.gather(first, second)

        self.sleep_mock.assert_not_awaited()
        self.assertEqual(self.device.data["power_adjustment"], "Automatic")
        self.assertEqual(int.from_bytes(self.client.value[38:42], "little"), 422)
        self.assertEqual(
            [command for command, _ in self.client.requests],
            [duml.GET_COMMAND, duml.SET_COMMAND, duml.GET_COMMAND] * 2,
        )

    async def test_queued_charge_and_discharge_writes_preserve_both_setpoints(self):
        reading = asyncio.Event()
        release = asyncio.Event()
        queued = asyncio.Event()
        send = self.client.write_gatt_char

        async def hold_first_readback(*args, **kwargs):
            if len(self.client.requests) == 2:
                reading.set()
                await release.wait()
            await send(*args, **kwargs)

        async def discharge():
            queued.set()
            await self.device.set_discharge_power(422)

        self.client.write_gatt_char = hold_first_readback
        first = asyncio.create_task(self.device.set_charge_power(700))
        await reading.wait()
        second = asyncio.create_task(discharge())
        await queued.wait()
        self.assertFalse(second.done())
        self.assertEqual(len(self.client.requests), 2)
        self.assertEqual(self.device.data["charge_power_w"], 500)

        release.set()
        await asyncio.gather(first, second)

        self.assertEqual(self.device.data["charge_power_w"], 700)
        self.assertEqual(self.device.data["discharge_power_w"], 422)
        self.sleep_mock.assert_not_awaited()
        self.assertEqual(
            [command for command, _ in self.client.requests],
            [duml.GET_COMMAND, duml.SET_COMMAND, duml.GET_COMMAND] * 2,
        )

    async def test_initial_config_explicitly_reads_eco_mode(self) -> None:
        await self.device.refresh_config()

        self.assertEqual(
            self.client.requests,
            [(duml.GET_COMMAND, bytes((0, key, 0x10))) for key in (1, 4, 0x18, 0x16)],
        )
        self.assertEqual(self.device.data["discharge_power_w"], 93)
        self.assertTrue(self.device.data["discharge_power_available"])
        self.assertEqual(self.device.data["charge_power_w"], 500)
        self.assertTrue(self.device.data["charge_power_available"])

    async def test_missing_optional_config_does_not_break_refresh(self) -> None:
        await self.device.refresh_config()
        self.client.value = None

        await self.device.refresh_config()

        self.assertFalse(self.device.data["discharge_power_available"])
        self.assertIsNone(self.device.data["discharge_power_w"])
        self.assertFalse(self.device.data["charge_power_available"])
        self.assertIsNone(self.device.data["charge_power_w"])
        self.assertIsNone(self.device.data["charge_power_min_w"])
        self.assertIsNone(self.device.data["charge_power_max_w"])
        self.assertIsNone(self.device.data["key_18"])
        self.assertIsNone(self.device.data["power_adjustment"])

    async def test_optional_read_failure_is_tolerated_but_disconnect_propagates(self):
        read_config = self.device._read_config
        for error in (
            device_module.DjiPowerError("timeout waiting for 0x60 response"),
            device_module.DjiPowerError("station returned malformed config data"),
            device_module.DjiPowerDisconnectedError("disconnected"),
        ):
            async def fail_eco_mode(key, error=error):
                if key == 0x18:
                    raise error
                return await read_config(key)

            with self.subTest(error=str(error)), patch.object(
                self.device, "_read_config", side_effect=fail_eco_mode
            ):
                if isinstance(error, device_module.DjiPowerDisconnectedError):
                    with self.assertRaises(device_module.DjiPowerDisconnectedError):
                        await self.device.refresh_config()
                else:
                    await self.device.refresh_config()
                self.assertFalse(self.device.data["discharge_power_available"])
                self.assertFalse(self.device.data["charge_power_available"])

    async def test_charge_write_uses_fresh_config_and_preserves_other_fields(self):
        await self.device.refresh_config()
        fresh = bytearray(SYNTHETIC_ECO_MODE + b"future-extension")
        fresh[38:42] = (211).to_bytes(4, "little")
        fresh[2:4] = b"\x01\x01"
        self.client.value = bytes(fresh)
        self.client.requests.clear()

        await self.device.set_charge_power(700)

        self.assertEqual(
            [command for command, _ in self.client.requests],
            [duml.GET_COMMAND, duml.SET_COMMAND, duml.GET_COMMAND],
        )
        sent = duml.parse_keyed_values(self.client.requests[1][1])[0x18]
        self.assertEqual(sent[:26], fresh[:26])
        self.assertEqual(sent[26:30], (700).to_bytes(4, "little"))
        self.assertEqual(sent[30:], fresh[30:])
        self.assertEqual(self.device.data["charge_power_w"], 700)
        self.assertEqual(self.device.data["discharge_power_w"], 211)
        self.sleep_mock.assert_not_awaited()

    async def test_charge_write_rechecks_mode_and_bounds_before_sending(self):
        await self.device.refresh_config()
        automatic = bytearray(SYNTHETIC_ECO_MODE)
        automatic[17] = 1
        lower_max = bytearray(SYNTHETIC_ECO_MODE)
        lower_max[18:22] = (600).to_bytes(4, "little")
        for state, watts in (
            (None, 700),
            (SYNTHETIC_ECO_MODE[:85], 700),
            (bytes(automatic), 700),
            (bytes(lower_max), 700),
            (SYNTHETIC_ECO_MODE, 99),
            (SYNTHETIC_ECO_MODE, 1201),
            (SYNTHETIC_ECO_MODE, 700.5),
        ):
            with self.subTest(state_length=len(state or b""), watts=watts):
                self.client.value = state
                self.client.requests.clear()
                with self.assertRaises(device_module.DjiPowerError):
                    await self.device.set_charge_power(watts)
                self.assertEqual(
                    self.client.requests, [(duml.GET_COMMAND, b"\x00\x18\x10")]
                )

    async def test_charge_write_requires_ack_and_matching_fresh_readback(self):
        for failure in ("missing_ack", "rejected_ack", "malformed_ack", "unapplied"):
            with self.subTest(failure=failure):
                self.client = DischargePowerClient(self.device)
                self.device._client = self.client
                self.device._write_characteristic = object()
                if failure == "missing_ack":
                    self.client.ack_value = None
                elif failure == "rejected_ack":
                    self.client.ack_value = bytes.fromhex("01000000")
                elif failure == "malformed_ack":
                    self.client.ack_value = b"\x00"
                else:
                    self.client.apply_set = False

                with self.assertRaises(device_module.DjiPowerError):
                    await self.device.set_charge_power(700)

                self.assertEqual(self.device.data["charge_power_w"], 500)
                self.assertEqual(self.device.data["discharge_power_w"], 93)
                self.assertEqual(
                    len(self.client.requests), 11 if failure == "unapplied" else 2
                )

    async def test_missing_charge_readback_clears_even_unchanged_setpoint(self):
        self.client.omit_after_set = True

        with self.assertRaisesRegex(device_module.DjiPowerError, "omitted"):
            await self.device.set_charge_power(500)

        self.assertFalse(self.device.data["charge_power_available"])
        self.assertIsNone(self.device.data["charge_power_w"])
        self.assertFalse(self.device.data["discharge_power_available"])

    async def test_set_uses_fresh_config_preserves_other_fields_and_reads_back(self):
        await self.device.refresh_config()
        # Simulate another controller changing unrelated settings after our cache.
        fresh = bytearray(SYNTHETIC_ECO_MODE + b"future-extension")
        fresh[2:4] = b"\x01\x01"
        fresh[42:79] = b"x" * 37
        self.client.value = bytes(fresh)
        self.client.requests.clear()
        updates = []
        self.device.add_state_listener(updates.append)

        await self.device.set_discharge_power(422)

        self.assertEqual(
            [command for command, _ in self.client.requests],
            [duml.GET_COMMAND, duml.SET_COMMAND, duml.GET_COMMAND],
        )
        self.assertEqual(self.client.requests[0][1], b"\x00\x18\x10")
        self.assertEqual(self.client.requests[-1][1], b"\x00\x18\x10")
        sent = duml.parse_keyed_values(self.client.requests[1][1])[0x18]
        self.assertEqual(sent[:38], fresh[:38])
        self.assertEqual(sent[38:42], bytes.fromhex("a6010000"))
        self.assertEqual(sent[42:], fresh[42:])
        self.assertEqual(self.device.data["discharge_power_w"], 422)
        self.assertEqual([update["discharge_power_w"] for update in updates], [93, 422])

    async def test_disconnect_during_read_does_not_publish_after_disconnect(self):
        await self.device.refresh_config()
        events = []
        self.device.add_state_listener(lambda _: events.append("state"))
        self.device.add_disconnect_listener(lambda _: events.append("disconnected"))

        async def disconnect(_key):
            self.device._on_disconnect(self.client)
            raise device_module.DjiPowerDisconnectedError("disconnected")

        with (
            patch.object(self.device, "_read_config", side_effect=disconnect),
            self.assertRaises(device_module.DjiPowerDisconnectedError),
        ):
            await self.device.set_discharge_power(422)
        self.assertEqual(events, ["disconnected"])
        self.assertFalse(self.device.is_connected)

    async def test_invalid_fresh_config_or_value_never_sends_set(self) -> None:
        disabled = bytearray(SYNTHETIC_ECO_MODE)
        disabled[17] = 1
        for state, watts in (
            (None, 422),
            (SYNTHETIC_ECO_MODE[:42], 422),
            (bytes(disabled), 422),
            (SYNTHETIC_ECO_MODE, 801),
            (SYNTHETIC_ECO_MODE, 422.5),
        ):
            with self.subTest(state_length=len(state or b""), watts=watts):
                self.client.value = state
                self.client.requests.clear()
                with self.assertRaises(device_module.DjiPowerError):
                    await self.device.set_discharge_power(watts)
                self.assertEqual(
                    self.client.requests, [(duml.GET_COMMAND, b"\x00\x18\x10")]
                )

    async def test_other_models_cannot_send_discharge_power(self) -> None:
        for model in ("DJI Power 1000", "DJI Power 1000 V2", "DJI Power 1000 Mini"):
            with self.subTest(model=model):
                self.device.model = model
                with self.assertRaisesRegex(device_module.DjiPowerError, "Power 2000"):
                    await self.device.set_discharge_power(422)
                with self.assertRaisesRegex(device_module.DjiPowerError, "Power 2000"):
                    await self.device.set_charge_power(700)
                with self.assertRaisesRegex(device_module.DjiPowerError, "Power 2000"):
                    await self.device.set_power_adjustment("Automatic")
        self.assertEqual(self.client.requests, [])

    async def test_failed_or_missing_ack_never_confirms_requested_value(self) -> None:
        for ack, message in (
            (b"\x01\x00\x00\x00", "failed with status 1"),
            (None, "omitted key 0x18"),
            (b"\x00", "malformed"),
        ):
            with self.subTest(ack=ack):
                self.client.ack_value = ack
                self.client.requests.clear()
                with self.assertRaisesRegex(device_module.DjiPowerError, message):
                    await self.device.set_discharge_power(422)
                self.assertEqual(self.device.data["discharge_power_w"], 93)
                self.assertEqual(len(self.client.requests), 2)

    async def test_ack_without_matching_readback_times_out(self) -> None:
        self.client.apply_set = False
        with self.assertRaisesRegex(device_module.DjiPowerError, "did not report"):
            await self.device.set_discharge_power(422)
        self.assertEqual(self.device.data["discharge_power_w"], 93)
        self.assertEqual(len(self.client.requests), 11)

    async def test_missing_readback_cannot_confirm_even_an_unchanged_setpoint(self):
        self.client.omit_after_set = True
        with self.assertRaisesRegex(device_module.DjiPowerError, "omitted"):
            await self.device.set_discharge_power(93)
        self.assertFalse(self.device.data["discharge_power_available"])
        self.assertIsNone(self.device.data["discharge_power_w"])

    async def test_adjustment_mode_readback_enables_and_disables_watt_control(self):
        # Start in Automatic; Manual should restore the saved 93 W setpoint.
        initial = bytearray(SYNTHETIC_ECO_MODE + b"extension")
        initial[17] = 1
        self.client.value = bytes(initial)
        await self.device.refresh_config()
        self.assertEqual(self.device.data["power_adjustment"], "Automatic")
        self.assertFalse(self.device.data["discharge_power_available"])
        self.assertFalse(self.device.data["charge_power_available"])

        for mode, encoded in (("Manual", 2), ("Automatic", 1)):
            with self.subTest(mode=mode):
                self.client.requests.clear()
                await self.device.set_power_adjustment(mode)
                self.assertEqual(
                    [command for command, _ in self.client.requests],
                    [duml.GET_COMMAND, duml.SET_COMMAND, duml.GET_COMMAND],
                )
                self.assertEqual(self.client.requests[0][1], b"\x00\x18\x10")
                self.assertEqual(self.client.requests[-1][1], b"\x00\x18\x10")
                sent = duml.parse_keyed_values(self.client.requests[1][1])[0x18]
                self.assertEqual(sent[:17], initial[:17])
                self.assertEqual(sent[17], encoded)
                self.assertEqual(sent[18:], initial[18:])
                self.assertEqual(self.device.data["power_adjustment"], mode)
                self.assertEqual(
                    self.device.data["discharge_power_available"], mode == "Manual"
                )
                self.assertEqual(
                    self.device.data["discharge_power_w"],
                    93 if mode == "Manual" else None,
                )
                self.assertEqual(
                    self.device.data["charge_power_available"], mode == "Manual"
                )
                self.assertEqual(
                    self.device.data["charge_power_w"],
                    500 if mode == "Manual" else None,
                )

    async def test_automatic_rechecks_meter_in_fresh_config(self) -> None:
        await self.device.refresh_config()
        unlinked = bytearray(SYNTHETIC_ECO_MODE)
        unlinked[42:79] = bytes(37)
        self.client.value = bytes(unlinked)
        self.client.requests.clear()

        with self.assertRaisesRegex(device_module.DjiPowerError, "link a smart meter"):
            await self.device.set_power_adjustment("Automatic")

        self.assertEqual(self.client.requests, [(duml.GET_COMMAND, b"\x00\x18\x10")])
        self.assertEqual(self.device.data["power_adjustment"], "Manual")
        await self.device.set_power_adjustment("Manual")
        self.assertEqual(self.client.value, unlinked)

    async def test_invalid_adjustment_config_or_option_never_sends_set(self) -> None:
        wrong_mode = bytearray(SYNTHETIC_ECO_MODE)
        wrong_mode[16] = 2
        for current, option in (
            (None, "Manual"),
            (SYNTHETIC_ECO_MODE[:42], "Manual"),
            (bytes(wrong_mode), "Manual"),
            (SYNTHETIC_ECO_MODE, "Off"),
        ):
            with self.subTest(option=option, length=len(current or b"")):
                self.client.value = current
                self.client.requests.clear()
                with self.assertRaises(device_module.DjiPowerError):
                    await self.device.set_power_adjustment(option)
                self.assertEqual(
                    self.client.requests, [(duml.GET_COMMAND, b"\x00\x18\x10")]
                )

    async def test_adjustment_ack_failure_does_not_change_reported_mode(self) -> None:
        for ack in (None, bytes.fromhex("01000000")):
            with self.subTest(ack=ack):
                self.client.ack_value = ack
                self.client.requests.clear()
                with self.assertRaises(device_module.DjiPowerError):
                    await self.device.set_power_adjustment("Automatic")
                self.assertEqual(self.device.data["power_adjustment"], "Manual")
                self.assertTrue(self.device.data["discharge_power_available"])
                self.assertEqual(len(self.client.requests), 2)

    async def test_adjustment_ack_without_changed_readback_times_out(self) -> None:
        self.client.apply_set = False
        with self.assertRaisesRegex(device_module.DjiPowerError, "did not report"):
            await self.device.set_power_adjustment("Automatic")
        self.assertEqual(self.device.data["power_adjustment"], "Manual")
        self.assertTrue(self.device.data["discharge_power_available"])
        self.assertEqual(len(self.client.requests), 11)

    async def test_missing_adjustment_readback_clears_both_controls(self) -> None:
        self.client.omit_after_set = True
        with self.assertRaisesRegex(device_module.DjiPowerError, "omitted"):
            await self.device.set_power_adjustment("Manual")
        self.assertIsNone(self.device.data["power_adjustment"])
        self.assertFalse(self.device.data["discharge_power_available"])
        self.assertFalse(self.device.data["charge_power_available"])


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
        device._write_characteristic = object()
        return device, client

    def _connecting_station(self, *args, **kwargs):
        device, client = self._station(*args, **kwargs)
        device._client = None
        device._write_characteristic = None
        client.services = FakeGattServices(FakeGattService(*DEFAULT_GATT_LAYOUT))
        client.start_notify = AsyncMock()
        client.disconnect = AsyncMock()
        device._establish = AsyncMock(return_value=client)
        device.refresh_config = AsyncMock()
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
            device_module.DjiPowerAuthenticationError,
            r"station rejected authentication \(status=02\)",
        ):
            await device._authenticate()
        self.assertEqual(len(client.requests), 2)

    async def test_status_03_restarts_handshake_with_fresh_nonce_and_sequence(self):
        old_nonce, new_nonce = bytes.fromhex("11223344"), bytes.fromhex("55667788")
        for model in (
            "DJI Power 1000", "DJI Power 1000 V2", "DJI Power 1000 Mini",
            "DJI Power 2000",
        ):
            with self.subTest(model=model):
                device, client = self._station(
                    model, encrypted=model == "DJI Power 1000",
                    auth_responses=[
                        b"\x00" + old_nonce, b"\x03" + bytes(4) + b"extension",
                        b"\x00" + new_nonce, bytes(5),
                    ],
                )
                send = client.send

                def send_with_stale_reply(
                    command, payload, *, sequence=0, flags=0, send=send, client=client,
                ):
                    if len(client.wire_requests) == 3:
                        send(
                            duml.AUTH_COMMAND, bytes(5),
                            sequence=client.wire_requests[1].sequence, flags=0x80,
                        )
                    send(command, payload, sequence=sequence, flags=flags)

                client.send = send_with_stale_reply
                with self.assertLogs(device_module.__name__, level="DEBUG") as logs:
                    await device._authenticate()

                self.assertEqual(
                    client.requests,
                    [
                        (duml.AUTH_COMMAND, b"\x00"),
                        (duml.AUTH_COMMAND, b"\x01" + old_nonce + b"ab" * 16 + b"\x00"),
                        (duml.AUTH_COMMAND, b"\x00"),
                        (duml.AUTH_COMMAND, b"\x01" + new_nonce + b"ab" * 16 + b"\x00"),
                    ],
                )
                self.assertEqual(len({p.sequence for p in client.wire_requests}), 4)
                self.assertIs(device._client, client)
                self.assertEqual(device._pending, {})
                output = "\n".join(logs.output)
                for secret in (old_nonce.hex(), new_nonce.hex(), "ab" * 16):
                    self.assertNotIn(secret, output)

    async def test_persistent_status_03_stops_after_one_fresh_handshake(self):
        for encrypted in (False, True):
            with self.subTest(encrypted=encrypted):
                device, client = self._connecting_station(
                    "DJI Power 1000" if encrypted else "DJI Power 2000",
                    encrypted=encrypted, result=b"\x03" + bytes(4),
                )
                with self.assertRaisesRegex(
                    device_module.DjiPowerAuthenticationError,
                    r"station rejected authentication \(status=03\)",
                ):
                    await device.connect()
                self.assertEqual(len(client.requests), 4)
                self.assertIsNone(device._client)
                self.assertEqual(device._pending, {})
                client.disconnect.assert_awaited_once()
                device._establish.assert_awaited_once()
                device.refresh_config.assert_not_awaited()

    async def test_other_or_incomplete_check_status_does_not_retry(self):
        for result in (
            b"", b"\x01" + bytes(4), b"\x02" + bytes(4), b"\xff" + bytes(4),
            *(b"\x03" + bytes(length) for length in range(4)),
        ):
            with self.subTest(result=result):
                device, client = self._station(
                    "DJI Power 2000", encrypted=False, result=result
                )
                status = result[:1].hex() or "missing"
                with self.assertRaisesRegex(
                    device_module.DjiPowerAuthenticationError,
                    rf"station rejected authentication \(status={status}\)",
                ):
                    await device._authenticate()
                self.assertEqual(len(client.requests), 2)

    async def test_retry_rejects_invalid_new_challenge_before_sending_key(self):
        for challenge in (b"", b"\x00" * 4, b"\x03" + bytes(4)):
            with self.subTest(challenge=challenge):
                device, client = self._station(
                    "DJI Power 2000", encrypted=False, auth_responses=[
                        b"\x00\x11\x22\x33\x44", b"\x03" + bytes(4), challenge,
                    ],
                )
                with self.assertRaisesRegex(
                    device_module.DjiPowerAuthenticationError, "invalid auth challenge"
                ):
                    await device._authenticate()
                self.assertEqual(len(client.requests), 3)
                self.assertEqual(client.requests[-1], (duml.AUTH_COMMAND, b"\x00"))

    async def test_undecodable_key_check_does_not_retry(self):
        self.device._request = AsyncMock(side_effect=[
            self._auth_response(b"\x00\x11\x22\x33\x44"),
            duml.DumlPacket(
                0xAB, 0x02, 2, 0x86, duml.POWER_COMMAND_SET, duml.AUTH_COMMAND,
                b"\x03" + bytes(15),
            ),
        ])
        with self.assertRaisesRegex(device_module.DjiPowerError, "cannot decode"):
            await self.device._authenticate()
        self.assertEqual(self.device._request.await_count, 2)

    async def test_short_successful_key_check_remains_accepted(self):
        device, client = self._station(result=b"\x00")
        await device._authenticate()
        self.assertEqual(len(client.requests), 2)

    async def test_interrupted_handshake_retry_cleans_up_connection(self):
        for request_number in (3, 4):
            for failure in ("cancel", "deadline", "disconnect"):
                with self.subTest(request_number=request_number, failure=failure):
                    device, client = self._connecting_station(auth_responses=[
                        b"\x00\x11\x22\x33\x44", b"\x03" + bytes(4),
                        b"\x00\x55\x66\x77\x88", bytes(5),
                    ])
                    started = asyncio.Event()
                    write = client.write_gatt_char
                    requests = []

                    async def interrupt(
                        uuid, value, *, response, requests=requests,
                        request_number=request_number, started=started,
                        failure=failure, device=device, client=client, write=write,
                    ):
                        requests.append(duml.DumlPacket.decode(value))
                        if len(requests) == request_number:
                            started.set()
                            if failure == "disconnect":
                                device._on_disconnect(client)
                                return
                            await asyncio.Event().wait()
                        await write(uuid, value, response=response)

                    client.write_gatt_char = interrupt
                    with patch.object(
                        device_module, "DEFAULT_CONNECT_TIMEOUT",
                        0.05 if failure == "deadline" else 1,
                    ):
                        task = asyncio.create_task(device.connect())
                        await asyncio.wait_for(started.wait(), 1)
                        if failure == "cancel":
                            task.cancel()
                        error = (
                            asyncio.CancelledError if failure == "cancel"
                            else device_module.DjiPowerError
                        )
                        with self.assertRaises(error):
                            await task

                    self.assertEqual(len(requests), request_number)
                    self.assertIsNone(device._client)
                    self.assertIsNone(device._write_characteristic)
                    self.assertEqual(device._pending, {})
                    self.assertIsNone(device._expansion_refresh_task)
                    device.refresh_config.assert_not_awaited()
                    device._establish.assert_awaited_once()
                    if failure != "disconnect":
                        client.disconnect.assert_awaited_once()

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
                        services=FakeGattServices(FakeGattService(*DEFAULT_GATT_LAYOUT)),
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
                    self.assertIsNone(self.device._write_characteristic)

    async def test_cache_retry_closes_failed_clients(self):
        for retry_fails in (False, True):
            with self.subTest(retry_fails=retry_fails):
                first = types.SimpleNamespace(
                    is_connected=True,
                    services=FakeGattServices(FakeGattService(*DEFAULT_GATT_LAYOUT)),
                    start_notify=AsyncMock(side_effect=BleakError("bad cache")),
                    clear_cache=AsyncMock(), disconnect=AsyncMock(),
                )

                async def subscribe(*args, retry_fails=retry_fails):
                    if retry_fails:
                        raise BleakError("retry failed")
                    self.device._report_event.set()

                second = types.SimpleNamespace(
                    is_connected=True, start_notify=subscribe, disconnect=AsyncMock(),
                    services=FakeGattServices(FakeGattService(*ALTERNATE_GATT_LAYOUT)),
                )
                self.device._establish = AsyncMock(side_effect=[first, second])
                self.device._authenticate = AsyncMock()
                self.device.refresh_config = AsyncMock()
                if retry_fails:
                    with self.assertRaisesRegex(BleakError, "retry failed"):
                        await self.device.connect()
                    self.assertIsNone(self.device._client)
                    self.assertIsNone(self.device._write_characteristic)
                    self.device._authenticate.assert_not_awaited()
                else:
                    await self.device.connect()
                    self.assertIs(self.device._client, second)
                    self.assertIs(
                        self.device._write_characteristic,
                        second.services.get_service(ALTERNATE_GATT_LAYOUT[0])
                        .get_characteristic(ALTERNATE_GATT_LAYOUT[2]),
                    )
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
        self.device._write_characteristic = object()
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
        self.device._write_characteristic = object()

        response = await self.device._request(duml.AUTH_COMMAND, b"\x00")

        self.assertEqual(response.payload, b"right")
        self.assertEqual(self.device._pending, {})

    async def test_refresh_retains_existing_keyed_get_requests(self) -> None:
        client = GetClient(self.device)
        self.device._client = client
        self.device._write_characteristic = object()

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
        self.assertFalse(self.device.data["charging"])
        self.assertEqual(len(updates), 1)

    async def test_report_transitions_keep_battery_state_across_partial_pushes(
        self,
    ) -> None:
        updates = []
        self.device.add_state_listener(updates.append)
        cases = (
            (None, None, 516, None, None, None),
            (1, 120, None, 1, 120, True),
            (2, 5940, 516, 2, 5940, False),
            (None, None, 0, 2, 5940, False),
            (0, 0, None, 0, 0, False),
            (1, 15, None, 1, 15, True),
            (255, 12, None, 255, 12, None),
            (None, None, 600, 255, 12, None),
        )
        for sequence, case in enumerate(cases, start=1):
            time_type, duration, input_w, expected_type, expected_time, charging = case
            with self.subTest(sequence=sequence, time_type=time_type):
                payload = duml.build_keyed_header(sequence)
                if time_type is not None:
                    battery = bytearray.fromhex("c819000000c8190000")
                    battery[2:4] = duration.to_bytes(2, "little")
                    battery[4] = time_type
                    payload += record(0x3020, battery)
                if input_w is not None:
                    power = (500).to_bytes(2, "little") + input_w.to_bytes(2, "little")
                    payload += record(0x3030, power)
                wire = duml.DumlPacket(
                    0xAB,
                    0x02,
                    sequence,
                    0,
                    duml.POWER_COMMAND_SET,
                    duml.REPORT_COMMAND,
                    payload,
                ).encode()

                for offset in range(0, len(wire), 7):
                    self.device._on_notify(None, bytearray(wire[offset : offset + 7]))

                self.assertEqual(len(updates), sequence)
                self.assertEqual(updates[-1].get("battery_time_type"), expected_type)
                self.assertEqual(updates[-1].get("runtime_min"), expected_time)
                self.assertIs(updates[-1].get("charging"), charging)
                if sequence == 1:
                    self.assertNotIn("charging", updates[-1])
                if expected_type == 255:
                    self.assertIn("charging", updates[-1])

        self.assertEqual(updates[1]["runtime_min"], 120)
        self.assertTrue(updates[1]["charging"])


class ExpansionBatteryTests(unittest.IsolatedAsyncioTestCase):
    """Synthetic pack traffic and background-task lifecycle, without hardware."""

    def setUp(self) -> None:
        self.device = device_module.DjiPowerDevice(
            FakeBleDevice(), "ab" * 16, name="Test station", model="DJI Power 2000"
        )
        self.client = types.SimpleNamespace(
            is_connected=True,
            stop_notify=AsyncMock(),
            disconnect=AsyncMock(),
        )
        self.device._client = self.client
        self.device._write_characteristic = object()
        self.payload = duml.build_keyed_set_payload(
            [(0x01, expansion_battery(temperature=2512))], timestamp_ms=1
        )
        self.device.data = {
            "battery_percent": 80,
            "expansion_batteries": [{"serial_number": "PREVIOUS-PACK"}],
        }

    async def asyncTearDown(self) -> None:
        await self.device.disconnect()

    def push(self, payload: bytes) -> None:
        self.device._handle_packet(
            duml.DumlPacket(
                0xAB, 2, 1, 0, duml.POWER_COMMAND_SET,
                duml.TELEMETRY_COMMAND, payload,
            )
        )

    async def test_keyed_get_decodes_packs_through_each_model_transport(self):
        for model in sorted(device_module.EXPANSION_MODELS):
            with self.subTest(model=model):
                device = device_module.DjiPowerDevice(
                    FakeBleDevice(), "ab" * 16, name="Test station", model=model
                )
                requests = []

                async def write(
                    _uuid, value, *, response,
                    device=device, model=model, requests=requests,
                ):
                    self.assertTrue(response)
                    packet = duml.DumlPacket.decode(value)
                    requests.append(packet)
                    self.assertEqual(device._decode_payload(packet), b"\x00\x01\x10")
                    payload = self.payload
                    flags = 0x80
                    if model == "DJI Power 1000":
                        payload = duml.encrypt_power_1000_payload(payload)
                        flags |= 6
                    reply = duml.DumlPacket(
                        0xAB, 2, packet.sequence, flags, duml.POWER_COMMAND_SET,
                        duml.GET_COMMAND, payload,
                    ).encode()
                    for offset in range(0, len(reply), 7):
                        device._on_notify(None, bytearray(reply[offset : offset + 7]))

                device._client = types.SimpleNamespace(
                    is_connected=True, write_gatt_char=write
                )
                device._write_characteristic = object()
                await device._read_expansion_batteries()

                self.assertEqual(requests[0].command_id, duml.GET_COMMAND)
                self.assertEqual(
                    requests[0].flags, 0x26 if model == "DJI Power 1000" else 0x20
                )
                self.assertEqual(
                    device.data["expansion_batteries"][0]["battery_percent"], 62.5
                )
                self.assertEqual(
                    device.data["expansion_batteries"][0]["temperature"], 25.12
                )
                self.assertEqual(device._pending, {})

    async def test_push_preserves_missing_list_and_clears_explicit_empty(self):
        self.push(self.payload)
        packs = self.device.data["expansion_batteries"]
        self.push(duml.build_keyed_set_payload([(0x15, b"\x3c\x00")], timestamp_ms=1))
        self.assertEqual(self.device.data["expansion_batteries"], packs)
        self.push(duml.build_keyed_set_payload([(0x01, b"")], timestamp_ms=1))
        self.assertEqual(self.device.data["expansion_batteries"], [])
        self.assertEqual(self.device.data["battery_percent"], 80)

    async def test_malformed_keyed_push_invalidates_pack_state(self):
        self.push(self.payload[:-1])
        self.assertIsNone(self.device.data["expansion_batteries"])
        self.assertEqual(self.device.data["battery_percent"], 80)

    async def test_targeted_get_omission_invalidates_previous_pack_state(self):
        with patch.object(self.device, "_read_config", AsyncMock(return_value={})):
            await self.device._read_expansion_batteries()
        self.assertIsNone(self.device.data["expansion_batteries"])
        self.assertEqual(self.device.data["battery_percent"], 80)

    async def test_optional_read_failure_invalidates_only_pack_state(self):
        for error in (
            device_module.DjiPowerError("timeout"), BleakError("read failed")
        ):
            with self.subTest(error=type(error).__name__):
                self.device.data["expansion_batteries"] = [{}]
                with patch.object(
                    self.device, "_read_config", AsyncMock(side_effect=error)
                ):
                    await self.device._read_expansion_batteries()
                self.assertIsNone(self.device.data["expansion_batteries"])
                self.assertEqual(self.device.data["battery_percent"], 80)

    async def test_disconnect_during_read_never_publishes_a_successful_update(self):
        updates = []
        self.device.add_state_listener(updates.append)

        async def read(_key):
            self.device._on_disconnect(self.client)
            raise device_module.DjiPowerDisconnectedError("disconnected")

        with (
            patch.object(self.device, "_read_config", read),
            self.assertRaises(device_module.DjiPowerDisconnectedError),
        ):
            await self.device._read_expansion_batteries()
        self.assertEqual(updates, [])

    async def test_stalled_gatt_write_times_out_and_releases_request(self):
        async def stalled_write(*_args, **_kwargs):
            await asyncio.Event().wait()

        self.client.write_gatt_char = stalled_write
        with patch.object(device_module, "DEFAULT_REQUEST_TIMEOUT", 0.01):
            await asyncio.wait_for(self.device._read_expansion_batteries(), timeout=1)
        self.assertIsNone(self.device.data["expansion_batteries"])
        self.assertEqual(self.device.data["battery_percent"], 80)
        self.assertEqual(self.device._pending, {})

    async def test_connect_starts_one_worker_and_disconnect_cancels_it(self):
        self.device._client = None

        async def initialize():
            self.device._client = self.client
            self.device._write_characteristic = object()

        with patch.object(self.device, "_connect_and_initialize", initialize):
            await self.device.connect()
            task = self.device._expansion_refresh_task
            self.assertIsNotNone(task)
            await self.device.connect()
            self.assertIs(self.device._expansion_refresh_task, task)
            await self.device.disconnect()
        self.assertTrue(task.cancelled())
        self.assertIsNone(self.device._expansion_refresh_task)
        self.client.disconnect.assert_awaited_once()

    async def test_unsupported_model_does_not_start_worker(self):
        self.device.model = "DJI Power"
        self.device._start_expansion_refresh()
        self.assertIsNone(self.device._expansion_refresh_task)

    async def test_usb_model_worker_refreshes_switches_without_pack_reads(self):
        self.device.model = "DJI Power 1000 Mini"

        async def accessories():
            self.assertTrue(self.device._operation_lock.locked())
            if accessory.await_count == 2:
                self.client.is_connected = False

        with (
            patch.object(device_module.asyncio, "sleep", AsyncMock()) as sleep,
            patch.object(
                self.device, "_refresh_accessory_config",
                AsyncMock(side_effect=accessories),
            ) as accessory,
            patch.object(
                self.device, "_read_expansion_batteries", AsyncMock()
            ) as packs,
        ):
            await self.device._refresh_expansion_loop()
        sleep.assert_awaited_once_with(30.0)
        self.assertEqual(accessory.await_count, 2)
        packs.assert_not_awaited()

    async def test_worker_refreshes_even_empty_snapshot_under_operation_lock(self):
        self.device.data["expansion_batteries"] = []

        async def read():
            self.assertTrue(self.device._operation_lock.locked())
            self.client.is_connected = False

        with (
            patch.object(device_module.asyncio, "sleep", AsyncMock()) as sleep,
            patch.object(self.device, "_refresh_accessory_config", AsyncMock()),
            patch.object(
                self.device, "_read_expansion_batteries", AsyncMock(side_effect=read)
            ) as refresh,
        ):
            await self.device._refresh_expansion_loop()
        sleep.assert_awaited_once_with(30.0)
        refresh.assert_awaited_once()

    async def test_disconnect_cancels_worker_and_pending_request(self):
        for unexpected in (False, True):
            with self.subTest(unexpected=unexpected):
                self.device._client = self.client
                self.device._write_characteristic = object()
                started = asyncio.Event()

                async def write(*_args, started=started, **_kwargs):
                    started.set()

                self.client.write_gatt_char = write
                with patch.object(device_module, "EXPANSION_REFRESH_INTERVAL", 0):
                    self.device._start_expansion_refresh()
                    task = self.device._expansion_refresh_task
                    await asyncio.wait_for(started.wait(), timeout=1)
                    self.assertEqual(len(self.device._pending), 1)
                    if unexpected:
                        self.device._on_disconnect(self.client)
                    await self.device.disconnect()
                self.assertTrue(task.cancelled())
                self.assertEqual(self.device._pending, {})
                self.assertIsNone(self.device._expansion_refresh_task)


if __name__ == "__main__":
    unittest.main()
