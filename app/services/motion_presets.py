from __future__ import annotations

"""Motion sensitivity presets service.

Provides named sensitivity presets (off / very_low / low / medium / high /
very_high) mapping to concrete detector parameters for
``MotionTuner`` / ``MotionDetector`` / ``MotionZoningEngine``.
"""

import copy
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Preset definitions
# Higher sensitivity == lower thresholds / smaller minimum blob areas.
# ---------------------------------------------------------------------------

_PRESETS: Dict[str, Dict[str, Any]] = {
    "off": {
        "name": "off",
        "label": "Off",
        "title": "Off",
        "description": "Motion detection disabled.",
        "sensitivity": 0,
        "threshold": 255,
        "motion_threshold": 255,
        "contour_area": 100000,
        "motion_contour_area": 100000,
        "min_contour_area": 100000,
        "min_area": 100000,
        "min_blob_area": 100000,
        "blob_min_area": 100000,
        "threshold_percentage": 100.0,
        "motion_threshold_percentage": 100.0,
        "improve_contrast": False,
        "blur_radius": 1,
        "delta_alpha": 0.2,
        "frame_alpha": 0.01,
        "lightning_threshold": 0.8,
    },
    "very_low": {
        "name": "very_low",
        "label": "Very Low",
        "title": "Very Low",
        "description": "Least sensitive; only large, obvious motion triggers.",
        "sensitivity": 15,
        "threshold": 50,
        "motion_threshold": 50,
        "contour_area": 500,
        "motion_contour_area": 500,
        "min_contour_area": 500,
        "min_area": 500,
        "min_blob_area": 500,
        "blob_min_area": 500,
        "threshold_percentage": 12.0,
        "motion_threshold_percentage": 12.0,
        "improve_contrast": False,
        "blur_radius": 2,
        "delta_alpha": 0.2,
        "frame_alpha": 0.01,
        "lightning_threshold": 0.8,
    },
    "low": {
        "name": "low",
        "label": "Low",
        "title": "Low",
        "description": "Low sensitivity; ignores small motion and noise.",
        "sensitivity": 35,
        "threshold": 40,
        "motion_threshold": 40,
        "contour_area": 200,
        "motion_contour_area": 200,
        "min_contour_area": 200,
        "min_area": 200,
        "min_blob_area": 200,
        "blob_min_area": 200,
        "threshold_percentage": 8.0,
        "motion_threshold_percentage": 8.0,
        "improve_contrast": False,
        "blur_radius": 2,
        "delta_alpha": 0.2,
        "frame_alpha": 0.01,
        "lightning_threshold": 0.8,
    },
    "medium": {
        "name": "medium",
        "label": "Medium",
        "title": "Medium",
        "description": "Balanced default sensitivity for general use.",
        "sensitivity": 60,
        "threshold": 30,
        "motion_threshold": 30,
        "contour_area": 100,
        "motion_contour_area": 100,
        "min_contour_area": 100,
        "min_area": 100,
        "min_blob_area": 100,
        "blob_min_area": 100,
        "threshold_percentage": 5.0,
        "motion_threshold_percentage": 5.0,
        "improve_contrast": True,
        "blur_radius": 1,
        "delta_alpha": 0.2,
        "frame_alpha": 0.01,
        "lightning_threshold": 0.8,
    },
    "high": {
        "name": "high",
        "label": "High",
        "title": "High",
        "description": "High sensitivity; detects smaller/fainter motion.",
        "sensitivity": 80,
        "threshold": 20,
        "motion_threshold": 20,
        "contour_area": 30,
        "motion_contour_area": 30,
        "min_contour_area": 30,
        "min_area": 30,
        "min_blob_area": 30,
        "blob_min_area": 30,
        "threshold_percentage": 2.5,
        "motion_threshold_percentage": 2.5,
        "improve_contrast": True,
        "blur_radius": 1,
        "delta_alpha": 0.2,
        "frame_alpha": 0.01,
        "lightning_threshold": 0.8,
    },
    "very_high": {
        "name": "very_high",
        "label": "Very High",
        "title": "Very High",
        "description": "Maximum sensitivity; triggers on slight motion.",
        "sensitivity": 95,
        "threshold": 12,
        "motion_threshold": 12,
        "contour_area": 10,
        "motion_contour_area": 10,
        "min_contour_area": 10,
        "min_area": 10,
        "min_blob_area": 10,
        "blob_min_area": 10,
        "threshold_percentage": 1.0,
        "motion_threshold_percentage": 1.0,
        "improve_contrast": True,
        "blur_radius": 1,
        "delta_alpha": 0.2,
        "frame_alpha": 0.01,
        "lightning_threshold": 0.8,
    },
}

