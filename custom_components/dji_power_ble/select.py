"""Power-adjustment mode from keyed configuration."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .accessory import (
    AccessoryIdentity,
    DjiPowerCarChargerEntity,
    async_discover_accessories,
)
from .const import DOMAIN
from .entity import DjiPowerEntity
from .features import ModelFeature, supports_feature


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    if supports_feature(coordinator.device.model, ModelFeature.TOU_POWER_CONTROL):
        async_add_entities([DjiPowerAdjustmentSelect(coordinator)])
    async_discover_accessories(
        coordinator,
        entry,
        async_add_entities,
        {
            "car_chargers": lambda identity: [
                DjiPowerCarModeSelect(coordinator, identity)
            ]
        },
    )


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
            raise ServiceValidationError("power adjustment must be Manual or Automatic")
        await self.coordinator.async_set_power_adjustment(option)


class DjiPowerCarModeSelect(DjiPowerCarChargerEntity, SelectEntity):
    """Choose which direction a reported car recharger transfers power."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = ["Auto", "Recharge", "Charge"]

    def __init__(self, coordinator, identity: AccessoryIdentity) -> None:
        super().__init__(coordinator, identity, "car_mode", "car recharging mode")

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.charger_enabled
            and self.current_option is not None
        )

    @property
    def current_option(self) -> str | None:
        mode = (self.row or {}).get("mode")
        if type(mode) is int and 1 <= mode <= len(self._attr_options):
            return self._attr_options[mode - 1]
        return None

    async def async_select_option(self, option: str) -> None:
        if option not in self._attr_options:
            raise ServiceValidationError(
                "car recharging mode must be Auto, Recharge or Charge"
            )
        await self.async_set_charger(mode=self._attr_options.index(option) + 1)
