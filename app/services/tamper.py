"""Camera tamper detection baseline for VigilCam.

Detects camera tampering (covered lens, moved camera, spray-paint / blackout)
by comparing a live frame's grayscale histogram against a stored reference
frame histogram per camera.

This is a dependency-light baseline:

* reference frame store: in-memory per-camera ``{histogram, captured_at}``.
* histogram drift compare: normalized histograms (default 32 bins) compared
  with total-variation distance ``0.5 * sum|h1 - h2|`` in ``[0, 1]``.

Frames are accepted in many shapes (raw bytes, lists of pixel values,
base64 strings, dicts carrying ``pixels``/``frame``/``histogram``, numpy
arrays, PIL images) so the HTTP layer and future ingestors can share it.

Standard library only; numpy/PIL are used opportunistically when present.
"""

from __future__ import annotations

import base64
import math
import threading
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


DEFAULT_BINS = 32
DEFAULT_THRESHOLD = 0.35
DARK_MEAN = 8.0
BRIGHT_MEAN = 248.0

_CONFIG: Dict[str, Any] = {
    "threshold": float(DEFAULT_THRESHOLD),
    "bins": int(DEFAULT_BINS),
    "dark_mean": float(DARK_MEAN),
    "bright_mean": float(BRIGHT_MEAN),
}

# camera_id -> {"histogram": [...], "bins": int, "num_pixels": int,
#               "mean": float, "captured_at": iso, "captured_ts": float}
_REFERENCES: Dict[int, Dict[str, Any]] = {}

_LOCK = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    try:
        return dt.isoformat()
    except Exception:  # noqa: BLE001
        return None


