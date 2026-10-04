# Energy Saver

DJI Home's **Energy Optimization** page selects how a station uses its battery
with the grid. When a station reports the page (its eco record is available and it
sets station rule 5), the integration exposes these entities:

| Entity | Shown | Behavior |
| --- | --- | --- |
| Energy saver mode | With the page | Disabled, Scheduled periods, Max self-consumption or Time of use (TOU). |
| Discharge during peak periods | While Scheduled periods is selected | DJI Home's Discharge During Peak Periods. Turning it on needs price periods. |
| Charge during off-peak periods | While Scheduled periods is selected | DJI Home's Charge During Off-Peak Periods. Turning it on needs price periods. |
| Off-peak charging power | While Scheduled periods is selected | Watts within the station's reported limits, in 10 W steps like DJI Home. |
| Energy saver auto resume | While a grid-tied mode is selected, with station rule 17 | Resumes Energy Saver when the AC port reconnects to the grid. |
| Meter phase | While the linked smart meter steers the station (diagnostic) | The phase DJI Home set for the meter: A, B, C, A+B or A+B+C. |
| Power adjustment, Recharge power, Discharge power | While Time of use is selected | See [Time of Use power](protocol.md#energy-saver). |

Entities are created the first time the station reports what they need, then remain
and are unavailable while it is not reported. Discovery runs after connection setup,
and the integration re-reads the page every 30 seconds, so changes made in DJI Home
appear within that time.

## Selecting a mode

**Disabled** and **Scheduled periods** are always offered. Scheduled periods and
Time of use need at least one [electricity price period](time-periods.md); set the
periods first.

The grid-tied modes, **Max self-consumption** and **Time of use**, appear only after
grid-tied operation was set up in DJI Home:

- the station sets station rule 6, which DJI Home requires for grid-tied modes;
- the station's stored grid-tied selection is Max self-consumption or Time of use,
  which DJI Home stores only when a grid-tied mode is selected or a meter is bound;
  it is kept while another mode is selected, so the integration can switch back; and
- Max self-consumption also needs a smart meter linked in DJI Home.

The meter steers the station in Max self-consumption, and in Time of use with
Automatic power adjustment; the **Meter phase** sensor is available then.

The integration never sets up grid-tied operation, links or configures a meter, or
changes the meter phase. Do these in DJI Home. DJI Home also checks an online list
of countries where grid-tied modes are allowed; the integration cannot check it and
relies on the earlier DJI Home setup.

Before each grid-tied selection, DJI Home reminds you that the AC input port must be
secured with screws, as described in DJI's Grid-Tied Installation Manual. Home
Assistant cannot show that reminder. Only select or automate grid-tied modes on an
installation that DJI Home has set up for them.

DJI Home itself switches to a grid-tied mode only while it reaches the station
through DJI's cloud, which it uses for status notifications; over Bluetooth it
refuses. The integration writes over Bluetooth, which DJI Home never does for these
modes, so a station could refuse; the integration then reports that the station did
not apply the change. Smart-meter readings likely reach the station through the
cloud as well, so keep the station online while a grid-tied mode is selected.

The station's active mode is always listed. If the station reports a mode that DJI
Home does not label, the selector shows no option, and choosing one replaces it, as
in DJI Home.

## Auto resume

As DJI Home explains, Energy Saver becomes unavailable when the AC port is
disconnected from the grid. With **Energy saver auto resume** on, it resumes when the
AC port reconnects. Enable it only while the station is connected to the household
electrical network: DJI Home warns that moving the station between other grids, such
as RVs or microgrids, with the setting on can damage it. DJI Home asks you to
confirm that warning before turning the setting on; Home Assistant cannot, so turn it
on only after reading it. As in DJI Home, the switch is available only while a
grid-tied mode is selected.

## Writes

Every change reads the station's current Energy Saver record and rules, changes only
the requested field, and preserves every other setting. Selecting a mode changes only
the mode and, for grid-tied modes, the grid-tied selection, as DJI Home does. The
integration reports success only after the station reads back the new value.

These controls follow DJI Home 1.6.9 and have not yet been tested on a station.
