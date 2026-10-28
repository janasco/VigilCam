"""Mask-image to polygon-zone import service.

Converts a binary/grayscale mask image (e.g. a white-on-black PNG painted
in an external editor) into a list of polygon zone dicts compatible with
:mod:`app.services.motion_zoning` (``PolygonZone`` / ``MotionZoningEngine``)
and :mod:`app.services.zone_templates` (normalized 0-1 coordinates).

This fills the gap between:

- :mod:`app.services.motion_zoning` — freehand polygon zones where callers
  supply arbitrary ``points``, and
- :mod:`app.services.zone_templates` — named shape presets (full / entry /
  exit / driveway / perimeter).

Neither of those accepts an image mask; this module does::

    from app.services import mask_import as mi
    zones = mi.mask_bytes_to_zones(png_bytes)  # normalized 0-1 polygons

White (or bright) pixels are treated as the monitored area. Each connected
component becomes one ``include`` polygon zone (use ``mode="exclude"`` to
invert the meaning). Contours are extracted with OpenCV and simplified
with ``approxPolyDP``.
"""

from __future__ import annotations

import base64
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np

DEFAULT_THRESHOLD = 127
DEFAULT_MIN_AREA = 25
DEFAULT_EPSILON_RATIO = 0.01
DEFAULT_MAX_POLYGONS = 16
DEFAULT_MODE = "include"
DEFAULT_THRESHOLD_PERCENTAGE = 5.0
DEFAULT_NAME_PREFIX = "mask"


# ---------------------------------------------------------------------------
# Decoding helpers
# ---------------------------------------------------------------------------

def _decode_base64_string(value: str) -> Optional[bytes]:
    s = (value or "").strip()
    if not s:
        return None
    if s.startswith("data:") and "," in s:
        s = s.split(",", 1)[1]
    s = "".join(s.split())
    # Pad if needed.
    pad = len(s) % 4
    if pad:
        s += "=" * (4 - pad)
    try:
        return base64.b64decode(s, validate=False)
    except Exception:
        return None


def decode_mask_image(data: Any) -> Optional[np.ndarray]:
    """Decode raw bytes (PNG/JPEG/BMP/...) to a BGR ``np.ndarray``.

    Returns ``None`` when the payload cannot be decoded as an image.
    Accepts ``bytes``/``bytearray``, base64 ``str`` (plain or data-URL),
    ``np.ndarray`` (passed through), and PIL images.
    """
    if data is None:
        return None
    if isinstance(data, np.ndarray):
        return data
    if isinstance(data, str):
        raw = _decode_base64_string(data)
        if raw is None:
            return None
        data = raw
    if hasattr(data, "read"):
        try:
            data = data.read()
        except Exception:
            return None
    # PIL image?
    if hasattr(data, "convert") and hasattr(data, "size"):
        try:
            import PIL.Image  # local import so cv2-only envs still work

            arr = np.array(data.convert("RGB"))
            if arr.size == 0:
                return None
            return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
        except Exception:
            return None
    if isinstance(data, (bytes, bytearray, memoryview)):
        buf = bytes(data)
        if not buf:
            return None
        # Quick magic check: reject obvious non-images early (still try decode).
        try:
            arr = np.frombuffer(buf, dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            return img
        except Exception:
            return None
    if isinstance(data, (list, tuple)):
        try:
            arr = np.asarray(data, dtype=np.uint8)
            if arr.size == 0:
                return None
            if arr.ndim in (2, 3):
                return arr
        except Exception:
            return None
        return None
    return None


def decode_image(data: Any) -> Optional[np.ndarray]:
    """Alias of :func:`decode_mask_image`."""
    return decode_mask_image(data)


def decode_mask(data: Any) -> Optional[np.ndarray]:
    """Alias of :func:`decode_mask_image`."""
    return decode_mask_image(data)


def _to_grayscale(img: np.ndarray) -> np.ndarray:
    if img.ndim == 2:
        return img
    if img.ndim == 3:
        if img.shape[2] == 4:
            return cv2.cvtColor(img, cv2.COLOR_BGRA2GRAY)
        if img.shape[2] == 3:
            return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        return img[:, :, 0]
    return np.asarray(img)


def binarize_mask(
    gray: Any,
    threshold: Any = DEFAULT_THRESHOLD,
    invert: Any = False,
) -> np.ndarray:
    """Threshold a grayscale image to a 0/255 binary mask."""
    try:
        thr = int(float(threshold))
    except (TypeError, ValueError):
        thr = DEFAULT_THRESHOLD
    thr = max(0, min(255, thr))
    arr = np.asarray(gray)
    if arr.size == 0:
        return np.zeros((1, 1), dtype=np.uint8)
    if arr.dtype != np.uint8:
        arr = np.clip(arr, 0, 255).astype(np.uint8)
    if arr.ndim == 3:
        arr = _to_grayscale(arr)
    _, binary = cv2.threshold(arr, thr, 255, cv2.THRESH_BINARY)
    inv = str(invert).strip().lower() in ("1", "true", "yes", "on") if isinstance(invert, str) else bool(invert)
    if inv:
        binary = cv2.bitwise_not(binary)
    return binary


def binarize(gray: Any, threshold: Any = DEFAULT_THRESHOLD, invert: Any = False) -> np.ndarray:
    """Alias of :func:`binarize_mask`."""
    return binarize_mask(gray, threshold=threshold, invert=invert)


def to_binary(gray: Any, threshold: Any = DEFAULT_THRESHOLD, invert: Any = False) -> np.ndarray:
    """Alias of :func:`binarize_mask`."""
    return binarize_mask(gray, threshold=threshold, invert=invert)


# ---------------------------------------------------------------------------
# Contour -> polygon conversion
# ---------------------------------------------------------------------------

def _coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "y", "on", "normalized", "norm"):
        return True
    if s in ("0", "false", "no", "n", "off", "pixels", "pixel", "absolute"):
        return False
    return default


