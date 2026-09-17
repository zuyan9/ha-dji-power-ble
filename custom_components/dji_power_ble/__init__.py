"""DJI Power local BLE integration."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import (
    BluetoothCallbackMatcher,
    BluetoothChange,
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HassJob, HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import (
    CONF_CONNECTION_SOURCE,
    CONF_KEEP_CONNECTION,
    CONF_MODEL,
    CONF_PAIR_KEY,
    CONF_SERIAL_NUMBER,
    CONNECTION_SOURCE_AUTOMATIC,
    DOMAIN,
    MANUFACTURER_ID,
)
from .coordinator import DjiPowerCoordinator
from .device import DjiPowerDevice
from .duml import ProtocolError, parse_manufacturer_data

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

_REAPPEAR_CALLBACKS_KEY = f"{DOMAIN}_reappear_callbacks"
_SHUTDOWN_CALLBACKS_KEY = f"{DOMAIN}_shutdown_callbacks"
_RETAINED_CONNECTIONS_KEY = f"{DOMAIN}_retained_connections"
_MAX_ADVERTISEMENT_AGE = 60.0
_RELOAD_HANDOFF_TIMEOUT = 60.0


@dataclass
class _RetainedConnection:
    """Own a live device between unload and the next setup, or until cleanup."""

    device: DjiPowerDevice
    timer: asyncio.TimerHandle | None = None
    cancel_shutdown: Callable[[], None] | None = None
    closing_task: asyncio.Task[None] | None = None


def _local_adapter(entry: ConfigEntry) -> str | None:
    source = entry.options.get(CONF_CONNECTION_SOURCE, CONNECTION_SOURCE_AUTOMATIC)
    return None if source == CONNECTION_SOURCE_AUTOMATIC else source.upper()


def _matches_connection(entry: ConfigEntry, device: DjiPowerDevice) -> bool:
    """Compare the new entry settings with the actual connected device."""
    model = entry.data.get(CONF_MODEL)
    if not model or model == "DJI Power":
        model = device.model
    return device.matches_connection(
        entry.data[CONF_ADDRESS], entry.data[CONF_PAIR_KEY],
        local_adapter=_local_adapter(entry), model=model,
    )


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Apply ordinary options live; reconnect only when connection settings change."""
    if coordinator := hass.data.get(DOMAIN, {}).get(entry.entry_id):
        if _matches_connection(entry, coordinator.device):
            coordinator.async_apply_options()
        else:
            hass.config_entries.async_schedule_reload(entry.entry_id)


def _start_retained_close(
    hass: HomeAssistant, entry_id: str, retained: _RetainedConnection, *,
    keep_connection: bool = False, from_shutdown: bool = False,
) -> asyncio.Task[None] | None:
    """Start cleanup once, keeping it reachable until the old link is closed."""
    connections = hass.data.get(_RETAINED_CONNECTIONS_KEY, {})
    if connections.get(entry_id) is not retained:
        return None
    if retained.closing_task is not None:
        return retained.closing_task
    if retained.timer is not None:
        retained.timer.cancel()
    if retained.cancel_shutdown is not None:
        # HA iterates its shutdown job list while starting these coroutines.
        # Removing this job from inside itself could skip the next job.
        if not from_shutdown:
            retained.cancel_shutdown()
        retained.cancel_shutdown = None

    def finished(task: asyncio.Task[None]) -> None:
        if connections.get(entry_id) is retained:
            connections.pop(entry_id)
        if not task.cancelled() and task.exception() is not None:
            _LOGGER.warning("Could not close the retained Bluetooth client")

    close = (
        retained.device.disconnect(keep_connection=True)
        if keep_connection else retained.device.disconnect()
    )
    retained.closing_task = hass.async_create_task(close)
    retained.closing_task.add_done_callback(finished)
    return retained.closing_task


async def _async_close_retained(
    hass: HomeAssistant, entry_id: str, *,
    keep_connection: bool = False, from_shutdown: bool = False,
) -> None:
    """Close an unclaimed device, including any cleanup already in progress."""
    if retained := hass.data.get(_RETAINED_CONNECTIONS_KEY, {}).get(entry_id):
        task = _start_retained_close(
            hass, entry_id, retained, keep_connection=keep_connection,
            from_shutdown=from_shutdown,
        )
        if task is not None:
            await asyncio.shield(task)


