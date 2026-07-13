"""Diagnostics support for DJI Power BLE."""

from __future__ import annotations

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant

from .const import CONF_PAIR_KEY, CONF_SERIAL_NUMBER, DOMAIN

CONFIG_TO_REDACT = {CONF_ADDRESS, CONF_PAIR_KEY, CONF_SERIAL_NUMBER}
STATE_TO_REDACT = {"key_0e"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, object]:
    """Return protocol state without exposing the local credential."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    return {
        "config_entry": async_redact_data(dict(entry.data), CONFIG_TO_REDACT),
        "options": dict(entry.options),
        "connected": coordinator.device.is_connected,
        "state": async_redact_data(dict(coordinator.data or {}), STATE_TO_REDACT),
    }
