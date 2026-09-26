"""Connectivity, charging, and battery maintenance binary sensors."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import DjiPowerEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            DjiPowerConnected(coordinator),
            DjiPowerCharging(coordinator),
            DjiPowerCloudConnected(coordinator),
        ]
    )
    added = False

    @callback
    def async_discover_maintenance_charging() -> None:
        """Add the maintenance state once the station's base info reports it."""
        nonlocal added
        if added or not coordinator.last_update_success:
            return
        if "maintenance_charging" not in (coordinator.data or {}):
            return
        added = True
        async_add_entities([DjiPowerMaintenanceCharging(coordinator)])

    entry.async_on_unload(
        coordinator.async_add_listener(async_discover_maintenance_charging)
    )
    async_discover_maintenance_charging()


class DjiPowerCharging(DjiPowerEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.BATTERY_CHARGING
    _attr_name = "Charging"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.data[CONF_ADDRESS]}_charging"

    @property
    def is_on(self) -> bool | None:
        return (self.coordinator.data or {}).get("charging")


class DjiPowerMaintenanceCharging(DjiPowerEntity, BinarySensorEntity):
    """Whether the station is charging to 100% to maintain its battery."""

    _attr_icon = "mdi:battery-heart-variant"
    _attr_translation_key = "maintenance_charging"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = (
            f"{coordinator.entry.data[CONF_ADDRESS]}_maintenance_charging"
        )

    @property
    def is_on(self) -> bool | None:
        return (self.coordinator.data or {}).get("maintenance_charging")


class DjiPowerConnected(DjiPowerEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_name = "Connected"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.data[CONF_ADDRESS]}_connected"

    @property
    def is_on(self) -> bool:
        return self.coordinator.device.is_connected


class DjiPowerCloudConnected(DjiPowerEntity, BinarySensorEntity):
    """Whether the station reports an active DJI cloud connection."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_name = "DJI cloud connected"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.data[CONF_ADDRESS]}_cloud_connected"

    @property
    def is_on(self) -> bool | None:
        return (self.coordinator.data or {}).get("cloud_connected")