def _retain_for_reload(
    hass: HomeAssistant, entry: ConfigEntry, device: DjiPowerDevice
) -> None:
    """Keep the live client briefly so the next coordinator can take ownership."""
    retained = _RetainedConnection(device)

    async def shutdown() -> None:
        await _async_close_retained(
            hass, entry.entry_id, keep_connection=True, from_shutdown=True
        )

    retained.cancel_shutdown = hass.async_add_shutdown_job(HassJob(shutdown))
    try:
        retained.timer = hass.loop.call_later(
            _RELOAD_HANDOFF_TIMEOUT, _start_retained_close,
            hass, entry.entry_id, retained,
        )
    except BaseException:
        retained.cancel_shutdown()
        raise
    hass.data.setdefault(_RETAINED_CONNECTIONS_KEY, {})[entry.entry_id] = retained


async def _async_take_retained(
    hass: HomeAssistant, entry: ConfigEntry
) -> DjiPowerDevice | None:
    """Claim a compatible session, or finish closing it before a new connection."""
    connections = hass.data.get(_RETAINED_CONNECTIONS_KEY, {})
    retained = connections.get(entry.entry_id)
    if retained is None:
        return None
    if (
        retained.closing_task is not None
        or entry.disabled_by is not None
        or not entry.options.get(CONF_KEEP_CONNECTION, False)
        or not retained.device.can_retain_connection
        or not _matches_connection(entry, retained.device)
    ):
        await _async_close_retained(hass, entry.entry_id)
        return None
    # No await between claiming the device and disarming both cleanup paths.
    connections.pop(entry.entry_id)
    if retained.timer is not None:
        retained.timer.cancel()
    if retained.cancel_shutdown is not None:
        retained.cancel_shutdown()
    return retained.device


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register actions even when no station is currently loaded."""
    from .services import async_setup_services

    async_setup_services(hass)
    return True


def _register_reappear_callback(
    hass: HomeAssistant, entry: ConfigEntry, address: str,
    local_adapter: str | None = None,
) -> None:
    """Reload the entry once its single connectable advert returns."""
    callbacks: dict[str, Callable[[], None]] = hass.data.setdefault(
        _REAPPEAR_CALLBACKS_KEY, {}
    )
    if entry.entry_id in callbacks:
        return
    registered_at = hass.loop.time()

    def _on_device_reappear(
        service_info: BluetoothServiceInfoBleak,
        change: BluetoothChange,
    ) -> None:
        # HA replays cached advertisements while registering the callback.
        if service_info.time <= registered_at:
            return
        if local_adapter is not None and service_info.source.upper() != local_adapter:
            return
        _LOGGER.info("Device %s reappeared; scheduling reload", address)
        _cancel_reappear_callback(hass, entry)
        hass.config_entries.async_schedule_reload(entry.entry_id)

    callbacks[entry.entry_id] = bluetooth.async_register_callback(
        hass,
        _on_device_reappear,
        BluetoothCallbackMatcher(address=address, connectable=True),
        BluetoothScanningMode.PASSIVE,
    )


def _cancel_reappear_callback(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Cancel a pending advertisement callback."""
    callbacks: dict[str, Callable[[], None]] = hass.data.get(
        _REAPPEAR_CALLBACKS_KEY, {}
    )
    if cancel := callbacks.pop(entry.entry_id, None):
        cancel()


