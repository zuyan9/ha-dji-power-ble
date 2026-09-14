"""Offline checks for discharge-power entities and their coordinator bridge."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

COMPONENT = Path(__file__).parents[1] / "custom_components" / "dji_power_ble"
PACKAGE = "_dji_power_number_tests"


class _Entity:
    def __init__(self, coordinator) -> None:
        self.coordinator = coordinator

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success


class _DataUpdateCoordinator:
    def __class_getitem__(cls, item):
        return cls


class _SelectEntity:
    @property
    def options(self) -> list[str]:
        return self._attr_options


class _DjiPowerError(Exception):
    pass


class _HomeAssistantError(Exception):
    pass


def _module(name: str, **attributes) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    return module


def _load_modules() -> tuple[types.ModuleType, types.ModuleType, types.ModuleType]:
    """Load the real control code with isolated Home Assistant interfaces."""
    modules = {
        PACKAGE: _module(PACKAGE, __path__=[str(COMPONENT)]),
        f"{PACKAGE}.entity": _module(f"{PACKAGE}.entity", DjiPowerEntity=_Entity),
        f"{PACKAGE}.device": _module(
            f"{PACKAGE}.device",
            DjiPowerDevice=object,
            DjiPowerError=_DjiPowerError,
            DjiPowerAuthenticationError=_DjiPowerError,
        ),
        "homeassistant": _module("homeassistant"),
        "homeassistant.components": _module("homeassistant.components"),
        "homeassistant.components.number": _module(
            "homeassistant.components.number",
            NumberEntity=type("NumberEntity", (), {}),
            NumberDeviceClass=types.SimpleNamespace(POWER="power"),
            NumberMode=types.SimpleNamespace(SLIDER="slider", BOX="box"),
        ),
        "homeassistant.components.select": _module(
            "homeassistant.components.select", SelectEntity=_SelectEntity
        ),
        "homeassistant.config_entries": _module(
            "homeassistant.config_entries",
            ConfigEntry=object,
            ConfigEntryState=types.SimpleNamespace(LOADED="loaded"),
        ),
        "homeassistant.const": _module(
            "homeassistant.const",
            CONF_ADDRESS="address",
            PERCENTAGE="%",
            EntityCategory=types.SimpleNamespace(CONFIG="config"),
            UnitOfPower=types.SimpleNamespace(WATT="W"),
        ),
        "homeassistant.core": _module(
            "homeassistant.core", HomeAssistant=object, callback=lambda method: method
        ),
        "homeassistant.exceptions": _module(
            "homeassistant.exceptions", HomeAssistantError=_HomeAssistantError
        ),
        "homeassistant.helpers": _module("homeassistant.helpers"),
        "homeassistant.helpers.entity_platform": _module(
            "homeassistant.helpers.entity_platform", AddEntitiesCallback=object
        ),
        "homeassistant.helpers.update_coordinator": _module(
            "homeassistant.helpers.update_coordinator",
            DataUpdateCoordinator=_DataUpdateCoordinator,
            UpdateFailed=Exception,
        ),
        "bleak.exc": _module("bleak.exc", BleakError=Exception),
    }
    loaded = {}
    with patch.dict(sys.modules, modules):
        for name in ("number", "coordinator", "select"):
            spec = importlib.util.spec_from_file_location(
                f"{PACKAGE}.{name}", COMPONENT / f"{name}.py"
            )
            assert spec and spec.loader
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            loaded[name] = module
    return loaded["number"], loaded["coordinator"], loaded["select"]


number, coordinator_module, select = _load_modules()


class DischargePowerNumberTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.coordinator = types.SimpleNamespace(
            entry=types.SimpleNamespace(data={"address": "AA:BB:CC:DD:EE:FF"}),
            device=types.SimpleNamespace(model="DJI Power 2000"),
            last_update_success=True,
            data={
                "discharge_power_available": True,
                "discharge_power_w": 93,
                "discharge_power_min_w": 0,
                "discharge_power_max_w": 800,
            },
            async_set_discharge_power=AsyncMock(),
        )
        self.entity = number.DjiPowerDischargePowerNumber(self.coordinator)

    async def test_only_power_2000_gets_control_before_config_arrives(self) -> None:
        self.coordinator.data = {}
        entry = types.SimpleNamespace(entry_id="station")
        hass = types.SimpleNamespace(
            data={"dji_power_ble": {"station": self.coordinator}}
        )
        for model in (
            "DJI Power 2000",
            "DJI Power 1000",
            "DJI Power 1000 V2",
            "DJI Power 1000 Mini",
        ):
            with self.subTest(model=model):
                self.coordinator.device.model = model
                add_entities = Mock()
                await number.async_setup_entry(hass, entry, add_entities)
                entities = add_entities.call_args.args[0]
                controls = [
                    entity
                    for entity in entities
                    if isinstance(entity, number.DjiPowerDischargePowerNumber)
                ]
                self.assertEqual(len(controls), int(model == "DJI Power 2000"))
                self.assertEqual(len(entities) - len(controls), 2)
                if controls:
                    self.assertFalse(controls[0].available)

    def test_value_and_limits_follow_reported_config(self) -> None:
        self.assertTrue(self.entity.available)
        self.assertEqual(self.entity.native_value, 93)
        self.assertEqual(self.entity.native_min_value, 0)
        self.assertEqual(self.entity.native_max_value, 800)
        self.coordinator.data.update(
            discharge_power_w=422,
            discharge_power_min_w=50,
            discharge_power_max_w=600,
        )
        self.assertTrue(self.entity.available)
        self.assertEqual(self.entity.native_value, 422)
        self.assertEqual(self.entity.native_min_value, 50)
        self.assertEqual(self.entity.native_max_value, 600)

    def test_unavailable_without_capability_or_valid_bounds(self) -> None:
        original = dict(self.coordinator.data)
        for missing in original:
            with self.subTest(missing=missing):
                self.coordinator.data = {
                    key: value for key, value in original.items() if key != missing
                }
                self.assertFalse(self.entity.available)
        for invalid in (
            {"discharge_power_available": False},
            {"discharge_power_min_w": 100},
            {"discharge_power_max_w": 90},
        ):
            with self.subTest(invalid=invalid):
                self.coordinator.data = original | invalid
                self.assertFalse(self.entity.available)
        self.coordinator.data = {}
        self.assertEqual(self.entity.native_min_value, 0)
        self.assertEqual(self.entity.native_max_value, 0)

    def test_disconnected_coordinator_makes_control_unavailable(self) -> None:
        self.coordinator.last_update_success = False
        self.assertFalse(self.entity.available)

    def test_cleared_config_has_numeric_bounds_while_unavailable(self) -> None:
        self.coordinator.data.update(
            discharge_power_available=False,
            discharge_power_w=None,
            discharge_power_min_w=None,
            discharge_power_max_w=None,
        )
        self.assertFalse(self.entity.available)
        self.assertIsNone(self.entity.native_value)
        self.assertEqual(self.entity.native_min_value, 0)
        self.assertEqual(self.entity.native_max_value, 0)

    async def test_set_integer_watts_without_optimistic_state(self) -> None:
        await self.entity.async_set_native_value(422.0)
        self.coordinator.async_set_discharge_power.assert_awaited_once_with(422)
        self.assertEqual(self.entity.native_value, 93)

    async def test_rejects_fractional_watts(self) -> None:
        with self.assertRaisesRegex(ValueError, "whole number of watts"):
            await self.entity.async_set_native_value(422.5)
        self.coordinator.async_set_discharge_power.assert_not_awaited()


class DischargePowerCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.device = types.SimpleNamespace(
            data={"discharge_power_w": 93}, set_discharge_power=AsyncMock()
        )
        self.coordinator = coordinator_module.DjiPowerCoordinator.__new__(
            coordinator_module.DjiPowerCoordinator
        )
        self.coordinator.device = self.device
        self.coordinator._publish = Mock()

    async def test_publishes_confirmed_device_state_after_write(self) -> None:
        async def confirmed_write(watts: int) -> None:
            self.coordinator._publish.assert_not_called()
            self.device.data["discharge_power_w"] = watts

        self.device.set_discharge_power.side_effect = confirmed_write
        await self.coordinator.async_set_discharge_power(422)
        self.device.set_discharge_power.assert_awaited_once_with(422)
        self.coordinator._publish.assert_called_once_with({"discharge_power_w": 422})
        self.assertIsNot(self.coordinator._publish.call_args.args[0], self.device.data)

    async def test_failed_write_is_service_error_without_publishing(self) -> None:
        self.device.set_discharge_power.side_effect = _DjiPowerError(
            "readback timed out"
        )
        with self.assertRaisesRegex(_HomeAssistantError, "readback timed out"):
            await self.coordinator.async_set_discharge_power(422)
        self.coordinator._publish.assert_not_called()
