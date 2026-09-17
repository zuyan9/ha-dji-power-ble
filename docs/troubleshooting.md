# Troubleshooting

The same workflow should work across hardware models for discovery, authentication, missing readings,
and control problems. Start with HA logs; collect BLE traffic when logs are insufficient.

| Where the problem occurs | Use |
| --- | --- |
| Home Assistant, including ESPHome proxies | HA debug logs |
| Direct Bluetooth discovery or communication | `scan`, then `capture` |
| DJI Home, or comparing app behavior | Android HCI capture, then `decode` |

## Home Assistant logs

Close DJI Home app. In HA **Settings → Devices & services → DJI Power**, enable debug logging,
reproduce once, then disable logging and download the log. Temporarily disable the
entry if it keeps reloading. See [HA's logging instructions](https://www.home-assistant.io/docs/configuration/troubleshooting/#debug-logs-and-diagnostics).

If setup fails before that menu is available, merge this into `configuration.yaml`
and restart HA. Remove it and restart again after collecting logs:

```yaml
logger:
  logs:
    custom_components.dji_power_ble: debug
```

Restart HA after updating integration files to get newly added logging.

## Keep the station connected during HA restarts and reloads

In the HA integration options, **Bluetooth connection source** defaults to
**Automatic**, which allows local adapters and ESPHome Bluetooth proxies. Selecting
a specific local adapter uses only that adapter. If it disconnects, the station
stays unavailable instead of switching to another adapter or proxy.

Selecting a local Linux Bluetooth adapter also allows
**Keep connection during HA restarts and reloads**. This attempts to preserve
the Bluetooth connection when HA Core stops, then restore notifications and verify
the existing authenticated session when HA starts. Integration reloads reuse the
live device connection when the adapter and credentials stay the same.

BlueZ and the selected built-in or USB adapter must remain running. This cannot
preserve a connection through a host reboot, Bluetooth reset, loss of range, or an
ESPHome proxy. Disabling or removing the entry still disconnects. Switching adapters
or changing credentials requires a new connection and authentication.

Changing the update interval or retention option applies immediately without
reconnecting. Turning retention off keeps the current connection open, but later
restarts and reloads disconnect normally.

Retention may help avoid fresh authentication after long station uptime; it cannot
repair a station that already rejects new authentication. If the connection is lost
and authentication fails, a full restart that resets power the station's controller
may be needed. Restarting HA alone does not reset that controller.

## Local BLE tools

[`scripts/dji_power_debug.py`](../scripts/dji_power_debug.py) shares the integration's
codec and session implementation. Keep `scripts/` and `custom_components/` together.
Offline decoding needs only Python 3.11+; scanning/capture also need BLE dependencies:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install "bleak>=3.0" "bleak-retry-connector>=3.5" "cryptography>=44.0.0"
```

The CLI needs a local Bluetooth adapter; it cannot use ESPHome proxies or inspect HA's discovery cache.

Disable the station's HA entry, close DJI Home app, and keep the station nearby:

```bash
.venv/bin/python scripts/dji_power_debug.py scan --output artifacts/scan.jsonl
.venv/bin/python scripts/dji_power_debug.py capture \
  --address DEVICE_IDENTIFIER --output artifacts/station.jsonl
python3 scripts/dji_power_debug.py decode artifacts/station.jsonl \
  --output artifacts/report.jsonl
```

Use the identifier returned by `scan` (a UUID on macOS). The `capture` command prompts 
for an existing pairing key, authenticates, reads the device configuration, and listens 
for 30 seconds. It will not modify settings, link accounts, or update firmware. If it 
fails, partial captures are retained. Remember to re-enable HA afterward. Run any 
command with `--help` to view available options.

Captures are not filtered by known model or command IDs, so unknown payloads remain 
available for inspection. However, live sessions still depend on the currently implemented 
protocol. The decoder only reports checksum-valid DUML frames, not raw Bluetooth packets.

## DJI Home app captures

Capturing Android Bluetooth HCI logs allows us to compare the integration's connection 
and authentication process with the DJI Home app. Make sure to use a station already linked to DJI Home.

1. Make sure the power station is set up in the DJI Home app. Disable its Home Assistant entry.
2. Turn Wi-Fi and cellular data off on the phone, keeping Bluetooth on. This prevents DJI Home app from using cloud.
3. Enable Android Developer options (usually tapping Build number seven times under Settings → About phone).
4. Enable **Bluetooth HCI snoop log** in Developer options. Select **Enabled** or **Full**.
   Turn Bluetooth off and back on before opening the app so logging captures. 
   Disconnect other Bluetooth devices on the phone.
5. Open DJI Home app. It should connect to the station via Bluetooth.
   It might say "Network Unavailable", that's expected.
   Navigate in the app, make sure we have some readings, we can change some power station settings too.
6. Use **Developer options → Take bug report** (choose **Full** if available). Wait for the completion notification
   and export the zip file. See [Android's bug-report guide](https://developer.android.com/studio/debug/bug-report#capture).
7. Close DJI Home, disable Bluetooth HCI logging, turn Bluetooth off and back on.
   Turn phone's WiFi and Cellular back on; re-enable the Home Assistant entry.
8. Extract the zip file and search for `btsnoop_hci.log`, often under `FS/data/misc/bluetooth/logs/`. 
   Attach this log file to the Github issue, or share it privately with the maintainer
   (since this may contain sensitive information such as pair_key and device identity).
9. To refresh the bluetooth pair key, reset the power station, set it up in DJI Home app and HA agian.

## Inspect and share

Default HA reports contain metadata, not names, addresses, credentials, or payloads.
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
