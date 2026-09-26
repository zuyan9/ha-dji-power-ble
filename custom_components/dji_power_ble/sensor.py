"""Sensors from decoded telemetry (verified against the DJI cloud OSD)."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_ADDRESS,
    PERCENTAGE,
    EntityCategory,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .accessory import (
    SDC_ACCESSORY_NAMES,
    PortIdentity,
    accessory_firmware,
    accessory_inputs,
    port_name,
    reported_sdc_accessories,
)
from .const import DOMAIN
from .coordinator import DjiPowerCoordinator
from .entity import DjiPowerEntity
from .features import ModelFeature, supports_feature

DESCRIPTIONS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key="battery_percent",
        name="Battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="input_w",
        name="Input power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="output_w",
        name="Output power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="charge_input_w",
        name="Charging input",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="ac_output_w",
        name="AC output",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="dc_output_w",
        name="USB output",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="usb_a_output_w",
        name="USB-A output",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="usb_a_1_output_w",
        name="USB-A1 output",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="usb_a_2_output_w",
        name="USB-A2 output",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="usb_c_output_w",
        name="USB-C output",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="usb_c_1_output_w",
        name="USB-C1 output",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="usb_c_2_output_w",
        name="USB-C2 output",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="sdc_input_w",
        name="SDC input",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="sdc_output_w",
        name="SDC output",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="sdc_lite_input_w",
        name="SDC Lite input",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="sdc_lite_output_w",
        name="SDC Lite output",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="cigarette_output_w",
        name="12 V output",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="xt60_output_w",
        name="XT60 output",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="temperature",
        name="Battery temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="battery_cycle_count",
        translation_key="battery_cycle_count",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="primary_battery_percent",
        name="Primary battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
    ),
    SensorEntityDescription(
        key="primary_runtime_min",
        name="Primary battery runtime",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        entity_registry_enabled_default=False,
    ),
    SensorEntityDescription(
        key="energy_reserve",
        name="Energy reserve",
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    SensorEntityDescription(
        key="display_timeout_s",
        name="Display timeout",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    SensorEntityDescription(
        key="timezone_offset_min",
        name="Timezone offset",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    SensorEntityDescription(
        key="firmware",
        name="Firmware",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="firmware_secondary",
        name="Secondary firmware",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)

BATTERY_TIME_DESCRIPTIONS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key="runtime_min",
        name="Remaining Time",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
    ),
    SensorEntityDescription(
        key="recharging_time_min",
        name="Recharging Time",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
    ),
)

EXPANSION_DESCRIPTIONS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key="battery_percent",
        translation_key="expansion_battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="cycle_count",
        translation_key="expansion_cycle_count",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="rated_capacity_wh",
        translation_key="expansion_rated_capacity",
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="temperature",
        translation_key="expansion_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
    ),
)


# Per-input readings by input kind: unique-ID suffix, name, and row field.
INPUT_METRICS: dict[str, tuple[tuple[str, str, str], ...]] = {
    "solar": (
        ("power", "power", "input_w"),
        ("voltage", "voltage", "input_voltage_v"),
    ),
    "car": (
        ("recharge_power", "recharge power", "input_w"),
        ("charge_power", "charge power", "output_w"),
        ("voltage", "voltage", "voltage"),
    ),
    "grid": (
        ("power", "power", "output_w"),
        ("voltage", "voltage", "output_voltage_v"),
    ),
}


def _expansion_batteries(coordinator: DjiPowerCoordinator) -> dict[str, dict]:
    """Return the current packs with stable identities."""
    return {
        pack["serial_number"]: pack
        for pack in (coordinator.data or {}).get("expansion_batteries") or []
        if pack.get("serial_number")
    }


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    registry = er.async_get(hass)
    legacy_unique_id = f"{entry.data[CONF_ADDRESS]}_country_code"
    if entity_id := registry.async_get_entity_id("sensor", DOMAIN, legacy_unique_id):
        registry.async_remove(entity_id)

    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        DjiPowerSensor(coordinator, description) for description in DESCRIPTIONS
    )
    async_add_entities(
        DjiPowerBatteryTimeSensor(coordinator, description)
        for description in BATTERY_TIME_DESCRIPTIONS
    )
    if supports_feature(coordinator.device.model, ModelFeature.TARIFF_SCHEDULE):
        async_add_entities([DjiPowerTimePeriodsSensor(coordinator)])
    device_registry = dr.async_get(hass)
    known: set[tuple[str, str]] = set()
    restored = []
    for registered in er.async_entries_for_config_entry(registry, entry.entry_id):
        if registered.domain != "sensor" or registered.platform != DOMAIN:
            continue
        for description in EXPANSION_DESCRIPTIONS:
            prefix, suffix = "expansion_", f"_{description.key}"
            if not registered.unique_id.startswith(prefix):
                continue
            if not registered.unique_id.endswith(suffix):
                continue
            serial = registered.unique_id[len(prefix) : -len(suffix)]
            if serial:
                restored.append(
                    DjiPowerExpansionSensor(coordinator, serial, description)
                )
                known.add((serial, description.key))
    if restored:
        async_add_entities(restored)

    @callback
    def async_discover_expansion_batteries() -> None:
        """Add connected packs and refresh their optional firmware metadata."""
        entities = []
        for serial, pack in _expansion_batteries(coordinator).items():
            for description in EXPANSION_DESCRIPTIONS:
                if (serial, description.key) in known:
                    continue
                if description.key == "temperature" and pack.get("temperature") is None:
                    continue
                entities.append(
                    DjiPowerExpansionSensor(coordinator, serial, description)
                )
                known.add((serial, description.key))
            if firmware := pack.get("firmware"):
                device = device_registry.async_get_device(
                    identifiers={(DOMAIN, f"expansion_{serial}")}
                )
                if device and device.sw_version != firmware:
                    device_registry.async_update_device(device.id, sw_version=firmware)
        if entities:
            async_add_entities(entities)

    entry.async_on_unload(
        coordinator.async_add_listener(async_discover_expansion_batteries)
    )
    async_discover_expansion_batteries()

    if not supports_feature(coordinator.device.model, ModelFeature.SDC_CONTROLS):
        return
    seen: set[tuple] = set()

    @callback
    def async_discover_sdc_accessories() -> None:
        """Add each reported accessory's identity and input readings once."""
        if not coordinator.last_update_success:
            return
        entities: list[SensorEntity] = []
        ports = reported_sdc_accessories(coordinator.data or {})
        for identity, port in ports.items():
            for factory in (
                DjiPowerSdcAccessorySensor,
                DjiPowerSdcAccessoryFirmwareSensor,
            ):
                if (identity, factory) not in seen:
                    seen.add((identity, factory))
                    entities.append(factory(coordinator, identity))
            for form, ordinal in accessory_inputs(port):
                for metric in INPUT_METRICS[form]:
                    token = identity, form, ordinal, metric[0]
                    if token not in seen:
                        seen.add(token)
                        entities.append(
                            DjiPowerSdcInputSensor(
                                coordinator, identity, form, ordinal, metric
                            )
                        )
        if entities:
            async_add_entities(entities)

    entry.async_on_unload(
        coordinator.async_add_listener(async_discover_sdc_accessories)
    )
    async_discover_sdc_accessories()


