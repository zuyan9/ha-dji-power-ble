"""Diagnostics support for DJI Power BLE."""

from __future__ import annotations

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, CONF_NAME
from homeassistant.core import HomeAssistant

from .const import CONF_CONNECTION_SOURCE, CONF_PAIR_KEY, CONF_SERIAL_NUMBER, DOMAIN
from .duml import eco_tail_offset

CONFIG_TO_REDACT = {CONF_ADDRESS, CONF_NAME, CONF_PAIR_KEY, CONF_SERIAL_NUMBER}
STATE_TO_REDACT = {
    "key_01",
    "key_03",
    "key_04",
    "key_0e",
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
# The eco-mode record is exported in parts; see _eco_mode_diagnostics.
PARTIAL_SETTINGS_TO_EXPORT = {"key_18"}


def _eco_mode_diagnostics(value: object) -> object:
    """Keep the eco-mode settings and layout but not the linked meter.

    Bytes 0-41 hold numeric settings. The meter ID follows (and in the 103-byte
    layout the meter brand), so only the known seven-byte tail is kept after it.
    """
    try:
        raw = bytes.fromhex(value) if isinstance(value, str) else None
    except ValueError:
        raw = None
    if raw is None:
        return None
    result: dict[str, object] = {"length": len(raw), "settings": raw[:42].hex()}
    if (offset := eco_tail_offset(raw)) is not None:
        result["tail"] = raw[offset : offset + 7].hex()
    return result


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, object]:
    """Return protocol state without exposing the local credential."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    state = dict(coordinator.data or {})
    if "key_18" in state:
        state["key_18"] = _eco_mode_diagnostics(state["key_18"])
    private_keys = STATE_TO_REDACT | {
        key for key in state
        if key.startswith("key_")
        and key not in RAW_SETTINGS_TO_EXPORT | PARTIAL_SETTINGS_TO_EXPORT
    }
    return {
        "config_entry": async_redact_data(dict(entry.data), CONFIG_TO_REDACT),
        "options": async_redact_data(dict(entry.options), {CONF_CONNECTION_SOURCE}),
        "connected": coordinator.device.is_connected,
        "state": async_redact_data(state, private_keys),
    }
