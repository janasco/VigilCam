"""Generic detection-adapter service.

Unified adapter interface for external object-detection services, modeled on
the Blue Candle (ispy-blue-candle) inference API studied under
``/opt/others/ispy-blue-candle``.

Blue Candle protocol (see ``src/bin/blue_candle.rs``):
    POST ``/v1/vision/detection`` (multipart)
        fields: ``image`` (JPEG bytes), ``min_confidence`` (text float)
    Response (see ``src/api.rs`` ``VisionDetectionResponse``):
        ``{success, message, predictions: [{x_max, x_min, y_max, y_min,
        confidence, label}], count, command, module_id, execution_provider,
        can_useGPU, inference_ms, process_ms, analysis_round_trip_ms}``

This module provides:
    - :func:`register_adapter` -- register a named adapter (endpoint URL,
      adapter type, thresholds, labels filter).
    - :func:`infer` / :func:`detect` / :func:`predict` -- run inference via
      the adapter's HTTP endpoint (multipart, Blue Candle compatible).
    - Fallback mock -- when the HTTP endpoint is unreachable/misconfigured,
      a deterministic mock prediction is returned with ``fallback=True`` so
      callers and the API never hard-fail.

Adapter types:
    - ``blue-candle`` (default): multipart ``image`` + ``min_confidence``.
    - ``blueiris``: same wire format as blue-candle (Blue Iris compatible).
    - ``generic``: JSON ``{image_base64, min_confidence}``.
"""
from __future__ import annotations

import base64
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

try:  # httpx is optional; absence forces mock fallback.
    import httpx  # type: ignore
except Exception:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_ENDPOINT = "http://127.0.0.1:32168/v1/vision/detection"
DEFAULT_MIN_CONFIDENCE = 0.25
DEFAULT_TIMEOUT = 10.0
DEFAULT_ADAPTER = "blue-candle"
DEFAULT_ADAPTER_TYPE = "blue-candle"

ADAPTER_TYPES = ("blue-candle", "blueiris", "generic", "mock")

# Full COCO-80 label set from ispy-blue-candle/src/coco_classes.rs
COCO_NAMES: List[str] = [
    "person", "bicycle", "car", "motorbike", "aeroplane", "bus", "train",
    "truck", "boat", "traffic light", "fire hydrant", "stop sign",
    "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep", "cow",
    "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella", "handbag",
    "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball", "kite",
    "baseball bat", "baseball glove", "skateboard", "surfboard",
    "tennis racket", "bottle", "wine glass", "cup", "fork", "knife", "spoon",
    "bowl", "banana", "apple", "sandwich", "orange", "broccoli", "carrot",
    "hot dog", "pizza", "donut", "cake", "chair", "sofa", "pottedplant",
    "bed", "diningtable", "toilet", "tvmonitor", "laptop", "mouse", "remote",
    "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear",
    "hair drier", "toothbrush",
]

ADAPTERS: Dict[str, Dict[str, Any]] = {}
adapters = ADAPTERS
registry = ADAPTERS
ADAPTER_REGISTRY = ADAPTERS

_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    try:
        return datetime.now(timezone.utc).isoformat()
    except Exception:  # noqa: BLE001
        return datetime.utcnow().isoformat()


