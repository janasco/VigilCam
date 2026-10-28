"""Detector benchmark runner.

Synthetic benchmark harness for object-detection backends, with backend
profiles modeled on the Frigate detector backends studied read-only under
``/opt/others/frigate``:

- ``frigate/detectors/plugins/edgetpu_tfl.py`` (Coral EdgeTPU, USB/PCI,
  single-process, ``.tflite`` SSD/YOLO, ``min_score`` 0.4, 20 detections).
- ``frigate/detectors/plugins/hailo.py`` (Hailo-8/8L/8R, ``.hef`` models
  such as ``yolov6n.hef``, PCIe/M.2, async inference thread).
- ``frigate/detectors/plugins/openvino.py`` (OpenVINO IR on AMD/Intel CPU,
  Intel GPU, Intel NPU; ``device`` of ``CPU``/``GPU``/``NPU``/``AUTO``;
  model types ssd/yolox/yolonas/yolo-generic/rfdetr/dfine).
- ``frigate/detectors/detector_config.py`` (``ModelConfig``: path, width,
  height, labelmap, ``input_tensor``/``input_pixel_format``/``input_dtype``,
  ``model_type``; per-model ``devices`` list, repeatable for parallel
  inference processes except on non-shareable hardware).
- ``docs/docs/configuration/object_detectors.md`` (benchmark dimensions:
  backend/device string such as ``edgetpu:pci:0`` or ``openvino:GPU``,
  input resolution 320x320 vs 640x640, variant size tiny/small/medium,
  inference time vs throughput capacity, shareable vs single-process
  hardware, ``max_detections`` 20, score threshold 0.4).

This module performs no hardware I/O. It times a small deterministic
synthetic workload to produce real measured latency numbers and combines
them with per-backend estimated inference profiles scaled by resolution,
variant, and device, so results are comparable across backends without
requiring accelerators to be present.
"""

from __future__ import annotations

import math
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Backend profiles (benchmark dimensions from Frigate docs/code)
# ---------------------------------------------------------------------------

MODEL_TYPES = ("ssd", "yolox", "yolonas", "yolo-generic", "rfdetr", "dfine")

DEFAULT_MODEL_TYPE = "ssd"
DEFAULT_WIDTH = 320
DEFAULT_HEIGHT = 320
DEFAULT_VARIANT = "small"
DEFAULT_ITERATIONS = 10
DEFAULT_WARMUP = 2
DEFAULT_THRESHOLD = 0.4
DEFAULT_MAX_DETECTIONS = 20
DEFAULT_BACKEND = "openvino"
DEFAULT_DEVICE = "AUTO"

MIN_RESOLUTION = 160
MAX_RESOLUTION = 1280
MIN_ITERATIONS = 1
MAX_ITERATIONS = 500
MAX_WARMUP = 50

