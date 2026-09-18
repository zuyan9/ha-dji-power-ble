# Power 2000 Electricity Price Periods

The **Electricity price time periods** sensor displays the number of scheduled periods configured on the station. Its `periods` attribute details the full schedule, while the `timezone_offset_min` attribute reflects the station's UTC offset in minutes (when reported). If the station fails to report a valid schedule, the sensor becomes unavailable. This sensor and its schedule updates are not supported on other station models.

Open **Settings → Devices & services → DJI Power → Configure → Electricity price periods** to edit the schedule directly. The editor reads the current periods from the connected station.

For automations, use the **DJI Power: Set electricity price time periods** action (also available under Developer Tools). Select your Power 2000 in the UI editor, or specify its Home Assistant device ID in YAML:

```yaml
action: dji_power_ble.set_time_periods
data:
  device_id: YOUR_POWER_2000_DEVICE_ID
  periods:
    - type: off_peak
      start: "00:30"
      end: "05:30"
    - type: peak
      days: [mon, tue, wed, thu, fri]
      start: "16:00"
      end: "20:00"
```

- Each call **overwrites all** existing peak and off-peak periods. Since this action replaces rather than appends to the schedule, be sure to include any periods you want to keep.
- `type` must be either `peak` or `off_peak`. Omit `days` to apply the period daily, or provide a list using `mon`, `tue`, `wed`, `thu`, `fri`, `sat`, and `sun`.
- Wrap start and end times in quotes using the 24-hour `HH:MM` format. Times are based on the **station's local time zone**, which may differ from Home Assistant's. This action does not adjust the station's time zone or apply Home Assistant's daylight saving rules.
- If the end time is earlier than the start time, the period spans midnight starting on each selected day. Identical start and end times as well as overlapping periods—including overlaps across midnight or the Sunday–Monday boundary—are not allowed. Adjacent periods are permitted.
- Supports up to eight peak and eight off-peak periods.
- Setting `periods: []` clears the schedule, but only if Scheduled Periods and all grid-tied modes are disabled. Switch modes in the DJI Home app before removing the last period.

This schedule is shared between Scheduled Periods and TOU modes.
