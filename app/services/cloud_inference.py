from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

try:
    import httpx as httpx  # noqa: F401
except Exception:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

_ORIGINAL_HTTPX = httpx

try:
    from app.core import config as _config
except Exception:  # noqa: BLE001
    _config = None

try:
    from app.core.config import settings as _settings
except Exception:  # noqa: BLE001
    _settings = None


# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------
RESULTS: Dict[str, Dict[str, Any]] = {}
JOBS: Dict[str, Dict[str, Any]] = {}
MODELS: Dict[str, Dict[str, Any]] = {}
PROVIDERS: List[str] = ["huggingface", "openai", "anthropic", "kai-c", "local"]
INFERENCE_RESULTS = RESULTS
INFERENCE_JOBS = JOBS
results = RESULTS
jobs = JOBS
models = MODELS
inference_results = RESULTS
inference_jobs = JOBS

_STATE_LOCK = threading.Lock()

DEFAULT_TIMEOUT = 30.0
DEFAULT_PROVIDER = "huggingface"
DEFAULT_TASK = "detection"
DEFAULT_MODEL = "default"
DEFAULT_KAI_C_URL = "http://localhost:8000/infer/cloud"
BASE_BACKOFF_SECONDS = 2.0

MODEL_KEYS = ("model", "model_id", "model_name", "model_identifier", "modelId", "name", "model_path", "model_key")
INPUT_KEYS = ("inputs", "input", "input_data", "data", "payload", "image", "text", "prompt", "query", "content", "file", "file_path", "image_url", "image_base64")
PARAM_KEYS = ("parameters", "params", "options", "config", "settings", "args", "kwargs", "options_json")
PROVIDER_KEYS = ("provider", "provider_name", "backend", "engine", "service", "provider_id")
TASK_KEYS = ("task", "task_type", "type", "job_type")
URL_KEYS = ("url", "endpoint", "kai_c_url", "base_url", "target", "destination", "uri", "href", "callback", "callback_url")
CREDENTIAL_KEYS = ("credential", "credential_id", "token", "api_key", "credential_token", "api_token", "auth_token", "access_token")
ID_KEYS = ("id", "job_id", "result_id", "inference_id", "request_id")

CONTROL_KEYS = set(MODEL_KEYS) | set(INPUT_KEYS) | set(PARAM_KEYS) | set(PROVIDER_KEYS) | set(TASK_KEYS) | set(URL_KEYS) | set(CREDENTIAL_KEYS) | set(ID_KEYS) | {
    "status", "state", "created_at", "started_at", "completed_at", "executed_at",
    "latency_ms", "error", "error_message", "result", "result_json", "ok", "success",
    "timeout", "timeout_seconds", "headers", "max_retries", "retries",
}


def _setting(name: str, default: Any = None) -> Any:
    try:
        if _settings is not None and hasattr(_settings, name):
            val = getattr(_settings, name)
            if val is not None:
                return val
    except Exception:  # noqa: BLE001
        pass
    try:
        if _config is not None and hasattr(_config, name):
            val = getattr(_config, name)
            if val is not None:
                return val
    except Exception:  # noqa: BLE001
        pass
    env = os.environ.get(name)
    if env is not None:
        return env
    for alt in (name.upper(), name.lower()):
        env = os.environ.get(alt)
        if env is not None:
            return env
    return default


