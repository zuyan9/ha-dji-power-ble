"""Offline checks for power-adjustment selection and its coordinator bridge."""

from __future__ import annotations

import types
import unittest
from unittest.mock import AsyncMock, Mock

from tests.test_number import (
    _DjiPowerError,
    _HomeAssistantError,
    _ServiceValidationError,
    coordinator_module,
    select,
)


class PowerAdjustmentSelectTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.coordinator = types.SimpleNamespace(
            entry=types.SimpleNamespace(data={"address": "AA:BB:CC:DD:EE:FF"}),
            device=types.SimpleNamespace(model="DJI Power 2000"),
            last_update_success=True,
            data={"power_adjustment": "Manual"},
            async_set_power_adjustment=AsyncMock(),
            async_add_listener=Mock(return_value=Mock()),
        )
        self.entity = select.DjiPowerAdjustmentSelect(self.coordinator)

    async def test_only_power_2000_gets_selector_before_config_arrives(self) -> None:
        self.coordinator.data = {}
        entry = types.SimpleNamespace(entry_id="station", async_on_unload=Mock())
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
                await select.async_setup_entry(hass, entry, add_entities)
                if model == "DJI Power 2000":
                    entities = add_entities.call_args.args[0]
                    self.assertEqual(len(entities), 1)
                    self.assertIsInstance(entities[0], select.DjiPowerAdjustmentSelect)
                    self.assertFalse(entities[0].available)
                else:
                    add_entities.assert_not_called()

    def test_reported_mode_updates_without_watt_bounds(self) -> None:
        for mode in ("Manual", "Automatic", "Manual"):
            with self.subTest(mode=mode):
                self.coordinator.data["power_adjustment"] = mode
                self.assertTrue(self.entity.available)
                self.assertEqual(self.entity.current_option, mode)
                self.assertEqual(self.entity.options, ["Manual", "Automatic"])

    def test_missing_unknown_or_disconnected_state_is_unavailable(self) -> None:
        for data in ({}, {"power_adjustment": None}, {"power_adjustment": "Unknown"}):
            with self.subTest(data=data):
                self.coordinator.data = data
                self.assertFalse(self.entity.available)
                self.assertIsNone(self.entity.current_option)
                self.assertEqual(self.entity.options, ["Manual", "Automatic"])
        self.coordinator.data = {"power_adjustment": "Automatic"}
        self.coordinator.last_update_success = False
        self.assertFalse(self.entity.available)

    async def test_valid_selection_forwards_without_optimistic_change(self) -> None:
        for mode in ("Automatic", "Manual"):
            with self.subTest(mode=mode):
                self.coordinator.async_set_power_adjustment.reset_mock()
                await self.entity.async_select_option(mode)
                self.coordinator.async_set_power_adjustment.assert_awaited_once_with(
                    mode
                )
                self.assertEqual(self.entity.current_option, "Manual")

    async def test_invalid_option_is_not_forwarded(self) -> None:
        for mode in ("Unknown", "manual", ""):
            with (
                self.subTest(mode=mode),
                self.assertRaisesRegex(_ServiceValidationError, "Manual or Automatic"),
            ):
                await self.entity.async_select_option(mode)
        self.coordinator.async_set_power_adjustment.assert_not_awaited()


class PowerAdjustmentCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.device = types.SimpleNamespace(
            data={"power_adjustment": "Manual"}, set_power_adjustment=AsyncMock()
        )
        self.coordinator = coordinator_module.DjiPowerCoordinator.__new__(
            coordinator_module.DjiPowerCoordinator
        )
        self.coordinator.device = self.device
        self.coordinator._publish = Mock()

    async def test_publishes_device_state_only_after_confirmed_selection(self) -> None:
        async def confirmed_write(mode: str) -> None:
            self.coordinator._publish.assert_not_called()
            self.device.data["power_adjustment"] = mode

        self.device.set_power_adjustment.side_effect = confirmed_write
        await self.coordinator.async_set_power_adjustment("Automatic")
        self.device.set_power_adjustment.assert_awaited_once_with("Automatic")
        self.coordinator._publish.assert_called_once_with(
            {"power_adjustment": "Automatic"}
        )
        self.assertIsNot(self.coordinator._publish.call_args.args[0], self.device.data)

    async def test_selection_error_is_service_error_without_publishing(self) -> None:
        self.device.set_power_adjustment.side_effect = _DjiPowerError(
            "Automatic requires a linked power meter"
        )
        with self.assertRaisesRegex(_HomeAssistantError, "linked power meter"):
            await self.coordinator.async_set_power_adjustment("Automatic")
        self.coordinator._publish.assert_not_called()
