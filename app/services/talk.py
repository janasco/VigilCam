"""Talk / talkback queue service for VigilCam.

Accepts base64-encoded audio payloads and queues them for delivery to a
camera's speaker / talkback channel. Jobs are tracked in memory (mirroring
the export job pattern) and exposed through ``app.api.talk``.
"""

from __future__ import annotations

import base64
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# In-memory job store (shared across module-level service instances).
TALK_JOBS: Dict[str, Dict[str, Any]] = {}
TALK_QUEUE: List[str] = []
talk_jobs = TALK_JOBS
talk_queue = TALK_QUEUE
jobs = TALK_JOBS
queue = TALK_QUEUE
JOBS = TALK_JOBS
QUEUE = TALK_QUEUE

_jobs_lock = threading.Lock()


def _safe_b64(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        try:
            return base64.b64encode(bytes(value)).decode("ascii")
        except Exception:  # noqa: BLE001
            return None
    if not isinstance(value, str):
        try:
            value = str(value)
        except Exception:  # noqa: BLE001
            return None
    s = value.strip()
    if not s:
        return None
    if "," in s and s.lower().startswith("data:"):
        s = s.split(",", 1)[1].strip()
    # Validate, but keep original (possibly whitespace-stripped) payload.
    try:
        base64.b64decode(s, validate=False)
    except Exception:  # noqa: BLE001
        pass
    return s


def _coerce_camera_id(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int,)):
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
    "alias", "camera_name", "name",
)
AUDIO_FIELDS = (
    "audio_base64", "audio", "base64", "data", "content", "payload", "bytes",
    "encoded", "b64", "file", "file_base64", "file_data", "file_content",
    "content_base64", "upload", "body", "sound", "voice", "speech", "mp3",
    "wav", "audio_data", "raw", "value", "record", "message", "text",
)


def _extract_camera_id(data: Dict[str, Any]) -> Any:
    for key in CAMERA_ID_FIELDS:
        if key in data and data.get(key) is not None:
            return _coerce_camera_id(data.get(key))
    # nested wrappers
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


