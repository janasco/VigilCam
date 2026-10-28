"""Zone shape templates service.

Provides named polygon *shape* presets for motion zones, distinct from:

- :mod:`app.services.motion_presets` — motion *sensitivity* levels
  (off / very_low / low / medium / high / very_high) mapping to detector
  thresholds, and
- :mod:`app.services.motion_zoning` — freehand polygon zones
  (``PolygonZone`` / ``MotionZoningEngine``) where callers supply arbitrary
  ``points``.

This module supplies ready-made zone *shapes* (full / entry / exit /
driveway / perimeter) expressed in normalized 0-1 coordinates so the UI can
offer one-click templates instead of freehand drawing only.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


def _zone(points: List[List[float]], name: str, mode: str = "include",
          threshold_percentage: float = 5.0) -> Dict[str, Any]:
    return {
        "points": [list(p) for p in points],
        "mode": mode,
        "threshold_percentage": float(threshold_percentage),
        "name": name,
    }


_TEMPLATES: Dict[str, Dict[str, Any]] = {
    "full": {
        "name": "full",
        "label": "Full Frame",
        "title": "Full Frame",
        "description": "Cover the entire frame.",
        "zones": [
            _zone([[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]], "full"),
        ],
    },
    "entry": {
        "name": "entry",
        "label": "Entry",
        "title": "Entry",
        "description": "Bottom-center doorway / entry corridor.",
        "zones": [
            _zone([[0.35, 0.55], [0.65, 0.55], [0.65, 1.0], [0.35, 1.0]], "entry"),
        ],
    },
    "exit": {
        "name": "exit",
        "label": "Exit",
        "title": "Exit",
        "description": "Top-edge exit band.",
        "zones": [
            _zone([[0.0, 0.0], [1.0, 0.0], [1.0, 0.22], [0.0, 0.22]], "exit"),
        ],
    },
    "driveway": {
        "name": "driveway",
        "label": "Driveway",
        "title": "Driveway",
        "description": "Lower trapezoid covering a driveway approach.",
        "zones": [
            _zone([[0.05, 0.45], [0.95, 0.45], [1.0, 1.0], [0.0, 1.0]], "driveway"),
        ],
    },
    "perimeter": {
        "name": "perimeter",
        "label": "Perimeter",
        "title": "Perimeter",
        "description": "Border ring (top/bottom/left/right strips).",
        "zones": [
            _zone([[0.0, 0.0], [1.0, 0.0], [1.0, 0.12], [0.0, 0.12]], "perimeter-top"),
            _zone([[0.0, 0.88], [1.0, 0.88], [1.0, 1.0], [0.0, 1.0]], "perimeter-bottom"),
            _zone([[0.0, 0.0], [0.12, 0.0], [0.12, 1.0], [0.0, 1.0]], "perimeter-left"),
            _zone([[0.88, 0.0], [1.0, 0.0], [1.0, 1.0], [0.88, 1.0]], "perimeter-right"),
        ],
    },
}

_NAME_ALIASES: Dict[str, str] = {
    "full": "full",
    "full_frame": "full",
    "fullframe": "full",
    "full-frame": "full",
    "all": "full",
    "entire": "full",
    "whole": "full",
    "entry": "entry",
    "entrance": "entry",
    "entryway": "entry",
    "door": "entry",
    "doorway": "entry",
    "exit": "exit",
    "leave": "exit",
    "driveway": "driveway",
    "drive": "driveway",
    "drive_way": "driveway",
    "perimeter": "perimeter",
    "border": "perimeter",
    "fence": "perimeter",
    "yard": "perimeter",
    "edge": "perimeter",
}

DEFAULT_TEMPLATE_NAME = "full"
DEFAULT_TEMPLATE = "full"

ZONE_TEMPLATES = _TEMPLATES
TEMPLATES = _TEMPLATES
SHAPE_TEMPLATES = _TEMPLATES
ZONE_SHAPE_TEMPLATES = _TEMPLATES
TEMPLATE_NAMES = list(_TEMPLATES.keys())


@dataclass
class ZoneShapeTemplate:
    """Typed zone shape template."""

    name: str = "full"
    label: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    zones: List[Dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        canonical = normalize_template_name(self.name) or "full"
        base = _TEMPLATES.get(canonical, _TEMPLATES["full"])
        self.name = canonical
        if self.label is None:
            self.label = base.get("label", canonical)
        if self.title is None:
            self.title = base.get("title", canonical)
        if self.description is None:
            self.description = base.get("description", "")
        if not self.zones:
            self.zones = copy.deepcopy(base.get("zones", []))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "title": self.title,
            "description": self.description,
            "zones": copy.deepcopy(self.zones),
        }

    def as_dict(self) -> Dict[str, Any]:
        return self.to_dict()


ZoneTemplate = ZoneShapeTemplate
ShapeTemplate = ZoneShapeTemplate
ZoneShapePreset = ZoneShapeTemplate


def _split_camel(value: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", value)


def normalize_template_name(name: Any) -> Optional[str]:
    """Normalize a user-supplied template name to its canonical key."""
    if name is None:
        return None
    if isinstance(name, ZoneShapeTemplate):
        return normalize_template_name(name.name)
    if isinstance(name, dict):
        for k in ("template", "template_name", "name", "shape", "zone_template",
                  "preset", "preset_name", "mode", "value", "key", "id"):
            if name.get(k) is not None:
                return normalize_template_name(name.get(k))
        return None
    s = str(name).strip()
    if not s:
        return None
    s = _split_camel(s)
    s = s.strip().lower().replace("-", "_").replace(" ", "_").replace("/", "_")
    s = re.sub(r"__+", "_", s)
    if s in _TEMPLATES:
        return s
    if s in _NAME_ALIASES:
        return _NAME_ALIASES[s]
    return None


def canonical_name(name: Any, default: Optional[str] = DEFAULT_TEMPLATE_NAME) -> Optional[str]:
    out = normalize_template_name(name)
    return out if out is not None else default


def normalize_name(name: Any, default: Optional[str] = DEFAULT_TEMPLATE_NAME) -> Optional[str]:
    return canonical_name(name, default)


def resolve_template_name(name: Any, default: Optional[str] = DEFAULT_TEMPLATE_NAME) -> Optional[str]:
    return canonical_name(name, default)


def is_valid_template(name: Any) -> bool:
    return normalize_template_name(name) is not None


def is_valid(name: Any) -> bool:
    return is_valid_template(name)


def _copy(name: str) -> Dict[str, Any]:
    return copy.deepcopy(_TEMPLATES[name])


def get_template(name: Any = None, default: Any = None, **kwargs: Any) -> Optional[Dict[str, Any]]:
    """Return a deep copy of the template dict, or ``default`` if unknown."""
    if name is None:
        for k in ("template", "template_name", "shape", "zone_template",
                  "preset", "preset_name", "name", "key", "id", "value"):
            if kwargs.get(k) is not None:
                name = kwargs.get(k)
                break
    if name is None:
        name = DEFAULT_TEMPLATE_NAME
    canonical = normalize_template_name(name)
    if canonical is None:
        return default
    return _copy(canonical)


def get_zone_template(name: Any = None, default: Any = None, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return get_template(name, default, **kwargs)


def get_shape_template(name: Any = None, default: Any = None, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return get_template(name, default, **kwargs)


def fetch_template(name: Any = None, default: Any = None, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return get_template(name, default, **kwargs)


def load_template(name: Any = None, default: Any = None, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return get_template(name, default, **kwargs)


def read_template(name: Any = None, default: Any = None, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return get_template(name, default, **kwargs)


def get_template_or_default(name: Any = None, **kwargs: Any) -> Dict[str, Any]:
    canonical = normalize_template_name(name) or DEFAULT_TEMPLATE_NAME
    return _copy(canonical)


def list_templates(**kwargs: Any) -> List[Dict[str, Any]]:
    return [copy.deepcopy(v) for v in _TEMPLATES.values()]


def list_zone_templates(**kwargs: Any) -> List[Dict[str, Any]]:
    return list_templates(**kwargs)


def list_shape_templates(**kwargs: Any) -> List[Dict[str, Any]]:
    return list_templates(**kwargs)


def get_all_templates(**kwargs: Any) -> List[Dict[str, Any]]:
    return list_templates(**kwargs)


def get_templates(**kwargs: Any) -> List[Dict[str, Any]]:
    return list_templates(**kwargs)


def get_all(**kwargs: Any) -> List[Dict[str, Any]]:
    return list_templates(**kwargs)


def all_templates(**kwargs: Any) -> List[Dict[str, Any]]:
    return list_templates(**kwargs)


def list_template_names(**kwargs: Any) -> List[str]:
    return list(_TEMPLATES.keys())


def get_template_names(**kwargs: Any) -> List[str]:
    return list(_TEMPLATES.keys())


def template_names(**kwargs: Any) -> List[str]:
    return list(_TEMPLATES.keys())


def scale_points(points: Any, width: float = 1.0, height: float = 1.0) -> List[List[float]]:
    """Scale normalized 0-1 points to pixel dimensions."""
    out: List[List[float]] = []
    for p in list(points or []):
        try:
            x = float(p[0]) * float(width)
            y = float(p[1]) * float(height)
        except (TypeError, ValueError, IndexError):
            continue
        out.append([x, y])
    return out


def get_template_zones(name: Any = None, width: Any = None, height: Any = None,
                       **kwargs: Any) -> List[Dict[str, Any]]:
    """Return zone dicts for a template, optionally scaled to pixels."""
    template = get_template_or_default(name)
    zones = copy.deepcopy(template.get("zones", []))
    if width is not None or height is not None:
        w = float(width) if width is not None else 1.0
        h = float(height) if height is not None else 1.0
        for z in zones:
            z["points"] = scale_points(z.get("points", []), w, h)
    return zones


def get_zones(name: Any = None, **kwargs: Any) -> List[Dict[str, Any]]:
    return get_template_zones(name, **kwargs)


def to_zones(name: Any = None, **kwargs: Any) -> List[Dict[str, Any]]:
    return get_template_zones(name, **kwargs)


def to_polygon_zones(name: Any = None, **kwargs: Any) -> List[Any]:
    """Return zones as ``PolygonZone`` objects (lazy import)."""
    from app.services.motion_zoning import PolygonZone

    return [PolygonZone(**z) for z in get_template_zones(name, **kwargs)]


def to_motion_zones(name: Any = None, **kwargs: Any) -> List[Any]:
    return to_polygon_zones(name, **kwargs)


def build_zones(name: Any = None, **kwargs: Any) -> List[Dict[str, Any]]:
    return get_template_zones(name, **kwargs)


def instantiate(name: Any = None, **kwargs: Any) -> List[Dict[str, Any]]:
    return get_template_zones(name, **kwargs)


def apply_template(name: Any = None, **kwargs: Any) -> Dict[str, Any]:
    """Return the template dict with resolved zones (tolerant helper)."""
    template = get_template_or_default(name)
    out = copy.deepcopy(template)
    width = kwargs.get("width", kwargs.get("w", kwargs.get("frame_width")))
    height = kwargs.get("height", kwargs.get("h", kwargs.get("frame_height")))
    if width is not None or height is not None:
        out["zones"] = get_template_zones(template.get("name"), width=width or 1.0, height=height or 1.0)
    return out


__all__ = [
    "ZoneShapeTemplate",
    "ZoneTemplate",
    "ShapeTemplate",
    "ZoneShapePreset",
    "ZONE_TEMPLATES",
    "TEMPLATES",
    "SHAPE_TEMPLATES",
    "ZONE_SHAPE_TEMPLATES",
    "TEMPLATE_NAMES",
    "DEFAULT_TEMPLATE_NAME",
    "DEFAULT_TEMPLATE",
    "normalize_template_name",
    "canonical_name",
    "normalize_name",
    "resolve_template_name",
    "is_valid_template",
    "is_valid",
    "get_template",
    "get_zone_template",
    "get_shape_template",
    "fetch_template",
    "load_template",
    "read_template",
    "get_template_or_default",
    "list_templates",
    "list_zone_templates",
    "list_shape_templates",
    "get_all_templates",
    "get_templates",
    "get_all",
    "all_templates",
    "list_template_names",
    "get_template_names",
    "template_names",
    "scale_points",
    "get_template_zones",
    "get_zones",
    "to_zones",
    "to_polygon_zones",
    "to_motion_zones",
    "build_zones",
    "instantiate",
    "apply_template",
]
