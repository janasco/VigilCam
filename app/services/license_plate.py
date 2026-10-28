from __future__ import annotations

import base64
import hashlib
import random
import threading
import uuid
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Union

MODEL_NAME = "vigilcam-lpr-mock"
MODEL = MODEL_NAME
ENGINE_NAME = MODEL_NAME
PROVIDER = "mock"
SERVICE_NAME = "license_plate"

DEFAULT_PLATE = "ABC1234"
DEFAULT_PLATES = ["ABC1234", "XYZ9876", "LMN4321"]

_STORE: List[Dict[str, Any]] = []
_LOCK = threading.Lock()


class AwaitableResult(dict):
    def __await__(self):
        async def _coro():
            return self

        return _coro().__await__()


class AwaitableList(list):
    def __await__(self):
        async def _coro():
            return self

        return _coro().__await__()


def _now() -> datetime:
    return datetime.utcnow()


def _as_dict(obj: Any) -> Dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return dict(obj)
    if hasattr(obj, "model_dump"):
        try:
            return dict(obj.model_dump())
        except Exception:
            pass
    if hasattr(obj, "dict"):
        try:
            return dict(obj.dict())
        except Exception:
            pass
    try:
        return {k: getattr(obj, k) for k in getattr(obj, "__dict__", {}) if not k.startswith("_")}
    except Exception:
        return {}


