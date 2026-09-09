#!/usr/bin/env python3
"""Scan, capture, and inspect DJI Power BLE traffic. See docs/troubleshooting.md."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import getpass
import importlib
import json
import math
import os
import struct
import sys
import time
import types
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO, TextIO

# Load the shipped codec/session without importing Home Assistant's entry point.
COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "dji_power_ble"
PACKAGE = "_dji_power_debug"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(COMPONENT)]
sys.modules.setdefault(PACKAGE, package)
duml = importlib.import_module(f"{PACKAGE}.duml")

MANUFACTURER_ID = 0x08AA
FORMAT = "dji-power-ble-capture-v1"
AUTH_STAGES = {0: "start_bind", 1: "check_secret_key"}


def private_output(path: Path) -> TextIO:
    """Create a new capture; never overwrite an existing file."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(fd, "w", encoding="utf-8")


def packet_fields(packet) -> dict:
    return {
        "source": packet.source,
        "destination": packet.destination,
        "sequence": packet.sequence,
        "flags": packet.flags,
        "command_set": packet.command_set,
        "command_id": packet.command_id,
        "payload_length": len(packet.payload),
    }


def credential_request(packet) -> bool:
    """Suppress entire authentication requests that could contain credentials."""
    return (
        packet.command_set == duml.POWER_COMMAND_SET
        and packet.command_id in (duml.AUTH_COMMAND, 0x68)
        and not packet.is_response
        and packet.payload != b"\x00"
    )


def auth_stage(packet) -> str:
    """Interpret an operation only when its payload is plaintext."""
    if packet.flags & 0x0F:
        return "encrypted"
    return AUTH_STAGES.get(packet.payload[0] if packet.payload else -1, "unknown")


class Recorder:
    """Write private JSONL evidence, including original notification boundaries."""

    def __init__(self, output: TextIO, mode: str) -> None:
        self.output = output
        self.started = time.monotonic()
        self.emit("session", format=FORMAT, mode=mode, platform=sys.platform)

    def emit(self, event: str, **fields) -> None:
        row = {"event": event, "t": round(time.monotonic() - self.started, 6), **fields}
        self.output.write(json.dumps(row) + "\n")
        self.output.flush()

    def gatt(self, direction: str, value: bytes | bytearray) -> None:
        if direction == "tx":
            packet = duml.DumlPacket.decode(bytes(value))
            if credential_request(packet):
                self.emit(
                    "redacted_auth",
                    direction=direction,
                    stage=auth_stage(packet),
                    **packet_fields(packet),
                )
                return
        self.emit("gatt", direction=direction, data=value.hex())


