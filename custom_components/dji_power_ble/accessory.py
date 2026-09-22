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
CAR_CHARGER_TYPES = {3, 4}
SDC_INTERFACE_TYPES = {5, 6}
USB_INTERFACE_TYPES = {3, 4}


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
        port = "SDC" if interface_type == 5 else "SDC Lite"
        self._attr_name = f"{port} {seq} {name}"
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
