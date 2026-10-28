"""CodeProject.AI-compatible detection-adapter service.

Client-side adapter for CodeProject.AI Server vision endpoints, kept
separate from the Blue Iris-style ``app.services.detector_adapter``
(Blue Candle) module. There is intentionally no other CodeProject.AI
adapter in the tree -- this module is the canonical one.

CodeProject.AI Server protocol (``http://<host>:32168``)::

    POST /v1/vision/detection          (multipart ``image`` + ``min_confidence``)
    POST /v1/vision/custom/<model>     (custom-model variant, same fields)
    GET  /v1/info                     (server version)
    GET  /v1/status                   (module status)

Response (camelCase)::

    {success, message, count, command, moduleId, executionProvider,
     canUseGPU, inferenceMs, processMs, analysisRoundTripMs,
     predictions: [{confidence, label, x_min, y_min, x_max, y_max}]}

This module mirrors the defensive style of ``detector_adapter``:

- :func:`register_adapter` -- register a named adapter (endpoint URL,
  adapter type, thresholds, labels filter, optional custom model).
- :func:`infer` / :func:`detect` / :func:`predict` -- run inference via
  the adapter's HTTP endpoint (multipart, CodeProject.AI compatible).
- Fallback mock -- when the endpoint is unreachable/misconfigured, a
  deterministic mock prediction is returned with ``fallback=True`` so
  callers and the API never hard-fail.

Adapter types accepted here (``cpai`` family plus Blue Candle aliases
for interop):

    ``codeproject-ai`` (default), ``codeprojectai``, ``codeproject``,
    ``cpai``, ``blue-candle``, ``blueiris``, ``generic``, ``mock``.
"""
from __future__ import annotations

import base64
import threading
import time
import uuid  # noqa: F401  (parity with detector_adapter; used by callers)
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

try:  # httpx is optional; absence forces mock fallback.
    import httpx  # type: ignore
except Exception:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_BASE_URL = "http://127.0.0.1:32168"
DEFAULT_ENDPOINT = "http://127.0.0.1:32168/v1/vision/detection"
DEFAULT_MIN_CONFIDENCE = 0.25
DEFAULT_TIMEOUT = 10.0
DEFAULT_ADAPTER = "cpai"
DEFAULT_ADAPTER_TYPE = "codeproject-ai"

ADAPTER_TYPES = (
    "codeproject-ai",
    "codeprojectai",
    "codeproject",
    "cpai",
    "blue-candle",
    "blueiris",
    "generic",
    "mock",
)

# Canonical CPAI type aliases -> normalized form.
TYPE_ALIASES = {
    "blue_candle": "blue-candle",
    "bluecandle": "blue-candle",
    "blue-iris": "blueiris",
    "blue_iris": "blueiris",
    "code-project-ai": "codeproject-ai",
    "code_project_ai": "codeproject-ai",
    "codeprojectai": "codeproject-ai",
    "codeproject": "codeproject-ai",
    "cpai": "codeproject-ai",
    "codeproject-ai": "codeproject-ai",
}

# Full COCO-80 label set (same source as detector_adapter / ispy-blue-candle).
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
    "bed", "diningtable", "tvmonitor", "laptop", "mouse", "remote",
    "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear",
    "hair drier", "toothbrush",
]

CPAI_MODULE_IDS = (
    "ObjectDetectionYOLOv5-6.2",
    "ObjectDetectionYOLOv5Net",
    "ObjectDetectionCoral",
    "FaceProcessing",
)

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
        try:
            raw = base64.b64decode(s, validate=False)
            if raw:
                return raw
        except Exception:  # noqa: BLE001
            pass
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


def _is_bare_host(value: str) -> bool:
    s = value.strip().rstrip("/")
    if "://" not in s:
        return False
    # Bare host has no path beyond scheme://host[:port].
    after = s.split("://", 1)[1]
    return "/" not in after


