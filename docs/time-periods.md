# Power 2000 Electricity Price Periods

The **Electricity price time periods** sensor displays the number of scheduled periods configured on the station. Its `periods` attribute details the full schedule, while the `timezone_offset_min` attribute reflects the station's UTC offset in minutes (when reported). If the station fails to report a valid schedule, the sensor becomes unavailable. This sensor and its schedule updates are not supported on other station models.

Open **Settings → Devices & services → DJI Power → Configure → Electricity price periods** to edit the schedule directly. The editor reads the current periods from the connected station.

For automation, select your Power 2000 under **DJI Power: Set electricity price time periods**. Under **Time periods**, click **Add** to set the peak/off-peak rate, start/end times, and optional weekdays. Use the edit or delete buttons on each row to modify the schedule. To apply the schedule daily, leave the weekdays blank or select all seven days. Keep seconds set to `00`.

These forms define the payload sent by the automation; they do not load or immediately alter the station's schedule. Each run replaces all peak and off-peak periods on the selected station. To load and edit the current schedule directly, use **Configure** above.

This action is also available under **Developer Tools → Actions** or in YAML:

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
Enclose start and end times in quotes using the 24-hour `HH:MM` format. The time picker's `HH:MM:00` format is also supported, provided seconds are set to zero. Times are evaluated in the **station's local time zone**, which may differ from Home Assistant's. This action will not modify the station's time zone or apply Home Assistant's daylight saving rules.
- If the end time is earlier than the start time, the period spans midnight starting on each selected day. Identical start and end times as well as overlapping periods—including overlaps across midnight or the Sunday–Monday boundary—are not allowed. Adjacent periods are permitted.
- Supports up to eight peak and eight off-peak periods.
To clear the schedule, use **Configure** or explicitly set `periods: []` in the YAML. Simply deleting the last row in the automation form may remove the required field instead of sending an empty list. Before clearing, ensure that Scheduled Periods and all grid-tied modes are disabled; you can switch modes in the DJI Home app.

This schedule is shared between Scheduled Periods and TOU modes.
