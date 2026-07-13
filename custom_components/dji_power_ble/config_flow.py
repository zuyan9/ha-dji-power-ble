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
from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigFlow
from homeassistant.const import CONF_ADDRESS, CONF_EMAIL, CONF_NAME, CONF_PASSWORD
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import format_mac

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
    CONF_MODEL,
    CONF_PAIR_KEY,
    CONF_SERIAL_NUMBER,
    DOMAIN,
    MANUFACTURER_ID,
)
from .duml import ProtocolError, normalize_pair_key, parse_manufacturer_data

_LOGGER = logging.getLogger(__name__)

CONF_DEVICE = "device"
CONF_CAPTCHA = "captcha_code"
CONF_TOKEN = "member_token"


class DjiPowerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for DJI Power local BLE."""

    VERSION = 1

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

    def _discovered_stations(self) -> dict[str, str]:
        """Currently-advertising, not-yet-configured DJI Power stations."""
        configured = {e.data.get(CONF_ADDRESS) for e in self._async_current_entries()}
        out: dict[str, str] = {}
        for info in async_discovered_service_info(self.hass, connectable=True):
            if info.address in configured:
                continue
            name = info.name or ""
            if MANUFACTURER_ID in (info.manufacturer_data or {}):
                out[info.address] = f"{name or 'DJI Power'} ({info.address})"
        return out

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
            address = format_mac(user_input[CONF_ADDRESS].strip())
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
                        CONF_MODEL: self._discovered_model or "DJI Power",
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
        return self.async_show_form(step_id="manual", data_schema=schema, errors=errors)

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
            self._address = format_mac(user_input[CONF_ADDRESS].strip())
            self._name = user_input.get(CONF_NAME) or self._discovered_name
            token = user_input[CONF_TOKEN].strip()
            if not self._address:
                errors["base"] = "address_required"
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
        return self.async_show_form(step_id="token", data_schema=schema, errors=errors)

    # ---------------------------------------------------------------- account
    async def async_step_account(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Collect the BLE address and DJI account credentials."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._address = format_mac(user_input[CONF_ADDRESS].strip())
            self._email = user_input[CONF_EMAIL].strip()
            self._password = user_input[CONF_PASSWORD]
            self._name = user_input.get(CONF_NAME) or self._discovered_name
            if not self._address:
                errors["base"] = "address_required"
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
            step_id="account", data_schema=schema, errors=errors
        )

    async def async_step_captcha(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show DJI's image captcha inline; the user types the characters.

        DJI serves a plain image captcha (no Google, no domain lock), so it can be
        rendered directly in the config flow. A wrong code just reloads a new image.
        """
        errors: dict[str, str] = {}
        if self._client is None:
            self._client = DjiCloudClient(async_get_clientsession(self.hass))

        if user_input is not None:
            assert self._email and self._password and self._srandom
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
        errors: dict[str, str] = {}
        if user_input is not None:
            assert self._client and self._email and self._password
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
        if self._devices is None:
            assert self._client and self._token
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
        assert self._address
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
                CONF_MODEL: self._discovered_model or "DJI Power",
                CONF_SERIAL_NUMBER: device.sn,
            },
        )
