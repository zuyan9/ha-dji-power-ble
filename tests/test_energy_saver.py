"""Offline Energy Saver codec, capability and device checks.

Records are synthetic app-schema values, not Power 2000 captures.
"""

from __future__ import annotations

import types
import unittest
from unittest.mock import AsyncMock, Mock, call, patch

from tests.test_accessory_entities import (
    ADDRESS,
    _Coordinator,
    _ServiceValidationError,
    number,
    select,
    switch,
)
from tests.test_capabilities import features
from tests.test_device import (
    FakeBleDevice,
    StationClient,
    device_module,
    requested_config_keys,
)
from tests.test_duml import SYNTHETIC_ECO_MODE, duml
from tests.test_number import _DjiPowerError, _HomeAssistantError, coordinator_module
from tests.test_schedule_device import PEAK_VALUE
from tests.test_sensor import sensor

BRAND = b"brand".ljust(17, b"\x00")


def eco_record(
    *,
    mode: int = 3,
    grid_mode: int = 2,
    meter: bytes = b"meter-id",
    layout: int = 103,
    phase: int = 4,
    auto_sw: int = 1,
    peak: int = 1,
    valley: int = 2,
    off_peak: tuple[int, int, int] = (2000, 200, 800),
) -> bytes:
    """Build an available record in DJI Home 1.6.0 (86) or 1.6.9 (103) layout."""
    value = bytearray(SYNTHETIC_ECO_MODE)
    value[0:4] = bytes((1, mode, peak, valley))
    value[4:16] = b"".join(watts.to_bytes(4, "little") for watts in off_peak)
    value[16] = grid_mode
    value[42:79] = meter.ljust(37, b"\x00")
    tail = phase.to_bytes(2, "little") + (250).to_bytes(4, "little") + bytes((auto_sw,))
    value[79:] = tail
    if layout == 103:
        value[79:79] = BRAND
    return bytes(value)


def decode(value: bytes) -> dict[str, object]:
    return duml.parse_telemetry(
        duml.build_keyed_set_payload([(duml.ECO_MODE_KEY, value)])
    )


def sent_record(payload: bytes) -> bytes:
    entries = duml.parse_keyed_values(payload)
    assert set(entries) == {duml.ECO_MODE_KEY, duml.RULES_KEY}
    assert entries[duml.RULES_KEY] == b"\x0c\x00" + b"1e00efffff3f"
    return entries[duml.ECO_MODE_KEY]


class EnergySaverDecodeTests(unittest.TestCase):
    def test_modes_follow_mode_then_grid_mode(self) -> None:
        for mode, grid_mode, expected in (
            (1, 0, "disabled"),
            (1, 3, "disabled"),
            (2, 2, "scheduled"),
            (3, 2, "self_consumption"),
            (3, 3, "time_of_use"),
            (0, 3, None),
            (3, 0, None),
            (3, 1, None),
            (3, 4, None),
            (4, 3, None),
        ):
            with self.subTest(mode=mode, grid_mode=grid_mode):
                parsed = decode(eco_record(mode=mode, grid_mode=grid_mode))
                self.assertEqual(parsed["energy_saver_mode"], expected)
                self.assertEqual(parsed["eco_grid_mode"], grid_mode)

    def test_both_app_layouts_decode_meter_phase_and_auto_resume(self) -> None:
        for layout in (86, 103):
            for phase, label in duml.METER_PHASES.items():
                # DJI Home shows every value other than 1 as off.
                for auto_sw, enabled in ((1, True), (2, False), (0, False)):
                    with self.subTest(layout=layout, phase=phase, auto_sw=auto_sw):
                        value = eco_record(layout=layout, phase=phase, auto_sw=auto_sw)
                        self.assertEqual(len(value), layout)
                        parsed = decode(value)
                        self.assertEqual(parsed["meter_phase"], label)
                        self.assertIs(parsed["auto_resume_enabled"], enabled)

    def test_unlabelled_phases_and_unknown_lengths_stay_unknown(self) -> None:
        for phase in (0, 5, 6, 8, 0x0104):
            with self.subTest(phase=phase):
                self.assertIsNone(decode(eco_record(phase=phase))["meter_phase"])
        for value in (
            eco_record(layout=86) + b"\x00",
            eco_record(layout=103)[:-1],
            eco_record(layout=103)[:96],
        ):
            with self.subTest(length=len(value)):
                parsed = decode(value)
                self.assertIsNone(parsed["meter_phase"])
                self.assertIsNone(parsed["auto_resume_enabled"])
                self.assertEqual(parsed["energy_saver_mode"], "self_consumption")
                self.assertIs(parsed["eco_meter_linked"], True)

    def test_longer_records_use_the_first_103_bytes_like_dji_home(self) -> None:
        parsed = decode(eco_record(phase=7, auto_sw=2) + b"\x01\x01extension")
        self.assertEqual(parsed["meter_phase"], "abc")
        self.assertIs(parsed["auto_resume_enabled"], False)

    def test_scheduled_switches_and_off_peak_power(self) -> None:
        parsed = decode(eco_record(peak=1, valley=2))
        self.assertIs(parsed["peak_discharge_enabled"], True)
        self.assertIs(parsed["off_peak_charge_enabled"], False)
        self.assertIs(parsed["off_peak_charge_power_available"], True)
        self.assertEqual(
            (
                parsed["off_peak_charge_power_min_w"],
                parsed["off_peak_charge_power_max_w"],
                parsed["off_peak_charge_power_w"],
            ),
            (200, 2000, 800),
        )
        for peak, valley in ((0, 3), (3, 0)):
            with self.subTest(peak=peak, valley=valley):
                parsed = decode(eco_record(peak=peak, valley=valley))
                self.assertIs(parsed["peak_discharge_enabled"], False)
                self.assertIs(parsed["off_peak_charge_enabled"], False)
        for bounds in ((100, 200, 150), (2000, 200, 199), (2000, 200, 2001)):
            with self.subTest(bounds=bounds):
                parsed = decode(eco_record(off_peak=bounds))
                self.assertIs(parsed["off_peak_charge_power_available"], False)
                self.assertIsNone(parsed["off_peak_charge_power_w"])
                self.assertIsNone(parsed["off_peak_charge_power_min_w"])
                self.assertIsNone(parsed["off_peak_charge_power_max_w"])

    def test_meter_link_ignores_nul_bytes_like_dji_home(self) -> None:
        for meter, linked in (
            (b"meter-id", True),
            (b"", False),
            (b"\x00meter-id", True),
            (b"\x00" * 36 + b"m", True),
            (b"\xff\xfe", False),
        ):
            with self.subTest(meter=meter):
                parsed = decode(eco_record(meter=meter))
                self.assertIs(parsed["eco_meter_linked"], linked)

    def test_short_record_clears_every_decoded_field(self) -> None:
        current = decode(eco_record())
        current.update(decode(eco_record()[:85]))
        for key in (
            "eco_available", "energy_saver_mode", "eco_grid_mode",
            "eco_meter_linked", "peak_discharge_enabled", "off_peak_charge_enabled",
            "off_peak_charge_power_w", "off_peak_charge_power_min_w",
            "off_peak_charge_power_max_w", "meter_phase", "auto_resume_enabled",
        ):
            self.assertIsNone(current[key], key)
        self.assertIs(current["off_peak_charge_power_available"], False)

    def test_unrelated_snapshot_keeps_decoded_fields(self) -> None:
        current = decode(eco_record())
        update = duml.parse_telemetry(
            duml.build_keyed_set_payload([(0x02, b"\x01")])
        )
        self.assertNotIn("energy_saver_mode", update)
        current.update(update)
        self.assertEqual(current["energy_saver_mode"], "self_consumption")
        self.assertIs(current["auto_resume_enabled"], True)