def recording_device_class(base):
    """Instrument the existing session; its auth and GET behavior stay shared."""

    class RecordingClient:
        def __init__(self, client, recorder) -> None:
            self.client = client
            self.recorder = recorder

        def __getattr__(self, name):
            return getattr(self.client, name)

        async def write_gatt_char(self, uuid, data, *, response):
            self.recorder.gatt("tx", data)
            await self.client.write_gatt_char(uuid, data, response=response)

    class RecordingDevice(base):
        def __init__(self, *args, recorder, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.recorder = recorder

        async def _establish(self):
            client = await super()._establish()
            self.recorder.emit("reset", connection=0)
            return RecordingClient(client, self.recorder)

        def _on_notify(self, characteristic, chunk) -> None:
            self.recorder.gatt("rx", chunk)
            super()._on_notify(characteristic, chunk)

    return RecordingDevice


async def scan(args, recorder: Recorder, *, advertisements: dict | None = None):
    from bleak import BleakScanner

    found = {}

    def discovered(device, advertisement):
        data = advertisement.manufacturer_data.get(MANUFACTURER_ID)
        if data is None:
            return
        if args.address and device.address.casefold() != args.address.casefold():
            return
        recorder.emit(
            "advertisement",
            address=device.address,
            name=advertisement.local_name,
            rssi=advertisement.rssi,
            manufacturer_data=data.hex(),
        )
        if device.address not in found:
            # Local selection aid; the shareable report is produced by `decode`.
            print(f"Found DJI advertisement: {device.address}", file=sys.stderr)
        found[device.address] = device
        if advertisements is not None:
            advertisements[device.address] = data

    async with BleakScanner(detection_callback=discovered):
        await asyncio.sleep(args.scan_seconds)
    return found


async def capture(args, recorder: Recorder) -> None:
    module = importlib.import_module(f"{PACKAGE}.device")
    key = duml.normalize_pair_key(
        args.key_file.read_text(encoding="ascii")
        if args.key_file
        else getpass.getpass("Existing pair key (hidden): ")
    )
    advertisements = {}
    devices = await scan(args, recorder, advertisements=advertisements)
    if not devices:
        raise RuntimeError("No matching DJI advertisement; no connection attempted")
    address, ble_device = next(iter(devices.items()))
    model = "DJI Power"
    if address in advertisements:
        with contextlib.suppress(duml.ProtocolError):
            model = duml.parse_manufacturer_data(advertisements[address]).model
    device = recording_device_class(module.DjiPowerDevice)(
        ble_device, key, name="Debug capture", model=model, recorder=recorder
    )
    disconnected = asyncio.Event()
    device.add_disconnect_listener(lambda _: disconnected.set())
    try:
        recorder.emit("connecting")
        await device.connect()
        recorder.emit("authenticated")
        try:
            async with asyncio.timeout(args.seconds):
                await disconnected.wait()
            raise RuntimeError("Station disconnected during capture")
        except TimeoutError:
            pass
    finally:
        await device.disconnect()


def read_btsnoop(stream: BinaryIO) -> Iterator[dict]:
    """Extract ATT values from H4 btsnoop, reassembling interleaved ACL traffic."""
    if stream.read(16) != b"btsnoop\x00" + struct.pack(">II", 1, 1002):
        raise ValueError("Expected btsnoop version 1 with H4 link type 1002")
    pending: dict[tuple[int, str], bytearray] = {}
    first_timestamp = None
    while header := stream.read(24):
        if len(header) != 24:
            raise ValueError("Truncated btsnoop record header")
        original, included, flags, _drops, timestamp = struct.unpack(">IIIIQ", header)
        if included > original or included > 1024 * 1024:
            raise ValueError("Invalid btsnoop record length")
        data = stream.read(included)
        if len(data) != included:
            raise ValueError("Truncated btsnoop record body")
        if first_timestamp is None:
            first_timestamp = timestamp
        elapsed = (timestamp - first_timestamp) / 1_000_000
        # Reset streams on disconnect or connection-complete (handles are reused).
        handle = None
        if len(data) >= 6 and data[:2] in (b"\x04\x05", b"\x04\x03"):
            handle = int.from_bytes(data[4:6], "little") & 0x0FFF
        elif len(data) >= 7 and data[:2] == b"\x04\x3e" and data[3] in (1, 10):
            handle = int.from_bytes(data[5:7], "little") & 0x0FFF
        if handle is not None:
            for key in list(pending):
                if key[0] == handle:
                    del pending[key]
            yield {"event": "reset", "connection": handle, "t": elapsed}
            continue
        if len(data) < 5 or data[0] != 2:
            continue
        packed_handle, length = struct.unpack_from("<HH", data, 1)
        handle = packed_handle & 0x0FFF
        boundary = (packed_handle >> 12) & 3
        direction = "rx" if flags & 1 else "tx"
        key = (handle, direction)
        if len(data) != length + 5:
            pending.pop(key, None)
            continue
        if boundary in (0, 2):
            pending[key] = bytearray(data[5:])
        elif boundary == 1 and key in pending:
            pending[key].extend(data[5:])
        else:
            continue
        buffer = pending[key]
        if len(buffer) < 4:
            continue
        l2_length, cid = struct.unpack_from("<HH", buffer)
        if len(buffer) < l2_length + 4:
            continue
        del pending[key]
        if len(buffer) != l2_length + 4 or cid != 4:
            continue
        att = buffer[4:]
        if len(att) < 3 or att[0] not in (0x12, 0x52, 0x1B, 0x1D):
            continue
        yield {
            "event": "gatt",
            "t": elapsed,
            "connection": handle,
            "direction": direction,
            "channel": int.from_bytes(att[1:3], "little"),
            "data": att[3:].hex(),
        }


def read_capture(path: Path) -> Iterator[dict]:
    with path.open("rb") as stream:
        if stream.read(8) == b"btsnoop\x00":
            stream.seek(0)
            yield from read_btsnoop(stream)
            return
    with path.open(encoding="utf-8") as stream:
        header = json.loads(stream.readline())
        if header.get("event") != "session" or header.get("format") != FORMAT:
            raise ValueError("Expected a DJI debug JSONL capture or H4 btsnoop file")
        yield header
        for line in stream:
            if line.strip():
                yield json.loads(line)


def decode(
    events: Iterator[dict], *, raw: bool = False, command=None
) -> Iterator[dict]:
    """Emit metadata by default; arbitrary payloads require explicit raw mode."""
    streams = {}
    operations = {}
    stations = {}
    links = {}
    link_number = 0
    for event in events:
        kind = event["event"]
        elapsed = round(float(event["t"]), 6)
        connection = event.get("connection", 0)
        if kind == "reset":
            streams = {
                key: value for key, value in streams.items() if key[0] != connection
            }
            operations = {
                key: value for key, value in operations.items() if key[0] != connection
            }
            links.pop(connection, None)
            continue
        if kind == "advertisement":
            if command is not None:
                continue
            address = event["address"]
            station = stations.setdefault(address, f"station_{len(stations) + 1}")
            data = bytes.fromhex(event["manufacturer_data"])
            row = {
                "event": kind,
                "t": elapsed,
                "station": station,
                "rssi": event["rssi"],
            }
            try:
                parsed = duml.parse_manufacturer_data(data)
                row.update(model=parsed.model, bound=parsed.bound)
            except duml.ProtocolError:
                row["malformed_advertisement"] = True
            if raw:
                row.update(address=address, manufacturer_data=data.hex())
            yield row
            continue
        if kind not in ("gatt", "redacted_auth"):
            if command is None and kind in (
                "connecting",
                "authenticated",
                "error",
                "done",
            ):
                yield {"event": kind, "t": elapsed}
            continue
        if connection not in links:
            link_number += 1
            links[connection] = f"link_{link_number}"
        link = links[connection]
        direction = event["direction"]
        if direction not in ("rx", "tx"):
            raise ValueError("Invalid capture direction")
        if kind == "redacted_auth":
            # The recorder discarded the entire credential-bearing request.
            packet = duml.DumlPacket(
                *(
                    int(event[field])
                    for field in (
                        "source",
                        "destination",
                        "sequence",
                        "flags",
                        "command_set",
                        "command_id",
                    )
                )
            )
            packets = [packet]
        else:
            key = (connection, direction, event.get("channel", 0))
            packets = streams.setdefault(key, duml.DumlStream()).feed(
                bytes.fromhex(event["data"])
            )
        for packet in packets:
            row = {
                "event": "packet",
                "t": elapsed,
                "link": link,
                "direction": direction,
                **packet_fields(packet),
            }
            if kind == "redacted_auth":
                row["payload_length"] = int(event["payload_length"])
            auth = (
                packet.command_set == duml.POWER_COMMAND_SET
                and packet.command_id == duml.AUTH_COMMAND
            )
            request_key = (
                connection,
                packet.sequence,
                packet.command_set,
                packet.command_id,
            )
            if auth:
                if packet.is_response:
                    row["stage"] = operations.pop(request_key, "unknown")
                    row["status"] = (
                        "encrypted"
                        if packet.flags & 0x0F
                        else packet.payload[:1].hex() or "missing"
                    )
                else:
                    stage = (
                        event.get("stage", "unknown")
                        if kind == "redacted_auth"
                        else auth_stage(packet)
                    )
                    row["stage"] = stage
                    operations[request_key] = stage
            if command is not None and packet.command_id != command:
                continue
            if kind == "redacted_auth" or credential_request(packet):
                row["payload_redacted"] = True
            elif raw:
                row["payload"] = packet.payload.hex()
            yield row


def seconds(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or not 0 < number <= 600:
        raise argparse.ArgumentTypeError(
            "seconds must be greater than 0 and at most 600"
        )
    return number


def command_id(value: str) -> int:
    number = int(value, 0)
    if not 0 <= number <= 255:
        raise argparse.ArgumentTypeError("command must fit in one byte")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    for name in ("scan", "capture"):
        action = actions.add_parser(
            name, help=f"{name} using this computer's Bluetooth"
        )
        action.add_argument(
            "--output", type=Path, required=True, help="new private JSONL file"
        )
        action.add_argument(
            "--address", required=name == "capture", help="BLE address or macOS UUID"
        )
        action.add_argument("--scan-seconds", type=seconds, default=10.0)
        if name == "capture":
            action.add_argument(
                "--seconds", type=seconds, default=30.0, help="listen time after setup"
            )
            action.add_argument(
                "--key-file",
                type=Path,
                help="existing pair key; otherwise prompt securely",
            )
    action = actions.add_parser(
        "decode", help="inspect JSONL or H4 btsnoop offline; no BLE needed"
    )
    action.add_argument("input", type=Path)
    action.add_argument("--output", type=Path, help="new report file; default stdout")
    action.add_argument(
        "--command", type=command_id, help="filter command ID, e.g. 0x66"
    )
    action.add_argument(
        "--raw",
        action="store_true",
        help="include private payloads and advertisement addresses",
    )
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    recorder = None
    try:
        if args.action == "decode":
            output = (
                private_output(args.output)
                if args.output
                else contextlib.nullcontext(sys.stdout)
            )
            count = 0
            with output as stream:
                for row in decode(
                    read_capture(args.input), raw=args.raw, command=args.command
                ):
                    stream.write(json.dumps(row) + "\n")
                    count += 1
            print(f"Decoded {count} records", file=sys.stderr)
            if count == 0:
                print(
                    "No matching records. Check the filter and whether the capture "
                    "contains full ATT traffic.",
                    file=sys.stderr,
                )
        else:
            with private_output(args.output) as stream:
                recorder = Recorder(stream, args.action)
                try:
                    asyncio.run(
                        scan(args, recorder)
                        if args.action == "scan"
                        else capture(args, recorder)
                    )
                except BaseException:
                    recorder.emit("error")
                    raise
                recorder.emit("done")
            print(f"Private capture saved to {args.output}", file=sys.stderr)
    except Exception as error:
        # CLI boundary: BLE and protocol exceptions need a concise local error too.
        print(f"Error ({type(error).__name__}): {error}", file=sys.stderr)
        if recorder is not None:
            print(f"Partial private capture retained at {args.output}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Stopped; any partial capture is retained", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
