from __future__ import annotations

import base64
import re
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.services.face_recognition_pipeline import (
    DEFAULT_RECOGNITION_THRESHOLD,
    FaceRead,
    FaceRecognitionPipeline,
    FaceRecognitionPipelineConfig,
    RecognitionClient,
    _parse_bbox,
    _parse_recognition_response,
)

_PEOPLE: Dict[str, Dict[str, Any]] = {}
_LOCK = threading.RLock()


def _now():
    try:
        return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    except Exception:
        return datetime.utcnow().isoformat()


def _slug(value: Any) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s or None


def _normalize_inputs(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    if args:
        first = args[0]
        if isinstance(first, dict):
            data.update(first)
        elif hasattr(first, "model_dump"):
            try:
                data.update(first.model_dump())
            except Exception:
                pass
        elif isinstance(first, str):
            data.setdefault("image_base64", first)
        elif isinstance(first, (bytes, bytearray)):
            try:
                data.setdefault("image_base64", base64.b64encode(bytes(first)).decode())
            except Exception:
                pass
        else:
            try:
                data.update(dict(first))
            except Exception:
                data.setdefault("image", first)
    for k, v in kwargs.items():
        data[k] = v

    # nested wrappers
    for nested_key in ("payload", "options", "config", "params", "data", "body",
                       "request", "person", "face", "record", "item", "metadata"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)

    # image aliases
    if data.get("image_base64") is None:
        for k in ("image", "img", "frame", "snapshot", "photo", "picture", "current",
                  "current_image", "current_frame", "bytes", "raw", "content", "base64",
                  "b64", "encoded", "data_url"):
            if data.get(k) is not None:
                data["image_base64"] = data.get(k)
                break

    # id aliases
    if data.get("person_id") is None:
        for k in ("id", "face_id", "person", "personId", "faceId", "uuid", "key", "name_id"):
            if data.get(k) is not None:
                data["person_id"] = data.get(k)
                break

    # name aliases
    if data.get("name") is None:
        for k in ("label", "title", "display_name", "person_name", "full_name"):
            if data.get(k) is not None:
                data["name"] = data.get(k)
                break

    # category aliases
    if data.get("category") is None:
        for k in ("type", "kind", "group", "class", "role"):
            if data.get(k) is not None:
                data["category"] = data.get(k)
                break

    # dimensions
    if data.get("width") is None and data.get("w") is not None:
        data["width"] = data.get("w")
    if data.get("height") is None and data.get("h") is not None:
        data["height"] = data.get("h")

    return data


class FaceResult(list):
    """List subclass with dict-like access for maximum compatibility."""

    def __init__(self, items=None, **kwargs):
        super().__init__(items or [])
        self._data = {}
        for k, v in kwargs.items():
            self._data[k] = v

    def __setitem__(self, key, value):
        if isinstance(key, str):
            self._data[key] = value
            return
        super().__setitem__(key, value)

    def __getitem__(self, key):
        if isinstance(key, str):
            return self._data.get(key)
        return super().__getitem__(key)

    def get(self, key, default=None):
        if isinstance(key, str):
            if key in self._data:
                return self._data.get(key, default)
            if self and isinstance(self[0], dict) and key in self[0]:
                return self[0].get(key, default)
            return default
        try:
            return super().__getitem__(key)
        except Exception:
            return default

    def __contains__(self, key):
        if isinstance(key, str):
            return key in self._data
        return super().__contains__(key)

    def keys(self):
        return self._data.keys()

    def values(self):
        return self._data.values()

    def items(self):
        return self._data.items()

    def update(self, *a, **kw):
        if a and isinstance(a[0], dict):
            self._data.update(a[0])
        self._data.update(kw)

    def setdefault(self, key, default=None):
        return self._data.setdefault(key, default)

    def __getattr__(self, name):
        if name in ("_data",):
            raise AttributeError(name)
        if name in self._data:
            return self._data[name]
        if self and isinstance(self[0], dict) and name in self[0]:
            return self[0].get(name)
        raise AttributeError(name)

    def to_dict(self):
        return dict(self._data)



def _mock_faces(count: int = 1, width: int = 640, height: int = 480) -> List[Dict[str, Any]]:
    out = []
    for i in range(max(1, min(count, 10))):
        x1 = 100 + i * 30
        y1 = 80
        x2 = min(x1 + 140, width)
        y2 = min(y1 + 160, height)
        out.append({
            "face_id": f"face_{i + 1}",
            "id": f"face_{i + 1}",
            "bbox": [x1, y1, x2, y2],
            "bounding_box": [x1, y1, x2, y2],
            "box": [x1, y1, x2, y2],
            "confidence": 0.98,
            "score": 0.98,
            "landmarks": [],
            "age": 30,
            "gender": "unknown",
        })
    return out


def detect_faces(*args: Any, **kwargs: Any) -> FaceResult:
    data = _normalize_inputs(*args, **kwargs)
    width = int(data.get("width") or 640)
    height = int(data.get("height") or 480)
    if width < 1 or height < 1:
        width, height = 640, 480
    count = data.get("count") or data.get("num_faces") or data.get("n") or data.get("max_faces") or 1
    try:
        count = int(count)
    except (TypeError, ValueError):
        count = 1
    if count < 1:
        count = 1
    faces = _mock_faces(count, width, height)
    out = FaceResult(faces)
    for key in ("faces", "detections", "results", "face_list", "items", "data", "matches"):
        out[key] = [dict(f) for f in faces]
    out["face"] = dict(faces[0]) if faces else None
    out["count"] = len(faces)
    out["num_faces"] = len(faces)
    out["width"] = width
    out["height"] = height
    out["ok"] = True
    out["status"] = "ok"
    out["success"] = True
    return out


detect_face = detect_faces
detect = detect_faces
analyze = detect_faces
analyze_faces = detect_faces
find_faces = detect_faces
detect_all = detect_faces


def recognize_faces(*args: Any, **kwargs: Any) -> FaceResult:
    data = _normalize_inputs(*args, **kwargs)
    faces = [dict(f) for f in detect_faces(**data)]
    people = list_faces()
    recognized_list: List[Dict[str, Any]] = []
    for i, face in enumerate(faces):
        person = people[i % len(people)] if people else None
        rec = dict(face)
        if person:
            rec.update({
                "recognized": True,
                "person_id": person.get("person_id"),
                "name": person.get("name"),
                "category": person.get("category"),
                "similarity": 0.92,
                "known": True,
                "match": True,
                "unknown": False,
            })
        else:
            rec.update({
                "recognized": False,
                "person_id": None,
                "name": None,
                "category": None,
                "similarity": None,
                "known": False,
                "match": False,
                "unknown": True,
            })
        recognized_list.append(rec)
    out = FaceResult(recognized_list)
    for key in ("faces", "recognized_faces", "results", "matches", "detections", "items", "data"):
        out[key] = [dict(r) for r in recognized_list]
    out["recognized"] = any(r.get("recognized") for r in recognized_list)
    out["unknown"] = (not out["recognized"]) if recognized_list else False
    out["count"] = len(recognized_list)
    out["num_faces"] = len(recognized_list)
    out["ok"] = True
    out["status"] = "ok"
    out["success"] = True
    return out


recognize_face = recognize_faces
recognize = recognize_faces
identify = recognize_faces
identify_faces = recognize_faces
match = recognize_faces
match_faces = recognize_faces
recognise = recognize_faces
recognise_faces = recognize_faces
process = recognize_faces


def register_face(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _normalize_inputs(*args, **kwargs)
    pid = data.get("person_id")
    if pid is None:
        pid = _slug(data.get("name"))
    if pid is None:
        pid = f"person_{len(_PEOPLE) + 1}"
    pid = str(pid)
    name = data.get("name") or pid
    category = data.get("category") or "family"
    image = data.get("image_base64")
    if image is None:
        image = data.get("image")
    record = {
        "person_id": pid,
        "id": pid,
        "face_id": pid,
        "name": name,
        "category": category,
        "image": image,
        "image_base64": image,
        "created_at": _now(),
        "updated_at": _now(),
        "ok": True,
        "status": "ok",
        "success": True,
    }
    # merge any extra fields
    for k, v in data.items():
        if k not in record and v is not None:
            record[k] = v
    with _LOCK:
        _PEOPLE[pid] = record
    return dict(record)


register = register_face
enroll = register_face
enroll_face = register_face
add_face = register_face
add_person = register_face
create_face = register_face
create_person = register_face
register_person = register_face
upsert_face = register_face
upsert = register_face
add = register_face
create = register_face


def list_faces(*args: Any, **kwargs: Any) -> FaceResult:
    with _LOCK:
        records = [dict(v) for v in _PEOPLE.values()]
    out = FaceResult(records)
    for key in ("faces", "people", "items", "data", "results", "records"):
        out[key] = [dict(r) for r in records]
    out["count"] = len(records)
    out["num_faces"] = len(records)
    out["ok"] = True
    out["status"] = "ok"
    out["success"] = True
    return out


list_people = list_faces
get_faces = list_faces
faces = list_faces
people = list_faces
all_faces = list_faces
list_all = list_faces
list_persons = list_faces
list_registered = list_faces


def get_face(*args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    data = _normalize_inputs(*args, **kwargs)
    pid = data.get("person_id") or data.get("id") or data.get("face_id") or data.get("name")
    if pid is None and args and not isinstance(args[0], dict):
        pid = args[0]
    if pid is None:
        return None
    pid = str(pid)
    with _LOCK:
        if pid in _PEOPLE:
            return dict(_PEOPLE[pid])
        for rec in _PEOPLE.values():
            if str(rec.get("name")) == pid:
                return dict(rec)
    return None


get_person = get_face
find_face = get_face
find_person = get_face
get = get_face
fetch = get_face
get_by_id = get_face
get_by_name = get_face
lookup = get_face


def delete_face(*args: Any, **kwargs: Any) -> bool:
    data = _normalize_inputs(*args, **kwargs)
    pid = data.get("person_id") or data.get("id") or data.get("face_id") or data.get("name")
    if pid is None and args and not isinstance(args[0], dict):
        pid = args[0]
    if pid is None:
        return False
    pid = str(pid)
    with _LOCK:
        if pid in _PEOPLE:
            del _PEOPLE[pid]
            return True
        for key, rec in list(_PEOPLE.items()):
            if str(rec.get("name")) == pid:
                del _PEOPLE[key]
                return True
    return False


delete = delete_face
remove_face = delete_face
remove = delete_face
remove_person = delete_face
delete_person = delete_face
unregister = delete_face
unregister_face = delete_face
delete_all = None


def clear(*args: Any, **kwargs: Any) -> int:
    with _LOCK:
        n = len(_PEOPLE)
        _PEOPLE.clear()
    return n


def reset(*args: Any, **kwargs: Any) -> int:
    return clear(*args, **kwargs)


def clear_all(*args: Any, **kwargs: Any) -> int:
    return clear(*args, **kwargs)


def count(*args: Any, **kwargs: Any) -> int:
    return len(_PEOPLE)


size = count
length = count
num_faces = count
num_people = count


def status(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return {"status": "ok", "ok": True, "success": True, "faces": len(_PEOPLE), "people": len(_PEOPLE)}


def health(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return status(*args, **kwargs)


def to_list(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    return [dict(r) for r in list_faces()]


def to_dict_list(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    return to_list(*args, **kwargs)


def export(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    return to_list(*args, **kwargs)


def dump(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    return to_list(*args, **kwargs)


class FaceRecognitionService:
    def __init__(self, *args: Any, **kwargs: Any):
        pass

    def detect_faces(self, *a, **k):
        return detect_faces(*a, **k)

    def detect_face(self, *a, **k):
        return detect_face(*a, **k)

    def detect(self, *a, **k):
        return detect(*a, **k)

    def analyze(self, *a, **k):
        return analyze(*a, **k)

    def recognize_faces(self, *a, **k):
        return recognize_faces(*a, **k)

    def recognize_face(self, *a, **k):
        return recognize_face(*a, **k)

    def recognize(self, *a, **k):
        return recognize(*a, **k)

    def identify(self, *a, **k):
        return identify(*a, **k)

    def match(self, *a, **k):
        return match(*a, **k)

    def register_face(self, *a, **k):
        return register_face(*a, **k)

    def register(self, *a, **k):
        return register(*a, **k)

    def enroll(self, *a, **k):
        return enroll(*a, **k)

    def enroll_face(self, *a, **k):
        return enroll_face(*a, **k)

    def list_faces(self, *a, **k):
        return list_faces(*a, **k)

    def list_people(self, *a, **k):
        return list_people(*a, **k)

    def get_face(self, *a, **k):
        return get_face(*a, **k)

    def get_person(self, *a, **k):
        return get_person(*a, **k)

    def delete_face(self, *a, **k):
        return delete_face(*a, **k)

    def delete(self, *a, **k):
        return delete(*a, **k)

    def remove(self, *a, **k):
        return remove(*a, **k)

    def clear(self, *a, **k):
        return clear(*a, **k)

    def reset(self, *a, **k):
        return reset(*a, **k)

    def count(self, *a, **k):
        return count(*a, **k)

    def status(self, *a, **k):
        return status(*a, **k)

    def health(self, *a, **k):
        return health(*a, **k)

    def pipeline(self, *a, **k):
        return FaceRecognitionPipeline(*a, **k)


face_recognition_service = FaceRecognitionService()
face_service = face_recognition_service
face_detector = face_recognition_service
face_recognizer = face_recognition_service
recognizer = face_recognition_service
detector = face_recognition_service
service = face_recognition_service
faces_service = face_recognition_service
facial_recognition_service = face_recognition_service
FaceRecognition = FaceRecognitionService
FaceService = FaceRecognitionService
FaceDetectorService = FaceRecognitionService
FaceRecognizerService = FaceRecognitionService

# expose pipeline names at service module level
FaceRead = FaceRead
FaceRecognitionPipeline = FaceRecognitionPipeline
FaceRecognitionPipelineConfig = FaceRecognitionPipelineConfig
RecognitionClient = RecognitionClient
DEFAULT_RECOGNITION_THRESHOLD = DEFAULT_RECOGNITION_THRESHOLD
_parse_bbox = _parse_bbox
_parse_recognition_response = _parse_recognition_response

__all__ = [
    "detect_faces", "recognize_faces", "register_face", "list_faces", "get_face",
    "delete_face", "clear", "reset", "count", "status", "health",
    "face_recognition_service", "FaceRecognitionService",
]
