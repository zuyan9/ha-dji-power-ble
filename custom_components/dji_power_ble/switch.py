"""AC output, backup reserve, Energy Saver, and reported port switches."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .accessory import (
    USB_INTERFACE_TYPES,
    AccessoryIdentity,
    DjiPowerAccessoryEntity,
    DjiPowerCarChargerEntity,
    async_discover_accessories,
    async_discover_backup_reserve,
    async_discover_feature,
)
from .const import DOMAIN
from .entity import DjiPowerEntity
from .features import ModelFeature, eligible_port_switches, feature_available


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_discover_backup_reserve(
        coordinator,
        entry,
        async_add_entities,
        lambda: [DjiPowerBackupReserveSwitch(coordinator)],
    )
    async_discover_feature(
        coordinator, entry, async_add_entities, ModelFeature.AUTO_RESUME,
        lambda: [DjiPowerAutoResumeSwitch(coordinator)],
    )
    async_discover_feature(
        coordinator, entry, async_add_entities, ModelFeature.SCHEDULED_CONTROLS,
        lambda: [
            DjiPowerScheduledSwitch(coordinator, "peak_discharge"),
            DjiPowerScheduledSwitch(coordinator, "off_peak_charge"),
        ],
    )
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
        interface_types=USB_INTERFACE_TYPES,
    )
    async_discover_accessories(
        coordinator,
        entry,
        async_add_entities,
        {
            "power_switches": lambda identity: [
                DjiPowerAcSwitch(coordinator, identity)
                if identity[0] == 2 else DjiPowerCarOutletSwitch(coordinator, identity)
            ],
        },
        interface_types={2, 7},
    )


class DjiPowerBackupReserveSwitch(DjiPowerEntity, SwitchEntity):
    """Enable the custom backup reserve level while the station offers it."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_name = "Custom backup reserve level"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = (
            f"{coordinator.entry.data[CONF_ADDRESS]}_custom_backup_reserve"
        )

    @property
    def available(self) -> bool:
        data = self.coordinator.data or {}
        return (
            super().available
            and feature_available(data, ModelFeature.RESERVE_CONTROL)
            and self.is_on is not None
        )

    @property
    def is_on(self) -> bool | None:
        value = (self.coordinator.data or {}).get("energy_reserve_enabled")
        return value if isinstance(value, bool) else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_energy_reserve(enabled=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_energy_reserve(enabled=False)


class DjiPowerAutoResumeSwitch(DjiPowerEntity, SwitchEntity):
    """Resume Energy Saver when the AC input reconnects to the household grid."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "auto_resume"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.data[CONF_ADDRESS]}_auto_resume"

    @property
    def available(self) -> bool:
        return super().available and feature_available(
            self.coordinator.data or {}, ModelFeature.AUTO_RESUME
        )

    @property
    def is_on(self) -> bool | None:
        value = (self.coordinator.data or {}).get("auto_resume_enabled")
        return value if isinstance(value, bool) else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_auto_resume(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_auto_resume(False)


class DjiPowerScheduledSwitch(DjiPowerEntity, SwitchEntity):
    """Discharge during peak or charge during off-peak in Scheduled Periods."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, switch: str) -> None:
        super().__init__(coordinator)
        self._switch = switch
        self._attr_translation_key = switch
        self._attr_unique_id = f"{coordinator.entry.data[CONF_ADDRESS]}_{switch}"

    @property
    def available(self) -> bool:
        return (
            super().available
            and feature_available(
                self.coordinator.data or {}, ModelFeature.SCHEDULED_CONTROLS
            )
            and self.is_on is not None
        )

    @property
    def is_on(self) -> bool | None:
        value = (self.coordinator.data or {}).get(f"{self._switch}_enabled")
        return value if isinstance(value, bool) else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_scheduled_switch(self._switch, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_scheduled_switch(self._switch, False)


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
    def row(self) -> dict | None:
        return eligible_port_switches(self.coordinator.data or {}).get(
            self._identity[:2]
        )

    @property
    def available(self) -> bool:
        return super().available and self.is_on is not None

    @property
    def is_on(self) -> bool | None:
        value = (self.row or {}).get("sw")
        return value == 1 if type(value) is int and value in (1, 2) else None


class DjiPowerAcSwitch(_DjiPowerPortSwitch):
    """Control an explicitly reported AC outlet, preserving the first outlet ID."""

    _attr_device_class = SwitchDeviceClass.OUTLET
    _interface_types = {2}

    def __init__(
        self, coordinator, identity: AccessoryIdentity = (2, 1, 0)
    ) -> None:
        super().__init__(coordinator, identity, "ac_output", "output")
        seq = identity[1]
        self._attr_name = "AC output" if seq == 1 else f"AC{seq} output"
        suffix = "ac_output" if seq == 1 else f"2_{seq}_ac_output"
        self._attr_unique_id = f"{coordinator.entry.data[CONF_ADDRESS]}_{suffix}"

    async def async_turn_on(self, **kwargs: Any) -> None:
        if self._identity[1] == 1:
            await self.coordinator.async_set_ac(True)
        else:
            await self.coordinator.async_set_port_switch(*self._identity[:2], True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        if self._identity[1] == 1:
            await self.coordinator.async_set_ac(False)
        else:
            await self.coordinator.async_set_port_switch(*self._identity[:2], False)


class DjiPowerCarOutletSwitch(_DjiPowerPortSwitch):
    """Control the station's reported 12 V car outlet."""

    _attr_device_class = SwitchDeviceClass.OUTLET
    _interface_types = {7}

    def __init__(self, coordinator, identity: AccessoryIdentity) -> None:
        super().__init__(coordinator, identity, "car_outlet_output", "output")
        seq = identity[1]
        self._attr_name = (
            "Car outlet output" if seq == 1 else f"Car outlet {seq} output"
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_port_switch(*self._identity[:2], True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_port_switch(*self._identity[:2], False)


class DjiPowerSdcSwitch(_DjiPowerPortSwitch):
    """Control an SDC interface only when its own switch row is reported."""

    def __init__(self, coordinator, identity: AccessoryIdentity) -> None:
        super().__init__(coordinator, identity, "sdc_power", "power")

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_sdc(*self._identity[:2], True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_sdc(*self._identity[:2], False)


class DjiPowerUsbSwitch(_DjiPowerPortSwitch):
    """Control a USB-A or USB-C output the station reports and offers."""

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
