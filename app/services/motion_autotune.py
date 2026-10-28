"""Motion auto-tune service.

Complements :mod:`app.services.motion_presets` (static named sensitivity
levels with no scene analysis) with data-driven threshold recommendation:

* analyse sample frames (background / no-motion samples),
* measure pixel noise from consecutive-frame absolute differences
  (or spatial noise for a single frame),
* recommend a pixel-difference ``threshold`` (and ``contour_area``)
  that sits just above the measured noise floor,
* map the recommendation to the nearest static preset for convenience.

Pure function style — no singletons, no side effects.
"""

from __future__ import annotations

import base64
import copy
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    import cv2  # type: ignore
except Exception:  # noqa: BLE001
    cv2 = None  # type: ignore

try:
    import numpy as np
except Exception:  # noqa: BLE001
    np = None  # type: ignore


MIN_THRESHOLD = 10
MAX_THRESHOLD = 60
DEFAULT_MARGIN = 8

_NOISE_METHOD_TEMPORAL = "temporal_diff"
_NOISE_METHOD_SPATIAL = "spatial_proxy"


# ---------------------------------------------------------------------------
# Decoding helpers
# ---------------------------------------------------------------------------

def _decode_one(value: Any) -> Optional[Any]:
    """Decode a single frame value to an ``np.ndarray`` (BGR) or ``None``."""
    if value is None:
        return None
    if np is None:
        return None
    if isinstance(value, np.ndarray):
        return value
    if isinstance(value, (bytes, bytearray)):
        if cv2 is None:
            return None
        buf = np.frombuffer(bytes(value), dtype=np.uint8)
        try:
            return cv2.imdecode(buf, cv2.IMREAD_COLOR)
        except Exception:  # noqa: BLE001
            return None
    s = str(value).strip()
    if not s:
        return None
    if s.startswith("data:") and "," in s:
        s = s.split(",", 1)[1]
    s = "".join(s.split())
    try:
        raw = base64.b64decode(s, validate=False)
    except Exception:  # noqa: BLE001
        return None
    if cv2 is None:
        return None
    buf = np.frombuffer(raw, dtype=np.uint8)
    try:
        return cv2.imdecode(buf, cv2.IMREAD_COLOR)
    except Exception:  # noqa: BLE001
        return None


def decode_frame(value: Any) -> Optional[Any]:
    """Public alias: decode one base64/bytes/ndarray frame."""
    return _decode_one(value)


def _to_gray(frame: Any) -> Optional[Any]:
    if frame is None or np is None:
        return None
    try:
        if not isinstance(frame, np.ndarray):
            return None
        if frame.ndim == 2:
            return frame.astype(np.uint8)
        if frame.ndim == 3 and frame.shape[2] == 1:
            return frame[:, :, 0].astype(np.uint8)
        if frame.ndim == 3 and frame.shape[2] == 4:
            if cv2 is not None:
                return cv2.cvtColor(frame, cv2.COLOR_BGRA2GRAY)
            return frame[:, :, :3].mean(axis=2).astype(np.uint8)
        if frame.ndim == 3 and frame.shape[2] == 3:
            if cv2 is not None:
                return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            return frame.mean(axis=2).astype(np.uint8)
    except Exception:  # noqa: BLE001
        return None
    return None


def coerce_frames(frames: Any) -> List[Any]:
    """Normalise user input to a list of decoded ``np.ndarray`` frames."""
    if frames is None:
        return []
    if np is not None and isinstance(frames, np.ndarray):
        # Single frame array (H, W[, C]) or a batch (N, H, W[, C]).
        if frames.ndim in (2, 3):
            decoded = _to_gray(frames)
            if decoded is not None:
                # Re-expand to BGR-like? Keep gray; callers handle gray.
                return [frames]
            return [frames]
        if frames.ndim == 4:
            return [frames[i] for i in range(frames.shape[0])]
        return [frames]
    if isinstance(frames, (str, bytes, bytearray)):
        single = _decode_one(frames)
        return [single] if single is not None else []
    if isinstance(frames, dict):
        for k in ("frames", "images", "samples", "frames_base64", "images_base64"):
            if frames.get(k) is not None:
                return coerce_frames(frames.get(k))
        # Single-frame dict with image keys.
        for k in ("image_base64", "image", "frame", "frame_base64", "current",
                  "snapshot", "snapshot_base64", "b64", "base64", "data"):
            if frames.get(k) is not None:
                return coerce_frames(frames.get(k))
        return []
    if isinstance(frames, Sequence):
        out: List[Any] = []
        for item in frames:
            if np is not None and isinstance(item, np.ndarray):
                out.append(item)
                continue
            decoded = _decode_one(item)
            if decoded is not None:
                out.append(decoded)
        return out
    decoded = _decode_one(frames)
    return [decoded] if decoded is not None else []


