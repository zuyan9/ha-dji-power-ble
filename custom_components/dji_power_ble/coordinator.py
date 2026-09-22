"""Home Assistant bridge for the persistent DJI Power device client."""

from __future__ import annotations

import asyncio
import logging

from bleak.exc import BleakError
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_KEEP_CONNECTION,
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
        return dict(self.device.data)

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
