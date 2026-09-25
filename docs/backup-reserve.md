# Custom backup reserve level

Power 1000, Power 1000 V2, and Power 2000 expose DJI Home's **Custom Backup Reserve
Level** setting as two configuration entities:

| Entity | Behavior |
| --- | --- |
| Custom backup reserve level | When on, the station recharges only from solar while the battery is at or above the reserve level. Below it, AC, solar, and car recharging can be used together. |
| Backup reserve level | The reserve level in whole percent. Available while the custom reserve is on. |

The station offers the setting while a solar-capable SDC accessory is attached: on
Power 1000 firmware, a Solar Panel Adapter Module or a 1.8 kW Solar/Car Charger. DJI
Home shows the setting only while it is offered, on every model. The entities are
created the first time the station offers it, then remain and are unavailable while it
is not offered. Power 1000 V2 and Power 2000 use the same setting record, but their
handling has not been hardware-tested with a solar accessory.

As in DJI Home, the level ranges from 5 % above the discharge limit up to the recharge
limit; with a 5 % discharge limit and a 90 % recharge limit, that is 10–90 %. The
range follows later limit changes. The station does not check the level itself, so the
integration reads fresh limits and rejects values outside the range before writing.

Each change reads the current setting, writes only the requested field, requires a
successful acknowledgement, and then reads the setting again to confirm it. The
existing **Energy reserve** diagnostic sensor continues to report the level.

While its phone is online, DJI Home saves settings through the DJI cloud, which may
later restore the app's value on a cloud-connected station. Change this setting in
one place at a time.

Power 1000 Mini does not create these entities.
