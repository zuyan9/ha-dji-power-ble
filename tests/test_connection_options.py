"""Offline checks for adapter selection and connection retention options."""

from __future__ import annotations

import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

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

    def async_create_entry(self, **kwargs):
        return {"type": "create_entry", **kwargs}

    def async_abort(self, **kwargs):
        return {"type": "abort", **kwargs}


def _module(name: str, **attributes) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    return module


def _load_flow() -> types.ModuleType:
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
        f"{PACKAGE}.duml": _module(
            f"{PACKAGE}.duml",
            ProtocolError=Exception,
            normalize_pair_key=lambda value: value,
            parse_manufacturer_data=lambda value: value,
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
        "homeassistant.helpers": _module("homeassistant.helpers"),
        "homeassistant.helpers.aiohttp_client": _module(
            "homeassistant.helpers.aiohttp_client",
            async_get_clientsession=lambda hass: None,
        ),
        "homeassistant.helpers.device_registry": _module(
            "homeassistant.helpers.device_registry", format_mac=str.upper
        ),
    }
    spec = importlib.util.spec_from_file_location(
        f"{PACKAGE}.config_flow", COMPONENT / "config_flow.py"
    )
    assert spec and spec.loader
    flow = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
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

    async def test_existing_entry_defaults_to_automatic_and_retention_disabled(self):
        result = await self.flow.async_step_init()

        self.assertEqual(result["step_id"], "init")
        self.assertEqual(
            self.flow.suggested,
            {"update_interval": 5, "connection_source": "automatic"},
        )
        result = await self.flow.async_step_init(
            {"update_interval": 5, "connection_source": "automatic"}
        )
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(
            result["data"],
            {
                "update_interval": 5,
                "connection_source": "automatic",
                "keep_connection": False,
            },
        )

    async def test_local_adapter_offers_explicit_retention_choice(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                result = await self.flow.async_step_init(
                    {"update_interval": "7", "connection_source": ADAPTER}
                )
                self.assertEqual(result["step_id"], "retention")
                self.assertFalse(self.flow.suggested["keep_connection"])

                result = await self.flow.async_step_retention(
                    {"keep_connection": enabled}
                )

                self.assertEqual(result["type"], "create_entry")
                self.assertEqual(result["data"]["connection_source"], ADAPTER)
                self.assertEqual(result["data"]["update_interval"], 7)
                self.assertIs(result["data"]["keep_connection"], enabled)

    async def test_unknown_model_can_enable_retention_before_station_is_loaded(self):
        self.flow.config_entry.data = {}

        result = await self.flow.async_step_init(
            {"update_interval": 5, "connection_source": ADAPTER}
        )

        self.assertEqual(result["step_id"], "retention")
        result = await self.flow.async_step_retention({"keep_connection": True})
        self.assertTrue(result["data"]["keep_connection"])

    async def test_all_models_can_enable_retention_on_selected_local_adapter(self):
        for model in (
            "DJI Power 1000", "DJI Power 2000", "DJI Power 1000 V2",
            "DJI Power 1000 Mini", "DJI Power",
        ):
            with self.subTest(model=model):
                self.flow.config_entry.data["model"] = model
                result = await self.flow.async_step_init(
                    {"update_interval": 5, "connection_source": ADAPTER}
                )

                self.assertEqual(result["step_id"], "retention")
                result = await self.flow.async_step_retention(
                    {"keep_connection": True}
                )
                self.assertEqual(result["type"], "create_entry")
                self.assertEqual(result["data"]["connection_source"], ADAPTER)
                self.assertTrue(result["data"]["keep_connection"])

    async def test_switch_to_automatic_clears_retention(self):
        self.flow.config_entry.options = {
            "connection_source": ADAPTER,
            "keep_connection": True,
        }

        result = await self.flow.async_step_init(
            {"update_interval": 5, "connection_source": "automatic"}
        )

        self.assertFalse(result["data"]["keep_connection"])
        result = await self.flow.async_step_retention({"keep_connection": True})
        self.assertEqual(result["reason"], "retention_unavailable")

    async def test_selected_missing_adapter_is_retained_without_fallback(self):
        self.flow.config_entry.options = {
            "connection_source": MISSING_ADAPTER,
            "keep_connection": True,
        }
        result = await self.flow.async_step_init()
        self.assertEqual(self.flow.suggested["connection_source"], MISSING_ADAPTER)
        validated = result["data_schema"](
            {"update_interval": 5, "connection_source": MISSING_ADAPTER}
        )
        self.assertEqual(validated["connection_source"], MISSING_ADAPTER)

        result = await self.flow.async_step_init(validated)
        self.assertEqual(result["step_id"], "retention")
        self.assertTrue(self.flow.suggested["keep_connection"])
        result = await self.flow.async_step_retention({"keep_connection": True})
        self.assertEqual(result["data"]["connection_source"], MISSING_ADAPTER)

    async def test_adapter_enumeration_error_keeps_current_selection(self):
        self.adapters.side_effect = RuntimeError("D-Bus unavailable")
        self.flow.config_entry.options["connection_source"] = MISSING_ADAPTER

        result = await self.flow.async_step_init()

        self.assertEqual(result["errors"], {"base": "adapters_unavailable"})
        self.assertEqual(self.flow.suggested["connection_source"], MISSING_ADAPTER)
        result = await self.flow.async_step_init(
            {"update_interval": 5, "connection_source": "automatic"}
        )
        self.assertEqual(result["type"], "create_entry")

    async def test_proxy_only_installation_can_save_automatic(self):
        self.adapters.return_value = {}

        result = await self.flow.async_step_init(
            {"update_interval": 5, "connection_source": "automatic"}
        )

        self.assertEqual(result["type"], "create_entry")
        self.assertFalse(result["data"]["keep_connection"])

    async def test_unknown_or_proxy_source_rejected(self):
        for source in (MISSING_ADAPTER, "esphome-proxy", "hci0", None):
            with self.subTest(source=source):
                result = await self.flow.async_step_init(
                    {"update_interval": 5, "connection_source": source}
                )
                self.assertEqual(result["type"], "form")
                self.assertEqual(
                    result["errors"]["connection_source"],
                    "invalid_connection_source",
                )

    async def test_interval_range_validation_is_preserved(self):
        for interval in (0, 61, "not a number"):
            with self.subTest(interval=interval):
                result = await self.flow.async_step_init(
                    {"update_interval": interval, "connection_source": "automatic"}
                )
                self.assertEqual(result["type"], "form")
                self.assertEqual(
                    result["errors"]["update_interval"], "invalid_update_interval"
                )
        for interval in (1, 60):
            with self.subTest(interval=interval):
                result = await self.flow.async_step_init(
                    {"update_interval": interval, "connection_source": "automatic"}
                )
                self.assertEqual(result["type"], "create_entry")

    async def test_crafted_retention_on_init_is_rejected(self):
        result = await self.flow.async_step_init(
            {
                "update_interval": 5,
                "connection_source": "automatic",
                "keep_connection": True,
            }
        )

        self.assertEqual(result["type"], "form")
        self.assertEqual(result["errors"]["base"], "invalid_connection_options")

    async def test_retention_step_requires_source_selection(self):
        result = await self.flow.async_step_retention({"keep_connection": True})

        self.assertEqual(result["type"], "abort")
        self.assertEqual(result["reason"], "retention_unavailable")

    async def test_retention_rejects_non_boolean_input(self):
        await self.flow.async_step_init(
            {"update_interval": 5, "connection_source": ADAPTER}
        )
        for value in ("true", 1, None):
            with self.subTest(value=value):
                result = await self.flow.async_step_retention(
                    {"keep_connection": value}
                )
                self.assertEqual(result["type"], "form")
                self.assertEqual(
                    result["errors"]["keep_connection"], "invalid_keep_connection"
                )

    def test_english_translations_match_strings(self):
        self.assertEqual(
            json.loads((COMPONENT / "strings.json").read_text()),
            json.loads((COMPONENT / "translations" / "en.json").read_text()),
        )