def build_endpoint(base_url: str, custom_model: Optional[str] = None) -> str:
    """Build a CodeProject.AI vision endpoint from a base URL.

    - No custom model -> ``<base>/v1/vision/detection`` (or *base* itself
      when it already points at a ``/v1/vision/...`` path).
    - With custom model -> ``<base>/v1/vision/custom/<model>``, unless
      *base* already contains ``/custom/`` (returned as-is).
    """
    base = (base_url or DEFAULT_BASE_URL).strip().rstrip("/") or DEFAULT_BASE_URL
    model = (custom_model or "").strip()
    if model:
        if "/custom/" in base:
            return base
        if base.endswith("/v1/vision/detection"):
            stem = base[: -len("/detection")]
            return f"{stem}/custom/{model}"
        if "/v1/vision/" in base:
            # Replace trailing vision sub-path with custom/<model>.
            stem = base.split("/v1/vision/")[0]
            return f"{stem}/v1/vision/custom/{model}"
        if _is_bare_host(base):
            return f"{base}/v1/vision/custom/{model}"
        return f"{base}/v1/vision/custom/{model}" if "://" in base else DEFAULT_ENDPOINT
    # No custom model.
    if "/v1/vision/" in base:
        return base
    if _is_bare_host(base):
        return f"{base}/v1/vision/detection"
    return base


def build_custom_endpoint(base_url: str, custom_model: str) -> str:
    """Explicit custom-model endpoint builder."""
    return build_endpoint(base_url, custom_model)


def endpoint_for_model(base_url: Any, custom_model: Any = None) -> str:
    try:
        base = str(base_url).strip() if base_url is not None else DEFAULT_BASE_URL
    except Exception:  # noqa: BLE001
        base = DEFAULT_BASE_URL
    model = str(custom_model).strip() if custom_model is not None else ""
    return build_endpoint(base or DEFAULT_BASE_URL, model or None)


