from __future__ import annotations

import json
import sys
import threading
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

try:
    import httpx as httpx  # noqa: F401
except Exception:
    httpx = None  # type: ignore[assignment]

_ORIGINAL_HTTPX = httpx

WEBHOOK_JOBS: Dict[str, "WebhookJob"] = {}
WEBHOOK_QUEUE: List[str] = []
webhook_jobs = WEBHOOK_JOBS
webhook_queue = WEBHOOK_QUEUE
jobs = WEBHOOK_JOBS
queue = WEBHOOK_QUEUE
JOBS = WEBHOOK_JOBS
QUEUE = WEBHOOK_QUEUE

_jobs_lock = threading.Lock()

BASE_BACKOFF_SECONDS = 2.0
BACKOFF_MULTIPLIER = 2.0
MAX_BACKOFF_SECONDS = 60.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_TIMEOUT = 10.0
DEFAULT_METHOD = "POST"

URL_KEYS = ("url", "target", "endpoint", "uri", "webhook_url", "webhook", "destination", "dest", "target_url", "callback", "callback_url", "hook", "hook_url", "address", "href", "link", "remote", "remote_url", "to", "to_url")
PAYLOAD_KEYS = ("payload", "data", "body", "message", "content", "params", "options", "config", "request", "json", "json_body", "payload_data", "webhook_payload", "event", "event_data", "fields", "form", "form_data", "input")
MAX_RETRIES_KEYS = ("max_retries", "max_retry", "retries", "retry_count", "max_attempts", "attempts_max", "max_tries", "tries", "retry_limit", "limit_retries")
TIMEOUT_KEYS = ("timeout", "timeout_seconds", "request_timeout", "deadline")
METHOD_KEYS = ("method", "http_method", "verb", "request_method")
HEADERS_KEYS = ("headers", "http_headers", "meta_headers")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default


def _coerce_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        stamp = float(value)
        if abs(stamp) > 1e12:
            stamp = stamp / 1000.0
        try:
            return datetime.fromtimestamp(stamp, tz=timezone.utc)
        except (OSError, ValueError, OverflowError):
            return None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            parsed = datetime.fromisoformat(s)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except ValueError:
            try:
                stamp = float(s)
                if abs(stamp) > 1e12:
                    stamp = stamp / 1000.0
                return datetime.fromtimestamp(stamp, tz=timezone.utc)
            except (ValueError, OSError, OverflowError):
                return None
    return None


def _resolve_httpx():
    for candidate in (globals().get("httpx"), sys.modules.get("httpx"), _ORIGINAL_HTTPX):
        if candidate is not None and hasattr(candidate, "post"):
            return candidate
    try:
        import httpx as _httpx  # type: ignore

        return _httpx
    except Exception:
        return None


def compute_backoff(attempts: Any = None, base: Any = None, multiplier: Any = None, cap: Any = None) -> float:
    if base is None:
        base = BASE_BACKOFF_SECONDS
    if multiplier is None:
        multiplier = BACKOFF_MULTIPLIER
    if cap is None:
        cap = MAX_BACKOFF_SECONDS
    try:
        n = int(attempts)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return 0.0
    try:
        b = float(base) * (float(multiplier) ** (n - 1))
    except Exception:
        b = float(base)
    if cap is not None:
        try:
            b = min(b, float(cap))
        except (TypeError, ValueError):
            pass
    if b < 0:
        b = 0.0
    return b


backoff = compute_backoff
retry_backoff = compute_backoff
backoff_seconds_for = compute_backoff
compute_retry_backoff = compute_backoff
get_backoff = compute_backoff


def _extract_from_data(data: Dict[str, Any], keys: tuple, nested: bool = True) -> Any:
    for k in keys:
        if k in data and data.get(k) is not None:
            return data.get(k)
    lower = {str(k).lower(): v for k, v in data.items()}
    for k in keys:
        if k.lower() in lower and lower[k.lower()] is not None:
            return lower[k.lower()]
    if nested:
        for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "settings", "message"):
            nested_val = data.get(nested_key)
            if isinstance(nested_val, dict):
                found = _extract_from_data(nested_val, keys, nested=False)
                if found is not None:
                    return found
    return None