class EnergySaverBuildTests(unittest.TestCase):
    def test_mode_change_writes_only_mode_and_grid_mode(self) -> None:
        for layout in (86, 103):
            for source, target, changes in (
                (dict(mode=3, grid_mode=2), "disabled", {1: 1}),
                (dict(mode=3, grid_mode=3), "scheduled", {1: 2}),
                (dict(mode=1, grid_mode=2), "time_of_use", {1: 3, 16: 3}),
                (dict(mode=2, grid_mode=3), "self_consumption", {1: 3, 16: 2}),
                (dict(mode=3, grid_mode=3), "self_consumption", {1: 3, 16: 2}),
                (dict(mode=3, grid_mode=2), "time_of_use", {1: 3, 16: 3}),
            ):
                with self.subTest(layout=layout, source=source, target=target):
                    current = eco_record(layout=layout, **source)
                    for value in (current, current.hex()):
                        sent = sent_record(
                            duml.build_energy_saver_mode_set_payload(value, target)
                        )
                        expected = bytearray(current)
                        for offset, byte in changes.items():
                            expected[offset] = byte
                        self.assertEqual(sent, bytes(expected))
                        self.assertEqual(decode(sent)["energy_saver_mode"], target)

    def test_leaving_a_grid_tied_mode_keeps_grid_mode(self) -> None:
        for grid_mode in (2, 3):
            with self.subTest(grid_mode=grid_mode):
                sent = sent_record(
                    duml.build_energy_saver_mode_set_payload(
                        eco_record(grid_mode=grid_mode), "disabled"
                    )
                )
                self.assertEqual((sent[1], sent[16]), (1, grid_mode))

    def test_grid_tied_modes_need_an_earlier_setup(self) -> None:
        for grid_mode in (0, 1, 4):
            for target in duml.GRID_TIED_MODES:
                with (
                    self.subTest(grid_mode=grid_mode, target=target),
                    self.assertRaisesRegex(duml.ProtocolError, "in DJI Home"),
                ):
                    duml.build_energy_saver_mode_set_payload(
                        eco_record(mode=1, grid_mode=grid_mode), target
                    )
        for grid_mode in (0, 1):
            for target in ("disabled", "scheduled"):
                with self.subTest(grid_mode=grid_mode, target=target):
                    sent = sent_record(
                        duml.build_energy_saver_mode_set_payload(
                            eco_record(mode=1, grid_mode=grid_mode), target
                        )
                    )
                    self.assertEqual(decode(sent)["energy_saver_mode"], target)

    def test_self_consumption_needs_a_linked_meter(self) -> None:
        with self.assertRaisesRegex(duml.ProtocolError, "smart meter"):
            duml.build_energy_saver_mode_set_payload(
                eco_record(mode=3, grid_mode=3, meter=b""), "self_consumption"
            )
        sent = sent_record(
            duml.build_energy_saver_mode_set_payload(
                eco_record(mode=1, grid_mode=2, meter=b""), "time_of_use"
            )
        )
        self.assertEqual(decode(sent)["energy_saver_mode"], "time_of_use")

    def test_unlabelled_current_modes_can_be_replaced(self) -> None:
        for mode, grid_mode in ((0, 0), (0, 3), (3, 1), (4, 2)):
            with self.subTest(mode=mode, grid_mode=grid_mode):
                current = eco_record(mode=mode, grid_mode=grid_mode)
                self.assertIsNone(decode(current)["energy_saver_mode"])
                sent = sent_record(
                    duml.build_energy_saver_mode_set_payload(current, "disabled")
                )
                expected = bytearray(current)
                expected[1] = 1
                self.assertEqual(sent, bytes(expected))
        sent = sent_record(
            duml.build_energy_saver_mode_set_payload(
                eco_record(mode=0, grid_mode=3), "time_of_use"
            )
        )
        self.assertEqual(decode(sent)["energy_saver_mode"], "time_of_use")
        with self.assertRaisesRegex(duml.ProtocolError, "in DJI Home"):
            duml.build_energy_saver_mode_set_payload(
                eco_record(mode=3, grid_mode=1), "time_of_use"
            )

    def test_unknown_requested_mode_or_short_record_is_refused(self) -> None:
        for mode in ("Disabled", "off", "", None):
            with (
                self.subTest(mode=mode),
                self.assertRaisesRegex(duml.ProtocolError, "must be one of"),
            ):
                duml.build_energy_saver_mode_set_payload(eco_record(), mode)
        for value in (eco_record()[:85], "zz"):
            with self.subTest(length=len(value)), self.assertRaises(duml.ProtocolError):
                duml.build_energy_saver_mode_set_payload(value, "disabled")

    def test_auto_resume_changes_only_its_byte_in_either_layout(self) -> None:
        for layout, offset in ((86, 85), (103, 102)):
            for auto_sw, enabled, byte in ((2, True, 1), (1, False, 2), (1, True, 1)):
                with self.subTest(layout=layout, auto_sw=auto_sw, enabled=enabled):
                    current = eco_record(layout=layout, auto_sw=auto_sw)
                    sent = sent_record(
                        duml.build_auto_resume_set_payload(current, enabled)
                    )
                    expected = bytearray(current)
                    expected[offset] = byte
                    self.assertEqual(sent, bytes(expected))

    def test_auto_resume_replaces_any_state_in_a_known_layout(self) -> None:
        for auto_sw in (0, 3):
            with self.subTest(auto_sw=auto_sw):
                sent = sent_record(
                    duml.build_auto_resume_set_payload(
                        eco_record(auto_sw=auto_sw), True
                    )
                )
                self.assertEqual(sent[102], 1)
        longer = eco_record(auto_sw=1) + b"\x02extension"
        sent = sent_record(duml.build_auto_resume_set_payload(longer, False))
        self.assertEqual(sent, longer[:102] + b"\x02" + longer[103:])
        for value in (eco_record(layout=86)[:-1], eco_record(layout=103)[:-1]):
            with (
                self.subTest(length=len(value)),
                self.assertRaisesRegex(duml.ProtocolError, "unknown length"),
            ):
                duml.build_auto_resume_set_payload(value, True)

    def test_scheduled_switches_change_only_their_byte(self) -> None:
        for name, offset in duml.SCHEDULED_SWITCH_OFFSETS.items():
            for enabled, byte in ((True, 1), (False, 2)):
                with self.subTest(switch=name, enabled=enabled):
                    current = eco_record(mode=2, peak=2, valley=1)
                    sent = sent_record(
                        duml.build_scheduled_switch_set_payload(
                            current, name, enabled
                        )
                    )
                    expected = bytearray(current)
                    expected[offset] = byte
                    self.assertEqual(sent, bytes(expected))
        for value, name in (
            (eco_record(peak=0), "peak_discharge"),
            (eco_record(valley=3), "off_peak_charge"),
        ):
            with self.subTest(replaced=name):
                sent = sent_record(
                    duml.build_scheduled_switch_set_payload(value, name, True)
                )
                self.assertEqual(sent[duml.SCHEDULED_SWITCH_OFFSETS[name]], 1)
        for args, message in (
            ((eco_record(), "peak", True), "unknown Scheduled Periods switch"),
            ((eco_record()[:85], "peak_discharge", True), "86 bytes"),
        ):
            with (
                self.subTest(args=args[1:]),
                self.assertRaisesRegex(duml.ProtocolError, message),
            ):
                duml.build_scheduled_switch_set_payload(*args)

    def test_off_peak_power_stays_within_reported_bounds(self) -> None:
        current = eco_record(off_peak=(2000, 200, 800))
        for watts in (200, 1234, 2000):
            with self.subTest(watts=watts):
                sent = sent_record(
                    duml.build_off_peak_charge_power_set_payload(current, watts)
                )
                expected = bytearray(current)
                expected[12:16] = watts.to_bytes(4, "little")
                self.assertEqual(sent, bytes(expected))
        for watts in (199, 2001, 800.0, True, "800", None):
            with self.subTest(watts=watts), self.assertRaises(duml.ProtocolError):
                duml.build_off_peak_charge_power_set_payload(current, watts)
        with self.assertRaises(duml.ProtocolError):
            duml.build_off_peak_charge_power_set_payload(
                eco_record(off_peak=(100, 200, 150)), 150
            )


