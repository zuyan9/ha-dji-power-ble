"""Energy-management controls from keyed configuration."""

from __future__ import annotations

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_ADDRESS,
    PERCENTAGE,
    EntityCategory,
    UnitOfElectricPotential,
    UnitOfPower,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .accessory import (
    AccessoryIdentity,
    DjiPowerCarChargerEntity,
    async_discover_accessories,
    async_discover_backup_reserve,
)
from .const import DOMAIN
from .duml import CAR_CHARGER_NUMBERS, car_charger_numbers, energy_reserve_bounds
from .entity import DjiPowerEntity
from .features import ModelFeature, supports_feature


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[NumberEntity] = [
        DjiPowerLimitNumber(coordinator, "discharge_limit", "Discharge limit", 0, 15),
        DjiPowerLimitNumber(coordinator, "recharge_limit", "Recharge limit", 70, 100),
    ]
    if supports_feature(coordinator.device.model, ModelFeature.TOU_POWER_CONTROL):
        entities.extend(
            (
                DjiPowerDischargePowerNumber(coordinator),
                DjiPowerChargePowerNumber(coordinator),
            )
        )
    async_add_entities(entities)
    async_discover_backup_reserve(
        coordinator,
        entry,
        async_add_entities,
        lambda: [DjiPowerBackupReserveNumber(coordinator)],
    )
    async_discover_accessories(
        coordinator,
        entry,
        async_add_entities,
        {
            "car_chargers": lambda identity: [
                DjiPowerCarRechargePowerNumber(coordinator, identity),
                DjiPowerCarMinimumVoltageNumber(coordinator, identity),
                DjiPowerCarChargePowerNumber(coordinator, identity),
                DjiPowerCarChargingVoltageNumber(coordinator, identity),
                DjiPowerCarAutoVoltageNumber(coordinator, identity),
            ]
        },
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
    def available(self) -> bool:
        return super().available and isinstance(self.native_value, int)

    @property
    def native_value(self) -> int | None:
        return (self.coordinator.data or {}).get(self._key)

    async def async_set_native_value(self, value: float) -> None:
        limit = int(value)
        if limit != value:
            raise ServiceValidationError("limit must be a whole-number percentage")
        await self.coordinator.async_set_charge_limits(**{self._key: limit})


class DjiPowerBackupReserveNumber(DjiPowerEntity, NumberEntity):
    """Battery level above which only solar recharging is used.

    DJI Home bounds it by the discharge limit plus a margin and the recharge limit.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_name = "Backup reserve level"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = (
            f"{coordinator.entry.data[CONF_ADDRESS]}_backup_reserve_level"
        )

    def _bounds(self) -> tuple[int, int] | None:
        data = self.coordinator.data or {}
        return energy_reserve_bounds(
            data.get("discharge_limit"), data.get("recharge_limit")
        )

    @property
    def available(self) -> bool:
        data = self.coordinator.data or {}
        return (
            super().available
            and data.get("energy_reserve_available") is True
            and data.get("energy_reserve_enabled") is True
            and isinstance(self.native_value, int)
            and self._bounds() is not None
        )

    @property
    def native_value(self) -> int | None:
        return (self.coordinator.data or {}).get("energy_reserve")

    @property
    def native_min_value(self) -> int:
        # HA requires numeric bounds even while this entity is unavailable.
        bounds = self._bounds()
        return bounds[0] if bounds is not None else 0

    @property
    def native_max_value(self) -> int:
        bounds = self._bounds()
        return bounds[1] if bounds is not None else 100

    async def async_set_native_value(self, value: float) -> None:
        percent = int(value)
        if percent != value:
            raise ServiceValidationError(
                "backup reserve must be a whole-number percentage"
            )
        await self.coordinator.async_set_energy_reserve(percent=percent)


class _DjiPowerWattNumber(DjiPowerEntity, NumberEntity):
    """A power setting with station-reported availability and limits."""

    _key: str
    _attr_entity_category = EntityCategory.CONFIG
    _attr_device_class = NumberDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = (
            f"{coordinator.entry.data[CONF_ADDRESS]}_{self._key}_w"
        )

    @property
    def available(self) -> bool:
        data = self.coordinator.data or {}
        minimum = data.get(f"{self._key}_min_w")
        maximum = data.get(f"{self._key}_max_w")
        value = self.native_value
        return (
            super().available
            and data.get(f"{self._key}_available") is True
            and isinstance(minimum, int)
            and isinstance(maximum, int)
            and isinstance(value, int)
            and 0 <= minimum <= value <= maximum
        )

    @property
    def native_value(self) -> int | None:
        return (self.coordinator.data or {}).get(f"{self._key}_w")

    @property
    def native_min_value(self) -> int:
        # HA requires numeric bounds even while this entity is unavailable.
        value = (self.coordinator.data or {}).get(f"{self._key}_min_w")
        return value if isinstance(value, int) else 0

    @property
    def native_max_value(self) -> int:
        value = (self.coordinator.data or {}).get(f"{self._key}_max_w")
        return value if isinstance(value, int) else 0


class DjiPowerDischargePowerNumber(_DjiPowerWattNumber):
    """Fixed discharge power with station-reported limits."""

    _attr_name = "Discharge power"
    _key = "discharge_power"

    async def async_set_native_value(self, value: float) -> None:
        watts = int(value)
        if watts != value:
            raise ServiceValidationError(
                "discharge power must be a whole number of watts"
            )
        await self.coordinator.async_set_discharge_power(watts)


class DjiPowerChargePowerNumber(_DjiPowerWattNumber):
    """Recharge power with station-reported limits."""

    _attr_name = "Recharge power"
    _key = "charge_power"

    async def async_set_native_value(self, value: float) -> None:
        watts = int(value)
        if watts != value:
            raise ServiceValidationError(
                "recharge power must be a whole number of watts"
            )
        await self.coordinator.async_set_charge_power(watts)


class _DjiPowerCarNumber(DjiPowerCarChargerEntity, NumberEntity):
    """A car-charger value with independent, device-reported bounds.

    It is available only in the modes where DJI Home offers it.
    """

    _setting: str
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX

    @property
    def _field(self) -> str:
        return CAR_CHARGER_NUMBERS[self._setting][0]

    @property
    def _scale(self) -> int:
        return CAR_CHARGER_NUMBERS[self._setting][1]

    def _bounds(self) -> tuple[int, int, int] | None:
        row = self.row or {}
        values = tuple(
            row.get(f"{self._field}_{suffix}") for suffix in ("low", "v", "up")
        )
        if all(type(value) is int for value in values):
            minimum, value, maximum = values
            if 0 <= minimum <= value <= maximum and maximum > 0:
                return minimum, value, maximum
        return None

    @property
    def available(self) -> bool:
        numbers = car_charger_numbers(
            (self.row or {}).get("mode"),
            (self.coordinator.data or {}).get("car_auto_threshold"),
        )
        return (
            super().available
            and self.charger_enabled
            and self._field in numbers
            and self._bounds() is not None
        )

    @property
    def native_value(self) -> float | None:
        bounds = self._bounds()
        return bounds[1] / self._scale if bounds is not None else None

    @property
    def native_min_value(self) -> float:
        bounds = self._bounds()
        return bounds[0] / self._scale if bounds is not None else 0

    @property
    def native_max_value(self) -> float:
        bounds = self._bounds()
        return bounds[2] / self._scale if bounds is not None else 0


class _DjiPowerCarPowerNumber(_DjiPowerCarNumber):
    """A car-charger power in whole watts."""

    _attr_device_class = NumberDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_native_step = 1

    async def async_set_native_value(self, value: float) -> None:
        watts = int(value)
        if watts != value:
            raise ServiceValidationError(
                f"{self._attr_name} must be a whole number of watts"
            )
        await self.async_set_charger(**{self._setting: watts})


class _DjiPowerCarVoltageNumber(_DjiPowerCarNumber):
    """A vehicle voltage in hundredths of a volt."""

    _attr_device_class = NumberDeviceClass.VOLTAGE
    _attr_native_unit_of_measurement = UnitOfElectricPotential.VOLT
    _attr_native_step = 0.01

    async def async_set_native_value(self, value: float) -> None:
        await self.async_set_charger(**{self._setting: value})


class DjiPowerCarRechargePowerNumber(_DjiPowerCarPowerNumber):
    """Set power transferred from the car into the station."""

    _setting = "recharge_power_w"

    def __init__(self, coordinator, identity: AccessoryIdentity) -> None:
        super().__init__(
            coordinator, identity, "car_recharge_power", "car recharge power"
        )


class DjiPowerCarMinimumVoltageNumber(_DjiPowerCarVoltageNumber):
    """Set the minimum vehicle voltage for recharging the station."""

    _setting = "minimum_voltage_v"

    def __init__(self, coordinator, identity: AccessoryIdentity) -> None:
        super().__init__(
            coordinator,
            identity,
            "car_minimum_voltage",
            "minimum car recharging voltage",
        )


class DjiPowerCarChargePowerNumber(_DjiPowerCarPowerNumber):
    """Set power transferred from the station into the car."""

    _setting = "charge_power_w"

    def __init__(self, coordinator, identity: AccessoryIdentity) -> None:
        super().__init__(
            coordinator, identity, "car_charge_power", "car charging power"
        )


class DjiPowerCarChargingVoltageNumber(_DjiPowerCarVoltageNumber):
    """Set DJI Home's Voltage of Car Charging for charging the car."""

    _setting = "charge_voltage_v"

    def __init__(self, coordinator, identity: AccessoryIdentity) -> None:
        super().__init__(
            coordinator, identity, "car_charge_voltage", "car charging voltage"
        )


class DjiPowerCarAutoVoltageNumber(_DjiPowerCarVoltageNumber):
    """Set the vehicle voltage at or below which Auto mode charges the car.

    Above it, Auto mode recharges the station from the car.
    """

    _setting = "auto_voltage_v"

    def __init__(self, coordinator, identity: AccessoryIdentity) -> None:
        super().__init__(
            coordinator,
            identity,
            "car_auto_voltage",
            "car auto switching voltage",
        )
