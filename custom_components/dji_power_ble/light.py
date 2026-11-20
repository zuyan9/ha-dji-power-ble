"""Station light: on, off, brightness and the SOS effect."""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_EFFECT,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util.color import brightness_to_value, value_to_brightness

from .accessory import async_discover_feature
from .const import DOMAIN
from .duml import parse_light
from .entity import DjiPowerEntity
from .features import ModelFeature

# Only the Power 1000 Mini has a light.
LIGHT_MODELS = frozenset({"DJI Power 1000 Mini"})
EFFECT_SOS = "SOS"
MODE_OFF, MODE_ON, MODE_SOS = 1, 2, 3


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    if coordinator.device.model not in LIGHT_MODELS:
        return
    async_discover_feature(
        coordinator, entry, async_add_entities, ModelFeature.LIGHT_CONTROLS,
        lambda: [DjiPowerLight(coordinator)],
    )


class DjiPowerLight(DjiPowerEntity, LightEntity):
    """The station's light, with brightness in the range the station reports."""

    _attr_name = "Light"
    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_supported_features = LightEntityFeature.EFFECT
    _attr_effect_list = [EFFECT_SOS]

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.data[CONF_ADDRESS]}_light"

    def _light(self) -> dict[str, int] | None:
        return parse_light((self.coordinator.data or {}).get("key_1b"))

    @property
    def available(self) -> bool:
        return super().available and self._light() is not None

    @property
    def is_on(self) -> bool | None:
        light = self._light()
        return light["mode"] in (MODE_ON, MODE_SOS) if light else None

    @property
    def effect(self) -> str | None:
        light = self._light()
        return EFFECT_SOS if light and light["mode"] == MODE_SOS else None

    @property
    def brightness(self) -> int | None:
        light = self._light()
        if light is None:
            return None
        return value_to_brightness(
            (light["minimum"], light["maximum"]), light["brightness"]
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        light = self._light()
        if light is None:
            raise ServiceValidationError("the station did not report its light")
        effect = kwargs.get(ATTR_EFFECT)
        if effect not in (None, EFFECT_SOS):
            raise ServiceValidationError("the light only supports the SOS effect")
        brightness = None
        if ATTR_BRIGHTNESS in kwargs:
            brightness = round(
                brightness_to_value(
                    (light["minimum"], light["maximum"]), kwargs[ATTR_BRIGHTNESS]
                )
            )
        await self.coordinator.async_set_light(
            mode=MODE_SOS if effect == EFFECT_SOS else MODE_ON,
            brightness=brightness,
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_light(mode=MODE_OFF)
