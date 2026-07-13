"""AC output switch (cmd 0x63 keyed SET, key 0x0d).

Verified by btsnoop capture of the DJI Home app plus a live A/B test driving the
station from a non-phone BLE client with only the pair_key.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import DjiPowerEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([DjiPowerAcSwitch(coordinator)])


class DjiPowerAcSwitch(DjiPowerEntity, SwitchEntity):
    _attr_device_class = SwitchDeviceClass.OUTLET
    _attr_name = "AC output"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.data[CONF_ADDRESS]}_ac_output"

    @property
    def is_on(self) -> bool | None:
        return (self.coordinator.data or {}).get("ac_enabled")

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_ac(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_ac(False)
