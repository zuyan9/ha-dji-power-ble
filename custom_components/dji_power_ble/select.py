"""Energy Saver and power-adjustment modes from keyed configuration."""

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
    async_discover_feature,
)
from .const import DOMAIN
from .duml import ENERGY_SAVER_MODES
from .entity import DjiPowerEntity
from .features import ModelFeature, energy_saver_options, feature_available


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_discover_feature(
        coordinator, entry, async_add_entities, ModelFeature.ENERGY_SAVER,
        lambda: [DjiPowerEnergySaverModeSelect(coordinator)],
    )
    async_discover_feature(
        coordinator, entry, async_add_entities, ModelFeature.TOU_POWER_CONTROL,
        lambda: [DjiPowerAdjustmentSelect(coordinator)],
    )
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


class DjiPowerEnergySaverModeSelect(DjiPowerEntity, SelectEntity):
    """Select an Energy Saver mode among those set up in DJI Home."""

    _attr_translation_key = "energy_saver_mode"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = (
            f"{coordinator.entry.data[CONF_ADDRESS]}_energy_saver_mode"
        )

    @property
    def available(self) -> bool:
        return super().available and feature_available(
            self.coordinator.data or {}, ModelFeature.ENERGY_SAVER
        )

    @property
    def options(self) -> list[str]:
        # Grid-tied modes depend on station state; keep a valid list meanwhile.
        return energy_saver_options(self.coordinator.data or {}) or list(
            ENERGY_SAVER_MODES
        )

    @property
    def current_option(self) -> str | None:
        # Like DJI Home, other reported modes select nothing but can be changed.
        mode = (self.coordinator.data or {}).get("energy_saver_mode")
        return mode if mode in ENERGY_SAVER_MODES else None

    async def async_select_option(self, option: str) -> None:
        if option not in energy_saver_options(self.coordinator.data or {}):
            raise ServiceValidationError(
                "the station does not offer this Energy Saver mode; grid-tied "
                "modes appear after they are set up in DJI Home"
            )
        await self.coordinator.async_set_energy_saver_mode(option)


class DjiPowerAdjustmentSelect(DjiPowerEntity, SelectEntity):
    """Select automatic or manual power adjustment for grid-tied Time of Use."""

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
        return (
            super().available
            and feature_available(
                self.coordinator.data or {}, ModelFeature.TOU_POWER_CONTROL
            )
            and self.current_option is not None
        )

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
