"""Offline checks for battery times, expansion packs, and sensor discovery."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

COMPONENT = Path(__file__).parents[1] / "custom_components" / "dji_power_ble"
PACKAGE = "_dji_power_sensor_tests"
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

    @property
    def device_info(self):
        return self._attr_device_info


class _Description(types.SimpleNamespace):
    def __init__(self, **kwargs) -> None:
        super().__init__(
            **{
                "entity_category": None,
                "entity_registry_enabled_default": True,
                "translation_key": None,
                **kwargs,
            }
        )


def _module(name: str, **attributes) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    return module


def _load_sensor() -> types.ModuleType:
    """Load real entities against isolated Home Assistant registry interfaces."""
    modules = {
        PACKAGE: _module(PACKAGE, __path__=[str(COMPONENT)]),
        f"{PACKAGE}.coordinator": _module(
            f"{PACKAGE}.coordinator", DjiPowerCoordinator=object
        ),
        "homeassistant": _module("homeassistant"),
        "homeassistant.components": _module("homeassistant.components"),
        "homeassistant.components.sensor": _module(
            "homeassistant.components.sensor",
            SensorEntity=type("SensorEntity", (), {}),
            SensorEntityDescription=_Description,
            SensorDeviceClass=types.SimpleNamespace(
                BATTERY="battery",
                POWER="power",
                TEMPERATURE="temperature",
                DURATION="duration",
                ENERGY_STORAGE="energy_storage",
                ENUM="enum",
                VOLTAGE="voltage",
            ),
            SensorStateClass=types.SimpleNamespace(MEASUREMENT="measurement"),
        ),
        "homeassistant.config_entries": _module(
            "homeassistant.config_entries", ConfigEntry=object
        ),
        "homeassistant.const": _module(
            "homeassistant.const",
            CONF_ADDRESS="address",
            PERCENTAGE="%",
            EntityCategory=types.SimpleNamespace(DIAGNOSTIC="diagnostic"),
            UnitOfElectricPotential=types.SimpleNamespace(VOLT="V"),
            UnitOfEnergy=types.SimpleNamespace(WATT_HOUR="Wh"),
            UnitOfPower=types.SimpleNamespace(WATT="W"),
            UnitOfTemperature=types.SimpleNamespace(CELSIUS="°C"),
            UnitOfTime=types.SimpleNamespace(MINUTES="min", SECONDS="s"),
        ),
        "homeassistant.core": _module(
            "homeassistant.core", HomeAssistant=object, callback=lambda method: method
        ),
        "homeassistant.helpers": _module("homeassistant.helpers"),
        "homeassistant.helpers.device_registry": _module(
            "homeassistant.helpers.device_registry",
            DeviceInfo=dict,
            CONNECTION_BLUETOOTH="bluetooth",
            async_get=lambda hass: hass.device_registry,
        ),
        "homeassistant.helpers.entity_registry": _module(
            "homeassistant.helpers.entity_registry",
            async_get=lambda hass: hass.entity_registry,
            async_entries_for_config_entry=lambda registry, entry_id: [
                item for item in registry.entries if item.config_entry_id == entry_id
            ],
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
        for name in ("entity", "sensor"):
            spec = importlib.util.spec_from_file_location(
                f"{PACKAGE}.{name}", COMPONENT / f"{name}.py"
            )
            assert spec and spec.loader
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
    return module


sensor = _load_sensor()


def _pack(serial: str = "SYNTHETIC00001", **values) -> dict:
    return {
        "seq": 0,
        "serial_number": serial,
        "battery_percent": 65.25,
        "cycle_count": 123,
        "rated_capacity_wh": 2048,
        "temperature": None,
        "firmware": None,
        **values,
    }


class _EntityRegistry:
    def __init__(self) -> None:
        self.entries = []

    def async_get_entity_id(self, domain, platform, unique_id):
        return next(
            (item.entity_id for item in self.entries if item.unique_id == unique_id),
            None,
        )

    def async_remove(self, entity_id) -> None:
        self.entries = [item for item in self.entries if item.entity_id != entity_id]

    def register(self, unique_id, entry_id="station", domain="sensor", platform=DOMAIN):
        if self.async_get_entity_id(domain, platform, unique_id):
            return
        self.entries.append(
            types.SimpleNamespace(
                unique_id=unique_id,
                entity_id=f"{domain}.synthetic_{len(self.entries)}",
                domain=domain,
                platform=platform,
                config_entry_id=entry_id,
            )
        )


class _DeviceRegistry:
    def __init__(self) -> None:
        self.devices = {}
        self.updates = []

    def async_get_device(self, *, identifiers):
        return self.devices.get(next(iter(identifiers)))

    def async_update_device(self, device_id, **values) -> None:
        self.updates.append((device_id, values))
        for device in self.devices.values():
            if device.id == device_id:
                device.__dict__.update(values)

    def register(self, info) -> None:
        if (via := info.get("via_device")) and via not in self.devices:
            raise AssertionError("Station must be registered before a linked pack")
        identifier = next(iter(info["identifiers"]))
        if identifier not in self.devices:
            self.devices[identifier] = types.SimpleNamespace(
                id=f"device_{len(self.devices)}",
                sw_version=None,
            )
        self.devices[identifier].__dict__.update(info)


class BatteryTimeSensorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.unload_callbacks = []
        self.entry = types.SimpleNamespace(
            entry_id="station",
            title="Power station",
            data={"address": ADDRESS},
            async_on_unload=self.unload_callbacks.append,
        )
        self.coordinator = types.SimpleNamespace(
            entry=self.entry,
            device=types.SimpleNamespace(
                address=ADDRESS,
                model="DJI Power 2000",
                serial_number="SYNTHETICBASE",
            ),
            data={},
            last_update_success=True,
            async_add_listener=lambda listener: lambda: None,
        )
        self.entities = {
            description.key: sensor.DjiPowerBatteryTimeSensor(
                self.coordinator, description
            )
            for description in sensor.BATTERY_TIME_DESCRIPTIONS
        }

    async def test_discovery_preserves_existing_identity_across_models(self) -> None:
        for model in (
            "DJI Power 2000",
            "DJI Power 1000 V2",
            "DJI Power 1000 Mini",
            "DJI Power 1000",
            "DJI Power 500",
        ):
            with self.subTest(model=model):
                self.coordinator.device.model = model
                hass = types.SimpleNamespace(
                    data={DOMAIN: {self.entry.entry_id: self.coordinator}},
                    entity_registry=_EntityRegistry(),
                    device_registry=_DeviceRegistry(),
                )
                legacy_id = f"{ADDRESS}_runtime_min"
                hass.entity_registry.register(legacy_id)
                registered = hass.entity_registry.async_get_entity_id(
                    "sensor", DOMAIN, legacy_id
                )
                added = []
                await sensor.async_setup_entry(hass, self.entry, added.extend)
                times = [
                    entity for entity in added
                    if isinstance(entity, sensor.DjiPowerBatteryTimeSensor)
                ]
                self.assertEqual(len(times), 2)
                self.assertEqual(
                    {entity._attr_unique_id for entity in times},
                    {legacy_id, f"{ADDRESS}_recharging_time_min"},
                )
                self.assertEqual(
                    sum(entity._attr_unique_id == legacy_id for entity in added), 1
                )
                self.assertEqual(
                    hass.entity_registry.async_get_entity_id(
                        "sensor", DOMAIN, legacy_id
                    ),
                    registered,
                )
                for entity in times:
                    self.assertIsNone(entity.native_value)
                    self.assertFalse(entity.available)

    def test_names_units_and_default_visibility(self) -> None:
        self.assertEqual(
            {
                key: entity.entity_description.name
                for key, entity in self.entities.items()
            },
            {
                "runtime_min": "Remaining Time",
                "recharging_time_min": "Recharging Time",
            },
        )
        for entity in self.entities.values():
            description = entity.entity_description
            self.assertEqual(description.device_class, "duration")
            self.assertEqual(description.native_unit_of_measurement, "min")
            self.assertTrue(description.entity_registry_enabled_default)

    def test_mode_transitions_publish_only_the_applicable_time(self) -> None:
        for time_type, minutes, active_key in (
            (0, 5940, "runtime_min"),
            (1, 90, "recharging_time_min"),
            (2, 135, "runtime_min"),
            (1, 45, "recharging_time_min"),
            (0, 5940, "runtime_min"),
        ):
            with self.subTest(time_type=time_type, minutes=minutes):
                self.coordinator.data = {
                    "battery_time_type": time_type,
                    "runtime_min": minutes,
                    "input_w": 400,
                    "output_w": 380,
                }
                for key, entity in self.entities.items():
                    self.assertEqual(entity.available, key == active_key)
                    self.assertEqual(
                        entity.native_value, minutes if key == active_key else None
                    )

    def test_missing_or_unknown_type_clears_both_times(self) -> None:
        for data in (
            None,
            {},
            {"runtime_min": 120},
            {"runtime_min": 120, "battery_time_type": None},
            {"runtime_min": 120, "battery_time_type": 3},
            {"runtime_min": 120, "battery_time_type": 255},
        ):
            with self.subTest(data=data):
                self.coordinator.data = data
                for entity in self.entities.values():
                    self.assertIsNone(entity.native_value)
                    self.assertFalse(entity.available)

    def test_missing_duration_does_not_use_primary_or_a_derived_value(self) -> None:
        for time_type in (0, 1, 2):
            for missing in ({}, {"runtime_min": None}):
                with self.subTest(time_type=time_type, missing=missing):
                    self.coordinator.data = {
                        "battery_time_type": time_type,
                        "primary_runtime_min": 120,
                        "recharging_time_min": 90,
                        "input_w": 500,
                        **missing,
                    }
                    for entity in self.entities.values():
                        self.assertIsNone(entity.native_value)
                        self.assertFalse(entity.available)

    def test_zero_and_99_hours_are_preserved_in_each_applicable_mode(self) -> None:
        for time_type, active_key in (
            (0, "runtime_min"),
            (1, "recharging_time_min"),
            (2, "runtime_min"),
        ):
            for minutes in (0, 5940):
                with self.subTest(time_type=time_type, minutes=minutes):
                    self.coordinator.data = {
                        "battery_time_type": time_type, "runtime_min": minutes
                    }
                    self.assertTrue(self.entities[active_key].available)
                    self.assertEqual(self.entities[active_key].native_value, minutes)

    def test_coordinator_failure_and_recovery_preserve_active_time(self) -> None:
        for time_type, active_key in (
            (1, "recharging_time_min"), (2, "runtime_min")
        ):
            with self.subTest(time_type=time_type):
                self.coordinator.data = {
                    "battery_time_type": time_type, "runtime_min": 120
                }
                self.coordinator.last_update_success = False
                self.assertTrue(
                    all(not entity.available for entity in self.entities.values())
                )
                self.coordinator.last_update_success = True
                self.assertTrue(self.entities[active_key].available)
                self.assertEqual(self.entities[active_key].native_value, 120)

    def test_battery_cycle_count_is_a_station_diagnostic(self) -> None:
        description = next(
            item for item in sensor.DESCRIPTIONS if item.key == "battery_cycle_count"
        )
        entity = sensor.DjiPowerSensor(self.coordinator, description)

        self.assertEqual(entity._attr_unique_id, f"{ADDRESS}_battery_cycle_count")
        self.assertEqual(description.translation_key, "battery_cycle_count")
        self.assertEqual(description.entity_category, "diagnostic")
        self.assertTrue(description.entity_registry_enabled_default)
        self.assertIsNone(entity.native_value)
        for count in (0, 19):
            self.coordinator.data = {"battery_cycle_count": count}
            self.assertEqual(entity.native_value, count)

    def test_primary_runtime_remains_an_independent_disabled_sensor(self) -> None:
        description = next(
            item for item in sensor.DESCRIPTIONS if item.key == "primary_runtime_min"
        )
        entity = sensor.DjiPowerSensor(self.coordinator, description)
        self.assertEqual(entity._attr_unique_id, f"{ADDRESS}_primary_runtime_min")
        self.assertFalse(description.entity_registry_enabled_default)
        for time_type in (0, 1, 2):
            self.coordinator.data = {
                "battery_time_type": time_type,
                "runtime_min": 120,
                "primary_runtime_min": 37,
            }
            self.assertEqual(entity.native_value, 37)


class ExpansionSensorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.listeners = []
        self.unload_callbacks = []
        self.entry = types.SimpleNamespace(
            entry_id="station",
            title="Power station",
            data={"address": ADDRESS},
            async_on_unload=self.unload_callbacks.append,
        )
        self.coordinator = types.SimpleNamespace(
            entry=self.entry,
            device=types.SimpleNamespace(
                address=ADDRESS,
                model="DJI Power 2000",
                serial_number="SYNTHETICBASE",
            ),
            data={},
            last_update_success=True,
            async_add_listener=self.add_listener,
        )
        self.hass = types.SimpleNamespace(
            data={DOMAIN: {self.entry.entry_id: self.coordinator}},
            entity_registry=_EntityRegistry(),
            device_registry=_DeviceRegistry(),
        )
        self.entities = []

    def add_listener(self, listener):
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)

    def add_entities(self, entities) -> None:
        for entity in entities:
            self.entities.append(entity)
            self.hass.device_registry.register(entity.device_info)
            self.hass.entity_registry.register(entity._attr_unique_id)

    async def setup(self, packs=None) -> None:
        self.coordinator.data = {"expansion_batteries": packs}
        await sensor.async_setup_entry(self.hass, self.entry, self.add_entities)

    def publish(self, packs) -> None:
        self.coordinator.data = {"expansion_batteries": packs}
        for listener in self.listeners.copy():
            listener()

    def packs(self) -> list:
        return [
            entity
            for entity in self.entities
            if isinstance(entity, sensor.DjiPowerExpansionSensor)
        ]

    def pack_sensor(self, serial, key):
        return next(
            entity
            for entity in self.packs()
            if entity._attr_unique_id == f"expansion_{serial}_{key}"
        )

    async def test_connected_pack_has_linked_device_and_requested_metrics(self) -> None:
        pack = _pack(temperature=23.5, firmware="01.02.03.04")
        await self.setup([pack])
        self.assertEqual(
            len(self.entities) - len(self.packs()),
            len(sensor.DESCRIPTIONS) + len(sensor.BATTERY_TIME_DESCRIPTIONS) + 1,
        )
        self.assertEqual(len(self.packs()), 4)
        self.assertEqual(len(self.hass.device_registry.devices), 2)
        for entity in self.packs():
            key = entity.entity_description.key
            self.assertTrue(entity.available)
            self.assertEqual(entity.native_value, pack[key])
            self.assertEqual(entity.device_info["via_device"], (DOMAIN, ADDRESS))
            self.assertEqual(entity.device_info["serial_number"], pack["serial_number"])
            self.assertEqual(entity.device_info["sw_version"], pack["firmware"])
            self.assertEqual(entity.device_info["manufacturer"], "DJI")
            self.assertEqual(
                entity.device_info["model"], "DJI Power Expansion Battery 2000"
            )
            self.assertNotIn("connections", entity.device_info)
            self.assertTrue(entity.entity_description.entity_registry_enabled_default)
            self.assertEqual(
                entity.entity_description.entity_category,
                "diagnostic" if key in ("cycle_count", "rated_capacity_wh") else None,
            )

    async def test_hotplug_and_reordering_preserve_identity(self) -> None:
        await self.setup([])
        self.assertEqual(self.packs(), [])
        first, second = _pack(), _pack("SYNTHETIC00002", seq=1, battery_percent=40)
        self.publish([first, second])
        entities = self.packs().copy()
        self.assertEqual(len(entities), 6)
        self.publish(
            [
                second | {"seq": 0, "battery_percent": 45},
                first | {"seq": 1, "battery_percent": 70},
            ]
        )
        self.assertEqual(self.packs(), entities)
        self.assertEqual(
            self.pack_sensor(first["serial_number"], "battery_percent").native_value, 70
        )
        self.assertEqual(
            self.pack_sensor(second["serial_number"], "battery_percent").native_value,
            45,
        )

    async def test_unavailable_on_removal_invalid_snapshot_and_disconnect(self) -> None:
        first, second = _pack(), _pack("SYNTHETIC00002")
        await self.setup([first, second])
        self.publish([second])
        for entity in self.packs():
            self.assertEqual(
                entity.available, entity._serial == second["serial_number"]
            )
        for packs in ([], None):
            self.publish(packs)
            self.assertTrue(all(not entity.available for entity in self.packs()))
        self.publish([first, second])
        self.coordinator.last_update_success = False
        self.assertTrue(all(not entity.available for entity in self.packs()))
        self.coordinator.last_update_success = True
        self.assertTrue(all(entity.available for entity in self.packs()))
        self.assertEqual(len(self.packs()), 6)

    async def test_temperature_appears_once_and_becomes_unavailable(self) -> None:
        pack = _pack()
        await self.setup([pack])
        self.assertEqual(len(self.packs()), 3)
        self.publish([pack | {"temperature": 0}])
        temperature = self.pack_sensor(pack["serial_number"], "temperature")
        self.assertTrue(temperature.available)
        self.assertEqual(temperature.native_value, 0)
        self.publish([pack])
        self.assertFalse(temperature.available)
        self.assertIsNone(temperature.native_value)
        self.publish([pack | {"temperature": 25.75}])
        self.assertTrue(temperature.available)
        self.assertEqual(temperature.native_value, 25.75)
        self.assertEqual(len(self.packs()), 4)

    async def test_zero_values_are_valid_and_missing_percentage_is_not(self) -> None:
        pack = _pack(battery_percent=0, cycle_count=0)
        await self.setup([pack])
        self.assertTrue(all(entity.available for entity in self.packs()))
        self.publish([pack | {"battery_percent": None}])
        self.assertFalse(
            self.pack_sensor(pack["serial_number"], "battery_percent").available
        )
        self.assertTrue(
            self.pack_sensor(pack["serial_number"], "cycle_count").available
        )

    async def test_firmware_updates_when_reported_and_is_retained(self) -> None:
        pack = _pack()
        await self.setup([pack])
        device = self.hass.device_registry.async_get_device(
            identifiers={(DOMAIN, f"expansion_{pack['serial_number']}")}
        )
        self.assertIsNone(device.sw_version)
        for firmware in ("01.00.00.00", "01.01.00.00"):
            self.publish([pack | {"firmware": firmware}])
            self.assertEqual(device.sw_version, firmware)
            self.assertEqual(self.packs()[0].device_info["sw_version"], firmware)
        self.publish([pack | {"firmware": "01.01.00.00"}])
        self.publish([pack])
        self.assertEqual(device.sw_version, "01.01.00.00")
        self.assertEqual(len(self.hass.device_registry.updates), 2)

    async def test_reload_restores_absent_packs_and_cleans_up_listener(self) -> None:
        pack = _pack(temperature=20, firmware="01.00.00.00")
        await self.setup([pack])
        unique_ids = {entity._attr_unique_id for entity in self.packs()}
        # Expansion packs and SDC accessories each keep one discovery listener.
        self.assertEqual(len(self.listeners), 2)
        for callback in self.unload_callbacks:
            callback()
        self.assertEqual(self.listeners, [])
        self.unload_callbacks.clear()
        self.entities.clear()
        await self.setup([])
        self.assertEqual(
            {entity._attr_unique_id for entity in self.packs()}, unique_ids
        )
        self.assertTrue(all(not entity.available for entity in self.packs()))
        device = self.hass.device_registry.async_get_device(
            identifiers={(DOMAIN, f"expansion_{pack['serial_number']}")}
        )
        self.assertEqual(device.sw_version, "01.00.00.00")
        self.publish([pack])
        self.assertEqual(len(self.packs()), 4)
        self.assertTrue(all(entity.available for entity in self.packs()))

    async def test_ignores_missing_serial_and_unrelated_registry_entries(self) -> None:
        registry = self.hass.entity_registry
        registry.register("expansion_OTHER_battery_percent", entry_id="other")
        registry.register("expansion_WRONG_battery_percent", platform="another")
        registry.register("expansion_WRONG_battery_percent", domain="number")
        registry.register("expansion__battery_percent")
        registry.register("expansion_UNKNOWN_unknown_metric")
        await self.setup([_pack(""), _pack(None)])
        self.assertEqual(self.packs(), [])

    async def test_entity_translations_are_defined_and_synchronized(self) -> None:
        import json

        strings = json.loads((COMPONENT / "strings.json").read_text())
        translated = json.loads((COMPONENT / "translations/en.json").read_text())
        self.assertEqual(strings, translated)
        for description in (*sensor.DESCRIPTIONS, *sensor.EXPANSION_DESCRIPTIONS):
            if description.translation_key is None:
                continue
            self.assertTrue(
                strings["entity"]["sensor"][description.translation_key]["name"]
            )
        self.assertEqual(
            strings["entity"]["sensor"]["battery_cycle_count"]["name"],
            "Battery cycle count",
        )
        self.assertEqual(
            strings["entity"]["sensor"]["time_periods"]["name"],
            "Electricity price time periods",
        )

    async def test_time_period_sensor_created_only_for_power_2000(self) -> None:
        for model in (
            "DJI Power 2000", "DJI Power 1000 V2", "DJI Power 1000 Mini",
            "DJI Power 1000", "DJI Power",
        ):
            with self.subTest(model=model):
                self.entities.clear()
                self.coordinator.device.model = model
                await self.setup([])
                schedules = [
                    item for item in self.entities
                    if isinstance(item, sensor.DjiPowerTimePeriodsSensor)
                ]
                self.assertEqual(len(schedules), int(model == "DJI Power 2000"))
                if schedules:
                    self.assertEqual(
                        schedules[0]._attr_unique_id, f"{ADDRESS}_time_periods"
                    )
                    self.assertFalse(schedules[0].available)

    async def test_schedule_count_attributes_and_availability(self) -> None:
        await self.setup([])
        entity = next(
            item for item in self.entities
            if isinstance(item, sensor.DjiPowerTimePeriodsSensor)
        )
        period = {
            "type": "off_peak", "days": ["mon"], "start": "22:00", "end": "06:00"
        }
        for periods in ([], [period]):
            self.coordinator.data = {
                "time_periods": periods, "timezone_offset_min": 0
            }
            self.assertTrue(entity.available)
            self.assertEqual(entity.native_value, len(periods))
            self.assertEqual(
                entity.extra_state_attributes,
                {"periods": periods, "timezone_offset_min": 0},
            )
        self.coordinator.last_update_success = False
        self.assertFalse(entity.available)
        self.coordinator.last_update_success = True
        for invalid in (None, "invalid"):
            self.coordinator.data = {"time_periods": invalid}
            self.assertFalse(entity.available)
            self.assertIsNone(entity.native_value)
        self.coordinator.data = {}
        self.assertFalse(entity.available)
        self.assertNotIn("timezone_offset_min", entity.extra_state_attributes)


def _solar(input_w: int, volts: float) -> dict:
    return {
        "form": 1, "form_name": "solar", "output_w": 0, "input_w": input_w,
        "output_voltage_v": 0.0, "input_voltage_v": volts,
    }


def _sdc(*rows: dict, seq: int = 1, interface_type: int = 5, **values) -> dict:
    return {
        "group_type": 4, "group_name": "sdc", "seq": seq, "type": interface_type,
        "type_name": "sdc", "switch_state": 0, "enabled": None, "output_w": 0,
        "input_w": sum(row["input_w"] for row in rows), "accessory_type": 4,
        "accessory_inputs": list(rows), **values,
    }


class SdcAccessorySensorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.listeners = []
        self.unload_callbacks = []
        self.entry = types.SimpleNamespace(
            entry_id="station",
            title="Power station",
            data={"address": ADDRESS},
            async_on_unload=self.unload_callbacks.append,
        )
        self.coordinator = types.SimpleNamespace(
            entry=self.entry,
            device=types.SimpleNamespace(
                address=ADDRESS, model="DJI Power 1000", serial_number=None
            ),
            data={},
            last_update_success=True,
            async_add_listener=self.add_listener,
        )
        self.hass = types.SimpleNamespace(
            data={DOMAIN: {self.entry.entry_id: self.coordinator}},
            entity_registry=_EntityRegistry(),
            device_registry=_DeviceRegistry(),
        )
        self.entities = []

    def add_listener(self, listener):
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)

    def publish(self, **data) -> None:
        self.coordinator.data = data
        for listener in self.listeners.copy():
            listener()

    def sdc_entities(self) -> dict:
        return {
            entity._attr_unique_id.removeprefix(f"{ADDRESS}_"): entity
            for entity in self.entities
            if isinstance(entity, sensor._DjiPowerSdcSensor)
        }

    async def setup(self, **data) -> None:
        self.coordinator.data = data
        await sensor.async_setup_entry(self.hass, self.entry, self.entities.extend)

    async def test_charger_identity_firmware_and_both_solar_inputs(self) -> None:
        await self.setup(
            interfaces=[_sdc(_solar(39, 38.9), _solar(43, 40.12))],
            accessories=[{"type": 4, "firmware": "00.00.03.20"}],
        )

        entities = self.sdc_entities()
        self.assertEqual(
            {key: entity._attr_name for key, entity in entities.items()},
            {
                "5_1_accessory": "SDC 1 accessory",
                "5_1_accessory_firmware": "SDC 1 accessory firmware",
                "5_1_solar_1_power": "SDC 1 solar 1 power",
                "5_1_solar_1_voltage": "SDC 1 solar 1 voltage",
                "5_1_solar_2_power": "SDC 1 solar 2 power",
                "5_1_solar_2_voltage": "SDC 1 solar 2 voltage",
            },
        )
        self.assertEqual(
            {key: entity.native_value for key, entity in entities.items()},
            {
                "5_1_accessory": "solar_car_charger_1_8kw",
                "5_1_accessory_firmware": "00.00.03.20",
                "5_1_solar_1_power": 39,
                "5_1_solar_1_voltage": 38.9,
                "5_1_solar_2_power": 43,
                "5_1_solar_2_voltage": 40.12,
            },
        )
        self.assertTrue(all(entity.available for entity in entities.values()))
        accessory = entities["5_1_accessory"]
        self.assertEqual(accessory._attr_device_class, "enum")
        self.assertEqual(accessory._attr_entity_category, "diagnostic")
        self.assertEqual(
            accessory._attr_options, list(sensor.SDC_ACCESSORY_NAMES.values())
        )
        power, voltage = entities["5_1_solar_1_power"], entities["5_1_solar_1_voltage"]
        self.assertEqual(
            (power._attr_device_class, power._attr_native_unit_of_measurement),
            ("power", "W"),
        )
        self.assertEqual(
            (voltage._attr_device_class, voltage._attr_native_unit_of_measurement),
            ("voltage", "V"),
        )
        self.assertEqual(power._attr_state_class, "measurement")
        self.assertEqual(power.device_info["identifiers"], {(DOMAIN, ADDRESS)})

    async def test_idle_input_reads_zero_and_missing_accessory_is_unavailable(self):
        await self.setup(interfaces=[_sdc(_solar(39, 38.9), _solar(43, 40.12))])
        entities = self.sdc_entities()
        self.publish(interfaces=[_sdc(_solar(12, 37.5))])
        self.assertEqual(entities["5_1_solar_1_power"].native_value, 12)
        self.assertEqual(entities["5_1_solar_2_power"].native_value, 0)
        self.assertIsNone(entities["5_1_solar_2_voltage"].native_value)
        self.assertTrue(entities["5_1_solar_2_power"].available)
        self.publish(interfaces=[_sdc()])
        self.assertEqual(entities["5_1_solar_1_power"].native_value, 0)
        self.assertTrue(entities["5_1_accessory"].available)
        for interfaces in (
            [],
            [_sdc(accessory_type=0)],
            [{key: value for key, value in _sdc().items() if key != "accessory_type"}],
            [_sdc(), _sdc()],
        ):
            with self.subTest(interfaces=interfaces):
                self.publish(interfaces=interfaces)
                self.assertTrue(
                    all(not entity.available for entity in entities.values())
                )
        self.publish(interfaces=[_sdc(_solar(5, 30.0))])
        self.assertEqual(self.sdc_entities(), entities)
        self.coordinator.last_update_success = False
        self.assertTrue(all(not entity.available for entity in entities.values()))

    async def test_car_and_grid_inputs_are_added_when_reported(self) -> None:
        car = {
            "form": 2, "form_name": "car", "output_w": 0, "input_w": 600,
            "output_voltage_v": 13.8, "input_voltage_v": 13.8,
        }
        grid = {
            "form": 3, "form_name": "grid", "output_w": 300, "input_w": 0,
            "output_voltage_v": 230.0, "input_voltage_v": 0.0,
        }
        await self.setup(interfaces=[_sdc(_solar(39, 38.9))])
        self.publish(interfaces=[_sdc(car, grid, _solar(39, 38.9))])
        values = {
            key: entity.native_value for key, entity in self.sdc_entities().items()
        }
        self.assertEqual(values["5_1_car_1_recharge_power"], 600)
        self.assertEqual(values["5_1_car_1_charge_power"], 0)
        self.assertEqual(values["5_1_car_1_voltage"], 13.8)
        self.assertEqual(values["5_1_grid_1_power"], 300)
        self.assertEqual(values["5_1_grid_1_voltage"], 230.0)
        self.assertEqual(values["5_1_solar_1_power"], 39)
        self.assertEqual(
            self.sdc_entities()["5_1_car_1_recharge_power"]._attr_name,
            "SDC 1 car recharge power",
        )
        cable = car | {"output_voltage_v": 0.0, "input_voltage_v": 12.4}
        self.publish(interfaces=[_sdc(cable, accessory_type=1)])
        self.assertEqual(self.sdc_entities()["5_1_car_1_voltage"].native_value, 12.4)
        self.assertEqual(
            self.sdc_entities()["5_1_accessory"].native_value, "car_power_outlet_cable"
        )

    async def test_firmware_matches_ports_by_type_and_order_only_when_unambiguous(
        self,
    ) -> None:
        await self.setup(
            interfaces=[
                _sdc(), _sdc(seq=2, accessory_type=3), _sdc(seq=1, interface_type=6)
            ],
            accessories=[
                {"type": 4, "firmware": "A"},
                {"type": 3, "firmware": "B"},
                {"type": 4, "firmware": "C"},
            ],
        )
        entities = self.sdc_entities()
        self.assertEqual(entities["5_1_accessory_firmware"].native_value, "A")
        self.assertEqual(entities["5_2_accessory_firmware"].native_value, "B")
        self.assertEqual(entities["6_1_accessory_firmware"].native_value, "C")
        self.assertEqual(entities["6_1_accessory"]._attr_name, "SDC Lite 1 accessory")
        for accessories in (
            None, [], [{"type": 4, "firmware": "A"}], [{"type": 3, "firmware": None}]
        ):
            with self.subTest(accessories=accessories):
                self.coordinator.data = self.coordinator.data | {
                    "accessories": accessories
                }
                self.assertFalse(entities["5_2_accessory_firmware"].available)

    async def test_other_models_and_unrecognized_ports_create_no_entities(self) -> None:
        interfaces = [_sdc(_solar(39, 38.9))]
        for model in ("DJI Power 1000 Mini", "DJI Power 500", "DJI Power"):
            with self.subTest(model=model):
                self.entities.clear()
                self.coordinator.device.model = model
                await self.setup(interfaces=interfaces)
                self.assertEqual(self.sdc_entities(), {})
        self.coordinator.device.model = "DJI Power 2000"
        self.entities.clear()
        await self.setup(
            interfaces=[
                _sdc(accessory_type=0),
                _sdc(seq=2, accessory_type=9),
                {"type": 2, "seq": 1, "accessory_type": 4},
            ]
        )
        self.assertEqual(self.sdc_entities(), {})

    async def test_accessory_states_are_translated(self) -> None:
        import json

        strings = json.loads((COMPONENT / "strings.json").read_text())
        states = strings["entity"]["sensor"]["sdc_accessory"]["state"]
        self.assertEqual(set(states), set(sensor.SDC_ACCESSORY_NAMES.values()))
        self.assertEqual(states["solar_car_charger_1_8kw"], "1.8kW Solar/Car Charger")
