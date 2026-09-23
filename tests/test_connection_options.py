"""Offline checks for adapter selection and connection retention options."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import types
import unittest
from enum import StrEnum
from pathlib import Path
from unittest.mock import AsyncMock, patch

import voluptuous as vol

COMPONENT = Path(__file__).parents[1] / "custom_components" / "dji_power_ble"
PACKAGE = "_dji_power_connection_options_tests"
ADAPTER = "AA:BB:CC:DD:EE:01"
MISSING_ADAPTER = "AA:BB:CC:DD:EE:02"


class _ConfigFlow:
    def __init_subclass__(cls, **kwargs):
        pass


class _OptionsFlow:
    def add_suggested_values_to_schema(self, schema, suggested):
        self.suggested = suggested
        return schema

    def async_show_form(self, **kwargs):
        return {"type": "form", **kwargs}

    def async_show_menu(self, **kwargs):
        return {"type": "menu", **kwargs}

    def async_create_entry(self, **kwargs):
        return {"type": "create_entry", **kwargs}

    def async_abort(self, **kwargs):
        return {"type": "abort", **kwargs}


class _NumberSelectorMode(StrEnum):
    BOX = "box"


class _NumberSelector:
    """Model HA's numeric coercion and bounds without importing HA itself."""

    def __init__(self, config):
        self.config = config

    def __call__(self, value):
        return vol.All(
            vol.Coerce(float),
            vol.Range(min=self.config["min"], max=self.config["max"]),
        )(value)


class _SelectSelectorMode(StrEnum):
    DROPDOWN = "dropdown"
    LIST = "list"


class _SelectSelector:
    """Validate native selection choices without importing Home Assistant."""

    def __init__(self, config):
        self.config = config

    def __call__(self, value):
        choices = [
            option["value"] if isinstance(option, dict) else option
            for option in self.config["options"]
        ]
        if self.config.get("multiple"):
            return vol.All(list, [vol.In(choices)])(value)
        return vol.In(choices)(value)


class _TimeSelector:
    """Accept minute/second clock strings as the HA time selector does."""

    def __init__(self, config=None):
        self.config = config or {}

    def __call__(self, value):
        if not isinstance(value, str) or not re.fullmatch(
            r"(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?", value
        ):
            raise vol.Invalid("Invalid time")
        return value


class HomeAssistantError(Exception):
    """Preserve HA's translated error details used by the options flow."""

    def __init__(self, message=None, **kwargs):
        super().__init__(message)
        self.translation_key = kwargs.get("translation_key")
        self.translation_domain = kwargs.get("translation_domain")
        self.translation_placeholders = kwargs.get("translation_placeholders")


def _module(name: str, **attributes) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    return module


def _format_mac(value: str) -> str:
    """Match HA's format-only helper, including its lack of validation."""
    candidate = value
    if len(candidate) == 17 and candidate.count(":") == 5:
        return candidate.lower()
    if len(candidate) == 17 and candidate.count("-") == 5:
        candidate = candidate.replace("-", "")
    elif len(candidate) == 14 and candidate.count(".") == 2:
        candidate = candidate.replace(".", "")
    if len(candidate) == 12:
        return ":".join(candidate.lower()[index:index + 2] for index in range(0, 12, 2))
    return value


def _load_flow() -> types.ModuleType:
    selector = _module(
        "homeassistant.helpers.selector",
        NumberSelector=_NumberSelector,
        NumberSelectorConfig=dict,
        NumberSelectorMode=_NumberSelectorMode,
        SelectSelector=_SelectSelector,
        SelectSelectorConfig=dict,
        SelectSelectorMode=_SelectSelectorMode,
        TimeSelector=_TimeSelector,
        TimeSelectorConfig=dict,
    )
    modules = {
        PACKAGE: _module(PACKAGE, __path__=[str(COMPONENT)]),
        f"{PACKAGE}.cloud": _module(
            f"{PACKAGE}.cloud",
            CODE_IMAGE_CAPTCHA_ERROR=1,
            DjiAuthError=Exception,
            DjiCloudClient=object,
            DjiCloudError=Exception,
            DjiDevice=object,
            DjiRateLimited=Exception,
            DjiTwoFactorRequired=Exception,
        ),
        f"{PACKAGE}.local_ble": _module(
            f"{PACKAGE}.local_ble", async_local_adapters=AsyncMock()
        ),
        "homeassistant": _module("homeassistant"),
        "homeassistant.components": _module("homeassistant.components"),
        "homeassistant.components.bluetooth": _module(
            "homeassistant.components.bluetooth",
            BluetoothServiceInfoBleak=object,
            async_discovered_service_info=lambda *args, **kwargs: [],
        ),
        "homeassistant.config_entries": _module(
            "homeassistant.config_entries",
            ConfigEntry=object,
            ConfigFlow=_ConfigFlow,
            OptionsFlow=_OptionsFlow,
        ),
        "homeassistant.const": _module(
            "homeassistant.const",
            CONF_ADDRESS="address",
            CONF_EMAIL="email",
            CONF_NAME="name",
            CONF_PASSWORD="password",
        ),
        "homeassistant.core": _module(
            "homeassistant.core", callback=lambda method: method
        ),
        "homeassistant.data_entry_flow": _module(
            "homeassistant.data_entry_flow", FlowResult=dict
        ),
        "homeassistant.exceptions": _module(
            "homeassistant.exceptions", HomeAssistantError=HomeAssistantError
        ),
        "homeassistant.helpers": _module("homeassistant.helpers", selector=selector),
        "homeassistant.helpers.selector": selector,
        "homeassistant.helpers.aiohttp_client": _module(
            "homeassistant.helpers.aiohttp_client",
            async_get_clientsession=lambda hass: None,
        ),
        "homeassistant.helpers.device_registry": _module(
            "homeassistant.helpers.device_registry", format_mac=_format_mac
        ),
    }
    spec = importlib.util.spec_from_file_location(
        f"{PACKAGE}.config_flow", COMPONENT / "config_flow.py"
    )
    assert spec and spec.loader
    flow = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        duml_spec = importlib.util.spec_from_file_location(
            f"{PACKAGE}.duml", COMPONENT / "duml.py"
        )
        assert duml_spec and duml_spec.loader
        duml = importlib.util.module_from_spec(duml_spec)
        sys.modules[duml_spec.name] = duml
        duml_spec.loader.exec_module(duml)
        spec.loader.exec_module(flow)
    return flow