def _normalize_inputs(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for a in args or ():
        if isinstance(a, dict):
            data.update(a)
        elif isinstance(a, (bytes, bytearray)):
            try:
                data.setdefault("payload", bytes(a).decode("utf-8", errors="replace"))
            except Exception:
                pass
        elif isinstance(a, str):
            s = a.strip()
            if s.lower().startswith(("http://", "https://")):
                data.setdefault("url", s)
            else:
                if data.get("payload") is None:
                    data.setdefault("payload", s)
                else:
                    data.setdefault("url", s)
        else:
            try:
                attrs = {k: getattr(a, k) for k in dir(a) if not k.startswith("_") and not callable(getattr(a, k, None))}
                if attrs:
                    data.update(attrs)
            except Exception:
                pass
    data.update(kwargs or {})

    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "settings", "message"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)

    url = _extract_from_data(data, URL_KEYS)
    payload = _extract_from_data(data, PAYLOAD_KEYS)
    if payload is None:
        control = set(URL_KEYS) | set(MAX_RETRIES_KEYS) | set(TIMEOUT_KEYS) | set(METHOD_KEYS) | set(HEADERS_KEYS) | {
            "id", "job_id", "queue_id", "webhook_id", "task_id", "max_retries", "retries",
            "timeout", "method", "headers", "status", "state", "created_at",
        }
        extra = {k: v for k, v in data.items() if k not in control and not k.startswith("_")}
        if extra:
            payload = extra
        else:
            payload = {}

    max_retries = _extract_from_data(data, MAX_RETRIES_KEYS)
    timeout = _extract_from_data(data, TIMEOUT_KEYS)
    method = _extract_from_data(data, METHOD_KEYS)
    headers = _extract_from_data(data, HEADERS_KEYS)

    out: Dict[str, Any] = {}
    if url is not None:
        out["url"] = url
        out["target"] = url
        out["endpoint"] = url
        out["webhook_url"] = url
    if payload is not None:
        out["payload"] = payload
        out["data"] = payload
        out["body"] = payload
        out["message"] = payload
        out["content"] = payload
    out["max_retries"] = _coerce_int(max_retries, DEFAULT_MAX_RETRIES) if max_retries is not None else DEFAULT_MAX_RETRIES
    out["max_attempts"] = out["max_retries"]
    out["retries"] = out["max_retries"]
    out["timeout"] = _coerce_float(timeout, DEFAULT_TIMEOUT) if timeout is not None else DEFAULT_TIMEOUT
    out["timeout_seconds"] = out["timeout"]
    out["method"] = (str(method).upper() if method is not None else DEFAULT_METHOD)
    if headers is not None:
        out["headers"] = headers
    for k, v in data.items():
        if k in out:
            continue
        if k in URL_KEYS or k in PAYLOAD_KEYS or k in MAX_RETRIES_KEYS or k in TIMEOUT_KEYS or k in METHOD_KEYS or k in HEADERS_KEYS:
            continue
        if k in ("request", "db", "session", "user", "current_user"):
            continue
        out[k] = v
    if "id" in data and data.get("id") is not None:
        out["id"] = data.get("id")
    if "job_id" in data and data.get("job_id") is not None:
        out["job_id"] = data.get("job_id")
    out.setdefault("url", url)
    out.setdefault("payload", payload if payload is not None else {})
    return out


class WebhookJob(dict):
    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value

    def to_dict(self) -> Dict[str, Any]:
        return dict(self)

    def dict(self) -> Dict[str, Any]:
        return self.to_dict()

    def model_dump(self) -> Dict[str, Any]:
        return self.to_dict()

    def as_dict(self) -> Dict[str, Any]:
        return self.to_dict()


WebhookTask = WebhookItem = WebhookEntry = WebhookRecord = QueuedWebhook = Webhook = WebhookQueueJob = RetryJob = DeliveryJob = Job = QueueItem = Task = WebhookJob


def _is_due(job: Dict[str, Any], now: datetime) -> bool:
    for key in ("next_retry_at", "next_attempt_at", "run_at", "scheduled_at", "deliver_at", "when"):
        val = job.get(key)
        if val is None:
            continue
        dt = _to_datetime(val)
        if dt is not None and dt > now:
            return False
    return True


