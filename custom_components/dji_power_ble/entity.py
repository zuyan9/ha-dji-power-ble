"""Shared entity base."""
from __future__ import annotations

from homeassistant.const import CONF_ADDRESS, CONF_NAME
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import DjiPowerCoordinator


class DjiPowerEntity(CoordinatorEntity[DjiPowerCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: DjiPowerCoordinator) -> None:
        super().__init__(coordinator)
        address = coordinator.entry.data[CONF_ADDRESS]
        data = coordinator.data or {}
        sw = data.get("firmware")
        if data.get("firmware_secondary"):
            sw = f"{sw} / {data['firmware_secondary']}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, address)},
            connections={("bluetooth", address)},
            name=coordinator.entry.data.get(CONF_NAME, "DJI Power"),
            manufacturer="DJI",
            model="Power 1000 V2",
            sw_version=sw,
        )
