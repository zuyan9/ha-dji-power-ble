"""Persistent local BLE client for DJI Power stations."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import TypeAlias

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from .duml import (
    APP_SOURCE,
    AUTH_COMMAND,
    CHECK_SECRET_KEY,
    GET_COMMAND,
    HMS_COMMAND,
    NOTIFY_UUID,
    POWER_COMMAND_SET,
    POWER_DESTINATION,
    POWER_SWITCH_KEY,
    REPORT_COMMAND,
    RULES_KEY,
    SET_COMMAND,
    START_BIND,
    TELEMETRY_COMMAND,
    WRITE_UUID,
    DumlPacket,
    DumlStream,
    ProtocolError,
    build_ac_set_payload,
    build_charge_limits_set_payload,
    normalize_pair_key,
    parse_report,
    parse_set_ack,
    parse_telemetry,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_REQUEST_TIMEOUT = 8.0
DEFAULT_CONNECT_TIMEOUT = 30.0


class DjiPowerError(Exception):
    """Base device/transport error."""


class DjiPowerAuthenticationError(DjiPowerError):
    """The station rejected the local pair key."""


class DjiPowerDisconnectedError(DjiPowerError):
    """The BLE link disappeared during an operation."""


StateCallback: TypeAlias = Callable[[dict[str, object]], None]
DisconnectCallback: TypeAlias = Callable[[Exception | None], None]


class DjiPowerDevice:
    """One authenticated station with a long-lived push connection."""

    def __init__(
        self,
        ble_device: BLEDevice,
        pair_key: str | bytes,
        *,
        name: str,
        model: str = "DJI Power",
        serial_number: str | None = None,
    ) -> None:
        self._ble_device = ble_device
        self._pair_key = normalize_pair_key(pair_key)
        self._name = name
        self.model = model
        self.serial_number = serial_number
        self._client: BleakClient | None = None
        self._stream = DumlStream()
        self._sequence = 0x1000
        self._pending: dict[int, tuple[int, int, asyncio.Future[DumlPacket]]] = {}
        self._state_callbacks: set[StateCallback] = set()
        self._disconnect_callbacks: set[DisconnectCallback] = set()
        self._report_event = asyncio.Event()
        self._operation_lock = asyncio.Lock()
        self._disconnecting = False
        self.data: dict[str, object] = {}

    @property
    def address(self) -> str:
        """Return the current platform BLE address."""
        return self._ble_device.address

    @property
    def is_connected(self) -> bool:
        """Return whether the underlying GATT client is connected."""
        return self._client is not None and self._client.is_connected

    def update_ble_device(self, ble_device: BLEDevice) -> None:
        """Use a fresher scanner object for the next connection."""
        self._ble_device = ble_device

    def add_state_listener(self, callback: StateCallback) -> Callable[[], None]:
        """Register a state listener and return its unsubscribe function."""
        self._state_callbacks.add(callback)
        return lambda: self._state_callbacks.discard(callback)

    def add_disconnect_listener(
        self, callback: DisconnectCallback
    ) -> Callable[[], None]:
        """Register an unexpected-disconnect listener."""
        self._disconnect_callbacks.add(callback)
        return lambda: self._disconnect_callbacks.discard(callback)

    def _next_sequence(self) -> int:
        self._sequence = (self._sequence + 1) & 0xFFFF
        return self._sequence

    def _merge_data(self, update: dict[str, object]) -> None:
        changed = any(self.data.get(key) != value for key, value in update.items())
        if not changed:
            return
        self.data.update(update)
        snapshot = dict(self.data)
        for callback in tuple(self._state_callbacks):
            callback(snapshot)

    def _on_notify(self, _characteristic: object, chunk: bytearray) -> None:
        for packet in self._stream.feed(chunk):
            self._handle_packet(packet)

    def _handle_packet(self, packet: DumlPacket) -> None:
        pending = self._pending.get(packet.sequence)
        if pending is not None:
            command_set, command_id, future = pending
            if (
                packet.command_set == command_set
                and packet.command_id == command_id
                and packet.is_response
                and not future.done()
            ):
                future.set_result(packet)

        if packet.command_set != POWER_COMMAND_SET:
            return
        try:
            if packet.command_id == REPORT_COMMAND:
                update = parse_report(packet.payload)
                if update:
                    self._merge_data(update)
                    self._report_event.set()
            elif packet.command_id == TELEMETRY_COMMAND:
                update = parse_telemetry(packet.payload)
                if update:
                    self._merge_data(update)
            elif packet.command_id == HMS_COMMAND:
                # The dy302+ HMS body is still capture-gated. Preserve it in
                # diagnostics without inventing an entity schema.
                self._merge_data({"hms_raw": packet.payload.hex()})
        except ProtocolError as error:
            _LOGGER.debug(
                "%s: ignored malformed 0x%02x push: %s",
                self.address,
                packet.command_id,
                error,
            )

    def _on_disconnect(self, _client: BleakClient) -> None:
        self._client = None
        error = DjiPowerDisconnectedError(f"{self.address} disconnected")
        for _, _, future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()
        if self._disconnecting:
            return
        for callback in tuple(self._disconnect_callbacks):
            callback(error)

    async def _establish(self) -> BleakClient:
        return await establish_connection(
            BleakClientWithServiceCache,
            self._ble_device,
            self._name,
            disconnected_callback=self._on_disconnect,
            max_attempts=3,
        )

    async def connect(self) -> None:
        """Connect, subscribe, authenticate, and fetch initial state."""
        if self.is_connected:
            return
        self._disconnecting = False
        self._stream.clear()
        self._report_event.clear()
        try:
            async with asyncio.timeout(DEFAULT_CONNECT_TIMEOUT):
                await self._connect_and_initialize()
        except TimeoutError as error:
            await self.disconnect()
            raise DjiPowerError(
                f"connection timed out after {DEFAULT_CONNECT_TIMEOUT:.0f} seconds"
            ) from error

    async def _connect_and_initialize(self) -> None:
        """Establish and initialize the link within the caller's deadline."""
        client = await self._establish()
        try:
            await client.start_notify(NOTIFY_UUID, self._on_notify)
        except BleakError:
            # Partial service discovery can poison BlueZ's cache. This mirrors
            # the recovery used by the DJI app and by mature HA BLE integrations.
            _LOGGER.debug("%s: clearing incomplete GATT cache", self.address)
            with contextlib.suppress(AttributeError, BleakError):
                await client.clear_cache()
            self._disconnecting = True
            try:
                with contextlib.suppress(BleakError):
                    await client.disconnect()
            finally:
                self._disconnecting = False
            client = await self._establish()
            try:
                await client.start_notify(NOTIFY_UUID, self._on_notify)
            except Exception:
                self._disconnecting = True
                try:
                    with contextlib.suppress(BleakError):
                        await client.disconnect()
                finally:
                    self._disconnecting = False
                raise
        self._client = client
        try:
            await self._authenticate()
            await self.refresh_config()
            try:
                async with asyncio.timeout(5):
                    await self._report_event.wait()
            except TimeoutError:
                # Config state is enough to set up; the periodic report may be
                # delayed on an idle or older station.
                _LOGGER.debug(
                    "%s: no initial 0x61 push within five seconds", self.address
                )
        except Exception:
            await self.disconnect()
            raise

    async def disconnect(self) -> None:
        """Cleanly close the persistent link."""
        client = self._client
        if client is None:
            return
        self._disconnecting = True
        self._client = None
        try:
            with contextlib.suppress(BleakError, EOFError):
                await client.stop_notify(NOTIFY_UUID)
            with contextlib.suppress(BleakError):
                await client.disconnect()
        finally:
            self._disconnecting = False

    async def _request(
        self,
        command_id: int,
        payload: bytes,
        *,
        timeout: float = DEFAULT_REQUEST_TIMEOUT,
    ) -> DumlPacket:
        client = self._client
        if client is None or not client.is_connected:
            raise DjiPowerDisconnectedError(f"{self.address} is not connected")

        sequence = self._next_sequence()
        packet = DumlPacket(
            APP_SOURCE,
            POWER_DESTINATION,
            sequence,
            0x20,
            POWER_COMMAND_SET,
            command_id,
            payload,
        )
        future = asyncio.get_running_loop().create_future()
        self._pending[sequence] = (POWER_COMMAND_SET, command_id, future)
        try:
            await client.write_gatt_char(WRITE_UUID, packet.encode(), response=True)
            async with asyncio.timeout(timeout):
                return await future
        except TimeoutError as error:
            raise DjiPowerError(
                f"timeout waiting for 0x{command_id:02x} response"
            ) from error
        finally:
            self._pending.pop(sequence, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                # Mark a disconnect exception retrieved if the GATT write itself
                # raised before this coroutine got as far as awaiting the future.
                future.exception()

    async def _authenticate(self) -> None:
        challenge = await self._request(AUTH_COMMAND, bytes((START_BIND,)))
        if challenge.payload[:1] != b"\x00" or len(challenge.payload) < 5:
            raise DjiPowerAuthenticationError(
                "station returned an invalid auth challenge"
            )
        material = challenge.payload[1:5] + self._pair_key + b"\x00"
        result = await self._request(
            AUTH_COMMAND, bytes((CHECK_SECRET_KEY,)) + material
        )
        if result.payload[:1] != b"\x00":
            raise DjiPowerAuthenticationError("station rejected the pair key")

    async def refresh_config(self) -> None:
        """Fetch and publish a keyed configuration snapshot."""
        for module in (0x01, 0x04):
            response = await self._request(GET_COMMAND, bytes((0x00, module, 0x10)))
            try:
                update = parse_telemetry(response.payload)
            except ProtocolError as error:
                raise DjiPowerError("station returned malformed config data") from error
            self._merge_data(update)

    async def _set(self, payload: bytes, expected_keys: tuple[int, ...]) -> None:
        response = await self._request(SET_COMMAND, payload)
        try:
            parse_set_ack(response.payload, expected_keys)
        except ProtocolError as error:
            raise DjiPowerError(str(error)) from error

    async def _wait_for_values(self, expected: dict[str, object]) -> None:
        for _ in range(8):
            await asyncio.sleep(2)
            await self.refresh_config()
            if all(self.data.get(key) == value for key, value in expected.items()):
                return
        raise DjiPowerError("station did not report the requested values")

    async def set_ac(self, enabled: bool) -> None:
        """Set AC output and wait for a matching readback."""
        async with self._operation_lock:
            await self._set(
                build_ac_set_payload(enabled), (POWER_SWITCH_KEY, RULES_KEY)
            )
            await self._wait_for_values({"ac_enabled": enabled})

    async def set_charge_limits(
        self,
        *,
        discharge_limit: int | None = None,
        recharge_limit: int | None = None,
    ) -> None:
        """Set one or both energy-management limits."""
        async with self._operation_lock:
            current = self.data.get("key_05")
            old_discharge = self.data.get("discharge_limit")
            old_recharge = self.data.get("recharge_limit")
            if (
                not isinstance(current, str)
                or not isinstance(old_discharge, int)
                or not isinstance(old_recharge, int)
            ):
                raise DjiPowerError("charge-limit state is unavailable")
            requested_discharge = (
                old_discharge if discharge_limit is None else discharge_limit
            )
            requested_recharge = (
                old_recharge if recharge_limit is None else recharge_limit
            )
            try:
                payload = build_charge_limits_set_payload(
                    current, requested_discharge, requested_recharge
                )
            except ProtocolError as error:
                raise DjiPowerError(str(error)) from error
            await self._set(payload, (0x05,))
            await self._wait_for_values(
                {
                    "discharge_limit": requested_discharge,
                    "recharge_limit": requested_recharge,
                }
            )
