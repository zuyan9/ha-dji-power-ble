"""Home Assistant bridge for the persistent DJI Power device client."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from bleak.exc import BleakError
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_KEEP_CONNECTION,
    CONF_RECHARGE_POWER_MINIMUM,
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
)
from .device import DjiPowerDevice, DjiPowerError, DjiPowerScheduleChangedError

_LOGGER = logging.getLogger(__name__)


class DjiPowerCoordinator(DataUpdateCoordinator[dict[str, object]]):
    """Expose device pushes through HA's coordinator entity machinery."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device: DjiPowerDevice,
    ) -> None:
        super().__init__(hass, _LOGGER, name=f"{DOMAIN} {device.address}")
        self.entry = entry
        self.device = device
        self._closed = False
        self._publish_interval = float(
            entry.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)
        )
        self._last_push = 0.0
        self._pending_data: dict[str, object] | None = None
        self._push_timer: asyncio.TimerHandle | None = None
        # The adapter or proxy HA lists as holding the connection, and the signal
        # strength of the station's last advertisement there: once connected, the
        # station stops advertising.
        self.bluetooth_source: str | None = None
        self.bluetooth_rssi: int | None = None
        self._bluetooth_source_id: str | None = None
        self._unsub_allocations: Callable[[], None] | None = None
        device.set_charge_power_minimum(entry.options.get(CONF_RECHARGE_POWER_MINIMUM))
        self._unsub_state = device.add_state_listener(self._handle_state)
        self._unsub_disconnect = device.add_disconnect_listener(self._handle_disconnect)

    @callback
    def async_apply_options(self) -> None:
        """Apply compatible options without interrupting the Bluetooth session."""
        if self._closed:
            return
        self.device.keep_connection = self.entry.options.get(
            CONF_KEEP_CONNECTION, False
        )
        self.device.set_charge_power_minimum(
            self.entry.options.get(CONF_RECHARGE_POWER_MINIMUM)
        )
        interval = float(
            self.entry.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)
        )
        if interval == self._publish_interval:
            return
        self._publish_interval = interval
        if self._push_timer is not None:
            self._push_timer.cancel()
            self._push_timer = None
        if self._pending_data is not None:
            self._handle_state(self._pending_data)

    @callback
    def _handle_state(self, data: dict[str, object]) -> None:
        if self._closed:
            return
        now = self.hass.loop.time()
        remaining = self._publish_interval - (now - self._last_push)
        if remaining <= 0:
            self._publish(data)
            return
        self._pending_data = data
        if self._push_timer is None:
            self._push_timer = self.hass.loop.call_later(remaining, self._flush_pending)

    @callback
    def _publish(self, data: dict[str, object]) -> None:
        if self._closed:
            return
        self._last_push = self.hass.loop.time()
        self._pending_data = None
        if self._push_timer is not None:
            self._push_timer.cancel()
            self._push_timer = None
        self.async_set_updated_data(data)

    @callback
    def _flush_pending(self) -> None:
        if self._push_timer is not None:
            self._push_timer.cancel()
        self._push_timer = None
        if self._pending_data is not None:
            # A cancelled timer can already be queued when options change.
            # Recheck the current interval before publishing buffered data.
            self._handle_state(self._pending_data)

    @callback
    def _handle_disconnect(self, error: Exception | None) -> None:
        if self._closed:
            return
        # A queued pre-disconnect snapshot must not make entities available again.
        self._pending_data = None
        if self._push_timer is not None:
            self._push_timer.cancel()
            self._push_timer = None
        self.async_set_update_error(
            UpdateFailed(str(error) if error else "Bluetooth connection lost")
        )
        # Scheduling a reload while the entry is still setting up deadlocks the
        # setup task against its own reload. Initial-refresh failures already
        # become ConfigEntryNotReady and are retried by HA.
        if self.entry.state is ConfigEntryState.LOADED:
            self.hass.config_entries.async_schedule_reload(self.entry.entry_id)

    async def _async_update_data(self) -> dict[str, object]:
        """Establish the initial link; later updates arrive as pushes."""
        if self._closed:
            raise UpdateFailed("Bluetooth client is shutting down")
        try:
            await self.device.connect()
        except (BleakError, DjiPowerError, TimeoutError) as error:
            raise UpdateFailed(str(error)) from error
        self._async_track_bluetooth_source()
        return dict(self.device.data)

    @callback
    def _async_track_bluetooth_source(self) -> None:
        """Follow HA's connection allocations, which proxies can report late."""
        from habluetooth import get_manager

        if self._unsub_allocations is None:
            manager = get_manager()
            self._unsub_allocations = manager.async_register_allocation_callback(
                self._handle_allocations
            )
        self._async_update_bluetooth_source()

    @callback
    def _handle_allocations(self, _allocations: object) -> None:
        if not self._closed and self._async_update_bluetooth_source():
            self.async_update_listeners()

    @callback
    def _async_update_bluetooth_source(self) -> bool:
        """Find the source holding the connection; return whether it changed."""
        from habluetooth import get_manager
        from homeassistant.components import bluetooth

        address = self.device.address.upper()
        source = next(
            (
                allocation.source
                for allocation in get_manager().async_current_allocations() or ()
                if address in (item.upper() for item in allocation.allocated)
            ),
            None,
        )
        if source == self._bluetooth_source_id:
            return False
        self._bluetooth_source_id = source
        self.bluetooth_source = self.bluetooth_rssi = None
        if source is not None:
            scanner = bluetooth.async_scanner_by_source(self.hass, source)
            self.bluetooth_source = scanner.name if scanner is not None else source
            self.bluetooth_rssi = next(
                (
                    found.advertisement.rssi
                    for found in bluetooth.async_scanner_devices_by_address(
                        self.hass, address, connectable=True
                    )
                    if found.scanner.source == source
                ),
                None,
            )
        return True

    async def async_shutdown(self) -> None:
        """Release the client before HA cancels background Bluetooth tasks."""
        await self.async_disconnect(keep_connection=self.device.keep_connection)

    @callback
    def async_release_device(self) -> None:
        """Release coordinator callbacks while another owner keeps the device."""
        if self._closed:
            return
        self._closed = True
        self._unsub_state()
        self._unsub_disconnect()
        if self._unsub_allocations is not None:
            self._unsub_allocations()
            self._unsub_allocations = None
        self._pending_data = None
        if self._push_timer is not None:
            self._push_timer.cancel()
            self._push_timer = None

    async def async_disconnect(self, *, keep_connection: bool = False) -> None:
        """Unsubscribe callbacks and close the BLE link."""
        if self._closed:
            return
        self.async_release_device()
        if keep_connection:
            await self.device.disconnect(keep_connection=True)
        else:
            await self.device.disconnect()

    async def async_set_ac(self, enabled: bool) -> None:
        """Set AC output, converting library failures to HA service errors."""
        try:
            await self.device.set_ac(enabled)
        except DjiPowerError as error:
            raise HomeAssistantError(str(error)) from error
        self._publish(dict(self.device.data))

    async def async_set_sdc(
        self, interface_type: int, seq: int, enabled: bool
    ) -> None:
        """Set an SDC switch and publish its confirmed state immediately."""
        try:
            await self.device.set_sdc(interface_type, seq, enabled)
        except DjiPowerError as error:
            raise HomeAssistantError(str(error)) from error
        self._publish(dict(self.device.data))

    async def async_set_port_switch(
        self, interface_type: int, seq: int, enabled: bool
    ) -> None:
        """Set a reported output and publish its confirmed state."""
        try:
            await self.device.set_port_switch(interface_type, seq, enabled)
        except DjiPowerError as error:
            raise HomeAssistantError(str(error)) from error
        self._publish(dict(self.device.data))

    async def async_set_usb(
        self, interface_type: int, seq: int, enabled: bool
    ) -> None:
        """Set a USB output and publish its confirmed state immediately."""
        try:
            await self.device.set_usb(interface_type, seq, enabled)
        except DjiPowerError as error:
            raise HomeAssistantError(str(error)) from error
        self._publish(dict(self.device.data))

    async def async_set_car_charger(
        self,
        interface_type: int,
        seq: int,
        accessory_type: int,
        **changes: bool | int | float,
    ) -> None:
        """Set one charger control and publish the confirmed device state."""
        try:
            await self.device.set_car_charger(
                interface_type, seq, accessory_type, **changes
            )
        except DjiPowerError as error:
            raise HomeAssistantError(str(error)) from error
        self._publish(dict(self.device.data))

    async def async_set_charge_limits(
        self,
        *,
        discharge_limit: int | None = None,
        recharge_limit: int | None = None,
    ) -> None:
        """Set energy-management limits."""
        try:
            await self.device.set_charge_limits(
                discharge_limit=discharge_limit,
                recharge_limit=recharge_limit,
            )
        except DjiPowerError as error:
            raise HomeAssistantError(str(error)) from error
        self._publish(dict(self.device.data))

    async def async_set_energy_reserve(
        self, *, enabled: bool | None = None, percent: int | None = None
    ) -> None:
        """Set the custom backup reserve and publish its confirmed readback."""
        try:
            await self.device.set_energy_reserve(enabled=enabled, percent=percent)
        except DjiPowerError as error:
            raise HomeAssistantError(str(error)) from error
        self._publish(dict(self.device.data))

    async def async_set_discharge_power(self, watts: int) -> None:
        """Set fixed discharge power and publish its confirmed readback."""
        try:
            await self.device.set_discharge_power(watts)
        except DjiPowerError as error:
            raise HomeAssistantError(str(error)) from error
        self._publish(dict(self.device.data))

    async def async_set_charge_power(self, watts: int) -> None:
        """Set recharge power and publish its confirmed readback."""
        try:
            await self.device.set_charge_power(watts)
        except DjiPowerError as error:
            raise HomeAssistantError(str(error)) from error
        self._publish(dict(self.device.data))

    async def async_set_power_adjustment(self, mode: str) -> None:
        """Set power adjustment and publish its confirmed readback."""
        try:
            await self.device.set_power_adjustment(mode)
        except DjiPowerError as error:
            raise HomeAssistantError(str(error)) from error
        self._publish(dict(self.device.data))

    async def async_set_energy_saver_mode(self, mode: str) -> None:
        """Select an Energy Saver mode and publish its confirmed readback."""
        try:
            await self.device.set_energy_saver_mode(mode)
        except DjiPowerError as error:
            raise HomeAssistantError(str(error)) from error
        self._publish(dict(self.device.data))

    async def async_set_auto_resume(self, enabled: bool) -> None:
        """Set Energy Saver Auto Resume and publish its confirmed readback."""
        try:
            await self.device.set_auto_resume(enabled)
        except DjiPowerError as error:
            raise HomeAssistantError(str(error)) from error
        self._publish(dict(self.device.data))

    async def async_set_scheduled_switch(self, switch: str, enabled: bool) -> None:
        """Set a Scheduled Periods switch and publish its confirmed readback."""
        try:
            await self.device.set_scheduled_switch(switch, enabled)
        except DjiPowerError as error:
            raise HomeAssistantError(str(error)) from error
        self._publish(dict(self.device.data))

    async def async_set_off_peak_charge_power(self, watts: int) -> None:
        """Set off-peak charging watts and publish their confirmed readback."""
        try:
            await self.device.set_off_peak_charge_power(watts)
        except DjiPowerError as error:
            raise HomeAssistantError(str(error)) from error
        self._publish(dict(self.device.data))

    async def async_get_time_periods(self) -> list[dict[str, object]]:
        """Read tariff periods for editing and publish the fresh station state."""
        try:
            periods = await self.device.get_time_periods()
        except DjiPowerError as error:
            raise HomeAssistantError(str(error)) from error
        self._publish(dict(self.device.data))
        return periods

    async def async_set_time_periods(
        self, periods: object, *, expected_periods: object | None = None
    ) -> None:
        """Replace tariff periods and publish the confirmed station settings."""
        try:
            if expected_periods is None:
                await self.device.set_time_periods(periods)
            else:
                await self.device.set_time_periods(
                    periods, expected_periods=expected_periods
                )
        except DjiPowerScheduleChangedError as error:
            raise HomeAssistantError(
                str(error),
                translation_domain=DOMAIN,
                translation_key="schedule_changed",
            ) from error
        except DjiPowerError as error:
            raise HomeAssistantError(str(error)) from error
        self._publish(dict(self.device.data))
