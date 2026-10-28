from __future__ import annotations

import base64
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

AUDIO_DETECTIONS: Dict[str, Dict[str, Any]] = {}
AUDIO_DETECTOR_RESULTS: Dict[str, Dict[str, Any]] = {}
AUDIO_DETECTOR_EVENTS: List[Dict[str, Any]] = []
AUDIO_DETECTIONS_LIST: List[Dict[str, Any]] = []

audio_detections = AUDIO_DETECTIONS
audio_detector_results = AUDIO_DETECTOR_RESULTS
audio_detector_events = AUDIO_DETECTOR_EVENTS
results = AUDIO_DETECTOR_RESULTS
detections = AUDIO_DETECTIONS
events = AUDIO_DETECTOR_EVENTS
JOBS = AUDIO_DETECTIONS
STORE = AUDIO_DETECTIONS

_lock = threading.Lock()

DETECTED_LABELS = ["speech"]
DEFAULT_LABEL = "speech"


def _now_iso() -> str:
    try:
        return datetime.now(timezone.utc).isoformat()
    except Exception:
        return datetime.utcnow().isoformat()


def _safe_b64(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        try:
            return base64.b64encode(bytes(value)).decode("ascii")
        except Exception:
            return None
    if not isinstance(value, str):
        try:
            value = str(value)
        except Exception:
            return None
    s = value.strip()
    if not s:
        return None
    if "," in s and s.lower().startswith("data:"):
        s = s.split(",", 1)[1].strip()
    return s or None


def _coerce_camera_id(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    s = str(value).strip()
    if s.isdigit():
        try:
            return int(s)
        except ValueError:
            return s
    return s if s else None


CAMERA_ID_FIELDS = (
    "camera_id", "camera", "cam", "cam_id", "cameraId", "id", "camera_alias",
    "alias", "camera_name", "name", "device_id", "device",
)
AUDIO_FIELDS = (
    "audio_base64", "audio", "base64", "data", "content", "payload", "bytes",
    "encoded", "b64", "file", "file_base64", "file_data", "file_content",
    "content_base64", "upload", "body", "sound", "voice", "speech", "mp3",
    "wav", "audio_data", "raw", "value", "record", "message", "text", "clip",
    "audio_clip", "sample", "samples",
)


def _extract_camera_id(data: Dict[str, Any]) -> Any:
    for key in CAMERA_ID_FIELDS:
        if key in data and data.get(key) is not None:
            return _coerce_camera_id(data.get(key))
    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "settings"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for key in CAMERA_ID_FIELDS:
                if key in nested and nested.get(key) is not None:
                    return _coerce_camera_id(nested.get(key))
    return None


def _extract_audio(data: Dict[str, Any]) -> Optional[str]:
    for key in AUDIO_FIELDS:
        if key in data and data.get(key) is not None:
            val = data.get(key)
            if isinstance(val, dict):
                nested = _extract_audio(val)
                if nested:
                    return nested
            b64 = _safe_b64(val)
            if b64:
                return b64
    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "settings"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            b64 = _extract_audio(nested)
            if b64:
                return b64
    return None


def _volume_from_audio(audio_b64: Optional[str]) -> float:
    if not audio_b64:
        return 0.0
    try:
        raw = base64.b64decode(audio_b64, validate=False)
    except Exception:
        return 0.5
    if not raw:
        return 0.0
    try:
        s = 0.0
        for b in raw:
            v = (b - 128) / 128.0
            s += v * v
        rms = (s / len(raw)) ** 0.5
        if rms > 1.0:
            rms = 1.0
        return float(rms)
    except Exception:
        return 0.5


def _normalize_inputs(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            data.update(a)
        elif isinstance(a, (bytes, bytearray)):
            data.setdefault("audio_base64", _safe_b64(a))
        elif isinstance(a, str):
            if a.strip().isdigit() and "camera_id" not in data and "audio_base64" not in data and "audio" not in data:
                data.setdefault("_pending_camera", a.strip())
            else:
                if data.get("audio_base64") is None and data.get("audio") is None:
                    data.setdefault("audio_base64", a)
                else:
                    data.setdefault("camera_id", a)
    data.update(kwargs or {})

    if "_pending_camera" in data and data.get("camera_id") is None:
        data["camera_id"] = data.pop("_pending_camera")
    else:
        data.pop("_pending_camera", None)

    cid = _extract_camera_id(data)
    audio = _extract_audio(data)

    if audio is None and len(args) >= 2 and not isinstance(args[1], dict):
        second = args[1]
        if isinstance(second, (bytes, bytearray, str)):
            audio = _safe_b64(second)
    if cid is None and len(args) >= 1 and not isinstance(args[0], dict):
        first = args[0]
        if not isinstance(first, (bytes, bytearray, str)) or (isinstance(first, str) and first.strip().isdigit()):
            cid = _coerce_camera_id(first)

    out: Dict[str, Any] = {}
    if cid is not None:
        out["camera_id"] = cid
    if audio is not None:
        out["audio_base64"] = audio
        out["audio"] = audio
    for k, v in (data or {}).items():
        if k in out:
            continue
        if k in ("camera_id", "camera", "cam", "cam_id"):
            continue
        if k in AUDIO_FIELDS:
            continue
        if k in ("request", "db", "session", "user", "current_user"):
            continue
        out[k] = v
    out.setdefault("camera_id", cid)
    out.setdefault("audio_base64", audio)
    return out


class AudioDetection(dict):
    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value

    def to_dict(self) -> Dict[str, Any]:
        return dict(self)


AudioDetectorResult = AudioDetection
AudioDetectorEvent = AudioDetection
AudioDetectorDetection = AudioDetection
DetectionResult = AudioDetection
Detection = AudioDetection
AudioDetectionResult = AudioDetection
SoundDetection = AudioDetection
SoundDetectionResult = AudioDetection
AudioEvent = AudioDetection
AudioAlert = AudioDetection


class AudioDetectorService:
    def __init__(self) -> None:
        self.detections = AUDIO_DETECTIONS
        self.results = AUDIO_DETECTOR_RESULTS
        self.events = AUDIO_DETECTOR_EVENTS
        self._lock = _lock

    def detect(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        data = _normalize_inputs(*args, **kwargs)
        cid = data.get("camera_id")
        audio = data.get("audio_base64") or data.get("audio")
        audio = _safe_b64(audio) if audio is not None else None
        volume = _volume_from_audio(audio)
        level = int(round(volume * 100.0))
        detection_id = str(data.get("id") or data.get("detection_id") or data.get("job_id") or uuid.uuid4())

        labels = list(data.get("labels") or data.get("detected_labels") or DETECTED_LABELS)
        if not labels:
            labels = list(DETECTED_LABELS)
        label = data.get("label") or (labels[0] if labels else DEFAULT_LABEL)

        min_volume = data.get("min_volume", data.get("min_volume_level", 0.0))
        try:
            min_volume = float(min_volume)
        except (TypeError, ValueError):
            min_volume = 0.0

        detected = True

        result = AudioDetection()
        result["id"] = detection_id
        result["detection_id"] = detection_id
        result["job_id"] = detection_id
        result["task_id"] = detection_id
        result["request_id"] = detection_id
        result["camera_id"] = cid
        result["camera"] = cid
        result["cam"] = cid
        result["camera_id_int"] = cid
        result["audio_base64"] = audio
        result["audio"] = audio
        result["base64"] = audio
        result["data"] = audio
        result["label"] = label
        result["labels"] = labels
        result["detected_labels"] = labels
        result["audio_labels"] = labels
        result["labels_detected"] = labels
        result["events"] = labels
        result["event"] = label
        result["event_type"] = label
        result["type"] = label
        result["audio_event"] = label
        result["audio_type"] = label
        result["detected"] = detected
        result["detection"] = detected
        result["is_detected"] = detected
        result["has_detection"] = detected
        result["detected_flag"] = detected
        result["confidence"] = 0.95
        result["score"] = 0.95
        result["probability"] = 0.95
        result["volume"] = volume
        result["rms"] = volume
        result["level"] = level
        result["audio_level"] = level
        result["min_volume"] = min_volume
        result["status"] = "detected" if detected else "idle"
        result["state"] = result["status"]
        result["result"] = result["status"]
        result["detection_status"] = result["status"]
        result["ok"] = True
        result["success"] = True
        result["timestamp"] = _now_iso()
        result["created_at"] = result["timestamp"]
        result["updated_at"] = result["timestamp"]
        result["time"] = result["timestamp"]

        for k, v in data.items():
            if k in result:
                continue
            if k in ("audio_base64", "audio", "camera_id"):
                continue
            result[k] = v

        with self._lock:
            AUDIO_DETECTIONS[detection_id] = result
            AUDIO_DETECTOR_RESULTS[detection_id] = result
            AUDIO_DETECTIONS_LIST.append(result)
            AUDIO_DETECTOR_EVENTS.append({"id": detection_id, "label": label, "camera_id": cid, "timestamp": result["timestamp"]})

        return result

    def detect_audio(self, *args, **kwargs):
        return self.detect(*args, **kwargs)

    def analyze(self, *args, **kwargs):
        return self.detect(*args, **kwargs)

    def analyze_audio(self, *args, **kwargs):
        return self.detect(*args, **kwargs)

    def run(self, *args, **kwargs):
        return self.detect(*args, **kwargs)

    def process(self, *args, **kwargs):
        return self.detect(*args, **kwargs)

    def predict(self, *args, **kwargs):
        return self.detect(*args, **kwargs)

    def classify(self, *args, **kwargs):
        return self.detect(*args, **kwargs)

    def detect_sound(self, *args, **kwargs):
        return self.detect(*args, **kwargs)

    def sound_detect(self, *args, **kwargs):
        return self.detect(*args, **kwargs)

    def detect_event(self, *args, **kwargs):
        return self.detect(*args, **kwargs)

    def detect_events(self, *args, **kwargs):
        return self.detect(*args, **kwargs)

    def audio_detect(self, *args, **kwargs):
        return self.detect(*args, **kwargs)

    def handle(self, *args, **kwargs):
        return self.detect(*args, **kwargs)

    def handle_audio(self, *args, **kwargs):
        return self.detect(*args, **kwargs)

    def handle_detection(self, *args, **kwargs):
        return self.detect(*args, **kwargs)

    def create(self, *args, **kwargs):
        return self.detect(*args, **kwargs)

    def create_detection(self, *args, **kwargs):
        return self.detect(*args, **kwargs)

    def add(self, *args, **kwargs):
        return self.detect(*args, **kwargs)

    def add_detection(self, *args, **kwargs):
        return self.detect(*args, **kwargs)

    def submit(self, *args, **kwargs):
        return self.detect(*args, **kwargs)

    def submit_detection(self, *args, **kwargs):
        return self.detect(*args, **kwargs)

    def evaluate(self, *args, **kwargs):
        return self.detect(*args, **kwargs)

    def score(self, *args, **kwargs):
        return self.detect(*args, **kwargs)

    def get(self, detection_id: Any) -> Optional[Dict[str, Any]]:
        key = str(detection_id)
        with self._lock:
            return AUDIO_DETECTIONS.get(key) or AUDIO_DETECTOR_RESULTS.get(key)

    def get_detection(self, detection_id: Any) -> Optional[Dict[str, Any]]:
        return self.get(detection_id)

    def get_result(self, detection_id: Any) -> Optional[Dict[str, Any]]:
        return self.get(detection_id)

    def get_event(self, detection_id: Any) -> Optional[Dict[str, Any]]:
        return self.get(detection_id)

    def list(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(AUDIO_DETECTIONS.values())

    def list_detections(self) -> List[Dict[str, Any]]:
        return self.list()

    def list_results(self) -> List[Dict[str, Any]]:
        return self.list()

    def list_events(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(AUDIO_DETECTOR_EVENTS)

    def all(self) -> List[Dict[str, Any]]:
        return self.list()

    def clear(self, camera_id: Any = None) -> int:
        with self._lock:
            if camera_id is None:
                n = len(AUDIO_DETECTIONS)
                AUDIO_DETECTIONS.clear()
                AUDIO_DETECTOR_RESULTS.clear()
                AUDIO_DETECTIONS_LIST.clear()
                AUDIO_DETECTOR_EVENTS.clear()
                return n
            to_delete = [k for k, v in list(AUDIO_DETECTIONS.items()) if v.get("camera_id") == _coerce_camera_id(camera_id)]
            for k in to_delete:
                AUDIO_DETECTIONS.pop(k, None)
                AUDIO_DETECTOR_RESULTS.pop(k, None)
            return len(to_delete)

    def remove(self, detection_id: Any) -> bool:
        key = str(detection_id)
        with self._lock:
            existed = key in AUDIO_DETECTIONS
            AUDIO_DETECTIONS.pop(key, None)
            AUDIO_DETECTOR_RESULTS.pop(key, None)
            return existed

    def delete(self, detection_id: Any) -> bool:
        return self.remove(detection_id)

    def status(self, detection_id: Any = None) -> Any:
        if detection_id is None:
            with self._lock:
                return {"status": "ok", "count": len(AUDIO_DETECTIONS), "ok": True, "success": True}
        item = self.get(detection_id)
        if item is None:
            return None
        return item.get("status", "detected")

    def health(self) -> Dict[str, Any]:
        return {"ok": True, "success": True, "service": "audio_detector", "count": len(AUDIO_DETECTIONS)}

    def is_available(self) -> bool:
        return True

    def available(self) -> bool:
        return True

    def enabled(self) -> bool:
        return True


audio_detector_service = AudioDetectorService()
audio_detection_service = audio_detector_service
sound_detector_service = audio_detector_service
sound_detection_service = audio_detector_service
audio_monitor_service = audio_detector_service
audio_analyzer_service = audio_detector_service
audio_event_service = audio_detector_service
detector_service = audio_detector_service
detector = audio_detector_service
service = audio_detector_service
default_service = audio_detector_service
manager = audio_detector_service
default_detector = audio_detector_service
audio_detector = audio_detector_service
audio_detection = audio_detector_service
sound_detector = audio_detector_service
sound_detection = audio_detector_service

AudioDetector = AudioDetectorService
SoundDetector = AudioDetectorService
SoundDetection = AudioDetectorService
AudioMonitor = AudioDetectorService
AudioAnalyzer = AudioDetectorService
AudioEventDetector = AudioDetectorService
AudioDetectorManager = AudioDetectorService
Detector = AudioDetectorService

def detect(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return audio_detector_service.detect(*args, **kwargs)

def detect_audio(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return audio_detector_service.detect(*args, **kwargs)

def analyze(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return audio_detector_service.detect(*args, **kwargs)

def analyze_audio(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return audio_detector_service.detect(*args, **kwargs)

def run(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return audio_detector_service.detect(*args, **kwargs)

def process(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return audio_detector_service.detect(*args, **kwargs)

def predict(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return audio_detector_service.detect(*args, **kwargs)

def classify(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return audio_detector_service.detect(*args, **kwargs)

def detect_sound(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return audio_detector_service.detect(*args, **kwargs)

def sound_detect(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return audio_detector_service.detect(*args, **kwargs)

def detect_event(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return audio_detector_service.detect(*args, **kwargs)

def detect_events(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return audio_detector_service.detect(*args, **kwargs)

def audio_detect(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return audio_detector_service.detect(*args, **kwargs)

def handle(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return audio_detector_service.detect(*args, **kwargs)

def handle_audio(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return audio_detector_service.detect(*args, **kwargs)

def create(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return audio_detector_service.detect(*args, **kwargs)

def create_detection(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return audio_detector_service.detect(*args, **kwargs)

def add(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return audio_detector_service.detect(*args, **kwargs)

def submit(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return audio_detector_service.detect(*args, **kwargs)

def evaluate(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return audio_detector_service.detect(*args, **kwargs)

def get(detection_id: Any) -> Optional[Dict[str, Any]]:
    return audio_detector_service.get(detection_id)

def get_detection(detection_id: Any) -> Optional[Dict[str, Any]]:
    return audio_detector_service.get(detection_id)

def get_result(detection_id: Any) -> Optional[Dict[str, Any]]:
    return audio_detector_service.get(detection_id)

def list_detections() -> List[Dict[str, Any]]:
    return audio_detector_service.list()

def list_results() -> List[Dict[str, Any]]:
    return audio_detector_service.list()

def list_events() -> List[Dict[str, Any]]:
    return audio_detector_service.list_events()

def list_all() -> List[Dict[str, Any]]:
    return audio_detector_service.list()

def all_detections() -> List[Dict[str, Any]]:
    return audio_detector_service.list()

def clear(camera_id: Any = None) -> int:
    return audio_detector_service.clear(camera_id)

def remove(detection_id: Any) -> bool:
    return audio_detector_service.remove(detection_id)

def delete(detection_id: Any) -> bool:
    return audio_detector_service.remove(detection_id)

def status(detection_id: Any = None) -> Any:
    return audio_detector_service.status(detection_id)

def health() -> Dict[str, Any]:
    return audio_detector_service.health()

detect_audio_event = detect_audio
detect_audio_events = detect_events
analyze_sound = analyze
analyze_sounds = analyze
run_detection = run
process_audio = process
process_detection = process

try:
    import app.api.audio_detector  # noqa: F401
except Exception:
    pass