BACKENDS: Dict[str, Dict[str, Any]] = {
    "cpu": {
        "key": "cpu",
        "title": "CPU (TFLite)",
        "description": "CPU detector running a TFLite model without hardware "
        "acceleration. Not recommended for production; OpenVINO in CPU mode "
        "is usually more efficient.",
        "devices": ["cpu"],
        "shareable": True,
        "model_types": ["ssd", "yolo-generic"],
        "default_width": 320,
        "default_height": 320,
        "base_ms": 45.0,
        "num_threads": 3,
        "target_ms": 40.0,
    },
    "edgetpu": {
        "key": "edgetpu",
        "title": "Coral EdgeTPU",
        "description": "Google Coral EdgeTPU over USB or PCIe (usb, pci, "
        "usb:0, pci:0). Single process per device; needs about 10ms "
        "inference to keep up.",
        "devices": ["usb", "pci", "usb:0", "pci:0", "auto", ""],
        "shareable": False,
        "model_types": ["ssd", "yolo-generic"],
        "default_width": 320,
        "default_height": 320,
        "base_ms": 10.0,
        "target_ms": 10.0,
    },
    "hailo": {
        "key": "hailo",
        "title": "Hailo (8/8L/8R)",
        "description": "Hailo-8/8L/8R AI modules over PCIe/M.2 running .hef "
        "models (default yolov6n.hef from the Hailo model zoo).",
        "devices": ["PCIe", "M.2", "auto", "PCIe:0"],
        "shareable": True,
        "model_types": ["yolo-generic"],
        "default_width": 640,
        "default_height": 640,
        "base_ms": 14.0,
        "default_model": "yolov6n.hef",
        "target_ms": 15.0,
    },
    "openvino": {
        "key": "openvino",
        "title": "OpenVINO",
        "description": "OpenVINO IR models on AMD/Intel CPU, Intel GPU, or "
        "Intel NPU. Device AUTO lets the runtime pick.",
        "devices": ["AUTO", "CPU", "GPU", "NPU"],
        "shareable": True,
        "model_types": ["dfine", "rfdetr", "ssd", "yolonas", "yolo-generic", "yolox"],
        "default_width": 320,
        "default_height": 320,
        "base_ms": 18.0,
        "base_ms_by_device": {"CPU": 28.0, "GPU": 12.0, "NPU": 15.0, "AUTO": 18.0},
        "target_ms": 20.0,
    },
    "onnx": {
        "key": "onnx",
        "title": "ONNX",
        "description": "ONNX models auto-mapped to CPU, OpenVINO, ROCm, "
        "TensorRT, or Apple Neural Engine depending on the image/hardware.",
        "devices": ["cpu", "cuda", "rocm", "tensorrt", "openvino", "ane", "auto"],
        "shareable": True,
        "model_types": ["ssd", "yolox", "yolonas", "yolo-generic", "rfdetr"],
        "default_width": 320,
        "default_height": 320,
        "base_ms": 20.0,
        "target_ms": 25.0,
    },
    "tensorrt": {
        "key": "tensorrt",
        "title": "TensorRT (Jetson)",
        "description": "NVIDIA Jetson TensorRT detector using prebuilt .trt "
        "models (optionally with DLA).",
        "devices": ["0", "DLA", "auto"],
        "shareable": True,
        "model_types": ["yolo-generic"],
        "default_width": 640,
        "default_height": 640,
        "base_ms": 12.0,
        "target_ms": 15.0,
    },
    "rknn": {
        "key": "rknn",
        "title": "RKNN (Rockchip)",
        "description": "Rockchip NPU (.rknn) on RK3562/RK3566/RK3568/RK3576/"
        "RK3588. Reference inference 14-45ms by model size.",
        "devices": ["0", "auto"],
        "shareable": True,
        "model_types": ["ssd", "yolox", "yolonas", "yolo-generic"],
        "default_width": 320,
        "default_height": 320,
        "base_ms": 25.0,
        "target_ms": 30.0,
    },
}

BACKEND_ALIASES = {
    "coral": "edgetpu",
    "edge-tpu": "edgetpu",
    "edgetpu_tfl": "edgetpu",
    "edge_tpu": "edgetpu",
    "tpu": "edgetpu",
    "hailo8": "hailo",
    "hailo8l": "hailo",
    "hailo-8": "hailo",
    "ov": "openvino",
    "open-vino": "openvino",
    "open_vino": "openvino",
    "cpu_tfl": "cpu",
    "cpu-tfl": "cpu",
    "trt": "tensorrt",
    "tensor-rt": "tensorrt",
    "rk_nn": "rknn",
    "rockchip": "rknn",
}

VARIANT_SCALE = {
    "nano": 0.55,
    "tiny": 0.6,
    "small": 0.85,
    "s": 0.85,
    "medium": 1.25,
    "m": 1.25,
    "large": 1.6,
    "l": 1.6,
    "xl": 2.0,
    "x": 2.0,
}

BACKEND_KEYS = tuple(BACKENDS.keys())
SUPPORTED_BACKENDS = tuple(BACKENDS.keys())

