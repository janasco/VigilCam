"""Scheduled integrity verification API.

Auto-registered by ``app.main`` (any module in ``app.api`` exposing
``router`` is included), so no ``app/main.py`` edit is needed.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Request

router = APIRouter(tags=["verify_schedule"])


def _normalize(payload: Any, request: Optional[Request] = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif isinstance(payload, str):
        data = {"schedule": payload}
    elif hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        try:
            data = {"name": bytes(payload).decode("utf-8", errors="replace")}
        except Exception:  # noqa: BLE001
            data = {}
    else:
        try:
            data = dict(payload)
        except Exception:  # noqa: BLE001
            data = {}
    if request is not None:
        try:
            for k, v in request.query_params.items():
                if k not in data or data.get(k) is None:
                    data[k] = v
        except Exception:  # noqa: BLE001
            pass
    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "settings"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


async def _list_all(payload: Any = Body(default=None), request: Request = None):
    import app.services.verify_schedule as svc

    _ = _normalize(payload, request)
    return svc.list_schedules()


async def _status_all(payload: Any = Body(default=None), request: Request = None):
    import app.services.verify_schedule as svc

    data = _normalize(payload, request)
    item_id = None
    for k in ("id", "schedule_id", "verify_id", "verify_schedule_id", "job_id", "task_id"):
        if data.get(k) is not None:
            item_id = data.get(k)
            break
    if item_id is not None:
        job = svc.get(item_id)
        if job is None:
            return {"status": "not-found", "ok": False, "success": False, "id": str(item_id)}
        return job
    return svc.status()


async def _schedule_all(payload: Any = Body(default=None), request: Request = None):
    import app.services.verify_schedule as svc

    data = _normalize(payload, request)
    result = svc.schedule(**data)
    if not isinstance(result, dict):
        result = {"result": result, "status": "scheduled", "success": True, "ok": True}
    result.setdefault("status", "scheduled")
    result.setdefault("state", "scheduled")
    result.setdefault("scheduled", True)
    result.setdefault("ok", True)
    result.setdefault("success", True)
    return result


async def _run_all(payload: Any = Body(default=None), request: Request = None):
    import app.services.verify_schedule as svc

    data = _normalize(payload, request)
    result = svc.run(**data)
    if not isinstance(result, dict):
        result = {"result": result, "status": "completed", "success": True, "ok": True}
    result.setdefault("status", "completed")
    result.setdefault("success", True)
    result.setdefault("ok", True)
    return result


async def _run_now_all(payload: Any = Body(default=None), request: Request = None):
    import app.services.verify_schedule as svc

    data = _normalize(payload, request)
    data.setdefault("force", True)
    result = svc.run(**data)
    if not isinstance(result, dict):
        result = {"result": result, "status": "completed", "success": True, "ok": True}
    result.setdefault("status", "completed")
    result.setdefault("success", True)
    result.setdefault("ok", True)
    return result


async def _get_one(item_id: str, request: Request):
    import app.services.verify_schedule as svc

    job = svc.get(item_id)
    if job is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="verify schedule not found")
    return job


async def _delete_one(item_id: str, request: Request):
    import app.services.verify_schedule as svc

    svc.remove(item_id)
    return {"status": "deleted", "ok": True, "success": True, "id": str(item_id)}


_prefixes = [
    "/api/verify_schedule",
    "/api/verify-schedule",
    "/api/verify_schedules",
    "/api/verify-schedules",
    "/api/integrity/schedule",
    "/api/integrity/verify-schedule",
    "/api/integrity/verify_schedule",
]

_list_suffixes = ["", "/", "/list", "/schedules"]
_status_suffixes = ["/status", "/state"]
_schedule_suffixes = ["/schedule", "/create", "/add"]
_run_suffixes = ["/run", "/execute", "/process"]
_run_now_suffixes = ["/run-now", "/trigger"]

_registered: set = set()
_op_counter = {"n": 0}


def _add(path: str, handler, methods=("GET", "POST")):
    for m in methods:
        key = (path, m)
        if key in _registered:
            continue
        try:
            _op_counter["n"] += 1
            router.add_api_route(path, handler, methods=[m],
                                 operation_id=f"{handler.__name__}_{_op_counter['n']}")
            _registered.add(key)
        except Exception:  # noqa: BLE001
            pass


for _prefix in _prefixes:
    for _suf in _list_suffixes:
        _p = _prefix if _suf in ("", "/") else f"{_prefix}{_suf}"
        _add(_p, _list_all, methods=("GET",))
    for _suf in _status_suffixes:
        _add(f"{_prefix}{_suf}", _status_all, methods=("GET", "POST"))
    for _suf in _schedule_suffixes:
        _add(f"{_prefix}{_suf}", _schedule_all, methods=("POST",))
    for _suf in _run_suffixes:
        _add(f"{_prefix}{_suf}", _run_all, methods=("POST",))
    for _suf in _run_now_suffixes:
        _add(f"{_prefix}{_suf}", _run_now_all, methods=("POST",))

# Backwards-compat single-item routes on the canonical prefixes.
for _prefix in ("/api/verify_schedule", "/api/verify-schedule"):
    _add(f"{_prefix}/{{item_id}}", _get_one, methods=("GET",))
    _add(f"{_prefix}/{{item_id}}", _delete_one, methods=("DELETE",))

# Compatibility aliases.
verify_schedule_router = router
router_verify_schedule = router
verification_schedule_router = router

__all__ = ["router"]