# ---------------------------------------------------------------------------
# Noise statistics
# ---------------------------------------------------------------------------

def _percentile(data: Any, q: float) -> float:
    try:
        return float(np.percentile(data, q))
    except Exception:  # noqa: BLE001
        flat = sorted(float(v) for v in np.ravel(data))
        if not flat:
            return 0.0
        idx = min(len(flat) - 1, max(0, int(round(q / 100.0 * (len(flat) - 1)))))
        return float(flat[idx])


def compute_noise_stats(grays: Sequence[Any]) -> Dict[str, Any]:
    """Compute noise stats from a list of grayscale frames."""
    if np is None:
        raise RuntimeError("numpy is required for motion auto-tune")
    if not grays:
        raise ValueError("no frames provided")
    # Normalise sizes to the first frame.
    try:
        h0, w0 = grays[0].shape[:2]
    except Exception:  # noqa: BLE001
        raise ValueError("invalid frame shape")
    normed: List[Any] = []
    for g in grays:
        try:
            if g.shape[:2] != (h0, w0):
                if cv2 is not None:
                    g = cv2.resize(g, (w0, h0), interpolation=cv2.INTER_NEAREST)
                else:
                    continue
            normed.append(g.astype(np.uint8))
        except Exception:  # noqa: BLE001
            continue
    if not normed:
        raise ValueError("no decodable frames")

    if len(normed) == 1:
        g = normed[0]
        if cv2 is not None:
            try:
                blurred = cv2.GaussianBlur(g, (3, 3), 0)
            except Exception:  # noqa: BLE001
                blurred = g
        else:
            blurred = g
        try:
            diff = np.abs(blurred.astype(np.int16) - g.astype(np.int16)).astype(np.uint8)
        except Exception:  # noqa: BLE001
            diff = np.zeros_like(g)
        flat = diff.ravel().astype(np.float64)
        method = _NOISE_METHOD_SPATIAL
        num_diffs = 0
    else:
        diffs = []
        for prev, curr in zip(normed[:-1], normed[1:]):
            try:
                if cv2 is not None:
                    d = cv2.absdiff(prev, curr)
                else:
                    d = np.abs(prev.astype(np.int16) - curr.astype(np.int16)).astype(np.uint8)
                diffs.append(d)
            except Exception:  # noqa: BLE001
                continue
        if not diffs:
            raise ValueError("could not compute frame differences")
        flat = np.concatenate([d.ravel() for d in diffs]).astype(np.float64)
        method = _NOISE_METHOD_TEMPORAL
        num_diffs = len(diffs)

    stats = {
        "method": method,
        "num_frames": len(normed),
        "num_diffs": num_diffs,
        "height": int(h0),
        "width": int(w0),
        "mean_absdiff": float(flat.mean()) if flat.size else 0.0,
        "std_absdiff": float(flat.std()) if flat.size else 0.0,
        "median_absdiff": _percentile(flat, 50.0) if flat.size else 0.0,
        "p95_absdiff": _percentile(flat, 95.0) if flat.size else 0.0,
        "p99_absdiff": _percentile(flat, 99.0) if flat.size else 0.0,
        "p999_absdiff": _percentile(flat, 99.9) if flat.size else 0.0,
        "max_absdiff": float(flat.max()) if flat.size else 0.0,
    }
    return stats


def analyze_frames(frames: Any, **kwargs: Any) -> Dict[str, Any]:
    """Analyse sample frames and return noise statistics.

    Accepts a list of ``np.ndarray`` frames, base64 strings, or a dict
    carrying ``frames``/``image`` keys (tolerant to API payloads).
    """
    decoded = coerce_frames(frames)
    # Tolerate dict payloads passed straight through.
    if not decoded and isinstance(frames, dict):
        extra = _extract_pair(frames)
        decoded = coerce_frames(extra)
    if not decoded:
        raise ValueError("no decodable frames provided")
    grays = []
    for f in decoded:
        g = _to_gray(f)
        if g is not None:
            grays.append(g)
    if not grays:
        raise ValueError("no decodable frames provided")
    stats = compute_noise_stats(grays)
    stats["ok"] = True
    return stats


