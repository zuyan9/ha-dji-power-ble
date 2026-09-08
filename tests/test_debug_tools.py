"""Offline tests for public capture tooling and its sharing boundary."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import struct
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "dji_power_debug.py"
SPEC = importlib.util.spec_from_file_location("dji_debug_test", SCRIPT)
assert SPEC and SPEC.loader
debug = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(debug)


def packet(payload=b"\x01\x00\x00\x00\x00", *, command=0x6A, response=True, sequence=7):
    return debug.duml.DumlPacket(
        0xAB if response else 2,
        2 if response else 0xAB,
        sequence,
        0x80 if response else 0x20,
        0x5A,
        command,
        payload,
    )


def gatt(data, *, direction="rx", connection=1, channel=0x38):
    return {
        "event": "gatt",
        "t": 1.0,
        "direction": direction,
        "connection": connection,
        "channel": channel,
        "data": data.hex(),
    }


def acl(data, *, handle=1, boundary=2):
    return b"\x02" + struct.pack("<HH", handle | (boundary << 12), len(data)) + data


def l2cap(data, *, cid=4, channel=0x38, opcode=0x1B):
    att = bytes((opcode,)) + struct.pack("<H", channel) + data
    return struct.pack("<HH", len(att), cid) + att


def snoop(*records):
    data = b"btsnoop\x00" + struct.pack(">II", 1, 1002)
    for index, (flags, body) in enumerate(records):
        data += struct.pack(">IIIIQ", len(body), len(body), flags, 0, 1000000 + index)
        data += body
    return io.BytesIO(data)


class DebugDecodeTests(unittest.TestCase):
    def test_summary_omits_identifiers_and_arbitrary_payloads(self):
        key = b"ab" * 16
        nonce = b"\x12\x34\x56\x78"
        address = "AA:BB:CC:DD:EE:FF"
        events = [
            {
                "event": "advertisement",
                "t": 0.0,
                "address": address,
                "name": "PRIVATE-STATION",
                "rssi": -60,
                "manufacturer_data": "971110aabbccddeeff",
            },
            gatt(packet(b"\x00", response=False).encode(), direction="tx"),
            gatt(packet(b"\x00" + nonce).encode()),
            gatt(
                packet(b"\x01" + nonce + key + b"\x00", response=False).encode(),
                direction="tx",
            ),
            gatt(packet(key + b"SERIAL", command=0x62).encode()),
        ]
        rows = list(debug.decode(iter(events)))
        self.assertEqual(rows[0]["station"], "station_1")
        self.assertEqual(rows[0]["model"], "DJI Power 1000 V2")
        self.assertTrue(rows[0]["bound"])
        self.assertEqual(rows[2]["stage"], "start_bind")
        self.assertEqual(rows[2]["status"], "00")
        self.assertTrue(rows[3]["payload_redacted"])
        output = json.dumps(rows)
        for sensitive in (
            address,
            "aabbccddeeff",
            "PRIVATE-STATION",
            key.decode(),
            key.hex(),
            nonce.hex(),
            "SERIAL",
        ):
            self.assertNotIn(sensitive, output)
        self.assertFalse(any("payload" in row for row in rows))

    def test_unknown_models_and_short_advertisements_remain_inspectable(self):
        for data, expected in (
            ("fe11", {"model": "DJI Power (0xFE)", "bound": True}),
            ("fe", {"malformed_advertisement": True}),
        ):
            with self.subTest(data=data):
                event = {
                    "event": "advertisement",
                    "t": 0.0,
                    "address": "AA:BB:CC:DD:EE:FF",
                    "rssi": -60,
                    "manufacturer_data": data,
                }
                summary = list(debug.decode(iter([event])))[0]
                for field, value in expected.items():
                    self.assertEqual(summary[field], value)
                self.assertNotIn("manufacturer_data", summary)
                raw = list(debug.decode(iter([event]), raw=True))[0]
                self.assertEqual(raw["manufacturer_data"], data)

    def test_unknown_commands_are_reported_without_a_payload_schema(self):
        frame = debug.duml.DumlPacket(0x30, 2, 7, 0x80, 0x7F, 0xFE, b"new-format")
        events = [gatt(frame.encode())]
        summary = list(debug.decode(iter(events)))[0]
        self.assertEqual(summary["command_set"], 0x7F)
        self.assertEqual(summary["command_id"], 0xFE)
        self.assertEqual(summary["payload_length"], len(frame.payload))
        self.assertNotIn("payload", summary)
        raw = list(debug.decode(iter(events), raw=True, command=0xFE))[0]
        self.assertEqual(raw["payload"], frame.payload.hex())

    def test_raw_mode_exposes_responses_but_suppresses_auth_credentials(self):
        request = packet(b"\x01" + b"\x12" * 4 + b"ab" * 16 + b"\x00", response=False)
        reply = packet()
        rows = list(
            debug.decode(
                iter([gatt(request.encode(), direction="tx"), gatt(reply.encode())]),
                raw=True,
            )
        )
        self.assertNotIn("payload", rows[0])
        self.assertTrue(rows[0]["payload_redacted"])
        self.assertEqual(rows[1]["payload"], "0100000000")

    def test_fragmented_gatt_values_and_command_filter(self):
        first = packet(command=0x61).encode()
        second = packet(b"alarm", command=0x66).encode()
        events = [gatt(first[:8]), gatt(first[8:] + second)]
        rows = list(debug.decode(iter(events), raw=True, command=0x66))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["payload"], b"alarm".hex())

    def test_streams_are_separate_per_connection_direction_and_characteristic(self):
        value = packet().encode()
        events = [
            gatt(value[:8]),
            gatt(value[8:], connection=2),
            gatt(value[8:], channel=0x99),
            gatt(value[8:], direction="tx"),
            gatt(value[8:]),
        ]
        rows = list(debug.decode(iter(events)))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["direction"], "rx")
        self.assertEqual(rows[0]["status"], "01")

    def test_handle_reuse_discards_partial_frames_and_request_history(self):
        value = packet().encode()
        events = [
            gatt(packet(b"\x00", response=False).encode(), direction="tx"),
            gatt(value[:8]),
            {"event": "reset", "t": 2.0, "connection": 1},
            gatt(value[8:]),
            gatt(value),
        ]
        rows = list(debug.decode(iter(events)))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["stage"], "unknown")
        self.assertNotEqual(rows[0]["link"], rows[1]["link"])

    def test_btsnoop_reassembles_interleaved_acl_fragments_and_att_values(self):
        value = packet().encode()
        first = l2cap(value[:8])
        source = snoop(
            (1, acl(first[:6])),
            (1, acl(l2cap(value), handle=2)),
            (1, acl(first[6:], boundary=1)),
            (1, acl(l2cap(value[8:]))),
        )
        rows = list(debug.decode(debug.read_btsnoop(source)))
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]["link"], rows[1]["link"])
        self.assertTrue(all(row["status"] == "01" for row in rows))

    def test_btsnoop_ignores_other_channels_and_bad_crc(self):
        value = packet().encode()
        bad = value[:-1] + bytes((value[-1] ^ 1,))
        source = snoop(
            (1, acl(l2cap(value, cid=5))),
            (1, acl(l2cap(bad + value))),
        )
        self.assertEqual(len(list(debug.decode(debug.read_btsnoop(source)))), 1)

    def test_btsnoop_disconnect_clears_partial_acl(self):
        value = l2cap(packet().encode())
        source = snoop(
            (1, acl(value[:8])),
            (1, b"\x04\x05\x04\x00\x01\x00\x13"),
            (1, acl(value[8:], boundary=1)),
            (1, acl(value)),
        )
        self.assertEqual(len(list(debug.decode(debug.read_btsnoop(source)))), 1)

    def test_invalid_and_truncated_snoop_fail_explicitly(self):
        valid = snoop((1, acl(l2cap(packet().encode())))).getvalue()
        for value in (b"invalid", valid[:20], valid[:-1]):
            with self.subTest(length=len(value)), self.assertRaises(ValueError):
                list(debug.read_btsnoop(io.BytesIO(value)))

    def test_recorder_retains_raw_notification_chunks_but_not_sent_key(self):
        stream = io.StringIO()
        recorder = debug.Recorder(stream, "capture")
        recorder.gatt("tx", packet(b"\x00", response=False).encode())
        recorder.gatt("rx", packet().encode())
        recorder.gatt(
            "tx",
            packet(
                b"\x01" + b"\x12" * 4 + b"ab" * 16 + b"\x00", response=False
            ).encode(),
        )
        text = stream.getvalue()
        self.assertNotIn((b"ab" * 16).hex(), text)
        events = [json.loads(line) for line in text.splitlines()]
        self.assertEqual(events[2]["data"], packet().encode().hex())
        rows = list(debug.decode(iter(events), raw=True))
        self.assertEqual(rows[2]["payload_length"], 38)
        self.assertTrue(rows[2]["payload_redacted"])

    def test_output_is_private_and_existing_files_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.jsonl"
            with debug.private_output(path) as stream:
                stream.write("existing evidence")
            with self.assertRaises(FileExistsError):
                debug.private_output(path)
            self.assertEqual(path.read_text(), "existing evidence")
            if os.name == "posix":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_decode_cli_works_offline_outside_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.jsonl"
            with path.open("w") as stream:
                debug.Recorder(stream, "capture").gatt("rx", packet().encode())
            result = subprocess.run(
                [sys.executable, "-I", str(SCRIPT), "decode", str(path)],
                cwd=directory,
                text=True,
                capture_output=True,
                check=True,
            )
        row = json.loads(result.stdout)
        self.assertEqual(row["status"], "01")
        self.assertNotIn("payload", row)


class DebugCaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_capture_reuses_real_session_auth_gets_and_cleanup(self):
        from .test_device import device_module

        requests = []
        closed = []

        class Client:
            is_connected = True

            def __init__(self, device):
                self.device = device

            async def start_notify(self, uuid, callback):
                self.callback = callback

            async def stop_notify(self, uuid):
                pass

            async def disconnect(self):
                self.is_connected = False
                closed.append(True)

            async def write_gatt_char(self, uuid, data, *, response):
                request = debug.duml.DumlPacket.decode(data)
                requests.append(request)
                if request.command_id == 0x6A:
                    payload = b"\x00\x12\x34\x56\x78"
                else:
                    payload = b"\x00" * 4 + debug.duml.build_keyed_header(1)
                    self.device._report_event.set()
                reply = packet(
                    payload, command=request.command_id, sequence=request.sequence
                ).encode()
                self.callback(None, reply[:6])
                self.callback(None, reply[6:])

        class Device(device_module.DjiPowerDevice):
            async def _establish(self):
                return Client(self)

        stream = io.StringIO()
        args = types.SimpleNamespace(key_file=None, seconds=0.001)
        with (
            patch.object(
                debug.importlib,
                "import_module",
                return_value=types.SimpleNamespace(DjiPowerDevice=Device),
            ),
            patch.object(
                debug,
                "scan",
                AsyncMock(
                    return_value={
                        "address": types.SimpleNamespace(address="AA:BB:CC:DD:EE:FF")
                    }
                ),
            ),
            patch.object(debug.getpass, "getpass", return_value="ab" * 16),
        ):
            await debug.capture(args, debug.Recorder(stream, "capture"))

        self.assertEqual(
            [request.command_id for request in requests], [0x6A, 0x6A, 0x60, 0x60]
        )
        self.assertEqual(
            requests[1].payload, b"\x01\x12\x34\x56\x78" + b"ab" * 16 + b"\x00"
        )
        self.assertEqual(closed, [True])
        self.assertNotIn((b"ab" * 16).hex(), stream.getvalue())
        rows = list(
            debug.decode(
                iter(json.loads(line) for line in stream.getvalue().splitlines())
            )
        )
        replies = [row for row in rows if row.get("direction") == "rx"]
        self.assertEqual(replies[1]["stage"], "check_secret_key")
        self.assertEqual(replies[1]["status"], "00")

    async def test_capture_preserves_rejection_and_disconnects_on_failure(self):
        disconnected = []

        class Client:
            async def write_gatt_char(self, uuid, data, *, response):
                pass

        class Device:
            def __init__(self, *args, **kwargs):
                pass

            def add_disconnect_listener(self, callback):
                pass

            async def _establish(self):
                return Client()

            def _on_notify(self, characteristic, chunk):
                pass

            async def connect(self):
                client = await self._establish()
                await client.write_gatt_char(
                    "uuid", packet(b"\x00", response=False).encode(), response=True
                )
                value = packet().encode()
                self._on_notify(None, value[:5])
                self._on_notify(None, value[5:])
                raise RuntimeError("authentication rejected")

            async def disconnect(self):
                disconnected.append(True)

        stream = io.StringIO()
        args = types.SimpleNamespace(key_file=None, seconds=1)
        with (
            patch.object(
                debug.importlib,
                "import_module",
                return_value=types.SimpleNamespace(DjiPowerDevice=Device),
            ),
            patch.object(debug, "scan", AsyncMock(return_value={"address": object()})),
            patch.object(debug.getpass, "getpass", return_value="ab" * 16),
            self.assertRaisesRegex(RuntimeError, "authentication rejected"),
        ):
            await debug.capture(args, debug.Recorder(stream, "capture"))
        self.assertEqual(disconnected, [True])
        rows = list(
            debug.decode(
                iter(json.loads(line) for line in stream.getvalue().splitlines()),
                raw=True,
            )
        )
        replies = [row for row in rows if row.get("direction") == "rx"]
        self.assertEqual(replies[0]["payload"], "0100000000")
        self.assertEqual(replies[0]["stage"], "start_bind")

    async def test_scan_filters_manufacturer_and_address_but_not_model(self):
        class Scanner:
            def __init__(self, detection_callback):
                self.callback = detection_callback

            async def __aenter__(self):
                for address, manufacturer in (
                    ("other", {}),
                    ("unselected", {2218: b"\x97\x11"}),
                    ("AA:BB", {2218: b"\xfe\x11"}),
                ):
                    self.callback(
                        types.SimpleNamespace(address=address),
                        types.SimpleNamespace(
                            manufacturer_data=manufacturer,
                            local_name="private-name",
                            rssi=-50,
                        ),
                    )

            async def __aexit__(self, *args):
                pass

        stream = io.StringIO()
        args = types.SimpleNamespace(address="aa:bb", scan_seconds=0.001)
        with patch.dict(
            sys.modules, {"bleak": types.SimpleNamespace(BleakScanner=Scanner)}
        ):
            found = await debug.scan(args, debug.Recorder(stream, "scan"))
        self.assertEqual(list(found), ["AA:BB"])
        records = [json.loads(line) for line in stream.getvalue().splitlines()]
        self.assertEqual(len(records), 2)
        self.assertEqual(records[1]["manufacturer_data"], "fe11")


if __name__ == "__main__":
    unittest.main()