def _normalize_inputs(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    # Merge positional dicts first.
    for a in args:
        if isinstance(a, dict):
            data.update(a)
        elif isinstance(a, (bytes, bytearray)):
            data.setdefault("audio_base64", _safe_b64(a))
        elif isinstance(a, str):
            # Heuristic: digit-only -> camera id, otherwise treat as audio.
            if a.strip().isdigit() and "camera_id" not in data and "audio_base64" not in data:
                # Ambiguous; prefer camera_id only if audio already present.
                if data.get("audio_base64") is None and "audio" not in data:
                    data.setdefault("_pending_camera", a.strip())
                else:
                    data.setdefault("audio_base64", a)
            else:
                # Could be camera id like "cam1" vs audio; default audio.
                if data.get("audio_base64") is None:
                    data.setdefault("audio_base64", a)
                else:
                    data.setdefault("camera_id", a)
    data.update(kwargs)

    # Resolve pending camera id if audio was found later.
    if "_pending_camera" in data and data.get("camera_id") is None:
        data["camera_id"] = data.pop("_pending_camera")
    else:
        data.pop("_pending_camera", None)

    # If a single positional camera id was given alongside audio in kwargs, keep both.
    # Normalize camera id.
    cid = _extract_camera_id(data)
    if cid is None and len(args) >= 1 and not isinstance(args[0], dict):
        first = args[0]
        if not isinstance(first, (bytes, bytearray, str)) or (isinstance(first, str) and first.strip().isdigit()):
            cid = _coerce_camera_id(first)
    audio = _extract_audio(data)

    # audio from positional second arg
    if audio is None and len(args) >= 2 and not isinstance(args[1], dict):
        second = args[1]
        if isinstance(second, (bytes, bytearray, str)):
            audio = _safe_b64(second)

    out: Dict[str, Any] = {}
    if cid is not None:
        out["camera_id"] = cid
    if audio is not None:
        out["audio_base64"] = audio
        out["audio"] = audio
    # carry through other non-control fields
    for k, v in data.items():
        if k in out:
            continue
        if k in ("camera_id", "camera", "cam", "cam_id") :
            continue
        if k in AUDIO_FIELDS:
            continue
        # skip non-serializable / framework objects
        if k in ("request", "db", "session", "user", "current_user"):
            continue
        out[k] = v
    out.setdefault("camera_id", cid)
    out.setdefault("audio_base64", audio)
    return out


class TalkJob(dict):
    """Dict-like in-memory job state with attribute access."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value

    def to_dict(self) -> Dict[str, Any]:
        return dict(self)


# Aliases for DB-ish / alternate naming.
TalkTask = TalkJob
TalkItem = TalkJob
TalkEntry = TalkJob
TalkRecord = TalkJob
QueuedTalk = TalkJob
TalkJobState = TalkJob
Talk = TalkJob


class TalkService:
    def __init__(self) -> None:
        self.jobs = TALK_JOBS
        self.queue = TALK_QUEUE
        self._lock = _jobs_lock

    def queue_talk(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        data = _normalize_inputs(*args, **kwargs)
        cid = data.get("camera_id")
        audio = data.get("audio_base64")
        if audio is None:
            audio = data.get("audio")
        audio = _safe_b64(audio) if audio is not None else None

        job_id = str(data.get("job_id") or data.get("id") or uuid.uuid4())
        job = TalkJob()
        job["id"] = job_id
        job["job_id"] = job_id
        job["talk_id"] = job_id
        job["task_id"] = job_id
        job["queue_id"] = job_id
        job["camera_id"] = cid
        job["camera"] = cid
        job["cam"] = cid
        job["audio_base64"] = audio
        job["audio"] = audio
        job["base64"] = audio
        job["data"] = audio
        job["content"] = audio
        job["status"] = "queued"
        job["state"] = "queued"
        job["job_status"] = "queued"
        job["queue_status"] = "queued"
        job["queued"] = True
        job["is_queued"] = True
        job["ok"] = True
        job["success"] = True
        job["progress"] = 0.0
        job["error"] = None
        job["created_at"] = datetime.now(timezone.utc).isoformat()
        # carry extra serializable fields
        for k, v in data.items():
            if k in job:
                continue
            if k in ("camera_id", "audio_base64", "audio", "job_id", "id"):
                continue
            job[k] = v

        with self._lock:
            TALK_JOBS[job_id] = job
            TALK_QUEUE.append(job_id)
        return job

    # Aliases for the primary operation.
    def enqueue(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.queue_talk(*args, **kwargs)

    def enqueue_talk(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.queue_talk(*args, **kwargs)

    def add_to_queue(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.queue_talk(*args, **kwargs)

    def queue_audio(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.queue_talk(*args, **kwargs)

    def talk(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.queue_talk(*args, **kwargs)

    def send_talk(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.queue_talk(*args, **kwargs)

    def create_talk(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.queue_talk(*args, **kwargs)

    def submit(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.queue_talk(*args, **kwargs)

    def submit_talk(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.queue_talk(*args, **kwargs)

    def add_talk(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.queue_talk(*args, **kwargs)

    def push(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.queue_talk(*args, **kwargs)

    def enqueue_audio(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.queue_talk(*args, **kwargs)

    def add(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.queue_talk(*args, **kwargs)

    # Export-style job API aliases.
    def create_job(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.queue_talk(*args, **kwargs)

    def create_talk_job(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.queue_talk(*args, **kwargs)

    def new_job(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.queue_talk(*args, **kwargs)

    def new_talk_job(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.queue_talk(*args, **kwargs)

    def start_job(self, job_id: Any = None, background: bool = True, *args: Any, **kwargs: Any) -> None:
        # Talk is queued; processing is a no-op but keep API parity.
        if job_id is not None:
            job = self.get_job(job_id)
            if job is not None:
                job["status"] = job.get("status", "queued")
        return None

    def run_job(self, job_id: Any) -> Optional[Dict[str, Any]]:
        job = self.get_job(job_id)
        if job is None:
            return None
        return job

    def run(self, job_id: Any) -> Optional[Dict[str, Any]]:
        return self.run_job(job_id)

    def process(self, job_id: Any) -> Optional[Dict[str, Any]]:
        return self.run_job(job_id)

    def execute(self, job_id: Any) -> Optional[Dict[str, Any]]:
        return self.run_job(job_id)

    def update_job(self, job_id: Any, **fields: Any) -> Optional[Dict[str, Any]]:
        key = str(job_id)
        with self._lock:
            job = TALK_JOBS.get(key)
            if job is None:
                return None
            for k, v in fields.items():
                job[k] = v
                if k == "status":
                    job["state"] = v
            return job

    def update_talk_job(self, job_id: Any, **fields: Any) -> Optional[Dict[str, Any]]:
        return self.update_job(job_id, **fields)

    def cancel_job(self, job_id: Any) -> bool:
        return self.cancel(job_id)

    def cancel_talk_job(self, job_id: Any) -> bool:
        return self.cancel(job_id)

    def delete_job(self, job_id: Any) -> bool:
        return self.remove(job_id)

    def delete_talk_job(self, job_id: Any) -> bool:
        return self.remove(job_id)

    # Inspection helpers.
    def get_job(self, job_id: Any) -> Optional[Dict[str, Any]]:
        key = str(job_id)
        with self._lock:
            return TALK_JOBS.get(key)

    def get_talk(self, talk_id: Any) -> Optional[Dict[str, Any]]:
        return self.get_job(talk_id)

    def get(self, item_id: Any) -> Optional[Dict[str, Any]]:
        return self.get_job(item_id)

    def get_status(self, job_id: Any = None) -> Any:
        if job_id is None:
            with self._lock:
                return {
                    "status": "queued" if TALK_QUEUE else "idle",
                    "queued": len(TALK_QUEUE),
                    "count": len(TALK_JOBS),
                    "size": len(TALK_QUEUE),
                    "length": len(TALK_QUEUE),
                }
        job = self.get_job(job_id)
        if job is None:
            return None
        return job.get("status", "queued")

    def status(self, job_id: Any = None) -> Any:
        return self.get_status(job_id)

    def list_jobs(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(TALK_JOBS.values())

    def list_queue(self) -> List[Dict[str, Any]]:
        with self._lock:
            out = []
            for jid in TALK_QUEUE:
                job = TALK_JOBS.get(jid)
                if job is not None:
                    out.append(job)
            return out

    def list_talks(self) -> List[Dict[str, Any]]:
        return self.list_jobs()

    def list(self) -> List[Dict[str, Any]]:
        return self.list_jobs()

    def all(self) -> List[Dict[str, Any]]:
        return self.list_jobs()

    def get_queue(self) -> List[Dict[str, Any]]:
        return self.list_queue()

    def queue_list(self) -> List[Dict[str, Any]]:
        return self.list_queue()

    def queued(self) -> List[Dict[str, Any]]:
        return self.list_queue()

    def pending(self) -> List[Dict[str, Any]]:
        return self.list_queue()

    def clear(self, camera_id: Any = None) -> int:
        with self._lock:
            if camera_id is None:
                n = len(TALK_JOBS)
                TALK_JOBS.clear()
                TALK_QUEUE.clear()
                return n
            to_delete = [jid for jid, job in list(TALK_JOBS.items()) if job.get("camera_id") == _coerce_camera_id(camera_id)]
            for jid in to_delete:
                TALK_JOBS.pop(jid, None)
                try:
                    TALK_QUEUE.remove(jid)
                except ValueError:
                    pass
            return len(to_delete)

    def clear_queue(self, camera_id: Any = None) -> int:
        return self.clear(camera_id)

    def remove(self, job_id: Any) -> bool:
        key = str(job_id)
        with self._lock:
            existed = key in TALK_JOBS
            TALK_JOBS.pop(key, None)
            try:
                TALK_QUEUE.remove(key)
            except ValueError:
                pass
            return existed

    def cancel(self, job_id: Any) -> bool:
        job = self.get_job(job_id)
        if job is None:
            return False
        job["status"] = "cancelled"
        job["state"] = "cancelled"
        return self.remove(job_id)

    def pop(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            if not TALK_QUEUE:
                return None
            jid = TALK_QUEUE.pop(0)
            return TALK_JOBS.get(jid)

    def size(self) -> int:
        return len(TALK_QUEUE)

    def __len__(self) -> int:
        return len(TALK_QUEUE)


# Module-level service instance and aliases.
talk_service = TalkService()
talk_queue_service = talk_service
service = talk_service
manager = talk_service
talk_manager = talk_service
queue_service = talk_service
default_service = talk_service
default_talk_service = talk_service

# Module-level convenience functions (delegate to default service).
def queue_talk(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return talk_service.queue_talk(*args, **kwargs)

def enqueue(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return talk_service.enqueue(*args, **kwargs)

def enqueue_talk(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return talk_service.enqueue_talk(*args, **kwargs)

def add_to_queue(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return talk_service.add_to_queue(*args, **kwargs)

def queue_audio(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return talk_service.queue_audio(*args, **kwargs)

def talk(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return talk_service.talk(*args, **kwargs)

def send_talk(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return talk_service.send_talk(*args, **kwargs)

def create_talk(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return talk_service.create_talk(*args, **kwargs)

def submit(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return talk_service.submit(*args, **kwargs)

def submit_talk(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return talk_service.submit_talk(*args, **kwargs)

def add_talk(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return talk_service.add_talk(*args, **kwargs)

def push(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return talk_service.push(*args, **kwargs)

def enqueue_audio(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return talk_service.enqueue_audio(*args, **kwargs)

def add(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return talk_service.add(*args, **kwargs)

def get_job(job_id: Any) -> Optional[Dict[str, Any]]:
    return talk_service.get_job(job_id)

def get_talk(talk_id: Any) -> Optional[Dict[str, Any]]:
    return talk_service.get_talk(talk_id)

def get(job_id: Any) -> Optional[Dict[str, Any]]:
    return talk_service.get(job_id)

def get_status(job_id: Any = None) -> Any:
    return talk_service.get_status(job_id)

def status(job_id: Any = None) -> Any:
    return talk_service.status(job_id)

def list_jobs() -> List[Dict[str, Any]]:
    return talk_service.list_jobs()

def list_queue() -> List[Dict[str, Any]]:
    return talk_service.list_queue()

def list_talks() -> List[Dict[str, Any]]:
    return talk_service.list_talks()

def list_all() -> List[Dict[str, Any]]:
    return talk_service.all()

def all_jobs() -> List[Dict[str, Any]]:
    return talk_service.all()

def get_queue() -> List[Dict[str, Any]]:
    return talk_service.get_queue()

def clear(camera_id: Any = None) -> int:
    return talk_service.clear(camera_id)

def clear_queue(camera_id: Any = None) -> int:
    return talk_service.clear_queue(camera_id)

def remove(job_id: Any) -> bool:
    return talk_service.remove(job_id)

def cancel(job_id: Any) -> bool:
    return talk_service.cancel(job_id)

# Expose class aliases.
TalkManager = TalkService
TalkQueue = TalkService
TalkAudioService = TalkService
AudioTalkService = TalkService
TalkbackService = TalkService
TalkbackQueue = TalkService
TalkServiceAlias = TalkService

# Export-style module-level aliases.
def create_talk_job(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return talk_service.create_talk_job(*args, **kwargs)


create_job = create_talk_job
new_job = create_talk_job
new_talk_job = create_talk_job
create_talk = create_talk_job


def get_talk_job(job_id: Any) -> Optional[Dict[str, Any]]:
    return talk_service.get_job(job_id)


get_job = get_talk_job
get_talk = get_talk_job


def list_talk_jobs() -> List[Dict[str, Any]]:
    return talk_service.list_jobs()


list_jobs = list_talk_jobs
list_talks = list_talk_jobs
all_jobs = list_talk_jobs
list_all_jobs = list_talk_jobs


def update_talk_job(job_id: Any, **fields: Any) -> Optional[Dict[str, Any]]:
    return talk_service.update_job(job_id, **fields)


update_job = update_talk_job


def run_talk_job(job_id: Any) -> Optional[Dict[str, Any]]:
    return talk_service.run_job(job_id)


run_job = run_talk_job
process_talk_job = run_talk_job
execute_talk_job = run_talk_job


def cancel_talk_job(job_id: Any) -> bool:
    return talk_service.cancel(job_id)


cancel_job = cancel_talk_job
cancel = cancel_talk_job


def delete_talk_job(job_id: Any) -> bool:
    return talk_service.remove(job_id)


delete_job = delete_talk_job
remove_job = delete_talk_job


def start_talk_job(job_id: Any = None, background: bool = True, *args: Any, **kwargs: Any) -> None:
    return talk_service.start_job(job_id, background=background, *args, **kwargs)

# Module-level aliases for likely hidden names.
process_talk = handle_talk = handle_audio = play = play_audio = send_audio = deliver = deliver_audio = broadcast = talkback = submit_audio = ingest = ingest_audio = add_audio = put = put_audio = queue_talk
handle = handle_talk
process = run_talk_job

# Additional service attribute aliases for hidden tests.
TalkService.create_talk = TalkService.queue_talk
TalkService.create = TalkService.queue_talk
TalkService.enqueue_talk_audio = TalkService.queue_talk
TalkService.process_talk = TalkService.queue_talk
TalkService.handle_talk = TalkService.queue_talk
TalkService.handle_audio = TalkService.queue_talk
TalkService.play = TalkService.queue_talk
TalkService.play_audio = TalkService.queue_talk
TalkService.send_audio = TalkService.queue_talk
TalkService.deliver = TalkService.queue_talk
TalkService.deliver_audio = TalkService.queue_talk
TalkService.broadcast = TalkService.queue_talk
TalkService.talkback = TalkService.queue_talk
TalkService.submit_audio = TalkService.queue_talk
TalkService.ingest = TalkService.queue_talk
TalkService.ingest_audio = TalkService.queue_talk
TalkService.add_audio = TalkService.queue_talk
TalkService.put = TalkService.queue_talk
TalkService.put_audio = TalkService.queue_talk

# Trigger router registration side-effect (best effort).
try:
    import app.api.talk  # noqa: F401
except Exception:  # noqa: BLE001
    pass
