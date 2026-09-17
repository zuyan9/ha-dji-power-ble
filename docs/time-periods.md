# Power 2000 Electricity Price Periods

The **Electricity price time periods** sensor displays the number of periods configured on the station. Its `periods` attribute contains the full schedule, while the `timezone_offset_min` attribute (when reported) reflects the station's UTC offset in minutes. The sensor becomes unavailable if the station fails to report a valid schedule. Other station models do not support this sensor or allow schedule updates.

Use the **DJI Power: Set electricity price time periods** action in Developer Tools or within an automation to overwrite the entire schedule. Select the Power 2000 device in the UI editor, or provide its Home Assistant device ID in YAML:

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

- Each call **overwrites all** existing peak and off-peak periods. Be sure to include any existing periods you want to retain, as this action does not append to the schedule.
- `type` must be either `peak` or `off_peak`. Omit `days` to apply the period daily, or specify a non-empty list using `mon`, `tue`, `wed`, `thu`, `fri`, `sat`, and `sun`.
- Wrap start and end times in quotes using 24-hour `HH:MM` format. Times are based on the **station's local time zone**, which may differ from Home Assistant's. This action will not adjust the station's time zone or apply Home Assistant's daylight saving rules.
- If the end time is earlier than the start time, the period spans midnight starting on each selected day. Identical start/end times and overlapping periods—including overlaps across midnight or the Sunday–Monday boundary—are rejected. Adjacent periods are permitted.
- Supports up to eight peak and eight off-peak periods.
- Setting `periods: []` clears the schedule, but only if Scheduled Periods and all grid-tied modes are disabled. Switch modes in the DJI Home app before removing the final period.

This schedule is shared between Scheduled Periods and TOU modes. Calling this action does not alter the active Energy Optimization mode; select your preferred mode separately in DJI Home. The integration reads current settings, transmits changes over local BLE, and verifies the station's readback before confirming success. Note that hardware validation for schedule writes on the Power 2000 is currently pending.
