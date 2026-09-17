"""Actions that replace a station's complete electricity-price schedule."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN
from .duml import ProtocolError, normalize_time_periods
from .features import ModelFeature, supports_feature

SERVICE_SET_TIME_PERIODS = "set_time_periods"


def _validate_periods(value: object) -> list[dict[str, object]]:
    """Apply the same semantic checks to actions and direct device writes."""
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


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register actions independently of individual station connections."""

    async def async_set_time_periods(call: ServiceCall) -> None:
        device = dr.async_get(hass).async_get(call.data["device_id"])
        if device is None:
            raise ServiceValidationError("The selected device no longer exists")

        for entry_id in device.config_entries:
            coordinator = hass.data.get(DOMAIN, {}).get(entry_id)
            if coordinator is None or (
                DOMAIN, coordinator.device.address
            ) not in device.identifiers:
                continue
            if not supports_feature(
                coordinator.device.model, ModelFeature.TARIFF_SCHEDULE
            ):
                raise ServiceValidationError(
                    "Electricity price time periods are supported only on Power 2000"
                )
            if (
                not coordinator.last_update_success
                or not coordinator.device.is_connected
            ):
                raise ServiceValidationError(
                    "The selected power station is unavailable"
                )
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
