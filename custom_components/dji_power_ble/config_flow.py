"""Config flow for DJI Power local BLE.

Three ways to provide the credential:
  * manual  — type the 32-hex pair_key (and BLE address) directly.
  * token   — paste an existing DJI member token for a one-time key lookup.
  * account — sign in to the DJI account, solve its image captcha, and fetch the
              pair_key automatically. Runtime remains local BLE.
"""

from __future__ import annotations

import base64
import contextlib
import logging
import re
from copy import deepcopy
from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    OptionsFlow,
)
from homeassistant.const import CONF_ADDRESS, CONF_EMAIL, CONF_NAME, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TimeSelector,
)

from .cloud import (
    CODE_IMAGE_CAPTCHA_ERROR,
    DjiAuthError,
    DjiCloudClient,
    DjiCloudError,
    DjiDevice,
    DjiRateLimited,
    DjiTwoFactorRequired,
)
from .const import (
    CONF_CONNECTION_SOURCE,
    CONF_KEEP_CONNECTION,
    CONF_MODEL,
    CONF_PAIR_KEY,
    CONF_SERIAL_NUMBER,
    CONF_UPDATE_INTERVAL,
    CONNECTION_SOURCE_AUTOMATIC,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    MANUFACTURER_ID,
    MAX_UPDATE_INTERVAL,
    MIN_UPDATE_INTERVAL,
)
from .duml import (
    MODEL_NAMES,
    TIME_PERIOD_DAYS,
    ProtocolError,
    normalize_pair_key,
    normalize_time_periods,
    parse_manufacturer_data,
)
from .features import ModelFeature, supports_feature
from .local_ble import async_local_adapters

_LOGGER = logging.getLogger(__name__)

CONF_DEVICE = "device"
CONF_CAPTCHA = "captcha_code"
CONF_TOKEN = "member_token"


def _normalize_address(value: str | None) -> str | None:
    """Normalize a Bluetooth MAC address and reject malformed input."""
    address = format_mac(value.strip()) if value else ""
    if re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", address):
        return address
    return None


OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_UPDATE_INTERVAL): NumberSelector(
            NumberSelectorConfig(
                min=MIN_UPDATE_INTERVAL,
                max=MAX_UPDATE_INTERVAL,
                step=1,
                mode=NumberSelectorMode.BOX,
                unit_of_measurement="s",
            )
        )
    }
)


class DjiPowerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for DJI Power local BLE."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> DjiPowerOptionsFlow:
        """Create the options flow."""
        return DjiPowerOptionsFlow()

    def __init__(self) -> None:
        self._discovered_address: str | None = None
        self._discovered_name: str | None = None
        self._discovered_model: str | None = None
        # Account-flow transient state (never persisted).
        self._address: str | None = None
        self._name: str | None = None
        self._email: str | None = None
        self._password: str | None = None
        self._client: DjiCloudClient | None = None
        self._captcha_ticket: str | None = None
        self._token: str | None = None
        self._devices: list[DjiDevice] | None = None
        self._srandom: str | None = None

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> FlowResult:
        """Handle a manufacturer-matched DJI Power advertisement."""
        await self.async_set_unique_id(format_mac(discovery_info.address))
        self._abort_if_unique_id_configured()
        self._discovered_address = discovery_info.address
        self._discovered_name = discovery_info.name or "DJI Power"
        manufacturer_data = discovery_info.manufacturer_data.get(MANUFACTURER_ID)
        if manufacturer_data:
            with contextlib.suppress(ProtocolError):
                self._discovered_model = parse_manufacturer_data(
                    manufacturer_data
                ).model
        self.context["title_placeholders"] = {"name": self._discovered_name}
        return await self.async_step_user()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Let the user choose how to supply the pair key."""
        return self.async_show_menu(
            step_id="user", menu_options=["account", "token", "manual"]
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Correct the station model without changing its connection details."""
        entry = self._get_reconfigure_entry()
        models = list(MODEL_NAMES.values())
        errors: dict[str, str] = {}
        if user_input is not None:
            model = user_input.get(CONF_MODEL)
            if model not in models:
                errors[CONF_MODEL] = "invalid_model"
            else:
                has_update_listener = bool(entry.update_listeners)
                changed = self.hass.config_entries.async_update_entry(
                    entry, data={**entry.data, CONF_MODEL: model}
                )
                # A notified listener owns the reload; unchanged data notifies none.
                if not changed or not has_update_listener:
                    self.hass.config_entries.async_schedule_reload(entry.entry_id)
                return self.async_abort(reason="reconfigure_successful")

        current = entry.data.get(CONF_MODEL)
        if current not in models:
            current = self._model_for_address(entry.data[CONF_ADDRESS])
        model_field = (
            vol.Required(CONF_MODEL, default=current)
            if current in models
            else vol.Required(CONF_MODEL)
        )
        schema = vol.Schema(
            {
                model_field: SelectSelector(
                    SelectSelectorConfig(
                        options=models, mode=SelectSelectorMode.DROPDOWN
                    )
                )
            }
        )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            errors=errors,
        )

    def _discovered_stations(self) -> dict[str, str]:
        """Currently-advertising, not-yet-configured DJI Power stations."""
        configured = {
            _normalize_address(entry.data.get(CONF_ADDRESS))
            for entry in self._async_current_entries()
        }
        out: dict[str, str] = {}
        for info in async_discovered_service_info(self.hass, connectable=True):
            if _normalize_address(info.address) in configured:
                continue
            name = info.name or ""
            if MANUFACTURER_ID in (info.manufacturer_data or {}):
                out[info.address] = f"{name or 'DJI Power'} ({info.address})"
        return out

    def _model_for_address(self, address: str) -> str:
        """Use only model information belonging to the submitted station."""
        normalized = _normalize_address(address)
        if (
            self._discovered_model
            and normalized == _normalize_address(self._discovered_address)
        ):
            return self._discovered_model
        for info in async_discovered_service_info(self.hass, connectable=True):
            if _normalize_address(info.address) != normalized:
                continue
            manufacturer_data = info.manufacturer_data.get(MANUFACTURER_ID)
            if manufacturer_data:
                with contextlib.suppress(ProtocolError):
                    return parse_manufacturer_data(manufacturer_data).model
        return "DJI Power"

    def _address_schema_part(self) -> dict:
        """Address field: prefilled if discovered, a dropdown if any station is
        advertising, else free text."""
        if self._discovered_address:
            return {vol.Required(CONF_ADDRESS, default=self._discovered_address): str}
        stations = self._discovered_stations()
        if stations:
            return {
                vol.Required(CONF_ADDRESS, default=next(iter(stations))): vol.In(
                    stations
                )
            }
        return {vol.Required(CONF_ADDRESS, default=""): str}

    # ----------------------------------------------------------------- manual
    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            address = _normalize_address(user_input[CONF_ADDRESS])
            if address is None:
                errors[CONF_ADDRESS] = "invalid_address"
            try:
                normalize_pair_key(user_input[CONF_PAIR_KEY])
            except ProtocolError:
                errors["base"] = "invalid_pair_key"
            if not errors:
                await self.async_set_unique_id(
                    format_mac(address), raise_on_progress=False
                )
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=user_input.get(CONF_NAME) or self._discovered_name or address,
                    data={
                        CONF_ADDRESS: address,
                        CONF_PAIR_KEY: user_input[CONF_PAIR_KEY].strip(),
                        CONF_NAME: user_input.get(CONF_NAME)
                        or self._discovered_name
                        or "DJI Power",
                        CONF_MODEL: self._model_for_address(address),
                    },
                )

        schema = vol.Schema(
            {
                **self._address_schema_part(),
                vol.Required(CONF_PAIR_KEY): str,
                vol.Optional(
                    CONF_NAME, default=self._discovered_name or "DJI Power"
                ): str,
            }
        )
        return self.async_show_form(
            step_id="manual",
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            errors=errors,
        )

    # ------------------------------------------------------------------ token
    async def async_step_token(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Paste an existing DJI x-member-token; fetch the pair_key directly.

        For users who already have a token (e.g. from tools/mem_scrape.py). No
        login or captcha needed — the token is used once to read the pair_key.
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            self._address = _normalize_address(user_input[CONF_ADDRESS])
            self._name = user_input.get(CONF_NAME) or self._discovered_name
            token = user_input[CONF_TOKEN].strip()
            if not self._address:
                errors[CONF_ADDRESS] = "invalid_address"
            else:
                self._client = DjiCloudClient(async_get_clientsession(self.hass))
                try:
                    self._devices = await self._client.list_devices(token)
                except DjiCloudError as err:
                    _LOGGER.warning("DJI device list failed: %s", err)
                    errors["base"] = "cannot_connect"
                else:
                    if not self._devices:
                        errors["base"] = "invalid_token"
                    else:
                        return await self.async_step_finish()

        schema = vol.Schema(
            {
                **self._address_schema_part(),
                vol.Required(CONF_TOKEN): str,
                vol.Optional(
                    CONF_NAME, default=self._discovered_name or "DJI Power"
                ): str,
            }
        )
        return self.async_show_form(
            step_id="token",
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            errors=errors,
        )

    # ---------------------------------------------------------------- account
    async def async_step_account(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Collect the BLE address and DJI account credentials."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._address = _normalize_address(user_input[CONF_ADDRESS])
            self._email = user_input[CONF_EMAIL].strip()
            self._password = user_input[CONF_PASSWORD]
            self._name = user_input.get(CONF_NAME) or self._discovered_name
            if not self._address:
                errors[CONF_ADDRESS] = "invalid_address"
            if not self._email:
                errors[CONF_EMAIL] = "email_required"
            if not self._password.strip():
                errors[CONF_PASSWORD] = "password_required"
            if not errors:
                return await self.async_step_captcha()

        schema = vol.Schema(
            {
                **self._address_schema_part(),
                vol.Required(CONF_EMAIL): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Optional(
                    CONF_NAME, default=self._discovered_name or "DJI Power"
                ): str,
            }
        )
        return self.async_show_form(
            step_id="account",
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            errors=errors,
        )

    async def async_step_captcha(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show DJI's image captcha inline; the user types the characters.

        DJI serves a plain image captcha (no Google, no domain lock), so it can be
        rendered directly in the config flow. A wrong code just reloads a new image.
        """
        if (
            _normalize_address(self._address) is None
            or not self._email
            or not self._password
            or not self._password.strip()
            or (
                user_input is not None
                and (not self._srandom or self._client is None)
            )
        ):
            return self.async_abort(reason="setup_incomplete")
        errors: dict[str, str] = {}
        if self._client is None:
            self._client = DjiCloudClient(async_get_clientsession(self.hass))

        if user_input is not None:
            try:
                self._captcha_ticket = await self._client.exchange_image_captcha(
                    self._srandom, user_input[CONF_CAPTCHA].strip()
                )
                self._token = await self._client.login(
                    self._email, self._password, self._captcha_ticket
                )
            except DjiTwoFactorRequired:
                return await self.async_step_twofa()
            except DjiRateLimited:
                errors["base"] = "rate_limited"
            except DjiAuthError as err:
                if err.code == CODE_IMAGE_CAPTCHA_ERROR:
                    errors["base"] = "invalid_code"
                else:
                    _LOGGER.warning("DJI login failed: %s", err)
                    errors["base"] = "login_failed"
            except DjiCloudError as err:
                _LOGGER.warning("DJI login failed: %s", err)
                errors["base"] = "login_failed"
            if not errors:
                return await self.async_step_finish()

        # (Re)load a fresh image captcha for the form.
        try:
            self._srandom, png = await self._client.get_image_captcha()
        except DjiCloudError as err:
            _LOGGER.warning("DJI image captcha fetch failed: %s", err)
            return self.async_abort(reason="cannot_connect")
        data_uri = "data:image/png;base64," + base64.b64encode(png).decode()

        return self.async_show_form(
            step_id="captcha",
            data_schema=vol.Schema({vol.Required(CONF_CAPTCHA): str}),
            errors=errors,
            description_placeholders={"image": data_uri},
        )

    async def async_step_twofa(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Ask for the email/2-step verification code and resubmit login."""
        if (
            self._client is None
            or not self._email
            or not self._password
            or not self._password.strip()
            or not self._captcha_ticket
            or _normalize_address(self._address) is None
        ):
            return self.async_abort(reason="setup_incomplete")
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                self._token = await self._client.login(
                    self._email,
                    self._password,
                    self._captcha_ticket or "",
                    email_code=user_input["code"].strip(),
                )
            except DjiTwoFactorRequired:
                errors["base"] = "invalid_code"
            except DjiRateLimited:
                errors["base"] = "rate_limited"
            except (DjiAuthError, DjiCloudError) as err:
                _LOGGER.warning("DJI 2FA login failed: %s", err)
                errors["base"] = "login_failed"
            if not errors:
                return await self.async_step_finish()

        return self.async_show_form(
            step_id="twofa",
            data_schema=vol.Schema({vol.Required("code"): str}),
            errors=errors,
        )

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Fetch devices with the token and create the entry."""
        if _normalize_address(self._address) is None:
            return self.async_abort(reason="setup_incomplete")
        if self._devices is None:
            if self._client is None or not self._token:
                return self.async_abort(reason="setup_incomplete")
            try:
                self._devices = await self._client.list_devices(self._token)
            except DjiCloudError as err:
                _LOGGER.warning("DJI device list failed: %s", err)
                return self.async_abort(reason="cannot_connect")
            finally:
                # Token did its one job; drop it.
                self._token = None
        if not self._devices:
            return self.async_abort(reason="no_devices")

        if len(self._devices) == 1:
            return await self._create_from_device(self._devices[0])

        if user_input is not None and CONF_DEVICE in user_input:
            chosen = next(
                (d for d in self._devices if d.sn == user_input[CONF_DEVICE]), None
            )
            if chosen is not None:
                return await self._create_from_device(chosen)

        options = {
            d.sn: f"{d.name} ({d.sn})" if d.sn else d.name for d in self._devices
        }
        return self.async_show_form(
            step_id="finish",
            data_schema=vol.Schema({vol.Required(CONF_DEVICE): vol.In(options)}),
        )

    async def _create_from_device(self, device: DjiDevice) -> FlowResult:
        if _normalize_address(self._address) is None:
            return self.async_abort(reason="setup_incomplete")
        await self.async_set_unique_id(
            format_mac(self._address), raise_on_progress=False
        )
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=self._name or device.name or self._address,
            data={
                CONF_ADDRESS: self._address,
                CONF_PAIR_KEY: device.pair_key,
                CONF_NAME: self._name or device.name or "DJI Power",
                CONF_MODEL: self._model_for_address(self._address),
                CONF_SERIAL_NUMBER: device.sn,
            },
        )


class DjiPowerOptionsFlow(OptionsFlow):
    """Configure runtime behavior for a DJI Power station."""

    def __init__(self) -> None:
        self._periods: list[dict[str, object]] | None = None
        self._original_periods: list[dict[str, object]] | None = None
        self._edit_index: int | None = None

    def _schedule_coordinator(self):
        return self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)

    def _supports_schedule(self) -> bool:
        coordinator = self._schedule_coordinator()
        model = (
            coordinator.device.model
            if coordinator is not None
            else self.config_entry.data.get(CONF_MODEL)
        )
        return supports_feature(model, ModelFeature.TARIFF_SCHEDULE)

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage integration options."""
        if user_input is None and self._supports_schedule():
            return self.async_show_menu(
                step_id="init", menu_options=["connection", "time_periods"]
            )
        return await self._async_connection_options(user_input, step_id="init")

    async def async_step_connection(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Change connection and update options."""
        return await self._async_connection_options(user_input, step_id="connection")

    async def _async_connection_options(
        self, user_input: dict[str, Any] | None, *, step_id: str
    ) -> FlowResult:
        errors: dict[str, str] = {}
        current = {
            CONF_UPDATE_INTERVAL: self.config_entry.options.get(
                CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL
            ),
            CONF_CONNECTION_SOURCE: self.config_entry.options.get(
                CONF_CONNECTION_SOURCE, CONNECTION_SOURCE_AUTOMATIC
            ),
            CONF_KEEP_CONNECTION: self.config_entry.options.get(
                CONF_KEEP_CONNECTION, False
            ),
        }
        sources = {CONNECTION_SOURCE_AUTOMATIC: "Automatic (local or proxy)"}
        try:
            sources.update(await async_local_adapters())
        except Exception:
            _LOGGER.warning("Could not enumerate local Bluetooth adapters")
            errors["base"] = "adapters_unavailable"
        selected = current[CONF_CONNECTION_SOURCE]
        if selected not in sources:
            sources[selected] = f"{selected} (unavailable)"

        schema = OPTIONS_SCHEMA.extend(
            {
                vol.Required(CONF_CONNECTION_SOURCE): vol.In(sources),
                vol.Required(CONF_KEEP_CONNECTION): bool,
            }
        )
        if user_input is not None:
            try:
                validated = schema(user_input)
                interval = validated[CONF_UPDATE_INTERVAL]
                if not interval.is_integer():
                    raise vol.Invalid(
                        "Expected whole seconds", path=[CONF_UPDATE_INTERVAL]
                    )
                validated[CONF_UPDATE_INTERVAL] = int(interval)
            except vol.Invalid as err:
                field = err.path[0] if err.path else "base"
                if field == CONF_UPDATE_INTERVAL:
                    errors[field] = "invalid_update_interval"
                elif field == CONF_CONNECTION_SOURCE:
                    errors[field] = "invalid_connection_source"
                elif field == CONF_KEEP_CONNECTION:
                    errors[field] = "invalid_keep_connection"
                else:
                    errors["base"] = "invalid_connection_options"
            else:
                if validated[CONF_CONNECTION_SOURCE] == CONNECTION_SOURCE_AUTOMATIC:
                    validated[CONF_KEEP_CONNECTION] = False
                return self.async_create_entry(
                    data={**self.config_entry.options, **validated}
                )
            current.update(
                {key: value for key, value in user_input.items() if key in current}
            )

        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(schema, current),
            errors=errors,
        )

    async def _async_load_periods(self) -> FlowResult | None:
        """Guard every schedule step and load an independent draft once."""
        if not self._supports_schedule():
            return self.async_abort(reason="schedule_not_supported")
        coordinator = self._schedule_coordinator()
        if coordinator is None:
            return self.async_abort(reason="station_unavailable")
        if self._periods is None:
            try:
                periods = await coordinator.async_get_time_periods()
                self._periods = normalize_time_periods(periods)
            except (HomeAssistantError, ProtocolError) as err:
                return self.async_show_form(
                    step_id="time_periods",
                    data_schema=vol.Schema({}),
                    errors={"base": "schedule_read_failed"},
                    description_placeholders={"periods": "", "reason": str(err)},
                )
            self._original_periods = deepcopy(self._periods)
        return None

    @staticmethod
    def _period_label(period: dict[str, object]) -> str:
        kind = "Peak" if period["type"] == "peak" else "Off-peak"
        days = period["days"]
        recurrence = (
            "Every day"
            if days == list(TIME_PERIOD_DAYS)
            else ", ".join(day.title() for day in days)
        )
        overnight = " (+1 day)" if period["end"] < period["start"] else ""
        return (
            f"{kind}: {recurrence}, {period['start']}–{period['end']}{overnight}"
        )

    def _periods_summary(self) -> str:
        return "\n".join(
            f"- {self._period_label(period)}" for period in self._periods or []
        ) or "No periods."

    async def async_step_time_periods(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show the current draft and available editing actions."""
        if result := await self._async_load_periods():
            return result
        actions = ["add_period"]
        if self._periods:
            actions.extend(["edit_period", "delete_period"])
        actions.extend(["save_periods", "discard_periods"])
        return self.async_show_menu(
            step_id="time_periods",
            menu_options=actions,
            description_placeholders={
                "periods": self._periods_summary(),
                "reason": "",
            },
        )

    async def async_step_add_period(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Append a period to the draft."""
        return await self._async_period_form(user_input, step_id="add_period")

    async def async_step_change_period(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Edit the selected draft period."""
        if self._edit_index is None:
            return await self.async_step_edit_period()
        return await self._async_period_form(user_input, step_id="change_period")

    async def _async_period_form(
        self, user_input: dict[str, Any] | None, *, step_id: str
    ) -> FlowResult:
        if result := await self._async_load_periods():
            return result
        if self._periods is None:
            return self.async_abort(reason="station_unavailable")
        schema = vol.Schema(
            {
                vol.Required("type"): SelectSelector(
                    SelectSelectorConfig(
                        options=["peak", "off_peak"], translation_key="period_type"
                    )
                ),
                vol.Required("days"): SelectSelector(
                    SelectSelectorConfig(
                        options=list(TIME_PERIOD_DAYS),
                        multiple=True,
                        translation_key="weekdays",
                    )
                ),
                vol.Required("start"): TimeSelector(),
                vol.Required("end"): TimeSelector(),
            }
        )
        current = (
            deepcopy(self._periods[self._edit_index])
            if step_id == "change_period"
            else {"type": "off_peak", "days": list(TIME_PERIOD_DAYS)}
        )
        errors = {}
        reason = ""
        if user_input is not None:
            try:
                period = schema(user_input)
                for field in ("start", "end"):
                    time = period[field]
                    if len(time) == 8 and time.endswith(":00"):
                        period[field] = time[:5]
                candidate = deepcopy(self._periods)
                if step_id == "change_period":
                    candidate[self._edit_index] = period
                else:
                    candidate.append(period)
                self._periods = normalize_time_periods(candidate)
            except (vol.Invalid, ProtocolError) as err:
                errors["base"] = "invalid_period"
                reason = str(err)
                current.update(user_input)
            else:
                self._edit_index = None
                return await self.async_step_time_periods()
        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(schema, current),
            errors=errors,
            description_placeholders={"reason": reason},
        )

    async def async_step_edit_period(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Select the period to edit."""
        return await self._async_select_period(user_input, step_id="edit_period")

    async def async_step_delete_period(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Remove a selected period from the draft."""
        return await self._async_select_period(user_input, step_id="delete_period")

    async def _async_select_period(
        self, user_input: dict[str, Any] | None, *, step_id: str
    ) -> FlowResult:
        if result := await self._async_load_periods():
            return result
        if not self._periods:
            return await self.async_step_time_periods()
        choices = {
            str(index): self._period_label(period)
            for index, period in enumerate(self._periods)
        }
        schema = vol.Schema({vol.Required("period"): vol.In(choices)})
        errors = {}
        if user_input is not None:
            try:
                index = int(schema(user_input)["period"])
            except vol.Invalid:
                errors["period"] = "invalid_period_selection"
            else:
                if step_id == "delete_period":
                    self._periods.pop(index)
                    self._edit_index = None
                    return await self.async_step_time_periods()
                self._edit_index = index
                return await self.async_step_change_period()
        return self.async_show_form(
            step_id=step_id, data_schema=schema, errors=errors
        )

    async def async_step_save_periods(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Review and save through the confirmed BLE write path."""
        if result := await self._async_load_periods():
            return result
        reason = ""
        if user_input is not None:
            coordinator = self._schedule_coordinator()
            try:
                await coordinator.async_set_time_periods(
                    deepcopy(self._periods),
                    expected_periods=deepcopy(self._original_periods),
                )
            except HomeAssistantError as err:
                if err.translation_key == "schedule_changed":
                    return await self.async_step_schedule_changed()
                reason = str(err)
            else:
                # This is a device write, not an options change or entry reload.
                return self.async_abort(reason="schedule_saved")
        return self.async_show_menu(
            step_id="save_periods",
            menu_options=["apply_periods", "time_periods"],
            description_placeholders={
                "periods": self._periods_summary(),
                "reason": reason,
            },
        )

    async def async_step_apply_periods(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Apply the draft after choosing Save schedule on the review screen."""
        return await self.async_step_save_periods({})

    async def async_step_schedule_changed(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Reload a conflicting schedule only after explicit confirmation."""
        if result := await self._async_load_periods():
            return result
        if user_input is not None:
            self._periods = self._original_periods = None
            self._edit_index = None
            return await self.async_step_time_periods()
        return self.async_show_form(
            step_id="schedule_changed",
            data_schema=vol.Schema({}),
            description_placeholders={"periods": self._periods_summary()},
        )

    async def async_step_discard_periods(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Close the editor without saving the draft."""
        return self.async_abort(reason="schedule_cancelled")