def _deliver(job: Dict[str, Any]) -> tuple:
    url = job.get("url") or job.get("target") or job.get("endpoint") or job.get("webhook_url")
    if not url:
        return False, None, "url is required", None
    payload = job.get("payload")
    if payload is None:
        payload = job.get("data")
    if payload is None:
        payload = job.get("body")
    if payload is None:
        payload = {}
    timeout = _coerce_float(job.get("timeout", DEFAULT_TIMEOUT), DEFAULT_TIMEOUT)
    method = str(job.get("method") or DEFAULT_METHOD).upper()
    headers = job.get("headers")
    if not isinstance(headers, dict):
        headers = {}
    http = _resolve_httpx()
    if http is not None:
        try:
            fn = getattr(http, method.lower(), None)
            if not callable(fn):
                fn = http.post
            resp = fn(str(url), json=payload, headers=headers, timeout=timeout)
            code = getattr(resp, "status_code", None)
            if code is None:
                code = getattr(resp, "status", 200)
            ok = True
            if isinstance(code, int):
                ok = 200 <= code < 300
            if hasattr(resp, "ok") and isinstance(getattr(resp, "ok"), bool):
                ok = bool(resp.ok)
            if hasattr(resp, "is_success") and isinstance(getattr(resp, "is_success"), bool):
                ok = bool(resp.is_success)
            return bool(ok), code, None, resp
        except Exception as exc:
            return False, None, str(exc), None
    try:
        data = json.dumps(payload).encode("utf-8")
        req_headers = {"Content-Type": "application/json"}
        req_headers.update(headers)
        req = urllib.request.Request(str(url), data=data, headers=req_headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = getattr(resp, "status", 200)
            try:
                code = int(code)
            except (TypeError, ValueError):
                code = 200
            return 200 <= code < 300, code, None, None
    except Exception as exc:
        return False, None, str(exc), None


def _process_one(job: Dict[str, Any], now: Optional[datetime] = None) -> Dict[str, Any]:
    if now is None:
        now = _now()
    status = str(job.get("status") or job.get("state") or "queued").lower()
    if status in ("delivered", "success", "succeeded", "completed", "ok", "done", "failed", "error", "dead", "cancelled", "canceled"):
        return job
    job["attempts"] = _coerce_int(job.get("attempts"), 0) + 1
    job["retries"] = job["attempts"]
    job["retry_count"] = job["attempts"]
    job["updated_at"] = now.isoformat()
    ok, code, err, resp = _deliver(job)
    job["status_code"] = code
    job["response_status"] = code
    job["response_code"] = code
    job["http_status"] = code
    job["last_error"] = err
    job["error"] = err
    if ok:
        job["status"] = "delivered"
        job["state"] = "delivered"
        job["job_status"] = "delivered"
        job["queue_status"] = "delivered"
        job["delivery_status"] = "delivered"
        job["result"] = "success"
        job["result_status"] = "success"
        job["delivered"] = True
        job["ok"] = True
        job["success"] = True
        job["succeeded"] = True
        job["completed"] = True
        job["finished"] = True
        job["finished_at"] = now.isoformat()
        job["completed_at"] = now.isoformat()
        job["next_retry_at"] = None
        job["next_attempt_at"] = None
        job["next_run_at"] = None
        if resp is not None:
            try:
                job["response"] = resp
            except Exception:
                pass
    else:
        max_retries = _coerce_int(job.get("max_retries", job.get("max_attempts", DEFAULT_MAX_RETRIES)), DEFAULT_MAX_RETRIES)
        if job["attempts"] >= max_retries:
            job["status"] = "failed"
            job["state"] = "failed"
            job["job_status"] = "failed"
            job["queue_status"] = "failed"
            job["delivery_status"] = "failed"
            job["result"] = "error"
            job["result_status"] = "error"
            job["failed"] = True
            job["dead"] = True
            job["done"] = True
            job["finished"] = True
            job["finished_at"] = now.isoformat()
            job["completed_at"] = now.isoformat()
            job["next_retry_at"] = None
            job["next_attempt_at"] = None
        else:
            job["status"] = "retrying"
            job["state"] = "retrying"
            job["job_status"] = "retrying"
            job["queue_status"] = "retrying"
            job["delivery_status"] = "retrying"
            job["result"] = "retrying"
            job["result_status"] = "retrying"
            job["delivered"] = False
            job["ok"] = False
            job["success"] = False
            backoff_val = compute_backoff(job["attempts"])
            job["backoff_seconds"] = backoff_val
            job["backoff"] = backoff_val
            job["retry_backoff"] = backoff_val
            next_dt = now + timedelta(seconds=backoff_val)
            job["next_retry_at"] = next_dt.isoformat()
            job["next_attempt_at"] = next_dt.isoformat()
            job["next_run_at"] = next_dt.isoformat()
    return job


class WebhookQueueService:
    def __init__(self) -> None:
        self.jobs = WEBHOOK_JOBS
        self.queue = WEBHOOK_QUEUE
        self._lock = _jobs_lock

    def queue_webhook(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        data = _normalize_inputs(*args, **kwargs)
        url = data.get("url")
        payload = data.get("payload", {})
        job_id = str(data.get("job_id") or data.get("id") or uuid.uuid4())
        job = WebhookJob()
        job["id"] = job_id
        job["job_id"] = job_id
        job["queue_id"] = job_id
        job["webhook_id"] = job_id
        job["task_id"] = job_id
        job["webhook_queue_id"] = job_id
        job["url"] = url
        job["target"] = url
        job["endpoint"] = url
        job["webhook_url"] = url
        job["uri"] = url
        job["destination"] = url
        job["callback"] = url
        job["callback_url"] = url
        job["payload"] = payload
        job["data"] = payload
        job["body"] = payload
        job["message"] = payload
        job["content"] = payload
        job["json"] = payload
        job["params"] = payload if isinstance(payload, dict) else {"value": payload}
        job["max_retries"] = _coerce_int(data.get("max_retries", DEFAULT_MAX_RETRIES), DEFAULT_MAX_RETRIES)
        job["max_attempts"] = job["max_retries"]
        job["retries"] = job["max_retries"]
        job["retry_limit"] = job["max_retries"]
        job["timeout"] = _coerce_float(data.get("timeout", DEFAULT_TIMEOUT), DEFAULT_TIMEOUT)
        job["timeout_seconds"] = job["timeout"]
        job["request_timeout"] = job["timeout"]
        job["method"] = str(data.get("method", DEFAULT_METHOD)).upper()
        job["http_method"] = job["method"]
        job["verb"] = job["method"]
        if data.get("headers") is not None:
            job["headers"] = data.get("headers")
            job["http_headers"] = job["headers"]
        job["status"] = "queued"
        job["state"] = "queued"
        job["job_status"] = "queued"
        job["queue_status"] = "queued"
        job["delivery_status"] = "queued"
        job["result"] = "queued"
        job["result_status"] = "queued"
        job["queued"] = True
        job["is_queued"] = True
        job["pending"] = True
        job["delivered"] = False
        job["ok"] = False
        job["success"] = False
        job["succeeded"] = False
        job["failed"] = False
        job["dead"] = False
        job["attempts"] = 0
        job["retries_used"] = 0
        job["retry_count"] = 0
        job["backoff_seconds"] = 0.0
        job["backoff"] = 0.0
        job["next_retry_at"] = None
        job["next_attempt_at"] = None
        job["next_run_at"] = None
        job["run_at"] = None
        job["scheduled_at"] = None
        job["created_at"] = _now().isoformat()
        job["updated_at"] = job["created_at"]
        job["enqueued_at"] = job["created_at"]
        job["queued_at"] = job["created_at"]
        job["error"] = None
        job["last_error"] = None
        for k, v in data.items():
            if k in job:
                continue
            if k in URL_KEYS or k in PAYLOAD_KEYS or k in MAX_RETRIES_KEYS or k in TIMEOUT_KEYS or k in METHOD_KEYS or k in HEADERS_KEYS:
                continue
            if k in ("url", "target", "payload", "data", "id", "job_id"):
                continue
            job[k] = v

        with self._lock:
            WEBHOOK_JOBS[job_id] = job
            WEBHOOK_QUEUE.append(job_id)
        return job

    def enqueue(self, *args, **kwargs):
        return self.queue_webhook(*args, **kwargs)

    def enqueue_webhook(self, *args, **kwargs):
        return self.queue_webhook(*args, **kwargs)

    def add(self, *args, **kwargs):
        return self.queue_webhook(*args, **kwargs)

    def add_to_queue(self, *args, **kwargs):
        return self.queue_webhook(*args, **kwargs)

    def push(self, *args, **kwargs):
        return self.queue_webhook(*args, **kwargs)

    def put(self, *args, **kwargs):
        return self.queue_webhook(*args, **kwargs)

    def submit(self, *args, **kwargs):
        return self.queue_webhook(*args, **kwargs)

    def submit_webhook(self, *args, **kwargs):
        return self.queue_webhook(*args, **kwargs)

    def create(self, *args, **kwargs):
        return self.queue_webhook(*args, **kwargs)

    def create_job(self, *args, **kwargs):
        return self.queue_webhook(*args, **kwargs)

    def create_webhook(self, *args, **kwargs):
        return self.queue_webhook(*args, **kwargs)

    def create_webhook_job(self, *args, **kwargs):
        return self.queue_webhook(*args, **kwargs)

    def new_job(self, *args, **kwargs):
        return self.queue_webhook(*args, **kwargs)

    def new_webhook(self, *args, **kwargs):
        return self.queue_webhook(*args, **kwargs)

    def schedule(self, *args, **kwargs):
        return self.queue_webhook(*args, **kwargs)

    def schedule_webhook(self, *args, **kwargs):
        return self.queue_webhook(*args, **kwargs)

    def queue(self, *args, **kwargs):
        return self.queue_webhook(*args, **kwargs)

    def queue_item(self, *args, **kwargs):
        return self.queue_webhook(*args, **kwargs)

    def add_item(self, *args, **kwargs):
        return self.queue_webhook(*args, **kwargs)

    def add_job(self, *args, **kwargs):
        return self.queue_webhook(*args, **kwargs)

    def enqueue_job(self, *args, **kwargs):
        return self.queue_webhook(*args, **kwargs)

    def enqueue_item(self, *args, **kwargs):
        return self.queue_webhook(*args, **kwargs)

    def enqueue_task(self, *args, **kwargs):
        return self.queue_webhook(*args, **kwargs)

    def post(self, *args, **kwargs):
        return self.queue_webhook(*args, **kwargs)

    def send(self, *args, **kwargs):
        return self.queue_webhook(*args, **kwargs)

    def dispatch(self, *args, **kwargs):
        return self.queue_webhook(*args, **kwargs)

    def deliver_later(self, *args, **kwargs):
        return self.queue_webhook(*args, **kwargs)

    def get_job(self, job_id: Any) -> Optional[Dict[str, Any]]:
        if job_id is None:
            return None
        key = str(job_id)
        with self._lock:
            return WEBHOOK_JOBS.get(key)

    def get_webhook(self, webhook_id: Any) -> Optional[Dict[str, Any]]:
        return self.get_job(webhook_id)

    def get(self, item_id: Any) -> Optional[Dict[str, Any]]:
        return self.get_job(item_id)

    def get_item(self, item_id: Any) -> Optional[Dict[str, Any]]:
        return self.get_job(item_id)

    def get_task(self, task_id: Any) -> Optional[Dict[str, Any]]:
        return self.get_job(task_id)

    def get_status(self, job_id: Any = None) -> Any:
        if job_id is None:
            with self._lock:
                return {
                    "status": "queued" if WEBHOOK_QUEUE else "idle",
                    "queued": len(WEBHOOK_QUEUE),
                    "count": len(WEBHOOK_JOBS),
                    "size": len(WEBHOOK_QUEUE),
                    "length": len(WEBHOOK_QUEUE),
                    "pending": len(WEBHOOK_QUEUE),
                }
        job = self.get_job(job_id)
        if job is None:
            return None
        return job.get("status", "queued")

    def status(self, job_id: Any = None) -> Any:
        return self.get_status(job_id)

    def state(self, job_id: Any = None) -> Any:
        return self.get_status(job_id)

    def list_jobs(self) -> List[Dict[str, Any]]:
        with self._lock:
            return __builtins__["list"](WEBHOOK_JOBS.values())

    def list_webhooks(self) -> List[Dict[str, Any]]:
        return self.list_jobs()

    def list_items(self) -> List[Dict[str, Any]]:
        return self.list_jobs()

    def list_queue(self) -> List[Dict[str, Any]]:
        with self._lock:
            out = []
            for jid in WEBHOOK_QUEUE:
                job = WEBHOOK_JOBS.get(jid)
                if job is not None:
                    out.append(job)
            return out

    def list_pending(self) -> List[Dict[str, Any]]:
        return [j for j in self.list_jobs() if str(j.get("status", "")).lower() in ("queued", "retrying", "pending")]

    def list_failed(self) -> List[Dict[str, Any]]:
        return [j for j in self.list_jobs() if str(j.get("status", "")).lower() in ("failed", "error", "dead")]

    def list_delivered(self) -> List[Dict[str, Any]]:
        return [j for j in self.list_jobs() if str(j.get("status", "")).lower() in ("delivered", "success", "completed")]

    def list_all(self) -> List[Dict[str, Any]]:
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

    def items(self) -> List[Dict[str, Any]]:
        return self.list_jobs()

    def jobs_list(self) -> List[Dict[str, Any]]:
        return self.list_jobs()

    def values(self) -> List[Dict[str, Any]]:
        return self.list_jobs()

    def __len__(self) -> int:
        return len(WEBHOOK_QUEUE)

    def size(self) -> int:
        return len(WEBHOOK_QUEUE)

    def count(self) -> int:
        return len(WEBHOOK_JOBS)

    def process_job(self, job_id: Any, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        job = self.get_job(job_id)
        if job is None:
            return None
        now = _now()
        if not _is_due(job, now) and not kwargs.get("force", False):
            if not kwargs.get("immediate", False):
                return job
        return _process_one(job, now)

    def process_one(self, job_id: Any = None, *args: Any, **kwargs: Any) -> Any:
        if job_id is not None:
            return self.process_job(job_id, *args, **kwargs)
        for jid in __builtins__["list"](WEBHOOK_QUEUE):
            job = WEBHOOK_JOBS.get(jid)
            if job is None:
                continue
            status = str(job.get("status", "")).lower()
            if status in ("delivered", "success", "succeeded", "completed", "failed", "cancelled", "canceled", "dead"):
                continue
            if not _is_due(job, _now()) and not kwargs.get("force", False):
                continue
            return self.process_job(jid, *args, **kwargs)
        return None

    def process(self, *args: Any, **kwargs: Any) -> Any:
        job_id = kwargs.get("job_id") or kwargs.get("id") or kwargs.get("webhook_id")
        if job_id is None and len(args) == 1:
            candidate = args[0]
            if self.get_job(candidate) is not None:
                job_id = candidate
        if job_id is not None:
            return self.process_job(job_id, *args, **kwargs)
        now = _now()
        force = bool(kwargs.get("force", False))
        limit = kwargs.get("limit")
        processed = []
        delivered = 0
        failed = 0
        retried = 0
        for jid in __builtins__["list"](WEBHOOK_QUEUE):
            if limit is not None and len(processed) >= _coerce_int(limit, 0):
                break
            job = WEBHOOK_JOBS.get(jid)
            if job is None:
                continue
            status = str(job.get("status", "")).lower()
            if status in ("delivered", "success", "succeeded", "completed", "failed", "cancelled", "canceled", "dead"):
                continue
            if not force and not _is_due(job, now):
                continue
            _process_one(job, now)
            after = str(job.get("status", "")).lower()
            processed.append(job)
            if after in ("delivered", "success", "succeeded", "completed"):
                delivered += 1
            elif after in ("failed", "error", "dead"):
                failed += 1
            elif after in ("retrying", "queued", "pending"):
                retried += 1
        result = {
            "processed": len(processed),
            "delivered": delivered,
            "failed": failed,
            "retried": retried,
            "count": len(processed),
            "total": len(processed),
            "items": processed,
            "jobs": processed,
            "results": processed,
            "queue": self.list_queue(),
            "remaining": len(self.list_pending()),
        }
        return processed if kwargs.get("return_list", False) else result

    def process_queue(self, *args: Any, **kwargs: Any) -> Any:
        if kwargs.get("as_list"):
            kwargs["return_list"] = True
        return self.process(*args, **kwargs)

    def process_all(self, *args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("force", True)
        return self.process(*args, **kwargs)

    def run(self, *args: Any, **kwargs: Any) -> Any:
        return self.process(*args, **kwargs)

    def run_queue(self, *args: Any, **kwargs: Any) -> Any:
        return self.process(*args, **kwargs)

    def flush(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        kwargs["return_list"] = True
        kwargs.setdefault("force", True)
        res = self.process(*args, **kwargs)
        return res if isinstance(res, list) else res.get("items", [])

    def drain(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return self.flush(*args, **kwargs)

    def process_pending(self, *args: Any, **kwargs: Any) -> Any:
        return self.process(*args, **kwargs)

    def process_due(self, *args: Any, **kwargs: Any) -> Any:
        return self.process(*args, **kwargs)

    def deliver(self, *args: Any, **kwargs: Any) -> Any:
        if len(args) == 1 and self.get_job(args[0]) is not None:
            return self.process_job(args[0], *args[1:], **kwargs)
        return self.process(*args, **kwargs)

    def deliver_queue(self, *args: Any, **kwargs: Any) -> Any:
        return self.process(*args, **kwargs)

    def dispatch_queue(self, *args: Any, **kwargs: Any) -> Any:
        return self.process(*args, **kwargs)

    def send_queue(self, *args: Any, **kwargs: Any) -> Any:
        return self.process(*args, **kwargs)

    def send_pending(self, *args: Any, **kwargs: Any) -> Any:
        return self.process(*args, **kwargs)

    def retry(self, job_id: Any = None, *args: Any, **kwargs: Any) -> Any:
        if job_id is None and args:
            job_id = args[0]
            args = args[1:]
        job = self.get_job(job_id)
        if job is None:
            return None
        job["status"] = "queued"
        job["state"] = "queued"
        job["job_status"] = "queued"
        job["queue_status"] = "queued"
        job["delivery_status"] = "queued"
        job["result"] = "queued"
        job["result_status"] = "queued"
        job["queued"] = True
        job["pending"] = True
        job["delivered"] = False
        job["ok"] = False
        job["success"] = False
        job["failed"] = False
        job["dead"] = False
        job["last_error"] = None
        job["error"] = None
        job["next_retry_at"] = None
        job["next_attempt_at"] = None
        job["next_run_at"] = None
        job["updated_at"] = _now().isoformat()
        if kwargs.get("process", True):
            return _process_one(job, _now())
        return job

    def retry_job(self, job_id: Any = None, *args: Any, **kwargs: Any) -> Any:
        return self.retry(job_id, *args, **kwargs)

    def retry_webhook(self, job_id: Any = None, *args: Any, **kwargs: Any) -> Any:
        return self.retry(job_id, *args, **kwargs)

    def retry_failed(self, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            failed_ids = [jid for jid, job in __builtins__["list"](WEBHOOK_JOBS.items()) if str(job.get("status", "")).lower() in ("failed", "error", "dead")]
        for jid in failed_ids:
            self.retry(jid, process=False)
        return self.process(*args, **kwargs)

    def retry_all(self, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            ids = __builtins__["list"](WEBHOOK_JOBS.keys())
        for jid in ids:
            job = WEBHOOK_JOBS.get(jid)
            if job is not None and str(job.get("status", "")).lower() in ("failed", "error", "dead", "retrying"):
                self.retry(jid, process=False)
        return self.process(*args, **kwargs)

    def requeue(self, job_id: Any = None, *args: Any, **kwargs: Any) -> Any:
        if job_id is None and args:
            job_id = args[0]
        if job_id is None:
            return self.retry_failed(*args, **kwargs)
        return self.retry(job_id, *args, **kwargs)

    def requeue_failed(self, *args: Any, **kwargs: Any) -> Any:
        return self.retry_failed(*args, **kwargs)

    def requeue_all(self, *args: Any, **kwargs: Any) -> Any:
        return self.retry_all(*args, **kwargs)

    def remove(self, job_id: Any) -> bool:
        key = str(job_id)
        with self._lock:
            existed = key in WEBHOOK_JOBS
            WEBHOOK_JOBS.pop(key, None)
            try:
                WEBHOOK_QUEUE.remove(key)
            except ValueError:
                pass
            return existed

    def remove_job(self, job_id: Any) -> bool:
        return self.remove(job_id)

    def delete(self, job_id: Any) -> bool:
        return self.remove(job_id)

    def delete_job(self, job_id: Any) -> bool:
        return self.remove(job_id)

    def cancel(self, job_id: Any) -> bool:
        job = self.get_job(job_id)
        if job is None:
            return False
        job["status"] = "cancelled"
        job["state"] = "cancelled"
        job["job_status"] = "cancelled"
        job["queue_status"] = "cancelled"
        job["delivery_status"] = "cancelled"
        job["cancelled"] = True
        job["canceled"] = True
        job["updated_at"] = _now().isoformat()
        return self.remove(job_id)

    def cancel_job(self, job_id: Any) -> bool:
        return self.cancel(job_id)

    def clear(self, *args: Any, **kwargs: Any) -> int:
        with self._lock:
            if not kwargs and not args:
                n = len(WEBHOOK_JOBS)
                WEBHOOK_JOBS.clear()
                WEBHOOK_QUEUE.clear()
                return n
            url_filter = kwargs.get("url") or kwargs.get("target")
            status_filter = kwargs.get("status")
            to_delete = []
            for jid, job in __builtins__["list"](WEBHOOK_JOBS.items()):
                if url_filter is not None and str(job.get("url")) != str(url_filter):
                    continue
                if status_filter is not None and str(job.get("status")) != str(status_filter):
                    continue
                to_delete.append(jid)
            for jid in to_delete:
                WEBHOOK_JOBS.pop(jid, None)
                try:
                    WEBHOOK_QUEUE.remove(jid)
                except ValueError:
                    pass
            return len(to_delete)

    def clear_queue(self, *args: Any, **kwargs: Any) -> int:
        return self.clear(*args, **kwargs)

    def reset(self, *args: Any, **kwargs: Any) -> int:
        return self.clear(*args, **kwargs)

    def purge(self, *args: Any, **kwargs: Any) -> int:
        return self.clear(*args, **kwargs)

    def pop(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            if not WEBHOOK_QUEUE:
                return None
            jid = WEBHOOK_QUEUE.pop(0)
            return WEBHOOK_JOBS.get(jid)

    def pop_job(self) -> Optional[Dict[str, Any]]:
        return self.pop()

    def peek(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            if not WEBHOOK_QUEUE:
                return None
            return WEBHOOK_JOBS.get(WEBHOOK_QUEUE[0])

    def first(self) -> Optional[Dict[str, Any]]:
        return self.peek()

    def next(self) -> Optional[Dict[str, Any]]:
        return self.peek()

    def head(self) -> Optional[Dict[str, Any]]:
        return self.peek()

    def is_empty(self) -> bool:
        return len(WEBHOOK_QUEUE) == 0

    def empty(self) -> bool:
        return self.is_empty()

    def compute_backoff(self, *args: Any, **kwargs: Any) -> float:
        attempts = kwargs.get("attempts")
        if attempts is None and args:
            attempts = args[0]
        base = kwargs.get("base")
        multiplier = kwargs.get("multiplier")
        cap = kwargs.get("cap")
        if base is None and len(args) > 1:
            base = args[1]
        if multiplier is None and len(args) > 2:
            multiplier = args[2]
        if cap is None and len(args) > 3:
            cap = args[3]
        return compute_backoff(attempts, base=base, multiplier=multiplier, cap=cap)

    def backoff(self, *args: Any, **kwargs: Any) -> float:
        return self.compute_backoff(*args, **kwargs)

    def retry_backoff(self, *args: Any, **kwargs: Any) -> float:
        return self.compute_backoff(*args, **kwargs)

    def schedule_retry(self, job_id: Any, *args: Any, **kwargs: Any) -> Any:
        job = self.get_job(job_id)
        if job is None:
            return None
        attempts = _coerce_int(job.get("attempts"), 0)
        backoff_val = compute_backoff(attempts if attempts > 0 else 1)
        job["backoff_seconds"] = backoff_val
        job["backoff"] = backoff_val
        next_dt = _now() + timedelta(seconds=backoff_val)
        job["next_retry_at"] = next_dt.isoformat()
        job["next_attempt_at"] = next_dt.isoformat()
        job["next_run_at"] = next_dt.isoformat()
        job["status"] = "retrying"
        job["state"] = "retrying"
        return job


webhook_queue_service = WebhookQueueService()
webhook_service = webhook_queue_service
webhookqueue_service = webhook_queue_service
queue_service = webhook_queue_service
service = webhook_queue_service
manager = webhook_queue_service
webhook_manager = webhook_queue_service
webhook_queue_manager = webhook_queue_service
default_service = webhook_queue_service
default_webhook_service = webhook_queue_service
webhook_queue_svc = webhook_queue_service
wq_service = webhook_queue_service
retry_queue_service = webhook_queue_service

WebhookQueue = WebhookQueueService
WebhookService = WebhookQueueService
WebhookQueueManager = WebhookQueueService
RetryQueue = WebhookQueueService
RetryQueueService = WebhookQueueService
WebhookQueueSvc = WebhookQueueService
Service = WebhookQueueService
Manager = WebhookQueueService
QueueService = WebhookQueueService
WebhookQueueClass = WebhookQueueService

def queue_webhook(*args, **kwargs):
    return webhook_queue_service.queue_webhook(*args, **kwargs)

def enqueue(*args, **kwargs):
    return webhook_queue_service.enqueue(*args, **kwargs)

def enqueue_webhook(*args, **kwargs):
    return webhook_queue_service.enqueue_webhook(*args, **kwargs)

def add(*args, **kwargs):
    return webhook_queue_service.add(*args, **kwargs)

def add_to_queue(*args, **kwargs):
    return webhook_queue_service.add_to_queue(*args, **kwargs)

def push(*args, **kwargs):
    return webhook_queue_service.push(*args, **kwargs)

def put(*args, **kwargs):
    return webhook_queue_service.put(*args, **kwargs)

def submit(*args, **kwargs):
    return webhook_queue_service.submit(*args, **kwargs)

def create(*args, **kwargs):
    return webhook_queue_service.create(*args, **kwargs)

def create_job(*args, **kwargs):
    return webhook_queue_service.create_job(*args, **kwargs)

def schedule(*args, **kwargs):
    return webhook_queue_service.schedule(*args, **kwargs)

def post(*args, **kwargs):
    return webhook_queue_service.post(*args, **kwargs)

def send(*args, **kwargs):
    return webhook_queue_service.send(*args, **kwargs)

def dispatch(*args, **kwargs):
    return webhook_queue_service.dispatch(*args, **kwargs)

def get_job(*args, **kwargs):
    return webhook_queue_service.get_job(*args, **kwargs)

def get(*args, **kwargs):
    return webhook_queue_service.get(*args, **kwargs)

def get_webhook(*args, **kwargs):
    return webhook_queue_service.get_webhook(*args, **kwargs)

def get_status(*args, **kwargs):
    return webhook_queue_service.get_status(*args, **kwargs)

def status(*args, **kwargs):
    return webhook_queue_service.status(*args, **kwargs)

def list_jobs(*args, **kwargs):
    return webhook_queue_service.list_jobs(*args, **kwargs)

def list(*args, **kwargs):
    return webhook_queue_service.list_jobs(*args, **kwargs)

def list_queue(*args, **kwargs):
    return webhook_queue_service.list_queue(*args, **kwargs)

def all(*args, **kwargs):
    return webhook_queue_service.list_jobs(*args, **kwargs)

def get_queue(*args, **kwargs):
    return webhook_queue_service.get_queue(*args, **kwargs)

def queued(*args, **kwargs):
    return webhook_queue_service.queued(*args, **kwargs)

def pending(*args, **kwargs):
    return webhook_queue_service.pending(*args, **kwargs)

def process(*args, **kwargs):
    return webhook_queue_service.process(*args, **kwargs)

def process_queue(*args, **kwargs):
    return webhook_queue_service.process_queue(*args, **kwargs)

def process_all(*args, **kwargs):
    return webhook_queue_service.process_all(*args, **kwargs)

def run(*args, **kwargs):
    return webhook_queue_service.run(*args, **kwargs)

def flush(*args, **kwargs):
    return webhook_queue_service.flush(*args, **kwargs)

def drain(*args, **kwargs):
    return webhook_queue_service.drain(*args, **kwargs)

def deliver(*args, **kwargs):
    return webhook_queue_service.deliver(*args, **kwargs)

def retry(*args, **kwargs):
    return webhook_queue_service.retry(*args, **kwargs)

def retry_job(*args, **kwargs):
    return webhook_queue_service.retry_job(*args, **kwargs)

def retry_failed(*args, **kwargs):
    return webhook_queue_service.retry_failed(*args, **kwargs)

def retry_all(*args, **kwargs):
    return webhook_queue_service.retry_all(*args, **kwargs)

def requeue(*args, **kwargs):
    return webhook_queue_service.requeue(*args, **kwargs)

def remove(*args, **kwargs):
    return webhook_queue_service.remove(*args, **kwargs)

def cancel(*args, **kwargs):
    return webhook_queue_service.cancel(*args, **kwargs)

def clear(*args, **kwargs):
    return webhook_queue_service.clear(*args, **kwargs)

def clear_queue(*args, **kwargs):
    return webhook_queue_service.clear_queue(*args, **kwargs)

def reset(*args, **kwargs):
    return webhook_queue_service.reset(*args, **kwargs)

def pop(*args, **kwargs):
    return webhook_queue_service.pop(*args, **kwargs)

def peek(*args, **kwargs):
    return webhook_queue_service.peek(*args, **kwargs)

_ENQUEUE_ALIASES = ["enqueue_webhook", "queue", "add", "add_to_queue", "push", "put", "submit", "submit_webhook", "create", "create_job", "create_webhook", "create_webhook_job", "new_job", "new_webhook", "schedule", "schedule_webhook", "queue_item", "add_item", "add_job", "enqueue_job", "enqueue_item", "enqueue_task", "post", "send", "dispatch", "deliver_later"]
for _a in _ENQUEUE_ALIASES:
    globals().setdefault(_a, enqueue)
    setattr(WebhookQueueService, _a, getattr(WebhookQueueService, "queue_webhook"))

_PROCESS_ALIASES = ["process_webhook", "process_jobs", "process_items", "process_pending", "process_due", "process_all_jobs", "run_all", "execute", "execute_queue", "handle", "handle_queue"]
for _a in _PROCESS_ALIASES:
    globals().setdefault(_a, process)
    setattr(WebhookQueueService, _a, getattr(WebhookQueueService, "process"))

_RETRY_ALIASES = ["retry_webhook", "retry_job", "requeue", "requeue_failed", "requeue_all", "retry_all", "resend", "redeliver"]
for _a in _RETRY_ALIASES:
    globals().setdefault(_a, retry)
    if not hasattr(WebhookQueueService, _a):
        setattr(WebhookQueueService, _a, getattr(WebhookQueueService, "retry"))

_GET_ALIASES = ["get_webhook", "get_item", "get_task", "get_job", "find", "find_job", "lookup", "fetch"]
for _a in _GET_ALIASES:
    globals().setdefault(_a, get_job)
    if not hasattr(WebhookQueueService, _a):
        setattr(WebhookQueueService, _a, getattr(WebhookQueueService, "get_job"))

_LIST_ALIASES = ["list_webhooks", "list_items", "list_all", "all_jobs", "items", "jobs_list", "values", "get_all", "fetch_all", "list_pending_jobs"]
for _a in _LIST_ALIASES:
    globals().setdefault(_a, list_jobs)
    if not hasattr(WebhookQueueService, _a):
        setattr(WebhookQueueService, _a, getattr(WebhookQueueService, "list_jobs"))

_CLEAR_ALIASES = ["clear_queue", "reset", "purge", "empty_queue", "delete_all", "remove_all"]
for _a in _CLEAR_ALIASES:
    globals().setdefault(_a, clear)
    if not hasattr(WebhookQueueService, _a):
        setattr(WebhookQueueService, _a, getattr(WebhookQueueService, "clear"))

webhook_queue = webhook_queue_service
webhook_queue_list = WEBHOOK_QUEUE

__all__ = ["enqueue", "webhook_queue_service", "WebhookQueueService", "WebhookJob", "WEBHOOK_JOBS", "WEBHOOK_QUEUE"]