def _extract_pair(payload: Dict[str, Any]) -> List[Any]:
    out: List[Any] = []
    for k in ("frames", "images", "samples", "frames_base64"):
        v = payload.get(k)
        if isinstance(v, (list, tuple)) and v:
            out.extend(list(v))
    for k in ("image_base64", "image", "frame", "frame_base64", "current",
              "snapshot", "previous_image_base64", "previous", "prev",
              "prev_image", "previous_image", "before"):
        v = payload.get(k)
        if v is not None and not isinstance(v, (list, tuple)):
            out.append(v)
    return out


# ---------------------------------------------------------------------------
# Recommendation
# ---------------------------------------------------------------------------

def _clamp_int(value: Any, low: int, high: int, default: int) -> int:
    try:
        v = int(round(float(value)))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, v))


def recommend_threshold(stats: Dict[str, Any],
                        margin: float = DEFAULT_MARGIN,
                        minimum: int = MIN_THRESHOLD,
                        maximum: int = MAX_THRESHOLD) -> int:
    """Recommend a pixel-difference threshold just above the noise floor."""
    try:
        margin_f = float(margin)
    except (TypeError, ValueError):
        margin_f = float(DEFAULT_MARGIN)
    margin_f = max(0.0, min(40.0, margin_f))
    base = stats.get("p99_absdiff", stats.get("p95_absdiff", 0.0)) if isinstance(stats, dict) else 0.0
    try:
        base_f = float(base)
    except (TypeError, ValueError):
        base_f = 0.0
    candidate = int(math.ceil(base_f + margin_f))
    return max(int(minimum), min(int(maximum), candidate))


def recommend_contour_area(width: Any, height: Any,
                           stats: Optional[Dict[str, Any]] = None,
                           threshold: Optional[int] = None) -> int:
    """Recommend a minimum contour area scaled by resolution and noise."""
    try:
        w = int(width or 640)
        h = int(height or 480)
    except (TypeError, ValueError):
        w, h = 640, 480
    if w < 1:
        w = 640
    if h < 1:
        h = 480
    pixels = float(w * h)
    ref = 640.0 * 480.0
    scale = pixels / ref
    # Noisier scenes -> larger blobs required to count as motion.
    noise_factor = 1.0
    if isinstance(stats, dict):
        try:
            p99 = float(stats.get("p99_absdiff", 0.0))
        except (TypeError, ValueError):
            p99 = 0.0
        if p99 >= 30.0:
            noise_factor = 2.0
        elif p99 >= 18.0:
            noise_factor = 1.5
        elif p99 >= 8.0:
            noise_factor = 1.2
    base_area = 60.0 * scale * noise_factor
    return _clamp_int(base_area, 10, 2000, 60)


def nearest_preset(threshold: int,
                   contour_area: Optional[int] = None) -> Dict[str, Any]:
    """Map a recommended threshold to the nearest static preset."""
    try:
        from app.services import motion_presets as _mp
        presets = _mp.list_motion_presets()
        best = None
        best_dist = None
        for p in presets:
            if p.get("name") == "off":
                continue
            try:
                d = abs(float(p.get("threshold", 30)) - float(threshold))
            except (TypeError, ValueError):
                continue
            if best is None or d < best_dist:  # type: ignore[operator]
                best, best_dist = p, d
        if best is not None:
            return copy.deepcopy(best)
        return _mp.get_preset_or_default("medium")
    except Exception:  # noqa: BLE001
        return {"name": "medium", "threshold": 30, "contour_area": 100,
                "threshold_percentage": 5.0, "sensitivity": 60}