NAME_KEYS = ("name", "adapter", "adapter_name", "adapterName", "adapter_id", "adapterId", "id", "key")
ENDPOINT_KEYS = ("endpoint", "url", "endpoint_url", "endpointUrl", "uri", "href", "target", "address")
BASE_URL_KEYS = ("base_url", "baseUrl", "base", "server", "server_url", "serverUrl", "host")
TYPE_KEYS = ("adapter_type", "adapterType", "type", "kind", "backend", "provider", "engine", "mode")
CONF_KEYS = ("min_confidence", "minConfidence", "min-confidence", "confidence", "threshold", "confidence_threshold", "confidenceThreshold")
TIMEOUT_KEYS = ("timeout", "timeout_seconds", "timeoutSeconds", "timeout_ms")
LABELS_KEYS = ("labels", "labels_filter", "labelsFilter", "filter", "allowed_labels", "classes")
MODEL_KEYS = ("custom_model", "customModel", "custom-model", "model", "model_name", "modelName", "module_id", "moduleId")
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
    for fk in ("request", "db", "session", "database", "user", "current_user", "conn", "connection"):
        data.pop(fk, None)

    name = _extract_first(data, NAME_KEYS)
    if name is None and positional_names:
        name = positional_names[0]
    if name is None:
        name = DEFAULT_ADAPTER
    name = str(name).strip() or DEFAULT_ADAPTER

    endpoint = _extract_first(data, ENDPOINT_KEYS)
    base_url = _extract_first(data, BASE_URL_KEYS)
    custom_model = _extract_first(data, MODEL_KEYS)
    if custom_model is not None:
        custom_model = str(custom_model).strip() or None
    if endpoint is None and positional_urls:
        endpoint = positional_urls[0]
    if endpoint is None and positional_names and len(positional_names) >= 2:
        endpoint = positional_names[1]
    if endpoint is None and base_url is not None:
        endpoint = build_endpoint(str(base_url).strip(), custom_model)
    elif endpoint is not None:
        endpoint = str(endpoint).strip() or DEFAULT_ENDPOINT
        # If a custom model was supplied alongside a generic endpoint,
        # rewrite to the custom-model variant.
        if custom_model and "/custom/" not in endpoint and (
            _is_bare_host(endpoint) or endpoint.endswith("/v1/vision/detection")
        ):
            endpoint = build_endpoint(endpoint, custom_model)
    else:
        endpoint = build_endpoint(DEFAULT_BASE_URL, custom_model) if custom_model else DEFAULT_ENDPOINT

    base_resolved = str(base_url).strip() if base_url is not None else None
    if not base_resolved:
        # Derive base from endpoint when possible.
        try:
            if "/v1/vision/" in endpoint:
                base_resolved = endpoint.split("/v1/vision/")[0]
            else:
                base_resolved = DEFAULT_BASE_URL
        except Exception:  # noqa: BLE001
            base_resolved = DEFAULT_BASE_URL

    adapter_type = _extract_first(data, TYPE_KEYS) or DEFAULT_ADAPTER_TYPE
    adapter_type = str(adapter_type).strip().lower() or DEFAULT_ADAPTER_TYPE
    adapter_type = TYPE_ALIASES.get(adapter_type, adapter_type)
    if adapter_type not in ADAPTER_TYPES:
        # Keep unknown types verbatim.
        pass

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
        "base_url": base_resolved,
        "baseUrl": base_resolved,
        "adapter_type": adapter_type,
        "type": adapter_type,
        "min_confidence": min_confidence,
        "minConfidence": min_confidence,
        "confidence": min_confidence,
        "threshold": min_confidence,
        "timeout": timeout_f,
        "timeout_seconds": timeout_f,
        "labels": labels,
        "custom_model": custom_model,
        "customModel": custom_model,
        "model": custom_model,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
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

    if _extract_first(data, NAME_KEYS) is None and positional_strs:
        first = positional_strs[0].strip()
        if _looks_like_url(first):
            data.setdefault("endpoint", first)
        elif len(first) <= 128 and "\n" not in first:
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
    base_url = _extract_first(data, BASE_URL_KEYS)
    custom_model = _extract_first(data, MODEL_KEYS)
    if custom_model is not None:
        custom_model = str(custom_model).strip() or None
    min_conf = _extract_first(data, CONF_KEYS)
    timeout = _extract_first(data, TIMEOUT_KEYS)

    image_raw = _extract_first(data, IMAGE_KEYS)
    image_bytes = _decode_image_bytes(image_raw)
    image_b64 = _safe_b64(image_raw) if image_bytes is not None else None
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
        "base_url": str(base_url).strip() if base_url is not None else None,
        "custom_model": custom_model,
        "customModel": custom_model,
        "model": custom_model,
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
                 labels: Optional[List[str]] = None,
                 custom_model: Optional[str] = None) -> Dict[str, Any]:
    name = (adapter or DEFAULT_ADAPTER).strip() if adapter else DEFAULT_ADAPTER
    preds = _mock_predictions()
    if labels:
        preds = [p for p in preds if p.get("label") in labels] or preds
    preds = [p for p in preds if float(p.get("confidence", 0.0)) >= float(min_confidence)]
    ep = endpoint or DEFAULT_ENDPOINT
    module_id = f"custom-{custom_model}" if custom_model else "ObjectDetectionYOLOv5-6.2"
    return {
        "success": True,
        "ok": True,
        "message": "mock fallback prediction (CodeProject.AI unreachable)",
        "error": error,
        "predictions": preds,
        "count": len(preds),
        "command": "detect",
        "module_id": module_id,
        "moduleId": module_id,
        "adapter": name,
        "adapter_name": name,
        "name": name,
        "endpoint": ep,
        "url": ep,
        "base_url": ep.split("/v1/vision/")[0] if "/v1/vision/" in ep else DEFAULT_BASE_URL,
        "custom_model": custom_model,
        "customModel": custom_model,
        "model": custom_model,
        "execution_provider": "CPU",
        "executionProvider": "CPU",
        "can_useGPU": False,
        "canUseGPU": False,
        "can_use_gpu": False,
        "inference_ms": 1,
        "inferenceMs": 1,
        "process_ms": 2,
        "processMs": 2,
        "analysis_round_trip_ms": 2,
        "analysisRoundTripMs": 2,
        "latency_ms": 2,
        "fallback": True,
        "mock": True,
        "simulated": True,
        "min_confidence": min_confidence,
        "minConfidence": min_confidence,
        "labels": labels or [],
        "created_at": _now_iso(),
    }


def _coerce_confidence(raw: Any) -> float:
    """Coerce a CPAI confidence (0-1 or 0-100 percent) to 0-1."""
    try:
        f = float(raw)
    except (TypeError, ValueError):
        return 0.0
    # Percent-style confidences (e.g. 85) arrive from some CPAI clients.
    if f > 1.0 and f <= 100.0:
        f = f / 100.0
    return max(0.0, min(1.0, f))


