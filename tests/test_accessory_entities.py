"""Offline accessory discovery, availability and service routing checks."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

COMPONENT = Path(__file__).parents[1] / "custom_components" / "dji_power_ble"
PACKAGE = "_dji_power_accessory_entity_tests"
ADDRESS = "AA:BB:CC:DD:EE:FF"


class _ServiceValidationError(Exception):
    pass


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


def _load_entities() -> dict[str, types.ModuleType]:
    modules = {
        PACKAGE: _module(PACKAGE, __path__=[str(COMPONENT)]),
        f"{PACKAGE}.coordinator": _module(
            f"{PACKAGE}.coordinator", DjiPowerCoordinator=object
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
            "homeassistant.components.select", SelectEntity=type("SelectEntity", (), {})
        ),
        "homeassistant.components.switch": _module(
            "homeassistant.components.switch",
            SwitchEntity=type("SwitchEntity", (), {}),
            SwitchDeviceClass=types.SimpleNamespace(OUTLET="outlet"),
        ),
        "homeassistant.config_entries": _module(
            "homeassistant.config_entries", ConfigEntry=object
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
            "homeassistant.exceptions", ServiceValidationError=_ServiceValidationError
        ),
        "homeassistant.helpers": _module("homeassistant.helpers"),
        "homeassistant.helpers.entity_platform": _module(
            "homeassistant.helpers.entity_platform", AddEntitiesCallback=object
        ),
        "homeassistant.helpers.device_registry": _module(
            "homeassistant.helpers.device_registry",
            DeviceInfo=dict,
            CONNECTION_BLUETOOTH="bluetooth",
        ),
        "homeassistant.helpers.update_coordinator": _module(
            "homeassistant.helpers.update_coordinator",
            CoordinatorEntity=_CoordinatorEntity,
        ),
    }
    loaded = {}
    with patch.dict(sys.modules, modules):
        for name in ("entity", "accessory", "number", "select", "switch"):
            spec = importlib.util.spec_from_file_location(
                f"{PACKAGE}.{name}", COMPONENT / f"{name}.py"
            )
            assert spec and spec.loader
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            loaded[name] = module
    return loaded


modules = _load_entities()
number, select, switch = (modules[name] for name in ("number", "select", "switch"))


def _car(**values) -> dict:
    """A synthetic report with independently bounded recharge settings."""
    return {
        "interface_type": 5,
        "seq": 1,
        "type": 4,
        "sw": 1,
        "mode": 2,
        "p_from_car_low": 100,
        "p_from_car_v": 1000,
        "p_from_car_up": 1800,
        "v_from_car_low": 1100,
        "v_from_car_v": 1250,
        "v_from_car_up": 1400,
        **values,
    }


class _Coordinator:
    def __init__(self, model="DJI Power 2000") -> None:
        self.entry = types.SimpleNamespace(data={"address": ADDRESS}, title="Station")
        self.device = types.SimpleNamespace(
            model=model, address=ADDRESS, serial_number=None
        )
        self.data = {}
        self.last_update_success = True
        self.listeners = []
        self.async_set_car_charger = AsyncMock()
        self.async_set_sdc = AsyncMock()
        self.async_set_usb = AsyncMock()
        self.async_set_energy_reserve = AsyncMock()

    def async_add_listener(self, listener):
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)

    def publish(self, data: dict) -> None:
        self.data = data
        for listener in list(self.listeners):
            listener()


class AccessoryDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.coordinator = _Coordinator()
        self.unloads = []
        self.entry = types.SimpleNamespace(
            entry_id="station", async_on_unload=self.unloads.append
        )
        self.hass = types.SimpleNamespace(
            data={"dji_power_ble": {"station": self.coordinator}}
        )
        self.entities = []

    async def _setup(self) -> None:
        for platform in (switch, select, number):
            await platform.async_setup_entry(
                self.hass, self.entry, self.entities.extend
            )

    def _accessories(self) -> list:
        return [entity for entity in self.entities if hasattr(entity, "_identity")]

    async def test_no_guessed_entities_and_listeners_clean_up(self) -> None:
        await self._setup()
        self.assertEqual(len(self.entities), 6)  # Existing AC, TOU, limits and watts.
        self.assertEqual(self._accessories(), [])
        # Accessory discovery on three platforms, reserve discovery on two.
        self.assertEqual(len(self.coordinator.listeners), 5)
        self.coordinator.publish(
            {
                "car_chargers": [None, {}, _car(type=99), _car(seq=True), _car(sw=0)],
                "power_switches": [
                    {"type": 3, "seq": 1, "sw": 1},
                    {"type": 5, "seq": 1, "sw": 0},
                ],
                "sdc_power": 500,
            }
        )
        self.assertEqual(self._accessories(), [])
        for unload in self.unloads:
            unload()
        self.assertEqual(self.coordinator.listeners, [])

    async def test_reported_accessories_discovered_at_setup(self) -> None:
        self.coordinator.data = {
            "car_chargers": [_car()],
            "power_switches": [{"type": 5, "seq": 1, "sw": 2}],
        }
        await self._setup()
        self.assertEqual(len(self._accessories()), 5)
        for entity in self._accessories():
            self.assertTrue(entity.available)
            self.assertEqual(
                entity._attr_device_info["identifiers"], {("dji_power_ble", ADDRESS)}
            )
            self.assertIn("SDC 1", entity._attr_name)
        self.assertEqual(
            len({entity._attr_unique_id for entity in self.entities}),
            len(self.entities),
        )

    async def test_zero_sequence_is_preserved_when_reported(self) -> None:
        await self._setup()
        self.coordinator.publish({"car_chargers": [_car(seq=0)]})
        self.assertEqual(len(self._accessories()), 4)
        entity = self._accessories()[0]
        self.assertTrue(entity.available)
        await entity.async_turn_on()
        self.coordinator.async_set_car_charger.assert_awaited_once_with(
            5, 0, 4, enabled=True
        )

    async def test_hotplug_reconnect_and_replacement_preserve_identity(self) -> None:
        await self._setup()
        report = {
            "car_chargers": [_car(interface_type=6, seq=7, type=3)],
            "power_switches": [{"type": 6, "seq": 7, "sw": 1}],
        }
        self.coordinator.publish(report)
        original = list(self._accessories())
        self.assertEqual(len(original), 5)
        self.assertTrue(all(entity.available for entity in original))
        self.assertTrue(all("SDC Lite 7" in entity._attr_name for entity in original))
        self.coordinator.publish({"car_chargers": [], "power_switches": []})
        self.assertTrue(all(not entity.available for entity in original))
        self.coordinator.publish(report)
        self.assertEqual(self._accessories(), original)
        self.assertTrue(all(entity.available for entity in original))
        self.coordinator.last_update_success = False
        self.assertTrue(all(not entity.available for entity in original))
        self.coordinator.publish({"car_chargers": [_car(seq=42)]})
        self.assertEqual(self._accessories(), original)
        self.coordinator.last_update_success = True
        self.coordinator.publish(
            report | {"car_chargers": [_car(interface_type=6, seq=7)]}
        )
        self.assertEqual(len(self._accessories()), 9)
        old_car = [entity for entity in original if entity._row_key == "car_chargers"]
        self.assertTrue(all(not entity.available for entity in old_car))

    async def test_duplicate_identity_is_unavailable_without_duplicate_discovery(
        self,
    ) -> None:
        await self._setup()
        self.coordinator.publish({"car_chargers": [_car(), _car(mode=1)]})
        self.assertEqual(self._accessories(), [])
        self.coordinator.publish({"car_chargers": [_car()]})
        self.assertEqual(len(self._accessories()), 4)
        self.coordinator.publish({"car_chargers": [_car(), _car()]})
        self.assertTrue(all(not entity.available for entity in self._accessories()))

    async def test_unsupported_model_does_not_register_or_create_accessory_controls(
        self,
    ) -> None:
        self.coordinator.device.model = "DJI Power"
        self.coordinator.data = {
            "car_chargers": [_car()],
            "power_switches": [{"type": 3, "seq": 1, "sw": 1}],
        }
        await self._setup()
        self.assertEqual(self._accessories(), [])
        self.assertEqual(self.coordinator.listeners, [])

    async def test_usb_model_creates_only_reported_usb_switches(self) -> None:
        self.coordinator.device.model = "DJI Power 1000 Mini"
        self.coordinator.data = {
            "car_chargers": [_car()],
            "power_switches": [
                {"type": 2, "seq": 1, "sw": 1},
                {"type": 3, "seq": 1, "sw": 1},
                {"type": 5, "seq": 1, "sw": 1},
                {"type": 4, "seq": 2, "sw": 2},
                {"type": 4, "seq": 1, "sw": 0},
            ],
        }
        await self._setup()
        self.assertEqual(len(self.coordinator.listeners), 1)
        self.assertEqual(
            [entity._attr_name for entity in self._accessories()],
            ["USB-A1 output", "USB-C2 output"],
        )
        self.assertTrue(all(
            isinstance(entity, switch.DjiPowerUsbSwitch)
            for entity in self._accessories()
        ))
        self.coordinator.publish(
            self.coordinator.data
            | {
                "power_switches": [
                    {"type": 3, "seq": 1, "sw": 2},
                    {"type": 3, "seq": 2, "sw": 1},
                    {"type": 4, "seq": 1, "sw": 1},
                    {"type": 4, "seq": 2, "sw": 1},
                ]
            }
        )
        self.assertEqual(
            [entity._attr_name for entity in self._accessories()],
            ["USB-A1 output", "USB-C2 output", "USB-A2 output", "USB-C1 output"],
        )
        self.assertEqual(
            [entity.is_on for entity in self._accessories()],
            [False, True, True, True],
        )


class AccessoryControlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.coordinator = _Coordinator()
        self.row = _car()
        self.coordinator.data = {
            "car_chargers": [self.row],
            "power_switches": [{"type": 5, "seq": 1, "sw": 1}],
        }
        self.master = switch.DjiPowerCarRechargingSwitch(self.coordinator, (5, 1, 4))
        self.mode = select.DjiPowerCarModeSelect(self.coordinator, (5, 1, 4))
        self.power = number.DjiPowerCarRechargePowerNumber(self.coordinator, (5, 1, 4))
        self.voltage = number.DjiPowerCarMinimumVoltageNumber(
            self.coordinator, (5, 1, 4)
        )
        self.sdc = switch.DjiPowerSdcSwitch(self.coordinator, (5, 1, 0))

    def test_mode_and_enabled_state_gate_only_dependent_controls(self) -> None:
        for mode, label in ((1, "Auto"), (2, "Recharge"), (3, "Charge")):
            with self.subTest(mode=mode):
                self.row["mode"] = mode
                self.assertTrue(self.master.available)
                self.assertTrue(self.mode.available)
                self.assertEqual(self.mode.current_option, label)
                self.assertEqual(self.power.available, mode == 2)
                self.assertEqual(self.voltage.available, mode == 2)
        self.row["sw"] = 2
        self.assertTrue(self.master.available)
        self.assertFalse(self.master.is_on)
        self.assertFalse(self.mode.available)
        self.assertFalse(self.power.available)
        self.assertFalse(self.voltage.available)
        self.assertTrue(self.sdc.available)

    def test_invalid_switch_or_mode_values_are_not_valid_capabilities(self) -> None:
        for value in (None, True, "1", 0, 3):
            with self.subTest(value=value):
                self.row["sw"] = value
                self.assertIsNone(self.master.is_on)
                self.assertFalse(self.master.available)
                self.assertFalse(self.mode.available)
                self.assertFalse(self.power.available)
        self.row["sw"] = 1
        for value in (None, True, "2", 0, 4):
            with self.subTest(mode=value):
                self.row["mode"] = value
                self.assertIsNone(self.mode.current_option)
                self.assertFalse(self.mode.available)
                self.assertFalse(self.voltage.available)
                self.assertFalse(self.master.available)

    def test_sdc_switch_name_does_not_assume_power_direction(self) -> None:
        self.assertEqual(self.sdc._attr_name, "SDC 1 power")
        self.assertEqual(self.sdc._attr_unique_id, f"{ADDRESS}_5_1_0_sdc_power")

    def test_numbers_use_live_independent_bounds_and_voltage_scaling(self) -> None:
        self.assertEqual(self.power.native_value, 1000)
        self.assertEqual(self.power.native_min_value, 100)
        self.assertEqual(self.power.native_max_value, 1800)
        self.assertEqual(self.voltage.native_value, 12.5)
        self.assertEqual(self.voltage.native_min_value, 11)
        self.assertEqual(self.voltage.native_max_value, 14)
        self.row.update(p_to_car_low="bad", v_to_car_up=None, v_auto_v=-1)
        self.assertTrue(self.power.available)
        self.assertTrue(self.voltage.available)
        self.row.update(p_from_car_up=900)
        self.assertFalse(self.power.available)
        self.assertTrue(self.voltage.available)
        self.row.update(p_from_car_v=500, p_from_car_up=1000, v_from_car_low=1300)
        self.assertTrue(self.power.available)
        self.assertEqual(self.power.native_max_value, 1000)
        self.assertFalse(self.voltage.available)
        self.assertIsNone(self.voltage.native_value)
        self.assertEqual(self.voltage.native_min_value, 0)
        self.assertEqual(self.voltage.native_max_value, 0)

    def test_missing_or_malformed_rows_clear_controls(self) -> None:
        for rows in (None, [], [{"seq": 1}], "invalid"):
            with self.subTest(rows=rows):
                self.coordinator.data = {"car_chargers": rows, "power_switches": rows}
                for entity in (
                    self.master, self.mode, self.power, self.voltage, self.sdc
                ):
                    self.assertFalse(entity.available)
                self.assertIsNone(self.power.native_value)
                self.assertEqual(self.power.native_max_value, 0)

    def test_zero_numeric_capability_is_not_writable(self) -> None:
        self.row.update(p_from_car_low=0, p_from_car_v=0, p_from_car_up=0)
        self.assertFalse(self.power.available)
        self.assertTrue(self.voltage.available)

    async def test_services_route_exact_reported_identity_without_optimistic_state(
        self,
    ) -> None:
        setter = self.coordinator.async_set_car_charger
        for action, expected in (
            (self.master.async_turn_off(), {"enabled": False}),
            (self.master.async_turn_on(), {"enabled": True}),
            (self.mode.async_select_option("Auto"), {"mode": 1}),
            (self.mode.async_select_option("Charge"), {"mode": 3}),
            (self.power.async_set_native_value(750), {"recharge_power_w": 750}),
            (self.voltage.async_set_native_value(12.8), {"minimum_voltage_v": 12.8}),
        ):
            setter.reset_mock()
            await action
            setter.assert_awaited_once_with(5, 1, 4, **expected)
        self.assertEqual(self.power.native_value, 1000)
        self.assertEqual(self.voltage.native_value, 12.5)
        self.assertEqual(self.mode.current_option, "Recharge")
        self.assertTrue(self.master.is_on)
        await self.sdc.async_turn_off()
        self.coordinator.async_set_sdc.assert_awaited_once_with(5, 1, False)
        self.coordinator.async_set_sdc.reset_mock()
        await self.sdc.async_turn_on()
        self.coordinator.async_set_sdc.assert_awaited_once_with(5, 1, True)

    async def test_bad_ui_values_are_rejected_before_forwarding(self) -> None:
        with self.assertRaisesRegex(
            _ServiceValidationError, "Auto, Recharge or Charge"
        ):
            await self.mode.async_select_option("Unknown")
        with self.assertRaisesRegex(_ServiceValidationError, "whole number of watts"):
            await self.power.async_set_native_value(750.5)
        self.coordinator.async_set_car_charger.assert_not_awaited()


class UsbSwitchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.coordinator = _Coordinator("DJI Power 1000 Mini")
        self.rows = [
            {"type": 3, "seq": 1, "sw": 1},
            {"type": 3, "seq": 2, "sw": 2},
            {"type": 4, "seq": 1, "sw": 1},
            {"type": 4, "seq": 2, "sw": 2},
        ]
        self.coordinator.data = {"power_switches": self.rows}
        self.switches = {
            (row["type"], row["seq"]): switch.DjiPowerUsbSwitch(
                self.coordinator, (row["type"], row["seq"], 0)
            )
            for row in self.rows
        }

    def test_names_ids_and_state_follow_each_reported_port(self) -> None:
        for (interface_type, seq), entity in self.switches.items():
            with self.subTest(port=(interface_type, seq)):
                port = "USB-A" if interface_type == 3 else "USB-C"
                self.assertEqual(entity._attr_name, f"{port}{seq} output")
                self.assertEqual(
                    entity._attr_unique_id,
                    f"{ADDRESS}_{interface_type}_{seq}_0_usb_output",
                )
                self.assertEqual(entity._attr_device_class, "outlet")
                self.assertTrue(entity.available)
                self.assertEqual(entity.is_on, seq == 1)

    def test_missing_invalid_or_ambiguous_rows_are_unavailable(self) -> None:
        entity = self.switches[(3, 1)]
        for rows in (
            None,
            [],
            "invalid",
            [{"type": 5, "seq": 1, "sw": 1}],
            [{"type": 3, "seq": 1, "sw": 1}, {"type": 3, "seq": 1, "sw": 2}],
        ):
            with self.subTest(rows=rows):
                self.coordinator.data = {"power_switches": rows}
                self.assertFalse(entity.available)
        for value in (None, True, "1", 0, 3):
            with self.subTest(sw=value):
                self.coordinator.data = {
                    "power_switches": [{"type": 3, "seq": 1, "sw": value}]
                }
                self.assertIsNone(entity.is_on)
                self.assertFalse(entity.available)
        self.coordinator.data = {"power_switches": self.rows}
        self.coordinator.last_update_success = False
        self.assertFalse(entity.available)

    async def test_services_route_exact_port_without_optimistic_state(self) -> None:
        for (interface_type, seq), entity in self.switches.items():
            with self.subTest(port=(interface_type, seq)):
                setter = self.coordinator.async_set_usb
                setter.reset_mock()
                await entity.async_turn_off()
                setter.assert_awaited_once_with(interface_type, seq, False)
                setter.reset_mock()
                await entity.async_turn_on()
                setter.assert_awaited_once_with(interface_type, seq, True)
                self.assertEqual(entity.is_on, seq == 1)
        self.coordinator.async_set_sdc.assert_not_awaited()


class BackupReserveEntityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.coordinator = _Coordinator("DJI Power 1000")
        self.coordinator.data = {"energy_reserve_available": True}
        self.entry = types.SimpleNamespace(
            entry_id="station", async_on_unload=lambda unload: None
        )
        self.hass = types.SimpleNamespace(
            data={"dji_power_ble": {"station": self.coordinator}}
        )
        self.added: list = []

    async def _entities(self) -> list:
        for platform in (switch, number):
            await platform.async_setup_entry(self.hass, self.entry, self.added.extend)
        return self._reserve_entities()

    def _reserve_entities(self) -> list:
        return [
            entity
            for entity in self.added
            if isinstance(
                entity,
                (
                    switch.DjiPowerBackupReserveSwitch,
                    number.DjiPowerBackupReserveNumber,
                ),
            )
        ]

    async def test_created_only_for_validated_model_with_stable_identity(self) -> None:
        for model in (
            "DJI Power 1000", "DJI Power 1000 V2", "DJI Power 2000",
            "DJI Power 1000 Mini", "DJI Power",
        ):
            with self.subTest(model=model):
                self.coordinator.device.model = model
                self.coordinator.listeners.clear()
                self.added.clear()
                entities = await self._entities()
                self.assertEqual(
                    len(entities),
                    2 if model not in ("DJI Power 1000 Mini", "DJI Power") else 0,
                )
                if entities:
                    self.assertEqual(
                        {entity._attr_unique_id for entity in entities},
                        {
                            f"{ADDRESS}_custom_backup_reserve",
                            f"{ADDRESS}_backup_reserve_level",
                        },
                    )
                    self.assertEqual(
                        {entity._attr_name for entity in entities},
                        {"Custom backup reserve level", "Backup reserve level"},
                    )
                    self.assertFalse(any(entity.available for entity in entities))

    async def test_created_once_when_the_station_first_offers_the_setting(
        self,
    ) -> None:
        self.coordinator.data = {"energy_reserve_available": False}
        self.assertEqual(await self._entities(), [])
        for data in ({}, {"energy_reserve_available": None}):
            self.coordinator.publish(data)
            self.assertEqual(self._reserve_entities(), [])
        self.coordinator.last_update_success = False
        self.coordinator.publish({"energy_reserve_available": True})
        self.assertEqual(self._reserve_entities(), [])
        self.coordinator.last_update_success = True
        self.coordinator.publish({"energy_reserve_available": True})
        entities = self._reserve_entities()
        self.assertEqual(
            {type(entity) for entity in entities},
            {switch.DjiPowerBackupReserveSwitch, number.DjiPowerBackupReserveNumber},
        )
        self.assertEqual(len(entities), 2)
        # Later offers add nothing; a withdrawn offer keeps the entities unavailable.
        self.coordinator.publish({"energy_reserve_available": True})
        self.coordinator.publish({"energy_reserve_available": False})
        self.assertEqual(self._reserve_entities(), entities)
        self.assertFalse(any(entity.available for entity in entities))

    async def test_availability_follows_offer_switch_and_level(self) -> None:
        reserve_switch, level = await self._entities()
        limits = {"discharge_limit": 5, "recharge_limit": 90}
        for data, switch_available, is_on, level_available in (
            ({}, False, None, False),
            (
                {"energy_reserve_available": False, "energy_reserve_enabled": True,
                 "energy_reserve": 80},
                False, True, False,
            ),
            (
                {"energy_reserve_available": True, "energy_reserve_enabled": False,
                 "energy_reserve": 80},
                True, False, False,
            ),
            (
                {"energy_reserve_available": True, "energy_reserve_enabled": True,
                 "energy_reserve": 80},
                True, True, True,
            ),
            (
                {"energy_reserve_available": True, "energy_reserve_enabled": None,
                 "energy_reserve": 80},
                False, None, False,
            ),
            (
                {"energy_reserve_available": True, "energy_reserve_enabled": True,
                 "energy_reserve": None},
                True, True, False,
            ),
        ):
            with self.subTest(data=data):
                self.coordinator.data = data | limits
                self.assertEqual(reserve_switch.available, switch_available)
                self.assertIs(reserve_switch.is_on, is_on)
                self.assertEqual(level.available, level_available)
        self.assertEqual(level.native_value, None)
        reserve = {
            "energy_reserve_available": True,
            "energy_reserve_enabled": True,
            "energy_reserve": 80,
        }
        self.coordinator.data = reserve | limits
        self.assertEqual(level.native_value, 80)
        self.assertEqual((level.native_min_value, level.native_max_value), (10, 90))
        self.coordinator.data = reserve | {"discharge_limit": 15, "recharge_limit": 100}
        self.assertEqual((level.native_min_value, level.native_max_value), (20, 100))
        for missing in (
            {}, {"discharge_limit": 15, "recharge_limit": 19},
            {"discharge_limit": None, "recharge_limit": 90},
        ):
            with self.subTest(limits=missing):
                self.coordinator.data = reserve | missing
                self.assertFalse(level.available)
                self.assertEqual(
                    (level.native_min_value, level.native_max_value), (0, 100)
                )
        self.coordinator.data = reserve | limits
        self.coordinator.last_update_success = False
        self.assertFalse(reserve_switch.available)
        self.assertFalse(level.available)

    async def test_services_route_only_the_requested_change(self) -> None:
        reserve_switch, level = await self._entities()
        await reserve_switch.async_turn_on()
        await reserve_switch.async_turn_off()
        await level.async_set_native_value(35.0)
        self.assertEqual(
            self.coordinator.async_set_energy_reserve.await_args_list,
            [
                unittest.mock.call(enabled=True),
                unittest.mock.call(enabled=False),
                unittest.mock.call(percent=35),
            ],
        )
        with self.assertRaises(_ServiceValidationError):
            await level.async_set_native_value(35.5)
        self.assertEqual(self.coordinator.async_set_energy_reserve.await_count, 3)