# Alias -> canonical name.
_NAME_ALIASES: Dict[str, str] = {
    "off": "off",
    "disabled": "off",
    "disable": "off",
    "none": "off",
    "lowest": "very_low",
    "verylow": "very_low",
    "very-low": "very_low",
    "very_low": "very_low",
    "min": "very_low",
    "minimum": "very_low",
    "insensitive": "low",
    "low": "low",
    "medium": "medium",
    "med": "medium",
    "normal": "medium",
    "balanced": "medium",
    "default": "medium",
    "standard": "medium",
    "on": "medium",
    "enabled": "medium",
    "high": "high",
    "sensitive": "high",
    "veryhigh": "very_high",
    "very-high": "very_high",
    "very_high": "very_high",
    "highest": "very_high",
    "max": "very_high",
    "maximum": "very_high",
    "ultra": "very_high",
    "extreme": "very_high",
}

DEFAULT_PRESET_NAME = "medium"
DEFAULT_PRESET = "medium"

# Compatibility top-level names.
PRESETS = _PRESETS
MOTION_PRESETS = _PRESETS
SENSITIVITY_PRESETS = _PRESETS
MOTION_SENSITIVITY_PRESETS = _PRESETS
PRESET_NAMES = list(_PRESETS.keys())


@dataclass
class MotionPreset:
    """Typed motion sensitivity preset."""

    name: str = "medium"
    label: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    sensitivity: int = 60
    threshold: int = 30
    contour_area: int = 100
    min_area: int = 100
    min_blob_area: int = 100
    threshold_percentage: float = 5.0
    improve_contrast: bool = True
    blur_radius: int = 1
    extra: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        canonical = normalize_preset_name(self.name) or "medium"
        base = _PRESETS.get(canonical, _PRESETS["medium"])
        # Fill defaults from base for fields left at sentinel? Only fill
        # label/title/description when not provided.
        if self.label is None:
            self.label = base.get("label", canonical)
        if self.title is None:
            self.title = base.get("title", canonical)
        if self.description is None:
            self.description = base.get("description", "")
        self.name = canonical

    def to_dict(self) -> Dict[str, Any]:
        base = copy.deepcopy(_PRESETS.get(self.name, _PRESETS["medium"]))
        # Overlay any explicitly customized scalar fields.
        for k in (
            "label",
            "title",
            "description",
            "sensitivity",
            "threshold",
            "contour_area",
            "min_area",
            "min_blob_area",
            "threshold_percentage",
            "improve_contrast",
            "blur_radius",
        ):
            v = getattr(self, k, None)
            if v is not None:
                base[k] = v
        # Keep derived aliases in sync.
        base["motion_threshold"] = base.get("threshold")
        base["motion_contour_area"] = base.get("contour_area")
        base["min_contour_area"] = base.get("contour_area")
        base["min_area"] = base.get("min_area", base.get("contour_area"))
        base["min_blob_area"] = base.get("min_blob_area", base.get("contour_area"))
        base["blob_min_area"] = base.get("min_blob_area")
        base["motion_threshold_percentage"] = base.get("threshold_percentage")
        base.update(self.extra or {})
        return base

    def as_dict(self) -> Dict[str, Any]:
        return self.to_dict()

    def config_dict(self) -> Dict[str, Any]:
        return self.to_dict()

    def get_config(self) -> Dict[str, Any]:
        return self.to_dict()


# Aliases for the dataclass.
SensitivityPreset = MotionPreset
MotionSensitivityPreset = MotionPreset
Preset = MotionPreset
MotionPresetConfig = MotionPreset


def _split_camel(value: str) -> str:
    # veryHigh -> very_High
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", value)


def normalize_preset_name(name: Any) -> Optional[str]:
    """Normalize a user-supplied preset name to its canonical key."""
    if name is None:
        return None
    if isinstance(name, MotionPreset):
        return normalize_preset_name(name.name)
    if isinstance(name, dict):
        for k in ("preset", "preset_name", "name", "level", "sensitivity_preset", "mode", "value"):
            if name.get(k) is not None:
                return normalize_preset_name(name.get(k))
        return None
    s = str(name).strip()
    if not s:
        return None
    s = _split_camel(s)
    s = s.strip().lower().replace("-", "_").replace(" ", "_").replace("/", "_")
    s = re.sub(r"__+", "_", s)
    if s in _PRESETS:
        return s
    if s in _NAME_ALIASES:
        return _NAME_ALIASES[s]
    # numeric sensitivity 0-100 -> nearest preset
    try:
        num = float(s)
        return preset_for_sensitivity(num)
    except (TypeError, ValueError):
        pass
    return None


