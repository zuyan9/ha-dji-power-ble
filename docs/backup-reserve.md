# Custom backup reserve level

Power 1000, Power 1000 V2, Power 2000, and Power Auro 2000 Elite expose DJI Home's
**Custom Backup Reserve Level** setting as two configuration entities:

| Entity | Behavior |
| --- | --- |
| Custom backup reserve level | When on, the station recharges only from solar while the battery is at or above the reserve level. Below it, AC, solar, and car recharging can be used together. |
| Backup reserve level | The reserve level in whole percent. Available while the custom reserve is on. |

DJI Home shows the setting only while it is offered, on every model. The entities are
created the first time the station reports it, then remain and are unavailable while it
is not reported.

As in DJI Home, the level ranges from 5 % above the discharge limit up to the recharge
limit; with a 5 % discharge limit and a 90 % recharge limit, that is 10–90 %. The
range follows later limit changes. The station does not check the level itself, so the
integration reads fresh limits and rejects values outside the range before writing.

The discharge and recharge limit sliders use the station's reported minimum and
maximum values. Changing either limit also adjusts a reported stored reserve to the
new limits in the same write, even while the custom reserve is off or unavailable.
The integration confirms both settings before reporting success.

Direct reserve changes read the current setting, write only the requested field,
require a successful acknowledgement, and then read the setting again to confirm it. The
existing **Energy reserve** diagnostic sensor continues to report the level.

While its phone is online, DJI Home saves settings through the DJI cloud, which may
later restore the app's value on a cloud-connected station. Change the setting one place
at a time.

Power 1000 Mini does not support this.
