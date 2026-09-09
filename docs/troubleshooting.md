# Troubleshooting

Use the same workflow across models for discovery, authentication, missing readings,
and control problems. Start with HA logs; collect BLE traffic when logs are insufficient.

| Where the problem occurs | Use |
| --- | --- |
| Home Assistant, including ESPHome proxies | HA debug logs |
| Direct Bluetooth discovery or communication | `scan`, then `capture` |
| DJI Home, or comparing app behavior | Android HCI capture, then `decode` |

## Home Assistant logs

Close DJI Home. In **Settings → Devices & services → DJI Power**, enable debug logging,
reproduce once, then disable logging and download the log. Temporarily disable the
entry if it keeps reloading. See [HA's logging instructions](https://www.home-assistant.io/docs/configuration/troubleshooting/#debug-logs-and-diagnostics).

If setup fails before that menu is available, merge this into `configuration.yaml`
and restart HA. Remove it and restart again after collecting logs:

```yaml
logger:
  logs:
    custom_components.dji_power_ble: debug
```

For authentication, `DJI Power auth response` lines include the stage, status byte,
and payload length without credentials or identifiers. `start_bind` precedes sending
the key; `check_secret_key` checks it. These observations do not establish the cause
of failure across all firmware versions. Restart HA after updating integration files
to get newly added logging.

## Local BLE tools

[`scripts/dji_power_debug.py`](../scripts/dji_power_debug.py) shares the integration's
codec and session implementation. Keep `scripts/` and `custom_components/` together.
Offline decoding needs only Python 3.11+; scanning/capture also need BLE dependencies:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install "bleak>=3.0" "bleak-retry-connector>=3.5" "cryptography>=44.0.0"
```

On Windows, use `.venv\Scripts\python.exe`. The CLI needs a local Bluetooth adapter;
it cannot use ESPHome proxies or inspect HA's discovery cache.

Disable the station's HA entry, close DJI Home, and keep the station nearby:

```bash
.venv/bin/python scripts/dji_power_debug.py scan --output artifacts/scan.jsonl
.venv/bin/python scripts/dji_power_debug.py capture \
  --address DEVICE_IDENTIFIER --output artifacts/station.jsonl
python3 scripts/dji_power_debug.py decode artifacts/station.jsonl \
  --output artifacts/report.jsonl
```

Use the identifier printed by `scan` (a UUID on macOS). `capture` prompts for an
existing pair key, authenticates, reads configuration, and listens for 30 seconds.
It does not change settings, link accounts, or update firmware. Failures retain partial
captures. Re-enable HA afterward. Run any command with `--help` for options.

Capture is not filtered by known model or command IDs; unknown payloads remain
available for inspection. Live sessions still depend on the currently implemented
protocol. The decoder reports checksum-valid DUML frames, not every Bluetooth packet.

## DJI Home captures

On Android, disable the HA entry, enable **Bluetooth HCI snoop log** in Developer
options, restart Bluetooth, and reproduce once in DJI Home. For a BLE-only comparison,
disable Wi-Fi/mobile data and confirm fresh readings still arrive.

Export the full `btsnoop_hci.log` using the phone's bug-report facility, then disable
HCI logging. Availability varies by phone; see [Android's instructions](https://source.android.com/docs/core/connect/bluetooth/verifying_debugging#debugging-with-logs).
Keep the bug report private. Extract the HCI file locally and pass it to the same
`decode` command. Supported inputs are CLI JSONL and H4 btsnoop v1/link type 1002;
PCAP, ZIP files, and enhanced ATT are not supported. An empty report does not prove
the device sent nothing.

## Inspect and share

Default reports contain metadata, not names, addresses, credentials, or payloads.
Encrypted authentication responses are labeled `status: encrypted`; their first wire
byte is ciphertext and cannot be interpreted as an authentication status. Capture uses
the advertised model to select the same transport handling as the integration.
For private payload inspection, add `--raw`; optionally filter with `--command 0x66`
(HMS), `0x61` (reports), or `0x62` (configuration). Known credential-bearing auth
requests remain suppressed even in raw mode.

Keep raw captures, raw reports, and complete HA logs private. Review excerpts before
sharing. Include the model, station/dongle firmware, integration/HA versions, adapter
or proxy, reproduction steps, and expected versus actual behavior. Capture one station
at a time. Output files are never overwritten and use owner-only permissions on POSIX;
the examples keep them in git-ignored `artifacts/`.