def _normalize_predictions(raw: Any) -> List[Dict[str, Any]]:
    if raw is None:
        return []
    if isinstance(raw, dict):
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
        # Accept CPAI camelCase + capitalized variants (Label/Confidence).
        def _int(*keys: str, default: int = 0) -> int:
            for k in keys:
                if item.get(k) is not None:
                    try:
                        return int(float(item[k]))
                    except (TypeError, ValueError):
                        continue
            # Case-insensitive fallback.
            low = {str(k).lower(): v for k, v in item.items()}
            for k in keys:
                if str(k).lower() in low and low[str(k).lower()] is not None:
                    try:
                        return int(float(low[str(k).lower()]))
                    except (TypeError, ValueError):
                        continue
            return default

        label = (
            item.get("label") or item.get("Label") or item.get("name")
            or item.get("Name") or item.get("class") or item.get("Class")
            or item.get("class_name") or item.get("className") or "object"
        )
        conf_raw = None
        for ck in ("confidence", "Confidence", "conf", "Conf", "score", "Score", "probability", "Probability"):
            if item.get(ck) is not None:
                conf_raw = item.get(ck)
                break
        out.append({
            "x_min": _int("x_min", "xmin", "xMin", "left", "Left", "x"),
            "y_min": _int("y_min", "ymin", "yMin", "top", "Top", "y"),
            "x_max": _int("x_max", "xmax", "xMax", "right", "Right", "width"),
            "y_max": _int("y_max", "ymax", "yMax", "bottom", "Bottom", "height"),
            "confidence": _coerce_confidence(conf_raw),
            "label": str(label),
        })
    return out


