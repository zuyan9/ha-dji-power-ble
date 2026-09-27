"""Known station models and capability predicates shared by controls."""

from enum import StrEnum

from .duml import MODEL_NAMES, PORT_SWITCH_RULE


class ModelFeature(StrEnum):
    """Optional features whose availability comes from station records."""

    TARIFF_SCHEDULE = "tariff_schedule"
    TOU_POWER_CONTROL = "tou_power_control"
    SDC_CONTROLS = "sdc_controls"
    USB_CONTROLS = "usb_controls"
    RESERVE_CONTROL = "reserve_control"


_KNOWN_MODELS = frozenset(MODEL_NAMES.values())


def is_known_model(model: str) -> bool:
    """Return whether discovery has identified a supported station model."""
    return model in _KNOWN_MODELS


def supports_feature(model: str, feature: ModelFeature) -> bool:
    """Allow known models to discover a feature, subject to station capability."""
    return is_known_model(model) and feature in ModelFeature


def station_rule_enabled(data: dict, rule: int) -> bool:
    """Return whether a valid station rules snapshot explicitly sets a rule."""
    rules = data.get("station_rules")
    return (
        isinstance(rules, list)
        and all(type(value) is int and value >= 0 for value in rules)
        and rule in rules
    )


def feature_available(data: dict, feature: ModelFeature) -> bool:
    """Apply the known record and station-rule gates for an optional feature."""
    if feature == ModelFeature.RESERVE_CONTROL:
        return data.get("energy_reserve_available") is True
    if feature == ModelFeature.TARIFF_SCHEDULE:
        return (
            isinstance(data.get("time_periods"), list)
            and data.get("eco_available") is True
            and station_rule_enabled(data, 5)
        )
    if feature == ModelFeature.TOU_POWER_CONTROL:
        return (
            data.get("eco_available") is True
            and station_rule_enabled(data, 5)
            and station_rule_enabled(data, 6)
            and data.get("power_adjustment") in ("Manual", "Automatic")
        )
    if feature == ModelFeature.USB_CONTROLS:
        return any(port[0] in (3, 4) for port in eligible_port_switches(data))
    if feature == ModelFeature.SDC_CONTROLS:
        return any(port[0] in (5, 6) for port in eligible_port_switches(data))
    return False


def eligible_port_switches(data: dict) -> dict[tuple[int, int], dict]:
    """Return unambiguous offered output switches from the reported port list.

    AC and car outlets need a valid row. USB and SDC additionally require the
    station's rule 11; present watts are not a capability signal.
    """
    rows = data.get("power_switches")
    if not isinstance(rows, list):
        return {}
    result: dict[tuple[int, int], dict] = {}
    seen: set[tuple[int, int]] = set()
    duplicates: set[tuple[int, int]] = set()
    offered = station_rule_enabled(data, PORT_SWITCH_RULE)
    for row in rows:
        if not isinstance(row, dict):
            continue
        interface_type, seq = row.get("type"), row.get("seq")
        if (
            type(interface_type) is not int
            or interface_type not in (2, 3, 4, 5, 6, 7)
            or type(seq) is not int
            or not 0 <= seq <= 255
        ):
            continue
        identity = interface_type, seq
        if identity in seen:
            duplicates.add(identity)
        seen.add(identity)
        if type(row.get("sw")) is not int or row["sw"] not in (1, 2):
            continue
        if interface_type in (2, 7) or offered:
            result[identity] = row
    return {key: value for key, value in result.items() if key not in duplicates}
