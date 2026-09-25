"""Model eligibility for optional controls, separate from live availability."""

from enum import StrEnum


class ModelFeature(StrEnum):
    """Features that require explicit model validation."""

    TARIFF_SCHEDULE = "tariff_schedule"
    TOU_POWER_CONTROL = "tou_power_control"
    SDC_CONTROLS = "sdc_controls"
    USB_CONTROLS = "usb_controls"
    RESERVE_CONTROL = "reserve_control"


_FEATURE_MODELS = {
    ModelFeature.TARIFF_SCHEDULE: frozenset({"DJI Power 2000"}),
    ModelFeature.TOU_POWER_CONTROL: frozenset({"DJI Power 2000"}),
    ModelFeature.SDC_CONTROLS: frozenset(
        {"DJI Power 1000", "DJI Power 1000 V2", "DJI Power 2000"}
    ),
    ModelFeature.USB_CONTROLS: frozenset({"DJI Power 1000 Mini"}),
    ModelFeature.RESERVE_CONTROL: frozenset({"DJI Power 1000"}),
}


def supports_feature(model: str, feature: ModelFeature) -> bool:
    """Return whether the model is eligible for a supported feature."""
    return model in _FEATURE_MODELS[feature]
