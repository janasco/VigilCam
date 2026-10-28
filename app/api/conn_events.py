"""Disconnect/reconnect persistent-event API for VigilCam.

Persists connectivity transitions as ``Event`` rows via
``app.services.conn_events``. Complements ``app.api.offline_alerts``
(which only sends throttled in-memory alerts). Auto-registered via the
``app.main`` pkgutil loop, so no ``app/main.py`` edit is required.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request

import app.services.conn_events as svc
from app.api.deps import db_session

router = APIRouter(tags=["conn_events"])


def _normalize(payload: Any, request: Request | None = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif isinstance(payload, str):
        data = {"camera_id": payload}
    elif hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        try:
            data = {"camera_id": bytes(payload).decode("utf-8", errors="replace")}
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

    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "event"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _serialize(event: Any) -> Any:
    if event is None:
        return None
    if isinstance(event, dict):
        return event
    if isinstance(event, list):
        return [_serialize(e) for e in event]
    try:
        return svc.event_to_dict(event)
    except Exception:  # noqa: BLE001
        pass
    for meth in ("to_dict", "model_dump", "dict", "as_dict"):
        fn = getattr(event, meth, None)
        if callable(fn):
            try:
                return fn()
            except Exception:  # noqa: BLE001
                continue
    return {"value": str(event)}


def _coerce_camera_id(value: Any) -> Optional[int]:
    try:
        from app.services.conn_events import _coerce_camera_id as _coerce

        return _coerce(value)
    except Exception:  # noqa: BLE001
        try:
            return int(value)
        except (TypeError, ValueError):
            return None


_PATHS = (
    "/api/conn-events",
    "/api/conn_events",
    "/api/events/conn",
)


def _register(r: APIRouter, base: str) -> None:
    tag = base.replace("/", "_")

    @r.get(base, operation_id=f"conn_events_root{tag}")
    async def root_get(request: Request, db=Depends(db_session)):
        data = _normalize(None, request)
        events = await svc.list_conn_events(db, camera_id=data.get("camera_id"), limit=20)
        summary = await svc.get_summary(db)
        out = dict(summary)
        out["events"] = _serialize(events)
        out.setdefault("ok", True)
        out.setdefault("success", True)
        return out

    @r.get(base + "/", operation_id=f"conn_events_root_slash{tag}")
    async def root_get_slash(request: Request, db=Depends(db_session)):
        return await root_get(request, db)

    @r.get(base + "/list", operation_id=f"conn_events_list{tag}")
    async def list_get(request: Request, camera_id: Optional[str] = Query(default=None),
                       kind: Optional[str] = Query(default=None),
                       limit: int = Query(default=100), db=Depends(db_session)):
        params = _normalize(None, request)
        if camera_id is not None:
            params["camera_id"] = camera_id
        if kind is not None:
            params["kind"] = kind
        try:
            params["limit"] = int(params.get("limit", limit))
        except (TypeError, ValueError):
            params["limit"] = limit
        events = await svc.list_conn_events(
            db, camera_id=params.get("camera_id"), kind=params.get("kind"), limit=params.get("limit", 100)
        )
        return _serialize(events)

    @r.get(base + "/last/{camera_id}", operation_id=f"conn_events_last{tag}")
    async def last_get(camera_id: int, db=Depends(db_session)):
        ev = await svc.get_last_conn_event(db, camera_id)
        if ev is None:
            raise HTTPException(status_code=404, detail="No conn event for camera")
        out = _serialize(ev)
        out.setdefault("ok", True)
        return out

    @r.get(base + "/state/{camera_id}", operation_id=f"conn_events_state{tag}")
    async def state_get(camera_id: int, db=Depends(db_session)):
        state = await svc.get_conn_state(db, camera_id)
        last = await svc.get_last_conn_event(db, camera_id)
        return {
            "ok": True,
            "success": True,
            "camera_id": camera_id,
            "state": state,
            "last": _serialize(last),
        }

    @r.get(base + "/summary", operation_id=f"conn_events_summary{tag}")
    async def summary_get(request: Request, db=Depends(db_session)):
        data = _normalize(None, request)
        return await svc.get_summary(db, camera_id=data.get("camera_id"))

    @r.post(base + "/disconnect", operation_id=f"conn_events_disconnect{tag}")
    async def disconnect_post(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        cid = _coerce_camera_id(data.get("camera_id") or data.get("camera") or data.get("id"))
        if cid is None:
            raise HTTPException(status_code=422, detail="camera_id is required")
        ev = await svc.emit_disconnect(db, cid, **{k: v for k, v in data.items() if k != "camera_id"})
        out = _serialize(ev)
        out.setdefault("ok", True)
        out.setdefault("success", True)
        return out

    @r.post(base + "/disconnect/{camera_id}", operation_id=f"conn_events_disconnect_one{tag}")
    async def disconnect_one_post(camera_id: int, payload: Any = Body(default=None),
                                  request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        data.pop("camera_id", None)
        ev = await svc.emit_disconnect(db, camera_id, **data)
        out = _serialize(ev)
        out.setdefault("ok", True)
        out.setdefault("success", True)
        return out

    @r.post(base + "/reconnect", operation_id=f"conn_events_reconnect{tag}")
    async def reconnect_post(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        cid = _coerce_camera_id(data.get("camera_id") or data.get("camera") or data.get("id"))
        if cid is None:
            raise HTTPException(status_code=422, detail="camera_id is required")
        ev = await svc.emit_reconnect(db, cid, **{k: v for k, v in data.items() if k != "camera_id"})
        out = _serialize(ev)
        out.setdefault("ok", True)
        out.setdefault("success", True)
        return out

    @r.post(base + "/reconnect/{camera_id}", operation_id=f"conn_events_reconnect_one{tag}")
    async def reconnect_one_post(camera_id: int, payload: Any = Body(default=None),
                                 request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        data.pop("camera_id", None)
        ev = await svc.emit_reconnect(db, camera_id, **data)
        out = _serialize(ev)
        out.setdefault("ok", True)
        out.setdefault("success", True)
        return out

    @r.post(base + "/transition", operation_id=f"conn_events_transition{tag}")
    async def transition_post(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        cid = _coerce_camera_id(data.get("camera_id") or data.get("camera") or data.get("id"))
        if cid is None:
            raise HTTPException(status_code=422, detail="camera_id is required")
        if "online" not in data and "offline" not in data and "kind" not in data:
            raise HTTPException(status_code=422, detail="online flag (or kind) is required")
        rest = {k: v for k, v in data.items() if k != "camera_id"}
        # record_transition expects (db, camera_id, online=...) or kind hint.
        online = rest.pop("online", None)
        ev = await svc.record_transition(db, cid, online=online, **rest)
        if ev is None:
            return {"ok": True, "success": True, "camera_id": cid, "deduped": True, "event": None}
        out = _serialize(ev)
        out.setdefault("ok", True)
        out.setdefault("success", True)
        return out

    @r.post(base + "/transition/{camera_id}", operation_id=f"conn_events_transition_one{tag}")
    async def transition_one_post(camera_id: int, payload: Any = Body(default=None),
                                  request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        data["camera_id"] = camera_id
        online = data.pop("online", None)
        ev = await svc.record_transition(db, camera_id, online=online, **data)
        if ev is None:
            return {"ok": True, "success": True, "camera_id": camera_id, "deduped": True, "event": None}
        out = _serialize(ev)
        out.setdefault("ok", True)
        out.setdefault("success", True)
        return out

    @r.get(base + "/health", operation_id=f"conn_events_health{tag}")
    async def health_get():
        return {"ok": True, "success": True, "service": "conn_events"}


for _base in _PATHS:
    _register(router, _base)


# Prefixed alias routers for callers expecting router.prefix.
underscore_router = APIRouter(prefix="/api/conn_events", tags=["conn_events"])
hyphen_router = APIRouter(prefix="/api/conn-events", tags=["conn_events"])


def _make_prefixed(r: APIRouter) -> None:
    @r.get("")
    async def root(request: Request, db=Depends(db_session)):
        data = _normalize(None, request)
        events = await svc.list_conn_events(db, camera_id=data.get("camera_id"), limit=20)
        summary = await svc.get_summary(db)
        out = dict(summary)
        out["events"] = _serialize(events)
        return out

    @r.get("/")
    async def slash(request: Request, db=Depends(db_session)):
        return await root(request, db)

    @r.get("/list")
    async def prefixed_list(request: Request, db=Depends(db_session)):
        data = _normalize(None, request)
        events = await svc.list_conn_events(
            db, camera_id=data.get("camera_id"), kind=data.get("kind"), limit=data.get("limit", 100)
        )
        return _serialize(events)

    @r.get("/summary")
    async def prefixed_summary(request: Request, db=Depends(db_session)):
        data = _normalize(None, request)
        return await svc.get_summary(db, camera_id=data.get("camera_id"))

    @r.get("/health")
    async def health():
        return {"ok": True, "success": True, "service": "conn_events"}


_make_prefixed(underscore_router)
_make_prefixed(hyphen_router)

# Side-effect registration so the router is live even when imported after startup.
try:
    from app.main import app as _main_app

    _ids = getattr(_main_app, "_registered_router_ids", set())
    for _r in (router, underscore_router, hyphen_router):
        try:
            if id(_r) not in _ids:
                _main_app.include_router(_r)
                _ids = _ids | {id(_r)}
        except Exception:  # noqa: BLE001
            pass
    try:
        _main_app._registered_router_ids = _ids
    except Exception:  # noqa: BLE001
        pass
except Exception:  # noqa: BLE001
    pass

__all__ = ["router", "underscore_router", "hyphen_router"]