def autotune(frames: Any = None,
             width: Any = None,
             height: Any = None,
             margin: float = DEFAULT_MARGIN,
             minimum_threshold: int = MIN_THRESHOLD,
             maximum_threshold: int = MAX_THRESHOLD,
             **kwargs: Any) -> Dict[str, Any]:
    """Full auto-tune: analyse frames, recommend threshold + preset.

    ``frames`` may be a list of arrays/base64 strings or a dict payload.
    ``width``/``height`` optionally override the detected resolution for
    the contour-area recommendation.
    """
    # Tolerate API-style kwargs (image/previous keys at top level).
    if frames is None:
        for k in ("frames", "images", "samples", "frames_base64", "images_base64",
                  "image_base64", "image", "frame", "previous_image_base64",
                  "previous", "prev"):
            if kwargs.get(k) is not None:
                frames = kwargs.get(k)
                break
    if frames is None and isinstance(kwargs.get("payload"), dict):
        frames = kwargs.get("payload")
    for alias, target in (("min_threshold", "minimum_threshold"),
                          ("min", "minimum_threshold"),
                          ("max_threshold", "maximum_threshold"),
                          ("max", "maximum_threshold")):
        if target not in kwargs and alias in kwargs and kwargs[alias] is not None:
            if "minimum" in target:
                minimum_threshold = kwargs[alias]
            else:
                maximum_threshold = kwargs[alias]
    stats = analyze_frames(frames)
    try:
        minimum_threshold = int(minimum_threshold)
    except (TypeError, ValueError):
        minimum_threshold = MIN_THRESHOLD
    try:
        maximum_threshold = int(maximum_threshold)
    except (TypeError, ValueError):
        maximum_threshold = MAX_THRESHOLD
    if minimum_threshold > maximum_threshold:
        minimum_threshold, maximum_threshold = maximum_threshold, minimum_threshold
    threshold = recommend_threshold(stats, margin=margin,
                                    minimum=minimum_threshold,
                                    maximum=maximum_threshold)
    w = width if width is not None else stats.get("width", 640)
    h = height if height is not None else stats.get("height", 480)
    for k in ("width", "w", "frame_width", "image_width"):
        if (w is None or w == 640) and kwargs.get(k) is not None:
            w = kwargs.get(k)
    for k in ("height", "h", "frame_height", "image_height"):
        if (h is None or h == 480) and kwargs.get(k) is not None:
            h = kwargs.get(k)
    contour_area = recommend_contour_area(w, h, stats, threshold)
    preset = nearest_preset(threshold, contour_area)
    sensitivity = preset.get("sensitivity")
    try:
        thr_pct = float(preset.get("threshold_percentage", 5.0))
    except (TypeError, ValueError):
        thr_pct = 5.0
    explanation = (
        "Measured p99 frame-difference noise of "
        f"{stats.get('p99_absdiff', 0.0):.1f} (mean "
        f"{stats.get('mean_absdiff', 0.0):.2f}, method "
        f"{stats.get('method')}); recommended threshold {threshold} "
        f"(margin {margin}) maps to preset '{preset.get('name')}'."
    )
    return {
        "ok": True,
        "success": True,
        "service": "motion_autotune",
        "noise": stats,
        "noise_stats": stats,
        "recommended_threshold": threshold,
        "threshold": threshold,
        "motion_threshold": threshold,
        "recommended_contour_area": contour_area,
        "contour_area": contour_area,
        "min_contour_area": contour_area,
        "min_area": contour_area,
        "recommended_preset": preset.get("name"),
        "preset": preset.get("name"),
        "preset_name": preset.get("name"),
        "preset_config": preset,
        "threshold_percentage": thr_pct,
        "motion_threshold_percentage": thr_pct,
        "sensitivity": sensitivity,
        "width": stats.get("width"),
        "height": stats.get("height"),
        "explanation": explanation,
    }


# Aliases for tolerant imports.
analyze_sample_frames = analyze_frames
analyze_noise = analyze_frames
measure_noise = analyze_frames
auto_tune = autotune
autoTune = autotune
tune = autotune
analyse_frames = analyze_frames
analyse_sample_frames = analyze_frames
get_noise_stats = analyze_frames
noise_stats = analyze_frames
recommend = autotune
suggest_threshold = recommend_threshold
suggest_contour_area = recommend_contour_area

__all__ = [
    "MIN_THRESHOLD",
    "MAX_THRESHOLD",
    "DEFAULT_MARGIN",
    "decode_frame",
    "coerce_frames",
    "compute_noise_stats",
    "analyze_frames",
    "analyze_sample_frames",
    "analyse_frames",
    "analyse_sample_frames",
    "analyze_noise",
    "measure_noise",
    "get_noise_stats",
    "noise_stats",
    "recommend_threshold",
    "suggest_threshold",
    "recommend_contour_area",
    "suggest_contour_area",
    "nearest_preset",
    "autotune",
    "auto_tune",
    "autoTune",
    "tune",
    "recommend",
]