flow_module = _load_flow()


class ConnectionOptionsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.flow = flow_module.DjiPowerOptionsFlow()
        self.flow.hass = types.SimpleNamespace(data={})
        self.flow.config_entry = types.SimpleNamespace(
            entry_id="station",
            data={"model": "DJI Power 1000"},
            options={},
        )
        self.adapters = self.enterContext(
            patch.object(
                flow_module,
                "async_local_adapters",
                AsyncMock(return_value={ADAPTER: f"Local adapter ({ADAPTER})"}),
            )
        )

    @staticmethod
    def options(interval=5, source="automatic", keep=False):
        return {
            "update_interval": interval,
            "connection_source": source,
            "keep_connection": keep,
        }

    async def test_existing_entry_defaults_to_one_page_with_retention_disabled(self):
        result = await self.flow.async_step_init()

        self.assertEqual(result["type"], "form")
        self.assertEqual(result["step_id"], "init")
        self.assertEqual(self.flow.suggested, self.options())
        self.assertEqual(
            {marker.schema for marker in result["data_schema"].schema},
            {"update_interval", "connection_source", "keep_connection"},
        )

        result = await self.flow.async_step_init(self.options())

        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["data"], self.options())

    async def test_interval_selector_is_numeric_box_with_seconds_and_zero_minimum(self):
        result = await self.flow.async_step_init()
        selector = next(
            validator
            for marker, validator in result["data_schema"].schema.items()
            if marker.schema == "update_interval"
        )

        self.assertIsInstance(selector, _NumberSelector)
        self.assertEqual(selector.config["min"], 0)
        self.assertEqual(selector.config["max"], 60)
        self.assertEqual(selector.config["step"], 1)
        self.assertEqual(selector.config["mode"], _NumberSelectorMode.BOX)
        self.assertEqual(selector.config["unit_of_measurement"], "s")

    async def test_local_adapter_and_retention_save_together_on_one_page(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                result = await self.flow.async_step_init(
                    self.options("7", ADAPTER, enabled)
                )

                self.assertEqual(result["type"], "create_entry")
                self.assertEqual(result["data"], self.options(7, ADAPTER, enabled))
                self.assertIs(type(result["data"]["update_interval"]), int)
                self.assertIs(result["data"]["keep_connection"], enabled)

    async def test_unknown_model_can_enable_retention_before_station_is_loaded(self):
        self.flow.config_entry.data = {}

        result = await self.flow.async_step_init(self.options(5, ADAPTER, True))

        self.assertEqual(result["type"], "create_entry")
        self.assertTrue(result["data"]["keep_connection"])

    async def test_all_models_can_enable_retention_on_selected_local_adapter(self):
        for model in (
            "DJI Power 1000", "DJI Power 2000", "DJI Power 1000 V2",
            "DJI Power 1000 Mini", "DJI Power",
        ):
            with self.subTest(model=model):
                self.flow.config_entry.data["model"] = model

                result = await self.flow.async_step_init(
                    self.options(5, ADAPTER, True)
                )

                self.assertEqual(result["type"], "create_entry")
                self.assertEqual(result["data"]["connection_source"], ADAPTER)
                self.assertTrue(result["data"]["keep_connection"])

    async def test_automatic_clears_retention_even_when_submitted_enabled(self):
        self.flow.config_entry.options = self.options(5, ADAPTER, True)
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                result = await self.flow.async_step_init(
                    self.options(5, "automatic", enabled)
                )

                self.assertEqual(result["type"], "create_entry")
                self.assertEqual(result["data"], self.options())

    async def test_selected_missing_adapter_is_retained_without_fallback(self):
        current = self.options(7, MISSING_ADAPTER, True)
        self.flow.config_entry.options = current

        result = await self.flow.async_step_init()

        self.assertEqual(self.flow.suggested, current)
        validated = result["data_schema"](current)
        self.assertEqual(validated["connection_source"], MISSING_ADAPTER)

        result = await self.flow.async_step_init(validated)

        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["data"], current)

    async def test_adapter_enumeration_error_keeps_all_current_values(self):
        self.adapters.side_effect = RuntimeError("D-Bus unavailable")
        current = self.options(7, MISSING_ADAPTER, True)
        self.flow.config_entry.options = current

        result = await self.flow.async_step_init()

        self.assertEqual(result["errors"], {"base": "adapters_unavailable"})
        self.assertEqual(self.flow.suggested, current)

        result = await self.flow.async_step_init(self.options())

        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["data"], self.options())

    async def test_proxy_only_installation_can_save_automatic(self):
        self.adapters.return_value = {}

        result = await self.flow.async_step_init(self.options())

        self.assertEqual(result["type"], "create_entry")
        self.assertFalse(result["data"]["keep_connection"])

    async def test_unknown_or_proxy_source_rejected(self):
        for source in (MISSING_ADAPTER, "esphome-proxy", "hci0", None):
            with self.subTest(source=source):
                result = await self.flow.async_step_init(self.options(source=source))

                self.assertEqual(result["type"], "form")
                self.assertEqual(result["step_id"], "init")
                self.assertEqual(
                    result["errors"]["connection_source"],
                    "invalid_connection_source",
                )

    async def test_intervals_outside_bounds_or_fractional_are_rejected(self):
        for interval in (-1, 61, "not a number", None, 0.5, 7.5, "7.5"):
            with self.subTest(interval=interval):
                result = await self.flow.async_step_init(self.options(interval))

                self.assertEqual(result["type"], "form")
                self.assertEqual(result["step_id"], "init")
                self.assertEqual(
                    result["errors"]["update_interval"], "invalid_update_interval"
                )

    async def test_zero_boundaries_and_numeric_input_store_integer_seconds(self):
        for interval, expected in ((0, 0), (1, 1), (60, 60), ("7", 7), (7.0, 7)):
            with self.subTest(interval=interval):
                result = await self.flow.async_step_init(self.options(interval))

                self.assertEqual(result["type"], "create_entry")
                self.assertEqual(result["data"]["update_interval"], expected)
                self.assertIs(type(result["data"]["update_interval"]), int)

    async def test_retention_rejects_non_boolean_input_on_single_page(self):
        for source in ("automatic", ADAPTER):
            for value in ("true", "false", 0, 1, None):
                with self.subTest(source=source, value=value):
                    result = await self.flow.async_step_init(
                        self.options(5, source, value)
                    )

                    self.assertEqual(result["type"], "form")
                    self.assertEqual(result["step_id"], "init")
                    self.assertEqual(
                        result["errors"]["keep_connection"],
                        "invalid_keep_connection",
                    )

    async def test_invalid_input_preserves_other_submitted_values(self):
        submitted = self.options("invalid", ADAPTER, True)

        result = await self.flow.async_step_init(submitted)

        self.assertEqual(result["type"], "form")
        self.assertEqual(self.flow.suggested, submitted)
        result = await self.flow.async_step_init(self.options(0, ADAPTER, True))
        self.assertEqual(result["data"], self.options(0, ADAPTER, True))

    async def test_all_three_fields_are_required(self):
        errors = {
            "update_interval": "invalid_update_interval",
            "connection_source": "invalid_connection_source",
            "keep_connection": "invalid_keep_connection",
        }
        for missing, expected in errors.items():
            with self.subTest(missing=missing):
                submitted = self.options()
                submitted.pop(missing)

                result = await self.flow.async_step_init(submitted)

                self.assertEqual(result["type"], "form")
                self.assertEqual(result["errors"][missing], expected)

    async def test_unknown_stored_options_survive_but_unknown_input_is_rejected(self):
        self.flow.config_entry.options = {"future_option": "preserved"}

        result = await self.flow.async_step_init(self.options(7, ADAPTER, True))

        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(
            result["data"],
            {"future_option": "preserved", **self.options(7, ADAPTER, True)},
        )
        result = await self.flow.async_step_init(
            {"unrecognized_field": True, **self.options()}
        )
        self.assertEqual(result["type"], "form")
        self.assertEqual(result["errors"]["base"], "invalid_connection_options")

    def test_english_translations_match_strings(self):
        self.assertEqual(
            json.loads((COMPONENT / "strings.json").read_text()),
            json.loads((COMPONENT / "translations" / "en.json").read_text()),
        )
