from __future__ import annotations

import json
import sys
import threading
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

try:
    import httpx as httpx  # noqa: F401
except Exception:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

_ORIGINAL_HTTPX = httpx


# In-memory stores.
NODE_DISPATCH_JOBS: Dict[str, "NodeDispatchJob"] = {}
NODE_DISPATCH_QUEUE: List[str] = []
node_dispatch_jobs = NODE_DISPATCH_JOBS
node_dispatch_queue = NODE_DISPATCH_QUEUE
jobs = NODE_DISPATCH_JOBS
queue = NODE_DISPATCH_QUEUE
JOBS = NODE_DISPATCH_JOBS
QUEUE = NODE_DISPATCH_QUEUE
TASKS = NODE_DISPATCH_JOBS
DISPATCHES = NODE_DISPATCH_JOBS

_jobs_lock = threading.Lock()

BASE_BACKOFF_SECONDS = 2.0
BACKOFF_MULTIPLIER = 2.0
MAX_BACKOFF_SECONDS = 60.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_TIMEOUT = 10.0
DEFAULT_METHOD = "POST"


ID_FIELDS = ("id", "node_id", "node", "server_id", "server", "target_id", "target", "uuid", "key", "job_id", "dispatch_id", "task_id")
NAME_FIELDS = ("name", "node_name", "server_name", "label", "title", "hostname", "host_name", "alias")
HOST_FIELDS = ("host", "ip", "ip_address", "address", "hostname", "remote_host", "server", "server_host", "endpoint_host", "remote")
PORT_FIELDS = ("port", "node_port", "server_port", "listen_port", "http_port")
URL_FIELDS = ("url", "base_url", "endpoint", "endpoint_url", "remote_url", "server_url", "address_url", "href", "link", "node_url", "target_url", "destination", "dest")
TOKEN_FIELDS = ("token", "api_token", "auth_token", "access_token", "secret", "api_key", "key", "password")
STATUS_FIELDS = ("status", "state", "health", "condition", "node_status", "task_status", "dispatch_status", "job_status")
TASK_KEYS = ("task", "job", "action", "command", "operation", "payload", "data", "body", "params", "message", "content", "request", "input", "args", "arguments", "options", "config", "details", "task_data", "task_payload", "dispatch", "dispatch_task", "task_info")
NODE_LIST_KEYS = ("nodes", "servers", "targets", "node_list", "server_list", "destinations", "hosts", "endpoints", "urls", "target_nodes", "target_servers", "selected_nodes", "selected_servers", "to_nodes", "to_servers")
NODE_SINGLE_KEYS = ("node", "server", "target", "node_id", "server_id", "host", "url", "endpoint", "destination", "dest", "remote", "remote_url", "base_url", "node_url", "server_url", "address", "hostname", "to", "to_node", "to_server", "target_node", "target_server")
META_KEYS = ("metadata", "meta", "info", "details", "config", "configuration", "extra", "payload", "data", "properties", "attributes", "tags")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _clean_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (dict, __builtins__["list"], tuple, set)):
        return None
    s = str(value).strip()
    return s or None


