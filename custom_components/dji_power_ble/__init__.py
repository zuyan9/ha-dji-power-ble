"""DJI Power local BLE integration."""

from __future__ import annotations

import logging
from collections.abc import Callable

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import (
    BluetoothCallbackMatcher,
    BluetoothChange,
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import (
    CONF_MODEL,
    CONF_PAIR_KEY,
    CONF_SERIAL_NUMBER,
    DOMAIN,
    MANUFACTURER_ID,
)
from .coordinator import DjiPowerCoordinator
from .device import DjiPowerDevice
from .duml import ProtocolError, parse_manufacturer_data

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BINARY_SENSOR, Platform.NUMBER, Platform.SENSOR, Platform.SWITCH]

_REAPPEAR_CALLBACKS_KEY = f"{DOMAIN}_reappear_callbacks"
_MAX_ADVERTISEMENT_AGE = 60.0


def _register_reappear_callback(
    hass: HomeAssistant, entry: ConfigEntry, address: str
) -> None:
    """Reload the entry once its single connectable advert returns."""
    callbacks: dict[str, Callable[[], None]] = hass.data.setdefault(
        _REAPPEAR_CALLBACKS_KEY, {}
    )
    if entry.entry_id in callbacks:
        return

    def _on_device_reappear(
        service_info: BluetoothServiceInfoBleak,
        change: BluetoothChange,
    ) -> None:
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


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up and retain one authenticated local-push connection."""
    address = entry.data[CONF_ADDRESS]
    discovery_info = bluetooth.async_last_service_info(hass, address, connectable=True)
    if (
        not bluetooth.async_address_present(hass, address)
        or discovery_info is None
        or hass.loop.time() - discovery_info.time > _MAX_ADVERTISEMENT_AGE
    ):
        _register_reappear_callback(hass, entry, address)
        raise ConfigEntryNotReady(f"Device {address} has no recent advertisement")

    ble_device = bluetooth.async_ble_device_from_address(
        hass, address, connectable=True
    )
    if ble_device is None:
        _register_reappear_callback(hass, entry, address)
        raise ConfigEntryNotReady(f"No BLE device object for {address}")

    _cancel_reappear_callback(hass, entry)
    device = DjiPowerDevice(
        ble_device,
        entry.data[CONF_PAIR_KEY],
        name=entry.title,
        model=_model_from_discovery(entry, discovery_info),
        serial_number=entry.data.get(CONF_SERIAL_NUMBER),
    )
    coordinator = DjiPowerCoordinator(hass, entry, device)
    try:
        await coordinator.async_config_entry_first_refresh()
    except ConfigEntryNotReady:
        await coordinator.async_disconnect()
        _register_reappear_callback(hass, entry, address)
        raise

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    if not device.is_connected:
        await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
        hass.data[DOMAIN].pop(entry.entry_id, None)
        await coordinator.async_disconnect()
        _register_reappear_callback(hass, entry, address)
        raise ConfigEntryNotReady(f"Device {address} disconnected during setup")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload entities and close the BLE connection."""
    _cancel_reappear_callback(hass, entry)
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded and (
        coordinator := hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    ):
        await coordinator.async_disconnect()
    return unloaded