def _coerce_float(value: Any, default: float) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_int(value: Any, default: int) -> int:
    try:
        if value is None:
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def contours_to_zones(
    contours: Any,
    image_width: int,
    image_height: int,
    *,
    min_area: Any = DEFAULT_MIN_AREA,
    epsilon_ratio: Any = DEFAULT_EPSILON_RATIO,
    max_polygons: Any = DEFAULT_MAX_POLYGONS,
    normalize: Any = True,
    normalized: Any = None,
    mode: Any = DEFAULT_MODE,
    threshold_percentage: Any = DEFAULT_THRESHOLD_PERCENTAGE,
    name_prefix: Any = DEFAULT_NAME_PREFIX,
    out_width: Any = None,
    out_height: Any = None,
    sort_by_area: Any = True,
) -> List[Dict[str, Any]]:
    """Convert OpenCV contours to polygon zone dicts."""
    w = max(1, int(image_width))
    h = max(1, int(image_height))
    try:
        min_a = float(min_area)
    except (TypeError, ValueError):
        min_a = float(DEFAULT_MIN_AREA)
    try:
        eps_ratio = float(epsilon_ratio)
    except (TypeError, ValueError):
        eps_ratio = float(DEFAULT_EPSILON_RATIO)
    if eps_ratio < 0:
        eps_ratio = 0.0
    try:
        max_n = int(float(max_polygons)) if max_polygons is not None else DEFAULT_MAX_POLYGONS
    except (TypeError, ValueError):
        max_n = DEFAULT_MAX_POLYGONS
    if normalized is not None:
        norm = _coerce_bool(normalized, True)
    else:
        norm = _coerce_bool(normalize, True)
    try:
        thr_pct = float(threshold_percentage)
    except (TypeError, ValueError):
        thr_pct = float(DEFAULT_THRESHOLD_PERCENTAGE)
    prefix = str(name_prefix or DEFAULT_NAME_PREFIX)
    mode_s = str(mode or DEFAULT_MODE).strip().lower()
    if mode_s not in ("include", "exclude"):
        mode_s = DEFAULT_MODE

    # Resolve output pixel dims when returning absolute coordinates.
    ow = _coerce_int(out_width, w) if out_width is not None else w
    oh = _coerce_int(out_height, h) if out_height is not None else h
    ow = max(1, ow)
    oh = max(1, oh)

    scored: List[Tuple[float, List[List[float]]]] = []
    for cnt in list(contours or []):
        try:
            area = float(cv2.contourArea(cnt))
        except Exception:
            continue
        if area < min_a:
            continue
        try:
            peri = float(cv2.arcLength(cnt, True))
        except Exception:
            peri = 0.0
        eps = max(1.0, eps_ratio * peri) if peri > 0 else 1.0
        if eps_ratio <= 0:
            approx = cnt.reshape(-1, 2)
        else:
            try:
                approx = cv2.approxPolyDP(cnt, eps, True).reshape(-1, 2)
            except Exception:
                continue
        if len(approx) < 3:
            continue
        pts: List[List[float]] = []
        for x, y in approx:
            fx = float(x)
            fy = float(y)
            if norm:
                pts.append([round(fx / max(1.0, float(w - 1)) if w > 1 else 0.0, 6),
                            round(fy / max(1.0, float(h - 1)) if h > 1 else 0.0, 6)])
            else:
                # Scale to requested output dims if they differ from mask dims.
                sx = fx * (float(ow) / float(w)) if ow != w else fx
                sy = fy * (float(oh) / float(h)) if oh != h else fy
                pts.append([round(sx, 3), round(sy, 3)])
        # Clamp normalized coords.
        if norm:
            pts = [[min(1.0, max(0.0, px)), min(1.0, max(0.0, py))] for px, py in pts]
        scored.append((area, pts))

    if sort_by_area:
        scored.sort(key=lambda t: t[0], reverse=True)
    if max_n is not None and max_n >= 0:
        scored = scored[:max_n]

    zones: List[Dict[str, Any]] = []
    for i, (area, pts) in enumerate(scored):
        zones.append({
            "points": pts,
            "mode": mode_s,
            "threshold_percentage": thr_pct,
            "threshold": thr_pct,
            "name": f"{prefix}-{i + 1}",
            "area": float(area),
            "area_ratio": float(area) / float(max(1, w * h)),
        })
    return zones