def offered(**changes) -> dict[str, object]:
    data = decode(eco_record()) | {"station_rules": [5, 6, 17]}
    return data | changes


class EnergySaverCapabilityTests(unittest.TestCase):
    def test_page_needs_availability_and_rule_5(self) -> None:
        feature = features.ModelFeature.ENERGY_SAVER
        self.assertTrue(features.feature_available(offered(), feature))
        for change in (
            {"eco_available": False}, {"eco_available": None},
            {"station_rules": [6, 17]}, {"station_rules": None},
        ):
            with self.subTest(change=change):
                self.assertFalse(features.feature_available(offered(**change), feature))
                self.assertEqual(features.energy_saver_options(offered(**change)), [])

    def test_unlabelled_mode_still_offers_modes(self) -> None:
        data = offered(energy_saver_mode=None)
        self.assertTrue(
            features.feature_available(data, features.ModelFeature.ENERGY_SAVER)
        )
        self.assertEqual(
            features.energy_saver_options(data), list(duml.ENERGY_SAVER_MODES)
        )
        self.assertEqual(
            features.energy_saver_options(data | {"eco_grid_mode": 0}),
            ["disabled", "scheduled"],
        )

    def test_grid_tied_modes_follow_rule_6_setup_and_meter(self) -> None:
        all_modes = list(duml.ENERGY_SAVER_MODES)
        for data, expected in (
            (offered(), all_modes),
            (
                offered(energy_saver_mode="disabled", eco_grid_mode=3),
                all_modes,
            ),
            (
                offered(energy_saver_mode="disabled", eco_meter_linked=False),
                ["disabled", "scheduled", "time_of_use"],
            ),
            (
                offered(energy_saver_mode="scheduled", eco_grid_mode=0),
                ["disabled", "scheduled"],
            ),
            (
                offered(energy_saver_mode="disabled", station_rules=[5, 17]),
                ["disabled", "scheduled"],
            ),
            # The active mode is always listed, even without its prerequisites.
            (
                offered(station_rules=[5]),
                ["disabled", "scheduled", "self_consumption"],
            ),
            (
                offered(
                    energy_saver_mode="time_of_use", eco_grid_mode=3,
                    eco_meter_linked=False,
                ),
                ["disabled", "scheduled", "time_of_use"],
            ),
        ):
            with self.subTest(
                mode=data["energy_saver_mode"], grid=data["eco_grid_mode"],
                rules=data["station_rules"], meter=data["eco_meter_linked"],
            ):
                self.assertEqual(features.energy_saver_options(data), expected)

    def test_auto_resume_scheduled_and_meter_phase_gates(self) -> None:
        feature = features.ModelFeature
        for mode in duml.GRID_TIED_MODES:
            with self.subTest(mode=mode):
                data = offered(energy_saver_mode=mode, power_adjustment="Automatic")
                self.assertTrue(features.feature_available(data, feature.AUTO_RESUME))
                self.assertTrue(features.feature_available(data, feature.METER_PHASE))
                self.assertFalse(
                    features.feature_available(data, feature.SCHEDULED_CONTROLS)
                )
        # In Time of Use, DJI Home shows the phase only for Automatic.
        for adjustment in ("Manual", None):
            with self.subTest(adjustment=adjustment):
                data = offered(
                    energy_saver_mode="time_of_use", power_adjustment=adjustment
                )
                self.assertTrue(features.feature_available(data, feature.AUTO_RESUME))
                self.assertFalse(
                    features.feature_available(data, feature.METER_PHASE)
                )
        self.assertTrue(features.feature_available(
            offered(energy_saver_mode="scheduled"), feature.SCHEDULED_CONTROLS
        ))
        for change, blocked in (
            ({"energy_saver_mode": "disabled"}, (feature.AUTO_RESUME,)),
            ({"energy_saver_mode": "scheduled"}, (feature.METER_PHASE,)),
            ({"energy_saver_mode": None}, (feature.AUTO_RESUME, feature.METER_PHASE)),
            ({"station_rules": [5, 17]}, (feature.AUTO_RESUME, feature.METER_PHASE)),
            ({"station_rules": [6, 17]}, (feature.AUTO_RESUME, feature.METER_PHASE)),
            ({"eco_available": False}, (feature.AUTO_RESUME, feature.METER_PHASE)),
            ({"station_rules": [5, 6]}, (feature.AUTO_RESUME,)),
            ({"auto_resume_enabled": None}, (feature.AUTO_RESUME,)),
            ({"eco_meter_linked": False}, (feature.METER_PHASE,)),
            ({"meter_phase": None}, (feature.METER_PHASE,)),
            ({"energy_saver_mode": "disabled"}, (feature.SCHEDULED_CONTROLS,)),
        ):
            for item in blocked:
                with self.subTest(change=change, feature=item):
                    self.assertFalse(
                        features.feature_available(offered(**change), item)
                    )


