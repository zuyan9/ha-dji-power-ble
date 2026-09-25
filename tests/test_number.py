"""Offline checks for watt controls and their coordinator bridge."""

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


class _DjiPowerScheduleChangedError(_DjiPowerError):
    pass


class _HomeAssistantError(Exception):
    pass


class _ServiceValidationError(_HomeAssistantError):
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
            DjiPowerScheduleChangedError=_DjiPowerScheduleChangedError,
        ),
        "homeassistant": _module("homeassistant"),
        "homeassistant.components": _module("homeassistant.components"),
        "homeassistant.components.number": _module(
            "homeassistant.components.number",
            NumberEntity=type("NumberEntity", (), {}),
            NumberDeviceClass=types.SimpleNamespace(POWER="power", VOLTAGE="voltage"),
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
            UnitOfElectricPotential=types.SimpleNamespace(VOLT="V"),
        ),
        "homeassistant.core": _module(
            "homeassistant.core", HomeAssistant=object, callback=lambda method: method
        ),
        "homeassistant.exceptions": _module(
            "homeassistant.exceptions",
            HomeAssistantError=_HomeAssistantError,
            ServiceValidationError=_ServiceValidationError,
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


class NumberSetupTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_power_2000_gets_both_controls_before_config_arrives(
        self,
    ) -> None:
        coordinator = types.SimpleNamespace(
            entry=types.SimpleNamespace(data={"address": "AA:BB:CC:DD:EE:FF"}),
            device=types.SimpleNamespace(model="DJI Power 2000"),
            last_update_success=True,
            data={},
            async_add_listener=Mock(return_value=Mock()),
        )
        entry = types.SimpleNamespace(entry_id="station", async_on_unload=Mock())
        hass = types.SimpleNamespace(data={"dji_power_ble": {"station": coordinator}})
        for model in (
            "DJI Power 2000",
            "DJI Power 1000",
            "DJI Power 1000 V2",
            "DJI Power 1000 Mini",
        ):
            with self.subTest(model=model):
                coordinator.device.model = model
                add_entities = Mock()
                await number.async_setup_entry(hass, entry, add_entities)
                entities = add_entities.call_args.args[0]
                for entity_class in (
                    number.DjiPowerDischargePowerNumber,
                    number.DjiPowerChargePowerNumber,
                ):
                    controls = [
                        entity
                        for entity in entities
                        if isinstance(entity, entity_class)
                    ]
                    self.assertEqual(len(controls), int(model == "DJI Power 2000"))
                    if controls:
                        self.assertFalse(controls[0].available)
                reserve = [
                    entity
                    for entity in entities
                    if isinstance(entity, number.DjiPowerBackupReserveNumber)
                ]
                self.assertEqual(len(reserve), int(model == "DJI Power 1000"))
                if reserve:
                    self.assertFalse(reserve[0].available)
                self.assertEqual(
                    len(entities),
                    {"DJI Power 2000": 4, "DJI Power 1000": 3}.get(model, 2),
                )


class LimitNumberTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.coordinator = types.SimpleNamespace(
            entry=types.SimpleNamespace(data={"address": "AA:BB:CC:DD:EE:FF"}),
            last_update_success=True,
            data={"discharge_limit": 10, "recharge_limit": 80},
            async_set_charge_limits=AsyncMock(),
        )
        self.entities = [
            number.DjiPowerLimitNumber(
                self.coordinator, "discharge_limit", "Discharge limit", 0, 15
            ),
            number.DjiPowerLimitNumber(
                self.coordinator, "recharge_limit", "Recharge limit", 70, 100
            ),
        ]

    def test_cleared_limits_are_unavailable_until_fresh_values_arrive(self) -> None:
        for entity in self.entities:
            with self.subTest(key=entity._key):
                original = entity.native_value
                self.assertTrue(entity.available)
                self.coordinator.data[entity._key] = None
                self.assertFalse(entity.available)
                self.assertIsNone(entity.native_value)
                self.coordinator.data[entity._key] = original
                self.assertTrue(entity.available)

    def test_missing_or_disconnected_state_is_unavailable(self) -> None:
        original = self.coordinator.data
        self.coordinator.data = {}
        for entity in self.entities:
            self.assertFalse(entity.available)
        self.coordinator.data = original
        self.coordinator.last_update_success = False
        for entity in self.entities:
            self.assertFalse(entity.available)

    async def test_sets_whole_percentages_without_optimistic_changes(self) -> None:
        for entity, value in zip(self.entities, (12.0, 90.0), strict=True):
            with self.subTest(key=entity._key):
                original = entity.native_value
                setter = self.coordinator.async_set_charge_limits
                setter.reset_mock()
                await entity.async_set_native_value(value)
                setter.assert_awaited_once_with(**{entity._key: int(value)})
                self.assertEqual(entity.native_value, original)

    async def test_fractional_limits_raise_service_validation_error(self) -> None:
        for entity in self.entities:
            with (
                self.subTest(key=entity._key),
                self.assertRaisesRegex(
                    _ServiceValidationError, "whole-number percentage"
                ),
            ):
                await entity.async_set_native_value(entity.native_value + 0.5)
        self.coordinator.async_set_charge_limits.assert_not_awaited()


class _PowerNumberTests:
    _key: str
    _name: str
    _entity_class: type

    def _data(self, **values: object) -> dict[str, object]:
        return {f"{self._key}_{key}": value for key, value in values.items()}

    def setUp(self) -> None:
        self.coordinator = types.SimpleNamespace(
            entry=types.SimpleNamespace(data={"address": "AA:BB:CC:DD:EE:FF"}),
            device=types.SimpleNamespace(model="DJI Power 2000"),
            last_update_success=True,
            data=self._data(available=True, w=93, min_w=0, max_w=800),
        )
        self.setter = AsyncMock()
        setattr(self.coordinator, f"async_set_{self._key}", self.setter)
        self.entity = self._entity_class(self.coordinator)

    def test_entity_metadata_and_unique_id(self) -> None:
        self.assertEqual(self.entity._attr_name, self._name)
        self.assertEqual(
            self.entity._attr_unique_id, f"AA:BB:CC:DD:EE:FF_{self._key}_w"
        )
        self.assertEqual(self.entity._attr_native_unit_of_measurement, "W")
        self.assertEqual(self.entity._attr_native_step, 1)
        self.assertEqual(self.entity._attr_device_class, "power")
        self.assertEqual(self.entity._attr_mode, "box")

    def test_value_and_limits_follow_reported_config(self) -> None:
        self.assertTrue(self.entity.available)
        self.assertEqual(self.entity.native_value, 93)
        self.assertEqual(self.entity.native_min_value, 0)
        self.assertEqual(self.entity.native_max_value, 800)
        self.coordinator.data.update(self._data(w=422, min_w=50, max_w=600))
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
            self._data(available=False),
            self._data(min_w=100),
            self._data(max_w=90),
            self._data(min_w=-1),
            self._data(w="93"),
            self._data(min_w="0"),
            self._data(max_w="800"),
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
            self._data(available=False, w=None, min_w=None, max_w=None)
        )
        self.assertFalse(self.entity.available)
        self.assertIsNone(self.entity.native_value)
        self.assertEqual(self.entity.native_min_value, 0)
        self.assertEqual(self.entity.native_max_value, 0)

    async def test_set_integer_watts_without_optimistic_state(self) -> None:
        await self.entity.async_set_native_value(422.0)
        self.setter.assert_awaited_once_with(422)
        self.assertEqual(self.entity.native_value, 93)

    async def test_rejects_fractional_watts(self) -> None:
        with self.assertRaisesRegex(_ServiceValidationError, "whole number of watts"):
            await self.entity.async_set_native_value(422.5)
        self.setter.assert_not_awaited()