def binary_to_zones(
    binary: Any,
    **kwargs: Any,
) -> List[Dict[str, Any]]:
    """Convert a 0/255 binary mask array to polygon zone dicts."""
    arr = np.asarray(binary)
    if arr.size == 0:
        return []
    if arr.ndim == 3:
        arr = _to_grayscale(arr)
    if arr.dtype != np.uint8:
        arr = np.clip(arr, 0, 255).astype(np.uint8)
    # Ensure binary.
    _, bw = cv2.threshold(arr, 127, 255, cv2.THRESH_BINARY)
    # Re-threshold only if caller passed explicit threshold? Handled upstream.
    # Detect the common case of a fully empty mask quickly.
    if int(np.count_nonzero(bw)) == 0:
        return []
    h, w = bw.shape[:2]
    # Allow caller override of binarization threshold via kwargs remap.
    find = cv2.findContours(bw, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    contours = find[0] if len(find) == 2 else find[1]
    # Map friendly option aliases.
    opts = _normalize_options(kwargs)
    return contours_to_zones(contours, w, h, **opts)


def _normalize_options(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Map tolerant option aliases to canonical contour kwargs."""
    out: Dict[str, Any] = {}
    # min area
    for k in ("min_area", "minArea", "min_blob_area", "minBlobArea", "area_min"):
        if kwargs.get(k) is not None:
            out["min_area"] = kwargs[k]
            break
    else:
        # ratio-based minimum
        for k in ("min_area_ratio", "minAreaRatio", "area_ratio_min"):
            if kwargs.get(k) is not None:
                try:
                    ratio = float(kwargs[k])
                    img_w = _coerce_int(kwargs.get("image_width", kwargs.get("width")), 0)
                    img_h = _coerce_int(kwargs.get("image_height", kwargs.get("height")), 0)
                    if img_w and img_h:
                        out["min_area"] = ratio * img_w * img_h
                    else:
                        out["min_area"] = DEFAULT_MIN_AREA
                except (TypeError, ValueError):
                    out["min_area"] = DEFAULT_MIN_AREA
                break
    for k in ("epsilon_ratio", "epsilon", "eps", "approx_eps", "approxEps", "simplify", "epsilonRatio"):
        if kwargs.get(k) is not None:
            out["epsilon_ratio"] = kwargs[k]
            break
    for k in ("max_polygons", "maxPolygons", "max_zones", "maxZones", "max", "limit", "count"):
        if kwargs.get(k) is not None:
            out["max_polygons"] = kwargs[k]
            break
    norm = None
    for k in ("normalize", "normalized", "norm", "as_normalized"):
        if kwargs.get(k) is not None:
            norm = kwargs[k]
            break
    for k in ("pixels", "absolute", "as_pixels", "pixel_coords"):
        if kwargs.get(k) is not None:
            # pixels=True means normalize=False
            pv = _coerce_bool(kwargs[k], False)
            if pv:
                norm = False
            break
    out["normalize"] = norm if norm is not None else True
    for k in ("mode", "zone_mode", "zoneMode"):
        if kwargs.get(k) is not None:
            out["mode"] = kwargs[k]
            break
    for k in ("threshold_percentage", "threshold", "threshold_percent", "threshold_pct", "percent"):
        if kwargs.get(k) is not None:
            out["threshold_percentage"] = kwargs[k]
            break
    for k in ("name_prefix", "namePrefix", "prefix", "name", "label"):
        if kwargs.get(k) is not None:
            out["name_prefix"] = kwargs[k]
            break
    for k in ("out_width", "width", "w", "frame_width", "img_width", "target_width"):
        if kwargs.get(k) is not None and k not in ("width", "w") or out.get("normalize") is False:
            # Only treat width/height as output dims; keep them regardless.
            out["out_width"] = kwargs[k]
            break
    for k in ("out_height", "height", "h", "frame_height", "img_height", "target_height"):
        if kwargs.get(k) is not None:
            out["out_height"] = kwargs[k]
            break
    return out


def mask_array_to_zones(image: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """Convert an image array (BGR/RGB/gray) to polygon zones."""
    threshold = kwargs.pop("threshold", kwargs.pop("thresh", DEFAULT_THRESHOLD))
    invert = kwargs.pop("invert", kwargs.pop("inverted", False))
    if isinstance(image, np.ndarray):
        arr = image
    else:
        decoded = decode_mask_image(image)
        if decoded is None:
            return []
        arr = decoded
    gray = _to_grayscale(arr)
    binary = binarize_mask(gray, threshold=threshold, invert=invert)
    opts = _normalize_options(kwargs)
    # Honor explicit min_area_ratio against this image's size.
    if "min_area" not in opts:
        for k in ("min_area_ratio", "minAreaRatio"):
            if kwargs.get(k) is not None:
                try:
                    opts["min_area"] = float(kwargs[k]) * gray.shape[1] * gray.shape[0]
                except (TypeError, ValueError):
                    pass
    h, w = binary.shape[:2]
    find = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    contours = find[0] if len(find) == 2 else find[1]
    return contours_to_zones(contours, w, h, **opts)


def mask_bytes_to_zones(data: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """Convert raw image bytes (PNG/JPEG/...) to polygon zone dicts.

    Accepts bytes, base64 strings (plain or data-URL), numpy arrays, and
    PIL images. Keyword options: ``threshold``, ``invert``, ``min_area``,
    ``epsilon_ratio``/``epsilon``, ``max_polygons``/``max_zones``,
    ``normalize`` (default True → 0-1 coords), ``mode``, 
    ``threshold_percentage``, ``name_prefix``, ``width``/``height``
    (output pixel dims when ``normalize`` is False).
    """
    # Allow callers to pass the image under a named key.
    if isinstance(data, dict):
        for k in ("image", "mask", "data", "content", "base64", "image_base64",
                  "mask_base64", "image_b64", "mask_b64", "png", "bytes", "file_data"):
            if data.get(k) is not None:
                data = data.get(k)
                break
        # Merge remaining dict keys as options.
        if isinstance(data, dict):
            merged = dict(data)
            merged.update(kwargs)
            kwargs = merged
            data = None
    if data is None:
        # Maybe kwargs themselves carry the image.
        for k in ("image", "mask", "data", "content", "base64", "image_base64",
                  "mask_base64", "image_b64", "mask_b64", "png", "bytes", "file_data"):
            if kwargs.get(k) is not None:
                data = kwargs.pop(k)
                break
    if data is None:
        return []
    img = decode_mask_image(data)
    if img is None:
        raise ValueError("Could not decode mask image bytes")
    return mask_array_to_zones(img, **kwargs)


def mask_base64_to_zones(value: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """Convert a base64 (or data-URL) mask string to polygon zones."""
    raw = _decode_base64_string(str(value)) if isinstance(value, str) else None
    if raw is None and isinstance(value, str):
        # Maybe it is already raw bytes encoded as text? Fail clearly.
        raise ValueError("Could not decode base64 mask image")
    if raw is None:
        raw = value  # type: ignore[assignment]
    return mask_bytes_to_zones(raw, **kwargs)


def base64_to_zones(value: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """Alias of :func:`mask_base64_to_zones`."""
    return mask_base64_to_zones(value, **kwargs)


# Aliases covering likely caller vocabularies.
def mask_to_zones(data: Any = None, **kwargs: Any) -> List[Dict[str, Any]]:
    if data is None:
        for k in ("image", "mask", "data", "content", "base64", "bytes"):
            if kwargs.get(k) is not None:
                data = kwargs.pop(k)
                break
    return mask_bytes_to_zones(data, **kwargs)


def mask_to_polygons(data: Any = None, **kwargs: Any) -> List[Dict[str, Any]]:
    return mask_to_zones(data, **kwargs)


def mask_to_polygon_zones(data: Any = None, **kwargs: Any) -> List[Dict[str, Any]]:
    return mask_to_zones(data, **kwargs)


def import_mask(data: Any = None, **kwargs: Any) -> List[Dict[str, Any]]:
    return mask_to_zones(data, **kwargs)


def import_mask_image(data: Any = None, **kwargs: Any) -> List[Dict[str, Any]]:
    return mask_to_zones(data, **kwargs)


def parse_mask(data: Any = None, **kwargs: Any) -> List[Dict[str, Any]]:
    return mask_to_zones(data, **kwargs)


def parse_mask_image(data: Any = None, **kwargs: Any) -> List[Dict[str, Any]]:
    return mask_to_zones(data, **kwargs)


def convert_mask(data: Any = None, **kwargs: Any) -> List[Dict[str, Any]]:
    return mask_to_zones(data, **kwargs)


def convert_mask_image(data: Any = None, **kwargs: Any) -> List[Dict[str, Any]]:
    return mask_to_zones(data, **kwargs)


def process_mask(data: Any = None, **kwargs: Any) -> List[Dict[str, Any]]:
    return mask_to_zones(data, **kwargs)


def extract_zones(data: Any = None, **kwargs: Any) -> List[Dict[str, Any]]:
    return mask_to_zones(data, **kwargs)


def extract_polygons(data: Any = None, **kwargs: Any) -> List[Dict[str, Any]]:
    return mask_to_zones(data, **kwargs)


def mask_bytes_to_polygons(data: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    return mask_bytes_to_zones(data, **kwargs)


def mask_array_to_polygons(image: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    return mask_array_to_zones(image, **kwargs)


def to_polygon_zones(data: Any = None, **kwargs: Any) -> List[Any]:
    """Return zones as ``PolygonZone`` objects (lazy import)."""
    from app.services.motion_zoning import PolygonZone

    zones = mask_to_zones(data, **kwargs)
    return [PolygonZone(**z) for z in zones]


def to_motion_zones(data: Any = None, **kwargs: Any) -> List[Any]:
    """Alias of :func:`to_polygon_zones`."""
    return to_polygon_zones(data, **kwargs)


def to_zones(data: Any = None, **kwargs: Any) -> List[Dict[str, Any]]:
    """Alias of :func:`mask_to_zones`."""
    return mask_to_zones(data, **kwargs)


def get_zones(data: Any = None, **kwargs: Any) -> List[Dict[str, Any]]:
    """Alias of :func:`mask_to_zones`."""
    return mask_to_zones(data, **kwargs)


def build_zones(data: Any = None, **kwargs: Any) -> List[Dict[str, Any]]:
    """Alias of :func:`mask_to_zones`."""
    return mask_to_zones(data, **kwargs)


__all__ = [
    "DEFAULT_THRESHOLD",
    "DEFAULT_MIN_AREA",
    "DEFAULT_EPSILON_RATIO",
    "DEFAULT_MAX_POLYGONS",
    "DEFAULT_MODE",
    "DEFAULT_THRESHOLD_PERCENTAGE",
    "DEFAULT_NAME_PREFIX",
    "decode_mask_image",
    "decode_image",
    "decode_mask",
    "binarize_mask",
    "binarize",
    "to_binary",
    "contours_to_zones",
    "binary_to_zones",
    "mask_array_to_zones",
    "mask_bytes_to_zones",
    "mask_base64_to_zones",
    "base64_to_zones",
    "mask_to_zones",
    "mask_to_polygons",
    "mask_to_polygon_zones",
    "import_mask",
    "import_mask_image",
    "parse_mask",
    "parse_mask_image",
    "convert_mask",
    "convert_mask_image",
    "process_mask",
    "extract_zones",
    "extract_polygons",
    "mask_bytes_to_polygons",
    "mask_array_to_polygons",
    "to_polygon_zones",
    "to_motion_zones",
    "to_zones",
    "get_zones",
    "build_zones",
]
