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
    CAR_CHARGERS_KEY,
    CHARGE_LIMIT_KEY,
    CHECK_SECRET_KEY,
    ECO_MODE_KEY,
    EXPANSION_BATTERIES_KEY,
    GET_COMMAND,
    HMS_COMMAND,
    NOTIFY_UUID,
    POWER_1000_ENCRYPTION_TYPE,
    POWER_COMMAND_SET,
    POWER_DESTINATION,
    POWER_SWITCH_KEY,
    REPORT_COMMAND,
    RULES_KEY,
    SET_COMMAND,
    START_BIND,
    TELEMETRY_COMMAND,
    TIME_PERIODS_KEY,
    WRITE_UUID,
    DumlPacket,
    DumlStream,
    ProtocolError,
    build_ac_set_payload,
    build_car_charger_set_payload,
    build_charge_limits_set_payload,
    build_charge_power_set_payload,
    build_discharge_power_set_payload,
    build_power_adjustment_set_payload,
    build_sdc_switch_set_payload,
    build_time_periods_set_payload,
    decrypt_power_1000_payload,
    encrypt_power_1000_payload,
    normalize_pair_key,
    normalize_time_periods,
    parse_keyed_values,
    parse_report,
    parse_set_ack,
    parse_telemetry,
    validate_time_periods_mode,
)
from .features import ModelFeature, supports_feature

_LOGGER = logging.getLogger(__name__)

DEFAULT_REQUEST_TIMEOUT = 8.0
DEFAULT_CONNECT_TIMEOUT = 30.0
READBACK_RETRIES = 8
READBACK_RETRY_INTERVAL = 2.0
EXPANSION_REFRESH_INTERVAL = 30.0
EXPANSION_MODELS = {"DJI Power 1000", "DJI Power 1000 V2", "DJI Power 2000"}


class DjiPowerError(Exception):
    """Base device/transport error."""


