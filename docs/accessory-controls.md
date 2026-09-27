# Port switches and SDC accessory controls

These use the existing Bluetooth connection on every supported station model,
including the Mini. Controls appear from the station's reported rows, availability
and rules; a model name alone does not establish support. Recognized accessory and
charger types remain the ones listed below.

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

Recognized accessories:
- DJI Power Car Power Outlet to SDC Power Cable (12V/24V)
- DJI Power Solar Panel Adapter Module (MPPT)
- 1kW Car Charger
- 1.8kW Solar/Car Charger
- SDC to PoE Power Cable

DJI lists the car power outlet cable, solar panel adapter, and both chargers as
incompatible with the Power Auro 2000 Elite, whose solar and car inputs are built in.
Its SDC switches and total SDC readings do not depend on these accessories.

Inputs are numbered within their kind in the station's order: the 1.8 kW charger lists
its dedicated solar input before its shared Car/Solar input. Input sensors are added 
when the station first reports that input. While the accessory stays attached, an input
that carries no power reads 0 W and its voltage is unknown. All accessory sensors 
become unavailable when the accessory is removed.

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
| Car charging power | Sets station-to-car power in watts. |
| Car charging voltage | Sets the vehicle voltage for station-to-car charging, which DJI Home calls **Voltage of Car Charging**. |
| Car auto switching voltage | In Auto, the station charges the car when the vehicle voltage is at or below this value, and recharges from the car above it. |

The mode selector is available while car recharging is enabled. Like DJI Home, each
number is available only in the modes that use it:

| Mode | Available numbers |
| --- | --- |
| Recharge | Car recharge power, minimum car recharging voltage |
| Charge | Car charging power, car charging voltage |
| Auto | Both powers and the auto switching voltage, or both powers and both voltages |

In Auto, the station's rules choose between the two layouts, as they do in DJI Home.
The integration reads the rules with the charger settings and again before a
write. If a refresh cannot read the rules, Auto displays only the two powers; a
write still requires a successful fresh rules read.

A number also needs valid bounds reported by the station. The integration uses those
bounds; it does not assume that all accessories share a fixed wattage or voltage range.
Power accepts whole watts and voltage accepts hundredths of a volt.

Each write changes only the addressed field in the reported configuration, preserving
other settings and charger records in the payload. Selecting a mode does not rewrite
its power or voltage settings.

## SDC switches

An **SDC power** or **SDC Lite power** switch appears for each port with an explicit
supported switch record and station rule 11. SDC power readings alone do not
establish switch support. This follows DJI Home: a station without rule 11 no longer
offers SDC switches, even if an earlier integration version created them.
The port switch and the car-recharging switch are separate settings. The original
Power 1000 reports only its AC switch, so it has no SDC power switch.

## USB outputs

A **USB-A1 output**, **USB-A2 output**, **USB-C1 output**, or **USB-C2 output**
switch appears for each USB port with an explicit supported switch record, on any
station whose rules include rule 11. DJI Home uses the same two conditions for its
USB toggles. A station that reports USB rows without rule 11 gets no USB switches, and
existing USB switches become unavailable while the rules are unknown.

## AC and car outlets

AC outputs and car outlets (interface type 7) appear when the station reports a
valid switch row, without requiring rule 11. The existing main **AC output** entity
keeps its identity; additional outputs use their reported port sequences.

All port writes read fresh switch rows and rules under the same operation lock.
With rule 21, the SET includes the complete list; otherwise it includes only the
addressed row. The selected row's extra bytes are preserved in both cases, and
full-list writes also preserve every other row. Missing rules in a complete reply
mean clear bits; a failed or malformed rules read prevents the write.

## Discovery and confirmation

Accessory discovery runs after connection setup and refreshes every 30 seconds,
alongside expansion-pack refreshes where supported. The refresh reads the charger
settings, the switch list, the accessory list, reserve and station rules on every
supported model. Telemetry reports update the
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
data to identify the model when Home Assistant has no advertisement available. A Power
Auro 2000 Elite set up with an earlier version, shown as **DJI Power (0x9E)**, is
renamed automatically at its next setup.

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
