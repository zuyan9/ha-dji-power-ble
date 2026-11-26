"""Offline checks for selector entities and their coordinator bridges."""

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


class DisplaySettingSelectTests(unittest.IsolatedAsyncioTestCase):
    async def test_v2_controls_appear_once_a_complete_record_is_reported(self) -> None:
        listeners = []
        entities = []
        coordinator = types.SimpleNamespace(
            entry=types.SimpleNamespace(data={"address": "AA:BB:CC:DD:EE:FF"}),
            device=types.SimpleNamespace(model="DJI Power 1000 V2"),
            last_update_success=True,
            data={},
            async_add_listener=lambda listener: listeners.append(listener) or Mock(),
            async_set_display_setting=AsyncMock(),
        )
        entry = types.SimpleNamespace(entry_id="station", async_on_unload=Mock())
        hass = types.SimpleNamespace(data={"dji_power_ble": {"station": coordinator}})
        await select.async_setup_entry(hass, entry, entities.extend)
        self.assertEqual(entities, [])
        coordinator.data["display_settings"] = {
            "device_timeout": 10800, "screen_timeout": 0,
            "ac_timeout": 1800,
        }
        for listener in listeners:
            listener()
        self.assertEqual(
            [entity.current_option for entity in entities],
            ["3 h", "Never", "30 min"],
        )
        await entities[1].async_select_option("10 s")
        coordinator.async_set_display_setting.assert_awaited_once_with(
            "screen_timeout", "10 s"
        )


# A Power 1000 Mini's 500 W and 1000 W speeds; each one's last byte is its switch.
SLOW_SELECTED = "1f10060001f4010000011f10060002e803000002"
FAST_SELECTED = "1f10060001f4010000021f10060002e803000001"


class RechargeModeSelectTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.listeners = []
        self.entities = []
        self.coordinator = types.SimpleNamespace(
            entry=types.SimpleNamespace(data={"address": "AA:BB:CC:DD:EE:FF"}),
            device=types.SimpleNamespace(model="DJI Power 1000 Mini"),
            last_update_success=True,
            data={},
            async_add_listener=(
                lambda listener: self.listeners.append(listener) or Mock()
            ),
            async_set_recharge_mode=AsyncMock(),
        )
        entry = types.SimpleNamespace(entry_id="station", async_on_unload=Mock())
        hass = types.SimpleNamespace(
            data={"dji_power_ble": {"station": self.coordinator}}
        )
        await select.async_setup_entry(hass, entry, self.entities.extend)

    def publish(self, **data) -> None:
        self.coordinator.data = data
        for listener in self.listeners:
            listener()

    def test_selector_appears_once_rule_8_and_a_speed_list_are_reported(self) -> None:
        for data in (
            {"key_1e": SLOW_SELECTED},
            {"key_1e": SLOW_SELECTED, "station_rules": [7, 11]},
            {"station_rules": [8]},
        ):
            with self.subTest(data=data):
                self.publish(**data)
                self.assertEqual(self.entities, [])
        self.publish(key_1e=SLOW_SELECTED, station_rules=[8])
        self.assertEqual(
            [type(entity) for entity in self.entities],
            [select.DjiPowerRechargeModeSelect],
        )
        entity = self.entities[0]
        self.assertEqual(entity._attr_unique_id, "AA:BB:CC:DD:EE:FF_recharge_mode")
        self.assertTrue(entity.available)
        self.assertEqual(entity.options, ["500 W", "1000 W"])
        self.assertEqual(entity.current_option, "500 W")
        self.publish(key_1e=FAST_SELECTED, station_rules=[8])
        self.assertEqual(entity.current_option, "1000 W")
        self.assertEqual(len(self.entities), 1)

    def test_missing_rule_list_or_connection_is_unavailable(self) -> None:
        self.publish(key_1e=SLOW_SELECTED, station_rules=[8])
        entity = self.entities[0]
        for data in (
            {"key_1e": SLOW_SELECTED, "station_rules": None},
            {"key_1e": SLOW_SELECTED, "station_rules": [7]},
            {"station_rules": [8]},
        ):
            with self.subTest(data=data):
                self.publish(**data)
                self.assertFalse(entity.available)
        self.assertEqual(entity.options, [])
        self.assertIsNone(entity.current_option)
        self.publish(key_1e=SLOW_SELECTED, station_rules=[8])
        self.assertTrue(entity.available)
        self.coordinator.last_update_success = False
        self.assertFalse(entity.available)

    async def test_selection_forwards_reported_watts_without_optimistic_change(
        self,
    ) -> None:
        self.publish(key_1e=SLOW_SELECTED, station_rules=[8])
        entity = self.entities[0]
        await entity.async_select_option("1000 W")
        self.coordinator.async_set_recharge_mode.assert_awaited_once_with(1000)
        self.assertEqual(entity.current_option, "500 W")

    async def test_unoffered_option_is_not_forwarded(self) -> None:
        self.publish(key_1e=SLOW_SELECTED, station_rules=[8])
        for option in ("750 W", "1000", ""):
            with (
                self.subTest(option=option),
                self.assertRaisesRegex(_ServiceValidationError, "does not offer"),
            ):
                await self.entities[0].async_select_option(option)
        self.coordinator.async_set_recharge_mode.assert_not_awaited()


class PowerAdjustmentSelectTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.coordinator = types.SimpleNamespace(
            entry=types.SimpleNamespace(data={"address": "AA:BB:CC:DD:EE:FF"}),
            device=types.SimpleNamespace(model="DJI Power 2000"),
            last_update_success=True,
            data={
                "power_adjustment": "Manual", "eco_available": True,
                "station_rules": [5, 6],
            },
            async_set_power_adjustment=AsyncMock(),
            async_add_listener=Mock(return_value=Mock()),
        )
        self.entity = select.DjiPowerAdjustmentSelect(self.coordinator)

    async def test_known_models_wait_for_reported_selector_capability(self) -> None:
        entry = types.SimpleNamespace(entry_id="station", async_on_unload=Mock())
        hass = types.SimpleNamespace(
            data={"dji_power_ble": {"station": self.coordinator}}
        )
        for model in (
            "DJI Power 2000",
            "DJI Power Auro 2000 Elite",
            "DJI Power 1000",
            "DJI Power 1000 V2",
            "DJI Power 1000 Mini",
        ):
            with self.subTest(model=model):
                self.coordinator.device.model = model
                self.coordinator.data = {}
                listeners = []
                self.coordinator.async_add_listener.side_effect = (
                    lambda listener, listeners=listeners:
                        listeners.append(listener) or Mock()
                )
                add_entities = Mock()
                await select.async_setup_entry(hass, entry, add_entities)
                add_entities.assert_not_called()
                self.coordinator.data = {
                    "power_adjustment": "Manual", "eco_available": True,
                    "station_rules": [5, 6],
                }
                for listener in listeners:
                    listener()
                # The Energy Saver page gate also adds its mode selector.
                added = [
                    entity for call_ in add_entities.call_args_list
                    for entity in call_.args[0]
                ]
                self.assertEqual(
                    [type(entity) for entity in added],
                    [
                        select.DjiPowerEnergySaverModeSelect,
                        select.DjiPowerAdjustmentSelect,
                    ],
                )
                entities = added[1:]
                self.assertTrue(entities[0].available)
                self.coordinator.data["station_rules"] = None
                self.assertFalse(entities[0].available)
                self.coordinator.data["station_rules"] = [5, 6]
                for listener in listeners:
                    listener()
                self.assertTrue(entities[0].available)
                self.assertEqual(add_entities.call_count, 2)

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


class RechargeModeCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.device = types.SimpleNamespace(
            data={"key_1e": SLOW_SELECTED}, set_recharge_mode=AsyncMock()
        )
        self.coordinator = coordinator_module.DjiPowerCoordinator.__new__(
            coordinator_module.DjiPowerCoordinator
        )
        self.coordinator.device = self.device
        self.coordinator._publish = Mock()

    async def test_publishes_device_state_only_after_confirmed_selection(self) -> None:
        async def confirmed_write(watts: int) -> None:
            self.coordinator._publish.assert_not_called()
            self.device.data["key_1e"] = FAST_SELECTED

        self.device.set_recharge_mode.side_effect = confirmed_write
        await self.coordinator.async_set_recharge_mode(1000)
        self.device.set_recharge_mode.assert_awaited_once_with(1000)
        self.coordinator._publish.assert_called_once_with({"key_1e": FAST_SELECTED})
        self.assertIsNot(self.coordinator._publish.call_args.args[0], self.device.data)

    async def test_selection_error_is_service_error_without_publishing(self) -> None:
        self.device.set_recharge_mode.side_effect = _DjiPowerError(
            "station did not report the requested recharge speed"
        )
        with self.assertRaisesRegex(_HomeAssistantError, "did not report"):
            await self.coordinator.async_set_recharge_mode(1000)
        self.coordinator._publish.assert_not_called()