class DjiPowerAuthenticationError(DjiPowerError):
    """The station rejected the challenge or local pair key."""


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
        self._encrypted_transport = model == "DJI Power 1000"
        self.serial_number = serial_number
        self._client: BleakClient | None = None
        self._stream = DumlStream()
        self._sequence = 0x1000
        self._pending: dict[int, tuple[int, int, asyncio.Future[DumlPacket]]] = {}
        self._state_callbacks: set[StateCallback] = set()
        self._disconnect_callbacks: set[DisconnectCallback] = set()
        self._report_event = asyncio.Event()
        self._operation_lock = asyncio.Lock()
        self._expansion_refresh_task: asyncio.Task[None] | None = None
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
                update = parse_report(self._decode_payload(packet))
                if update:
                    self._merge_data(update)
                    self._report_event.set()
            elif packet.command_id == TELEMETRY_COMMAND:
                update = parse_telemetry(self._decode_payload(packet))
                if update:
                    self._merge_data(update)
            elif packet.command_id == HMS_COMMAND:
                # The dy302+ HMS body is still capture-gated. Preserve it in
                # diagnostics without inventing an entity schema.
                self._merge_data({"hms_raw": self._decode_payload(packet).hex()})
        except ProtocolError as error:
            if packet.command_id == TELEMETRY_COMMAND:
                invalidated = {"expansion_batteries": None}
                if supports_feature(self.model, ModelFeature.SDC_CONTROLS):
                    invalidated.update(
                        car_chargers=None,
                        key_0a=None,
                        power_switches=None,
                        key_0d=None,
                        ac_enabled=None,
                    )
                if supports_feature(self.model, ModelFeature.TARIFF_SCHEDULE):
                    invalidated.update(time_periods=None, key_16=None)
                self._merge_data(invalidated)
            _LOGGER.debug(
                "%s: ignored malformed 0x%02x push: %s",
                self.address,
                packet.command_id,
                error,
            )

    def _decode_payload(self, packet: DumlPacket) -> bytes:
        """Decode a wire payload before interpreting command-specific fields."""
        if packet.encryption_type == 0:
            return packet.payload
        if (
            packet.encryption_type == POWER_1000_ENCRYPTION_TYPE
            and self._encrypted_transport
        ):
            return decrypt_power_1000_payload(packet.payload)
        raise ProtocolError(
            f"unsupported payload encryption type {packet.encryption_type} "
            f"for {self.model}"
        )

    def _on_disconnect(self, _client: BleakClient) -> None:
        self._client = None
        if self._expansion_refresh_task is not None:
            self._expansion_refresh_task.cancel()
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
            self._start_expansion_refresh()
        except TimeoutError as error:
            await self.disconnect()
            raise DjiPowerError(
                f"connection timed out after {DEFAULT_CONNECT_TIMEOUT:.0f} seconds"
            ) from error
        except BaseException:
            await self.disconnect()
            raise

    async def _connect_and_initialize(self) -> None:
        """Establish and initialize the link within the caller's deadline."""
        client = await self._establish()
        self._client = client
        try:
            await client.start_notify(NOTIFY_UUID, self._on_notify)
        except BleakError:
            # Partial service discovery can poison BlueZ's cache. This mirrors
            # the recovery used by the DJI app and by mature HA BLE integrations.
            _LOGGER.debug("%s: clearing incomplete GATT cache", self.address)
            with contextlib.suppress(AttributeError, BleakError):
                await client.clear_cache()
            await self.disconnect()
            client = await self._establish()
            self._client = client
            await client.start_notify(NOTIFY_UUID, self._on_notify)
        await self._authenticate()
        await self.refresh_config()
        try:
            async with asyncio.timeout(5):
                await self._report_event.wait()
        except TimeoutError:
            # Config state is enough to set up; the periodic report may be
            # delayed on an idle or older station.
            _LOGGER.debug("%s: no initial 0x61 push within five seconds", self.address)

    async def disconnect(self) -> None:
        """Cleanly close the persistent link."""
        task = self._expansion_refresh_task
        self._expansion_refresh_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        client = self._client
        if client is None:
            return
        self._disconnecting = True
        self._client = None
        try:
            # Bleak stops notifications as part of disconnecting.
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
        flags = 0x20
        if self._encrypted_transport:
            payload = encrypt_power_1000_payload(payload)
            flags |= POWER_1000_ENCRYPTION_TYPE
        packet = DumlPacket(
            APP_SOURCE,
            POWER_DESTINATION,
            sequence,
            flags,
            POWER_COMMAND_SET,
            command_id,
            payload,
        )
        future = asyncio.get_running_loop().create_future()
        self._pending[sequence] = (POWER_COMMAND_SET, command_id, future)
        try:
            async with asyncio.timeout(timeout):
                await client.write_gatt_char(WRITE_UUID, packet.encode(), response=True)
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

    def _log_auth_response(
        self, stage: str, packet: DumlPacket, payload: bytes | None
    ) -> None:
        """Log response metadata without credentials, nonces, or device identifiers."""
        _LOGGER.debug(
            "DJI Power auth response: stage=%s status=%s payload_length=%d "
            "source=0x%02x destination=0x%02x flags=0x%02x sequence=%d "
            "decoded_payload_length=%s",
            stage,
            (payload[:1].hex() or "missing") if payload is not None else "encrypted",
            len(packet.payload),
            packet.source,
            packet.destination,
            packet.flags,
            packet.sequence,
            len(payload) if payload is not None else "unknown",
        )

    def _auth_payload(self, stage: str, packet: DumlPacket) -> bytes:
        try:
            payload = self._decode_payload(packet)
        except ProtocolError as error:
            self._log_auth_response(stage, packet, None)
            raise DjiPowerError(f"cannot decode {stage} response: {error}") from error
        self._log_auth_response(stage, packet, payload)
        return payload

    async def _authenticate(self) -> None:
        challenge = await self._request(AUTH_COMMAND, bytes((START_BIND,)))
        challenge_payload = self._auth_payload("start_bind", challenge)
        if challenge_payload[:1] != b"\x00" or len(challenge_payload) < 5:
            raise DjiPowerAuthenticationError(
                "station returned an invalid auth challenge"
            )
        material = challenge_payload[1:5] + self._pair_key + b"\x00"
        result = await self._request(
            AUTH_COMMAND, bytes((CHECK_SECRET_KEY,)) + material
        )
        result_payload = self._auth_payload("check_secret_key", result)
        if result_payload[:1] != b"\x00":
            raise DjiPowerAuthenticationError("station rejected the pair key")

    async def refresh_config(self) -> None:
        """Fetch and publish a keyed configuration snapshot."""
        await self._read_expansion_batteries()
        await self._read_config(0x04)
        if supports_feature(self.model, ModelFeature.TOU_POWER_CONTROL):
            try:
                await self._read_eco_mode()
            except DjiPowerDisconnectedError:
                raise
            except DjiPowerError as error:
                # Older firmware may omit or reject this optional setting.
                _LOGGER.debug("Eco-mode configuration unavailable: %s", error)
        if supports_feature(self.model, ModelFeature.TARIFF_SCHEDULE):
            try:
                await self._read_time_periods()
            except DjiPowerDisconnectedError:
                raise
            except DjiPowerError as error:
                _LOGGER.debug("Time-period configuration unavailable: %s", error)

    def _start_expansion_refresh(self) -> None:
        """Refresh optional packs and accessories over the existing session."""
        if self.model not in EXPANSION_MODELS or not self.is_connected:
            return
        if (
            self._expansion_refresh_task is None
            or self._expansion_refresh_task.done()
        ):
            self._expansion_refresh_task = asyncio.create_task(
                self._refresh_expansion_loop(), name="dji_power_expansion_refresh"
            )

    async def _refresh_expansion_loop(self) -> None:
        try:
            # Discover optional controls after setup, so an unsupported request
            # cannot consume the connection's initialization deadline.
            async with self._operation_lock:
                await self._refresh_accessory_config()
            while self.is_connected:
                await asyncio.sleep(EXPANSION_REFRESH_INTERVAL)
                async with self._operation_lock:
                    await self._read_expansion_batteries()
                    await self._refresh_accessory_config()
        except DjiPowerDisconnectedError:
            return

    async def _refresh_accessory_config(self) -> None:
        """Discover attached controls without making optional failures fatal."""
        if not supports_feature(self.model, ModelFeature.SDC_CONTROLS):
            return
        for key, state_key in (
            (CAR_CHARGERS_KEY, "car_chargers"),
            (POWER_SWITCH_KEY, "power_switches"),
        ):
            try:
                await self._read_accessory_config(key, state_key)
            except DjiPowerDisconnectedError:
                raise
            except DjiPowerError as error:
                _LOGGER.debug("Accessory configuration unavailable: %s", error)

    async def _read_accessory_config(
        self, key: int, state_key: str
    ) -> dict[str, object]:
        """Require a fresh complete list and invalidate failed snapshots."""
        raw_key = f"key_{key:02x}"
        try:
            async with asyncio.timeout(DEFAULT_REQUEST_TIMEOUT):
                update = await self._read_config(key)
            if not isinstance(update.get(raw_key), str) or not isinstance(
                update.get(state_key), list
            ):
                raise DjiPowerError(f"station omitted valid {state_key} configuration")
        except DjiPowerDisconnectedError:
            # The disconnect callback owns availability; do not republish here.
            raise
        except (DjiPowerError, BleakError, EOFError, TimeoutError) as error:
            if not self.is_connected:
                raise DjiPowerDisconnectedError("Bluetooth connection lost") from error
            invalidated = {raw_key: None, state_key: None}
            if key == POWER_SWITCH_KEY:
                invalidated["ac_enabled"] = None
            self._merge_data(invalidated)
            raise DjiPowerError(
                f"cannot read {state_key} configuration: {str(error) or 'timed out'}"
            ) from error
        return update

    async def _read_expansion_batteries(self) -> None:
        """Read a fresh pack list; optional failures invalidate only pack state."""
        try:
            # Bound the write as well as the response so a stalled GATT write
            # cannot hold the operation lock and leave old pack readings live.
            async with asyncio.timeout(DEFAULT_REQUEST_TIMEOUT):
                update = await self._read_config(EXPANSION_BATTERIES_KEY)
        except DjiPowerDisconnectedError:
            raise
        except (DjiPowerError, BleakError, EOFError, TimeoutError) as error:
            if not self.is_connected:
                raise DjiPowerDisconnectedError("Bluetooth connection lost") from error
            self._merge_data({"expansion_batteries": None})
            _LOGGER.debug(
                "Expansion-battery snapshot unavailable: %s",
                str(error) or "read timed out",
            )
            return
        if "expansion_batteries" not in update:
            self._merge_data({"expansion_batteries": None})

    async def _read_config(self, key: int) -> dict[str, object]:
        response = await self._request(GET_COMMAND, bytes((0x00, key, 0x10)))
        try:
            update = parse_telemetry(self._decode_payload(response))
        except ProtocolError as error:
            raise DjiPowerError("station returned malformed config data") from error
        self._merge_data(update)
        return update

    async def _read_eco_mode(self) -> str:
        """Require a fresh eco-mode record rather than reusing cached settings."""
        try:
            update = await self._read_config(ECO_MODE_KEY)
            current = update.get("key_18")
            if not isinstance(current, str):
                raise DjiPowerError("station omitted eco-mode configuration")
        except DjiPowerDisconnectedError:
            # The disconnect callback already marks the coordinator unavailable.
            # Publishing state here would mark its last update successful again.
            raise
        except DjiPowerError:
            self._merge_data(
                {
                    "key_18": None,
                    "power_adjustment": None,
                    "charge_power_available": False,
                    "charge_power_w": None,
                    "charge_power_min_w": None,
                    "charge_power_max_w": None,
                    "discharge_power_available": False,
                    "discharge_power_w": None,
                    "discharge_power_min_w": None,
                    "discharge_power_max_w": None,
                }
            )
            raise
        return current

    async def _wait_for_eco_mode_values(self, expected: dict[str, object]) -> None:
        for attempt in range(READBACK_RETRIES + 1):
            if attempt:
                await asyncio.sleep(READBACK_RETRY_INTERVAL)
            await self._read_eco_mode()
            if all(self.data.get(key) == value for key, value in expected.items()):
                return
        raise DjiPowerError("station did not report the requested eco-mode values")

    async def _read_time_periods(self) -> list[dict[str, object]]:
        """Require a valid fresh schedule, including an explicit empty list."""
        try:
            update = await self._read_config(TIME_PERIODS_KEY)
            periods = update.get("time_periods")
            if not isinstance(periods, list):
                raise DjiPowerError("station omitted or returned invalid time periods")
        except DjiPowerDisconnectedError:
            raise
        except (DjiPowerError, BleakError, EOFError) as error:
            if not self.is_connected:
                raise DjiPowerDisconnectedError("Bluetooth connection lost") from error
            self._merge_data({"time_periods": None, "key_16": None})
            raise DjiPowerError(str(error)) from error
        return periods

    async def set_time_periods(self, periods: object) -> None:
        """Replace Power 2000 tariff periods and confirm a fresh matching list."""
        if not supports_feature(self.model, ModelFeature.TARIFF_SCHEDULE):
            raise DjiPowerError("time-period control is only enabled for Power 2000")
        try:
            requested = normalize_time_periods(periods)
        except ProtocolError as error:
            raise DjiPowerError(str(error)) from error
        async with self._operation_lock:
            current = await self._read_time_periods()
            eco_mode = await self._read_eco_mode()
            try:
                validate_time_periods_mode(eco_mode, clearing=not requested)
            except ProtocolError as error:
                raise DjiPowerError(str(error)) from error
            if requested == current:
                return
            await self._set(
                build_time_periods_set_payload(requested),
                (TIME_PERIODS_KEY, RULES_KEY),
            )
            for attempt in range(READBACK_RETRIES + 1):
                if attempt:
                    await asyncio.sleep(READBACK_RETRY_INTERVAL)
                if await self._read_time_periods() == requested:
                    return
            raise DjiPowerError("station did not report the requested time periods")

    async def _set(self, payload: bytes, expected_keys: tuple[int, ...]) -> None:
        response = await self._request(SET_COMMAND, payload)
        try:
            parse_set_ack(self._decode_payload(response), expected_keys)
        except ProtocolError as error:
            raise DjiPowerError(str(error)) from error

    async def _wait_for_values(
        self, expected: dict[str, object], *, config_key: int
    ) -> None:
        """Confirm from a fresh targeted read, delaying only subsequent attempts."""
        for attempt in range(READBACK_RETRIES + 1):
            if attempt:
                await asyncio.sleep(READBACK_RETRY_INTERVAL)
            update = await self._read_config(config_key)
            if all(
                key in update and update[key] == value
                for key, value in expected.items()
            ):
                return
        raise DjiPowerError("station did not report the requested values")

    async def set_ac(self, enabled: bool) -> None:
        """Set AC output and wait for a matching readback."""
        async with self._operation_lock:
            update = await self._read_accessory_config(
                POWER_SWITCH_KEY, "power_switches"
            )
            try:
                payload = build_ac_set_payload(
                    enabled, current_value=update["key_0d"]
                )
            except ProtocolError as error:
                raise DjiPowerError(str(error)) from error
            await self._set(payload, (POWER_SWITCH_KEY, RULES_KEY))
            await self._wait_for_accessory_values(
                POWER_SWITCH_KEY, "power_switches",
                {"type": 2, "seq": 1}, {"sw": 1 if enabled else 2},
            )

    @staticmethod
    def _accessory_row(
        update: dict[str, object], state_key: str, identity: dict[str, int]
    ) -> dict[str, int]:
        rows = update.get(state_key)
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict) and all(
                    row.get(key) == value for key, value in identity.items()
                ):
                    return row
        raise DjiPowerError("the requested accessory is no longer available")

    async def _wait_for_accessory_values(
        self,
        config_key: int,
        state_key: str,
        identity: dict[str, int],
        expected: dict[str, int],
    ) -> None:
        """Confirm the addressed accessory from fresh reads, never cached rows."""
        for attempt in range(READBACK_RETRIES + 1):
            if attempt:
                await asyncio.sleep(READBACK_RETRY_INTERVAL)
            update = await self._read_accessory_config(config_key, state_key)
            row = self._accessory_row(update, state_key, identity)
            if all(row.get(key) == value for key, value in expected.items()):
                return
        raise DjiPowerError("accessory did not report the requested values")

    async def set_sdc(self, interface_type: int, seq: int, enabled: bool) -> None:
        """Set a reported SDC switch while retaining all other switch rows."""
        if not supports_feature(self.model, ModelFeature.SDC_CONTROLS):
            raise DjiPowerError("SDC controls are not supported on this model")
        async with self._operation_lock:
            update = await self._read_accessory_config(
                POWER_SWITCH_KEY, "power_switches"
            )
            try:
                payload = build_sdc_switch_set_payload(
                    update["key_0d"], interface_type, seq, enabled
                )
            except ProtocolError as error:
                raise DjiPowerError(str(error)) from error
            await self._set(payload, tuple(parse_keyed_values(payload)))
            await self._wait_for_accessory_values(
                POWER_SWITCH_KEY, "power_switches",
                {"type": interface_type, "seq": seq},
                {"sw": 1 if enabled else 2},
            )

    async def set_car_charger(
        self,
        interface_type: int,
        seq: int,
        accessory_type: int,
        *,
        enabled: bool | None = None,
        mode: int | None = None,
        recharge_power_w: int | None = None,
        minimum_voltage_v: float | None = None,
    ) -> None:
        """Edit one reported charger setting and confirm its addressed row."""
        if not supports_feature(self.model, ModelFeature.SDC_CONTROLS):
            raise DjiPowerError("car-charger controls are not supported on this model")
        identity = {
            "interface_type": interface_type, "seq": seq, "type": accessory_type
        }
        async with self._operation_lock:
            update = await self._read_accessory_config(CAR_CHARGERS_KEY, "car_chargers")
            try:
                payload = build_car_charger_set_payload(
                    update["key_0a"], interface_type, seq, accessory_type,
                    enabled=enabled, mode=mode, recharge_power_w=recharge_power_w,
                    minimum_voltage_v=minimum_voltage_v,
                )
            except ProtocolError as error:
                raise DjiPowerError(str(error)) from error
            requested = self._accessory_row(
                parse_telemetry(payload), "car_chargers", identity
            )
            fields = [
                field for field, value in (
                    ("sw", enabled), ("mode", mode),
                    ("p_from_car_v", recharge_power_w),
                    ("v_from_car_v", minimum_voltage_v),
                ) if value is not None
            ]
            if recharge_power_w is not None or minimum_voltage_v is not None:
                fields.extend(("sw", "mode"))
            expected = {field: requested[field] for field in fields}
            await self._set(payload, tuple(parse_keyed_values(payload)))
            await self._wait_for_accessory_values(
                CAR_CHARGERS_KEY, "car_chargers", identity, expected
            )

    async def set_charge_limits(
        self,
        *,
        discharge_limit: int | None = None,
        recharge_limit: int | None = None,
    ) -> None:
        """Set one or both energy-management limits."""
        async with self._operation_lock:
            update = await self._read_config(CHARGE_LIMIT_KEY)
            current = update.get("key_05")
            old_discharge = update.get("discharge_limit")
            old_recharge = update.get("recharge_limit")
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
            await self._set(payload, (CHARGE_LIMIT_KEY,))
            await self._wait_for_values(
                {
                    "discharge_limit": requested_discharge,
                    "recharge_limit": requested_recharge,
                },
                config_key=CHARGE_LIMIT_KEY,
            )

    async def set_charge_power(self, watts: int) -> None:
        """Set Power 2000 manual recharge watts and require matching readback."""
        if not supports_feature(self.model, ModelFeature.TOU_POWER_CONTROL):
            raise DjiPowerError(
                "charge-power control is only enabled for Power 2000"
            )
        async with self._operation_lock:
            current = await self._read_eco_mode()
            try:
                payload = build_charge_power_set_payload(current, watts)
            except ProtocolError as error:
                raise DjiPowerError(str(error)) from error
            await self._set(payload, (ECO_MODE_KEY,))
            await self._wait_for_eco_mode_values(
                {"charge_power_available": True, "charge_power_w": watts}
            )

    async def set_discharge_power(self, watts: int) -> None:
        """Set Power 2000 manual discharge watts and require matching readback."""
        if not supports_feature(self.model, ModelFeature.TOU_POWER_CONTROL):
            raise DjiPowerError(
                "discharge-power control is only enabled for Power 2000"
            )
        async with self._operation_lock:
            current = await self._read_eco_mode()
            try:
                payload = build_discharge_power_set_payload(current, watts)
            except ProtocolError as error:
                raise DjiPowerError(str(error)) from error
            await self._set(payload, (ECO_MODE_KEY,))
            await self._wait_for_eco_mode_values(
                {"discharge_power_available": True, "discharge_power_w": watts}
            )

    async def set_power_adjustment(self, mode: str) -> None:
        """Select Automatic/Manual power adjustment and confirm its readback."""
        if not supports_feature(self.model, ModelFeature.TOU_POWER_CONTROL):
            raise DjiPowerError(
                "power-adjustment control is only enabled for Power 2000"
            )
        async with self._operation_lock:
            current = await self._read_eco_mode()
            try:
                payload = build_power_adjustment_set_payload(current, mode)
            except ProtocolError as error:
                raise DjiPowerError(str(error)) from error
            await self._set(payload, (ECO_MODE_KEY,))
            await self._wait_for_eco_mode_values({"power_adjustment": mode})