class DjiPowerSensor(DjiPowerEntity, SensorEntity):
    def __init__(self, coordinator, description: SensorEntityDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = (
            f"{coordinator.entry.data[CONF_ADDRESS]}_{description.key}"
        )

    @property
    def native_value(self):
        value = (self.coordinator.data or {}).get(self.entity_description.key)
        if isinstance(value, str) and len(value) > 255:
            return value[:255]
        return value


class DjiPowerBatteryTimeSensor(DjiPowerSensor):
    """Show the reported duration only for the matching battery time category."""

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None

    @property
    def native_value(self) -> int | None:
        data = self.coordinator.data or {}
        time_types = (0, 2) if self.entity_description.key == "runtime_min" else (1,)
        if data.get("battery_time_type") not in time_types:
            return None
        return data.get("runtime_min")


class DjiPowerTimePeriodsSensor(DjiPowerEntity, SensorEntity):
    """Station tariff periods, with a count suitable for the sensor state."""

    _attr_translation_key = "time_periods"
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coordinator: DjiPowerCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = (
            f"{coordinator.entry.data[CONF_ADDRESS]}_time_periods"
        )

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None

    @property
    def native_value(self) -> int | None:
        periods = (self.coordinator.data or {}).get("time_periods")
        return len(periods) if isinstance(periods, list) else None

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        data = self.coordinator.data or {}
        attributes = {"periods": data.get("time_periods")}
        if (offset := data.get("timezone_offset_min")) is not None:
            attributes["timezone_offset_min"] = offset
        return attributes


class DjiPowerExpansionSensor(CoordinatorEntity[DjiPowerCoordinator], SensorEntity):
    """A sensor on an expansion pack sharing the station's BLE connection."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DjiPowerCoordinator,
        serial: str,
        description: SensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self._serial = serial
        self.entity_description = description
        self._attr_unique_id = f"expansion_{serial}_{description.key}"

    @property
    def device_info(self) -> DeviceInfo:
        info = DeviceInfo(
            identifiers={(DOMAIN, f"expansion_{self._serial}")},
            name=f"Expansion battery {self._serial[-4:]}",
            manufacturer="DJI",
            model="DJI Power Expansion Battery 2000",
            serial_number=self._serial,
            via_device=(DOMAIN, self.coordinator.device.address),
        )
        pack = _expansion_batteries(self.coordinator).get(self._serial, {})
        if firmware := pack.get("firmware"):
            info["sw_version"] = firmware
        return info

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None

    @property
    def native_value(self) -> float | int | None:
        pack = _expansion_batteries(self.coordinator).get(self._serial, {})
        return pack.get(self.entity_description.key)


class _DjiPowerSdcSensor(DjiPowerEntity, SensorEntity):
    """A reading for the accessory attached to one SDC-family port."""

    def __init__(
        self,
        coordinator: DjiPowerCoordinator,
        identity: PortIdentity,
        key: str,
        name: str,
    ) -> None:
        super().__init__(coordinator)
        self._identity = identity
        interface_type, seq = identity
        self._attr_name = f"{port_name(interface_type, seq)} {name}"
        self._attr_unique_id = (
            f"{coordinator.entry.data[CONF_ADDRESS]}_{interface_type}_{seq}_{key}"
        )

    @property
    def port(self) -> dict | None:
        return reported_sdc_accessories(self.coordinator.data or {}).get(
            self._identity
        )

    @property
    def available(self) -> bool:
        return super().available and self.port is not None


class DjiPowerSdcAccessorySensor(_DjiPowerSdcSensor):
    """The attached accessory, named as DJI Home names it."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_options = list(SDC_ACCESSORY_NAMES.values())
    _attr_translation_key = "sdc_accessory"

    def __init__(
        self, coordinator: DjiPowerCoordinator, identity: PortIdentity
    ) -> None:
        super().__init__(coordinator, identity, "accessory", "accessory")

    @property
    def native_value(self) -> str | None:
        port = self.port
        return SDC_ACCESSORY_NAMES.get(port["accessory_type"]) if port else None


class DjiPowerSdcAccessoryFirmwareSensor(_DjiPowerSdcSensor):
    """The attached accessory's firmware from the station's accessory list."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: DjiPowerCoordinator, identity: PortIdentity
    ) -> None:
        super().__init__(
            coordinator, identity, "accessory_firmware", "accessory firmware"
        )

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None

    @property
    def native_value(self) -> str | None:
        return accessory_firmware(self.coordinator.data or {}, self._identity)


class DjiPowerSdcInputSensor(_DjiPowerSdcSensor):
    """Power or voltage of one numbered accessory input.

    An attached accessory omits inputs that carry no power, so their power is 0.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: DjiPowerCoordinator,
        identity: PortIdentity,
        form: str,
        ordinal: int,
        metric: tuple[str, str, str],
    ) -> None:
        key, name, self._field = metric
        self._slot = form, ordinal
        label = form if ordinal == 1 and form != "solar" else f"{form} {ordinal}"
        super().__init__(
            coordinator, identity, f"{form}_{ordinal}_{key}", f"{label} {name}"
        )
        if self._field.endswith("_w"):
            self._attr_device_class = SensorDeviceClass.POWER
            self._attr_native_unit_of_measurement = UnitOfPower.WATT
        else:
            self._attr_device_class = SensorDeviceClass.VOLTAGE
            self._attr_native_unit_of_measurement = UnitOfElectricPotential.VOLT

    @property
    def native_value(self) -> float | int | None:
        port = self.port
        if port is None:
            return None
        row = accessory_inputs(port).get(self._slot)
        if row is None:
            return 0 if self._field.endswith("_w") else None
        if self._field == "voltage":
            return row.get("input_voltage_v") or row.get("output_voltage_v")
        return row.get(self._field)
