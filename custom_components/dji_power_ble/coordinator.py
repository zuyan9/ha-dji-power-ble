"""Connect to a DJI Power station over BLE, authenticate, poll telemetry."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from bleak import BleakClient
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DEFAULT_SCAN_INTERVAL, DOMAIN
from .duml import (
    APP_SOURCE,
    AUTH_COMMAND,
    CHECK_SECRET_KEY,
    NOTIFY_UUID,
    POWER_COMMAND_SET,
    POWER_DESTINATION,
    REPORT_COMMAND,
    SET_COMMAND,
    START_BIND,
    TELEMETRY_COMMAND,
    WRITE_UUID,
    DumlPacket,
    build_ac_set_payload,
    build_charge_limits_set_payload,
    extract_packets,
    normalize_pair_key,
    parse_report,
    parse_telemetry,
)

_LOGGER = logging.getLogger(__name__)


class DjiPowerCoordinator(DataUpdateCoordinator[dict]):
    """Polls the station: connect -> auth (0x6a) -> telemetry (0x62) -> parse."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, address: str, pair_key: str) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} {address}",
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self.entry = entry
        self.address = address.upper()
        self._pair_key = normalize_pair_key(pair_key)
        self._seq = 0x1000
        self._buffer = bytearray()
        self._queue: asyncio.Queue[DumlPacket] = asyncio.Queue()
        # One BLE link at a time: serialise scheduled polls and on-demand writes.
        self._lock = asyncio.Lock()

    def _next_seq(self) -> int:
        self._seq = (self._seq + 1) & 0xFFFF
        return self._seq

    def _on_notify(self, _char, data: bytearray) -> None:
        self._buffer += bytes(data)
        for packet in extract_packets(self._buffer):
            self._queue.put_nowait(packet)

    async def _request(self, client: BleakClient, command_id: int, payload: bytes,
                       match_id: int, timeout: float = 8.0) -> DumlPacket:
        pkt = DumlPacket(APP_SOURCE, POWER_DESTINATION, self._next_seq(), 0x20,
                         POWER_COMMAND_SET, command_id, payload)
        await client.write_gatt_char(WRITE_UUID, pkt.encode(), response=True)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise UpdateFailed(f"timeout waiting for 0x{match_id:02x} reply")
            packet = await asyncio.wait_for(self._queue.get(), remaining)
            if packet.command_set == POWER_COMMAND_SET and packet.command_id == match_id:
                return packet

    async def _wait_for(self, match_id: int, timeout: float = 5.0) -> DumlPacket:
        """Wait for an unsolicited push (e.g. the 0x61 report) from the notify queue."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError
            packet = await asyncio.wait_for(self._queue.get(), remaining)
            if packet.command_set == POWER_COMMAND_SET and packet.command_id == match_id:
                return packet

    async def _connect(self) -> BleakClient:
        """Open a fresh notify-subscribed BLE link with empty reassembly state."""
        device = bluetooth.async_ble_device_from_address(self.hass, self.address, connectable=True)
        if device is None:
            raise UpdateFailed(f"{self.address} not in BLE range")
        self._buffer.clear()
        while not self._queue.empty():
            self._queue.get_nowait()
        client = await establish_connection(BleakClientWithServiceCache, device, self.address)
        try:
            await client.start_notify(NOTIFY_UUID, self._on_notify)
        except BleakError:
            # A weak/partial first connection can cache a GATT service list that
            # is missing the notify characteristic. Without this, every reconnect
            # reuses the stale cache and fails until Home Assistant is restarted.
            # Clear the cache, force a fresh discovery, and try once more.
            _LOGGER.debug("%s: notify char missing; clearing GATT cache", self.address)
            try:
                await client.clear_cache()
            except Exception:  # noqa: BLE001
                pass
            await client.disconnect()
            client = await establish_connection(BleakClientWithServiceCache, device, self.address)
            await client.start_notify(NOTIFY_UUID, self._on_notify)
        return client

    async def _authenticate(self, client: BleakClient) -> None:
        """0x6a auth: startBind -> NONCE, then checkSecretKey(NONCE + key + 00)."""
        challenge = await self._request(client, AUTH_COMMAND, bytes((START_BIND,)), AUTH_COMMAND)
        if challenge.payload[:1] != b"\x00" or len(challenge.payload) < 5:
            raise UpdateFailed("unexpected auth challenge")
        material = challenge.payload[1:5] + self._pair_key + b"\x00"
        result = await self._request(
            client, AUTH_COMMAND, bytes((CHECK_SECRET_KEY,)) + material, AUTH_COMMAND)
        if result.payload[:1] != b"\x00":
            raise UpdateFailed("station rejected pair_key")

    @staticmethod
    async def _close(client: BleakClient) -> None:
        try:
            await client.stop_notify(NOTIFY_UUID)
        except Exception:  # noqa: BLE001
            pass
        await client.disconnect()

    async def _async_update_data(self) -> dict:
        async with self._lock:
            client = await self._connect()
            try:
                await self._authenticate(client)
                # 0x62 telemetry = config (firmware/region/AC state); 0x61 = live push.
                telemetry = await self._request(client, TELEMETRY_COMMAND, b"\x01", TELEMETRY_COMMAND)
                data = parse_telemetry(telemetry.payload)
                try:
                    report = await self._wait_for(REPORT_COMMAND, timeout=5.0)
                    data.update(parse_report(report.payload))
                except (UpdateFailed, TimeoutError):
                    pass  # battery/power unavailable this cycle; config still returned
                return data
            finally:
                await self._close(client)

    async def _async_set_payload(self, payload: bytes) -> None:
        """Send one keyed 0x63 SET body."""
        async with self._lock:
            client = await self._connect()
            try:
                await self._authenticate(client)
                ack = await self._request(client, SET_COMMAND, payload, SET_COMMAND)
                if ack.payload[:1] not in (b"\x00", b"\x01"):
                    raise UpdateFailed(f"station rejected SET: {ack.payload.hex()}")
            finally:
                await self._close(client)

    async def _async_refresh_after_set(self, expected: dict[str, object] | None = None) -> None:
        """Refresh after the station's asynchronous 0x63 apply has settled."""
        for _ in range(8):
            await asyncio.sleep(2.0)
            await self.async_request_refresh()
            if expected is None or all((self.data or {}).get(key) == value
                                       for key, value in expected.items()):
                return
        raise UpdateFailed("station did not report the requested SET values")

    async def async_set_ac(self, enabled: bool) -> None:
        """Turn AC output on/off over BLE (cmd 0x63 keyed SET), then refresh state."""
        await self._async_set_payload(build_ac_set_payload(enabled))
        await self._async_refresh_after_set()

    async def async_set_charge_limits(
        self, *, discharge_limit: int | None = None, recharge_limit: int | None = None
    ) -> None:
        """Set energy-management limits over BLE while preserving unknown fields."""
        data = dict(self.data or {})
        current_value = data.get("key_05")
        current_discharge = data.get("discharge_limit")
        current_recharge = data.get("recharge_limit")
        if not isinstance(current_value, str) or not isinstance(current_discharge, int) \
                or not isinstance(current_recharge, int):
            raise UpdateFailed("charge-limit state is unavailable")
        requested_discharge = current_discharge if discharge_limit is None else discharge_limit
        requested_recharge = current_recharge if recharge_limit is None else recharge_limit
        try:
            payload = build_charge_limits_set_payload(
                current_value, requested_discharge, requested_recharge
            )
        except ValueError as error:
            raise UpdateFailed(str(error)) from error
        await self._async_set_payload(payload)
        await self._async_refresh_after_set(
            {"discharge_limit": requested_discharge, "recharge_limit": requested_recharge}
        )