# Station rules 5, 6 and 17: count 30, mask 0x020060.
ENERGY_SAVER_RULES = b"\x0a\x00" + b"1e00600002"


def rules_record(*rules: int) -> bytes:
    text = (30).to_bytes(2, "little") + sum(1 << rule for rule in rules).to_bytes(
        3, "little"
    )
    return len(text.hex()).to_bytes(2, "little") + text.hex().encode()


class EnergySaverClient(StationClient):
    """Answer encoded GET/SET packets from synthetic keyed values."""

    def __init__(self, device, value: bytes) -> None:
        super().__init__(device, encrypted=False)
        self.values = {0x18: value, 0x0E: ENERGY_SAVER_RULES, 0x16: PEAK_VALUE}
        self.apply_set = True

    async def write_gatt_char(self, uuid, value, *, response):  # noqa: ARG002
        request = duml.DumlPacket.decode(value)
        self.requests.append((request.command_id, request.payload))
        if request.command_id == duml.GET_COMMAND:
            entries = [
                (key, self.values[key])
                for key in requested_config_keys(request.payload)
                if self.values.get(key) is not None
            ]
            reply = bytes(4) + duml.build_keyed_set_payload(entries, timestamp_ms=1)
        elif request.command_id == duml.SET_COMMAND:
            requested = duml.parse_keyed_values(request.payload)
            assert set(requested) == {0x18, 0x0E}
            if self.apply_set:
                self.values[0x18] = requested[0x18]
            reply = duml.build_keyed_set_payload(
                [(key, bytes(4)) for key in requested], timestamp_ms=1
            )
        else:
            raise AssertionError(f"unexpected command {request.command_id}")
        self.send(request.command_id, reply, sequence=request.sequence, flags=0x80)


