"""Privacy mask regions for output blackout (snapshots / MJPEG).

This module is deliberately separate from motion zoning:

- :mod:`app.services.motion_zoning` — polygon ``include``/``exclude`` zones
  only gate *detection* (they are applied to the motion delta/threshold
  image inside ``MotionZoningEngine.process_and_evaluate`` and never modify
  the caller's frame, snapshot, or stream bytes).
- This module — privacy mask regions are applied to *output* frames: they
  paint solid black polygons over the image returned for snapshots and MJPEG
  streams.

Regions may be expressed either as polygons::

    {"points": [[x0, y0], [x1, y1], ...], "name": "door"}

or as rectangles::

    {"x": 10, "y": 20, "width": 100, "height": 60}
    {"x0": 10, "y0": 20, "x1": 110, "y1": 80}
    {"bbox": [10, 20, 100, 60]}
    {"rect": [10, 20, 100, 60]}

Coordinates are auto-detected per region: values in the 0-1 range are
treated as normalized fractions of the frame size, anything larger is
treated as pixels. An explicit ``units`` key (``"normalized"``/``"pixels"``)
overrides the heuristic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np

DEFAULT_COLOR: Tuple[int, int, int] = (0, 0, 0)

# In-memory store: str(camera_id) -> list of normalized region dicts.
_STORE: Dict[str, List[Dict[str, Any]]] = {}


# ---------------------------------------------------------------------------
# Coercion helpers
# ---------------------------------------------------------------------------

def _camera_key(camera_id: Any) -> str:
    if camera_id is None:
        return "default"
    return str(camera_id).strip() or "default"


def _coerce_number(value: Any) -> Optional[float]:
    try:
        if value is None or isinstance(value, bool):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _points_from_rect(data: Dict[str, Any]) -> Optional[List[List[float]]]:
    """Extract polygon points from rect-style keys, if present."""
    x = y = w = h = None
    for k in ("x", "left", "x0", "xmin", "col"):
        if data.get(k) is not None:
            x = _coerce_number(data.get(k))
            break
    for k in ("y", "top", "y0", "ymin", "row"):
        if data.get(k) is not None:
            y = _coerce_number(data.get(k))
            break
    for k in ("width", "w", "cols"):
        if data.get(k) is not None:
            w = _coerce_number(data.get(k))
            break
    for k in ("height", "h", "rows"):
        if data.get(k) is not None:
            h = _coerce_number(data.get(k))
            break
    x1 = y1 = None
    for k in ("x1", "xmax", "right"):
        if data.get(k) is not None:
            x1 = _coerce_number(data.get(k))
            break
    for k in ("y1", "ymax", "bottom"):
        if data.get(k) is not None:
            y1 = _coerce_number(data.get(k))
            break
    for k in ("bbox", "rect", "box", "rectangle", "bounds"):
        if data.get(k) is not None:
            v = data.get(k)
            if isinstance(v, (list, tuple)) and len(v) >= 4:
                try:
                    bx, by, bw, bh = (float(v[0]), float(v[1]), float(v[2]), float(v[3]))
                except (TypeError, ValueError):
                    continue
                # bbox may be [x0, y0, x1, y1] when w/h look like absolutes;
                # treat as [x, y, w, h] (repo convention for regions).
                return [[bx, by], [bx + bw, by], [bx + bw, by + bh], [bx, by + bh]]
            if isinstance(v, dict):
                return _points_from_rect(v)
    if x is not None and y is not None:
        if w is not None and h is not None:
            return [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]
        if x1 is not None and y1 is not None:
            return [[x, y], [x1, y], [x1, y1], [x, y1]]
    return None


def _coerce_point(point: Any) -> Optional[List[float]]:
    if isinstance(point, dict):
        x = point.get("x", point.get("X", point.get("lon")))
        y = point.get("y", point.get("Y", point.get("lat")))
        if x is None or y is None:
            # {"0": x, "1": y} style
            try:
                x = point.get(0, point.get("0"))
                y = point.get(1, point.get("1"))
            except Exception:
                return None
        try:
            return [float(x), float(y)]  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
    if isinstance(point, (list, tuple)):
        if len(point) >= 2:
            try:
                return [float(point[0]), float(point[1])]
            except (TypeError, ValueError):
                return None
        return None
    return None


def _coerce_points(value: Any) -> List[List[float]]:
    if value is None:
        return []
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return []
        if "," in s and " " not in s:
            try:
                vals = [float(v) for v in s.split(",")]
            except ValueError:
                return []
            return [[vals[i], vals[i + 1]] for i in range(0, len(vals) - 1, 2)]
        out: List[List[float]] = []
        for token in s.replace(";", " ").split():
            if "," in token:
                x, y = token.split(",", 1)
                try:
                    out.append([float(x), float(y)])
                except ValueError:
                    continue
        return out
    if isinstance(value, (list, tuple)):
        if value and all(isinstance(p, (int, float, np.integer, np.floating)) for p in value):
            vals = [float(v) for v in value]
            return [[vals[i], vals[i + 1]] for i in range(0, len(vals) - 1, 2)]
        out = []
        for p in value:
            if isinstance(p, (list, tuple)) and len(p) == 1 and isinstance(p[0], (list, tuple)):
                p = p[0]
            cp = _coerce_point(p)
            if cp is not None:
                out.append(cp)
        return out
    return []


def _explicit_units(data: Dict[str, Any]) -> Optional[str]:
    for k in ("units", "coords", "coord_system", "coordinate_system", "space"):
        v = data.get(k)
        if v is not None:
            s = str(v).strip().lower()
            if "norm" in s or "0-1" in s or "fraction" in s or "relative" in s:
                return "normalized"
            if "pixel" in s or "abs" in s:
                return "pixels"
    return None


def normalize_region(region: Any) -> Optional[Dict[str, Any]]:
    """Normalize one region to ``{"points", "name", ...}`` or ``None``."""
    if region is None:
        return None
    if isinstance(region, (list, tuple)):
        # Bare point list -> polygon.
        if region and all(isinstance(p, (int, float, np.integer, np.floating)) for p in region):
            pts = _coerce_points(list(region))
        else:
            pts = _coerce_points(region)
        if len(pts) < 3:
            return None
        return {"points": pts, "name": None}
    if not isinstance(region, dict):
        return None
    data = dict(region)
    points = None
    for k in ("points", "polygon", "coords", "coordinates", "vertices", "pts", "poly"):
        if data.get(k) is not None:
            points = _coerce_points(data.get(k))
            break
    if not points:
        points = _points_from_rect(data)
    if not points or len(points) < 3:
        return None
    name = data.get("name", data.get("label", data.get("id")))
    out: Dict[str, Any] = {
        "points": [[float(x), float(y)] for x, y in points],
        "name": str(name) if name is not None else None,
    }
    units = _explicit_units(data)
    if units is not None:
        out["units"] = units
    # Preserve rect echo for callers (harmless).
    for k in ("x", "y", "width", "height", "x0", "y0", "x1", "y1", "bbox", "rect"):
        if data.get(k) is not None:
            out.setdefault(k, data.get(k))
    return out


def normalize_regions(regions: Any) -> List[Dict[str, Any]]:
    """Normalize a region or list of regions; invalid entries are dropped."""
    if regions is None:
        return []
    if isinstance(regions, dict):
        # Single region vs {name: region} mapping.
        probe = normalize_region(regions)
        if probe is not None and probe.get("points"):
            # Could still be a mapping of named regions; a real region has
            # points-like keys. If none of the values look like regions,
            # treat as single.
            has_points_key = any(
                k in regions for k in ("points", "polygon", "coords", "coordinates", "vertices", "pts", "x", "y", "bbox", "rect")
            )
            if has_points_key:
                out = probe
                return [out]
        # Mapping of name -> region.
        out_list: List[Dict[str, Any]] = []
        for name, sub in regions.items():
            sub_n = normalize_region(sub)
            if sub_n is None:
                continue
            if sub_n.get("name") is None:
                sub_n["name"] = str(name)
            out_list.append(sub_n)
        return out_list
    if isinstance(regions, (list, tuple)):
        out_list = []
        for r in regions:
            n = normalize_region(r)
            if n is not None:
                out_list.append(n)
        return out_list
    n = normalize_region(regions)
    return [n] if n is not None else []


def validate_regions(regions: Any) -> List[Dict[str, Any]]:
    """Alias of :func:`normalize_regions` (raises nothing; drops invalid)."""
    return normalize_regions(regions)


# ---------------------------------------------------------------------------
# Store CRUD
# ---------------------------------------------------------------------------

def set_masks(camera_id: Any, regions: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """Replace the privacy masks for ``camera_id``. Returns stored regions."""
    for alias in ("masks", "mask", "zones", "polygons", "polygon", "zone_list"):
        if regions is None and kwargs.get(alias) is not None:
            regions = kwargs.get(alias)
    normalized = normalize_regions(regions)
    _STORE[_camera_key(camera_id)] = normalized
    return [dict(r) for r in normalized]


def get_masks(camera_id: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """Return a copy of the stored masks for ``camera_id`` (possibly ``[]``)."""
    return [dict(r) for r in _STORE.get(_camera_key(camera_id), [])]


def list_masks(camera_id: Any = None, **kwargs: Any) -> Any:
    """List masks for one camera, or the whole store when ``camera_id`` is None."""
    if camera_id is None:
        for alias in ("camera", "cam", "camera_id", "id"):
            if kwargs.get(alias) is not None:
                camera_id = kwargs.get(alias)
                break
    if camera_id is None:
        return {k: [dict(r) for r in v] for k, v in _STORE.items()}
    return get_masks(camera_id)


def clear_masks(camera_id: Any, **kwargs: Any) -> int:
    """Remove all masks for ``camera_id``. Returns number removed."""
    key = _camera_key(camera_id)
    removed = len(_STORE.get(key, []))
    _STORE.pop(key, None)
    return removed


def clear_all(**kwargs: Any) -> int:
    """Remove every stored mask. Returns number of regions removed."""
    total = sum(len(v) for v in _STORE.values())
    _STORE.clear()
    return total


def add_mask(camera_id: Any, region: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """Append one region to ``camera_id``. Returns updated list."""
    normalized = normalize_region(region)
    if normalized is None:
        raise ValueError("region must contain a polygon (points) or rectangle")
    key = _camera_key(camera_id)
    _STORE.setdefault(key, []).append(normalized)
    return [dict(r) for r in _STORE[key]]


def remove_mask(camera_id: Any, index: Any = None, name: Any = None, **kwargs: Any) -> List[Dict[str, Any]]:
    """Remove one mask by ``index`` or ``name``. Returns updated list."""
    for alias in ("idx", "i", "mask_index", "region_index"):
        if index is None and kwargs.get(alias) is not None:
            index = kwargs.get(alias)
    for alias in ("label", "id", "mask_name", "region_name"):
        if name is None and kwargs.get(alias) is not None:
            name = kwargs.get(alias)
    key = _camera_key(camera_id)
    current = _STORE.get(key, [])
    if index is not None:
        try:
            current.pop(int(index))
        except (IndexError, TypeError, ValueError) as exc:
            raise IndexError(f"mask index out of range: {index}") from exc
    elif name is not None:
        for i, r in enumerate(current):
            if str(r.get("name")) == str(name):
                current.pop(i)
                break
        else:
            raise KeyError(f"no mask named {name!r}")
    else:
        raise ValueError("remove_mask requires index or name")
    return [dict(r) for r in current]


def has_masks(camera_id: Any) -> bool:
    """Return True when ``camera_id`` has at least one mask region."""
    return bool(_STORE.get(_camera_key(camera_id)))


# ---------------------------------------------------------------------------
# Frame application (output blackout)
# ---------------------------------------------------------------------------

def _is_normalized(points: List[List[float]]) -> bool:
    try:
        return all(-0.001 <= float(x) <= 1.001 and -0.001 <= float(y) <= 1.001 for x, y in points)
    except (TypeError, ValueError):
        return False


def _scale_points(points: List[List[float]], width: int, height: int, units: Optional[str] = None) -> List[Tuple[int, int]]:
    if units == "normalized":
        relative = True
    elif units == "pixels":
        relative = False
    else:
        relative = _is_normalized(points)
    out: List[Tuple[int, int]] = []
    for x, y in points:
        if relative:
            ix = int(round(float(x) * (width - 1)))
            iy = int(round(float(y) * (height - 1)))
        else:
            ix = int(round(float(x)))
            iy = int(round(float(y)))
        ix = max(0, min(width - 1, ix))
        iy = max(0, min(height - 1, iy))
        out.append((ix, iy))
    return out


def _resolve_regions(camera_id: Any, regions: Any, kwargs: Dict[str, Any]) -> List[Dict[str, Any]]:
    if regions is None:
        for alias in ("masks", "mask", "zones", "polygons", "polygon", "zone_list"):
            if kwargs.get(alias) is not None:
                regions = kwargs.get(alias)
                break
    if regions is not None:
        return normalize_regions(regions)
    if camera_id is not None:
        return get_masks(camera_id)
    return []


def _resolve_color(kwargs: Dict[str, Any]) -> Tuple[int, int, int]:
    for k in ("color", "colour", "fill", "fill_color"):
        v = kwargs.get(k)
        if v is not None:
            if isinstance(v, (list, tuple)) and len(v) >= 3:
                try:
                    return (int(v[0]), int(v[1]), int(v[2]))
                except (TypeError, ValueError):
                    pass
            if isinstance(v, str):
                s = v.strip().lower().lstrip("#")
                if s in ("black", "0", "000", "000000"):
                    return (0, 0, 0)
                if len(s) == 6:
                    try:
                        # hex rrggbb -> BGR for OpenCV
                        r = int(s[0:2], 16)
                        g = int(s[2:4], 16)
                        b = int(s[4:6], 16)
                        return (b, g, r)
                    except ValueError:
                        pass
    return DEFAULT_COLOR


def apply_privacy_masks(frame: Any, camera_id: Any = None, regions: Any = None, **kwargs: Any) -> Any:
    """Return a copy of ``frame`` with mask regions painted solid black.

    The input array is never mutated. ``frame`` may be a BGR/RGB ``ndarray``
    (3-channel), BGRA (4-channel), or single-channel grayscale. Regions use
    the same 0-1 normalized / pixel convention as :func:`normalize_region`.
    """
    # Tolerant positional aliases: apply(frame, regions) or (frame, camera_id).
    if regions is None and camera_id is not None and isinstance(camera_id, (list, tuple, dict)):
        regions, camera_id = camera_id, kwargs.get("camera_id", kwargs.get("camera"))
    resolved = _resolve_regions(camera_id, regions, kwargs)
    if frame is None or not resolved:
        return frame.copy() if isinstance(frame, np.ndarray) else frame
    if not isinstance(frame, np.ndarray):
        return frame
    out = frame.copy()
    color = _resolve_color(kwargs)
    h, w = out.shape[:2]
    single = out.ndim == 2
    for region in resolved:
        pts = _scale_points(region.get("points", []), w, h, region.get("units"))
        if len(pts) < 3:
            continue
        arr = np.array(pts, dtype=np.int32).reshape((-1, 1, 2))
        if single:
            cv2.fillPoly(out, [arr], 0)
        elif out.shape[2] == 4:
            cv2.fillPoly(out, [arr], (color[0], color[1], color[2], 255))
        else:
            cv2.fillPoly(out, [arr], color)
    return out


def decode_jpeg_bytes(data: Any) -> Optional[np.ndarray]:
    """Decode JPEG/PNG bytes (or base64) to a BGR ``ndarray``."""
    if data is None:
        return None
    if isinstance(data, np.ndarray):
        return data
    raw: Optional[bytes] = None
    if isinstance(data, (bytes, bytearray)):
        raw = bytes(data)
    elif isinstance(data, str):
        s = data.strip()
        if s.startswith("data:") and "," in s:
            s = s.split(",", 1)[1]
        s = "".join(s.split())
        try:
            import base64 as _b64

            raw = _b64.b64decode(s, validate=False)
        except Exception:
            return None
    if raw is None:
        return None
    try:
        buf = np.frombuffer(raw, dtype=np.uint8)
        return cv2.imdecode(buf, cv2.IMREAD_COLOR)
    except Exception:
        return None


def encode_jpeg_bytes(frame: np.ndarray, quality: int = 85) -> bytes:
    """Encode a BGR ``ndarray`` to JPEG bytes."""
    try:
        q = max(1, min(100, int(quality)))
    except (TypeError, ValueError):
        q = 85
    ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), q])
    if not ok:
        raise ValueError("JPEG encoding failed")
    return bytes(buf.tobytes())


def apply_privacy_masks_to_jpeg(data: Any, camera_id: Any = None, regions: Any = None, **kwargs: Any) -> bytes:
    """Decode JPEG ``data``, black out mask regions, re-encode to JPEG."""
    frame = decode_jpeg_bytes(data)
    if frame is None:
        raise ValueError("Could not decode image bytes")
    resolved = _resolve_regions(camera_id, regions, kwargs)
    if not resolved:
        # Nothing to mask: return valid JPEG bytes regardless.
        return encode_jpeg_bytes(frame, kwargs.get("quality", 85))
    masked = apply_privacy_masks(frame, camera_id=None, regions=resolved, **kwargs)
    return encode_jpeg_bytes(masked, kwargs.get("quality", 85))


# ---------------------------------------------------------------------------
# Dataclass facade
# ---------------------------------------------------------------------------

@dataclass
class PrivacyMaskRegion:
    """Typed privacy mask region (output blackout polygon)."""

    points: Any = field(default_factory=list)
    name: Optional[str] = None
    label: Optional[str] = None
    units: Optional[str] = None

    def __post_init__(self) -> None:
        if self.label is not None and self.name is None:
            self.name = self.label
        normalized = normalize_region({"points": self.points, "name": self.name, "units": self.units})
        if normalized is None:
            # Maybe rect-style passed as points? Keep raw; validation on use.
            self.points = _coerce_points(self.points)
        else:
            self.points = normalized["points"]
            self.name = normalized.get("name")
            if normalized.get("units"):
                self.units = normalized["units"]

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"points": [list(p) for p in (self.points or [])], "name": self.name}
        if self.units:
            out["units"] = self.units
        return out

    @classmethod
    def from_dict(cls, data: Any) -> "PrivacyMaskRegion":
        if isinstance(data, cls):
            return data
        if isinstance(data, (list, tuple)):
            return cls(points=data)
        if isinstance(data, dict):
            for k in ("points", "polygon", "coords", "coordinates", "vertices", "pts"):
                if data.get(k) is not None:
                    return cls(
                        points=data.get(k),
                        name=data.get("name", data.get("label", data.get("id"))),
                        units=_explicit_units(data),
                    )
            rect_pts = _points_from_rect(data)
            if rect_pts is not None:
                return cls(
                    points=rect_pts,
                    name=data.get("name", data.get("label", data.get("id"))),
                    units=_explicit_units(data),
                )
            return cls(points=data.get("points", []))
        return cls(points=[])


PrivacyRegion = PrivacyMaskRegion
MaskRegion = PrivacyMaskRegion
PrivacyMask = PrivacyMaskRegion
Mask = PrivacyMaskRegion


# Aliases covering likely caller vocabularies.
def apply_masks(frame: Any, camera_id: Any = None, regions: Any = None, **kwargs: Any) -> Any:
    return apply_privacy_masks(frame, camera_id=camera_id, regions=regions, **kwargs)


def mask_frame(frame: Any, camera_id: Any = None, regions: Any = None, **kwargs: Any) -> Any:
    return apply_privacy_masks(frame, camera_id=camera_id, regions=regions, **kwargs)


def blackout_regions(frame: Any, camera_id: Any = None, regions: Any = None, **kwargs: Any) -> Any:
    return apply_privacy_masks(frame, camera_id=camera_id, regions=regions, **kwargs)


def apply_blackout(frame: Any, camera_id: Any = None, regions: Any = None, **kwargs: Any) -> Any:
    return apply_privacy_masks(frame, camera_id=camera_id, regions=regions, **kwargs)


def mask_snapshot(frame: Any, camera_id: Any = None, regions: Any = None, **kwargs: Any) -> Any:
    return apply_privacy_masks(frame, camera_id=camera_id, regions=regions, **kwargs)


def mask_mjpeg_frame(frame: Any, camera_id: Any = None, regions: Any = None, **kwargs: Any) -> Any:
    return apply_privacy_masks(frame, camera_id=camera_id, regions=regions, **kwargs)


def get_masked_frame(frame: Any, camera_id: Any = None, regions: Any = None, **kwargs: Any) -> Any:
    return apply_privacy_masks(frame, camera_id=camera_id, regions=regions, **kwargs)


def apply_masks_to_jpeg(data: Any, camera_id: Any = None, regions: Any = None, **kwargs: Any) -> bytes:
    return apply_privacy_masks_to_jpeg(data, camera_id=camera_id, regions=regions, **kwargs)


def mask_jpeg(data: Any, camera_id: Any = None, regions: Any = None, **kwargs: Any) -> bytes:
    return apply_privacy_masks_to_jpeg(data, camera_id=camera_id, regions=regions, **kwargs)


def mask_jpeg_bytes(data: Any, camera_id: Any = None, regions: Any = None, **kwargs: Any) -> bytes:
    return apply_privacy_masks_to_jpeg(data, camera_id=camera_id, regions=regions, **kwargs)


def set_privacy_masks(camera_id: Any, regions: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    return set_masks(camera_id, regions, **kwargs)


def get_privacy_masks(camera_id: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    return get_masks(camera_id, **kwargs)


__all__ = [
    "DEFAULT_COLOR",
    "PrivacyMaskRegion",
    "PrivacyRegion",
    "MaskRegion",
    "PrivacyMask",
    "Mask",
    "normalize_region",
    "normalize_regions",
    "validate_regions",
    "set_masks",
    "get_masks",
    "list_masks",
    "clear_masks",
    "clear_all",
    "add_mask",
    "remove_mask",
    "has_masks",
    "apply_privacy_masks",
    "apply_privacy_masks_to_jpeg",
    "decode_jpeg_bytes",
    "encode_jpeg_bytes",
    "apply_masks",
    "mask_frame",
    "blackout_regions",
    "apply_blackout",
    "mask_snapshot",
    "mask_mjpeg_frame",
    "get_masked_frame",
    "apply_masks_to_jpeg",
    "mask_jpeg",
    "mask_jpeg_bytes",
    "set_privacy_masks",
    "get_privacy_masks",
]
