"""Offline checks for station binary sensors and maintenance-charge discovery."""

from __future__ import annotations

import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

COMPONENT = Path(__file__).parents[1] / "custom_components" / "dji_power_ble"
PACKAGE = "_dji_power_binary_sensor_tests"
DOMAIN = "dji_power_ble"
ADDRESS = "AA:BB:CC:DD:EE:FF"


class _CoordinatorEntity:
    def __class_getitem__(cls, item):
        return cls

    def __init__(self, coordinator) -> None:
        self.coordinator = coordinator

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success


def _module(name: str, **attributes) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    return module


def _load_binary_sensor() -> types.ModuleType:
    """Load the real platform against isolated Home Assistant interfaces."""
    modules = {
        PACKAGE: _module(PACKAGE, __path__=[str(COMPONENT)]),
        f"{PACKAGE}.coordinator": _module(
            f"{PACKAGE}.coordinator", DjiPowerCoordinator=object
        ),
        "homeassistant": _module("homeassistant"),
        "homeassistant.components": _module("homeassistant.components"),
        "homeassistant.components.binary_sensor": _module(
            "homeassistant.components.binary_sensor",
            BinarySensorEntity=type("BinarySensorEntity", (), {}),
            BinarySensorDeviceClass=types.SimpleNamespace(
                BATTERY_CHARGING="battery_charging", CONNECTIVITY="connectivity"
            ),
        ),
        "homeassistant.config_entries": _module(
            "homeassistant.config_entries", ConfigEntry=object
        ),
        "homeassistant.const": _module(
            "homeassistant.const",
            CONF_ADDRESS="address",
            EntityCategory=types.SimpleNamespace(DIAGNOSTIC="diagnostic"),
        ),
        "homeassistant.core": _module(
            "homeassistant.core", HomeAssistant=object, callback=lambda method: method
        ),
        "homeassistant.helpers": _module("homeassistant.helpers"),
        "homeassistant.helpers.device_registry": _module(
            "homeassistant.helpers.device_registry",
            DeviceInfo=dict,
            CONNECTION_BLUETOOTH="bluetooth",
        ),
        "homeassistant.helpers.entity_platform": _module(
            "homeassistant.helpers.entity_platform", AddEntitiesCallback=object
        ),
        "homeassistant.helpers.update_coordinator": _module(
            "homeassistant.helpers.update_coordinator",
            CoordinatorEntity=_CoordinatorEntity,
        ),
    }
    with patch.dict(sys.modules, modules):
        for name in ("const", "entity", "binary_sensor"):
            spec = importlib.util.spec_from_file_location(
                f"{PACKAGE}.{name}", COMPONENT / f"{name}.py"
            )
            assert spec and spec.loader
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
    return module


binary_sensor = _load_binary_sensor()


class _Coordinator:
    def __init__(self, data: dict | None = None) -> None:
        self.entry = types.SimpleNamespace(data={"address": ADDRESS}, title="Station")
        self.device = types.SimpleNamespace(
            model="DJI Power 2000",
            address=ADDRESS,
            serial_number=None,
            is_connected=True,
        )
        self.data = data or {}
        self.last_update_success = True
        self.listeners = []

    def async_add_listener(self, listener):
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)

    def publish(self, data: dict) -> None:
        self.data = data
        for listener in list(self.listeners):
            listener()


class MaintenanceChargingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.coordinator = _Coordinator()
        self.unloads = []
        self.entry = types.SimpleNamespace(
            entry_id="station", async_on_unload=self.unloads.append
        )
        self.hass = types.SimpleNamespace(data={DOMAIN: {"station": self.coordinator}})
        self.added: list = []

    async def _setup(self) -> None:
        await binary_sensor.async_setup_entry(self.hass, self.entry, self.added.extend)

    def _maintenance(self) -> list:
        return [
            entity
            for entity in self.added
            if isinstance(entity, binary_sensor.DjiPowerMaintenanceCharging)
        ]

    async def test_created_at_setup_when_base_info_reports_charge_type(self) -> None:
        self.coordinator.data = {"maintenance_charging": False}
        await self._setup()

        (entity,) = self._maintenance()
        self.assertEqual(entity._attr_unique_id, f"{ADDRESS}_maintenance_charging")
        self.assertEqual(entity._attr_translation_key, "maintenance_charging")
        self.assertIs(entity.is_on, False)
        self.assertEqual(len(self.added), 4)

    async def test_station_without_charge_type_gets_no_entity(self) -> None:
        # The original Power 1000 reports firmware and cycles, not charge type.
        self.coordinator.data = {"firmware": "01.00.1800", "battery_cycle_count": 3}
        await self._setup()
        self.coordinator.publish(
            {"firmware": "01.00.1800", "battery_cycle_count": 4}
        )

        self.assertEqual(self._maintenance(), [])
        self.assertEqual(len(self.added), 3)

    async def test_created_once_after_a_later_report(self) -> None:
        await self._setup()
        self.assertEqual(self._maintenance(), [])

        self.coordinator.last_update_success = False
        self.coordinator.publish({"maintenance_charging": True})
        self.assertEqual(self._maintenance(), [])

        self.coordinator.last_update_success = True
        self.coordinator.publish({"maintenance_charging": None})
        (entity,) = self._maintenance()
        for value in (True, False, None):
            self.coordinator.publish({"maintenance_charging": value})
        self.assertEqual(self._maintenance(), [entity])

    async def test_state_follows_reported_charge_type(self) -> None:
        self.coordinator.data = {"maintenance_charging": None}
        await self._setup()
        (entity,) = self._maintenance()

        for data, expected in (
            ({"maintenance_charging": True}, True),
            ({"maintenance_charging": False}, False),
            ({"maintenance_charging": None}, None),
            ({}, None),
        ):
            with self.subTest(data=data):
                self.coordinator.publish(data)
                self.assertIs(entity.is_on, expected)
        self.coordinator.data = None
        self.assertIsNone(entity.is_on)

    async def test_unavailable_while_disconnected(self) -> None:
        self.coordinator.data = {"maintenance_charging": True}
        await self._setup()
        (entity,) = self._maintenance()

        self.assertTrue(entity.available)
        self.coordinator.last_update_success = False
        self.assertFalse(entity.available)

    async def test_listener_is_released_on_unload(self) -> None:
        await self._setup()
        self.assertEqual(len(self.coordinator.listeners), 1)
        for unload in self.unloads:
            unload()
        self.assertEqual(self.coordinator.listeners, [])

    def test_entity_name_is_translated(self) -> None:
        strings = json.loads((COMPONENT / "strings.json").read_text())
        translated = json.loads((COMPONENT / "translations/en.json").read_text())

        self.assertEqual(strings, translated)
        self.assertEqual(
            strings["entity"]["binary_sensor"]["maintenance_charging"]["name"],
            "Battery maintenance charging",
        )


if __name__ == "__main__":
    unittest.main()