GET_ECO = (duml.GET_COMMAND, b"\x00\x18\x10")
GET_RULES = (duml.GET_COMMAND, b"\x00\x0e\x10")
GET_PERIODS = (duml.GET_COMMAND, b"\x00\x16\x10")


class EnergySaverDeviceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.device = device_module.DjiPowerDevice(
            FakeBleDevice(), "ab" * 16, name="Station", model="DJI Power 2000"
        )
        self.client = EnergySaverClient(self.device, eco_record())
        self.device._client = self.client
        self.device._write_characteristic = object()
        sleeper = patch.object(device_module.asyncio, "sleep", new_callable=AsyncMock)
        self.sleep = sleeper.start()
        self.addCleanup(sleeper.stop)

    def sent(self) -> bytes:
        payloads = [
            payload for command, payload in self.client.requests
            if command == duml.SET_COMMAND
        ]
        self.assertEqual(len(payloads), 1)
        return sent_record(payloads[0])

    def assert_not_written(self) -> None:
        self.assertNotIn(
            duml.SET_COMMAND, [command for command, _ in self.client.requests]
        )

    async def test_disable_writes_only_mode_after_fresh_reads(self) -> None:
        await self.device.set_energy_saver_mode("disabled")

        self.assertEqual(
            self.client.requests[:2] + self.client.requests[3:],
            [GET_ECO, GET_RULES, GET_ECO],
        )
        expected = bytearray(eco_record())
        expected[1] = 1
        self.assertEqual(self.sent(), bytes(expected))
        self.assertEqual(self.device.data["energy_saver_mode"], "disabled")
        self.assertEqual(self.device.data["eco_grid_mode"], 2)
        self.sleep.assert_not_awaited()

    async def test_scheduled_modes_need_fresh_price_periods(self) -> None:
        self.client.values[0x18] = eco_record(mode=1, grid_mode=3)
        for mode in ("scheduled", "time_of_use"):
            with self.subTest(mode=mode):
                self.client.values[0x16] = PEAK_VALUE
                self.client.requests.clear()
                await self.device.set_energy_saver_mode(mode)
                self.assertEqual(
                    [request for request in self.client.requests
                     if request[0] == duml.GET_COMMAND],
                    [GET_ECO, GET_RULES, GET_PERIODS, GET_ECO],
                )
                self.assertEqual(self.device.data["energy_saver_mode"], mode)

                self.client.values[0x18] = eco_record(mode=1, grid_mode=3)
                self.client.values[0x16] = b""
                self.client.requests.clear()
                with self.assertRaisesRegex(
                    device_module.DjiPowerError, "time periods"
                ):
                    await self.device.set_energy_saver_mode(mode)
                self.assert_not_written()

    async def test_missing_periods_record_blocks_scheduled_modes(self) -> None:
        self.client.values[0x18] = eco_record(mode=1, grid_mode=3)
        self.client.values[0x16] = None
        for mode in ("scheduled", "time_of_use"):
            with (
                self.subTest(mode=mode),
                self.assertRaises(device_module.DjiPowerError),
            ):
                await self.device.set_energy_saver_mode(mode)
        self.assert_not_written()

    async def test_active_mode_is_not_rewritten(self) -> None:
        await self.device.set_energy_saver_mode("self_consumption")
        self.assertEqual(self.client.requests, [GET_ECO, GET_RULES])

    async def test_fresh_station_state_decides_each_mode(self) -> None:
        for record, rules, mode, message in (
            (eco_record(), rules_record(6, 17), "disabled", "Energy Saver modes"),
            (eco_record(mode=1), rules_record(5), "time_of_use", "grid-tied modes"),
            (
                eco_record(mode=1, grid_mode=0), ENERGY_SAVER_RULES,
                "time_of_use", "in DJI Home",
            ),
            (
                eco_record(mode=1, meter=b""), ENERGY_SAVER_RULES,
                "self_consumption", "smart meter",
            ),
        ):
            with self.subTest(mode=mode, message=message):
                self.client.values.update({0x18: record, 0x0E: rules})
                self.client.requests.clear()
                with self.assertRaisesRegex(device_module.DjiPowerError, message):
                    await self.device.set_energy_saver_mode(mode)
                self.assert_not_written()

    async def test_unlabelled_mode_is_replaced_like_dji_home(self) -> None:
        self.client.values[0x18] = eco_record(mode=0, grid_mode=0)
        await self.device.set_energy_saver_mode("disabled")
        expected = bytearray(eco_record(mode=0, grid_mode=0))
        expected[1] = 1
        self.assertEqual(self.sent(), bytes(expected))
        self.assertEqual(self.device.data["energy_saver_mode"], "disabled")

    async def test_unrecognized_model_sends_nothing(self) -> None:
        self.device.model = "DJI Power (0xFF)"
        for call_ in (
            self.device.set_energy_saver_mode("disabled"),
            self.device.set_auto_resume(False),
            self.device.set_scheduled_switch("peak_discharge", True),
            self.device.set_off_peak_charge_power(500),
        ):
            with self.assertRaisesRegex(device_module.DjiPowerError, "not supported"):
                await call_
        self.assertEqual(self.client.requests, [])

    async def test_auto_resume_write_and_rule_17_gate(self) -> None:
        for layout, offset in ((86, 85), (103, 102)):
            with self.subTest(layout=layout):
                self.client.values[0x0E] = ENERGY_SAVER_RULES
                self.client.values[0x18] = eco_record(layout=layout)
                self.client.requests.clear()
                await self.device.set_auto_resume(False)
                self.assertEqual(
                    self.client.requests[:2] + self.client.requests[3:],
                    [GET_ECO, GET_RULES, GET_ECO],
                )
                self.assertEqual(self.sent()[offset], 2)
                self.assertIs(self.device.data["auto_resume_enabled"], False)

                self.client.values[0x0E] = rules_record(5, 6)
                self.client.requests.clear()
                with self.assertRaisesRegex(
                    device_module.DjiPowerError, "does not offer Auto Resume"
                ):
                    await self.device.set_auto_resume(True)
                self.assert_not_written()

    async def test_scheduled_controls_need_scheduled_periods(self) -> None:
        self.client.values[0x18] = eco_record(mode=2, peak=2, valley=2)
        await self.device.set_scheduled_switch("peak_discharge", True)
        self.assertEqual(self.sent()[2:4], b"\x01\x02")
        self.client.requests.clear()
        await self.device.set_scheduled_switch("off_peak_charge", True)
        self.assertEqual(self.sent()[2:4], b"\x01\x01")
        self.client.requests.clear()
        await self.device.set_off_peak_charge_power(1500)
        self.assertEqual(self.sent()[12:16], (1500).to_bytes(4, "little"))
        self.assertEqual(self.device.data["off_peak_charge_power_w"], 1500)

        self.client.values[0x18] = eco_record(mode=3)
        self.client.requests.clear()
        for call_ in (
            self.device.set_scheduled_switch("peak_discharge", False),
            self.device.set_off_peak_charge_power(500),
        ):
            with self.assertRaisesRegex(
                device_module.DjiPowerError, "Scheduled Periods controls"
            ):
                await call_
        self.assert_not_written()

    async def test_scheduled_switches_turn_on_only_with_price_periods(self) -> None:
        self.client.values[0x18] = eco_record(mode=2, peak=2, valley=1)
        self.client.values[0x16] = b""
        with self.assertRaisesRegex(device_module.DjiPowerError, "time periods"):
            await self.device.set_scheduled_switch("peak_discharge", True)
        self.assertEqual(
            self.client.requests, [GET_ECO, GET_RULES, GET_PERIODS]
        )
        self.client.requests.clear()
        await self.device.set_scheduled_switch("off_peak_charge", False)
        self.assertNotIn(GET_PERIODS, self.client.requests)
        self.assertEqual(self.sent()[2:4], b"\x02\x02")

    async def test_unconfirmed_mode_change_reports_failure(self) -> None:
        self.client.apply_set = False
        with self.assertRaisesRegex(
            device_module.DjiPowerError, "requested eco-mode values"
        ):
            await self.device.set_energy_saver_mode("disabled")
        self.assertEqual(self.sleep.await_count, device_module.READBACK_RETRIES)
        self.assertEqual(self.device.data["energy_saver_mode"], "self_consumption")

    async def test_failed_eco_read_clears_every_decoded_field(self) -> None:
        await self.device._read_eco_mode()
        self.assertEqual(self.device.data["meter_phase"], "c")
        self.client.values[0x18] = None
        with self.assertRaises(device_module.DjiPowerError):
            await self.device.set_auto_resume(False)
        for key in (
            "key_18", "energy_saver_mode", "eco_grid_mode", "eco_meter_linked",
            "meter_phase", "auto_resume_enabled", "peak_discharge_enabled",
            "off_peak_charge_power_w",
        ):
            self.assertIsNone(self.device.data[key], key)
        self.assert_not_written()

    async def test_periodic_refresh_reads_eco_only_when_discovered(self) -> None:
        self.device.model = "DJI Power 1000 V2"
        self.client.values[0x18] = eco_record(mode=1)
        for discovered, reads in (([0x05, 0x0E], 0), ([0x0E, 0x18], 1)):
            with self.subTest(discovered=discovered):
                self.device.data["discovery_keys"] = discovered
                self.client.requests.clear()
                await self.device._refresh_accessory_config()
                self.assertEqual(self.client.requests.count(GET_ECO), reads)
        self.assertEqual(self.device.data["energy_saver_mode"], "disabled")


