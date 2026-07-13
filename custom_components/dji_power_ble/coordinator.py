"""Home Assistant bridge for the persistent DJI Power device client."""

from __future__ import annotations

import asyncio
import logging

from bleak.exc import BleakError
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL, DOMAIN
from .device import (
    DjiPowerAuthenticationError,
    DjiPowerDevice,
    DjiPowerError,
)

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
        self._publish_interval = float(
            entry.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)
        )
        self._last_push = 0.0
        self._pending_data: dict[str, object] | None = None
        self._push_timer: asyncio.TimerHandle | None = None
        self._unsub_state = device.add_state_listener(self._handle_state)
        self._unsub_disconnect = device.add_disconnect_listener(self._handle_disconnect)

    @callback
    def _handle_state(self, data: dict[str, object]) -> None:
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
        self._last_push = self.hass.loop.time()
        self._pending_data = None
        if self._push_timer is not None:
            self._push_timer.cancel()
            self._push_timer = None
        self.async_set_updated_data(data)

    @callback
    def _flush_pending(self) -> None:
        self._push_timer = None
        if self._pending_data is not None:
            self._publish(self._pending_data)

    @callback
    def _handle_disconnect(self, error: Exception | None) -> None:
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
        try:
            await self.device.connect()
        except DjiPowerAuthenticationError as error:
            raise UpdateFailed("station rejected the pair key") from error
        except (BleakError, DjiPowerError, TimeoutError) as error:
            raise UpdateFailed(str(error)) from error
        return dict(self.device.data)

    async def async_disconnect(self) -> None:
        """Unsubscribe callbacks and close the BLE link."""
        self._unsub_state()
        self._unsub_disconnect()
        if self._push_timer is not None:
            self._push_timer.cancel()
            self._push_timer = None
        await self.device.disconnect()

    async def async_set_ac(self, enabled: bool) -> None:
        """Set AC output, converting library failures to HA service errors."""
        try:
            await self.device.set_ac(enabled)
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