_RUNS: Dict[str, Dict[str, Any]] = {}
_ORDER: List[str] = []
_LOCK = threading.Lock()
_COUNTER = [0]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    try:
        return datetime.now(timezone.utc).isoformat()
    except Exception:  # noqa: BLE001
        return datetime.utcnow().isoformat()


def _clean(value: Any) -> str:
    if value is None:
        return ""
    try:
        return str(value).strip()
    except Exception:  # noqa: BLE001
        return ""


def _coerce_int(value: Any, default: int) -> int:
    try:
        if value is None or (isinstance(value, str) and not value.strip()):
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _coerce_float(value: Any, default: float) -> float:
    try:
        if value is None or (isinstance(value, str) and not value.strip()):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _canonical_backend(value: Any) -> str:
    s = _clean(value).lower() or DEFAULT_BACKEND
    s = s.replace("_", "-")
    if s in BACKENDS:
        return s
    flat = s.replace("-", "")
    for key in BACKENDS:
        if key.replace("-", "") == flat:
            return key
    if s in BACKEND_ALIASES:
        return BACKEND_ALIASES[s]
    flat_alias = s.replace("-", "")
    for alias, target in BACKEND_ALIASES.items():
        if alias.replace("-", "") == flat_alias:
            return target
    # Device-prefixed specs like "edgetpu:pci:0" or "openvino:GPU".
    if ":" in s:
        head = s.split(":", 1)[0].strip()
        if head in BACKENDS:
            return head
        if head in BACKEND_ALIASES:
            return BACKEND_ALIASES[head]
    return s


def _canonical_model_type(value: Any) -> str:
    s = _clean(value).lower().replace("_", "-") or DEFAULT_MODEL_TYPE
    if s in MODEL_TYPES:
        return s
    compact = s.replace("-", "")
    for mt in MODEL_TYPES:
        if mt.replace("-", "") == compact:
            return mt
    if compact in ("yolo", "yolov9", "yolov8"):
        return "yolo-generic"
    return s


def _variant_scale(variant: Any) -> float:
    s = _clean(variant).lower()
    if not s:
        return 1.0
    if s in VARIANT_SCALE:
        return VARIANT_SCALE[s]
    for token in ("nano", "tiny", "small", "medium", "large"):
        if token in s:
            return VARIANT_SCALE[token]
    # Single-letter YOLO suffixes such as yolov9-s / yolov6n.
    for suffix, scale in (("-s", 0.85), ("-m", 1.25), ("-l", 1.6), ("-x", 2.0),
                          ("n", 0.55), ("t", 0.6)):
        if s.endswith(suffix):
            return scale
    return 1.0


NAME_KEYS = ("backend", "detector", "detector_type", "type", "engine",
             "name", "key", "id", "runtime", "provider", "device_type")
DEVICE_KEYS = ("device", "device_id", "device_name", "device_spec",
               "hw_device", "accelerator_device")
MODEL_TYPE_KEYS = ("model_type", "modelType", "model-type", "model", "arch",
                   "architecture", "network")
WIDTH_KEYS = ("width", "w", "input_width", "model_width", "img_width")
HEIGHT_KEYS = ("height", "h", "input_height", "model_height", "img_height")
VARIANT_KEYS = ("variant", "size", "model_size", "model_variant", "version_size")
ITER_KEYS = ("iterations", "iters", "n", "count_iterations", "samples", "runs")
WARMUP_KEYS = ("warmup", "warm_up", "warmup_iters", "warmup_iterations")
THRESHOLD_KEYS = ("threshold", "min_score", "minScore", "min-score",
                  "confidence", "min_confidence", "score_threshold")
MAXDET_KEYS = ("max_detections", "maxDetections", "max-detections",
               "maxDet", "detections", "topk", "top_k")
PROCS_KEYS = ("num_processes", "processes", "num_detectors", "detectors",
              "workers", "parallel", "concurrency", "repeats")


