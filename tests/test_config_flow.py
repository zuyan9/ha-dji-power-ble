"""Offline setup validation and recovery tests with synthetic cloud responses."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

import voluptuous as vol

from tests.test_cloud import cloud
from tests.test_connection_options import _OptionsFlow, flow_module

ADDRESS = "aa:bb:cc:dd:ee:ff"
PAIR_KEY = "ab" * 16
PASSWORD = " synthetic-password "


def advertisement(address, manufacturer_data=b"\x94\x10"):
    return SimpleNamespace(
        address=address,
        name="Synthetic station",
        manufacturer_data={flow_module.MANUFACTURER_ID: manufacturer_data},
    )


class ConfigFlowTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.devices = [cloud.DjiDevice("Synthetic station", "SN1", "", PAIR_KEY)]
        self.client = SimpleNamespace(
            get_image_captcha=AsyncMock(return_value=("challenge", b"image")),
            exchange_image_captcha=AsyncMock(return_value="ticket"),
            login=AsyncMock(return_value="US_synthetic"),
            list_devices=AsyncMock(return_value=self.devices),
        )
        self.factory = self.enterContext(
            patch.object(flow_module, "DjiCloudClient", return_value=self.client)
        )
        for name in (
            "DjiCloudError", "DjiAuthError", "DjiTwoFactorRequired", "DjiRateLimited",
        ):
            self.enterContext(patch.object(flow_module, name, getattr(cloud, name)))

    def flow(self):
        flow = flow_module.DjiPowerConfigFlow()
        flow.hass = SimpleNamespace()
        flow._async_current_entries = lambda: []
        flow.async_set_unique_id = AsyncMock()
        flow._abort_if_unique_id_configured = Mock()
        for name in (
            "async_show_form", "async_abort", "async_create_entry",
            "add_suggested_values_to_schema",
        ):
            setattr(flow, name, getattr(_OptionsFlow, name).__get__(flow))
        return flow

    def reconfigure_flow(self, model="DJI Power", has_update_listener=True):
        flow = self.flow()
        entry = SimpleNamespace(
            entry_id="station",
            title="Existing station",
            update_listeners=[Mock()] if has_update_listener else [],
            data={
                "address": ADDRESS,
                "pair_key": PAIR_KEY,
                "name": "Existing station",
                "serial_number": "SN1",
                "model": model,
            },
            options={"keep_connection": True, "connection_source": "adapter"},
        )

        def update_entry(target, *, data):
            changed = target.data != data
            target.data = data
            return changed

        flow._get_reconfigure_entry = Mock(return_value=entry)
        flow.hass.config_entries = SimpleNamespace(
            async_update_entry=Mock(side_effect=update_entry),
            async_schedule_reload=Mock(),
        )
        return flow, entry

    async def test_reconfigure_unresolved_model_has_no_accidental_default(self):
        for configured in (None, "DJI Power", "DJI Power (0xFF)"):
            for candidates in (
                [],
                [advertisement("AA:BB:CC:DD:EE:02")],
                [advertisement(ADDRESS, b"\x94")],
                [advertisement(ADDRESS, b"\xff\x10")],
            ):
                with self.subTest(configured=configured, candidates=candidates):
                    flow, _ = self.reconfigure_flow(configured)
                    with patch.object(
                        flow_module, "async_discovered_service_info",
                        return_value=candidates,
                    ):
                        result = await flow.async_step_reconfigure()
                    self.assertEqual(result["step_id"], "reconfigure")
                    schema = result["data_schema"]
                    with self.assertRaises(vol.Invalid):
                        schema({})
                    field, selector = next(iter(schema.schema.items()))
                    self.assertEqual(field.schema, "model")
                    self.assertEqual(selector.config["mode"], "dropdown")
                    self.assertEqual(
                        selector.config["options"],
                        list(flow_module.MODEL_NAMES.values()),
                    )
                    self.assertEqual(len(schema.schema), 1)
                    flow.hass.config_entries.async_update_entry.assert_not_called()
                    flow.hass.config_entries.async_schedule_reload.assert_not_called()
        self.factory.assert_not_called()

    async def test_reconfigure_defaults_to_existing_model_or_matching_advertisement(
        self,
    ):
        for configured, candidates, expected in (
            ("DJI Power 1000", [advertisement(ADDRESS)], "DJI Power 1000"),
            ("DJI Power", [advertisement(ADDRESS.upper())], "DJI Power 2000"),
            (None, [advertisement(ADDRESS, b"\x98\x10")], "DJI Power 1000 Mini"),
        ):
            with self.subTest(configured=configured, expected=expected):
                flow, _ = self.reconfigure_flow(configured)
                with patch.object(
                    flow_module, "async_discovered_service_info",
                    return_value=candidates,
                ):
                    result = await flow.async_step_reconfigure()
                self.assertEqual(result["data_schema"]({}), {"model": expected})
                flow.hass.config_entries.async_update_entry.assert_not_called()

    async def test_reconfigure_preserves_credentials_and_options_with_one_reload_owner(
        self,
    ):
        for has_listener in (False, True):
            with self.subTest(has_listener=has_listener):
                flow, entry = self.reconfigure_flow(has_update_listener=has_listener)
                previous_data = dict(entry.data)
                previous_options = dict(entry.options)
                result = await flow.async_step_reconfigure({"model": "DJI Power 2000"})
                self.assertEqual(
                    result, {"type": "abort", "reason": "reconfigure_successful"}
                )
                self.assertEqual(
                    entry.data, {**previous_data, "model": "DJI Power 2000"}
                )
                self.assertEqual(entry.options, previous_options)
                self.assertEqual(entry.title, "Existing station")
                flow.hass.config_entries.async_update_entry.assert_called_once_with(
                    entry, data={**previous_data, "model": "DJI Power 2000"}
                )
                reload = flow.hass.config_entries.async_schedule_reload
                if has_listener:
                    reload.assert_not_called()  # The registered listener owns reload.
                else:
                    reload.assert_called_once_with(entry.entry_id)
                flow.async_set_unique_id.assert_not_awaited()
        self.factory.assert_not_called()

    async def test_reconfigure_unchanged_model_reloads_without_listener_notification(
        self,
    ):
        for has_listener in (False, True):
            with self.subTest(has_listener=has_listener):
                flow, entry = self.reconfigure_flow("DJI Power 2000", has_listener)
                result = await flow.async_step_reconfigure({"model": "DJI Power 2000"})
                self.assertEqual(result["reason"], "reconfigure_successful")
                flow.hass.config_entries.async_schedule_reload.assert_called_once_with(
                    entry.entry_id
                )

    async def test_reconfigure_allows_correcting_an_existing_specific_model(self):
        flow, entry = self.reconfigure_flow("DJI Power 1000 V2")
        result = await flow.async_step_reconfigure({"model": "DJI Power 2000"})
        self.assertEqual(result["reason"], "reconfigure_successful")
        self.assertEqual(entry.data["model"], "DJI Power 2000")

    async def test_reconfigure_rejects_unknown_or_missing_model(self):
        for submitted in ({}, {"model": ""}, {"model": "DJI Power"},
                          {"model": "DJI Power 500"}, {"model": None}):
            with self.subTest(submitted=submitted):
                flow, entry = self.reconfigure_flow()
                result = await flow.async_step_reconfigure(submitted)
                self.assertEqual(result["errors"], {"model": "invalid_model"})
                self.assertEqual(entry.data["model"], "DJI Power")
                with self.assertRaises(vol.Invalid):
                    result["data_schema"](submitted)
                flow.hass.config_entries.async_update_entry.assert_not_called()
                flow.hass.config_entries.async_schedule_reload.assert_not_called()
        self.factory.assert_not_called()

    @staticmethod
    def input_for(step, address=ADDRESS):
        return {
            "address": address,
            "name": "My station",
            **{
                "manual": {"pair_key": PAIR_KEY},
                "token": {"member_token": "US_synthetic"},
                "account": {"email": "test@example.invalid", "password": PASSWORD},
            }[step],
        }

    async def test_invalid_addresses_keep_form_input_without_cloud_requests(self):
        for step in ("manual", "token", "account"):
            for address in (
                "", "   ", "oops", "GG:GG:GG:GG:GG:GG", "AA:BB:CC:DD:EE",
                "AA:BB:CC:DD:EE:FFF", "AA:BB-CC:DD:EE:FF", "AA BB CC DD EE FF",
            ):
                with self.subTest(step=step, address=address):
                    flow = self.flow()
                    submitted = self.input_for(step, address)
                    result = await getattr(flow, f"async_step_{step}")(submitted)
                    self.assertEqual(result["type"], "form")
                    self.assertEqual(result["step_id"], step)
                    self.assertEqual(result["errors"]["address"], "invalid_address")
                    self.assertEqual(flow.suggested, submitted)
                    flow.async_set_unique_id.assert_not_awaited()
        self.factory.assert_not_called()

    async def test_all_entry_methods_accept_normalized_address_formats(self):
        for step in ("manual", "token", "account"):
            for address in (
                " AA:BB:CC:DD:EE:FF ", "AA-BB-CC-DD-EE-FF", "AABBCCDDEEFF",
                "aabb.ccdd.eeff",
            ):
                with self.subTest(step=step, address=address):
                    flow = self.flow()
                    result = await getattr(flow, f"async_step_{step}")(
                        self.input_for(step, address)
                    )
                    if step == "account":
                        self.assertEqual(result["step_id"], "captcha")
                        self.assertEqual(flow._address, ADDRESS)
                    else:
                        self.assertEqual(result["type"], "create_entry")
                        self.assertEqual(result["data"]["address"], ADDRESS)

    async def test_changed_discovery_address_resolves_its_own_model(self):
        original = advertisement("AA:BB:CC:DD:EE:01", b"\x91\x10")
        for step in ("manual", "token", "account"):
            for candidates, expected in (
                ([original, advertisement(ADDRESS.upper())], "DJI Power 2000"),
                ([original], "DJI Power"),
                ([original, advertisement(ADDRESS.upper(), b"\x94")], "DJI Power"),
            ):
                with self.subTest(step=step, expected=expected, candidates=candidates):
                    flow = self.flow()
                    flow._discovered_address = original.address
                    flow._discovered_model = "DJI Power 1000"
                    with patch.object(
                        flow_module, "async_discovered_service_info",
                        return_value=candidates,
                    ):
                        handler = getattr(flow, f"async_step_{step}")
                        form = await handler()
                        submitted = form["data_schema"](self.input_for(step))
                        result = await handler(submitted)
                        if step == "account":
                            result = await flow.async_step_captcha(
                                {"captcha_code": "code"}
                            )
                    self.assertEqual(result["type"], "create_entry")
                    self.assertEqual(result["data"]["address"], ADDRESS)
                    self.assertEqual(result["data"]["model"], expected)

    async def test_same_discovery_address_preserves_model_across_formats(self):
        for step in ("manual", "token", "account"):
            for address in (ADDRESS.upper(), "AA-BB-CC-DD-EE-FF", "AABBCCDDEEFF"):
                with self.subTest(step=step, address=address):
                    flow = self.flow()
                    flow._discovered_address = ADDRESS
                    flow._discovered_model = "DJI Power 1000"
                    result = await getattr(flow, f"async_step_{step}")(
                        self.input_for(step, address)
                    )
                    if step == "account":
                        result = await flow.async_step_captcha({"captcha_code": "code"})
                    self.assertEqual(result["data"]["model"], "DJI Power 1000")

    async def test_configured_filter_preserves_remaining_dropdown_keys(self):
        other = advertisement("AA:BB:CC:DD:EE:02")
        for stored in (ADDRESS, "AA-BB-CC-DD-EE-FF", "AABBCCDDEEFF"):
            with self.subTest(stored=stored):
                flow = self.flow()
                flow._async_current_entries = lambda stored=stored: [
                    SimpleNamespace(data={"address": stored})
                ]
                with patch.object(
                    flow_module, "async_discovered_service_info",
                    return_value=[advertisement(ADDRESS.upper()), other],
                ):
                    self.assertEqual(
                        flow._discovered_stations(),
                        {other.address: f"Synthetic station ({other.address})"},
                    )
                    form = await flow.async_step_manual()
                submitted = self.input_for("manual", other.address)
                self.assertEqual(form["data_schema"](submitted), submitted)

    async def test_only_configured_advertisement_leaves_manual_address_editable(self):
        flow = self.flow()
        flow._async_current_entries = lambda: [
            SimpleNamespace(data={"address": ADDRESS})
        ]
        with patch.object(
            flow_module, "async_discovered_service_info",
            return_value=[advertisement(ADDRESS.upper())],
        ):
            form = await flow.async_step_manual()
        submitted = self.input_for("manual", "AA:BB:CC:DD:EE:02")
        self.assertEqual(form["data_schema"](submitted), submitted)

    async def test_account_rejects_blank_credentials_before_fetching_captcha(self):
        for field in ("email", "password"):
            for value in ("", " \t "):
                with self.subTest(field=field, value=value):
                    flow = self.flow()
                    submitted = {**self.input_for("account"), field: value}
                    result = await flow.async_step_account(submitted)
                    self.assertEqual(result["step_id"], "account")
                    self.assertEqual(result["errors"][field], f"{field}_required")
                    self.assertEqual(flow.suggested, submitted)
        self.factory.assert_not_called()

    async def test_missing_captcha_state_aborts_without_cloud_requests(self):
        for missing in ("_address", "_email", "_password", "_srandom", "_client"):
            with self.subTest(missing=missing):
                flow = self.flow()
                flow._address = ADDRESS
                flow._email = "test@example.invalid"
                flow._password = PASSWORD
                flow._srandom = "challenge"
                flow._client = self.client
                setattr(flow, missing, None)
                result = await flow.async_step_captcha({"captcha_code": "code"})
                self.assertEqual(
                    result, {"type": "abort", "reason": "setup_incomplete"}
                )
        self.factory.assert_not_called()
        self.client.get_image_captcha.assert_not_awaited()
        self.client.exchange_image_captcha.assert_not_awaited()
        self.client.login.assert_not_awaited()

    async def test_missing_twofa_state_aborts_without_cloud_requests(self):
        for missing in (
            "_address", "_email", "_password", "_captcha_ticket", "_client",
        ):
            with self.subTest(missing=missing):
                flow = self.flow()
                flow._address = ADDRESS
                flow._email = "test@example.invalid"
                flow._password = PASSWORD
                flow._captcha_ticket = "ticket"
                flow._client = self.client
                setattr(flow, missing, None)
                result = await flow.async_step_twofa({"code": "123456"})
                self.assertEqual(
                    result, {"type": "abort", "reason": "setup_incomplete"}
                )
        self.client.login.assert_not_awaited()

    async def test_missing_finish_state_aborts_before_fetching_devices(self):
        for missing in ("_address", "_client", "_token"):
            with self.subTest(missing=missing):
                flow = self.flow()
                flow._address = ADDRESS
                flow._client = self.client
                flow._token = "US_synthetic"
                setattr(flow, missing, None)
                result = await flow.async_step_finish()
                self.assertEqual(
                    result, {"type": "abort", "reason": "setup_incomplete"}
                )
        self.client.list_devices.assert_not_awaited()

    async def test_missing_address_cannot_create_entry_from_cached_device(self):
        flow = self.flow()
        result = await flow._create_from_device(self.devices[0])
        self.assertEqual(result, {"type": "abort", "reason": "setup_incomplete"})
        flow.async_set_unique_id.assert_not_awaited()

    async def test_account_captcha_and_device_selection_succeed(self):
        flow = self.flow()
        self.devices.append(cloud.DjiDevice("Other station", "SN2", "", "cd" * 16))
        submitted = {**self.input_for("account"), "email": " test@example.invalid "}
        result = await flow.async_step_account(submitted)
        self.assertEqual(result["step_id"], "captcha")
        result = await flow.async_step_captcha({"captcha_code": " code "})
        self.assertEqual(result["step_id"], "finish")
        self.client.exchange_image_captcha.assert_awaited_once_with("challenge", "code")
        self.client.login.assert_awaited_once_with(
            "test@example.invalid", PASSWORD, "ticket"
        )
        result = await flow.async_step_finish({"device": "SN2"})
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["data"]["pair_key"], "cd" * 16)
        self.assertNotIn("password", result["data"])
        self.assertNotIn("member_token", result["data"])

    async def test_twofa_success_preserves_password_exactly(self):
        flow = self.flow()
        self.client.login.side_effect = [
            cloud.DjiTwoFactorRequired(553), "US_synthetic",
        ]
        await flow.async_step_account(self.input_for("account"))
        result = await flow.async_step_captcha({"captcha_code": "code"})
        self.assertEqual(result["step_id"], "twofa")
        result = await flow.async_step_twofa({"code": " 123456 "})
        self.assertEqual(result["type"], "create_entry")
        self.client.login.assert_awaited_with(
            "test@example.invalid", PASSWORD, "ticket", email_code="123456"
        )

    def test_setup_error_translations_are_synchronized(self):
        component = Path(__file__).parents[1] / "custom_components" / "dji_power_ble"
        strings = json.loads((component / "strings.json").read_text())
        english = json.loads((component / "translations/en.json").read_text())
        self.assertEqual(strings, english)
        for key in (
            "invalid_address", "email_required", "password_required", "invalid_model"
        ):
            self.assertTrue(strings["config"]["error"][key])
        self.assertTrue(strings["config"]["abort"]["setup_incomplete"])
        self.assertTrue(strings["config"]["abort"]["reconfigure_successful"])
        self.assertTrue(strings["config"]["step"]["reconfigure"]["data"]["model"])