def _coerce_camera_id(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    try:
        if hasattr(value, "id") and not isinstance(value, (int, str, float)):
            value = getattr(value, "id")
        return int(value)
    except (TypeError, ValueError):
        try:
            s = str(value).strip()
        except Exception:  # noqa: BLE001
            return None
        if not s:
            return None
        import re

        m = re.search(r"(\d+)$", s)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                return None
        return None


def _coerce_threshold(value: Any, default: float) -> float:
    if value is None:
        return float(default)
    try:
        if isinstance(value, bool):
            return float(default)
        v = float(value)
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(v):
        return float(default)
    return max(0.0, min(1.0, v))


def _coerce_bins(value: Any, default: int = DEFAULT_BINS) -> int:
    if value is None:
        return int(default)
    try:
        if isinstance(value, bool):
            return int(default)
        v = int(value)
    except (TypeError, ValueError):
        return int(default)
    return max(4, min(256, v))


def _flatten(values: Any, depth: int = 0) -> List[float]:
    out: List[float] = []
    if values is None:
        return out
    if isinstance(values, (bytes, bytearray)):
        return [float(b) for b in values]
    if isinstance(values, str):
        s = values.strip()
        if not s:
            return out
        # Try base64 first, then comma/space separated numbers.
        try:
            raw = base64.b64decode(s, validate=False)
            if raw:
                return [float(b) for b in raw]
        except Exception:  # noqa: BLE001
            pass
        parts = s.replace(",", " ").split()
        for p in parts:
            try:
                out.append(float(p))
            except ValueError:
                continue
        return out
    if isinstance(values, (list, tuple)):
        for item in values:
            if isinstance(item, (list, tuple)) and depth < 2:
                out.extend(_flatten(item, depth + 1))
            else:
                try:
                    if isinstance(item, bool):
                        continue
                    out.append(float(item))  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    continue
        return out
    # numpy arrays / PIL images / generic sequences.
    try:
        import numpy as _np  # type: ignore

        if isinstance(values, _np.ndarray):
            try:
                flat = values.ravel().tolist()
                return _flatten(flat, depth + 1)
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass
    for attr in ("tobytes", "getdata", "tolist", "ravel"):
        fn = getattr(values, attr, None)
        if callable(fn):
            try:
                conv = fn()
                if isinstance(conv, (bytes, bytearray)):
                    return [float(b) for b in conv]
                return _flatten(list(conv), depth + 1)
            except Exception:  # noqa: BLE001
                continue
    try:
        return _flatten(list(values), depth + 1)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001
        pass
    try:
        return [float(values)]  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return out


def _frame_to_pixels(frame: Any) -> Optional[List[float]]:
    """Normalize an arbitrary frame payload to grayscale 0-255 samples."""
    if frame is None:
        return None
    if isinstance(frame, dict):
        # Direct histogram passthrough handled by callers; here extract pixels.
        for key in ("pixels", "data", "frame", "image", "values", "grayscale",
                    "gray", "samples", "bytes", "raw", "content"):
            if frame.get(key) is not None:
                pix = _frame_to_pixels(frame.get(key))
                if pix:
                    return pix
        for key in ("base64", "b64", "base64_frame", "image_base64"):
            if frame.get(key) is not None:
                pix = _frame_to_pixels(frame.get(key))
                if pix:
                    return pix
        # Single scalar mean + count -> synthesize uniform samples.
        if frame.get("mean") is not None and frame.get("num_pixels") is not None:
            try:
                m = max(0.0, min(255.0, float(frame["mean"])))
                n = max(1, int(frame["num_pixels"]))
                return [m] * min(n, 4096)
            except (TypeError, ValueError):
                pass
        return None
    if isinstance(frame, (bytes, bytearray)):
        if not frame:
            return None
        return [float(b) for b in frame]
    if isinstance(frame, str):
        vals = _flatten(frame)
        return vals or None
    if isinstance(frame, (list, tuple)):
        vals = _flatten(frame)
        return vals or None
    # Objects with image-like helpers (PIL Image, numpy array, etc.).
    for attr in ("histogram",):
        _ = attr
    vals = _flatten(frame)
    return vals or None


def _pixels_to_histogram(pixels: List[float], bins: int) -> List[float]:
    n = max(1, len(pixels))
    hist = [0.0] * int(bins)
    width = 256.0 / float(bins)
    for p in pixels:
        try:
            v = float(p)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(v):
            continue
        v = max(0.0, min(255.999, v))
        idx = int(v / width)
        if idx < 0:
            idx = 0
        elif idx >= int(bins):
            idx = int(bins) - 1
        hist[idx] += 1.0
    total = sum(hist) or 1.0
    return [h / total for h in hist]


def compute_histogram(frame: Any, bins: Any = None) -> Optional[List[float]]:
    """Compute a normalized grayscale histogram for ``frame``.

    Returns ``None`` when no pixel data can be extracted. Accepts raw
    bytes, pixel lists, base64 strings, dicts, numpy arrays, PIL images.
    """
    nbins = _coerce_bins(bins, _CONFIG.get("bins", DEFAULT_BINS))
    if isinstance(frame, dict) and frame.get("histogram") is not None:
        try:
            h = [float(x) for x in frame["histogram"]]  # type: ignore[union-attr]
            if not h:
                return None
            # Resample to requested bin count when lengths differ.
            if len(h) != nbins:
                h = _resample_histogram(h, nbins)
            total = sum(h) or 1.0
            return [x / total for x in h]
        except (TypeError, ValueError):
            pass
    pixels = _frame_to_pixels(frame)
    if not pixels:
        return None
    return _pixels_to_histogram(pixels, nbins)


def histogram_mean_from_pixels(frame: Any) -> Optional[float]:
    pixels = _frame_to_pixels(frame)
    if not pixels:
        return None
    vals = [v for v in pixels if math.isfinite(float(v))]
    if not vals:
        return None
    return sum(float(v) for v in vals) / len(vals)


def _resample_histogram(hist: List[float], bins: int) -> List[float]:
    if len(hist) == bins:
        return list(hist)
    if not hist or bins <= 0:
        return [0.0] * max(1, bins)
    out = [0.0] * bins
    for i, mass in enumerate(hist):
        # Map old bin center onto new bins proportionally.
        pos = (i + 0.5) * bins / len(hist) - 0.5
        lo = int(math.floor(pos))
        frac = pos - lo
        if 0 <= lo < bins:
            out[lo] += mass * (1.0 - frac)
        if 0 <= lo + 1 < bins:
            out[lo + 1] += mass * frac
    return out


def compare_histograms(h1: Any, h2: Any) -> Optional[float]:
    """Total-variation drift between two normalized histograms in [0, 1]."""
    try:
        a = [float(x) for x in h1 or []]
        b = [float(x) for x in h2 or []]
    except (TypeError, ValueError):
        return None
    if not a or not b:
        return None
    if len(a) != len(b):
        longer, shorter = (a, b) if len(a) > len(b) else (b, a)
        shorter = _resample_histogram(shorter, len(longer))
        if len(a) > len(b):
            a, b = longer, shorter
        else:
            a, b = shorter, longer
    sa = sum(a) or 1.0
    sb = sum(b) or 1.0
    a = [x / sa for x in a]
    b = [x / sb for x in b]
    return 0.5 * sum(abs(x - y) for x, y in zip(a, b))


# Alias names for the drift primitive.
histogram_distance = compare_histograms
histogram_drift = compare_histograms
compare = compare_histograms


def set_reference(camera_id: Any, frame: Any = None, bins: Any = None, **kwargs: Any) -> Dict[str, Any]:
    """Store ``frame``'s histogram as the tamper baseline for a camera."""
    cid = _coerce_camera_id(camera_id if camera_id is not None else kwargs.get("camera"))
    if cid is None:
        cid = _coerce_camera_id(kwargs.get("camera_name") or kwargs.get("id"))
    if cid is None:
        raise ValueError("camera_id is required")
    if frame is None:
        for key in ("pixels", "data", "image", "frame_data", "histogram",
                    "values", "samples", "bytes", "raw", "content", "base64"):
            if kwargs.get(key) is not None:
                frame = kwargs.get(key)
                break
        if frame is None and kwargs:
            # Allow passing the whole payload dict as the frame.
            frame = dict(kwargs)
    nbins = _coerce_bins(
        bins if bins is not None else kwargs.get("bins", _CONFIG.get("bins", DEFAULT_BINS))
    )
    hist = compute_histogram(frame, nbins)
    if hist is None:
        raise ValueError("no pixel data found in frame payload")
    pixels = _frame_to_pixels(frame if not isinstance(frame, dict) or frame.get("histogram") is None else None)
    mean: Optional[float] = None
    if pixels:
        try:
            mean = sum(float(v) for v in pixels) / max(1, len(pixels))
        except (TypeError, ValueError):
            mean = None
    elif isinstance(frame, dict) and frame.get("mean") is not None:
        try:
            mean = float(frame["mean"])
        except (TypeError, ValueError):
            mean = None
    now = _now()
    record = {
        "camera_id": cid,
        "id": cid,
        "histogram": hist,
        "bins": nbins,
        "num_pixels": len(pixels) if pixels else None,
        "num_bins": nbins,
        "mean": mean,
        "mean_brightness": mean,
        "captured_at": _iso(now),
        "captured_ts": now.timestamp(),
        "updated_at": _iso(now),
        "ok": True,
        "success": True,
    }
    with _LOCK:
        _REFERENCES[cid] = deepcopy(record)
        return deepcopy(_REFERENCES[cid])


def get_reference(camera_id: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    cid = _coerce_camera_id(camera_id if camera_id is not None else kwargs.get("camera_id", kwargs.get("id")))
    if cid is None:
        return None
    with _LOCK:
        rec = _REFERENCES.get(cid)
        return deepcopy(rec) if rec is not None else None


def has_reference(camera_id: Any) -> bool:
    return get_reference(camera_id) is not None


def clear_reference(camera_id: Any = None, **kwargs: Any) -> Any:
    """Clear one camera baseline, or all when no id is given."""
    if camera_id is None:
        camera_id = kwargs.get("camera_id", kwargs.get("id", kwargs.get("camera")))
    if camera_id is None and not kwargs:
        with _LOCK:
            _REFERENCES.clear()
        return {"ok": True, "cleared": True, "count": 0}
    cid = _coerce_camera_id(camera_id)
    if cid is None:
        with _LOCK:
            _REFERENCES.clear()
        return {"ok": True, "cleared": True, "count": 0}
    with _LOCK:
        removed = _REFERENCES.pop(cid, None) is not None
    return {"ok": True, "cleared": bool(removed), "removed": bool(removed), "camera_id": cid}


def list_references() -> List[Dict[str, Any]]:
    with _LOCK:
        return [deepcopy(v) for _, v in sorted(_REFERENCES.items())]


def compare_frame(
    camera_id: Any = None,
    frame: Any = None,
    threshold: Any = None,
    bins: Any = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Compare ``frame`` against the stored baseline for ``camera_id``.

    Returns ``{"tampered": bool, "drift": float, ...}``. When no baseline
    exists the result reports ``"compared": False`` and ``tampered: False``
    rather than raising, so callers can prompt for baseline capture.
    """
    if camera_id is None:
        camera_id = kwargs.get("camera_id", kwargs.get("id", kwargs.get("camera")))
    cid = _coerce_camera_id(camera_id)
    if cid is None:
        raise ValueError("camera_id is required")
    if frame is None:
        for key in ("pixels", "data", "image", "frame_data", "histogram",
                    "values", "samples", "bytes", "raw", "content", "base64", "frame"):
            if kwargs.get(key) is not None:
                frame = kwargs.get(key)
                break
    if threshold is None:
        for key in ("threshold", "drift_threshold", "sensitivity", "limit", "tolerance"):
            if kwargs.get(key) is not None:
                threshold = kwargs.get(key)
                break
    thr = _coerce_threshold(threshold, _CONFIG.get("threshold", DEFAULT_THRESHOLD))
    ref = get_reference(cid)
    if ref is None:
        return {
            "camera_id": cid,
            "id": cid,
            "tampered": False,
            "tamper": False,
            "tamper_detected": False,
            "drift": None,
            "distance": None,
            "threshold": thr,
            "compared": False,
            "has_reference": False,
            "reason": "no baseline reference stored",
            "ok": True,
            "success": True,
        }
    nbins = _coerce_bins(bins if bins is not None else kwargs.get("bins", ref.get("bins", DEFAULT_BINS)))
    hist = compute_histogram(frame, nbins)
    if hist is None:
        return {
            "camera_id": cid,
            "id": cid,
            "tampered": False,
            "tamper": False,
            "tamper_detected": False,
            "drift": None,
            "distance": None,
            "threshold": thr,
            "compared": False,
            "has_reference": True,
            "reason": "no pixel data in frame payload",
            "ok": False,
            "success": False,
            "error": "no pixel data in frame payload",
        }
    ref_hist = ref.get("histogram") or []
    drift = compare_histograms(ref_hist, _resample_histogram(hist, len(ref_hist)))
    if drift is None:
        drift = 1.0
    tampered = bool(drift > thr)
    reason = "drift above threshold" if tampered else "within baseline"
    # Heuristic overrides: fully dark / fully bright frames almost always
    # mean a covered or blinded lens even when histograms partially overlap.
    mean = histogram_mean_from_pixels(frame)
    dark_at = float(_CONFIG.get("dark_mean", DARK_MEAN))
    bright_at = float(_CONFIG.get("bright_mean", BRIGHT_MEAN))
    if mean is not None and (mean <= dark_at or mean >= bright_at):
        tampered = True
        reason = "covered lens (extreme brightness)" if mean <= dark_at else "blinded lens (extreme brightness)"
    return {
        "camera_id": cid,
        "id": cid,
        "tampered": tampered,
        "tamper": tampered,
        "tamper_detected": tampered,
        "drift": float(drift),
        "distance": float(drift),
        "score": float(drift),
        "threshold": float(thr),
        "mean": mean,
        "mean_brightness": mean,
        "reference_mean": ref.get("mean"),
        "compared": True,
        "has_reference": True,
        "reason": reason,
        "reference_captured_at": ref.get("captured_at"),
        "ok": True,
        "success": True,
    }


def get_status(camera_id: Any = None, **kwargs: Any) -> Dict[str, Any]:
    """Baseline presence + last compare metadata for one camera (no frame)."""
    cid = _coerce_camera_id(camera_id if camera_id is not None else kwargs.get("camera_id", kwargs.get("id")))
    if cid is None:
        raise ValueError("camera_id is required")
    ref = get_reference(cid)
    return {
        "camera_id": cid,
        "id": cid,
        "has_reference": ref is not None,
        "has_baseline": ref is not None,
        "reference": ref,
        "baseline": ref,
        "threshold": float(_CONFIG.get("threshold", DEFAULT_THRESHOLD)),
        "bins": int(_CONFIG.get("bins", DEFAULT_BINS)),
        "ok": True,
        "success": True,
    }


def get_summary() -> Dict[str, Any]:
    refs = list_references()
    return {
        "cameras": len(refs),
        "camera_count": len(refs),
        "total": len(refs),
        "baselines": len(refs),
        "references": len(refs),
        "threshold": float(_CONFIG.get("threshold", DEFAULT_THRESHOLD)),
        "bins": int(_CONFIG.get("bins", DEFAULT_BINS)),
        "ok": True,
        "success": True,
    }


def get_config() -> Dict[str, Any]:
    return {
        "threshold": float(_CONFIG.get("threshold", DEFAULT_THRESHOLD)),
        "drift_threshold": float(_CONFIG.get("threshold", DEFAULT_THRESHOLD)),
        "bins": int(_CONFIG.get("bins", DEFAULT_BINS)),
        "num_bins": int(_CONFIG.get("bins", DEFAULT_BINS)),
        "dark_mean": float(_CONFIG.get("dark_mean", DARK_MEAN)),
        "bright_mean": float(_CONFIG.get("bright_mean", BRIGHT_MEAN)),
    }


def configure(**kwargs: Any) -> Dict[str, Any]:
    for key in ("threshold", "drift_threshold", "sensitivity", "limit", "tolerance"):
        if kwargs.get(key) is not None:
            _CONFIG["threshold"] = _coerce_threshold(kwargs[key], _CONFIG["threshold"])
            break
    for key in ("bins", "num_bins", "histogram_bins"):
        if kwargs.get(key) is not None:
            _CONFIG["bins"] = _coerce_bins(kwargs[key], _CONFIG["bins"])
            break
    return get_config()


def clear(camera_id: Any = None, **kwargs: Any) -> Any:
    return clear_reference(camera_id, **kwargs)


# Compatibility aliases.
store_reference = set_reference
save_reference = set_reference
capture_reference = set_reference
capture_baseline = set_reference
set_baseline = set_reference
get_baseline = get_reference
has_baseline = has_reference
clear_baseline = clear_reference
remove_reference = clear_reference
detect_tamper = compare_frame
check_tamper = compare_frame
check = compare_frame
detect = compare_frame
compare_with_reference = compare_frame
status = get_status
summary = get_summary
config = get_config


__all__ = [
    "DEFAULT_BINS",
    "DEFAULT_THRESHOLD",
    "compute_histogram",
    "compare_histograms",
    "histogram_distance",
    "histogram_drift",
    "set_reference",
    "get_reference",
    "has_reference",
    "clear_reference",
    "list_references",
    "compare_frame",
    "detect_tamper",
    "check_tamper",
    "get_status",
    "get_summary",
    "get_config",
    "configure",
    "clear",
]