def _cancel_shutdown_callback(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove Core-only shutdown handling on ordinary entry unload."""
    callbacks: dict[str, Callable[[], None]] = hass.data.get(
        _SHUTDOWN_CALLBACKS_KEY, {}
    )
    if cancel := callbacks.pop(entry.entry_id, None):
        cancel()


def _model_from_discovery(
    entry: ConfigEntry, discovery_info: BluetoothServiceInfoBleak | None
) -> str:
    configured = entry.data.get(CONF_MODEL)
    if isinstance(configured, str) and configured and configured != "DJI Power":
        return configured
    if discovery_info is not None:
        manufacturer_data = discovery_info.manufacturer_data.get(MANUFACTURER_ID)
        if manufacturer_data:
            try:
                return parse_manufacturer_data(manufacturer_data).model
            except ProtocolError:
                pass
    return configured if isinstance(configured, str) and configured else "DJI Power"


async def _async_create_device(
    hass: HomeAssistant, entry: ConfigEntry
) -> DjiPowerDevice:
    """Resolve the selected source when there is no live client to reuse."""
    address = entry.data[CONF_ADDRESS].upper()
    local_adapter = _local_adapter(entry)
    keep_connection = entry.options.get(CONF_KEEP_CONNECTION, False)
    allocate_slot = release_slot = None
    discovery_info = bluetooth.async_last_service_info(hass, address, connectable=True)
    model = _model_from_discovery(entry, discovery_info)
    if keep_connection and local_adapter is None:
        raise ConfigEntryNotReady(
            "Connection retention requires a selected local adapter"
        )
    if local_adapter is not None:
        # BlueZ can retain an already-connected device which no longer
        # advertises. Resolve ONLY the explicitly selected adapter.
        from bleak.exc import BleakError
        from habluetooth import get_manager

        from .local_ble import async_local_device

        try:
            ble_device = await async_local_device(local_adapter, address)
        except (BleakError, OSError, RuntimeError) as error:
            raise ConfigEntryNotReady(
                "Selected local Bluetooth adapter unavailable"
            ) from error
        if ble_device is None:
            _register_reappear_callback(hass, entry, address, local_adapter)
            raise ConfigEntryNotReady(
                "Station unavailable on the selected local Bluetooth adapter"
            )
        manager = get_manager()
        allocate_slot = manager.async_allocate_connection_slot
        release_slot = manager.async_release_connection_slot
    elif (
        not bluetooth.async_address_present(hass, address)
        or discovery_info is None
        or hass.loop.time() - discovery_info.time > _MAX_ADVERTISEMENT_AGE
    ):
        _register_reappear_callback(hass, entry, address)
        raise ConfigEntryNotReady(f"Device {address} has no recent advertisement")
    else:
        ble_device = bluetooth.async_ble_device_from_address(
            hass, address, connectable=True
        )
        if ble_device is None:
            _register_reappear_callback(hass, entry, address)
            raise ConfigEntryNotReady(f"No BLE device object for {address}")

    _cancel_reappear_callback(hass, entry)
    return DjiPowerDevice(
        ble_device,
        entry.data[CONF_PAIR_KEY],
        name=entry.title,
        model=model,
        serial_number=entry.data.get(CONF_SERIAL_NUMBER),
        local_adapter=local_adapter,
        keep_connection=keep_connection,
        allocate_connection_slot=allocate_slot,
        release_connection_slot=release_slot,
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a coordinator, reusing a live client during a compatible reload."""
    device = None
    coordinator = None
    platforms_started = False
    try:
        device = await _async_take_retained(hass, entry)
        if device is None:
            device = await _async_create_device(hass, entry)
        _cancel_reappear_callback(hass, entry)
        coordinator = DjiPowerCoordinator(hass, entry, device)
        await coordinator.async_config_entry_first_refresh()
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
        platforms_started = True
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        if not device.is_connected:
            raise ConfigEntryNotReady(
                f"Device {device.address} disconnected during setup"
            )
        if (
            device.model != "DJI Power"
            and entry.data.get(CONF_MODEL) in (None, "DJI Power")
        ):
            # Retained connections may not advertise at the next HA startup.
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_MODEL: device.model}
            )
        entry.async_on_unload(entry.add_update_listener(_async_options_updated))
        hass.data.setdefault(_SHUTDOWN_CALLBACKS_KEY, {})[entry.entry_id] = (
            hass.async_add_shutdown_job(HassJob(coordinator.async_shutdown))
        )
    except BaseException as error:
        try:
            if platforms_started:
                await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
        finally:
            hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
            _cancel_shutdown_callback(hass, entry)
            if coordinator is not None:
                await coordinator.async_disconnect()
            elif device is not None:
                await device.disconnect()
        if isinstance(error, ConfigEntryNotReady):
            _register_reappear_callback(
                hass, entry, entry.data[CONF_ADDRESS].upper(), _local_adapter(entry)
            )
        raise
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload entities and hand a compatible local connection to the next setup."""
    _cancel_reappear_callback(hass, entry)
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        _cancel_shutdown_callback(hass, entry)
    if unloaded and (
        coordinator := hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    ):
        device = coordinator.device
        if (
            entry.disabled_by is None
            and entry.options.get(CONF_KEEP_CONNECTION, False)
            and device.can_retain_connection
            and _matches_connection(entry, device)
        ):
            coordinator.async_release_device()
            try:
                _retain_for_reload(hass, entry, device)
            except BaseException:
                await device.disconnect()
                raise
        else:
            await coordinator.async_disconnect()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Close an unclaimed reload session and remove all pending callbacks."""
    _cancel_reappear_callback(hass, entry)
    _cancel_shutdown_callback(hass, entry)
    await _async_close_retained(hass, entry.entry_id)