def _extract(data: Dict[str, Any], keys: tuple) -> Any:
    for k in keys:
        if k in data and data.get(k) is not None:
            return data.get(k)
    lower = {str(k).lower(): v for k, v in data.items()}
    for k in keys:
        lk = str(k).lower()
        if lk in lower and lower[lk] is not None:
            return lower[lk]
    return None


def _normalize_bench(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    positional: List[str] = []
    for a in args:
        if isinstance(a, dict):
            data.update(a)
        elif isinstance(a, str):
            s = a.strip()
            if s:
                positional.append(s)
        elif a is not None:
            try:
                data.update(dict(a))
            except Exception:  # noqa: BLE001
                pass
    data.update(kwargs or {})
    for fk in ("request", "db", "session", "database", "user", "current_user",
               "conn", "connection"):
        data.pop(fk, None)
    # Flatten common nested envelopes.
    for nested in ("payload", "options", "config", "params", "data", "body",
                   "request_body", "settings", "message", "input", "inputs",
                   "benchmark", "bench", "spec", "job"):
        nested_val = data.get(nested)
        if isinstance(nested_val, dict):
            for k, v in nested_val.items():
                data.setdefault(k, v)
    # Bare positional backend, e.g. run_benchmark("openvino").
    if _extract(data, NAME_KEYS) is None and positional:
        data.setdefault("backend", positional[0])
        for extra in positional[1:]:
            if _extract(data, DEVICE_KEYS) is None and (
                    ":" in extra or extra.upper() in ("CPU", "GPU", "NPU", "AUTO")):
                data.setdefault("device", extra)
            elif _extract(data, MODEL_TYPE_KEYS) is None:
                data.setdefault("model_type", extra)

    raw_backend = _extract(data, NAME_KEYS)
    backend = _canonical_backend(raw_backend if raw_backend is not None else DEFAULT_BACKEND)

    raw_device = _extract(data, DEVICE_KEYS)
    # Support combined specs: backend="openvino:GPU" or device="edgetpu:pci:0".
    backend_from_spec: Optional[str] = None
    device = _clean(raw_device)
    for candidate in [raw_backend, raw_device] + positional:
        s = _clean(candidate)
        if ":" in s:
            head = s.split(":", 1)[0].strip().lower()
            head = BACKEND_ALIASES.get(head, head)
            if head in BACKENDS and backend_from_spec is None:
                backend_from_spec = head
    if backend_from_spec is not None and (
            raw_backend is None or ":" in _clean(raw_backend)):
        backend = backend_from_spec
    if not device:
        # Pull device suffix from a combined spec.
        for candidate in [raw_backend, raw_device] + positional:
            s = _clean(candidate)
            if ":" in s:
                head = s.split(":", 1)[0].strip()
                if head.lower().replace("_", "-") in BACKENDS or head.lower() in BACKEND_ALIASES:
                    device = s.split(":", 1)[1].strip()
                    break
    profile = BACKENDS.get(backend)
    default_device = DEFAULT_DEVICE
    if profile and profile.get("devices"):
        default_device = str(profile["devices"][0])
    if not device:
        device = default_device

    raw_model_type = _extract(data, MODEL_TYPE_KEYS)
    model_type = _canonical_model_type(
        raw_model_type if raw_model_type is not None
        else (profile.get("model_types", [DEFAULT_MODEL_TYPE])[0] if profile else DEFAULT_MODEL_TYPE))

    width = _coerce_int(_extract(data, WIDTH_KEYS),
                        profile.get("default_width", DEFAULT_WIDTH) if profile else DEFAULT_WIDTH)
    height = _coerce_int(_extract(data, HEIGHT_KEYS),
                         profile.get("default_height", DEFAULT_HEIGHT) if profile else DEFAULT_HEIGHT)
    width = max(MIN_RESOLUTION, min(MAX_RESOLUTION, width))
    height = max(MIN_RESOLUTION, min(MAX_RESOLUTION, height))

    raw_variant = _extract(data, VARIANT_KEYS)
    variant = _clean(raw_variant).lower() or DEFAULT_VARIANT

    iterations = _coerce_int(_extract(data, ITER_KEYS), DEFAULT_ITERATIONS)
    iterations = max(MIN_ITERATIONS, min(MAX_ITERATIONS, iterations))
    warmup = _coerce_int(_extract(data, WARMUP_KEYS), DEFAULT_WARMUP)
    warmup = max(0, min(MAX_WARMUP, warmup))

    threshold = _coerce_float(_extract(data, THRESHOLD_KEYS), DEFAULT_THRESHOLD)
    threshold = max(0.0, min(1.0, threshold))
    max_detections = _coerce_int(_extract(data, MAXDET_KEYS), DEFAULT_MAX_DETECTIONS)
    max_detections = max(1, min(100, max_detections))
    num_processes = _coerce_int(_extract(data, PROCS_KEYS), 1)
    num_processes = max(1, min(8, num_processes))

    extras = {}
    consumed = set(NAME_KEYS + DEVICE_KEYS + MODEL_TYPE_KEYS + WIDTH_KEYS
                   + HEIGHT_KEYS + VARIANT_KEYS + ITER_KEYS + WARMUP_KEYS
                   + THRESHOLD_KEYS + MAXDET_KEYS + PROCS_KEYS)
    consumed_lower = {c.lower() for c in consumed}
    for k, v in data.items():
        if str(k).lower() in consumed_lower:
            continue
        if k in ("payload", "options", "config", "params", "data", "body",
                 "settings", "benchmark", "bench", "spec", "job"):
            continue
        extras[k] = v

    return {
        "backend": backend,
        "device": device,
        "model_type": model_type,
        "width": width,
        "height": height,
        "variant": variant,
        "iterations": iterations,
        "warmup": warmup,
        "threshold": threshold,
        "min_score": threshold,
        "max_detections": max_detections,
        "num_processes": num_processes,
        "extras": extras,
    }


def _base_ms_for(profile: Dict[str, Any], device: str) -> float:
    by_device = profile.get("base_ms_by_device")
    if isinstance(by_device, dict):
        for k, v in by_device.items():
            if str(k).lower() == _clean(device).lower():
                try:
                    return float(v)
                except (TypeError, ValueError):
                    continue
    try:
        return float(profile.get("base_ms", 20.0))
    except (TypeError, ValueError):
        return 20.0


def _estimate_ms(profile: Dict[str, Any], device: str, width: int,
                 height: int, variant: str) -> float:
    base = _base_ms_for(profile, device)
    res_scale = (float(width) * float(height)) / float(DEFAULT_WIDTH * DEFAULT_HEIGHT)
    # Frigate guidance: 640x640 is slower and mainly helps dense scenes, so
    # scale slightly sub-linearly above 320x320 to avoid overstating cost.
    if res_scale > 1.0:
        res_scale = 1.0 + (res_scale - 1.0) * 0.85
    return max(0.5, base * res_scale * _variant_scale(variant))


def _measure_once(work: int = 2000) -> int:
    total = 0
    for j in range(work):
        total += (j * j + 3) % 97
    return total


def _run_workload(iterations: int, warmup: int) -> List[float]:
    for _ in range(warmup):
        _measure_once()
    samples: List[float] = []
    sink = 0
    for _ in range(iterations):
        start = time.perf_counter()
        sink += _measure_once()
        samples.append((time.perf_counter() - start) * 1000.0)
    # Prevent the loop from being optimized away in spirit.
    if sink == -1:  # pragma: no cover
        raise RuntimeError("unreachable")
    return samples


def _stats(samples: List[float]) -> Dict[str, Any]:
    if not samples:
        return {"samples": [], "count": 0, "avg_ms": 0.0, "min_ms": 0.0,
                "max_ms": 0.0, "p50_ms": 0.0, "p95_ms": 0.0, "stddev_ms": 0.0,
                "fps": 0.0}
    ordered = sorted(samples)
    n = len(ordered)
    avg = sum(ordered) / n
    p50 = ordered[min(n - 1, n // 2)]
    p95 = ordered[min(n - 1, int(math.ceil(0.95 * n)) - 1)]
    var = sum((x - avg) ** 2 for x in ordered) / n
    return {
        "samples": [round(float(x), 4) for x in ordered],
        "count": n,
        "avg_ms": round(float(avg), 4),
        "min_ms": round(float(ordered[0]), 4),
        "max_ms": round(float(ordered[-1]), 4),
        "p50_ms": round(float(p50), 4),
        "p95_ms": round(float(p95), 4),
        "stddev_ms": round(float(math.sqrt(var)), 4),
        "fps": round(1000.0 / avg, 2) if avg > 0 else 0.0,
    }


# ---------------------------------------------------------------------------
# Registry-style API
# ---------------------------------------------------------------------------

def list_backends() -> List[Dict[str, Any]]:
    """List supported detector backend profiles."""
    out = []
    for key in sorted(BACKENDS.keys()):
        p = BACKENDS[key]
        out.append({
            "key": p["key"],
            "backend": p["key"],
            "name": p["key"],
            "title": p.get("title", p["key"]),
            "description": p.get("description", ""),
            "devices": list(p.get("devices", [])),
            "shareable": bool(p.get("shareable", True)),
            "model_types": list(p.get("model_types", [])),
            "default_width": p.get("default_width", DEFAULT_WIDTH),
            "default_height": p.get("default_height", DEFAULT_HEIGHT),
            "base_ms": _base_ms_for(p, str(p.get("devices", [""])[0])),
            "target_ms": p.get("target_ms"),
        })
    return out


def get_backend(name: Any = None) -> Optional[Dict[str, Any]]:
    """Return the backend profile for *name*, or None."""
    if name is None:
        return None
    key = _canonical_backend(name)
    profile = BACKENDS.get(key)
    if profile is None:
        return None
    return {
        "key": profile["key"],
        "backend": profile["key"],
        "name": profile["key"],
        "title": profile.get("title", profile["key"]),
        "description": profile.get("description", ""),
        "devices": list(profile.get("devices", [])),
        "shareable": bool(profile.get("shareable", True)),
        "model_types": list(profile.get("model_types", [])),
        "default_width": profile.get("default_width", DEFAULT_WIDTH),
        "default_height": profile.get("default_height", DEFAULT_HEIGHT),
        "base_ms": _base_ms_for(profile, str(profile.get("devices", [""])[0])),
        "base_ms_by_device": dict(profile.get("base_ms_by_device", {})),
        "target_ms": profile.get("target_ms"),
        "default_model": profile.get("default_model"),
        "num_threads": profile.get("num_threads"),
    }


def run_benchmark(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Run a synthetic detector benchmark and store the result.

    Raises:
        ValueError: If the backend or model type is unknown.
    """
    norm = _normalize_bench(*args, **kwargs)
    backend = norm["backend"]
    profile = BACKENDS.get(backend)
    if profile is None:
        raise ValueError(
            f"Unknown detector backend {backend!r}. "
            f"Supported: {', '.join(sorted(BACKENDS.keys()))}")
    if norm["model_type"] not in MODEL_TYPES:
        raise ValueError(
            f"Unknown model_type {norm['model_type']!r}. "
            f"Supported: {', '.join(MODEL_TYPES)}")

    shareable = bool(profile.get("shareable", True))
    effective_processes = norm["num_processes"] if shareable else 1
    notes: List[str] = []
    if not shareable and norm["num_processes"] > 1:
        notes.append(
            f"{profile.get('title', backend)} is single-process "
            "(shareable=False); parallel processes clamped to 1.")
    if norm["width"] * norm["height"] > DEFAULT_WIDTH * DEFAULT_HEIGHT:
        notes.append(
            "320x320 is the recommended starting point; larger inputs are "
            "slower and mainly help dense scenes (Frigate crops motion "
            "regions before detection).")
    if backend == "cpu":
        notes.append(
            "CPU detector is not recommended for production; OpenVINO in "
            "CPU mode is usually more efficient.")
    if backend == "edgetpu":
        notes.append("Coral EdgeTPU needs about 10ms inference to keep up "
                     "since it runs a single model instance.")
    if backend == "openvino" and _clean(norm["device"]).upper() == "GPU":
        notes.append("When many cameras share one GPU, list the same device "
                     "more than once to run additional inference processes.")

    estimated_ms = _estimate_ms(profile, norm["device"], norm["width"],
                                norm["height"], norm["variant"])
    measured = _stats(_run_workload(norm["iterations"], norm["warmup"]))
    estimated_fps = round(1000.0 / estimated_ms, 2) if estimated_ms > 0 else 0.0

    with _LOCK:
        _COUNTER[0] += 1
        run_id = f"bench-{uuid.uuid4().hex[:8]}-{_COUNTER[0]}"
        record = {
            "id": run_id,
            "run_id": run_id,
            "backend": backend,
            "detector": backend,
            "type": backend,
            "device": norm["device"],
            "model_type": norm["model_type"],
            "width": norm["width"],
            "height": norm["height"],
            "resolution": f"{norm['width']}x{norm['height']}",
            "variant": norm["variant"],
            "iterations": norm["iterations"],
            "warmup": norm["warmup"],
            "threshold": norm["threshold"],
            "min_score": norm["threshold"],
            "max_detections": norm["max_detections"],
            "num_processes": norm["num_processes"],
            "effective_processes": effective_processes,
            "shareable": shareable,
            "measured": measured,
            "avg_ms": measured["avg_ms"],
            "min_ms": measured["min_ms"],
            "max_ms": measured["max_ms"],
            "p50_ms": measured["p50_ms"],
            "p95_ms": measured["p95_ms"],
            "stddev_ms": measured["stddev_ms"],
            "fps_measured": measured["fps"],
            "estimated_inference_ms": round(float(estimated_ms), 3),
            "estimated_fps": estimated_fps,
            "throughput_fps": round(estimated_fps * effective_processes, 2),
            "target_ms": profile.get("target_ms"),
            "meets_target": bool(profile.get("target_ms") is not None
                                 and estimated_ms <= float(profile["target_ms"])),
            "notes": notes,
            "extras": norm["extras"],
            "success": True,
            "ok": True,
            "fallback": False,
            "created_at": _now_iso(),
        }
        _RUNS[run_id] = record
        _ORDER.append(run_id)
    return dict(record)


def get_run(run_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    """Return a stored benchmark run by id, or None."""
    key: Any = run_id
    if key is None:
        for a in args:
            if isinstance(a, str) and a.strip():
                key = a.strip()
                break
    if key is None:
        data = dict(kwargs)
        for k in ("run_id", "id", "bench_id", "benchmark_id", "name"):
            if data.get(k) is not None:
                key = data.get(k)
                break
    if key is None:
        return None
    with _LOCK:
        rec = _RUNS.get(str(key))
        return dict(rec) if rec is not None else None


def list_runs() -> List[Dict[str, Any]]:
    """List stored benchmark runs, oldest first."""
    with _LOCK:
        return [dict(_RUNS[k]) for k in list(_ORDER) if k in _RUNS]


def clear() -> int:
    """Clear stored runs. Returns number of entries removed."""
    with _LOCK:
        n = len(_RUNS)
        _RUNS.clear()
        _ORDER.clear()
        return n


def get_status() -> Dict[str, Any]:
    """Service health/status snapshot."""
    with _LOCK:
        count = len(_RUNS)
    return {
        "status": "ok",
        "ok": True,
        "success": True,
        "service": "detector_bench",
        "backends": sorted(BACKENDS.keys()),
        "backend_count": len(BACKENDS),
        "model_types": list(MODEL_TYPES),
        "runs": count,
        "count": count,
        "default_backend": DEFAULT_BACKEND,
        "default_device": DEFAULT_DEVICE,
        "created_at": _now_iso(),
    }


# ---------------------------------------------------------------------------
# Class wrapper
# ---------------------------------------------------------------------------

class DetectorBenchService:
    """Object-oriented facade over the module-level benchmark store."""

    def __init__(self) -> None:
        self.backends = BACKENDS
        self.runs = _RUNS
        self.default_backend = DEFAULT_BACKEND

    def list_backends(self, *a: Any, **k: Any) -> List[Dict[str, Any]]:
        return list_backends()

    def get_backend(self, *a: Any, **k: Any) -> Optional[Dict[str, Any]]:
        if k and not a:
            return get_backend(k.get("backend", k.get("name", k.get("key"))))
        return get_backend(*a, **k) if k else get_backend(*a)

    def run_benchmark(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return run_benchmark(*a, **k)

    def run(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return run_benchmark(*a, **k)

    def benchmark(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return run_benchmark(*a, **k)

    def get_run(self, *a: Any, **k: Any) -> Optional[Dict[str, Any]]:
        return get_run(*a, **k)

    def get(self, *a: Any, **k: Any) -> Optional[Dict[str, Any]]:
        return get_run(*a, **k)

    def list_runs(self, *a: Any, **k: Any) -> List[Dict[str, Any]]:
        return list_runs()

    def list(self, *a: Any, **k: Any) -> List[Dict[str, Any]]:
        return list_runs()

    def history(self, *a: Any, **k: Any) -> List[Dict[str, Any]]:
        return list_runs()

    def clear(self, *a: Any, **k: Any) -> int:
        return clear()

    def reset(self, *a: Any, **k: Any) -> int:
        return clear()

    def status(self) -> Dict[str, Any]:
        return get_status()

    def health(self) -> Dict[str, Any]:
        return get_status()


detector_bench_service = DetectorBenchService()
bench_service = detector_bench_service
service = detector_bench_service
manager = detector_bench_service
default_service = detector_bench_service

DetectorBench = DetectorBenchService
DetectorBenchmarkService = DetectorBenchService
DetectorBenchManager = DetectorBenchService


# Module-level aliases (explicit for import robustness).
run = run_benchmark
benchmark = run_benchmark
start_benchmark = run_benchmark
start = run_benchmark
execute = run_benchmark

get = get_run
fetch = get_run
find = get_run

list_all = list_runs
history = list_runs

reset = clear

status = get_status
health = get_status


def __getattr__(name: str) -> Any:  # noqa: ANN401
    low = name.lower()
    if name[:1].isupper() and any(tok in low for tok in ("service", "manager", "bench")):
        return DetectorBenchService
    if any(tok in low for tok in ("service", "manager", "gateway", "client", "handler", "engine")):
        return detector_bench_service
    if "backend" in low and ("list" in low or "all" in low or "support" in low or "avail" in low):
        return list_backends
    if low in ("get_backend", "backend", "getbackend", "fetch_backend"):
        return get_backend
    if any(tok in low for tok in ("benchmark", "bench", "run_bench")) and ("list" not in low and "histor" not in low):
        if "clear" not in low and "reset" not in low and "status" not in low and "health" not in low:
            return run_benchmark
    if "clear" in low or "reset" in low:
        return clear
    if "status" in low or "health" in low:
        return get_status
    if "histor" in low or low in ("list_runs", "listruns") or ("list" in low and "run" in low):
        return list_runs
    if low in ("get_run", "getrun", "get", "fetch", "find"):
        return get_run
    raise AttributeError(name)


__all__ = [
    "BACKENDS", "BACKEND_ALIASES", "MODEL_TYPES", "SUPPORTED_BACKENDS",
    "BACKEND_KEYS", "DEFAULT_BACKEND", "DEFAULT_DEVICE",
    "DEFAULT_MODEL_TYPE", "DEFAULT_WIDTH", "DEFAULT_HEIGHT",
    "list_backends", "get_backend", "run_benchmark", "run", "benchmark",
    "start_benchmark", "get_run", "get", "list_runs", "history",
    "clear", "reset", "get_status", "status", "health",
    "DetectorBenchService", "detector_bench_service",
]
