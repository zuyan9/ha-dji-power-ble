# SDC accessories, car-charger, and USB output controls

These entities use the station's existing Bluetooth connection. SDC accessory readings
and SDC and car-charger controls are enabled on Power 1000, Power 1000 V2, and Power
2000; USB output switches are enabled on Power 1000 Mini. They appear when the station
reports a supported accessory or switch record.

## Accessory information and input readings

Each accessory reported on an SDC or SDC Lite port adds sensors to the station's device,
named by its port:

| Sensor | Value |
| --- | --- |
| Accessory | Accessory type, as DJI Home names it |
| Accessory firmware | Firmware version from the station's accessory list |
| Solar 1 power, solar 1 voltage | One solar input; a second input adds solar 2 |
| Car recharge power, car charge power, car voltage | Car to station, station to car, and vehicle voltage |
| Grid power, grid voltage | Grid-connection output |

Recognized accessories are the Car Power Outlet to SDC Power Cable (12V/24V), the Solar
Panel Adapter Module (MPPT), the 1kW Car Charger, the 1.8kW Solar/Car Charger, and the
SDC to PoE Power Cable.

The station does not identify an input's physical connector. Inputs are numbered
within their kind in the station's order: the 1.8 kW charger lists its dedicated solar
input before its shared Car/Solar input. Input sensors are added when the station
first reports that input. While the accessory stays attached, an input that carries no
power reads 0 W and its voltage is unknown. All accessory sensors become unavailable
when the accessory is removed.

## Car chargers

The integration recognizes the 1 kW Car Charger and 1.8 kW Solar/Car Charger.
Each reported charger adds controls to its station's device, named by its SDC or
SDC Lite port. The station reports car settings only while the charger's Car/Solar
input detects a 12 V or 24 V vehicle system, as established from Power 1000 firmware.
With solar panels on that input, the station reports no car settings and no car
controls appear; the solar readings above remain available.

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
The port switch and the car-recharging switch are separate settings. The original
Power 1000 reports only its AC switch, so it has no SDC power switch.

## USB outputs

On Power 1000 Mini, a **USB-A1 output**, **USB-A2 output**, **USB-C1 output**, or
**USB-C2 output** switch appears for each USB port with an explicit supported switch
record. Other models do not create USB switches.

AC, SDC, and USB writes retain the complete reported switch list and change only the
addressed switch.

## Discovery and confirmation

Accessory discovery runs after connection setup and refreshes every 30 seconds,
alongside expansion-pack refreshes where supported. The refresh reads the charger
settings, the switch list, and the accessory list. Telemetry reports update the
accessory type and input readings as they arrive. Device pushes can update the
controls sooner.
Newly attached accessories are discovered automatically. Missing, malformed, or failed
snapshots make the affected controls unavailable. Disconnection also makes them
unavailable; reconnecting or reattaching the same reported accessory restores them.

If an older installation shows the station model as **DJI Power** and the controls
are missing, open the integration entry's menu in **Settings → Devices & services**,
choose **Reconfigure**, and select the model printed on the station. Saving reloads
the integration with that model and preserves the Bluetooth address, pair key, and
options. On a selected local adapter, setup also uses cached Bluetooth manufacturer
data to identify the model when Home Assistant has no advertisement available.

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
