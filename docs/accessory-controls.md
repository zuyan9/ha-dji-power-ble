# SDC, car-charger, and USB output controls

These controls use the station's existing Bluetooth connection. SDC and car-charger
controls are enabled on Power 1000, Power 1000 V2, and Power 2000; USB output
switches are enabled on Power 1000 Mini. They appear when the station reports
a supported accessory or switch record.

## Car chargers

The integration recognizes the 1 kW Car Charger and 1.8 kW Solar/Car Charger.
Each reported charger adds controls to its station's device, named by its SDC or
SDC Lite port:

| Control | Behavior |
| --- | --- |
| Car recharging | Enables or disables the charger's car-charging function. |
| Car recharging mode | Auto, Recharge (car to station), or Charge (station to car). |
| Car recharge power | Sets car-to-station power in watts. |
| Minimum car recharging voltage | Sets the vehicle voltage threshold for car-to-station charging. |

The mode selector is available while car recharging is enabled. Power and minimum
voltage are available while enabled in **Recharge** mode, with valid bounds reported
by the station. The integration uses those bounds; it does not assume that all
accessories share a fixed wattage or voltage range. Power accepts whole watts and
voltage accepts hundredths of a volt.

Each write changes only the addressed field in the reported configuration, preserving
other settings and charger records in the payload. Selecting a mode does not rewrite
its power or voltage settings. Reverse-charge power/voltage and the Auto-mode voltage
threshold remain configurable through DJI Home.

## SDC switches

An **SDC power** or **SDC Lite power** switch appears for each port with an explicit
supported switch record. SDC power readings alone do not establish switch support.
The port switch and the car-recharging switch are separate settings.

## USB outputs

On Power 1000 Mini, a **USB-A1 output**, **USB-A2 output**, **USB-C1 output**, or
**USB-C2 output** switch appears for each USB port with an explicit supported switch
record. Other models do not create USB switches.

AC, SDC, and USB writes retain the complete reported switch list and change only the
addressed switch.

## Discovery and confirmation

Accessory discovery runs after connection setup and refreshes every 30 seconds,
alongside expansion-pack refreshes where supported. Device pushes can update the
controls sooner.
Newly attached accessories are discovered automatically. Missing, malformed, or failed
snapshots make the affected controls unavailable. Disconnection also makes them
unavailable; reconnecting or reattaching the same reported accessory restores them.

Before each write, the integration reads the current configuration and checks the
target's identity, mode, and applicable bounds. It requires a successful acknowledgement
for every written key, then reads the configuration again to confirm the requested
value. An acknowledgement alone is insufficient. Confirmed values publish immediately,
independently of the normal Home Assistant update interval.

For hardware validation, compare the discovered controls and limits with DJI Home,
change one setting at a time within the reported range, and verify both readback and
actual charging or switching behavior. Also check that other ports and charger
settings remain unchanged. The station accepts only one Bluetooth client at a time;
use DJI Home over Wi-Fi, or alternate its Bluetooth session with the HA integration.
Keep captures and diagnostics private.