def _pick(payload: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    for k in keys:
        if payload.get(k) is not None:
            return payload.get(k)
    low = {str(k).lower(): v for k, v in payload.items()}
    for k in keys:
        if str(k).lower() in low and low[str(k).lower()] is not None:
            return low[str(k).lower()]
    return default


def _normalize_response(payload: Any, adapter: Optional[str] = None,
                        endpoint: Optional[str] = None, fallback: bool = False,
                        min_confidence: float = DEFAULT_MIN_CONFIDENCE,
                        labels: Optional[List[str]] = None,
                        custom_model: Optional[str] = None) -> Dict[str, Any]:
    name = (adapter or DEFAULT_ADAPTER).strip() if adapter else DEFAULT_ADAPTER
    ep = endpoint or DEFAULT_ENDPOINT
    if not isinstance(payload, dict):
        payload = {"predictions": [], "message": str(payload)}
    preds = _normalize_predictions(payload.get("predictions"))
    # Also accept capitalized "Predictions".
    if not preds and isinstance(payload.get("Predictions"), list):
        preds = _normalize_predictions(payload.get("Predictions"))
    if labels:
        preds = [p for p in preds if p.get("label") in labels]
    preds = [p for p in preds if float(p.get("confidence", 0.0)) >= float(min_confidence)]
    module_id = str(_pick(payload, "module_id", "moduleId", default="ObjectDetectionYOLOv5-6.2") or "ObjectDetectionYOLOv5-6.2")
    exec_provider = str(_pick(payload, "execution_provider", "executionProvider", default="CPU") or "CPU")
    can_gpu = bool(_pick(payload, "can_useGPU", "canUseGPU", "can_use_gpu", default=False))
    inference_ms = int(_pick(payload, "inference_ms", "inferenceMs", default=0) or 0)
    process_ms = int(_pick(payload, "process_ms", "processMs", default=0) or 0)
    roundtrip = int(_pick(payload, "analysis_round_trip_ms", "analysisRoundTripMs", default=0) or 0)
    count_raw = _pick(payload, "count", "Count")
    try:
        count = len(preds) if count_raw is None else int(count_raw)
    except (TypeError, ValueError):
        count = len(preds)
    success = bool(_pick(payload, "success", "Success", default=True))
    return {
        "success": success,
        "ok": success,
        "message": str(_pick(payload, "message", "Message", default="") or ""),
        "error": _pick(payload, "error", "Error", default=None),
        "predictions": preds,
        "count": count,
        "command": str(_pick(payload, "command", "Command", default="detect") or "detect"),
        "module_id": module_id,
        "moduleId": module_id,
        "adapter": name,
        "adapter_name": name,
        "name": name,
        "endpoint": ep,
        "url": ep,
        "base_url": ep.split("/v1/vision/")[0] if "/v1/vision/" in ep else DEFAULT_BASE_URL,
        "custom_model": custom_model,
        "customModel": custom_model,
        "model": custom_model,
        "execution_provider": exec_provider,
        "executionProvider": exec_provider,
        "can_useGPU": can_gpu,
        "canUseGPU": can_gpu,
        "can_use_gpu": can_gpu,
        "inference_ms": inference_ms,
        "inferenceMs": inference_ms,
        "process_ms": process_ms,
        "processMs": process_ms,
        "analysis_round_trip_ms": roundtrip,
        "analysisRoundTripMs": roundtrip,
        "latency_ms": roundtrip,
        "fallback": bool(fallback),
        "mock": False,
        "min_confidence": min_confidence,
        "minConfidence": min_confidence,
        "labels": labels or [],
        "created_at": _now_iso(),
    }


def to_cpai_response(result: Dict[str, Any]) -> Dict[str, Any]:
    """Project an internal result dict to the pure CPAI wire shape."""
    if not isinstance(result, dict):
        return {"success": False, "message": str(result), "predictions": [], "count": 0}
    preds = _normalize_predictions(result.get("predictions"))
    module_id = str(result.get("moduleId", result.get("module_id", "ObjectDetectionYOLOv5-6.2")))
    exec_provider = str(result.get("executionProvider", result.get("execution_provider", "CPU")))
    can_gpu = bool(result.get("canUseGPU", result.get("can_useGPU", False)))
    inference_ms = int(result.get("inferenceMs", result.get("inference_ms", 0)) or 0)
    process_ms = int(result.get("processMs", result.get("process_ms", 0)) or 0)
    roundtrip = int(result.get("analysisRoundTripMs", result.get("analysis_round_trip_ms", 0)) or 0)
    return {
        "success": bool(result.get("success", True)),
        "message": str(result.get("message", "") or ""),
        "count": int(result.get("count", len(preds))),
        "command": str(result.get("command", "detect")),
        "moduleId": module_id,
        "executionProvider": exec_provider,
        "canUseGPU": can_gpu,
        "inferenceMs": inference_ms,
        "processMs": process_ms,
        "analysisRoundTripMs": roundtrip,
        "predictions": [
            {
                "confidence": float(p.get("confidence", 0.0)),
                "label": str(p.get("label", "object")),
                "x_min": int(p.get("x_min", 0)),
                "y_min": int(p.get("y_min", 0)),
                "x_max": int(p.get("x_max", 0)),
                "y_max": int(p.get("y_max", 0)),
            }
            for p in preds
        ],
    }


def from_cpai_response(payload: Any, **kwargs: Any) -> Dict[str, Any]:
    """Parse a raw CPAI server payload into the normalized shape."""
    return _normalize_response(payload, **kwargs)


# Backwards/forwards-friendly alias.
parse_cpai_response = from_cpai_response


def _do_http_post(endpoint: str, image_bytes: bytes, min_confidence: float,
                  timeout: float, adapter_type: str, image_b64: Optional[str]) -> Any:
    if httpx is None:
        raise RuntimeError("httpx is not available")
    if adapter_type == "generic":
        payload = {"image_base64": image_b64, "image": image_b64, "min_confidence": min_confidence,
                   "minConfidence": min_confidence}
        return httpx.post(endpoint, json=payload, timeout=timeout)
    # CodeProject.AI wire: multipart ``image`` + ``min_confidence`` text field.
    files = {"image": ("image.jpg", image_bytes, "image/jpeg")}
    data = {"min_confidence": str(min_confidence)}
    return httpx.post(endpoint, files=files, data=data, timeout=timeout)


# ---------------------------------------------------------------------------
# Registry API
# ---------------------------------------------------------------------------

def register_adapter(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Register (or upsert) a named CodeProject.AI adapter."""
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
        for k, v in ADAPTERS.items():
            if k.lower() == key.lower():
                return dict(v)
    return None


def list_adapters() -> List[Dict[str, Any]]:
    """List all registered CodeProject.AI adapters."""
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
        "service": "cpai_adapter",
        "adapters": names,
        "count": count,
        "default_endpoint": DEFAULT_ENDPOINT,
        "default_base_url": DEFAULT_BASE_URL,
        "default_adapter": DEFAULT_ADAPTER,
        "created_at": _now_iso(),
    }


def get_info() -> Dict[str, Any]:
    """CodeProject.AI-style server info (``GET /v1/info`` shape)."""
    st = get_status()
    return {
        "success": True,
        "status": "ok",
        "ok": True,
        "service": "cpai_adapter",
        "version": "1.0",
        "hostname": "vigilcam",
        "modules": st["adapters"],
        "count": st["count"],
        "default_endpoint": DEFAULT_ENDPOINT,
        "default_base_url": DEFAULT_BASE_URL,
        "created_at": _now_iso(),
    }


# ---------------------------------------------------------------------------
# Inference API
# ---------------------------------------------------------------------------

def _resolve_endpoint(norm: Dict[str, Any], record: Optional[Dict[str, Any]]) -> str:
    endpoint = norm.get("endpoint") or (record.get("endpoint") if record else None)
    custom_model = norm.get("custom_model") or (record.get("custom_model") if record else None)
    base_url = norm.get("base_url") or (record.get("base_url") if record else None)
    if endpoint:
        endpoint = str(endpoint)
        if custom_model and "/custom/" not in endpoint and (
            _is_bare_host(endpoint) or endpoint.endswith("/v1/vision/detection")
        ):
            return build_endpoint(endpoint, str(custom_model))
        return endpoint
    if base_url:
        return build_endpoint(str(base_url), str(custom_model) if custom_model else None)
    if custom_model:
        return build_endpoint(DEFAULT_BASE_URL, str(custom_model))
    return DEFAULT_ENDPOINT


def infer(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Run inference via the named CodeProject.AI adapter endpoint.

    Falls back to a deterministic mock prediction when the endpoint is
    unreachable, unconfigured, or returns an error, so callers never
    hard-fail when CodeProject.AI Server is down.
    """
    norm = _normalize_infer(*args, **kwargs)
    name = norm.get("adapter") or norm.get("adapter_name") or norm.get("name")
    record = get_adapter(name) if name else None
    if record is None and name is None:
        with _LOCK:
            if len(ADAPTERS) == 1:
                record = dict(next(iter(ADAPTERS.values())))
                name = record.get("name")
    endpoint = _resolve_endpoint(norm, record)
    custom_model = norm.get("custom_model") or (record.get("custom_model") if record else None)
    min_confidence = float(norm.get("min_confidence", DEFAULT_MIN_CONFIDENCE))
    timeout = float(norm.get("timeout", DEFAULT_TIMEOUT))
    adapter_type = str((record.get("adapter_type") if record else None) or norm.get("adapter_type") or norm.get("type") or DEFAULT_ADAPTER_TYPE).lower()
    adapter_type = TYPE_ALIASES.get(adapter_type, adapter_type)
    labels: List[str] = list(record.get("labels") or []) if record else []
    if not labels and isinstance(norm.get("labels"), list):
        labels = [str(x) for x in norm["labels"]]

    image_bytes: Optional[bytes] = norm.get("image_bytes")
    image_b64: Optional[str] = norm.get("image_base64")
    if image_bytes is None:
        return _mock_result(adapter=name, endpoint=endpoint, error="no image supplied",
                            min_confidence=min_confidence, labels=labels, custom_model=custom_model)

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
            raise RuntimeError(f"CodeProject.AI endpoint HTTP {status_code}: {body}")
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
                                     fallback=False, min_confidence=min_confidence,
                                     labels=labels, custom_model=custom_model)
        result["latency_ms"] = int((time.time() - started) * 1000)
        result["status_code"] = status_code
        return result
    except Exception as exc:  # noqa: BLE001
        result = _mock_result(adapter=name, endpoint=endpoint, error=str(exc),
                              min_confidence=min_confidence, labels=labels, custom_model=custom_model)
        result["latency_ms"] = int((time.time() - started) * 1000)
        return result


def custom_infer(custom_model: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Run inference against a custom CodeProject.AI model."""
    if custom_model is not None and "custom_model" not in kwargs and "model" not in kwargs:
        kwargs["custom_model"] = custom_model
    return infer(*args, **kwargs)


def mock_infer(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Always return the mock fallback (no HTTP attempt)."""
    norm = _normalize_infer(*args, **kwargs)
    name = norm.get("adapter")
    endpoint = norm.get("endpoint") or DEFAULT_ENDPOINT
    custom_model = norm.get("custom_model")
    labels: List[str] = []
    record = get_adapter(name) if name else None
    if record and isinstance(record.get("labels"), list):
        labels = [str(x) for x in record["labels"]]
    if custom_model is None and record:
        custom_model = record.get("custom_model")
    if custom_model is None and norm.get("endpoint") is None and record is None:
        endpoint = DEFAULT_ENDPOINT
    elif custom_model and endpoint == DEFAULT_ENDPOINT:
        endpoint = build_endpoint(DEFAULT_BASE_URL, str(custom_model))
    return _mock_result(adapter=name, endpoint=str(endpoint),
                        error="mock requested", min_confidence=float(norm.get("min_confidence", DEFAULT_MIN_CONFIDENCE)),
                        labels=labels, custom_model=custom_model)


# ---------------------------------------------------------------------------
# Class wrapper
# ---------------------------------------------------------------------------

class CPAIAdapterService:
    """Object-oriented facade over the module-level registry."""

    def __init__(self) -> None:
        self.registry = ADAPTERS
        self.default_endpoint = DEFAULT_ENDPOINT
        self.default_base_url = DEFAULT_BASE_URL
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

    def custom_infer(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return custom_infer(*a, **k)

    def custom_detect(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return custom_infer(*a, **k)

    def mock_infer(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return mock_infer(*a, **k)

    def mock(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return mock_infer(*a, **k)

    def status(self) -> Dict[str, Any]:
        return get_status()

    def health(self) -> Dict[str, Any]:
        return get_status()

    def info(self) -> Dict[str, Any]:
        return get_info()


cpai_adapter_service = CPAIAdapterService()
cpai_service = cpai_adapter_service
adapter_service = cpai_adapter_service
service = cpai_adapter_service
manager = cpai_adapter_service
default_service = cpai_adapter_service

CPAIAdapter = CPAIAdapterService
CodeProjectAIAdapterService = CPAIAdapterService
CodeProjectAdapterService = CPAIAdapterService
DetectorAdapterService = CPAIAdapterService


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
detect_custom = custom_infer
predict_custom = custom_infer
custom_detect = custom_infer
custom_predict = custom_infer

mock = mock_infer
fallback = mock_infer

status = get_status
health = get_status
info = get_info


def __getattr__(name: str) -> Any:  # noqa: ANN401
    low = name.lower()
    if name[:1].isupper() and any(tok in low for tok in ("service", "manager", "adapter", "cpai", "codeproject")):
        return CPAIAdapterService
    if any(tok in low for tok in ("service", "manager", "gateway", "client", "handler", "engine")):
        return cpai_adapter_service
    if "register" in low or "upsert" in low or ("add" in low and "adapter" in low) or "create_adapter" in low:
        return register_adapter
    if "unregister" in low or "remove" in low or "delete" in low:
        return unregister_adapter
    if "mock" in low or "fallback" in low or "simulat" in low:
        return mock_infer
    if "custom" in low and any(tok in low for tok in ("infer", "detect", "predict")):
        return custom_infer
    if "info" in low:
        return get_info
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
    if "to_cpai" in low or "to_wire" in low:
        return to_cpai_response
    if "from_cpai" in low or "parse_cpai" in low:
        return from_cpai_response
    if "build_endpoint" in low or "endpoint_for" in low:
        return build_endpoint
    raise AttributeError(name)


__all__ = [
    "ADAPTERS", "adapters", "registry", "ADAPTER_REGISTRY",
    "COCO_NAMES", "ADAPTER_TYPES", "CPAI_MODULE_IDS",
    "DEFAULT_ENDPOINT", "DEFAULT_BASE_URL", "DEFAULT_MIN_CONFIDENCE",
    "DEFAULT_TIMEOUT", "DEFAULT_ADAPTER",
    "build_endpoint", "build_custom_endpoint", "endpoint_for_model",
    "to_cpai_response", "from_cpai_response", "parse_cpai_response",
    "register_adapter", "register", "add_adapter", "create_adapter", "upsert_adapter",
    "get_adapter", "get", "list_adapters", "list_all",
    "unregister_adapter", "unregister", "remove_adapter", "clear", "reset",
    "infer", "detect", "predict", "run", "custom_infer", "custom_detect",
    "mock_infer", "mock", "fallback",
    "get_status", "status", "health", "get_info", "info",
    "CPAIAdapterService", "cpai_adapter_service",
]
