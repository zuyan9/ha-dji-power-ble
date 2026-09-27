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
# Only understood numeric settings may be exported as raw values. Discovery
# also reads identity-bearing and unknown records, which remain private.
RAW_SETTINGS_TO_EXPORT = {
    "key_00", "key_02", "key_05", "key_06", "key_07", "key_08", "key_09",
    "key_0a", "key_0b", "key_0c", "key_0d", "key_15", "key_16", "key_1b",
    "key_1c", "key_1d", "key_1e", "key_20", "key_21", "key_23", "key_24",
    "key_25", "key_26", "key_27", "key_28",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, object]:
    """Return protocol state without exposing the local credential."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    state = dict(coordinator.data or {})
    private_keys = STATE_TO_REDACT | {
        key for key in state
        if key.startswith("key_") and key not in RAW_SETTINGS_TO_EXPORT
    }
    return {
        "config_entry": async_redact_data(dict(entry.data), CONFIG_TO_REDACT),
        "options": async_redact_data(dict(entry.options), {CONF_CONNECTION_SOURCE}),
        "connected": coordinator.device.is_connected,
        "state": async_redact_data(state, private_keys),
    }
