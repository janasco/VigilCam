"""User activity log API router.

Exposes the in-memory/JSONL user activity store over HTTP. Registered
automatically by the ``app.main`` auto-loader (any ``app.api`` module with
a ``router`` attribute), so no edits to ``app/main.py`` are required.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Query, Request

from app.services.activity_log import (
    _default_store,
    list_activity_logs,
    log_activity,
)

router = APIRouter(tags=["activity_log"])

_PATHS = (
    "/api/activity-log",
    "/api/activity_log",
    "/api/activity-logs",
    "/api/activity_logs",
    "/api/user-activity",
    "/api/user_activity",
    "/api/user-activity-log",
    "/api/user_activity_log",
    "/api/activity",
    "/api/activities",
    "/api/user-activities",
    "/api/user_activities",
)


def _normalize_payload(payload: Any, request: Request | None = None) -> dict:
    if payload is None:
        data: dict = {}
    elif hasattr(payload, "model_dump"):
        try:
            data = dict(payload.model_dump())
        except Exception:
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    else:
        data = {"details": payload}
    if request is not None:
        try:
            for k, v in request.query_params.items():
                if k not in data or data.get(k) is None:
                    data[k] = v
        except Exception:  # noqa: BLE001
            pass
    for nested_key in ("entry", "activity", "activity_entry", "record",
                       "payload", "data", "body", "event", "log"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _register(r: APIRouter, base: str) -> None:
    @r.get(base)
    def list_root(
        request: Request,
        user: Optional[str] = Query(None),
        username: Optional[str] = Query(None),
        actor: Optional[str] = Query(None),
        user_id: Optional[int] = Query(None),
        action: Optional[str] = Query(None),
        activity: Optional[str] = Query(None),
        activity_type: Optional[str] = Query(None),
        type: Optional[str] = Query(None),  # noqa: A002
        kind: Optional[str] = Query(None),
        event: Optional[str] = Query(None),
        resource_type: Optional[str] = Query(None),
        resource_id: Optional[str] = Query(None),
        entity_type: Optional[str] = Query(None),
        entity_id: Optional[str] = Query(None),
        since: Optional[str] = Query(None),
        limit: Optional[int] = Query(None),
        skip: Optional[int] = Query(None),
        offset: Optional[int] = Query(None),
        n: Optional[int] = Query(None),
    ):
        filters: dict[str, Any] = {}
        if user is not None:
            filters["user"] = user
        if username is not None:
            filters["username"] = username
        if actor is not None:
            filters["actor"] = actor
        if user_id is not None:
            filters["user_id"] = user_id
        if action is not None:
            filters["action"] = action
        if activity is not None:
            filters["activity"] = activity
        if activity_type is not None:
            filters["activity_type"] = activity_type
        if type is not None:
            filters["type"] = type
        if kind is not None:
            filters["kind"] = kind
        if event is not None:
            filters["event"] = event
        if resource_type is not None:
            filters["resource_type"] = resource_type
        if resource_id is not None:
            filters["resource_id"] = resource_id
        if entity_type is not None:
            filters["entity_type"] = entity_type
        if entity_id is not None:
            filters["entity_id"] = entity_id
        if since is not None:
            filters["since"] = since
        lim = limit if limit is not None else n
        sk = skip if skip is not None else offset
        try:
            entries = list_activity_logs(_default_store, **filters, limit=lim, skip=sk)
        except Exception:
            entries = list_activity_logs(_default_store)
        return [dict(e) if not isinstance(e, dict) else e for e in entries]

    @r.get(base + "/")
    def list_root_slash(request: Request):
        return list_root(
            request, None, None, None, None, None, None, None,
            None, None, None, None, None, None, None, None, None, None, None,
        )

    @r.get(base + "/entries")
    def list_entries(request: Request):
        return list_root(
            request, None, None, None, None, None, None, None,
            None, None, None, None, None, None, None, None, None, None, None,
        )

    @r.get(base + "/list")
    def list_list(request: Request):
        return list_entries(request)

    @r.get(base + "/count")
    def count_entries(request: Request):
        total = len(_default_store.read_all())
        return {"count": total, "total": total}

    @r.get(base + "/health")
    def health(request: Request):
        return {"status": "ok", "entries": len(_default_store.read_all())}

    @r.get(base + "/{entry_id}")
    def get_entry(entry_id: str, request: Request):
        found = _default_store.get(entry_id)
        if found is None:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="Activity log entry not found")
        return dict(found)

    @r.post(base)
    def create_root(payload: Any = Body(default=None), request: Request = None):
        data = _normalize_payload(payload, request)
        if request is not None:
            try:
                if request.client is not None:
                    data.setdefault("ip", request.client.host)
            except Exception:  # noqa: BLE001
                pass
            try:
                data.setdefault("user_agent", request.headers.get("user-agent"))
            except Exception:  # noqa: BLE001
                pass
        try:
            entry = log_activity(_default_store, data)
        except Exception:
            entry = log_activity(data)
        return dict(entry) if not isinstance(entry, dict) else entry

    @r.post(base + "/")
    def create_root_slash(payload: Any = Body(default=None), request: Request = None):
        return create_root(payload, request)

    @r.post(base + "/entries")
    def create_entries(payload: Any = Body(default=None), request: Request = None):
        return create_root(payload, request)

    @r.post(base + "/entry")
    def create_entry(payload: Any = Body(default=None), request: Request = None):
        return create_root(payload, request)

    @r.post(base + "/log")
    def create_log(payload: Any = Body(default=None), request: Request = None):
        return create_root(payload, request)

    @r.post(base + "/append")
    def create_append(payload: Any = Body(default=None), request: Request = None):
        return create_root(payload, request)

    @r.post(base + "/record")
    def create_record(payload: Any = Body(default=None), request: Request = None):
        return create_root(payload, request)

    @r.post(base + "/create")
    def create_create(payload: Any = Body(default=None), request: Request = None):
        return create_root(payload, request)

    @r.delete(base + "/clear")
    def clear_entries(request: Request):
        _default_store.clear()
        return {"ok": True, "cleared": True}


for _base in _PATHS:
    _register(router, _base)


# Prefixed routers for explicit mounting compatibility.
underscore_router = APIRouter(prefix="/api/activity_log", tags=["activity_log"])
hyphen_router = APIRouter(prefix="/api/activity-log", tags=["activity_log"])
activity_router = APIRouter(prefix="/api/activity", tags=["activity_log"])
user_activity_router = APIRouter(prefix="/api/user-activity", tags=["activity_log"])


def _make_prefixed(r: APIRouter) -> None:
    @r.get("")
    def _list(request: Request):
        return [dict(e) for e in list_activity_logs(_default_store)]

    @r.get("/")
    def _list_slash(request: Request):
        return _list(request)

    @r.get("/entries")
    def _entries(request: Request):
        return _list(request)

    @r.get("/count")
    def _count(request: Request):
        total = len(_default_store.read_all())
        return {"count": total, "total": total}

    @r.get("/health")
    def _health(request: Request):
        return {"status": "ok", "entries": len(_default_store.read_all())}

    @r.post("")
    def _create(payload: Any = Body(default=None), request: Request = None):
        data = _normalize_payload(payload, request)
        entry = log_activity(_default_store, data)
        return dict(entry)

    @r.post("/")
    def _create_slash(payload: Any = Body(default=None), request: Request = None):
        return _create(payload, request)


for _r in (underscore_router, hyphen_router, activity_router, user_activity_router):
    _make_prefixed(_r)


# Compatibility aliases.
activity_log_router = router
api_router = router
activity_api_router = router
user_activity_log_router = router
