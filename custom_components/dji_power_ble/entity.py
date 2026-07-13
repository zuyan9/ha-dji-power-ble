"""Shared entity base."""

from __future__ import annotations

from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import DjiPowerCoordinator


class DjiPowerEntity(CoordinatorEntity[DjiPowerCoordinator]):
    """Base entity backed by live device pushes."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: DjiPowerCoordinator) -> None:
        super().__init__(coordinator)
        device = coordinator.device
        data = coordinator.data or {}
        firmware = data.get("firmware")
        if secondary := data.get("firmware_secondary"):
            firmware = f"{firmware} / {secondary}" if firmware else str(secondary)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.address)},
            connections={(CONNECTION_BLUETOOTH, device.address)},
            name=coordinator.entry.title,
            manufacturer="DJI",
            model=device.model,
            serial_number=device.serial_number,
            sw_version=firmware,
        )
