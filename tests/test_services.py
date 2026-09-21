"""Offline action validation and station-device routing checks."""

from __future__ import annotations

import importlib.util
import json
import sys
import types
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import voluptuous as vol

COMPONENT = Path(__file__).parents[1] / "custom_components" / "dji_power_ble"
PACKAGE = "_dji_power_service_tests"
DOMAIN = "dji_power_ble"
ADDRESS = "AA:BB:CC:DD:EE:FF"


class ServiceValidationError(Exception):
    """HA action validation error replacement."""


def _module(name: str, **attributes) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    return module


def _load_services() -> types.ModuleType:
    modules = {
        PACKAGE: _module(PACKAGE, __path__=[str(COMPONENT)]),
        "homeassistant": _module("homeassistant"),
        "homeassistant.core": _module(
            "homeassistant.core", HomeAssistant=object, ServiceCall=object,
            callback=lambda function: function,
        ),
        "homeassistant.exceptions": _module(
            "homeassistant.exceptions", ServiceValidationError=ServiceValidationError
        ),
        "homeassistant.helpers": _module("homeassistant.helpers"),
        "homeassistant.helpers.device_registry": _module(
            "homeassistant.helpers.device_registry",
            async_get=lambda hass: hass.device_registry,
        ),
    }
    with patch.dict(sys.modules, modules):
        spec = importlib.util.spec_from_file_location(
            f"{PACKAGE}.services", COMPONENT / "services.py"
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    return module


services = _load_services()
PERIOD = {"type": "off_peak", "start": "00:30", "end": "05:30"}


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.coordinator = types.SimpleNamespace(
            device=types.SimpleNamespace(
                address=ADDRESS, model="DJI Power 2000", is_connected=True
            ),
            last_update_success=True,
            async_set_time_periods=AsyncMock(),
        )
        self.device_entry = types.SimpleNamespace(
            identifiers={(DOMAIN, ADDRESS)}, config_entries={"station"}
        )
        self.hass = types.SimpleNamespace(
            data={DOMAIN: {"station": self.coordinator}},
            device_registry=types.SimpleNamespace(
                async_get=Mock(return_value=self.device_entry)
            ),
            services=types.SimpleNamespace(async_register=Mock()),
        )
        services.async_setup_services(self.hass)
        self.handler = self.hass.services.async_register.call_args.args[2]
        self.schema = self.hass.services.async_register.call_args.kwargs["schema"]

    async def invoke(self, periods=None, **extra) -> None:
        data = self.schema({
            "device_id": "power-2000",
            "periods": [PERIOD] if periods is None else periods,
            **extra,
        })
        await self.handler(types.SimpleNamespace(data=data))

    async def test_registration_and_normalized_full_list(self) -> None:
        self.assertEqual(
            self.hass.services.async_register.call_args.args[:2],
            (DOMAIN, "set_time_periods"),
        )
        await self.invoke()
        self.hass.device_registry.async_get.assert_called_once_with("power-2000")
        self.coordinator.async_set_time_periods.assert_awaited_once_with([
            PERIOD | {"days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}
        ])

    async def test_form_times_and_yaml_times_produce_the_same_schedule(self) -> None:
        yaml_periods = [
            PERIOD,
            {"type": "peak", "days": ["sun", "mon"], "start": "22:00", "end": "00:00"},
        ]
        form_periods = [
            {**period, "start": period["start"] + ":00", "end": period["end"] + ":00"}
            for period in reversed(yaml_periods)
        ]
        original = deepcopy(form_periods)
        await self.invoke(yaml_periods)
        expected = self.coordinator.async_set_time_periods.call_args.args[0]
        self.coordinator.async_set_time_periods.reset_mock()

        await self.invoke(form_periods)

        self.coordinator.async_set_time_periods.assert_awaited_once_with(expected)
        self.assertEqual(form_periods, original)

    async def test_mixed_form_and_yaml_times_are_accepted(self) -> None:
        await self.invoke([PERIOD | {"start": "00:30:00"}])
        self.coordinator.async_set_time_periods.assert_awaited_once_with([
            PERIOD | {"days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}
        ])

    async def test_seconds_are_rejected_without_rounding_or_mutation(self) -> None:
        for field in ("start", "end"):
            for time in ("00:30:01", "00:30:59", "00:30:00.001"):
                periods = [PERIOD | {field: time}]
                original = deepcopy(periods)
                with (
                    self.subTest(field=field, time=time),
                    self.assertRaises(vol.Invalid),
                ):
                    await self.invoke(periods)
                self.assertEqual(periods, original)
        self.coordinator.async_set_time_periods.assert_not_awaited()

    async def test_form_times_still_require_a_valid_complete_schedule(self) -> None:
        period = PERIOD | {"start": "00:30:00", "end": "05:30:00"}
        for periods in (
            [period | {"days": []}],
            [period | {"days": ["mon", "mon"]}],
            [period | {"days": ["monday"]}],
            [period | {"start": "24:00:00"}],
            [period | {"end": "00:30:00"}],
            [period | {"start": "00:60:00"}],
            [period | {"start": "0:30:00"}],
            [period | {"start": "aa:bb:00"}],
            [period | {"start": None}],
            [period | {"end": 1800}],
            [period | {"cycle": "daily"}],
            [period, period | {"type": "peak"}],
            [period, None],
            [
                {"type": "peak", "start": f"{hour:02}:00:00", "end": f"{hour:02}:30:00"}
                for hour in range(9)
            ],
            [
                {
                    "type": "peak", "days": ["sun"],
                    "start": "23:00:00", "end": "01:00:00",
                },
                {
                    "type": "off_peak", "days": ["mon"],
                    "start": "00:30:00", "end": "02:00:00",
                },
            ],
        ):
            original = deepcopy(periods)
            with self.subTest(periods=periods), self.assertRaises(vol.Invalid):
                await self.invoke(periods)
            self.assertEqual(periods, original)
        self.coordinator.async_set_time_periods.assert_not_awaited()

    async def test_empty_list_reaches_device_for_fresh_mode_check(self) -> None:
        await self.invoke([])
        self.coordinator.async_set_time_periods.assert_awaited_once_with([])

    async def test_other_models_are_rejected_before_write(self) -> None:
        for model in ("DJI Power 1000 V2", "DJI Power 1000 Mini", "DJI Power 1000"):
            with self.subTest(model=model):
                self.coordinator.device.model = model
                with self.assertRaisesRegex(
                    ServiceValidationError, "only on Power 2000"
                ):
                    await self.invoke()
        self.coordinator.async_set_time_periods.assert_not_awaited()

    async def test_child_and_unrelated_devices_cannot_target_station(self) -> None:
        for identifiers in (
            {(DOMAIN, "expansion_SYNTHETIC")}, {("other", ADDRESS)},
            {(DOMAIN, "AA:BB:CC:DD:EE:00")},
        ):
            with self.subTest(identifiers=identifiers):
                self.device_entry.identifiers = identifiers
                with self.assertRaisesRegex(ServiceValidationError, "loaded DJI Power"):
                    await self.invoke()
        self.coordinator.async_set_time_periods.assert_not_awaited()

    async def test_missing_or_unloaded_devices_give_action_error(self) -> None:
        self.hass.device_registry.async_get.return_value = None
        with self.assertRaisesRegex(ServiceValidationError, "no longer exists"):
            await self.invoke()
        self.hass.device_registry.async_get.return_value = self.device_entry
        self.hass.data[DOMAIN].clear()
        with self.assertRaisesRegex(ServiceValidationError, "loaded DJI Power"):
            await self.invoke()
        self.coordinator.async_set_time_periods.assert_not_awaited()

    async def test_disconnect_or_failed_update_prevents_write(self) -> None:
        for connected, success in ((False, True), (True, False), (False, False)):
            with self.subTest(connected=connected, success=success):
                self.coordinator.device.is_connected = connected
                self.coordinator.last_update_success = success
                with self.assertRaisesRegex(ServiceValidationError, "unavailable"):
                    await self.invoke()
        self.coordinator.async_set_time_periods.assert_not_awaited()

    async def test_invalid_schedule_never_reaches_device(self) -> None:
        for periods in (
            "invalid", {}, [PERIOD | {"start": "1:00"}],
            [PERIOD | {"days": []}], [PERIOD | {"type": "normal"}],
            [PERIOD, PERIOD], [PERIOD | {"end": "00:30"}],
        ):
            with self.subTest(periods=periods), self.assertRaises(vol.Invalid):
                await self.invoke(periods)
        with self.assertRaises(vol.Invalid):
            await self.invoke(mode="scheduled")
        self.coordinator.async_set_time_periods.assert_not_awaited()

    def test_action_requires_explicit_device_and_periods(self) -> None:
        for data in ({}, {"device_id": "id"}, {"periods": []}):
            with self.subTest(data=data), self.assertRaises(vol.Invalid):
                self.schema(data)

    def test_translations_are_synchronized(self) -> None:
        strings = json.loads((COMPONENT / "strings.json").read_text())
        translated = json.loads((COMPONENT / "translations/en.json").read_text())
        self.assertEqual(strings, translated)
        action = strings["services"]["set_time_periods"]
        self.assertTrue(action["name"])
        self.assertEqual(set(action["fields"]), {"device_id", "periods"})
