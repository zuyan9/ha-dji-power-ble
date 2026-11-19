"""Actions that replace a station's complete electricity-price schedule."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN
from .duml import ProtocolError, normalize_time_periods
from .features import is_known_model

SERVICE_SET_TIME_PERIODS = "set_time_periods"


def _validate_periods(value: object) -> list[dict[str, object]]:
    """Accept HA time-selector values before applying schedule validation."""
    if isinstance(value, list):
        value = [
            dict(period) if isinstance(period, dict) else period for period in value
        ]
        for period in value:
            if not isinstance(period, dict):
                continue
            for field in ("start", "end"):
                time = period.get(field)
                if isinstance(time, str) and len(time) == 8 and time[5] == ":":
                    if time[6:] != "00":
                        raise vol.Invalid(
                            "period times must use whole minutes (00 seconds)"
                        )
                    period[field] = time[:5]
    try:
        return normalize_time_periods(value)
    except ProtocolError as error:
        raise vol.Invalid(str(error)) from error


SET_TIME_PERIODS_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): vol.All(str, vol.Length(min=1)),
        vol.Required("periods"): _validate_periods,
    }
)


def _is_station(device: dr.DeviceEntry, address: str) -> bool:
    """Match the station's own device, not one of its expansion batteries."""
    return any(
        domain == DOMAIN and identifier.upper() == address.upper()
        for domain, identifier in device.identifiers
    )


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register actions independently of individual station connections."""

    async def async_set_time_periods(call: ServiceCall) -> None:
        device = dr.async_get(hass).async_get(call.data["device_id"])
        if device is None:
            raise ServiceValidationError("The selected device no longer exists")

        for entry_id in device.config_entries:
            coordinator = hass.data.get(DOMAIN, {}).get(entry_id)
            if coordinator is None:
                # There is no coordinator while the entry reloads after a lost
                # connection or waits to retry setup.
                entry = hass.config_entries.async_get_entry(entry_id)
                if (
                    entry is not None
                    and entry.domain == DOMAIN
                    and _is_station(device, entry.data[CONF_ADDRESS])
                ):
                    raise ServiceValidationError(
                        "The selected power station is unavailable"
                    )
                continue
            if not _is_station(device, coordinator.device.address):
                continue
            if not is_known_model(coordinator.device.model):
                raise ServiceValidationError(
                    "Select a recognized DJI Power station model before "
                    "configuring electricity price time periods"
                )
            if (
                not coordinator.last_update_success
                or not coordinator.device.is_connected
            ):
                raise ServiceValidationError(
                    "The selected power station is unavailable"
                )
            # The device checks fresh schedule, Eco and rules records under its
            # operation lock; cached capability may still await discovery.
            await coordinator.async_set_time_periods(call.data["periods"])
            return

        raise ServiceValidationError(
            "Select a loaded DJI Power station, not an expansion battery"
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_TIME_PERIODS,
        async_set_time_periods,
        schema=SET_TIME_PERIODS_SCHEMA,
    )
