"""Offline tests for cloud failures exposed to the setup flow."""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock

import aiohttp

COMPONENT = Path(__file__).parents[1] / "custom_components" / "dji_power_ble"
SPEC = importlib.util.spec_from_file_location("dji_cloud_test", COMPONENT / "cloud.py")
assert SPEC and SPEC.loader
cloud = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = cloud
SPEC.loader.exec_module(cloud)


def response(payload=None):
    """An aiohttp response context without any network access."""
    result = MagicMock()
    result.__aenter__.return_value = result
    result.json = AsyncMock(return_value=payload)
    result.read = AsyncMock(return_value=b"captcha image")
    return result


def failures():
    """Failures at connection, HTTP status, and JSON decoding boundaries."""
    return (
        ("connect", aiohttp.ClientConnectionError("private connection detail")),
        ("connect", TimeoutError("private timeout detail")),
        (
            "status",
            aiohttp.ClientResponseError(None, (), status=503, message="private body"),
        ),
        ("json", json.JSONDecodeError("invalid JSON", "private response body", 0)),
    )


def failed_response(stage, error):
    result = response()
    if stage == "connect":
        result.__aenter__.side_effect = error
    elif stage == "status":
        result.raise_for_status.side_effect = error
    else:
        result.json.side_effect = error
    return result


class CloudTests(unittest.IsolatedAsyncioTestCase):
    async def test_login_failures_use_the_config_flow_error_boundary(self):
        for stage, error in failures():
            with self.subTest(stage=stage, error=type(error).__name__):
                session = Mock(post=Mock(return_value=failed_response(stage, error)))
                client = cloud.DjiCloudClient(session)

                with self.assertRaises(cloud.DjiCloudError) as caught:
                    await client.login("test@example.com", "password", "ticket")

                self.assertEqual(str(caught.exception), "DJI account request failed")
                self.assertIs(caught.exception.__cause__, error)

    async def test_image_captcha_failures_use_the_config_flow_error_boundary(self):
        for stage, error in failures()[:3]:
            with self.subTest(stage=stage, error=type(error).__name__):
                session = Mock(get=Mock(return_value=failed_response(stage, error)))

                with self.assertRaises(cloud.DjiCloudError) as caught:
                    await cloud.DjiCloudClient(session).get_image_captcha()

                self.assertEqual(
                    str(caught.exception), "DJI image captcha request failed"
                )

    async def test_device_lookup_recovers_in_the_next_region(self):
        payload = {
            "data": {
                "dy_devices": [
                    {"base_info": {"name": "Power"}, "pair_info": {"pair_key": "ab"}}
                ]
            }
        }
        for stage, error in failures():
            with self.subTest(stage=stage, error=type(error).__name__):
                session = Mock(
                    get=Mock(
                        side_effect=[failed_response(stage, error), response(payload)]
                    )
                )

                devices = await cloud.DjiCloudClient(session).list_devices("US_test")

                self.assertEqual(devices, [cloud.DjiDevice("Power", "", "", "ab")])
                self.assertEqual(session.get.call_count, 2)

    async def test_failed_device_lookup_omits_transport_details(self):
        session = Mock(
            get=Mock(side_effect=TimeoutError("private token and response details"))
        )

        with self.assertRaises(cloud.DjiCloudError) as caught:
            await cloud.DjiCloudClient(session).list_devices("US_test")

        self.assertEqual(str(caught.exception), "DJI device list request failed")
        self.assertEqual(session.get.call_count, len(cloud.HOME_API_HOSTS))

    async def test_successful_account_requests_and_empty_device_list(self):
        session = Mock(
            post=Mock(return_value=response({"code": 0, "data": {"token": "US_test"}})),
            get=Mock(return_value=response({"data": {"dy_devices": []}})),
        )
        client = cloud.DjiCloudClient(session)

        self.assertEqual(
            await client.login("test@example.com", "password", "ticket"), "US_test"
        )
        srandom, image = await client.get_image_captcha()
        self.assertTrue(srandom)
        self.assertEqual(image, b"captcha image")
        self.assertEqual(await client.list_devices("US_test"), [])


if __name__ == "__main__":
    unittest.main()
