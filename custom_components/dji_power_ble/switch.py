"""AC output, reported USB output, and reported SDC accessory switches."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .accessory import (
    USB_INTERFACE_TYPES,
    AccessoryIdentity,
    DjiPowerAccessoryEntity,
    DjiPowerCarChargerEntity,
    async_discover_accessories,
)
from .const import DOMAIN
from .entity import DjiPowerEntity
from .features import ModelFeature


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([DjiPowerAcSwitch(coordinator)])
    async_discover_accessories(
        coordinator,
        entry,
        async_add_entities,
        {
            "car_chargers": lambda identity: [
                DjiPowerCarRechargingSwitch(coordinator, identity)
            ],
            "power_switches": lambda identity: [
                DjiPowerSdcSwitch(coordinator, identity)
            ],
        },
    )
    async_discover_accessories(
        coordinator,
        entry,
        async_add_entities,
        {
            "power_switches": lambda identity: [
                DjiPowerUsbSwitch(coordinator, identity)
            ],
        },
        feature=ModelFeature.USB_CONTROLS,
        interface_types=USB_INTERFACE_TYPES,
    )


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


class DjiPowerCarRechargingSwitch(DjiPowerCarChargerEntity, SwitchEntity):
    """Enable the reported car recharger without changing its stored settings."""

    def __init__(self, coordinator, identity: AccessoryIdentity) -> None:
        super().__init__(coordinator, identity, "car_recharging", "car recharging")

    @property
    def available(self) -> bool:
        mode = (self.row or {}).get("mode")
        return (
            super().available
            and self.is_on is not None
            and type(mode) is int
            and mode in (1, 2, 3)
        )

    @property
    def is_on(self) -> bool | None:
        value = (self.row or {}).get("sw")
        return value == 1 if type(value) is int and value in (1, 2) else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.async_set_charger(enabled=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.async_set_charger(enabled=False)


class _DjiPowerPortSwitch(DjiPowerAccessoryEntity, SwitchEntity):
    """A port switch available only while its own switch row is reported."""

    _row_key = "power_switches"

    @property
    def available(self) -> bool:
        return super().available and self.is_on is not None

    @property
    def is_on(self) -> bool | None:
        value = (self.row or {}).get("sw")
        return value == 1 if type(value) is int and value in (1, 2) else None


class DjiPowerSdcSwitch(_DjiPowerPortSwitch):
    """Control an SDC interface only when its own switch row is reported."""

    def __init__(self, coordinator, identity: AccessoryIdentity) -> None:
        super().__init__(coordinator, identity, "sdc_power", "power")

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_sdc(*self._identity[:2], True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_sdc(*self._identity[:2], False)


class DjiPowerUsbSwitch(_DjiPowerPortSwitch):
    """Control a USB-A or USB-C output only when its switch row is reported."""

    _attr_device_class = SwitchDeviceClass.OUTLET
    _interface_types = USB_INTERFACE_TYPES

    def __init__(self, coordinator, identity: AccessoryIdentity) -> None:
        super().__init__(coordinator, identity, "usb_output", "output")
        interface_type, seq, _ = identity
        port = "USB-A" if interface_type == 3 else "USB-C"
        self._attr_name = f"{port}{seq} output"

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_usb(*self._identity[:2], True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_usb(*self._identity[:2], False)