def canonical_name(name: Any, default: Optional[str] = DEFAULT_PRESET_NAME) -> Optional[str]:
    out = normalize_preset_name(name)
    return out if out is not None else default


def normalize_name(name: Any, default: Optional[str] = DEFAULT_PRESET_NAME) -> Optional[str]:
    return canonical_name(name, default)


def resolve_preset_name(name: Any, default: Optional[str] = DEFAULT_PRESET_NAME) -> Optional[str]:
    return canonical_name(name, default)


def is_valid_preset(name: Any) -> bool:
    return normalize_preset_name(name) is not None


def is_valid(name: Any) -> bool:
    return is_valid_preset(name)


def _copy(name: str) -> Dict[str, Any]:
    return copy.deepcopy(_PRESETS[name])


def get_preset(name: Any = None, default: Any = None, **kwargs: Any) -> Optional[Dict[str, Any]]:
    """Return a deep copy of the preset dict, or ``default``/None if unknown."""
    if name is None:
        # tolerate kwargs like preset=, name=, level=
        for k in ("preset", "preset_name", "level", "sensitivity_preset", "mode", "value"):
            if kwargs.get(k) is not None:
                name = kwargs.get(k)
                break
    if name is None:
        name = DEFAULT_PRESET_NAME
    canonical = normalize_preset_name(name)
    if canonical is None:
        return default
    return _copy(canonical)