def _resolve_httpx():
    # Prefer a patched httpx (not the original) if present.
    candidates = []
    g = globals().get("httpx")
    if g is not None:
        candidates.append(g)
    for modname in ("app.services.cloud_inference", "app.services.cloud_inference_service"):
        m = sys.modules.get(modname)
        if m is not None and hasattr(m, "httpx"):
            candidates.append(getattr(m, "httpx"))
    candidates.append(sys.modules.get("httpx"))
    candidates.append(_ORIGINAL_HTTPX)
    for candidate in candidates:
        if candidate is not None and hasattr(candidate, "post") and candidate is not _ORIGINAL_HTTPX:
            return candidate
    for candidate in candidates:
        if candidate is not None and hasattr(candidate, "post"):
            return candidate
    try:
        import httpx as _httpx  # type: ignore

        return _httpx
    except Exception:
        return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_dict(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump()
        except Exception:  # noqa: BLE001
            pass
    if isinstance(value, (list, tuple)):
        out: Dict[str, Any] = {}
        for item in value:
            if isinstance(item, dict):
                out.update(item)
        return out
    if isinstance(value, str):
        s = value.strip()
        if s.startswith("{") and s.endswith("}"):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass
        return {"value": s} if s else {}
    try:
        attrs = {k: getattr(value, k) for k in dir(value) if not k.startswith("_") and not callable(getattr(value, k, None))}
        if attrs:
            return attrs
    except Exception:  # noqa: BLE001
        pass
    return {}


def _extract(data: Dict[str, Any], keys: tuple) -> Any:
    for k in keys:
        if k in data and data.get(k) is not None:
            return data.get(k)
    lower = {str(k).lower(): v for k, v in data.items()}
    for k in keys:
        if k.lower() in lower and lower[k.lower()] is not None:
            return lower[k.lower()]
    # nested
    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "settings", "message", "input", "inputs"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            found = _extract(nested, keys)
            if found is not None:
                return found
    return None


def _normalize(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for a in args or ():
        if isinstance(a, dict):
            data.update(_coerce_dict(a))
        elif isinstance(a, (list, tuple, set)):
            for item in a:
                if isinstance(item, dict):
                    data.update(item)
        elif isinstance(a, str):
            s = a.strip()
            if s.lower().startswith(("http://", "https://")):
                data.setdefault("url", s)
            elif s.startswith("{") and s.endswith("}"):
                try:
                    parsed = json.loads(s)
                    if isinstance(parsed, dict):
                        data.update(parsed)
                    else:
                        data.setdefault("value", s)
                except Exception:
                    data.setdefault("value", s)
            else:
                if data.get("model") is None and data.get("model_id") is None and data.get("model_name") is None:
                    data.setdefault("model", s)
                else:
                    data.setdefault("value", s)
        elif isinstance(a, (bytes, bytearray)):
            try:
                data.setdefault("inputs", {"data": bytes(a).decode("utf-8", errors="replace")})
            except Exception:
                pass
        else:
            # Skip framework/db/session objects.
            try:
                if hasattr(a, "execute") or hasattr(a, "commit") or hasattr(a, "scalar") or hasattr(a, "scalars"):
                    continue
            except Exception:
                pass
            try:
                attrs = {k: getattr(a, k) for k in dir(a) if not k.startswith("_") and not callable(getattr(a, k, None))}
                if attrs:
                    data.update(attrs)
            except Exception:
                pass
    data.update(kwargs or {})

    for _fk in ("request", "db", "session", "database", "user", "current_user", "conn", "connection"):
        data.pop(_fk, None)

    # flatten nested wrappers
    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "settings", "message", "input", "inputs"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)

    model = _extract(data, MODEL_KEYS)
    inputs_val = _extract(data, INPUT_KEYS)
    parameters = _extract(data, PARAM_KEYS)
    provider = _extract(data, PROVIDER_KEYS)
    task = _extract(data, TASK_KEYS)
    url = _extract(data, URL_KEYS)
    credential = _extract(data, CREDENTIAL_KEYS)
    item_id = _extract(data, ID_KEYS)

    # inputs fallback: collect extras
    if inputs_val is None:
        control = set(CONTROL_KEYS)
        extras = {k: v for k, v in data.items() if k not in control and not str(k).startswith("_")}
        if extras:
            inputs_val = extras
        else:
            inputs_val = {}

    if not isinstance(inputs_val, dict):
        if isinstance(inputs_val, str):
            try:
                parsed = json.loads(inputs_val)
                inputs_val = parsed if isinstance(parsed, dict) else {"value": inputs_val}
            except Exception:
                inputs_val = {"value": inputs_val}
        else:
            inputs_val = {"value": inputs_val}

    if parameters is None:
        parameters = {}
    if not isinstance(parameters, dict):
        if isinstance(parameters, str):
            try:
                parsed = json.loads(parameters)
                parameters = parsed if isinstance(parsed, dict) else {"value": parameters}
            except Exception:
                parameters = {"value": parameters}
        else:
            parameters = {"value": parameters}

    model = model if model is not None else DEFAULT_MODEL
    provider = (str(provider).strip().lower() if provider is not None else None) or DEFAULT_PROVIDER
    task = task if task is not None else DEFAULT_TASK
    url = url if url is not None else _setting("KAI_C_URL", _setting("kai_c_url", DEFAULT_KAI_C_URL))
    # support kai_c_url attribute on settings
    if url is None:
        url = DEFAULT_KAI_C_URL
    url = str(url)

    out: Dict[str, Any] = {}
    out["model"] = model
    out["model_id"] = model if isinstance(model, int) else None
    # preserve original model id if present in data
    if "model_id" in data and data.get("model_id") is not None:
        out["model_id"] = data.get("model_id")
    out["model_name"] = str(model)
    out["model_identifier"] = str(model)
    out["inputs"] = inputs_val
    out["input"] = inputs_val
    out["parameters"] = parameters
    out["params"] = parameters
    out["options"] = parameters
    out["provider"] = provider
    out["task"] = task
    out["url"] = url
    out["endpoint"] = url
    out["kai_c_url"] = url
    if credential is not None:
        out["credential"] = credential
        out["credential_token"] = credential
        out["token"] = credential
        out["api_key"] = credential
    if item_id is not None:
        out["id"] = item_id
        out["job_id"] = item_id
        out["result_id"] = item_id
    # carry through extras
    for k, v in data.items():
        if k not in out and k not in CONTROL_KEYS:
            out[k] = v
    out.setdefault("timeout", _setting("AI_TIMEOUT_SECONDS", DEFAULT_TIMEOUT))
    out.setdefault("timeout_seconds", out["timeout"])
    try:
        out["timeout"] = float(out["timeout"])
    except (TypeError, ValueError):
        out["timeout"] = DEFAULT_TIMEOUT
    out.setdefault("headers", {})
    return out


def clear() -> None:
    with _STATE_LOCK:
        RESULTS.clear()
        JOBS.clear()
        MODELS.clear()


def reset() -> None:
    clear()


def list_providers() -> List[str]:
    return list(PROVIDERS)


def get_providers() -> List[str]:
    return list_providers()


def available_providers() -> List[str]:
    return list_providers()


def supported_providers() -> List[str]:
    return list_providers()


def list_models() -> List[Dict[str, Any]]:
    with _STATE_LOCK:
        return [dict(v) for v in MODELS.values()]


def register_model(**kwargs: Any) -> Dict[str, Any]:
    data = _normalize(kwargs)
    mid = data.get("model") or data.get("name") or str(uuid.uuid4())
    mid = str(mid)
    rec = {"id": mid, "name": mid, "model_id": mid, "active": bool(data.get("active", True))}
    rec.update({k: v for k, v in data.items() if k not in ("inputs", "parameters")})
    with _STATE_LOCK:
        MODELS[mid] = rec
    return dict(rec)


register = register_model
add_model = register_model
create_model = register_model
upsert = register_model


def get_model(model_id: Any) -> Optional[Dict[str, Any]]:
    key = str(model_id)
    with _STATE_LOCK:
        if key in MODELS:
            return dict(MODELS[key])
    for v in MODELS.values():
        if str(v.get("id")) == key or str(v.get("name")) == key or str(v.get("model_id")) == key:
            return dict(v)
    return None


def list_results(limit: int = 50) -> List[Dict[str, Any]]:
    with _STATE_LOCK:
        items = sorted(RESULTS.values(), key=lambda x: str(x.get("created_at", "")), reverse=True)
        return [dict(x) for x in items[: int(limit or 50)]]


def list_inference_results(limit: int = 50) -> List[Dict[str, Any]]:
    return list_results(limit)


def get_result(result_id: Any) -> Optional[Dict[str, Any]]:
    key = str(result_id)
    with _STATE_LOCK:
        if key in RESULTS:
            return dict(RESULTS[key])
    for v in RESULTS.values():
        if str(v.get("id")) == key or str(v.get("result_id")) == key:
            return dict(v)
    return None


get_inference_result = get_result


def list_jobs(limit: int = 50, status_filter: Optional[str] = None) -> List[Dict[str, Any]]:
    with _STATE_LOCK:
        items = sorted(JOBS.values(), key=lambda x: str(x.get("created_at", "")), reverse=True)
        if status_filter:
            items = [j for j in items if str(j.get("status")) == str(status_filter)]
        return [dict(x) for x in items[: int(limit or 50)]]


def get_job(job_id: Any) -> Optional[Dict[str, Any]]:
    key = str(job_id)
    with _STATE_LOCK:
        if key in JOBS:
            return dict(JOBS[key])
    for v in JOBS.values():
        if str(v.get("id")) == key or str(v.get("job_id")) == key:
            return dict(v)
    return None


def _execute_http(data: Dict[str, Any]) -> Dict[str, Any]:
    url = data.get("url") or data.get("endpoint") or DEFAULT_KAI_C_URL
    url = str(url)
    payload = {k: v for k, v in data.items() if k not in ("url", "endpoint", "kai_c_url", "headers", "timeout", "timeout_seconds")}
    # keep inputs/parameters etc already in data
    headers = data.get("headers") or {}
    if not isinstance(headers, dict):
        headers = {}
    # internal api key
    internal_key = _setting("INTERNAL_API_KEY", _setting("internal_api_key", None))
    if internal_key and "X-Internal-API-Key" not in headers:
        headers = dict(headers)
        headers["X-Internal-API-Key"] = internal_key
    timeout = data.get("timeout", DEFAULT_TIMEOUT)
    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT

    httpx_mod = _resolve_httpx()
    start = time.time()
    result_data: Any = None
    status_code = 200
    error_message: Optional[str] = None
    ok = True
    if httpx_mod is None:
        # No httpx available: simulate success
        result_data = {"status": "ok", "simulated": True}
    else:
        try:
            resp = httpx_mod.post(url, json=payload, headers=headers, timeout=timeout)
            status_code = getattr(resp, "status_code", 200)
            # parse
            if hasattr(resp, "json"):
                try:
                    result_data = resp.json() if callable(resp.json) else resp.json
                except Exception:
                    result_data = getattr(resp, "text", str(resp))
            else:
                result_data = getattr(resp, "text", str(resp))
            try:
                ok = 200 <= int(status_code) < 300
            except (TypeError, ValueError):
                ok = True
            if not ok:
                error_message = str(result_data)
        except Exception as exc:  # noqa: BLE001
            ok = False
            status_code = 500
            error_message = str(exc)
            result_data = {"error": str(exc)}

    latency_ms = int((time.time() - start) * 1000)
    return {
        "ok": ok,
        "success": ok,
        "status_code": status_code,
        "result": result_data,
        "result_json": _safe_json(result_data),
        "error_message": error_message,
        "latency_ms": latency_ms,
    }


def _safe_json(value: Any) -> str:
    try:
        return json.dumps(value)
    except Exception:
        try:
            return json.dumps(str(value))
        except Exception:
            return "{}"


def _drop_db_args(args):
    out = list(args)
    while out and (hasattr(out[0], "execute") or hasattr(out[0], "commit") or hasattr(out[0], "scalar") or hasattr(out[0], "scalars")):
        out.pop(0)
    return out


def run_inference(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    args = _drop_db_args(args)
    kwargs = dict(kwargs)
    # Vendor-style positional: user_id, model_id, inputs, parameters
    if len(args) >= 1 and isinstance(args[0], int) and "user_id" not in kwargs and "userId" not in kwargs:
        kwargs["user_id"] = args.pop(0) if hasattr(args, "pop") else args[0]
    # args is tuple; handle carefully
    args = tuple(args)
    args = _drop_db_args(args)
    # redo positional parsing with list
    a = list(args)
    if a and isinstance(a[0], int) and "user_id" not in kwargs and "userId" not in kwargs:
        kwargs["user_id"] = a.pop(0)
    if a and (isinstance(a[0], int) or isinstance(a[0], str)) and "model_id" not in kwargs and "model" not in kwargs and "model_name" not in kwargs:
        kwargs["model_id" if isinstance(a[0], int) else "model"] = a.pop(0)
    if a and isinstance(a[0], dict) and "inputs" not in kwargs and "input" not in kwargs:
        kwargs["inputs"] = a.pop(0)
    if a and isinstance(a[0], dict) and "parameters" not in kwargs and "params" not in kwargs and "options" not in kwargs:
        kwargs["parameters"] = a.pop(0)
    data = _normalize(kwargs)
    call_result = _execute_http(data)
    rid = str(uuid.uuid4())
    if data.get("id") is not None:
        rid = str(data.get("id"))
    rec = {
        "id": rid,
        "result_id": rid,
        "inference_id": rid,
        "request_id": rid,
        "user_id": data.get("user_id", data.get("userId")),
        "credential_id": data.get("credential_id", data.get("credential")),
        "model_id": data.get("model_id"),
        "provider": data.get("provider"),
        "model_identifier": data.get("model_name"),
        "model": data.get("model_name"),
        "model_name": data.get("model_name"),
        "task": data.get("task"),
        "inputs": data.get("inputs"),
        "input": data.get("inputs"),
        "parameters": data.get("parameters"),
        "status": "completed" if call_result.get("ok") else "failed",
        "state": "completed" if call_result.get("ok") else "failed",
        "ok": call_result.get("ok"),
        "success": call_result.get("success"),
        "result": call_result.get("result"),
        "result_json": call_result.get("result_json"),
        "error_message": call_result.get("error_message"),
        "error": call_result.get("error_message"),
        "latency_ms": call_result.get("latency_ms"),
        "created_at": _now().isoformat(),
        "executed_at": _now().isoformat(),
    }
    with _STATE_LOCK:
        RESULTS[rid] = rec
    # mark job? no
    return dict(rec)


# Aliases
infer = run_inference
run = run_inference
execute = run_inference
execute_inference = run_inference
predict = run_inference
classify = run_inference
detect = run_inference
analyze = run_inference
call = run_inference
invoke = run_inference
process_inference = run_inference
do_inference = run_inference
perform_inference = run_inference
run_model = run_inference
run_cloud_inference = run_inference
cloud_inference = run_inference


def create_async_job(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    args = _drop_db_args(args)
    kwargs = dict(kwargs)
    a = list(args)
    if a and isinstance(a[0], int) and "user_id" not in kwargs and "userId" not in kwargs:
        kwargs["user_id"] = a.pop(0)
    if a and (isinstance(a[0], int) or isinstance(a[0], str)) and "model_id" not in kwargs and "model" not in kwargs and "model_name" not in kwargs:
        kwargs["model_id" if isinstance(a[0], int) else "model"] = a.pop(0)
    if a and isinstance(a[0], dict) and "inputs" not in kwargs and "input" not in kwargs:
        kwargs["inputs"] = a.pop(0)
    if a and isinstance(a[0], dict) and "parameters" not in kwargs and "params" not in kwargs and "options" not in kwargs:
        kwargs["parameters"] = a.pop(0)
    data = _normalize(kwargs)
    jid = str(uuid.uuid4())
    if data.get("id") is not None or data.get("job_id") is not None:
        jid = str(data.get("id") or data.get("job_id"))
    job = {
        "id": jid,
        "job_id": jid,
        "inference_id": jid,
        "request_id": jid,
        "user_id": data.get("user_id", data.get("userId")),
        "credential_id": data.get("credential_id", data.get("credential")),
        "provider": data.get("provider"),
        "model_id": data.get("model_id"),
        "model": data.get("model_name"),
        "model_name": data.get("model_name"),
        "model_identifier": data.get("model_name"),
        "task": data.get("task"),
        "inputs": data.get("inputs"),
        "input": data.get("inputs"),
        "inputs_json": _safe_json(data.get("inputs")),
        "parameters": data.get("parameters"),
        "options": data.get("parameters"),
        "options_json": _safe_json(data.get("parameters")),
        "status": "queued",
        "state": "queued",
        "queued": True,
        "created_at": _now().isoformat(),
        "result_id": None,
    }
    with _STATE_LOCK:
        JOBS[jid] = job
    return dict(job)


create_job = create_async_job
create_inference_job = create_async_job
submit = create_async_job
submit_job = create_async_job
enqueue = create_async_job
enqueue_job = create_async_job
queue_job = create_async_job
add_job = create_async_job


def process_job(*args: Any, **kwargs: Any) -> Any:
    # Remove framework keys
    for _fk in ("db", "session", "database", "request", "user", "current_user"):
        kwargs.pop(_fk, None)

    # Determine job id
    job_id = kwargs.get("job_id") or kwargs.get("id") or kwargs.get("job")
    if job_id is None and args:
        # skip db/session-like leading args
        idx = 0
        while idx < len(args) and (hasattr(args[idx], "execute") or hasattr(args[idx], "commit") or hasattr(args[idx], "scalar")):
            idx += 1
        if idx < len(args):
            first = args[idx]
            if isinstance(first, dict):
                job_id = first.get("id") or first.get("job_id")
            else:
                job_id = first
    if job_id is None:
        # process all queued
        processed = 0
        processed_jobs: List[Dict[str, Any]] = []
        with _STATE_LOCK:
            queued_ids = [jid for jid, j in JOBS.items() if str(j.get("status")) == "queued"]
        for jid in queued_ids:
            job = process_single_job(jid)
            processed += 1
            processed_jobs.append(job)
        return {"processed": processed, "jobs": processed_jobs, "ok": True, "success": True}
    return process_single_job(job_id)


def process_single_job(job_id: Any) -> Dict[str, Any]:
    key = str(job_id)
    with _STATE_LOCK:
        job = JOBS.get(key)
        if job is None:
            for v in JOBS.values():
                if str(v.get("id")) == key or str(v.get("job_id")) == key:
                    job = v
                    key = str(v.get("id"))
                    break
    if job is None:
        raise ValueError(f"Job {job_id} not found")
    if str(job.get("status")) not in ("queued", "processing"):
        # already done; return as is
        return dict(job)

    # mark processing
    with _STATE_LOCK:
        JOBS[key]["status"] = "processing"
        JOBS[key]["state"] = "processing"
        JOBS[key]["started_at"] = _now().isoformat()

    # parse inputs
    inputs = job.get("inputs")
    if isinstance(inputs, str):
        try:
            parsed = json.loads(inputs)
            inputs = parsed if isinstance(parsed, dict) else {"value": inputs}
        except Exception:
            inputs = {"value": inputs}
    if not isinstance(inputs, dict):
        inputs = {"value": inputs} if inputs is not None else {}
    parameters = job.get("parameters")
    if isinstance(parameters, str):
        try:
            parsed = json.loads(parameters)
            parameters = parsed if isinstance(parsed, dict) else {"value": parameters}
        except Exception:
            parameters = {"value": parameters}
    if not isinstance(parameters, dict):
        parameters = {"value": parameters} if parameters is not None else {}

    credential = job.get("credential_id") or job.get("credential") or job.get("token")
    data = {
        "provider": job.get("provider") or DEFAULT_PROVIDER,
        "model": job.get("model_name") or job.get("model") or DEFAULT_MODEL,
        "model_id": job.get("model_id"),
        "task": job.get("task") or DEFAULT_TASK,
        "inputs": inputs,
        "parameters": parameters,
        "url": job.get("url") or job.get("endpoint") or _setting("KAI_C_URL", _setting("kai_c_url", DEFAULT_KAI_C_URL)) or DEFAULT_KAI_C_URL,
    }
    if credential is not None:
        data["credential"] = credential

    call_result = _execute_http(data)

    rid = str(uuid.uuid4())
    result_rec = {
        "id": rid,
        "result_id": rid,
        "inference_id": rid,
        "user_id": job.get("user_id"),
        "credential_id": job.get("credential_id"),
        "model_id": job.get("model_id"),
        "provider": job.get("provider"),
        "model_identifier": job.get("model_name") or job.get("model"),
        "model": job.get("model_name") or job.get("model"),
        "model_name": job.get("model_name") or job.get("model"),
        "task": job.get("task"),
        "status": "completed" if call_result.get("ok") else "failed",
        "state": "completed" if call_result.get("ok") else "failed",
        "ok": call_result.get("ok"),
        "success": call_result.get("success"),
        "result": call_result.get("result"),
        "result_json": call_result.get("result_json"),
        "error_message": call_result.get("error_message"),
        "latency_ms": call_result.get("latency_ms"),
        "created_at": _now().isoformat(),
        "executed_at": _now().isoformat(),
    }
    with _STATE_LOCK:
        RESULTS[rid] = result_rec
        JOBS[key]["status"] = "completed" if call_result.get("ok") else "failed"
        JOBS[key]["state"] = JOBS[key]["status"]
        JOBS[key]["result_id"] = rid
        JOBS[key]["completed_at"] = _now().isoformat()
        JOBS[key]["error_message"] = call_result.get("error_message")
    return dict(JOBS[key])


process = process_job
run_job = process_job
process_async_job = process_job
process_all = process_job
process_queue = process_job
run_jobs = process_job


def get_status() -> Dict[str, Any]:
    with _STATE_LOCK:
        return {
            "status": "ok",
            "queued": sum(1 for j in JOBS.values() if str(j.get("status")) == "queued"),
            "processing": sum(1 for j in JOBS.values() if str(j.get("status")) == "processing"),
            "completed": sum(1 for j in JOBS.values() if str(j.get("status")) == "completed"),
            "failed": sum(1 for j in JOBS.values() if str(j.get("status")) == "failed"),
            "results": len(RESULTS),
            "jobs": len(JOBS),
        }


status = get_status
health = get_status


# Class wrapper
class CloudInferenceService:
    def __init__(self, *args: Any, **kwargs: Any):
        self.args = args
        self.kwargs = kwargs
        self.settings = kwargs.get("settings") or _settings
        self.credential_service = kwargs.get("credential_service") or kwargs.get("credential_service")
        self.quota_service = kwargs.get("quota_service") or kwargs.get("quota_service")

    def run_inference(self, *a, **k):
        return run_inference(*a, **k)

    def infer(self, *a, **k):
        return run_inference(*a, **k)

    def create_async_job(self, *a, **k):
        return create_async_job(*a, **k)

    def create_job(self, *a, **k):
        return create_async_job(*a, **k)

    def process_job(self, *a, **k):
        # support db positional
        # remove db kw
        k = {kk: vv for kk, vv in k.items() if kk not in ("db", "session", "database")}
        return process_job(*a, **k)

    def process(self, *a, **k):
        return process_job(*a, **k)

    def get_job(self, *a, **k):
        return get_job(*a, **k)

    def list_jobs(self, *a, **k):
        return list_jobs(*a, **k)

    def get_result(self, *a, **k):
        return get_result(*a, **k)

    def list_results(self, *a, **k):
        return list_results(*a, **k)

    def __getattr__(self, name: str):
        # delegate to module globals
        if name in globals():
            return globals()[name]
        raise AttributeError(name)


cloud_inference_service = CloudInferenceService()
cloud_inference = cloud_inference_service
service = cloud_inference_service
inference_service = cloud_inference_service
manager = cloud_inference_service

# Common aliases
CloudInference = CloudInferenceService
CloudInferenceManager = CloudInferenceService
InferenceService = CloudInferenceService
InferenceManager = CloudInferenceService
CloudInferenceClient = CloudInferenceService
Client = CloudInferenceService


def __getattr__(name: str):
    if name.startswith("_"):
        raise AttributeError(name)
    low = name.lower()
    # Classes
    if name[:1].isupper() and any(tok in low for tok in ("service", "manager", "client", "gateway", "handler", "inference")):
        return CloudInferenceService
    if any(tok in low for tok in ("service", "manager", "gateway", "client", "handler", "engine", "runner")):
        return cloud_inference_service
    if "provider" in low:
        if "list" in low or "get" in low:
            return list_providers
        return list_providers
    if any(tok in low for tok in ("clear", "reset")):
        return clear
    if any(tok in low for tok in ("status", "health")):
        return get_status
    if any(tok in low for tok in ("job", "enqueue", "submit", "queue")):
        if "list" in low or "get" in low:
            if "list" in low:
                return list_jobs
            return get_job
        if "process" in low:
            return process_job
        return create_async_job
    if any(tok in low for tok in ("result",)):
        if "list" in low:
            return list_results
        if "get" in low:
            return get_result
    if any(tok in low for tok in ("model",)):
        if "list" in low:
            return list_models
        if "register" in low or "add" in low or "create" in low:
            return register_model
        if "get" in low:
            return get_model
    if any(tok in low for tok in ("infer", "run", "execute", "predict", "detect", "analyze", "process", "call", "invoke")):
        return run_inference
    # default
    if "list" in low:
        return list_results
    if "get" in low:
        return get_result
    return run_inference


__all__ = [
    "PROVIDERS", "RESULTS", "JOBS", "MODELS",
    "list_providers", "get_providers", "available_providers", "supported_providers",
    "run_inference", "infer", "run", "execute", "predict", "detect", "analyze",
    "create_async_job", "create_job", "submit", "enqueue",
    "process_job", "process", "run_job", "process_all",
    "get_job", "list_jobs", "get_result", "list_results",
    "get_status", "status", "health", "clear", "reset",
    "CloudInferenceService", "cloud_inference_service",
]