def entity_coordinator(data: dict) -> _Coordinator:
    coordinator = _Coordinator()
    coordinator.data = data
    for name in (
        "async_set_energy_saver_mode", "async_set_auto_resume",
        "async_set_scheduled_switch", "async_set_off_peak_charge_power",
    ):
        setattr(coordinator, name, AsyncMock())
    return coordinator


class EnergySaverEntityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.coordinator = entity_coordinator({})
        self.entry = types.SimpleNamespace(
            entry_id="station", async_on_unload=lambda unload: None
        )
        self.hass = types.SimpleNamespace(
            data={"dji_power_ble": {"station": self.coordinator}}
        )
        self.entities = []
        for platform in (switch, select, number):
            await platform.async_setup_entry(
                self.hass, self.entry, self.entities.extend
            )

    def energy_entities(self) -> dict[str, object]:
        return {
            entity._attr_unique_id.removeprefix(f"{ADDRESS}_"): entity
            for entity in self.entities
            if isinstance(entity, (
                select.DjiPowerEnergySaverModeSelect,
                switch.DjiPowerAutoResumeSwitch,
                switch.DjiPowerScheduledSwitch,
                number.DjiPowerOffPeakChargePowerNumber,
            ))
        }

    async def test_controls_appear_with_their_station_capability(self) -> None:
        self.assertEqual(self.energy_entities(), {})
        scheduled = offered(energy_saver_mode="scheduled")
        self.coordinator.publish(scheduled)
        self.assertEqual(set(self.energy_entities()), {
            "energy_saver_mode", "peak_discharge", "off_peak_charge",
            "off_peak_charge_power_w",
        })
        self.coordinator.publish(offered())
        self.coordinator.publish(offered())
        entities = self.energy_entities()
        self.assertEqual(set(entities), {
            "energy_saver_mode", "auto_resume", "peak_discharge",
            "off_peak_charge", "off_peak_charge_power_w",
        })
        self.assertEqual(len(self.entities), 5 + 2)  # Plus the two limits.
        self.assertTrue(entities["energy_saver_mode"].available)
        self.assertTrue(entities["auto_resume"].available)
        self.assertFalse(entities["peak_discharge"].available)
        self.assertFalse(entities["off_peak_charge_power_w"].available)

        self.coordinator.publish(scheduled)
        for entity in entities.values():
            self.assertIs(
                entity.available, entity is not entities["auto_resume"],
                entity._attr_unique_id,
            )
        self.coordinator.last_update_success = False
        for entity in entities.values():
            self.assertFalse(entity.available, entity._attr_unique_id)

    async def test_mode_select_lists_offered_modes_and_forwards_choice(self) -> None:
        self.coordinator.publish(offered(energy_saver_mode="disabled"))
        entity = self.energy_entities()["energy_saver_mode"]
        self.assertEqual(entity._attr_translation_key, "energy_saver_mode")
        self.assertEqual(entity.options, list(duml.ENERGY_SAVER_MODES))
        self.assertEqual(entity.current_option, "disabled")

        await entity.async_select_option("time_of_use")
        self.coordinator.async_set_energy_saver_mode.assert_awaited_once_with(
            "time_of_use"
        )
        self.assertEqual(entity.current_option, "disabled")

        self.coordinator.publish(
            offered(energy_saver_mode="disabled", eco_grid_mode=0)
        )
        self.assertEqual(entity.options, ["disabled", "scheduled"])
        for option in ("time_of_use", "self_consumption", "Disabled"):
            with (
                self.subTest(option=option),
                self.assertRaisesRegex(_ServiceValidationError, "DJI Home"),
            ):
                await entity.async_select_option(option)
        self.coordinator.async_set_energy_saver_mode.assert_awaited_once()

        # Like DJI Home, an unlabelled mode selects nothing but can be changed.
        self.coordinator.publish(offered(energy_saver_mode=None))
        self.assertTrue(entity.available)
        self.assertIsNone(entity.current_option)
        self.assertEqual(entity.options, list(duml.ENERGY_SAVER_MODES))
        await entity.async_select_option("scheduled")
        self.coordinator.async_set_energy_saver_mode.assert_awaited_with("scheduled")

        self.coordinator.publish(offered(station_rules=[6, 17]))
        self.assertFalse(entity.available)
        self.assertEqual(entity.options, list(duml.ENERGY_SAVER_MODES))

    async def test_switches_and_number_forward_requested_values(self) -> None:
        self.coordinator.publish(offered())
        self.coordinator.publish(offered(energy_saver_mode="scheduled"))
        entities = self.energy_entities()
        auto_resume = entities["auto_resume"]
        self.assertIs(auto_resume.is_on, True)
        await auto_resume.async_turn_off()
        await auto_resume.async_turn_on()
        self.assertEqual(
            self.coordinator.async_set_auto_resume.await_args_list,
            [call(False), call(True)],
        )
        for key, state in (("peak_discharge", True), ("off_peak_charge", False)):
            with self.subTest(switch=key):
                entity = entities[key]
                self.assertEqual(entity._attr_translation_key, key)
                self.assertIs(entity.is_on, state)
                await entity.async_turn_on()
                await entity.async_turn_off()
        self.assertEqual(
            self.coordinator.async_set_scheduled_switch.await_args_list,
            [
                call("peak_discharge", True),
                call("peak_discharge", False),
                call("off_peak_charge", True),
                call("off_peak_charge", False),
            ],
        )

        power = entities["off_peak_charge_power_w"]
        self.assertEqual(
            (power.native_min_value, power.native_max_value, power.native_value),
            (200, 2000, 800),
        )
        self.assertEqual(power._attr_native_step, 10)
        await power.async_set_native_value(1500.0)
        self.coordinator.async_set_off_peak_charge_power.assert_awaited_once_with(
            1500
        )
        with self.assertRaisesRegex(_ServiceValidationError, "whole number"):
            await power.async_set_native_value(1500.5)

    async def test_unknown_switch_states_are_unavailable(self) -> None:
        self.coordinator.publish(offered())
        self.coordinator.publish(offered(energy_saver_mode="scheduled"))
        entities = self.energy_entities()
        self.coordinator.publish(offered(
            energy_saver_mode="scheduled", peak_discharge_enabled=None,
            off_peak_charge_power_available=False,
        ))
        self.assertFalse(entities["peak_discharge"].available)
        self.assertTrue(entities["off_peak_charge"].available)
        self.assertFalse(entities["off_peak_charge_power_w"].available)
        self.coordinator.publish(offered(auto_resume_enabled=None))
        self.assertFalse(entities["auto_resume"].available)


