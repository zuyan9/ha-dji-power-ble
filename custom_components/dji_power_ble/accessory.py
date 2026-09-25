"""Discovery and identities for reported port switches and SDC accessories."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import DjiPowerEntity
from .features import ModelFeature, supports_feature

AccessoryIdentity = tuple[int, int, int]
PortIdentity = tuple[int, int]
CAR_CHARGER_TYPES = {3, 4}
SDC_INTERFACE_TYPES = {5, 6}
USB_INTERFACE_TYPES = {3, 4}
# DJI Home's names for the accessory types an SDC port reports.
SDC_ACCESSORY_NAMES = {
    1: "car_power_outlet_cable",
    2: "solar_panel_adapter",
    3: "car_charger_1kw",
    4: "solar_car_charger_1_8kw",
    5: "poe_cable",
}
ACCESSORY_INPUT_FORMS = ("solar", "car", "grid")


def port_name(interface_type: int, seq: int) -> str:
    """Return the station label for an SDC-family port."""
    return f"{'SDC' if interface_type == 5 else 'SDC Lite'} {seq}"


def reported_sdc_accessories(data: dict) -> dict[PortIdentity, dict]:
    """Return unambiguous SDC-family ports that report a recognized accessory."""
    result: dict[PortIdentity, dict] = {}
    duplicates = set()
    interfaces = data.get("interfaces")
    for item in interfaces if isinstance(interfaces, list) else ():
        if not isinstance(item, dict):
            continue
        interface_type, seq = item.get("type"), item.get("seq")
        accessory_type = item.get("accessory_type")
        if (
            type(interface_type) is not int
            or interface_type not in SDC_INTERFACE_TYPES
            or type(seq) is not int
            or type(accessory_type) is not int
            or accessory_type not in SDC_ACCESSORY_NAMES
        ):
            continue
        identity = interface_type, seq
        if identity in result:
            duplicates.add(identity)
        result[identity] = item
    return {key: value for key, value in result.items() if key not in duplicates}


def accessory_inputs(port: dict) -> dict[tuple[str, int], dict]:
    """Number each accessory input within its kind, in the station's row order.

    Rows carry no connector identity. The station lists a two-input charger's
    dedicated solar input before its shared car/solar input.
    """
    slots: dict[tuple[str, int], dict] = {}
    counts: dict[str, int] = {}
    rows = port.get("accessory_inputs")
    for row in rows if isinstance(rows, list) else ():
        form = row.get("form_name") if isinstance(row, dict) else None
        if form not in ACCESSORY_INPUT_FORMS:
            continue
        counts[form] = counts.get(form, 0) + 1
        slots[form, counts[form]] = row
    return slots


def accessory_firmware(data: dict, identity: PortIdentity) -> str | None:
    """Match accessory-list firmware to a port by type, then by port order."""
    ports = reported_sdc_accessories(data)
    rows = data.get("accessories")
    if identity not in ports or not isinstance(rows, list):
        return None
    accessory_type = ports[identity]["accessory_type"]
    same_ports = [
        key for key, port in ports.items() if port["accessory_type"] == accessory_type
    ]
    same_rows = [
        row
        for row in rows
        if isinstance(row, dict) and row.get("type") == accessory_type
    ]
    if len(same_ports) != len(same_rows):
        return None
    firmware = same_rows[same_ports.index(identity)].get("firmware")
    return firmware if isinstance(firmware, str) else None


def reported_rows(
    data: dict, key: str, interface_types: set[int] = SDC_INTERFACE_TYPES
) -> dict[AccessoryIdentity, dict]:
    """Return unambiguous rows with supported, fully reported identities."""
    rows = data.get(key)
    if not isinstance(rows, list):
        return {}
    result = {}
    duplicates = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        interface_type = row.get("interface_type" if key == "car_chargers" else "type")
        seq = row.get("seq")
        accessory_type = row.get("type") if key == "car_chargers" else 0
        if (
            type(interface_type) is not int
            or interface_type not in interface_types
            or type(seq) is not int
            or not 0 <= seq <= 255
            or type(accessory_type) is not int
            or (key == "car_chargers" and accessory_type not in CAR_CHARGER_TYPES)
        ):
            continue
        identity = interface_type, seq, accessory_type
        if identity in result:
            duplicates.add(identity)
        result[identity] = row
    return {key: value for key, value in result.items() if key not in duplicates}


def async_discover_accessories(
    coordinator,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    factories: dict[str, Callable[[AccessoryIdentity], list[DjiPowerEntity]]],
    *,
    feature: ModelFeature = ModelFeature.SDC_CONTROLS,
    interface_types: set[int] = SDC_INTERFACE_TYPES,
) -> None:
    """Add each reported accessory once, including after setup or reconnection."""
    if not supports_feature(coordinator.device.model, feature):
        return
    seen: set[tuple[str, AccessoryIdentity]] = set()

    @callback
    def discover() -> None:
        if not coordinator.last_update_success:
            return
        entities = []
        for key, factory in factories.items():
            rows = reported_rows(coordinator.data or {}, key, interface_types)
            for identity, row in rows.items():
                state = row.get("sw")
                if type(state) is not int or state not in (1, 2):
                    continue
                token = key, identity
                if token not in seen:
                    seen.add(token)
                    entities.extend(factory(identity))
        if entities:
            async_add_entities(entities)

    entry.async_on_unload(coordinator.async_add_listener(discover))
    discover()


class DjiPowerAccessoryEntity(DjiPowerEntity):
    """A station entity tied to a reported interface and accessory identity."""

    _row_key: str
    _interface_types: set[int] = SDC_INTERFACE_TYPES

    def __init__(
        self, coordinator, identity: AccessoryIdentity, key: str, name: str
    ) -> None:
        super().__init__(coordinator)
        self._identity = identity
        interface_type, seq, accessory_type = identity
        self._attr_name = f"{port_name(interface_type, seq)} {name}"
        self._attr_unique_id = (
            f"{coordinator.entry.data[CONF_ADDRESS]}_"
            f"{interface_type}_{seq}_{accessory_type}_{key}"
        )

    @property
    def row(self) -> dict[str, Any] | None:
        return reported_rows(
            self.coordinator.data or {}, self._row_key, self._interface_types
        ).get(self._identity)

    @property
    def available(self) -> bool:
        return super().available and self.row is not None


class DjiPowerCarChargerEntity(DjiPowerAccessoryEntity):
    """A control on a supported car recharger attached to an SDC port."""

    _row_key = "car_chargers"

    @property
    def charger_enabled(self) -> bool:
        value = (self.row or {}).get("sw")
        return type(value) is int and value == 1

    async def async_set_charger(self, **values: Any) -> None:
        await self.coordinator.async_set_car_charger(*self._identity, **values)
