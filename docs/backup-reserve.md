# Custom backup reserve level

Power 1000 exposes DJI Home's **Custom Backup Reserve Level** setting as two
configuration entities:

| Entity | Behavior |
| --- | --- |
| Custom backup reserve level | When on, the station recharges only from solar while the battery is at or above the reserve level. Below it, AC, solar, and car recharging can be used together. |
| Backup reserve level | The reserve level in whole percent. Available while the custom reserve is on. |

Both entities are available only while the station reports the setting as available.
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

Other models do not create these entities until their behavior is validated.
