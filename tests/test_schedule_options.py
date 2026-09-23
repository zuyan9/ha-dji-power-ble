"""Offline tests for the Power 2000 native schedule editor."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from tests.test_connection_options import (
    ADAPTER,
    HomeAssistantError,
    flow_module,
)

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
PEAK = {"type": "peak", "days": DAYS, "start": "17:00", "end": "21:00"}
OFF_PEAK = {"type": "off_peak", "days": DAYS, "start": "00:00", "end": "07:00"}


class ScheduleOptionsTests(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.coordinator = SimpleNamespace(
            device=SimpleNamespace(model="DJI Power 2000"),
            async_get_time_periods=AsyncMock(return_value=deepcopy([PEAK])),
            async_set_time_periods=AsyncMock(),
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
        self.adapters = self.enterContext(
            patch.object(
                flow_module,
                "async_local_adapters",
                AsyncMock(return_value={ADAPTER: f"Local adapter ({ADAPTER})"}),
            )
        )

    async def test_power_2000_menu_keeps_connection_options_accessible(self):
        result = await self.flow.async_step_init()

        self.assertEqual(result["type"], "menu")
        self.assertEqual(result["menu_options"], ["connection", "time_periods"])
        self.coordinator.async_get_time_periods.assert_not_awaited()

        result = await self.flow.async_step_connection()

        self.assertEqual(result["type"], "form")
        self.assertEqual(result["step_id"], "connection")
        submitted = {
            "update_interval": 0,
            "connection_source": ADAPTER,
            "keep_connection": True,
        }
        result = await self.flow.async_step_connection(submitted)
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(
            result["data"], {"future_option": "preserved", **submitted}
        )
        self.coordinator.async_set_time_periods.assert_not_awaited()

    async def test_live_model_takes_precedence_over_stored_model(self):
        self.flow.config_entry.data["model"] = "DJI Power"

        result = await self.flow.async_step_init()

        self.assertEqual(result["type"], "menu")
        self.coordinator.device.model = "DJI Power 1000 V2"
        self.flow.config_entry.data["model"] = "DJI Power 2000"

        result = await self.flow.async_step_init()

        self.assertEqual(result["type"], "form")
        self.assertEqual(result["step_id"], "init")

    async def test_schedule_entry_points_reject_other_models_without_io(self):
        self.coordinator.device.model = "DJI Power 1000 V2"
        self.flow.config_entry.data["model"] = self.coordinator.device.model
        for step in (
            "time_periods", "add_period", "edit_period", "change_period",
            "delete_period", "save_periods", "apply_periods", "schedule_changed",
        ):
            with self.subTest(step=step):
                result = await getattr(self.flow, f"async_step_{step}")()
                self.assertEqual(result["type"], "abort")
                self.assertEqual(result["reason"], "schedule_not_supported")
        self.coordinator.async_get_time_periods.assert_not_awaited()
        self.coordinator.async_set_time_periods.assert_not_awaited()

    async def test_unloaded_station_cannot_open_editor(self):
        self.flow.hass.data = {}

        result = await self.flow.async_step_time_periods()

        self.assertEqual(result["type"], "abort")
        self.assertEqual(result["reason"], "station_unavailable")
        self.coordinator.async_set_time_periods.assert_not_awaited()

    async def test_missing_draft_cannot_open_period_form(self):
        with patch.object(
            self.flow, "_async_load_periods", AsyncMock(return_value=None)
        ):
            result = await self.flow.async_step_add_period()

        self.assertEqual(result, {"type": "abort", "reason": "station_unavailable"})
        self.coordinator.async_set_time_periods.assert_not_awaited()

    async def test_editor_loads_once_and_shows_station_schedule(self):
        result = await self.flow.async_step_time_periods()

        self.assertEqual(result["type"], "menu")
        self.assertEqual(result["step_id"], "time_periods")
        self.assertIn("17:00", result["description_placeholders"]["periods"])
        self.assertIn("21:00", result["description_placeholders"]["periods"])
        self.assertIn("edit_period", result["menu_options"])
        self.assertIn("delete_period", result["menu_options"])
        await self.flow.async_step_time_periods()
        self.coordinator.async_get_time_periods.assert_awaited_once_with()
        self.coordinator.async_set_time_periods.assert_not_awaited()

    async def test_empty_schedule_has_no_edit_or_delete_choices(self):
        self.coordinator.async_get_time_periods.return_value = []

        result = await self.flow.async_step_time_periods()

        self.assertEqual(result["type"], "menu")
        self.assertIn("add_period", result["menu_options"])
        self.assertNotIn("edit_period", result["menu_options"])
        self.assertNotIn("delete_period", result["menu_options"])

    async def test_failed_read_can_retry_without_treating_failure_as_empty(self):
        self.coordinator.async_get_time_periods.side_effect = [
            HomeAssistantError("Station disconnected"), deepcopy([PEAK])
        ]

        result = await self.flow.async_step_time_periods()

        self.assertEqual(result["type"], "form")
        self.assertEqual(result["step_id"], "time_periods")
        self.assertEqual(result["errors"], {"base": "schedule_read_failed"})
        self.assertIn("disconnected", result["description_placeholders"]["reason"])
        self.coordinator.async_set_time_periods.assert_not_awaited()

        result = await self.flow.async_step_time_periods({})

        self.assertEqual(result["type"], "menu")
        self.assertEqual(self.coordinator.async_get_time_periods.await_count, 2)
        self.assertEqual(self.flow._periods, [PEAK])

    async def test_malformed_station_schedule_cannot_be_saved_as_empty(self):
        self.coordinator.async_get_time_periods.return_value = [{"type": "peak"}]

        result = await self.flow.async_step_save_periods({})

        self.assertEqual(result["type"], "form")
        self.assertEqual(result["step_id"], "time_periods")
        self.assertEqual(result["errors"], {"base": "schedule_read_failed"})
        self.assertIsNone(self.flow._periods)
        self.assertIsNone(self.flow._original_periods)
        self.coordinator.async_set_time_periods.assert_not_awaited()

    async def test_add_edit_and_delete_change_draft_until_explicit_save(self):
        await self.flow.async_step_time_periods()
        result = await self.flow.async_step_add_period({
            **OFF_PEAK, "days": list(reversed(DAYS)),
            "start": "00:00:00", "end": "07:00:00",
        })

        self.assertEqual(result["type"], "menu")
        self.assertEqual(self.flow._periods, [PEAK, OFF_PEAK])
        self.assertEqual(self.flow._original_periods, [PEAK])

        result = await self.flow.async_step_edit_period({"period": "0"})

        self.assertEqual(result["step_id"], "change_period")
        edited = {**PEAK, "start": "18:00", "end": "22:00", "days": ["mon"]}
        result = await self.flow.async_step_change_period(edited)
        self.assertEqual(result["type"], "menu")
        self.assertEqual(self.flow._periods, [edited, OFF_PEAK])

        result = await self.flow.async_step_delete_period({"period": "1"})

        self.assertEqual(result["type"], "menu")
        self.assertEqual(self.flow._periods, [edited])
        self.coordinator.async_set_time_periods.assert_not_awaited()
        self.assertEqual(self.coordinator.async_get_time_periods.return_value, [PEAK])

        result = await self.flow.async_step_save_periods()

        self.assertEqual(result["type"], "menu")
        self.assertEqual(result["step_id"], "save_periods")
        self.assertEqual(result["menu_options"], ["apply_periods", "time_periods"])
        self.assertIn("18:00", result["description_placeholders"]["periods"])
        self.coordinator.async_set_time_periods.assert_not_awaited()

        result = await self.flow.async_step_apply_periods()

        self.assertEqual(result, {"type": "abort", "reason": "schedule_saved"})
        self.coordinator.async_set_time_periods.assert_awaited_once_with(
            [edited], expected_periods=[PEAK]
        )
        self.assertEqual(self.flow.config_entry.options, self.original_options)

    async def test_back_from_review_preserves_draft_without_writing(self):
        await self.flow.async_step_add_period(deepcopy(OFF_PEAK))

        result = await self.flow.async_step_save_periods()

        self.assertIn("time_periods", result["menu_options"])
        result = await self.flow.async_step_time_periods()

        self.assertEqual(result["type"], "menu")
        self.assertEqual(result["step_id"], "time_periods")
        self.assertEqual(self.flow._periods, [PEAK, OFF_PEAK])
        self.assertEqual(self.flow._original_periods, [PEAK])
        self.coordinator.async_get_time_periods.assert_awaited_once_with()
        self.coordinator.async_set_time_periods.assert_not_awaited()

    async def test_invalid_additions_preserve_input_and_current_draft(self):
        await self.flow.async_step_time_periods()
        bad_inputs = (
            {**OFF_PEAK, "start": "17:30", "end": "18:30"},
            {**OFF_PEAK, "days": []},
            {**OFF_PEAK, "days": ["mon", "mon"]},
            {**OFF_PEAK, "days": ["monday"]},
            {**OFF_PEAK, "start": "00:00:01"},
            {**OFF_PEAK, "end": "24:00"},
            {**OFF_PEAK, "end": "00:00"},
            {**OFF_PEAK, "type": "standard"},
        )
        for submitted in bad_inputs:
            with self.subTest(submitted=submitted):
                result = await self.flow.async_step_add_period(submitted)

                self.assertEqual(result["type"], "form")
                self.assertEqual(result["step_id"], "add_period")
                self.assertEqual(result["errors"], {"base": "invalid_period"})
                self.assertEqual(self.flow.suggested, submitted)
                self.assertEqual(self.flow._periods, [PEAK])
        self.coordinator.async_set_time_periods.assert_not_awaited()

    async def test_ninth_period_of_one_type_is_rejected(self):
        self.coordinator.async_get_time_periods.return_value = [
            {**PEAK, "start": f"{hour:02}:00", "end": f"{hour:02}:30"}
            for hour in range(8)
        ]
        await self.flow.async_step_time_periods()

        result = await self.flow.async_step_add_period({
            **PEAK, "start": "08:00", "end": "08:30",
        })

        self.assertEqual(result["errors"], {"base": "invalid_period"})
        self.assertEqual(len(self.flow._periods), 8)
        self.coordinator.async_set_time_periods.assert_not_awaited()

    async def test_overnight_period_conflict_checks_following_weekday(self):
        self.coordinator.async_get_time_periods.return_value = [
            {**PEAK, "days": ["mon"], "start": "00:30", "end": "01:30"}
        ]
        await self.flow.async_step_time_periods()

        result = await self.flow.async_step_add_period({
            **OFF_PEAK, "days": ["sun"], "start": "23:00", "end": "01:00",
        })

        self.assertEqual(result["errors"], {"base": "invalid_period"})
        self.assertEqual(len(self.flow._periods), 1)

    async def test_invalid_edit_preserves_original_period(self):
        await self.flow.async_step_time_periods()
        await self.flow.async_step_edit_period({"period": "0"})
        submitted = {**PEAK, "start": "21:00"}

        result = await self.flow.async_step_change_period(submitted)

        self.assertEqual(result["step_id"], "change_period")
        self.assertEqual(result["errors"], {"base": "invalid_period"})
        self.assertEqual(self.flow.suggested, submitted)
        self.assertEqual(self.flow._periods, [PEAK])

    async def test_stale_or_invalid_selection_never_modifies_schedule(self):
        await self.flow.async_step_time_periods()
        for step in ("edit_period", "delete_period"):
            for index in ("-1", "1", "abc", True, 0):
                with self.subTest(step=step, index=index):
                    result = await getattr(self.flow, f"async_step_{step}")(
                        {"period": index}
                    )
                    self.assertEqual(result["type"], "form")
                    self.assertEqual(
                        result["errors"], {"period": "invalid_period_selection"}
                    )
                    self.assertEqual(self.flow._periods, [PEAK])
        self.coordinator.async_set_time_periods.assert_not_awaited()

    async def test_write_failure_retains_draft_for_explicit_retry(self):
        await self.flow.async_step_add_period(deepcopy(OFF_PEAK))
        self.coordinator.async_set_time_periods.side_effect = [
            HomeAssistantError("No matching station readback"), None
        ]

        result = await self.flow.async_step_save_periods({})

        self.assertEqual(result["type"], "menu")
        self.assertEqual(result["step_id"], "save_periods")
        self.assertEqual(result["menu_options"], ["apply_periods", "time_periods"])
        self.assertIn("readback", result["description_placeholders"]["reason"])
        self.assertEqual(self.flow._periods, [PEAK, OFF_PEAK])
        self.assertEqual(self.flow._original_periods, [PEAK])
        self.assertEqual(self.flow.config_entry.options, self.original_options)

        result = await self.flow.async_step_apply_periods()

        self.assertEqual(result["reason"], "schedule_saved")
        self.assertEqual(self.coordinator.async_set_time_periods.await_count, 2)

    async def test_failed_write_can_return_to_edit_then_save_changed_draft(self):
        await self.flow.async_step_add_period(deepcopy(OFF_PEAK))
        self.coordinator.async_set_time_periods.side_effect = [
            HomeAssistantError("No matching station readback"), None
        ]

        result = await self.flow.async_step_apply_periods()

        self.assertIn("time_periods", result["menu_options"])
        await self.flow.async_step_time_periods()
        await self.flow.async_step_edit_period({"period": "1"})
        edited = {**OFF_PEAK, "end": "06:00"}
        await self.flow.async_step_change_period(edited)
        result = await self.flow.async_step_save_periods()

        self.assertEqual(result["step_id"], "save_periods")
        self.assertIn("06:00", result["description_placeholders"]["periods"])
        self.assertEqual(self.coordinator.async_set_time_periods.await_count, 1)

        result = await self.flow.async_step_apply_periods()

        self.assertEqual(result["reason"], "schedule_saved")
        self.coordinator.async_set_time_periods.assert_awaited_with(
            [PEAK, edited], expected_periods=[PEAK]
        )
        self.assertEqual(self.coordinator.async_set_time_periods.await_count, 2)
        self.assertEqual(self.flow.config_entry.options, self.original_options)

    async def test_conflict_requires_explicit_reload_before_reapplying_edits(self):
        await self.flow.async_step_add_period(deepcopy(OFF_PEAK))
        self.coordinator.async_set_time_periods.side_effect = HomeAssistantError(
            "Schedule changed", translation_key="schedule_changed"
        )

        result = await self.flow.async_step_save_periods({})

        self.assertEqual(result["type"], "form")
        self.assertEqual(result["step_id"], "schedule_changed")
        self.assertEqual(self.flow._periods, [PEAK, OFF_PEAK])
        self.coordinator.async_get_time_periods.assert_awaited_once_with()
        latest = {**PEAK, "start": "18:00"}
        self.coordinator.async_get_time_periods.return_value = [latest]

        result = await self.flow.async_step_schedule_changed({})

        self.assertEqual(result["type"], "menu")
        self.assertEqual(self.flow._periods, [latest])
        self.assertEqual(self.flow._original_periods, [latest])
        self.assertEqual(self.coordinator.async_get_time_periods.await_count, 2)
        self.coordinator.async_set_time_periods.assert_awaited_once()

    async def test_discard_never_writes_or_changes_connection_options(self):
        await self.flow.async_step_add_period(deepcopy(OFF_PEAK))

        result = await self.flow.async_step_discard_periods()

        self.assertEqual(result, {"type": "abort", "reason": "schedule_cancelled"})
        self.coordinator.async_set_time_periods.assert_not_awaited()
        self.assertEqual(self.flow.config_entry.options, self.original_options)

    async def test_clearing_all_periods_still_uses_validated_station_write(self):
        await self.flow.async_step_time_periods()
        await self.flow.async_step_delete_period({"period": "0"})
        self.coordinator.async_set_time_periods.side_effect = HomeAssistantError(
            "Disable Energy Optimization before clearing all periods"
        )

        result = await self.flow.async_step_save_periods({})

        self.assertEqual(result["type"], "menu")
        self.assertEqual(result["step_id"], "save_periods")
        self.assertIn(
            "Disable Energy Optimization", result["description_placeholders"]["reason"]
        )
        self.coordinator.async_set_time_periods.assert_awaited_once_with(
            [], expected_periods=[PEAK]
        )
        self.assertEqual(self.flow._periods, [])
