"""DJI account cloud client — mint a member token and fetch the BLE pair_key.

This reproduces the DJI Home mobile account-center request signing (the `*-Mc`
headers, base64 HMAC-SHA1) so Home Assistant can fetch a station's local
`pair_key` from a logged-in DJI account, with no rooted phone and no BLE sniffing.

The cloud token is used ONCE at setup to read `pair_info.pair_key`, then dropped.
Daily operation is local BLE only and needs none of this.

Flow: validate_captcha(gRecaptchaResponse) -> captchaTicket -> user_login -> a
US_ member token -> home-api /users/devices/list -> pair_key.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
import uuid
from dataclasses import dataclass

import aiohttp

ACCOUNT_BASE = "https://account.dji.com/apis/apprest/v1"
# Regional home-api hosts (token is region-scoped; vg/us = global, hz = China).
HOME_API_HOSTS = (
    "https://home-api.djigate.com",
    "https://home-api-vg.djigate.com",
    "https://home-api-hz.djigate.com",
)
DEVICES_PATH = "/app/api/v1/users/devices/list"

# Google reCAPTCHA v2 site key DJI Home uses for app login. NOT domain-locked, so
# the widget can render on a Home-Assistant-served page.
RECAPTCHA_SITE_KEY = "6Ld2h4wqAAAAABva7yuX4vbMq34_Rt9_XpsiQkRV"

# Bootstrap HMAC key for Sign-Mc, recovered from the DJI Home Flutter snapshot
# (object pool pp+0x8e50). DECIDE-LATER: fine for local use; revisit before any
# public distribution (DJI may rotate it; bundling their key is a separate call).
SIGN_MC_KEY = "43421d0a-c0bf-4467-9542-3159cc6000cb"

USER_AGENT = "DJIHome/1.5.16 (Android)"

# DJI account-center error codes (from the app's error enum).
CODE_OK = 0
CODE_TICKET_EMPTY = 605
CODE_EMAIL_CODE_REQUIRED = 553
CODE_EMAIL_FREQUENCY_LIMITED = 554
CODE_TWO_STEP_REQUIRED = 556
CODE_SMS_REACHED_LIMIT = 508
CODE_IMAGE_CAPTCHA_ERROR = 523
CODE_IMAGE_CAPTCHA_REQUIRED = 524
CODE_CAPTCHA_VERIFY_ERROR = 601

# Codes that mean "we need a 2-step / email verification code from the user".
TWO_FACTOR_CODES = frozenset({CODE_EMAIL_CODE_REQUIRED, CODE_TWO_STEP_REQUIRED})
RATE_LIMIT_CODES = frozenset({CODE_EMAIL_FREQUENCY_LIMITED, CODE_SMS_REACHED_LIMIT})


class DjiCloudError(Exception):
    """Base error for the DJI cloud client."""


class DjiAuthError(DjiCloudError):
    """A login/captcha call returned a non-zero code."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"DJI cloud error {code}: {message}")
        self.code = code
        self.message = message


class DjiTwoFactorRequired(DjiCloudError):
    """user_login needs a 2-step / email verification code."""

    def __init__(self, code: int) -> None:
        super().__init__(f"two-step verification required (code {code})")
        self.code = code


class DjiRateLimited(DjiCloudError):
    """Verification code requests are rate-limited; the user must wait."""


@dataclass(slots=True)
class DjiDevice:
    """A device entry from the Home API with its local BLE pair_key."""

    name: str
    sn: str
    pair_uuid: str
    pair_key: str


def _signed_headers(client_name: str, device_id: str) -> dict[str, str]:
    """Build the DJI `*-Mc` common headers, signed with base64 HMAC-SHA1."""
    timestamp = str(int(time.time()))
    invoke_id = f"DeviceId-Mc{timestamp}{uuid.uuid4().hex[:6]}"
    material = (
        "AppId-Mc"
        "cr-app"
        "ClientName-Mc"
        + client_name
        + "DeviceId-Mc"
        + device_id
        + "InvokeId-Mc"
        + invoke_id
        + "Timestamp-Mc"
        + timestamp
    )
    sign = base64.b64encode(
        hmac.new(SIGN_MC_KEY.encode(), material.encode(), hashlib.sha1).digest()
    ).decode()
    return {
        "ClientName-Mc": client_name,
        "DeviceId-Mc": device_id,
        "AppId-Mc": "cr-app",
        "Timestamp-Mc": timestamp,
        "InvokeId-Mc": invoke_id,
        "Sign-Mc": sign,
        "X-Risk-Version": "1.0",
        "X-DJI-SDK-Version": "1.0.0",
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    }