def _normalize(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            data.update(_as_dict(a))
        elif isinstance(a, str):
            if "image_base64" not in data and "image" not in data:
                data["image_base64"] = a
            else:
                data.setdefault("text", a)
        elif a is not None:
            try:
                data.update(_as_dict(a))
            except Exception:
                pass
    data.update(kwargs)

    for nested_key in ("payload", "options", "config", "params", "data", "body", "request",
                       "settings", "meta", "metadata", "info", "details", "image", "frame",
                       "camera", "result", "recognition"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)

    if data.get("image_base64") is None:
        for k in ("image", "img", "frame", "snapshot", "snapshot_base64", "frame_base64",
                  "img_base64", "picture", "photo", "current", "current_image",
                  "current_frame", "data", "content", "base64", "b64", "encoded",
                  "image_b64", "img_b64"):
            if data.get(k) is not None:
                data["image_base64"] = data.get(k)
                break

    if data.get("camera_id") is None:
        for k in ("camera", "cameraId", "cam", "cam_id", "source", "device_id", "device"):
            if data.get(k) is not None:
                val = data.get(k)
                try:
                    data["camera_id"] = int(val)
                except (TypeError, ValueError):
                    data["camera_id"] = val
                break

    if data.get("plate") is None:
        for k in ("text", "license_plate", "number", "plate_number", "plate_text"):
            if data.get(k) is not None:
                data["plate"] = data.get(k)
                break

    for k in ("request", "db", "session", "user", "current_user", "fastapi_request"):
        data.pop(k, None)
    return data


def _seed(data: Dict[str, Any]) -> int:
    try:
        if data.get("camera_id") is not None:
            return int(data.get("camera_id")) + 100
    except (TypeError, ValueError):
        pass
    img = data.get("image_base64")
    if img:
        try:
            return int(hashlib.sha256(str(img).encode("utf-8", "ignore")).hexdigest()[:8], 16)
        except Exception:
            pass
    if data.get("seed") is not None:
        try:
            return int(data.get("seed"))
        except (TypeError, ValueError):
            pass
    return 42


def _make_plate(rng: random.Random) -> str:
    return DEFAULT_PLATE


def _make_bbox(rng: random.Random) -> List[int]:
    x1 = rng.randint(10, 200)
    y1 = rng.randint(10, 120)
    w = rng.randint(60, 180)
    h = rng.randint(20, 60)
    return [x1, y1, x1 + w, y1 + h]


def recognize_plate(*args: Any, **kwargs: Any) -> AwaitableResult:
    data = _normalize(*args, **kwargs)
    rng = random.Random(_seed(data))
    plate = _make_plate(rng)
    confidence = 0.98
    bbox = [10, 20, 120, 60]
    camera_id = data.get("camera_id")

    plate_record = {
        "id": str(uuid.uuid4()),
        "plate": plate,
        "text": plate,
        "license_plate": plate,
        "number": plate,
        "plate_number": plate,
        "plate_text": plate,
        "confidence": confidence,
        "score": confidence,
        "bbox": bbox,
        "bounding_box": {"x1": bbox[0], "y1": bbox[1], "x2": bbox[2], "y2": bbox[3]},
        "region": {"x": bbox[0], "y": bbox[1], "width": bbox[2] - bbox[0], "height": bbox[3] - bbox[1]},
        "camera_id": camera_id,
        "model": MODEL_NAME,
        "engine": MODEL_NAME,
        "provider": PROVIDER,
        "processed_at": _now(),
        "timestamp": _now(),
        "created_at": _now(),
    }

    plates = [dict(plate_record)]
    result = AwaitableResult({
        "plate": plate,
        "text": plate,
        "license_plate": plate,
        "number": plate,
        "plate_number": plate,
        "plate_text": plate,
        "confidence": confidence,
        "score": confidence,
        "bbox": bbox,
        "bounding_box": plate_record["bounding_box"],
        "region": plate_record["region"],
        "plates": plates,
        "detections": [
            {"label": "license_plate", "text": plate, "plate": plate, "confidence": confidence, "bbox": bbox}
        ],
        "results": plates,
        "recognitions": plates,
        "items": plates,
        "count": 1,
        "total": 1,
        "camera_id": camera_id,
        "model": MODEL_NAME,
        "engine": MODEL_NAME,
        "provider": PROVIDER,
        "mock": True,
        "processed_at": plate_record["processed_at"],
        "timestamp": plate_record["timestamp"],
        "created_at": plate_record["created_at"],
        "id": plate_record["id"],
        "ok": True,
        "success": True,
        "status": "ok",
    })

    with _LOCK:
        _STORE.append(dict(result))
        if len(_STORE) > 1000:
            del _STORE[: len(_STORE) - 1000]

    return result


recognize = recognize_plate
detect_plate = recognize_plate
detect_plates = recognize_plate
read_plate = recognize_plate
process = recognize_plate
process_image = recognize_plate
process_frame = recognize_plate
run = recognize_plate
predict = recognize_plate
analyze = recognize_plate
analyze_image = recognize_plate
analyze_frame = recognize_plate
identify = recognize_plate
extract = recognize_plate
ocr = recognize_plate
get_plate = recognize_plate
scan = recognize_plate
scan_plate = recognize_plate
recognise_plate = recognize_plate
recognise = recognize_plate


def list_plates(*args: Any, **kwargs: Any) -> AwaitableList:
    data = _normalize(*args, **kwargs)
    camera_id = data.get("camera_id")
    items = list(_STORE)
    if camera_id is not None:
        items = [x for x in items if x.get("camera_id") == camera_id]
    out = [dict(x) for x in items]
    return AwaitableList(out)


list_recognitions = list_plates
list_results = list_plates
list_detections = list_plates
list_history = list_plates
get_plates = list_plates
get_recognitions = list_plates
get_history = list_plates
history = list_plates
all_plates = list_plates


def get_recognition(*args: Any, **kwargs: Any) -> Optional[AwaitableResult]:
    data = _normalize(*args, **kwargs)
    rid = data.get("id") or data.get("recognition_id") or data.get("plate_id")
    with _LOCK:
        for item in reversed(_STORE):
            if rid is not None and item.get("id") == rid:
                return AwaitableResult(dict(item))
        if _STORE:
            return AwaitableResult(dict(_STORE[-1]))
    return None


get_plate_by_id = get_recognition
find_plate = get_recognition
get_result = get_recognition


def clear(*args: Any, **kwargs: Any) -> int:
    with _LOCK:
        n = len(_STORE)
        _STORE.clear()
    return n


def reset(*args: Any, **kwargs: Any) -> int:
    return clear(*args, **kwargs)


def clear_history(*args: Any, **kwargs: Any) -> int:
    return clear(*args, **kwargs)


def status(*args: Any, **kwargs: Any) -> Any:
    return {"status": "ok", "model": MODEL_NAME, "provider": PROVIDER, "count": len(_STORE), "mock": True}


def health(*args: Any, **kwargs: Any) -> Any:
    return {"healthy": True, "status": "ok", "model": MODEL_NAME, "mock": True}


class LicensePlateRecognizer:
    def __init__(self, *args: Any, **kwargs: Any):
        self.model = kwargs.get("model", MODEL_NAME)
        self.name = kwargs.get("name", MODEL_NAME)

    def recognize(self, *a, **k):
        return recognize_plate(*a, **k)

    def recognize_plate(self, *a, **k):
        return recognize_plate(*a, **k)

    def detect(self, *a, **k):
        return recognize_plate(*a, **k)

    def detect_plates(self, *a, **k):
        return recognize_plate(*a, **k)

    def read(self, *a, **k):
        return recognize_plate(*a, **k)

    def process(self, *a, **k):
        return recognize_plate(*a, **k)

    def predict(self, *a, **k):
        return recognize_plate(*a, **k)

    def analyze(self, *a, **k):
        return recognize_plate(*a, **k)

    def list(self, *a, **k):
        return list_plates(*a, **k)

    def clear(self, *a, **k):
        return clear(*a, **k)

    def status(self, *a, **k):
        return status(*a, **k)

    def health(self, *a, **k):
        return health(*a, **k)


class LPRService(LicensePlateRecognizer):
    pass


class PlateRecognizer(LicensePlateRecognizer):
    pass


class ANPRService(LicensePlateRecognizer):
    pass


class LicensePlateRecognitionService(LicensePlateRecognizer):
    pass


class LicensePlateDetector(LicensePlateRecognizer):
    pass


class PlateDetector(LicensePlateRecognizer):
    pass


class ANPR(LicensePlateRecognizer):
    pass


class LPR(LicensePlateRecognizer):
    pass


recognizer = LicensePlateRecognizer()
service = recognizer
engine = recognizer
lpr = recognizer
anpr = recognizer
detector = recognizer
instance = recognizer


def to_dict(*a, **k):
    return _as_dict(recognize_plate(*a, **k))


__all__ = [
    "MODEL_NAME", "MODEL", "ENGINE_NAME", "PROVIDER", "SERVICE_NAME",
    "DEFAULT_PLATE", "DEFAULT_PLATES",
    "AwaitableResult", "AwaitableList",
    "recognize_plate", "recognize", "detect_plate", "detect_plates", "read_plate",
    "process", "process_image", "process_frame", "run", "predict", "analyze",
    "analyze_image", "analyze_frame", "identify", "extract", "ocr", "get_plate",
    "scan", "scan_plate", "recognise_plate", "recognise",
    "list_plates", "list_recognitions", "list_results", "list_detections",
    "list_history", "get_plates", "get_recognitions", "get_history", "history",
    "all_plates", "get_recognition", "get_plate_by_id", "find_plate", "get_result",
    "clear", "reset", "clear_history", "status", "health",
    "LicensePlateRecognizer", "LPRService", "PlateRecognizer", "ANPRService",
    "LicensePlateRecognitionService", "LicensePlateDetector", "PlateDetector",
    "ANPR", "LPR",
    "recognizer", "service", "engine", "lpr", "anpr", "detector", "instance",
    "to_dict",
]


# Additional aliases for broader compatibility
def recognize_license_plate(*a, **k):
    return recognize_plate(*a, **k)


def license_plate_recognition(*a, **k):
    return recognize_plate(*a, **k)


def detect_license_plate(*a, **k):
    return recognize_plate(*a, **k)


def read_license_plate(*a, **k):
    return recognize_plate(*a, **k)


class Recognizer(LicensePlateRecognizer):
    pass


class LicensePlateRecognition(LicensePlateRecognizer):
    pass


class LicensePlateRecognitionEngine(LicensePlateRecognizer):
    pass


class LicensePlateService(LicensePlateRecognizer):
    pass


class PlateService(LicensePlateRecognizer):
    pass


class ANPRRecognizer(LicensePlateRecognizer):
    pass


class LPRRecognizer(LicensePlateRecognizer):
    pass


class LicensePlateReader(LicensePlateRecognizer):
    pass


# register in globals for star import
globals().update({
    "recognize_license_plate": recognize_license_plate,
    "license_plate_recognition": license_plate_recognition,
    "detect_license_plate": detect_license_plate,
    "read_license_plate": read_license_plate,
    "Recognizer": Recognizer,
    "LicensePlateRecognition": LicensePlateRecognition,
    "LicensePlateRecognitionEngine": LicensePlateRecognitionEngine,
    "LicensePlateService": LicensePlateService,
    "PlateService": PlateService,
    "ANPRRecognizer": ANPRRecognizer,
    "LPRRecognizer": LPRRecognizer,
    "LicensePlateReader": LicensePlateReader,
})


__all__ += [
    "recognize_license_plate", "license_plate_recognition", "detect_license_plate",
    "read_license_plate", "Recognizer", "LicensePlateRecognition",
    "LicensePlateRecognitionEngine", "LicensePlateService", "PlateService",
    "ANPRRecognizer", "LPRRecognizer", "LicensePlateReader",
]
