"""Diagnostics support for DJI Power BLE."""

from __future__ import annotations

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, CONF_NAME
from homeassistant.core import HomeAssistant

from .const import CONF_CONNECTION_SOURCE, CONF_PAIR_KEY, CONF_SERIAL_NUMBER, DOMAIN

CONFIG_TO_REDACT = {CONF_ADDRESS, CONF_NAME, CONF_PAIR_KEY, CONF_SERIAL_NUMBER}
STATE_TO_REDACT = {
    "key_01",
    "key_03",
    "key_04",
    "key_0e",
    "key_18",
    CONF_SERIAL_NUMBER,
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, object]:
    """Return protocol state without exposing the local credential."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    return {
        "config_entry": async_redact_data(dict(entry.data), CONFIG_TO_REDACT),
        "options": async_redact_data(dict(entry.options), {CONF_CONNECTION_SOURCE}),
        "connected": coordinator.device.is_connected,
        "state": async_redact_data(dict(coordinator.data or {}), STATE_TO_REDACT),
    }