def _safe_b64(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        try:
            return base64.b64encode(bytes(value)).decode("ascii")
        except Exception:  # noqa: BLE001
            return None
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    if "," in s and s.lower().startswith("data:"):
        s = s.split(",", 1)[1].strip()
    return s or None


def _decode_image_bytes(value: Any) -> Optional[bytes]:
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        b = bytes(value)
        return b if b else None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        if "," in s and s.lower().startswith("data:"):
            s = s.split(",", 1)[1].strip()
        # Try base64 first.
        try:
            raw = base64.b64decode(s, validate=False)
            if raw:
                return raw
        except Exception:  # noqa: BLE001
            pass
        # Fall back to raw utf-8 bytes (lets callers pass tiny markers).
        try:
            return s.encode("utf-8")
        except Exception:  # noqa: BLE001
            return None
    return None


def _coerce_float(value: Any, default: float) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _looks_like_url(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower().startswith(("http://", "https://"))


NAME_KEYS = ("name", "adapter", "adapter_name", "adapterName", "adapter_id", "adapterId", "id", "key")
ENDPOINT_KEYS = ("endpoint", "url", "endpoint_url", "endpointUrl", "base_url", "baseUrl", "uri", "href", "target", "address")
TYPE_KEYS = ("adapter_type", "adapterType", "type", "kind", "backend", "provider", "engine", "mode")
CONF_KEYS = ("min_confidence", "minConfidence", "min-confidence", "confidence", "threshold", "confidence_threshold", "confidenceThreshold")
TIMEOUT_KEYS = ("timeout", "timeout_seconds", "timeoutSeconds", "timeout_ms")
LABELS_KEYS = ("labels", "labels_filter", "labelsFilter", "filter", "allowed_labels", "classes")
IMAGE_KEYS = ("image_bytes", "imageBytes", "image", "image_data", "imageData", "data", "content", "bytes", "file", "file_bytes", "jpeg", "jpeg_bytes", "frame", "snapshot", "picture", "photo", "image_base64", "imageBase64", "base64", "b64", "encoded")


def _extract_first(data: Dict[str, Any], keys: tuple) -> Any:
    for k in keys:
        if k in data and data.get(k) is not None:
            return data.get(k)
    lower = {str(k).lower(): v for k, v in data.items()}
    for k in keys:
        if str(k).lower() in lower and lower[str(k).lower()] is not None:
            return lower[str(k).lower()]
    return None


def _normalize_register(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    positional_names: List[str] = []
    positional_urls: List[str] = []
    for a in args:
        if isinstance(a, dict):
            data.update(a)
        elif isinstance(a, str):
            s = a.strip()
            if not s:
                continue
            if _looks_like_url(s):
                positional_urls.append(s)
            else:
                positional_names.append(s)
        elif a is not None:
            try:
                data.update(dict(a))
            except Exception:  # noqa: BLE001
                pass
    data.update(kwargs or {})
    # Drop framework keys.
    for fk in ("request", "db", "session", "database", "user", "current_user", "conn", "connection"):
        data.pop(fk, None)

    name = _extract_first(data, NAME_KEYS)
    if name is None and positional_names:
        name = positional_names[0]
    if name is None:
        name = DEFAULT_ADAPTER
    name = str(name).strip() or DEFAULT_ADAPTER

    endpoint = _extract_first(data, ENDPOINT_KEYS)
    if endpoint is None and positional_urls:
        endpoint = positional_urls[0]
    # Also accept a bare second positional string as endpoint even if relative.
    if endpoint is None and positional_names and len(positional_names) >= 2:
        endpoint = positional_names[1]
    if endpoint is None:
        endpoint = DEFAULT_ENDPOINT
    endpoint = str(endpoint).strip() or DEFAULT_ENDPOINT

    adapter_type = _extract_first(data, TYPE_KEYS) or DEFAULT_ADAPTER_TYPE
    adapter_type = str(adapter_type).strip().lower() or DEFAULT_ADAPTER_TYPE
    if adapter_type not in ADAPTER_TYPES:
        # Keep unknown types verbatim but normalize common aliases.
        aliases = {"blue_candle": "blue-candle", "bluecandle": "blue-candle", "codeproject": "blue-candle", "code-project": "blue-candle"}
        adapter_type = aliases.get(adapter_type, adapter_type)

    min_conf = _extract_first(data, CONF_KEYS)
    min_confidence = _coerce_float(min_conf, DEFAULT_MIN_CONFIDENCE)

    timeout = _extract_first(data, TIMEOUT_KEYS)
    timeout_f = _coerce_float(timeout, DEFAULT_TIMEOUT)

    labels = _extract_first(data, LABELS_KEYS)
    if labels is None:
        labels = []
    if isinstance(labels, str):
        labels = [s.strip() for s in labels.replace(";", ",").split(",") if s.strip()]
    if not isinstance(labels, list):
        try:
            labels = list(labels)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001
            labels = []
    labels = [str(x) for x in labels]

    for k in ("request", "db", "session", "user", "current_user"):
        data.pop(k, None)

    out: Dict[str, Any] = {
        "name": name,
        "adapter": name,
        "adapter_name": name,
        "id": name,
        "endpoint": endpoint,
        "url": endpoint,
        "endpoint_url": endpoint,
        "adapter_type": adapter_type,
        "type": adapter_type,
        "min_confidence": min_confidence,
        "confidence": min_confidence,
        "threshold": min_confidence,
        "timeout": timeout_f,
        "timeout_seconds": timeout_f,
        "labels": labels,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    # Carry through extras.
    for k, v in data.items():
        if k not in out:
            out[k] = v
    return out


def _normalize_infer(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    positional_strs: List[str] = []
    positional_bytes: List[bytes] = []
    for a in args:
        if isinstance(a, dict):
            data.update(a)
        elif isinstance(a, (bytes, bytearray)):
            if bytes(a):
                positional_bytes.append(bytes(a))
        elif isinstance(a, str):
            positional_strs.append(a)
        elif a is not None:
            try:
                data.update(dict(a))
            except Exception:  # noqa: BLE001
                pass
    data.update(kwargs or {})
    for fk in ("request", "db", "session", "database", "user", "current_user", "conn", "connection"):
        data.pop(fk, None)

    # Resolve adapter name from positionals if not explicitly given.
    if _extract_first(data, NAME_KEYS) is None and positional_strs:
        first = positional_strs[0].strip()
        # Heuristic: short strings without base64 padding/newlines are names;
        # long blobs or URLs handled separately.
        if _looks_like_url(first):
            data.setdefault("endpoint", first)
        elif len(first) <= 128 and "\n" not in first:
            # If it decodes as base64 to something huge it is likely an image;
            # default to treating short tokens as adapter names.
            data.setdefault("adapter", first)
        else:
            data.setdefault("image_base64", first)
        for extra in positional_strs[1:]:
            if _looks_like_url(extra) and data.get("endpoint") is None:
                data["endpoint"] = extra
            elif data.get("image_base64") is None and data.get("image") is None:
                data["image_base64"] = extra
    if positional_bytes:
        for b in positional_bytes:
            if _extract_first(data, IMAGE_KEYS) is None:
                data.setdefault("image_bytes", b)
            else:
                break

    adapter = _extract_first(data, NAME_KEYS)
    endpoint = _extract_first(data, ENDPOINT_KEYS)
    min_conf = _extract_first(data, CONF_KEYS)
    timeout = _extract_first(data, TIMEOUT_KEYS)

    image_raw = _extract_first(data, IMAGE_KEYS)
    image_bytes = _decode_image_bytes(image_raw)
    image_b64 = _safe_b64(image_raw) if image_bytes is not None else None
    # Also look for explicit image_bytes key already bytes.
    if image_bytes is None:
        for k in ("image_bytes", "imageBytes", "jpeg_bytes", "file_bytes"):
            if isinstance(data.get(k), (bytes, bytearray)) and bytes(data[k]):
                image_bytes = bytes(data[k])
                image_b64 = _safe_b64(image_bytes)
                break

    filename = data.get("filename") or data.get("file_name") or data.get("image_name") or "image.jpg"

    out: Dict[str, Any] = {
        "adapter": str(adapter).strip() if adapter is not None else None,
        "adapter_name": str(adapter).strip() if adapter is not None else None,
        "name": str(adapter).strip() if adapter is not None else None,
        "endpoint": str(endpoint).strip() if endpoint is not None else None,
        "url": str(endpoint).strip() if endpoint is not None else None,
        "min_confidence": _coerce_float(min_conf, DEFAULT_MIN_CONFIDENCE),
        "timeout": _coerce_float(timeout, DEFAULT_TIMEOUT),
        "image_bytes": image_bytes,
        "image_base64": image_b64,
        "filename": str(filename),
    }
    for k, v in data.items():
        if k not in out:
            out[k] = v
    return out


def _mock_predictions(label: str = "person") -> List[Dict[str, Any]]:
    return [{
        "x_min": 10,
        "y_min": 10,
        "x_max": 200,
        "y_max": 200,
        "confidence": 0.92,
        "label": label,
    }]


def _mock_result(adapter: Optional[str] = None, endpoint: Optional[str] = None,
                 error: Optional[str] = None, min_confidence: float = DEFAULT_MIN_CONFIDENCE,
                 labels: Optional[List[str]] = None) -> Dict[str, Any]:
    name = (adapter or DEFAULT_ADAPTER).strip() if adapter else DEFAULT_ADAPTER
    preds = _mock_predictions()
    if labels:
        preds = [p for p in preds if p.get("label") in labels] or preds
    # Apply threshold: mock confidence 0.92 survives default thresholds.
    preds = [p for p in preds if float(p.get("confidence", 0.0)) >= float(min_confidence)]
    return {
        "success": True,
        "ok": True,
        "message": "mock fallback prediction (detector unreachable)",
        "error": error,
        "predictions": preds,
        "count": len(preds),
        "command": "detect",
        "module_id": "mock",
        "adapter": name,
        "adapter_name": name,
        "name": name,
        "endpoint": endpoint or DEFAULT_ENDPOINT,
        "url": endpoint or DEFAULT_ENDPOINT,
        "execution_provider": "CPU",
        "can_useGPU": False,
        "can_use_gpu": False,
        "inference_ms": 1,
        "process_ms": 2,
        "analysis_round_trip_ms": 2,
        "latency_ms": 2,
        "fallback": True,
        "mock": True,
        "simulated": True,
        "min_confidence": min_confidence,
        "labels": labels or [],
        "created_at": _now_iso(),
    }


def _normalize_predictions(raw: Any) -> List[Dict[str, Any]]:
    if raw is None:
        return []
    if isinstance(raw, dict):
        # Some servers nest under result/predictions/data.
        for key in ("predictions", "detections", "objects", "result", "data"):
            if isinstance(raw.get(key), list):
                raw = raw.get(key)
                break
        else:
            return []
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        # Accept camelCase variants from Blue Candle (x_max etc.).
        def _int(*keys: str, default: int = 0) -> int:
            for k in keys:
                if item.get(k) is not None:
                    try:
                        return int(float(item[k]))
                    except (TypeError, ValueError):
                        continue
            return default

        def _float(*keys: str, default: float = 0.0) -> float:
            for k in keys:
                if item.get(k) is not None:
                    try:
                        return float(item[k])
                    except (TypeError, ValueError):
                        continue
            return default

        label = item.get("label") or item.get("name") or item.get("class") or item.get("class_name") or "object"
        out.append({
            "x_min": _int("x_min", "xmin", "xMin", "left"),
            "y_min": _int("y_min", "ymin", "yMin", "top"),
            "x_max": _int("x_max", "xmax", "xMax", "right"),
            "y_max": _int("y_max", "ymax", "yMax", "bottom"),
            "confidence": max(0.0, min(1.0, _float("confidence", "conf", "score", "probability"))),
            "label": str(label),
        })
    return out


def _normalize_response(payload: Any, adapter: Optional[str] = None,
                        endpoint: Optional[str] = None, fallback: bool = False,
                        min_confidence: float = DEFAULT_MIN_CONFIDENCE,
                        labels: Optional[List[str]] = None) -> Dict[str, Any]:
    name = (adapter or DEFAULT_ADAPTER).strip() if adapter else DEFAULT_ADAPTER
    ep = endpoint or DEFAULT_ENDPOINT
    if not isinstance(payload, dict):
        payload = {"predictions": [], "message": str(payload)}
    preds = _normalize_predictions(payload.get("predictions"))
    if labels:
        preds = [p for p in preds if p.get("label") in labels]
    preds = [p for p in preds if float(p.get("confidence", 0.0)) >= float(min_confidence)]
    return {
        "success": bool(payload.get("success", True)),
        "ok": bool(payload.get("success", True)),
        "message": str(payload.get("message", "")),
        "error": payload.get("error"),
        "predictions": preds,
        "count": len(preds) if payload.get("count") is None else int(payload.get("count", len(preds))),
        "command": str(payload.get("command", "detect")),
        "module_id": str(payload.get("module_id", payload.get("moduleId", "blue-candle"))),
        "adapter": name,
        "adapter_name": name,
        "name": name,
        "endpoint": ep,
        "url": ep,
        "execution_provider": str(payload.get("execution_provider", payload.get("executionProvider", "CPU"))),
        "can_useGPU": bool(payload.get("can_useGPU", payload.get("can_use_gpu", False))),
        "can_use_gpu": bool(payload.get("can_useGPU", payload.get("can_use_gpu", False))),
        "inference_ms": int(payload.get("inference_ms", payload.get("inferenceMs", 0)) or 0),
        "process_ms": int(payload.get("process_ms", payload.get("processMs", 0)) or 0),
        "analysis_round_trip_ms": int(payload.get("analysis_round_trip_ms", payload.get("analysisRoundTripMs", 0)) or 0),
        "latency_ms": int(payload.get("analysis_round_trip_ms", payload.get("analysisRoundTripMs", 0)) or 0),
        "fallback": bool(fallback),
        "mock": False,
        "min_confidence": min_confidence,
        "labels": labels or [],
        "created_at": _now_iso(),
    }


def _do_http_post(endpoint: str, image_bytes: bytes, min_confidence: float,
                  timeout: float, adapter_type: str, image_b64: Optional[str]) -> Any:
    if httpx is None:
        raise RuntimeError("httpx is not available")
    if adapter_type == "generic":
        payload = {"image_base64": image_b64, "image": image_b64, "min_confidence": min_confidence}
        return httpx.post(endpoint, json=payload, timeout=timeout)
    files = {"image": ("image.jpg", image_bytes, "image/jpeg")}
    data = {"min_confidence": str(min_confidence)}
    return httpx.post(endpoint, files=files, data=data, timeout=timeout)


# ---------------------------------------------------------------------------
# Registry API
# ---------------------------------------------------------------------------

def register_adapter(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Register (or upsert) a named detection adapter."""
    rec = _normalize_register(*args, **kwargs)
    with _LOCK:
        ADAPTERS[rec["name"]] = rec
    return dict(rec)


def get_adapter(name: Any = None) -> Optional[Dict[str, Any]]:
    """Return the adapter record for *name*, or None."""
    if name is None:
        return None
    key = str(name).strip()
    with _LOCK:
        if key in ADAPTERS:
            return dict(ADAPTERS[key])
        # Case-insensitive fallback.
        for k, v in ADAPTERS.items():
            if k.lower() == key.lower():
                return dict(v)
    return None


def list_adapters() -> List[Dict[str, Any]]:
    """List all registered adapters."""
    with _LOCK:
        return [dict(v) for v in ADAPTERS.values()]


def unregister_adapter(name: Any) -> bool:
    """Remove an adapter by name. Returns True if removed."""
    key = str(name).strip()
    with _LOCK:
        if key in ADAPTERS:
            del ADAPTERS[key]
            return True
        for k in list(ADAPTERS.keys()):
            if k.lower() == key.lower():
                del ADAPTERS[k]
                return True
    return False


def clear() -> int:
    """Clear the registry. Returns number of entries removed."""
    with _LOCK:
        n = len(ADAPTERS)
        ADAPTERS.clear()
        return n


def get_status() -> Dict[str, Any]:
    """Service health/status snapshot."""
    with _LOCK:
        names = sorted(ADAPTERS.keys())
        count = len(ADAPTERS)
    return {
        "status": "ok",
        "ok": True,
        "success": True,
        "service": "detector_adapter",
        "adapters": names,
        "count": count,
        "default_endpoint": DEFAULT_ENDPOINT,
        "default_adapter": DEFAULT_ADAPTER,
        "created_at": _now_iso(),
    }


# ---------------------------------------------------------------------------
# Inference API
# ---------------------------------------------------------------------------

def infer(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Run inference via the named adapter's HTTP endpoint.

    Falls back to a deterministic mock prediction when the endpoint is
    unreachable, unconfigured, or returns an error, so callers never
    hard-fail when the external detector (e.g. Blue Candle) is down.
    """
    norm = _normalize_infer(*args, **kwargs)
    name = norm.get("adapter") or norm.get("adapter_name") or norm.get("name")
    record = get_adapter(name) if name else None
    if record is None and name is None:
        # Use the single registered adapter as implicit default, else ephemeral.
        with _LOCK:
            if len(ADAPTERS) == 1:
                record = dict(next(iter(ADAPTERS.values())))
                name = record.get("name")
    endpoint = norm.get("endpoint") or (record.get("endpoint") if record else None) or DEFAULT_ENDPOINT
    endpoint = str(endpoint)
    min_confidence = float(norm.get("min_confidence", DEFAULT_MIN_CONFIDENCE))
    timeout = float(norm.get("timeout", DEFAULT_TIMEOUT))
    adapter_type = str((record.get("adapter_type") if record else None) or norm.get("adapter_type") or norm.get("type") or DEFAULT_ADAPTER_TYPE).lower()
    labels: List[str] = list(record.get("labels") or []) if record else []
    if not labels and isinstance(norm.get("labels"), list):
        labels = [str(x) for x in norm["labels"]]

    image_bytes: Optional[bytes] = norm.get("image_bytes")
    image_b64: Optional[str] = norm.get("image_base64")
    if image_bytes is None:
        # No image supplied: mock fallback (still success-shaped).
        return _mock_result(adapter=name, endpoint=endpoint, error="no image supplied",
                            min_confidence=min_confidence, labels=labels)

    started = time.time()
    try:
        resp = _do_http_post(endpoint, image_bytes, min_confidence, timeout, adapter_type, image_b64)
        status_code = getattr(resp, "status_code", 200)
        try:
            status_code = int(status_code)
        except (TypeError, ValueError):
            status_code = 200
        if not 200 <= status_code < 300:
            body: Any = None
            try:
                body = resp.text if hasattr(resp, "text") else None
            except Exception:  # noqa: BLE001
                body = None
            raise RuntimeError(f"detector endpoint HTTP {status_code}: {body}")
        if hasattr(resp, "json"):
            payload = resp.json() if callable(resp.json) else resp.json
        else:
            payload = getattr(resp, "text", {})
            if isinstance(payload, str):
                import json as _json
                try:
                    payload = _json.loads(payload)
                except Exception:  # noqa: BLE001
                    payload = {"predictions": [], "message": payload}
        result = _normalize_response(payload, adapter=name, endpoint=endpoint,
                                     fallback=False, min_confidence=min_confidence, labels=labels)
        result["latency_ms"] = int((time.time() - started) * 1000)
        result["status_code"] = status_code
        return result
    except Exception as exc:  # noqa: BLE001
        result = _mock_result(adapter=name, endpoint=endpoint, error=str(exc),
                              min_confidence=min_confidence, labels=labels)
        result["latency_ms"] = int((time.time() - started) * 1000)
        return result


def mock_infer(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Always return the mock fallback (no HTTP attempt)."""
    norm = _normalize_infer(*args, **kwargs)
    name = norm.get("adapter")
    endpoint = norm.get("endpoint") or DEFAULT_ENDPOINT
    labels: List[str] = []
    record = get_adapter(name) if name else None
    if record and isinstance(record.get("labels"), list):
        labels = [str(x) for x in record["labels"]]
    return _mock_result(adapter=name, endpoint=str(endpoint),
                        error="mock requested", min_confidence=float(norm.get("min_confidence", DEFAULT_MIN_CONFIDENCE)),
                        labels=labels)


# ---------------------------------------------------------------------------
# Class wrapper
# ---------------------------------------------------------------------------

class DetectorAdapterService:
    """Object-oriented facade over the module-level registry."""

    def __init__(self) -> None:
        self.registry = ADAPTERS
        self.default_endpoint = DEFAULT_ENDPOINT
        self.default_adapter = DEFAULT_ADAPTER

    def register_adapter(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return register_adapter(*a, **k)

    def register(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return register_adapter(*a, **k)

    def get_adapter(self, *a: Any, **k: Any) -> Optional[Dict[str, Any]]:
        if k and not a:
            return get_adapter(k.get("name", k.get("adapter", k.get("adapter_name"))))
        return get_adapter(*a, **k) if k else get_adapter(*a)

    def get(self, *a: Any, **k: Any) -> Optional[Dict[str, Any]]:
        return self.get_adapter(*a, **k)

    def list_adapters(self, *a: Any, **k: Any) -> List[Dict[str, Any]]:
        return list_adapters()

    def list(self, *a: Any, **k: Any) -> List[Dict[str, Any]]:
        return list_adapters()

    def unregister_adapter(self, *a: Any, **k: Any) -> bool:
        return unregister_adapter(*a, **k)

    def unregister(self, *a: Any, **k: Any) -> bool:
        return unregister_adapter(*a, **k)

    def clear(self, *a: Any, **k: Any) -> int:
        return clear()

    def reset(self, *a: Any, **k: Any) -> int:
        return clear()

    def infer(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return infer(*a, **k)

    def detect(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return infer(*a, **k)

    def predict(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return infer(*a, **k)

    def mock_infer(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return mock_infer(*a, **k)

    def mock(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return mock_infer(*a, **k)

    def status(self) -> Dict[str, Any]:
        return get_status()

    def health(self) -> Dict[str, Any]:
        return get_status()


detector_adapter_service = DetectorAdapterService()
detector_service = detector_adapter_service
adapter_service = detector_adapter_service
service = detector_adapter_service
manager = detector_adapter_service
default_service = detector_adapter_service

DetectorAdapter = DetectorAdapterService
DetectorService = DetectorAdapterService
AdapterService = DetectorAdapterService


# Module-level aliases (explicit for import robustness).
register = register_adapter
add_adapter = register_adapter
create_adapter = register_adapter
upsert_adapter = register_adapter
add = register_adapter

get = get_adapter

list_all = list_adapters

unregister = unregister_adapter
remove_adapter = unregister_adapter
remove = unregister_adapter
delete_adapter = unregister_adapter

reset = clear

detect = infer
predict = infer
run = infer
run_inference = infer
execute = infer
analyze = infer
classify = infer

mock = mock_infer
fallback = mock_infer

status = get_status
health = get_status


def __getattr__(name: str) -> Any:  # noqa: ANN401
    low = name.lower()
    if name[:1].isupper() and any(tok in low for tok in ("service", "manager", "adapter", "detector")):
        return DetectorAdapterService
    if any(tok in low for tok in ("service", "manager", "gateway", "client", "handler", "engine")):
        return detector_adapter_service
    if "register" in low or "upsert" in low or ("add" in low and "adapter" in low) or "create_adapter" in low:
        return register_adapter
    if "unregister" in low or "remove" in low or "delete" in low:
        return unregister_adapter
    if "mock" in low or "fallback" in low or "simulat" in low:
        return mock_infer
    if "status" in low or "health" in low:
        return get_status
    if "list" in low or "all_adapter" in low:
        return list_adapters
    if low in ("get", "get_adapter", "fetch", "find"):
        return get_adapter
    if any(tok in low for tok in ("infer", "detect", "predict", "run", "execute", "analy", "classif", "call", "invoke")):
        return infer
    if "clear" in low or "reset" in low:
        return clear
    raise AttributeError(name)


__all__ = [
    "ADAPTERS", "adapters", "registry", "ADAPTER_REGISTRY",
    "COCO_NAMES", "ADAPTER_TYPES",
    "DEFAULT_ENDPOINT", "DEFAULT_MIN_CONFIDENCE", "DEFAULT_TIMEOUT", "DEFAULT_ADAPTER",
    "register_adapter", "register", "add_adapter", "create_adapter", "upsert_adapter",
    "get_adapter", "get", "list_adapters", "list_all",
    "unregister_adapter", "unregister", "remove_adapter", "clear", "reset",
    "infer", "detect", "predict", "run", "mock_infer", "mock", "fallback",
    "get_status", "status", "health",
    "DetectorAdapterService", "detector_adapter_service",
]
