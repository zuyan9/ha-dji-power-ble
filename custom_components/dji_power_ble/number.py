"""Energy-management controls from keyed configuration."""

from __future__ import annotations

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, PERCENTAGE, EntityCategory, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import DjiPowerEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[NumberEntity] = [
        DjiPowerLimitNumber(coordinator, "discharge_limit", "Discharge limit", 0, 15),
        DjiPowerLimitNumber(coordinator, "recharge_limit", "Recharge limit", 70, 100),
    ]
    if coordinator.device.model == "DJI Power 2000":
        entities.append(DjiPowerDischargePowerNumber(coordinator))
    async_add_entities(entities)


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


class DjiPowerDischargePowerNumber(DjiPowerEntity, NumberEntity):
    """Fixed discharge power with station-reported limits."""

    _attr_name = "Discharge power"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_device_class = NumberDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = (
            f"{coordinator.entry.data[CONF_ADDRESS]}_discharge_power_w"
        )

    @property
    def available(self) -> bool:
        data = self.coordinator.data or {}
        minimum = data.get("discharge_power_min_w")
        maximum = data.get("discharge_power_max_w")
        value = self.native_value
        return (
            super().available
            and data.get("discharge_power_available") is True
            and isinstance(minimum, int)
            and isinstance(maximum, int)
            and isinstance(value, int)
            and 0 <= minimum <= value <= maximum
        )

    @property
    def native_value(self) -> int | None:
        return (self.coordinator.data or {}).get("discharge_power_w")

    @property
    def native_min_value(self) -> int:
        # HA requires numeric bounds even while this entity is unavailable.
        value = (self.coordinator.data or {}).get("discharge_power_min_w")
        return value if isinstance(value, int) else 0

    @property
    def native_max_value(self) -> int:
        value = (self.coordinator.data or {}).get("discharge_power_max_w")
        return value if isinstance(value, int) else 0

    async def async_set_native_value(self, value: float) -> None:
        watts = int(value)
        if watts != value:
            raise ValueError("discharge power must be a whole number of watts")
        await self.coordinator.async_set_discharge_power(watts)