class MeterPhaseSensorTests(unittest.TestCase):
    def test_meter_phase_follows_the_linked_meter(self) -> None:
        coordinator = types.SimpleNamespace(
            entry=types.SimpleNamespace(data={"address": ADDRESS}, title="Station"),
            device=types.SimpleNamespace(
                model="DJI Power 2000", address=ADDRESS, serial_number=None
            ),
            last_update_success=True,
            data=offered(),
        )
        entity = sensor.DjiPowerMeterPhaseSensor(
            coordinator, sensor.METER_PHASE_DESCRIPTION
        )
        self.assertEqual(entity._attr_unique_id, f"{ADDRESS}_meter_phase")
        self.assertEqual(
            sensor.METER_PHASE_DESCRIPTION.options, ["a", "b", "ab", "c", "abc"]
        )
        self.assertTrue(entity.available)
        self.assertEqual(entity.native_value, "c")
        for change in (
            {"eco_meter_linked": False}, {"meter_phase": None},
            {"energy_saver_mode": "disabled"}, {"station_rules": [5, 17]},
        ):
            with self.subTest(change=change):
                coordinator.data = offered(**change)
                self.assertFalse(entity.available)


class EnergySaverCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.device = types.SimpleNamespace(
            data={"energy_saver_mode": "disabled"},
            set_energy_saver_mode=AsyncMock(),
            set_auto_resume=AsyncMock(),
            set_scheduled_switch=AsyncMock(),
            set_off_peak_charge_power=AsyncMock(),
        )
        self.coordinator = coordinator_module.DjiPowerCoordinator.__new__(
            coordinator_module.DjiPowerCoordinator
        )
        self.coordinator.device = self.device
        self.coordinator._publish = Mock()

    async def test_confirmed_writes_publish_and_failures_become_service_errors(
        self,
    ) -> None:
        for method, device_method, args in (
            ("async_set_energy_saver_mode", "set_energy_saver_mode", ("scheduled",)),
            ("async_set_auto_resume", "set_auto_resume", (True,)),
            (
                "async_set_scheduled_switch", "set_scheduled_switch",
                ("off_peak_charge", False),
            ),
            ("async_set_off_peak_charge_power", "set_off_peak_charge_power", (900,)),
        ):
            with self.subTest(method=method):
                self.coordinator._publish.reset_mock()
                await getattr(self.coordinator, method)(*args)
                getattr(self.device, device_method).assert_awaited_with(*args)
                self.coordinator._publish.assert_called_once_with(self.device.data)
                self.assertIsNot(
                    self.coordinator._publish.call_args.args[0], self.device.data
                )

                self.coordinator._publish.reset_mock()
                getattr(self.device, device_method).side_effect = _DjiPowerError(
                    "station refused"
                )
                with self.assertRaisesRegex(_HomeAssistantError, "station refused"):
                    await getattr(self.coordinator, method)(*args)
                self.coordinator._publish.assert_not_called()
