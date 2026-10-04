"""Offline tests for the unsupported advanced options."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from tests.test_connection_options import ADAPTER, _NumberSelector, flow_module

MINIMUM = "recharge_power_minimum"


class AdvancedOptionsTests(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.coordinator = SimpleNamespace(
            device=SimpleNamespace(model="DJI Power 2000"),
            data={"time_periods": [], "eco_available": True, "station_rules": [5, 6]},
        )
        self.flow = flow_module.DjiPowerOptionsFlow()
        self.flow.config_entry = SimpleNamespace(
            entry_id="station",
            data={"model": "DJI Power 2000"},
            options={
                "update_interval": 7,
                "connection_source": ADAPTER,
                "keep_connection": True,
                "future_option": "preserved",
            },
        )
        self.original_options = deepcopy(self.flow.config_entry.options)
        self.flow.hass = SimpleNamespace(
            data={flow_module.DOMAIN: {"station": self.coordinator}}
        )
        self.enterContext(
            patch.object(
                flow_module,
                "async_local_adapters",
                AsyncMock(return_value={ADAPTER: f"Local adapter ({ADAPTER})"}),
            )
        )

    async def test_menu_offers_advanced_with_grid_tied_energy_saver(self):
        original = dict(self.coordinator.data)
        for data, menu in (
            (original, ["connection", "time_periods", "advanced"]),
            (original | {"time_periods": None}, ["connection", "advanced"]),
            (original | {"station_rules": [5]}, ["connection", "time_periods"]),
        ):
            with self.subTest(data=data):
                self.coordinator.data = data

                result = await self.flow.async_step_init()

                self.assertEqual(result["type"], "menu")
                self.assertEqual(result["menu_options"], menu)

    async def test_stations_without_grid_tied_energy_saver_keep_one_page(self):
        original = dict(self.coordinator.data)
        cases = [
            original | {"eco_available": False},
            original | {"station_rules": [6]},
            original | {"station_rules": None},
            {},
        ]
        for data in cases:
            with self.subTest(data=data):
                self.coordinator.data = data
                result = await self.flow.async_step_init()
                self.assertEqual((result["type"], result["step_id"]), ("form", "init"))

        self.coordinator.data = original
        self.coordinator.device.model = "DJI Power"
        result = await self.flow.async_step_init()
        self.assertEqual((result["type"], result["step_id"]), ("form", "init"))

        self.flow.hass.data = {}
        result = await self.flow.async_step_init()
        self.assertEqual((result["type"], result["step_id"]), ("form", "init"))

    async def test_form_offers_optional_whole_watts_and_current_value(self):
        result = await self.flow.async_step_advanced()

        self.assertEqual((result["type"], result["step_id"]), ("form", "advanced"))
        self.assertEqual(result["errors"], {})
        self.assertEqual(self.flow.suggested, {})
        [(marker, selector)] = result["data_schema"].schema.items()
        self.assertEqual(marker.schema, MINIMUM)
        self.assertEqual(type(marker).__name__, "Optional")
        self.assertIsInstance(selector, _NumberSelector)
        self.assertEqual(selector.config["min"], 0)
        self.assertEqual(selector.config["max"], 10000)
        self.assertEqual(selector.config["step"], 1)
        self.assertEqual(selector.config["unit_of_measurement"], "W")

        self.flow.config_entry.options[MINIMUM] = 250
        await self.flow.async_step_advanced()
        self.assertEqual(self.flow.suggested, {MINIMUM: 250})

    async def test_saving_sets_or_clears_minimum_and_keeps_other_options(self):
        for minimum in (250, 250.0, "300", 0, 10000):
            with self.subTest(minimum=minimum):
                result = await self.flow.async_step_advanced({MINIMUM: minimum})

                self.assertEqual(result["type"], "create_entry")
                self.assertEqual(
                    result["data"], self.original_options | {MINIMUM: int(minimum)}
                )
                self.assertIs(type(result["data"][MINIMUM]), int)

        self.flow.config_entry.options[MINIMUM] = 250
        result = await self.flow.async_step_advanced({})

        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["data"], self.original_options)

    async def test_invalid_minimum_keeps_form_and_entered_value(self):
        for minimum in (250.5, -1, 10001, "many"):
            with self.subTest(minimum=minimum):
                result = await self.flow.async_step_advanced({MINIMUM: minimum})

                self.assertEqual(
                    (result["type"], result["step_id"]), ("form", "advanced")
                )
                self.assertEqual(
                    result["errors"], {MINIMUM: "invalid_recharge_power_minimum"}
                )
                self.assertEqual(self.flow.suggested, {MINIMUM: minimum})