class DischargePowerNumberTests(_PowerNumberTests, unittest.IsolatedAsyncioTestCase):
    _key = "discharge_power"
    _name = "Discharge power"
    _entity_class = number.DjiPowerDischargePowerNumber


class ChargePowerNumberTests(_PowerNumberTests, unittest.IsolatedAsyncioTestCase):
    _key = "charge_power"
    _name = "Recharge power"
    _entity_class = number.DjiPowerChargePowerNumber


class _PowerCoordinatorTests:
    _key: str

    def setUp(self) -> None:
        self.device = types.SimpleNamespace(data={f"{self._key}_w": 93})
        self.setter = AsyncMock()
        setattr(self.device, f"set_{self._key}", self.setter)
        self.coordinator = coordinator_module.DjiPowerCoordinator.__new__(
            coordinator_module.DjiPowerCoordinator
        )
        self.coordinator.device = self.device
        self.coordinator._publish = Mock()

    async def test_publishes_confirmed_device_state_after_write(self) -> None:
        async def confirmed_write(watts: int) -> None:
            self.coordinator._publish.assert_not_called()
            self.device.data[f"{self._key}_w"] = watts

        self.setter.side_effect = confirmed_write
        await getattr(self.coordinator, f"async_set_{self._key}")(422)
        self.setter.assert_awaited_once_with(422)
        self.coordinator._publish.assert_called_once_with({f"{self._key}_w": 422})
        self.assertIsNot(self.coordinator._publish.call_args.args[0], self.device.data)

    async def test_failed_write_is_service_error_without_publishing(self) -> None:
        self.setter.side_effect = _DjiPowerError("readback timed out")
        with self.assertRaisesRegex(_HomeAssistantError, "readback timed out"):
            await getattr(self.coordinator, f"async_set_{self._key}")(422)
        self.coordinator._publish.assert_not_called()


class DischargePowerCoordinatorTests(
    _PowerCoordinatorTests, unittest.IsolatedAsyncioTestCase
):
    _key = "discharge_power"


class ChargePowerCoordinatorTests(
    _PowerCoordinatorTests, unittest.IsolatedAsyncioTestCase
):
    _key = "charge_power"