def get_motion_preset(name: Any = None, default: Any = None, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return get_preset(name, default, **kwargs)


def get_sensitivity_preset(name: Any = None, default: Any = None, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return get_preset(name, default, **kwargs)


def get_motion_sensitivity_preset(name: Any = None, default: Any = None, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return get_preset(name, default, **kwargs)


def fetch_preset(name: Any = None, default: Any = None, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return get_preset(name, default, **kwargs)


def load_preset(name: Any = None, default: Any = None, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return get_preset(name, default, **kwargs)


def read_preset(name: Any = None, default: Any = None, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return get_preset(name, default, **kwargs)


def get_preset_or_default(name: Any = None, **kwargs: Any) -> Dict[str, Any]:
    canonical = normalize_preset_name(name) or DEFAULT_PRESET_NAME
    return _copy(canonical)


def get_preset_config(name: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return get_preset_or_default(name, **kwargs)


def preset_config(name: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return get_preset_or_default(name, **kwargs)


def get_config(name: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return get_preset_or_default(name, **kwargs)


def to_config(name: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return get_preset_or_default(name, **kwargs)


def to_tuner_config(name: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return get_preset_or_default(name, **kwargs)


def to_detector_config(name: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return get_preset_or_default(name, **kwargs)


def to_zoning_config(name: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return get_preset_or_default(name, **kwargs)


def describe_preset(name: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return get_preset_or_default(name, **kwargs)


def list_presets(**kwargs: Any) -> List[Dict[str, Any]]:
    return [copy.deepcopy(v) for v in _PRESETS.values()]


def list_motion_presets(**kwargs: Any) -> List[Dict[str, Any]]:
    return list_presets(**kwargs)


def list_sensitivity_presets(**kwargs: Any) -> List[Dict[str, Any]]:
    return list_presets(**kwargs)


def list_motion_sensitivity_presets(**kwargs: Any) -> List[Dict[str, Any]]:
    return list_presets(**kwargs)


def get_all_presets(**kwargs: Any) -> List[Dict[str, Any]]:
    return list_presets(**kwargs)


def get_presets(**kwargs: Any) -> List[Dict[str, Any]]:
    return list_presets(**kwargs)


def get_all(**kwargs: Any) -> List[Dict[str, Any]]:
    return list_presets(**kwargs)


def all_presets(**kwargs: Any) -> List[Dict[str, Any]]:
    return list_presets(**kwargs)


def get_categorized_presets(**kwargs: Any) -> Dict[str, Dict[str, Any]]:
    return {k: copy.deepcopy(v) for k, v in _PRESETS.items()}


def get_categorized(**kwargs: Any) -> Dict[str, Dict[str, Any]]:
    return get_categorized_presets(**kwargs)


def categorized_presets(**kwargs: Any) -> Dict[str, Dict[str, Any]]:
    return get_categorized_presets(**kwargs)


def parse_presets(**kwargs: Any) -> Dict[str, Dict[str, Any]]:
    return get_categorized_presets(**kwargs)


def list_preset_names(**kwargs: Any) -> List[str]:
    return list(_PRESETS.keys())


def get_preset_names(**kwargs: Any) -> List[str]:
    return list(_PRESETS.keys())


def preset_names(**kwargs: Any) -> List[str]:
    return list(_PRESETS.keys())


def preset_for_sensitivity(value: Any) -> str:
    """Map a numeric 0-100 sensitivity score to the nearest preset name."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return DEFAULT_PRESET_NAME
    if v <= 0:
        return "off"
    if v <= 25:
        return "very_low"
    if v <= 47:
        return "low"
    if v <= 70:
        return "medium"
    if v <= 88:
        return "high"
    return "very_high"


def sensitivity_to_preset(value: Any) -> str:
    return preset_for_sensitivity(value)


def preset_from_sensitivity(value: Any) -> str:
    return preset_for_sensitivity(value)


def get_sensitivity(name: Any = None, **kwargs: Any) -> Optional[int]:
    preset = get_preset(name, None, **kwargs)
    if preset is None:
        return None
    return preset.get("sensitivity")


def apply_preset(
    name: Any = None,
    tuner: Any = None,
    update_singleton: bool = True,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Resolve preset config and optionally apply it to a tuner object.

    When ``tuner`` is given (or the singleton can be imported) the tuner is
    updated in place via ``update_settings`` when available, else by setting
    attributes directly. Always returns the resolved preset config dict.
    """
    if name is None:
        for k in ("preset", "preset_name", "level", "sensitivity_preset", "mode", "value"):
            if kwargs.get(k) is not None:
                name = kwargs.pop(k)
                break
    canonical = normalize_preset_name(name) or DEFAULT_PRESET_NAME
    config = _copy(canonical)

    target = tuner
    if target is None and update_singleton:
        try:
            from app.services import motion_tuner as _mt  # lazy to avoid cycles

            target = getattr(_mt, "motion_tuner", None)
        except Exception:  # noqa: BLE001
            target = None

    if target is not None:
        try:
            if hasattr(target, "update_settings"):
                target.update_settings(
                    threshold=config.get("threshold"),
                    contour_area=config.get("contour_area"),
                    improve_contrast=config.get("improve_contrast"),
                    blur_radius=config.get("blur_radius"),
                )
            else:
                for k, v in config.items():
                    try:
                        setattr(target, k, v)
                    except Exception:  # noqa: BLE001
                        pass
        except Exception:  # noqa: BLE001
            pass
        # Merge explicit kwargs overrides.
        for k, v in kwargs.items():
            if v is None:
                continue
            try:
                if hasattr(target, "update_settings") and not k.startswith("_"):
                    target.update_settings(**{k: v})
                else:
                    setattr(target, k, v)
            except Exception:  # noqa: BLE001
                pass
            config[k] = v
    else:
        for k, v in kwargs.items():
            if v is None or k.startswith("_"):
                continue
            config[k] = v
    return copy.deepcopy(config)


def apply_motion_preset(name: Any = None, tuner: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return apply_preset(name, tuner=tuner, **kwargs)


def apply_sensitivity_preset(name: Any = None, tuner: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return apply_preset(name, tuner=tuner, **kwargs)


def apply_motion_sensitivity_preset(name: Any = None, tuner: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return apply_preset(name, tuner=tuner, **kwargs)


def apply(name: Any = None, tuner: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return apply_preset(name, tuner=tuner, **kwargs)


__all__ = [
    "MotionPreset",
    "SensitivityPreset",
    "MotionSensitivityPreset",
    "Preset",
    "MotionPresetConfig",
    "PRESETS",
    "MOTION_PRESETS",
    "SENSITIVITY_PRESETS",
    "MOTION_SENSITIVITY_PRESETS",
    "PRESET_NAMES",
    "DEFAULT_PRESET_NAME",
    "DEFAULT_PRESET",
    "normalize_preset_name",
    "canonical_name",
    "normalize_name",
    "resolve_preset_name",
    "is_valid_preset",
    "is_valid",
    "get_preset",
    "get_motion_preset",
    "get_sensitivity_preset",
    "get_motion_sensitivity_preset",
    "fetch_preset",
    "load_preset",
    "read_preset",
    "get_preset_or_default",
    "get_preset_config",
    "preset_config",
    "get_config",
    "to_config",
    "to_tuner_config",
    "to_detector_config",
    "to_zoning_config",
    "describe_preset",
    "list_presets",
    "list_motion_presets",
    "list_sensitivity_presets",
    "list_motion_sensitivity_presets",
    "get_all_presets",
    "get_presets",
    "get_all",
    "all_presets",
    "get_categorized_presets",
    "get_categorized",
    "categorized_presets",
    "parse_presets",
    "list_preset_names",
    "get_preset_names",
    "preset_names",
    "preset_for_sensitivity",
    "sensitivity_to_preset",
    "preset_from_sensitivity",
    "get_sensitivity",
    "apply_preset",
    "apply_motion_preset",
    "apply_sensitivity_preset",
    "apply_motion_sensitivity_preset",
    "apply",
]