class DjiCloudClient:
    """Minimal async DJI account/Home-API client for one-time pair_key fetch."""

    def __init__(
        self, session: aiohttp.ClientSession, *, timeout: float = 20.0
    ) -> None:
        self._session = session
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._client_name = "android-1.5.16"
        self._device_id = f"dji-home-{uuid.uuid4().hex[:16]}"

    async def _post(self, action: str, data: dict[str, str]) -> dict:
        headers = _signed_headers(self._client_name, self._device_id)
        async with self._session.post(
            f"{ACCOUNT_BASE}/{action}",
            headers=headers,
            data=data,
            timeout=self._timeout,
        ) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)

    async def get_image_captcha(self) -> tuple[str, bytes]:
        """Fetch DJI's own image captcha. Returns (srandom, png_bytes).

        The `srandom` ties the image to the later validate_captcha call. This is a
        plain image served from DJI's domain (no Google, no domain lock), so it can
        be shown natively in the Home Assistant config flow.
        """
        srandom = uuid.uuid4().hex
        headers = _signed_headers(self._client_name, self._device_id)
        headers.pop("Content-Type", None)
        async with self._session.get(
            f"{ACCOUNT_BASE}/vcode?srandom={srandom}",
            headers=headers,
            timeout=self._timeout,
        ) as resp:
            resp.raise_for_status()
            return srandom, await resp.read()

    async def exchange_image_captcha(self, srandom: str, code: str) -> str:
        """Exchange a typed image-captcha code for a DJI captchaTicket.

        Codes are case-sensitive. Raises DjiAuthError(524) on a wrong code.
        """
        body = {
            "captchaType": "imageCaptcha",
            "captchaModule": "AppLogin",
            "verificationCode": code,
            "srandom": srandom,
        }
        resp = await self._post("validate_captcha", body)
        code_ = resp.get("code")
        if code_ != CODE_OK:
            raise DjiAuthError(code_, resp.get("message", "captcha rejected"))
        ticket = (resp.get("data") or {}).get("captchaTicket")
        if not ticket:
            raise DjiAuthError(code_, "no captchaTicket in response")
        return ticket

    async def exchange_captcha(self, grecaptcha_response: str) -> str:
        """Exchange a Google reCAPTCHA token for a DJI captchaTicket (fallback)."""
        body = {
            "captchaType": "googleCaptcha",
            "captchaModule": "AppLogin",
            "gRecaptchaResponse": grecaptcha_response,
        }
        resp = await self._post("validate_captcha", body)
        code = resp.get("code")
        if code != CODE_OK:
            raise DjiAuthError(code, resp.get("message", "captcha rejected"))
        ticket = (resp.get("data") or {}).get("captchaTicket")
        if not ticket:
            raise DjiAuthError(code, "no captchaTicket in response")
        return ticket

    async def login(
        self,
        email: str,
        password: str,
        captcha_ticket: str,
        *,
        email_code: str | None = None,
    ) -> str:
        """Run user_login and return the US_ member token.

        Raises DjiTwoFactorRequired if the account needs an email/2-step code,
        DjiRateLimited if code requests are throttled, DjiAuthError otherwise.
        """
        body = {
            "userName": email,
            "password": password,
            "captchaTicket": captcha_ticket,
        }
        if email_code:
            # DEFENSIVE 2FA: the app's login body carries the verification code in
            # `emailCode`/`verificationCode`. The exact 2-step submit endpoint was
            # not confirmed from the snapshot; resubmitting user_login with the
            # code is the most likely path. Swap here if a dedicated verify
            # endpoint is later confirmed.
            body["emailCode"] = email_code
            body["verificationCode"] = email_code
        resp = await self._post("user_login", body)
        code = resp.get("code")
        if code in TWO_FACTOR_CODES:
            raise DjiTwoFactorRequired(code)
        if code in RATE_LIMIT_CODES:
            raise DjiRateLimited(resp.get("message", "rate limited"))
        if code != CODE_OK:
            raise DjiAuthError(code, resp.get("message", "login failed"))
        token = (resp.get("data") or {}).get("token")
        if not token or not token.startswith("US_"):
            raise DjiAuthError(code, "no member token in login response")
        return token

    async def list_devices(self, token: str) -> list[DjiDevice]:
        """Fetch all devices that expose a pair_key for this member token."""
        last_error: Exception | None = None
        for host in HOME_API_HOSTS:
            try:
                async with self._session.get(
                    f"{host}{DEVICES_PATH}",
                    headers={
                        "x-member-token": token,
                        "User-Agent": USER_AGENT,
                        "Accept": "application/json",
                    },
                    timeout=self._timeout,
                ) as resp:
                    resp.raise_for_status()
                    payload = await resp.json(content_type=None)
            except aiohttp.ClientError as err:  # try the next region
                last_error = err
                continue
            devices = _extract_devices(payload)
            if devices:
                return devices
        if last_error:
            raise DjiCloudError(f"home-api request failed: {last_error}")
        return []


def _extract_devices(payload: dict) -> list[DjiDevice]:
    out: list[DjiDevice] = []
    data = payload.get("data") or {}
    for key in ("dy_devices", "cr_devices"):
        for dev in data.get(key) or []:
            base = dev.get("base_info", {})
            pair = dev.get("pair_info", {})
            if pair.get("pair_key"):
                out.append(
                    DjiDevice(
                        name=base.get("name") or "DJI Power",
                        sn=base.get("sn") or "",
                        pair_uuid=pair.get("pair_uuid") or "",
                        pair_key=pair["pair_key"],
                    )
                )
    return out
