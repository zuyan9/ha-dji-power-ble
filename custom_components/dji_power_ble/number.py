"""Energy-management charge-limit controls (cmd 0x63 keyed SET, key 0x05)."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import DjiPowerEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            DjiPowerLimitNumber(
                coordinator, "discharge_limit", "Discharge limit", 0, 15
            ),
            DjiPowerLimitNumber(
                coordinator, "recharge_limit", "Recharge limit", 70, 100
            ),
        ]
    )


class DjiPowerLimitNumber(DjiPowerEntity, NumberEntity):
    """One value from the station's energy-management range control."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER

    def __init__(
        self, coordinator, key: str, name: str, minimum: int, maximum: int
    ) -> None:
        super().__init__(coordinator)
        self._key = key
        self._attr_name = name
        self._attr_native_min_value = minimum
        self._attr_native_max_value = maximum
        self._attr_unique_id = f"{coordinator.entry.data[CONF_ADDRESS]}_{key}"

    @property
    def native_value(self) -> int | None:
        return (self.coordinator.data or {}).get(self._key)

    async def async_set_native_value(self, value: float) -> None:
        limit = int(value)
        if limit != value:
            raise ValueError("limit must be a whole-number percentage")
        await self.coordinator.async_set_charge_limits(**{self._key: limit})