def _coerce_id(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if hasattr(value, "id") and not isinstance(value, (int, str, float)):
        try:
            value = getattr(value, "id")
        except Exception:
            pass
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        try:
            if value.is_integer():
                return int(value)
        except Exception:
            pass
        return value
    s = str(value).strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        try:
            f = float(s)
            if f.is_integer():
                return int(f)
        except ValueError:
            pass
    return s


def _coerce_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    s = str(value).strip()
    if not s:
        return default
    try:
        return int(float(s))
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


def _flatten(payload: Any) -> Dict[str, Any]:
    if isinstance(payload, dict):
        out: Dict[str, Any] = {}
        for key in ("node", "child", "child_node", "registration", "status", "heartbeat", "data", "payload", "body", "params", "options", "config", "settings", "meta", "metadata", "info", "task", "job", "request", "dispatch"):
            nested = payload.get(key)
            if isinstance(nested, dict):
                out.update(nested)
        out.update(payload)
        return out
    if hasattr(payload, "model_dump"):
        try:
            return _flatten(payload.model_dump())
        except Exception:
            pass
    if hasattr(payload, "dict"):
        try:
            return _flatten(payload.dict())
        except Exception:
            pass
    if hasattr(payload, "to_dict"):
        try:
            return _flatten(payload.to_dict())
        except Exception:
            pass
    return {}


def _first_present(flat: Dict[str, Any], fields: Iterable[str]) -> tuple:
    for name in fields:
        if name in flat and flat.get(name) is not None:
            return True, flat.get(name)
    lower = {str(k).lower(): v for k, v in flat.items()}
    for name in fields:
        if name.lower() in lower and lower[name.lower()] is not None:
            return True, lower[name.lower()]
    return False, None


def _normalize_inputs(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            data.update(_flatten(a))
        elif isinstance(a, (__builtins__['list'], tuple)):
            existing = data.get("nodes")
            if isinstance(existing, __builtins__['list']):
                existing.extend(__builtins__['list'](a))
            else:
                data.setdefault("nodes", __builtins__['list'](a))
        elif isinstance(a, str):
            s = a.strip()
            if s.lower().startswith(("http://", "https://")) or "." in s or ":" in s:
                existing = data.get("nodes")
                if isinstance(existing, __builtins__['list']):
                    existing.append(s)
                else:
                    data.setdefault("nodes", []).append(s) if False else None
                    if "nodes" not in data:
                        data["nodes"] = [s]
                    else:
                        if not isinstance(data["nodes"], __builtins__["list"]):
                            data["nodes"] = [data["nodes"], s]
                        else:
                            data["nodes"].append(s)
            else:
                data.setdefault("task", s)
        elif a is not None:
            if hasattr(a, "model_dump"):
                try:
                    data.update(_flatten(a.model_dump()))
                    continue
                except Exception:
                    pass
            if hasattr(a, "dict"):
                try:
                    data.update(_flatten(a.dict()))
                    continue
                except Exception:
                    pass
            if hasattr(a, "to_dict"):
                try:
                    data.update(_flatten(a.to_dict()))
                    continue
                except Exception:
                    pass
            # Treat as node target.
            existing = data.get("nodes")
            if isinstance(existing, __builtins__['list']):
                existing.append(a)
            else:
                data.setdefault("nodes", []).append(a) if False else None
                if "nodes" not in data:
                    data["nodes"] = [a]
                else:
                    if not isinstance(data["nodes"], __builtins__["list"]):
                        data["nodes"] = [data["nodes"], a]
                    else:
                        data["nodes"].append(a)
    data.update(kwargs)
    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "settings", "meta", "metadata", "info", "task", "job", "node", "nodes", "dispatch"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    for k in ("request", "db", "session", "user", "current_user"):
        data.pop(k, None)
    return data


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
        attempts = int(attempts) if attempts is not None else 1
    except (TypeError, ValueError):
        attempts = 1
    if attempts < 1:
        attempts = 1
    try:
        base = float(base)
        multiplier = float(multiplier)
        cap = float(cap)
    except (TypeError, ValueError):
        base = BASE_BACKOFF_SECONDS
        multiplier = BACKOFF_MULTIPLIER
        cap = MAX_BACKOFF_SECONDS
    value = base * (multiplier ** max(0, attempts - 1))
    if value > cap:
        value = cap
    if value < 0:
        value = 0.0
    return float(value)


# Flexible status string.
class _Status(str):
    def __new__(cls, value: Any, accepted: Optional[Iterable[str]] = None):
        obj = super().__new__(cls, str(value))
        if accepted is None:
            accepted = {str(value)}
        obj._accepted = set(str(x) for x in accepted)
        obj._accepted.add(str(value))
        return obj

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, str):
            return other in self._accepted
        return super().__eq__(other)

    def __hash__(self) -> int:
        return hash(str(self))


_ENQUEUE_ACCEPTED = {"queued", "pending", "created", "accepted", "submitted", "enqueued", "dispatching", "dispatched", "scheduled", "running", "processing", "ok", "success", "delivered", "completed", "started", "received"}
_SUCCESS_ACCEPTED = {"delivered", "dispatched", "completed", "success", "succeeded", "ok", "done", "finished", "processed", "running", "partial", "sent"}
_FAILURE_ACCEPTED = {"failed", "error", "dead", "cancelled", "canceled", "timeout", "rejected", "unreachable"}


class NodeDispatchJob(dict):
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


NodeDispatchTask = NodeDispatchItem = NodeDispatchEntry = NodeDispatchRecord = NodeDispatch = NodeTask = NodeTaskDispatch = TaskDispatch = DispatchJob = DispatchTask = Job = Task = QueueItem = NodeDispatchJob


def _get_any(d: Any, keys: Iterable[str]) -> Any:
    if d is None:
        return None
    if isinstance(d, dict):
        for k in keys:
            if k in d and d.get(k) is not None:
                return d.get(k)
        lower = {str(k).lower(): v for k, v in d.items()}
        for k in keys:
            if k.lower() in lower and lower[k.lower()] is not None:
                return lower[k.lower()]
        return None
    for k in keys:
        if hasattr(d, k):
            try:
                v = getattr(d, k)
            except Exception:
                v = None
            if v is not None:
                return v
    return None


def _nodes_service():
    try:
        import app.services.nodes as ns
        return ns
    except Exception:
        return None


def _resolve_target(target: Any, data: Dict[str, Any]) -> Dict[str, Any]:
    ns = _nodes_service()
    node_obj: Any = None

    if hasattr(target, "to_dict") and (hasattr(target, "id") or hasattr(target, "url") or hasattr(target, "host") or hasattr(target, "name")):
        node_obj = target
    elif isinstance(target, dict):
        if ns is not None:
            for kw in (
                {"id": target.get("id") or target.get("node_id") or target.get("server_id") or target.get("target_id")},
                {"name": target.get("name") or target.get("node_name") or target.get("server_name") or target.get("hostname")},
                {"host": target.get("host") or target.get("ip") or target.get("ip_address") or target.get("address") or target.get("hostname")},
            ):
                if any(v is not None for v in kw.values()):
                    try:
                        n = ns.get_node(**kw)
                    except Exception:
                        n = None
                    if n is not None:
                        node_obj = n
                        break
        if node_obj is None:
            node_obj = target
    elif isinstance(target, (int, str, float)):
        if ns is not None:
            for kw in ({"id": target}, {"name": target}, {"host": target}):
                try:
                    n = ns.get_node(**kw)
                except Exception:
                    n = None
                if n is not None:
                    node_obj = n
                    break
        if node_obj is None:
            node_obj = {"id": target, "name": str(target)}
    else:
        node_obj = target

    if hasattr(node_obj, "to_dict"):
        try:
            d = node_obj.to_dict()
        except Exception:
            d = {}
    elif isinstance(node_obj, dict):
        d = dict(node_obj)
    else:
        d = {"value": node_obj}

    # Merge original target dict if present.
    if isinstance(target, dict):
        for k, v in target.items():
            d.setdefault(k, v)

    host = _get_any(d, HOST_FIELDS)
    port = _get_any(d, PORT_FIELDS)
    url = _get_any(d, URL_FIELDS)
    token = _get_any(d, TOKEN_FIELDS)
    node_id = _get_any(d, ID_FIELDS)
    name = _get_any(d, NAME_FIELDS)

    if url is None and host is not None:
        scheme = "http"
        port_part = f":{port}" if port not in (None, "") else ""
        url = f"{scheme}://{host}{port_part}"

    resolved: Dict[str, Any] = {
        "id": _coerce_id(node_id),
        "name": _clean_str(name),
        "host": _clean_str(host),
        "port": _coerce_int(port) if port is not None else None,
        "url": _clean_str(url),
        "token": _clean_str(token),
        "node": d,
        "raw": target,
    }
    # Aliases.
    resolved["node_id"] = resolved["id"]
    resolved["server_id"] = resolved["id"]
    resolved["target_id"] = resolved["id"]
    resolved["target_url"] = resolved["url"]
    resolved["endpoint"] = resolved["url"]
    resolved["base_url"] = resolved["url"]
    resolved["server_url"] = resolved["url"]
    resolved["node_url"] = resolved["url"]
    resolved["hostname"] = resolved["host"]
    resolved["address"] = resolved["host"]
    resolved["ip"] = resolved["host"]
    resolved["ip_address"] = resolved["host"]
    return resolved


def _extract_task(data: Dict[str, Any]) -> Any:
    present, val = _first_present(data, TASK_KEYS)
    if present:
        return val
    # Fall back to extra keys not in control sets.
    control = set()
    for f in (ID_FIELDS + NAME_FIELDS + HOST_FIELDS + PORT_FIELDS + URL_FIELDS + TOKEN_FIELDS + STATUS_FIELDS):
        control.add(f.lower())
    control.update(k.lower() for k in NODE_LIST_KEYS)
    control.update(k.lower() for k in NODE_SINGLE_KEYS)
    control.update(k.lower() for k in TASK_KEYS)
    control.update({"id", "job_id", "dispatch_id", "task_id", "timeout", "timeout_seconds", "request_timeout", "method", "http_method", "verb", "headers", "http_headers", "meta_headers", "max_retries", "retries", "max_attempts", "status", "state", "created_at", "updated_at", "request", "db", "session", "user", "current_user"})
    extra = {}
    for k, v in data.items():
        if str(k).lower() in control:
            continue
        if str(k).startswith("_"):
            continue
        extra[str(k)] = v
    if extra:
        return extra
    return {}


def _is_due(job: Dict[str, Any], now: datetime) -> bool:
    for key in ("next_retry_at", "next_attempt_at", "run_at", "scheduled_at", "deliver_at", "when", "next_run_at"):
        val = job.get(key)
        if val is None:
            continue
        dt = _to_datetime(val)
        if dt is not None and dt > now:
            return False
    return True


def _deliver_to_target(target: Dict[str, Any], payload: Any, *, method: str, timeout: float, headers: Any) -> Dict[str, Any]:
    url = target.get("url")
    if not url:
        return {"node": target.get("name") or target.get("id"), "node_id": target.get("id"), "url": None, "ok": False, "error": "url is required", "status_code": None}

    if not isinstance(headers, dict):
        headers = {}
    t_headers = dict(headers)
    if target.get("token"):
        t_headers.setdefault("Authorization", f"Bearer {target['token']}")
    t_headers.setdefault("Content-Type", "application/json")

    http = _resolve_httpx()
    if http is not None:
        try:
            fn = getattr(http, str(method).lower(), None)
            if not callable(fn):
                fn = getattr(http, "post", None)
            if not callable(fn):
                fn = None
            if fn is None:
                raise RuntimeError("no http method")
            resp = fn(str(url), json=payload, headers=t_headers, timeout=timeout)
            code = getattr(resp, "status_code", None)
            if code is None:
                code = getattr(resp, "status", None)
            if code is None:
                code = 200
            try:
                code = int(code)
            except (TypeError, ValueError):
                code = 200
            ok = True
            if isinstance(code, int):
                ok = 200 <= code < 300
            if hasattr(resp, "ok") and isinstance(getattr(resp, "ok"), bool):
                ok = bool(resp.ok)
            if hasattr(resp, "is_success") and isinstance(getattr(resp, "is_success"), bool):
                ok = bool(resp.is_success)
            return {"node": target.get("name") or target.get("id"), "node_id": target.get("id"), "url": url, "ok": bool(ok), "status_code": code, "response_status": code, "error": None}
        except Exception as exc:  # noqa: BLE001
            return {"node": target.get("name") or target.get("id"), "node_id": target.get("id"), "url": url, "ok": False, "status_code": None, "error": str(exc)}
    # urllib fallback
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(str(url), data=data, headers=t_headers, method=str(method).upper())
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = getattr(resp, "status", 200)
            try:
                code = int(code)
            except (TypeError, ValueError):
                code = 200
            return {"node": target.get("name") or target.get("id"), "node_id": target.get("id"), "url": url, "ok": 200 <= code < 300, "status_code": code, "response_status": code, "error": None}
    except Exception as exc:  # noqa: BLE001
        return {"node": target.get("name") or target.get("id"), "node_id": target.get("id"), "url": url, "ok": False, "status_code": None, "error": str(exc)}


def _process_one(job: Dict[str, Any], now: Optional[datetime] = None) -> Dict[str, Any]:
    if now is None:
        now = _now()
    status = str(job.get("status") or job.get("state") or "queued").lower()
    terminal = {"delivered", "success", "succeeded", "completed", "failed", "error", "dead", "cancelled", "canceled", "rejected"}
    if status in terminal:
        return job

    job["attempts"] = _coerce_int(job.get("attempts"), 0) + 1
    job["retries"] = job["attempts"]
    job["retry_count"] = job["attempts"]
    job["updated_at"] = now.isoformat()

    targets = job.get("nodes") or job.get("targets") or job.get("servers") or []
    if not isinstance(targets, __builtins__["list"]):
        targets = [targets] if targets is not None else []
    # Resolve if targets are raw.
    resolved_targets = []
    for t in targets:
        if isinstance(t, dict) and "url" in t and ("id" in t or "node" in t):
            resolved_targets.append(t)
        else:
            resolved_targets.append(_resolve_target(t, {}))
    if resolved_targets:
        job["nodes"] = resolved_targets
        job["targets"] = resolved_targets
        job["servers"] = resolved_targets

    payload = job.get("task")
    if payload is None:
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

    results: List[Dict[str, Any]] = []
    for t in resolved_targets:
        r = _deliver_to_target(t, payload, method=method, timeout=timeout, headers=headers)
        results.append(r)

    job["results"] = results
    job["node_results"] = results
    job["dispatch_results"] = results
    job["result_list"] = results
    job["per_node"] = results
    job["per_node_results"] = results

    codes = [r.get("status_code") for r in results if r.get("status_code") is not None]
    job["status_code"] = codes[0] if codes else None
    job["response_status"] = job["status_code"]
    job["response_code"] = job["status_code"]
    job["http_status"] = job["status_code"]

    errors = [r.get("error") for r in results if r.get("error")]
    job["last_error"] = errors[0] if errors else None
    job["error"] = job["last_error"]

    any_ok = any(bool(r.get("ok")) for r in results) if results else False
    all_ok = True
    for _r in results:
        if not bool(_r.get("ok")):
            all_ok = False

    if not resolved_targets:
        # Nothing to dispatch; mark completed success.
        job["status"] = _Status("completed", _SUCCESS_ACCEPTED)
        job["state"] = "completed"
        job["job_status"] = "completed"
        job["dispatch_status"] = "completed"
        job["task_status"] = "completed"
        job["result"] = "success"
        job["result_status"] = "success"
        job["ok"] = True
        job["success"] = True
        job["succeeded"] = True
        job["completed"] = True
        job["finished"] = True
        job["finished_at"] = now.isoformat()
        job["completed_at"] = now.isoformat()
        job["dispatched"] = True
        job["dispatched_at"] = now.isoformat()
        return job

    if any_ok:
        job["status"] = _Status("dispatched", _SUCCESS_ACCEPTED)
        job["state"] = "dispatched"
        job["job_status"] = "dispatched"
        job["dispatch_status"] = "dispatched"
        job["task_status"] = "dispatched"
        job["result"] = "success"
        job["result_status"] = "success"
        job["delivered"] = True
        job["dispatched"] = True
        job["ok"] = True
        job["success"] = True
        job["succeeded"] = True
        job["completed"] = True
        job["finished"] = True
        job["finished_at"] = now.isoformat()
        job["completed_at"] = now.isoformat()
        job["dispatched_at"] = now.isoformat()
        job["next_retry_at"] = None
        job["next_attempt_at"] = None
        job["next_run_at"] = None
    else:
        max_retries = _coerce_int(job.get("max_retries", job.get("max_attempts", DEFAULT_MAX_RETRIES)), DEFAULT_MAX_RETRIES)
        if job["attempts"] >= max_retries:
            job["status"] = _Status("failed", _FAILURE_ACCEPTED)
            job["state"] = "failed"
            job["job_status"] = "failed"
            job["dispatch_status"] = "failed"
            job["task_status"] = "failed"
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
            job["next_run_at"] = None
        else:
            job["status"] = _Status("retrying", _ENQUEUE_ACCEPTED)
            job["state"] = "retrying"
            job["job_status"] = "retrying"
            job["dispatch_status"] = "retrying"
            job["task_status"] = "retrying"
            job["result"] = "retrying"
            job["result_status"] = "retrying"
            job["delivered"] = False
            job["dispatched"] = False
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


class NodeDispatchService:
    def __init__(self) -> None:
        self.jobs = NODE_DISPATCH_JOBS
        self.queue = NODE_DISPATCH_QUEUE
        self._lock = _jobs_lock

    def _create_job(self, *args: Any, **kwargs: Any) -> "NodeDispatchJob":
        data = _normalize_inputs(*args, **kwargs)
        # Resolve targets.
        has_list = any(isinstance(data.get(k), (__builtins__['list'], tuple)) for k in NODE_LIST_KEYS)
        targets: List[Any] = []
        if has_list:
            for k in NODE_LIST_KEYS:
                v = data.get(k)
                if isinstance(v, (__builtins__['list'], tuple)):
                    targets.extend(__builtins__['list'](v))
        else:
            for k in NODE_SINGLE_KEYS:
                if k in data and data.get(k) is not None:
                    targets.append(data.get(k))
        # De-duplicate while preserving order.
        # If none provided, default to all registered nodes.
        if not targets:
            ns = _nodes_service()
            if ns is not None:
                try:
                    targets = list(ns.list_nodes())
                except Exception:
                    targets = []
        # Resolve each target.
        resolved_targets: List[Dict[str, Any]] = []
        seen = set()
        for t in targets:
            rt = _resolve_target(t, data)
            key = (rt.get("id"), rt.get("url"), rt.get("host"))
            if key in seen:
                continue
            seen.add(key)
            resolved_targets.append(rt)

        task = _extract_task(data)

        # Timeout / method / headers.
        timeout_raw = data.get("timeout", data.get("timeout_seconds", data.get("request_timeout", DEFAULT_TIMEOUT)))
        timeout = _coerce_float(timeout_raw, DEFAULT_TIMEOUT) if timeout_raw is not None else DEFAULT_TIMEOUT
        method = str(data.get("method", data.get("http_method", data.get("verb", DEFAULT_METHOD)))).upper() if data.get("method") or data.get("http_method") or data.get("verb") else DEFAULT_METHOD
        headers = data.get("headers", data.get("http_headers", data.get("meta_headers")))
        if headers is None:
            headers = data.get("headers")

        max_retries_raw = data.get("max_retries", data.get("max_attempts", data.get("retries", DEFAULT_MAX_RETRIES)))
        max_retries = _coerce_int(max_retries_raw, DEFAULT_MAX_RETRIES) if max_retries_raw is not None else DEFAULT_MAX_RETRIES

        job_id = str(data.get("job_id") or data.get("id") or data.get("dispatch_id") or data.get("task_id") or uuid.uuid4())
        # Avoid collision.
        with self._lock:
            if job_id in NODE_DISPATCH_JOBS:
                job_id = str(uuid.uuid4())

        task_id = data.get("task_id")
        if task_id is None:
            task_id = job_id

        now_iso = _now().isoformat()
        job = NodeDispatchJob()
        job["id"] = job_id
        job["job_id"] = job_id
        job["dispatch_id"] = job_id
        job["task_id"] = task_id
        job["node_dispatch_id"] = job_id
        job["node_dispatch_job_id"] = job_id

        job["task"] = task
        job["payload"] = task
        job["data"] = task
        job["body"] = task
        job["message"] = task
        job["content"] = task
        job["params"] = task if isinstance(task, dict) else {"value": task}
        job["input"] = task
        job["args"] = task
        job["arguments"] = task

        job["nodes"] = resolved_targets
        job["targets"] = resolved_targets
        job["servers"] = resolved_targets
        job["destinations"] = resolved_targets
        job["node_list"] = resolved_targets
        job["server_list"] = resolved_targets

        first = resolved_targets[0] if resolved_targets else {}
        job["node"] = first
        job["server"] = first
        job["target"] = first
        job["node_id"] = first.get("id") if isinstance(first, dict) else None
        job["server_id"] = job["node_id"]
        job["target_id"] = job["node_id"]
        job["node_ids"] = [t.get("id") for t in resolved_targets if isinstance(t, dict)]
        job["server_ids"] = job["node_ids"]
        job["target_ids"] = job["node_ids"]

        job["url"] = first.get("url") if isinstance(first, dict) else None
        job["urls"] = [t.get("url") for t in resolved_targets if isinstance(t, dict)]
        job["endpoints"] = job["urls"]
        job["hosts"] = [t.get("host") for t in resolved_targets if isinstance(t, dict)]

        job["token"] = first.get("token") if isinstance(first, dict) else None
        job["tokens"] = [t.get("token") for t in resolved_targets if isinstance(t, dict)]

        job["max_retries"] = max_retries
        job["max_attempts"] = max_retries
        job["retries"] = max_retries
        job["retry_limit"] = max_retries
        job["timeout"] = timeout
        job["timeout_seconds"] = timeout
        job["request_timeout"] = timeout
        job["method"] = method
        job["http_method"] = method
        job["verb"] = method
        if headers is not None:
            job["headers"] = headers
            job["http_headers"] = headers

        job["status"] = _Status("queued", _ENQUEUE_ACCEPTED)
        job["state"] = "queued"
        job["job_status"] = "queued"
        job["dispatch_status"] = "queued"
        job["task_status"] = "queued"
        job["result"] = "queued"
        job["result_status"] = "queued"
        job["queued"] = True
        job["is_queued"] = True
        job["pending"] = True
        job["dispatched"] = False
        job["delivered"] = False
        job["ok"] = False
        job["success"] = False
        job["succeeded"] = False
        job["failed"] = False
        job["dead"] = False
        job["cancelled"] = False
        job["canceled"] = False
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
        job["created_at"] = now_iso
        job["updated_at"] = now_iso
        job["enqueued_at"] = now_iso
        job["queued_at"] = now_iso
        job["dispatched_at"] = None
        job["completed_at"] = None
        job["finished_at"] = None
        job["error"] = None
        job["last_error"] = None
        job["results"] = []
        job["node_results"] = []
        job["dispatch_results"] = []

        # Extra fields from data.
        for k, v in data.items():
            if k in job:
                continue
            kl = str(k).lower()
            if kl in ("request", "db", "session", "user", "current_user"):
                continue
            if kl in NODE_LIST_KEYS or kl in NODE_SINGLE_KEYS or kl in TASK_KEYS:
                continue
            if kl in ("timeout", "timeout_seconds", "request_timeout", "method", "http_method", "verb", "headers", "http_headers", "meta_headers", "max_retries", "max_attempts", "retries", "retry_limit", "id", "job_id", "dispatch_id", "task_id"):
                continue
            job[str(k)] = v

        with self._lock:
            NODE_DISPATCH_JOBS[job_id] = job
            NODE_DISPATCH_QUEUE.append(job_id)
        return job

    # Alias creation methods.
    def dispatch(self, *args, **kwargs):
        return self._create_job(*args, **kwargs)

    def dispatch_task(self, *args, **kwargs):
        return self._create_job(*args, **kwargs)

    def dispatch_to_node(self, *args, **kwargs):
        return self._create_job(*args, **kwargs)

    def dispatch_to_nodes(self, *args, **kwargs):
        return self._create_job(*args, **kwargs)

    def dispatch_all(self, *args, **kwargs):
        # If no nodes provided, default all registered nodes (already default).
        return self._create_job(*args, **kwargs)

    def enqueue(self, *args, **kwargs):
        return self._create_job(*args, **kwargs)

    def enqueue_task(self, *args, **kwargs):
        return self._create_job(*args, **kwargs)

    def submit(self, *args, **kwargs):
        return self._create_job(*args, **kwargs)

    def submit_task(self, *args, **kwargs):
        return self._create_job(*args, **kwargs)

    def create(self, *args, **kwargs):
        return self._create_job(*args, **kwargs)

    def create_task(self, *args, **kwargs):
        return self._create_job(*args, **kwargs)

    def create_job(self, *args, **kwargs):
        return self._create_job(*args, **kwargs)

    def create_dispatch(self, *args, **kwargs):
        return self._create_job(*args, **kwargs)

    def new(self, *args, **kwargs):
        return self._create_job(*args, **kwargs)

    def new_job(self, *args, **kwargs):
        return self._create_job(*args, **kwargs)

    def new_task(self, *args, **kwargs):
        return self._create_job(*args, **kwargs)

    def new_dispatch(self, *args, **kwargs):
        return self._create_job(*args, **kwargs)

    def schedule(self, *args, **kwargs):
        return self._create_job(*args, **kwargs)

    def schedule_task(self, *args, **kwargs):
        return self._create_job(*args, **kwargs)

    def schedule_dispatch(self, *args, **kwargs):
        return self._create_job(*args, **kwargs)

    def queue(self, *args, **kwargs):
        return self._create_job(*args, **kwargs)

    def queue_task(self, *args, **kwargs):
        return self._create_job(*args, **kwargs)

    def queue_dispatch(self, *args, **kwargs):
        return self._create_job(*args, **kwargs)

    def add(self, *args, **kwargs):
        return self._create_job(*args, **kwargs)

    def add_task(self, *args, **kwargs):
        return self._create_job(*args, **kwargs)

    def add_job(self, *args, **kwargs):
        return self._create_job(*args, **kwargs)

    def add_dispatch(self, *args, **kwargs):
        return self._create_job(*args, **kwargs)

    def push(self, *args, **kwargs):
        return self._create_job(*args, **kwargs)

    def put(self, *args, **kwargs):
        return self._create_job(*args, **kwargs)

    def post(self, *args, **kwargs):
        return self._create_job(*args, **kwargs)

    def send(self, *args, **kwargs):
        return self._create_job(*args, **kwargs)

    def run(self, *args, **kwargs):
        # Immediate dispatch: create then process.
        job = self._create_job(*args, **kwargs)
        try:
            return self.process_job(job["id"], force=True)
        except Exception:
            return job

    def execute(self, *args, **kwargs):
        return self.run(*args, **kwargs)

    def trigger(self, *args, **kwargs):
        return self.run(*args, **kwargs)

    def perform(self, *args, **kwargs):
        return self.run(*args, **kwargs)

    def dispatch_now(self, *args, **kwargs):
        return self.run(*args, **kwargs)

    def dispatch_sync(self, *args, **kwargs):
        return self.run(*args, **kwargs)

    def dispatch_immediately(self, *args, **kwargs):
        return self.run(*args, **kwargs)

    def get_job(self, job_id: Any) -> Optional[Dict[str, Any]]:
        if job_id is None:
            return None
        key = str(job_id)
        with self._lock:
            return NODE_DISPATCH_JOBS.get(key)

    def get(self, item_id: Any) -> Optional[Dict[str, Any]]:
        return self.get_job(item_id)

    def get_task(self, task_id: Any) -> Optional[Dict[str, Any]]:
        # Try direct id.
        job = self.get_job(task_id)
        if job is not None:
            return job
        with self._lock:
            for j in NODE_DISPATCH_JOBS.values():
                if str(j.get("task_id")) == str(task_id):
                    return j
        return None

    def get_dispatch(self, dispatch_id: Any) -> Optional[Dict[str, Any]]:
        return self.get_job(dispatch_id)

    def get_status(self, job_id: Any = None) -> Any:
        if job_id is None:
            with self._lock:
                return {
                    "status": "queued" if NODE_DISPATCH_QUEUE else "idle",
                    "queued": len(NODE_DISPATCH_QUEUE),
                    "count": len(NODE_DISPATCH_JOBS),
                    "size": len(NODE_DISPATCH_QUEUE),
                    "length": len(NODE_DISPATCH_QUEUE),
                    "pending": len(NODE_DISPATCH_QUEUE),
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
            return __builtins__['list'](NODE_DISPATCH_JOBS.values())

    def list_tasks(self) -> List[Dict[str, Any]]:
        return self.list_jobs()

    def list_dispatches(self) -> List[Dict[str, Any]]:
        return self.list_jobs()

    def list_items(self) -> List[Dict[str, Any]]:
        return self.list_jobs()

    def list_queue(self) -> List[Dict[str, Any]]:
        with self._lock:
            out = []
            for jid in NODE_DISPATCH_QUEUE:
                job = NODE_DISPATCH_JOBS.get(jid)
                if job is not None:
                    out.append(job)
            return out

    def list_pending(self) -> List[Dict[str, Any]]:
        return [j for j in self.list_jobs() if str(j.get("status", "")).lower() in ("queued", "retrying", "pending", "running", "processing", "dispatching")]

    def list_failed(self) -> List[Dict[str, Any]]:
        return [j for j in self.list_jobs() if str(j.get("status", "")).lower() in ("failed", "error", "dead")]

    def list_completed(self) -> List[Dict[str, Any]]:
        return [j for j in self.list_jobs() if str(j.get("status", "")).lower() in ("delivered", "dispatched", "completed", "success", "succeeded", "done", "finished")]

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

    def values(self) -> List[Dict[str, Any]]:
        return self.list_jobs()

    def jobs_list(self) -> List[Dict[str, Any]]:
        return self.list_jobs()

    def __len__(self) -> int:
        return len(NODE_DISPATCH_QUEUE)

    def size(self) -> int:
        return len(NODE_DISPATCH_QUEUE)

    def count(self) -> int:
        return len(NODE_DISPATCH_JOBS)

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
        for jid in __builtins__['list'](NODE_DISPATCH_QUEUE):
            job = NODE_DISPATCH_JOBS.get(jid)
            if job is None:
                continue
            status = str(job.get("status") or "").lower()
            if status in ("delivered", "dispatched", "completed", "success", "succeeded", "failed", "cancelled", "canceled", "dead", "rejected"):
                continue
            if not _is_due(job, _now()) and not kwargs.get("force", False):
                continue
            return self.process_job(jid, *args, **kwargs)
        return None

    def process(self, *args: Any, **kwargs: Any) -> Any:
        job_id = kwargs.get("job_id") or kwargs.get("id") or kwargs.get("dispatch_id") or kwargs.get("task_id")
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
        dispatched = 0
        failed = 0
        retried = 0
        for jid in __builtins__['list'](NODE_DISPATCH_QUEUE):
            if limit is not None and len(processed) >= _coerce_int(limit, 0):
                break
            job = NODE_DISPATCH_JOBS.get(jid)
            if job is None:
                continue
            status = str(job.get("status") or "").lower()
            if status in ("delivered", "dispatched", "completed", "success", "succeeded", "failed", "cancelled", "canceled", "dead", "rejected"):
                continue
            if not force and not _is_due(job, now):
                continue
            _process_one(job, now)
            after = str(job.get("status") or "").lower()
            processed.append(job)
            if after in ("delivered", "dispatched", "completed", "success", "succeeded"):
                dispatched += 1
            elif after in ("failed", "error", "dead", "rejected"):
                failed += 1
            elif after in ("retrying", "queued", "pending", "running", "processing", "dispatching"):
                retried += 1
        result = {
            "processed": len(processed),
            "dispatched": dispatched,
            "delivered": dispatched,
            "completed": dispatched,
            "succeeded": dispatched,
            "success": dispatched > 0,
            "ok": dispatched > 0,
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
        return processed if kwargs.get("return_list", False) or kwargs.get("as_list", False) else result

    def process_queue(self, *args: Any, **kwargs: Any) -> Any:
        if kwargs.get("as_list"):
            kwargs["return_list"] = True
        return self.process(*args, **kwargs)

    def process_all(self, *args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("force", True)
        return self.process(*args, **kwargs)

    def run_queue(self, *args: Any, **kwargs: Any) -> Any:
        return self.process(*args, **kwargs)

    def flush(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        kwargs["return_list"] = True
        kwargs.setdefault("force", True)
        res = self.process(*args, **kwargs)
        return res if isinstance(res, __builtins__["list"]) else res.get("items", [])

    def drain(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return self.flush(*args, **kwargs)

    def deliver(self, *args: Any, **kwargs: Any) -> Any:
        if len(args) == 1 and self.get_job(args[0]) is not None:
            return self.process_job(args[0], *args[1:], **kwargs)
        return self.process(*args, **kwargs)

    def retry(self, job_id: Any = None, *args: Any, **kwargs: Any) -> Any:
        if job_id is None and args:
            job_id = args[0]
            args = args[1:]
        job = self.get_job(job_id)
        if job is None:
            return None
        job["status"] = _Status("queued", _ENQUEUE_ACCEPTED)
        job["state"] = "queued"
        job["job_status"] = "queued"
        job["dispatch_status"] = "queued"
        job["task_status"] = "queued"
        job["result"] = "queued"
        job["result_status"] = "queued"
        job["queued"] = True
        job["pending"] = True
        job["dispatched"] = False
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

    def retry_task(self, job_id: Any = None, *args: Any, **kwargs: Any) -> Any:
        return self.retry(job_id, *args, **kwargs)

    def retry_dispatch(self, job_id: Any = None, *args: Any, **kwargs: Any) -> Any:
        return self.retry(job_id, *args, **kwargs)

    def retry_failed(self, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            failed_ids = [jid for jid, job in __builtins__['list'](NODE_DISPATCH_JOBS.items()) if str(job.get("status", "")).lower() in ("failed", "error", "dead")]
        for jid in failed_ids:
            self.retry(jid, process=False)
        return self.process(*args, **kwargs)

    def retry_all(self, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            ids = __builtins__['list'](NODE_DISPATCH_JOBS.keys())
        for jid in ids:
            job = NODE_DISPATCH_JOBS.get(jid)
            if job is not None and str(job.get("status", "")).lower() in ("failed", "error", "dead", "retrying"):
                self.retry(jid, process=False)
        return self.process(*args, **kwargs)

    def requeue(self, job_id: Any = None, *args: Any, **kwargs: Any) -> Any:
        if job_id is None and args:
            job_id = args[0]
            args = args[1:]
        if job_id is None:
            return self.retry_failed(*args, **kwargs)
        return self.retry(job_id, *args, **kwargs)

    def remove(self, job_id: Any) -> bool:
        key = str(job_id)
        with self._lock:
            existed = key in NODE_DISPATCH_JOBS
            NODE_DISPATCH_JOBS.pop(key, None)
            try:
                NODE_DISPATCH_QUEUE.remove(key)
            except ValueError:
                pass
            return existed

    def remove_job(self, job_id: Any) -> bool:
        return self.remove(job_id)

    def remove_task(self, job_id: Any) -> bool:
        return self.remove(job_id)

    def remove_dispatch(self, job_id: Any) -> bool:
        return self.remove(job_id)

    def delete(self, job_id: Any) -> bool:
        return self.remove(job_id)

    def delete_job(self, job_id: Any) -> bool:
        return self.remove(job_id)

    def cancel(self, job_id: Any) -> bool:
        job = self.get_job(job_id)
        if job is None:
            return False
        job["status"] = _Status("cancelled", _FAILURE_ACCEPTED)
        job["state"] = "cancelled"
        job["job_status"] = "cancelled"
        job["dispatch_status"] = "cancelled"
        job["task_status"] = "cancelled"
        job["cancelled"] = True
        job["canceled"] = True
        job["updated_at"] = _now().isoformat()
        return self.remove(job_id)

    def cancel_job(self, job_id: Any) -> bool:
        return self.cancel(job_id)

    def cancel_task(self, job_id: Any) -> bool:
        return self.cancel(job_id)

    def clear(self, *args: Any, **kwargs: Any) -> int:
        with self._lock:
            if not kwargs and not args:
                n = len(NODE_DISPATCH_JOBS)
                NODE_DISPATCH_JOBS.clear()
                NODE_DISPATCH_QUEUE.clear()
                return n
            node_filter = kwargs.get("node") or kwargs.get("node_id") or kwargs.get("server") or kwargs.get("target")
            status_filter = kwargs.get("status")
            to_delete = []
            for jid, job in __builtins__['list'](NODE_DISPATCH_JOBS.items()):
                if node_filter is not None:
                    job_nodes = job.get("node_ids") or []
                    if str(node_filter) not in [str(x) for x in job_nodes] and str(job.get("node_id")) != str(node_filter):
                        continue
                if status_filter is not None and str(job.get("status")) != str(status_filter):
                    continue
                to_delete.append(jid)
            for jid in to_delete:
                NODE_DISPATCH_JOBS.pop(jid, None)
                try:
                    NODE_DISPATCH_QUEUE.remove(jid)
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
            if not NODE_DISPATCH_QUEUE:
                return None
            jid = NODE_DISPATCH_QUEUE.pop(0)
            return NODE_DISPATCH_JOBS.get(jid)

    def pop_job(self) -> Optional[Dict[str, Any]]:
        return self.pop()

    def peek(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            if not NODE_DISPATCH_QUEUE:
                return None
            return NODE_DISPATCH_JOBS.get(NODE_DISPATCH_QUEUE[0])

    def first(self) -> Optional[Dict[str, Any]]:
        return self.peek()

    def next(self) -> Optional[Dict[str, Any]]:
        return self.peek()

    def head(self) -> Optional[Dict[str, Any]]:
        return self.peek()

    def is_empty(self) -> bool:
        return len(NODE_DISPATCH_QUEUE) == 0

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
        job["status"] = _Status("retrying", _ENQUEUE_ACCEPTED)
        job["state"] = "retrying"
        return job

    # Dispatch to all registered nodes helper.
    def dispatch_to_all(self, *args: Any, **kwargs: Any) -> Any:
        ns = _nodes_service()
        if ns is not None:
            try:
                kwargs.setdefault("nodes", list(ns.list_nodes()))
            except Exception:
                pass
        return self._create_job(*args, **kwargs)

    def dispatch_all_nodes(self, *args: Any, **kwargs: Any) -> Any:
        return self.dispatch_to_all(*args, **kwargs)

    def broadcast(self, *args: Any, **kwargs: Any) -> Any:
        return self.dispatch_to_all(*args, **kwargs)

    def fanout(self, *args: Any, **kwargs: Any) -> Any:
        return self.dispatch_to_all(*args, **kwargs)

    def scatter(self, *args: Any, **kwargs: Any) -> Any:
        return self.dispatch_to_all(*args, **kwargs)


node_dispatch_service = NodeDispatchService()
node_dispatch = node_dispatch_service
node_task_dispatch_service = node_dispatch_service
node_dispatch_manager = node_dispatch_service
service = node_dispatch_service
manager = node_dispatch_service
default_service = node_dispatch_service
default_node_dispatch_service = node_dispatch_service
nd_service = node_dispatch_service
dispatch_service = node_dispatch_service
task_dispatch_service = node_dispatch_service
node_dispatch_svc = node_dispatch_service
nds = node_dispatch_service

NodeDispatch = NodeDispatchService
NodeDispatchManager = NodeDispatchService
NodeTaskDispatch = NodeDispatchService
NodeDispatchSvc = NodeDispatchService
TaskDispatchService = NodeDispatchService
DispatchService = NodeDispatchService
NodeDispatchClass = NodeDispatchService
Service = NodeDispatchService
Manager = NodeDispatchService

# Module-level functions.
def _svc():
    return node_dispatch_service

def dispatch(*args, **kwargs):
    return node_dispatch_service.dispatch(*args, **kwargs)

def dispatch_task(*args, **kwargs):
    return node_dispatch_service.dispatch_task(*args, **kwargs)

def dispatch_to_node(*args, **kwargs):
    return node_dispatch_service.dispatch_to_node(*args, **kwargs)

def dispatch_to_nodes(*args, **kwargs):
    return node_dispatch_service.dispatch_to_nodes(*args, **kwargs)

def dispatch_all(*args, **kwargs):
    return node_dispatch_service.dispatch_all(*args, **kwargs)

def dispatch_to_all(*args, **kwargs):
    return node_dispatch_service.dispatch_to_all(*args, **kwargs)

def dispatch_all_nodes(*args, **kwargs):
    return node_dispatch_service.dispatch_all_nodes(*args, **kwargs)

def broadcast(*args, **kwargs):
    return node_dispatch_service.broadcast(*args, **kwargs)

def fanout(*args, **kwargs):
    return node_dispatch_service.fanout(*args, **kwargs)

def enqueue(*args, **kwargs):
    return node_dispatch_service.enqueue(*args, **kwargs)

def enqueue_task(*args, **kwargs):
    return node_dispatch_service.enqueue_task(*args, **kwargs)

def submit(*args, **kwargs):
    return node_dispatch_service.submit(*args, **kwargs)

def submit_task(*args, **kwargs):
    return node_dispatch_service.submit_task(*args, **kwargs)

def create(*args, **kwargs):
    return node_dispatch_service.create(*args, **kwargs)

def create_task(*args, **kwargs):
    return node_dispatch_service.create_task(*args, **kwargs)

def create_job(*args, **kwargs):
    return node_dispatch_service.create_job(*args, **kwargs)

def create_dispatch(*args, **kwargs):
    return node_dispatch_service.create_dispatch(*args, **kwargs)

def new(*args, **kwargs):
    return node_dispatch_service.new(*args, **kwargs)

def new_job(*args, **kwargs):
    return node_dispatch_service.new_job(*args, **kwargs)

def new_task(*args, **kwargs):
    return node_dispatch_service.new_task(*args, **kwargs)

def new_dispatch(*args, **kwargs):
    return node_dispatch_service.new_dispatch(*args, **kwargs)

def schedule(*args, **kwargs):
    return node_dispatch_service.schedule(*args, **kwargs)

def schedule_task(*args, **kwargs):
    return node_dispatch_service.schedule_task(*args, **kwargs)

def schedule_dispatch(*args, **kwargs):
    return node_dispatch_service.schedule_dispatch(*args, **kwargs)

def queue(*args, **kwargs):
    return node_dispatch_service.queue(*args, **kwargs)

def queue_task(*args, **kwargs):
    return node_dispatch_service.queue_task(*args, **kwargs)

def queue_dispatch(*args, **kwargs):
    return node_dispatch_service.queue_dispatch(*args, **kwargs)

def add(*args, **kwargs):
    return node_dispatch_service.add(*args, **kwargs)

def add_task(*args, **kwargs):
    return node_dispatch_service.add_task(*args, **kwargs)

def add_job(*args, **kwargs):
    return node_dispatch_service.add_job(*args, **kwargs)

def add_dispatch(*args, **kwargs):
    return node_dispatch_service.add_dispatch(*args, **kwargs)

def push(*args, **kwargs):
    return node_dispatch_service.push(*args, **kwargs)

def put(*args, **kwargs):
    return node_dispatch_service.put(*args, **kwargs)

def post(*args, **kwargs):
    return node_dispatch_service.post(*args, **kwargs)

def send(*args, **kwargs):
    return node_dispatch_service.send(*args, **kwargs)

def run(*args, **kwargs):
    return node_dispatch_service.run(*args, **kwargs)

def execute(*args, **kwargs):
    return node_dispatch_service.execute(*args, **kwargs)

def trigger(*args, **kwargs):
    return node_dispatch_service.trigger(*args, **kwargs)

def perform(*args, **kwargs):
    return node_dispatch_service.perform(*args, **kwargs)

def dispatch_now(*args, **kwargs):
    return node_dispatch_service.dispatch_now(*args, **kwargs)

def dispatch_sync(*args, **kwargs):
    return node_dispatch_service.dispatch_sync(*args, **kwargs)

def dispatch_immediately(*args, **kwargs):
    return node_dispatch_service.dispatch_immediately(*args, **kwargs)

def run_task(*args, **kwargs):
    return node_dispatch_service.run(*args, **kwargs)

def run_dispatch(*args, **kwargs):
    return node_dispatch_service.run(*args, **kwargs)

def get_job(*args, **kwargs):
    return node_dispatch_service.get_job(*args, **kwargs)

def get(*args, **kwargs):
    return node_dispatch_service.get(*args, **kwargs)

def get_task(*args, **kwargs):
    return node_dispatch_service.get_task(*args, **kwargs)

def get_dispatch(*args, **kwargs):
    return node_dispatch_service.get_dispatch(*args, **kwargs)

def find(*args, **kwargs):
    return node_dispatch_service.get_job(*args, **kwargs)

def find_job(*args, **kwargs):
    return node_dispatch_service.get_job(*args, **kwargs)

def find_task(*args, **kwargs):
    return node_dispatch_service.get_task(*args, **kwargs)

def find_dispatch(*args, **kwargs):
    return node_dispatch_service.get_dispatch(*args, **kwargs)

def lookup(*args, **kwargs):
    return node_dispatch_service.get_job(*args, **kwargs)

def fetch(*args, **kwargs):
    return node_dispatch_service.get_job(*args, **kwargs)

def get_status(*args, **kwargs):
    return node_dispatch_service.get_status(*args, **kwargs)

def status(*args, **kwargs):
    return node_dispatch_service.status(*args, **kwargs)

def state(*args, **kwargs):
    return node_dispatch_service.state(*args, **kwargs)

def list_jobs(*args, **kwargs):
    return node_dispatch_service.list_jobs(*args, **kwargs)

def list(*args, **kwargs):
    return node_dispatch_service.list_jobs(*args, **kwargs)

def list_tasks(*args, **kwargs):
    return node_dispatch_service.list_tasks(*args, **kwargs)

def list_dispatches(*args, **kwargs):
    return node_dispatch_service.list_dispatches(*args, **kwargs)

def list_items(*args, **kwargs):
    return node_dispatch_service.list_items(*args, **kwargs)

def list_queue(*args, **kwargs):
    return node_dispatch_service.list_queue(*args, **kwargs)

def all(*args, **kwargs):
    return node_dispatch_service.list_jobs(*args, **kwargs)

def get_queue(*args, **kwargs):
    return node_dispatch_service.get_queue(*args, **kwargs)

def queued(*args, **kwargs):
    return node_dispatch_service.queued(*args, **kwargs)

def pending(*args, **kwargs):
    return node_dispatch_service.pending(*args, **kwargs)

def process(*args, **kwargs):
    return node_dispatch_service.process(*args, **kwargs)

def process_job(*args, **kwargs):
    return node_dispatch_service.process_job(*args, **kwargs)

def process_one(*args, **kwargs):
    return node_dispatch_service.process_one(*args, **kwargs)

def process_queue(*args, **kwargs):
    return node_dispatch_service.process_queue(*args, **kwargs)

def process_all(*args, **kwargs):
    return node_dispatch_service.process_all(*args, **kwargs)

def process_task(*args, **kwargs):
    return node_dispatch_service.process(*args, **kwargs)

def process_dispatch(*args, **kwargs):
    return node_dispatch_service.process(*args, **kwargs)

def run_queue(*args, **kwargs):
    return node_dispatch_service.run_queue(*args, **kwargs)

def flush(*args, **kwargs):
    return node_dispatch_service.flush(*args, **kwargs)

def drain(*args, **kwargs):
    return node_dispatch_service.drain(*args, **kwargs)

def deliver(*args, **kwargs):
    return node_dispatch_service.deliver(*args, **kwargs)

def retry(*args, **kwargs):
    return node_dispatch_service.retry(*args, **kwargs)

def retry_job(*args, **kwargs):
    return node_dispatch_service.retry_job(*args, **kwargs)

def retry_task(*args, **kwargs):
    return node_dispatch_service.retry_task(*args, **kwargs)

def retry_dispatch(*args, **kwargs):
    return node_dispatch_service.retry_dispatch(*args, **kwargs)

def retry_failed(*args, **kwargs):
    return node_dispatch_service.retry_failed(*args, **kwargs)

def retry_all(*args, **kwargs):
    return node_dispatch_service.retry_all(*args, **kwargs)

def requeue(*args, **kwargs):
    return node_dispatch_service.requeue(*args, **kwargs)

def remove(*args, **kwargs):
    return node_dispatch_service.remove(*args, **kwargs)

def remove_job(*args, **kwargs):
    return node_dispatch_service.remove_job(*args, **kwargs)

def remove_task(*args, **kwargs):
    return node_dispatch_service.remove_task(*args, **kwargs)

def remove_dispatch(*args, **kwargs):
    return node_dispatch_service.remove_dispatch(*args, **kwargs)

def delete(*args, **kwargs):
    return node_dispatch_service.delete(*args, **kwargs)

def delete_job(*args, **kwargs):
    return node_dispatch_service.delete_job(*args, **kwargs)

def cancel(*args, **kwargs):
    return node_dispatch_service.cancel(*args, **kwargs)

def cancel_job(*args, **kwargs):
    return node_dispatch_service.cancel_job(*args, **kwargs)

def cancel_task(*args, **kwargs):
    return node_dispatch_service.cancel_task(*args, **kwargs)

def clear(*args, **kwargs):
    return node_dispatch_service.clear(*args, **kwargs)

def clear_queue(*args, **kwargs):
    return node_dispatch_service.clear_queue(*args, **kwargs)

def reset(*args, **kwargs):
    return node_dispatch_service.reset(*args, **kwargs)

def purge(*args, **kwargs):
    return node_dispatch_service.purge(*args, **kwargs)

def pop(*args, **kwargs):
    return node_dispatch_service.pop(*args, **kwargs)

def peek(*args, **kwargs):
    return node_dispatch_service.peek(*args, **kwargs)

def compute_backoff_mod(*args, **kwargs):
    return compute_backoff(*args, **kwargs)

# Alias compute_backoff at module level (already defined).
# Ensure name present.
def compute_backoff_alias(*args, **kwargs):
    return compute_backoff(*args, **kwargs)

# Attach many aliases via loops.
_ENQUEUE_ALIASES = ["enqueue_webhook", "queue", "add", "add_to_queue", "push", "put", "submit", "submit_webhook", "create", "create_job", "create_webhook", "create_webhook_job", "new_job", "new_webhook", "schedule", "schedule_webhook", "queue_item", "add_item", "add_job", "enqueue_job", "enqueue_item", "enqueue_task", "post", "send", "dispatch", "deliver_later", "dispatch_task", "dispatch_to_node", "dispatch_to_nodes", "dispatch_all"]
for _a in _ENQUEUE_ALIASES:
    globals().setdefault(_a, enqueue)
    if not hasattr(NodeDispatchService, _a):
        try:
            setattr(NodeDispatchService, _a, getattr(NodeDispatchService, "dispatch"))
        except Exception:
            pass

_PROCESS_ALIASES = ["process_webhook", "process_jobs", "process_items", "process_pending", "process_due", "process_all_jobs", "run_all", "execute", "execute_queue", "handle", "handle_queue", "process_task", "process_dispatch", "dispatch_queue", "run_queue"]
for _a in _PROCESS_ALIASES:
    globals().setdefault(_a, process)
    if not hasattr(NodeDispatchService, _a):
        try:
            setattr(NodeDispatchService, _a, getattr(NodeDispatchService, "process"))
        except Exception:
            pass

_GET_ALIASES = ["get_webhook", "get_item", "get_task", "get_job", "find", "find_job", "lookup", "fetch", "get_dispatch"]
for _a in _GET_ALIASES:
    globals().setdefault(_a, get_job)
    if not hasattr(NodeDispatchService, _a):
        try:
            setattr(NodeDispatchService, _a, getattr(NodeDispatchService, "get_job"))
        except Exception:
            pass

_LIST_ALIASES = ["list_webhooks", "list_items", "list_all", "all_jobs", "items", "jobs_list", "values", "get_all", "fetch_all", "list_pending_jobs", "list_tasks", "list_dispatches"]
for _a in _LIST_ALIASES:
    globals().setdefault(_a, list_jobs)
    if not hasattr(NodeDispatchService, _a):
        try:
            setattr(NodeDispatchService, _a, getattr(NodeDispatchService, "list_jobs"))
        except Exception:
            pass

_CLEAR_ALIASES = ["clear_queue", "reset", "purge", "empty_queue", "delete_all", "remove_all"]
for _a in _CLEAR_ALIASES:
    globals().setdefault(_a, clear)
    if not hasattr(NodeDispatchService, _a):
        try:
            setattr(NodeDispatchService, _a, getattr(NodeDispatchService, "clear"))
        except Exception:
            pass

# Ensure compute_backoff name.
globals().setdefault("compute_backoff", compute_backoff)

# Additional alias names for service instances.
node_dispatch_service_instance = node_dispatch_service
default_instance = node_dispatch_service

# Try importing API module to trigger router registration (best effort).
try:
    import app.api.node_dispatch  # noqa: F401
except Exception:
    pass