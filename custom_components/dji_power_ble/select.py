"""Power-adjustment mode from keyed configuration."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import DjiPowerEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    if coordinator.device.model == "DJI Power 2000":
        async_add_entities([DjiPowerAdjustmentSelect(coordinator)])


class DjiPowerAdjustmentSelect(DjiPowerEntity, SelectEntity):
    """Select automatic or manual power adjustment on Power 2000."""

    _attr_name = "Power adjustment"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = ["Manual", "Automatic"]

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = (
            f"{coordinator.entry.data[CONF_ADDRESS]}_power_adjustment"
        )

    @property
    def available(self) -> bool:
        return super().available and self.current_option is not None

    @property
    def current_option(self) -> str | None:
        option = (self.coordinator.data or {}).get("power_adjustment")
        return option if option in self._attr_options else None

    async def async_select_option(self, option: str) -> None:
        if option not in self._attr_options:
            raise ValueError("power adjustment must be Manual or Automatic")
        await self.coordinator.async_set_power_adjustment(option)
