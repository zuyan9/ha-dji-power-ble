"""Model eligibility for optional controls, separate from live availability."""

from enum import StrEnum

from .duml import MODEL_NAMES


class ModelFeature(StrEnum):
    """Features that require explicit model validation."""

    TARIFF_SCHEDULE = "tariff_schedule"
    TOU_POWER_CONTROL = "tou_power_control"
    SDC_CONTROLS = "sdc_controls"
    USB_CONTROLS = "usb_controls"
    RESERVE_CONTROL = "reserve_control"


# DJI Home handles the Power Auro 2000 Elite with the Power 2000's profile.
_POWER_2000_MODELS = frozenset({"DJI Power 2000", "DJI Power Auro 2000 Elite"})

_FEATURE_MODELS = {
    ModelFeature.TARIFF_SCHEDULE: _POWER_2000_MODELS,
    ModelFeature.TOU_POWER_CONTROL: _POWER_2000_MODELS,
    ModelFeature.SDC_CONTROLS: frozenset(
        {"DJI Power 1000", "DJI Power 1000 V2", *_POWER_2000_MODELS}
    ),
    # As in DJI Home, the station's rules and switch list decide USB switches.
    ModelFeature.USB_CONTROLS: frozenset(MODEL_NAMES.values()),
    ModelFeature.RESERVE_CONTROL: frozenset(
        {"DJI Power 1000", "DJI Power 1000 V2", *_POWER_2000_MODELS}
    ),
}


def supports_feature(model: str, feature: ModelFeature) -> bool:
    """Return whether the model is eligible for a supported feature."""
    return model in _FEATURE_MODELS[feature]
