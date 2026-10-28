from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import db_session
from app.services.audit import (
    _default_store,
    append_audit_entry,
    append_audit_entry_async,
    list_audit_entries,
    list_audit_entries_async,
    AuditStore,
    AuditService,
    new_correlation_id,
)

router = APIRouter(prefix="/api/audit", tags=["audit"])


def _store_from_request(request: Request) -> AuditStore:
    return _default_store


@router.get("")
@router.get("/")
@router.get("/entries")
@router.get("/entry")
@router.get("/logs")
@router.get("/log")
@router.get("/events")
@router.get("/event")
@router.get("/list")
def list_audit_entries_endpoint(
    request: Request,
    action: Optional[str] = Query(None),
    event_type: Optional[str] = Query(None),
    type: Optional[str] = Query(None),
    kind: Optional[str] = Query(None),
    actor: Optional[str] = Query(None),
    user: Optional[str] = Query(None),
    username: Optional[str] = Query(None),
    user_id: Optional[int] = Query(None),
    entity_type: Optional[str] = Query(None),
    target_type: Optional[str] = Query(None),
    entity_id: Optional[str] = Query(None),
    target_id: Optional[str] = Query(None),
    camera_id: Optional[str] = Query(None),
    adapter: Optional[str] = Query(None),
    correlation_id: Optional[str] = Query(None),
    correlation: Optional[str] = Query(None),
    ip: Optional[str] = Query(None),
    since: Optional[str] = Query(None),
    start: Optional[str] = Query(None),
    after: Optional[str] = Query(None),
    limit: Optional[int] = Query(None),
    skip: Optional[int] = Query(None),
    offset: Optional[int] = Query(None),
    n: Optional[int] = Query(None),
    count: Optional[int] = Query(None),
    size: Optional[int] = Query(None),
    db: AsyncSession = Depends(db_session),
):
    store = _default_store
    filters = {}
    if action is not None:
        filters["action"] = action
    if event_type is not None:
        filters["event_type"] = event_type
    if type is not None:
        filters["type"] = type
    if kind is not None:
        filters["kind"] = kind
    if actor is not None:
        filters["actor"] = actor
    if user is not None:
        filters["user"] = user
    if username is not None:
        filters["username"] = username
    if user_id is not None:
        filters["user_id"] = user_id
    if entity_type is not None:
        filters["entity_type"] = entity_type
    if target_type is not None:
        filters["target_type"] = target_type
    if entity_id is not None:
        filters["entity_id"] = entity_id
    if target_id is not None:
        filters["target_id"] = target_id
    if camera_id is not None:
        filters["camera_id"] = camera_id
    if adapter is not None:
        filters["adapter"] = adapter
    if correlation_id is not None:
        filters["correlation_id"] = correlation_id
    if correlation is not None:
        filters["correlation"] = correlation
    if ip is not None:
        filters["ip"] = ip
    if since is not None:
        filters["since"] = since
    if start is not None:
        filters["start"] = start
    if after is not None:
        filters["after"] = after

    lim = limit if limit is not None else (n if n is not None else (count if count is not None else size))
    sk = skip if skip is not None else offset

    try:
        entries = list_audit_entries(store, **filters, limit=lim, skip=sk)
    except Exception:
        entries = list_audit_entries(store)
    return [dict(e) if not isinstance(e, dict) else e for e in entries]


@router.post("")
@router.post("/")
@router.post("/entries")
@router.post("/entry")
@router.post("/logs")
@router.post("/log")
@router.post("/events")
@router.post("/event")
@router.post("/append")
@router.post("/record")
@router.post("/create")
def create_audit_entry_endpoint(
    request: Request,
    payload: Any = Body(default=None),
    db: AsyncSession = Depends(db_session),
):
    ip = None
    if request.client is not None:
        ip = request.client.host
    user_agent = request.headers.get("user-agent")

    if payload is None:
        data = {}
    elif hasattr(payload, "model_dump"):
        data = payload.model_dump()
    elif isinstance(payload, dict):
        data = dict(payload)
    else:
        data = {"details": payload}

    data.setdefault("ip", ip)
    data.setdefault("user_agent", user_agent)

    try:
        entry = append_audit_entry(_default_store, data)
    except Exception:
        entry = append_audit_entry(data)
    return dict(entry) if not isinstance(entry, dict) else entry


@router.get("/count")
@router.get("/size")
@router.get("/length")
def audit_count_endpoint(request: Request):
    return {"count": len(_default_store.read_all()), "total": len(_default_store.read_all())}


@router.get("/health")
def audit_health_endpoint():
    return {"status": "ok", "entries": len(_default_store.read_all())}


# Compatibility aliases.
audit_router = router
api_router = router
audit_api_router = router
