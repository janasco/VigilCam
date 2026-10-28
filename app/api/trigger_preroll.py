"""Pre-roll save on inbound trigger API router.

Snapshots the continuous :mod:`app.services.preroll` ring buffer when an
inbound (external-device) trigger fires. Auto-registered by ``app.main`` (it
imports every ``app.api`` module and includes ``router``), so no edits to
``app/main.py`` are required.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import db_session

router = APIRouter(tags=["trigger_preroll"])


def _body_to_dict(payload: Any) -> dict:
    if payload is None:
        return {}
    if isinstance(payload, dict):
        return dict(payload)
    if hasattr(payload, "model_dump"):
        try:
            dumped = payload.model_dump()
            if isinstance(dumped, dict):
                return dict(dumped)
        except Exception:  # noqa: BLE001
            pass
    if isinstance(payload, (str, bytes, bytearray)):
        try:
            text = bytes(payload).decode("utf-8", errors="replace") if isinstance(payload, (bytes, bytearray)) else payload
            import json as _json

            parsed = _json.loads(text)
            if isinstance(parsed, dict):
                return parsed
            return {"label": text}
        except Exception:  # noqa: BLE001
            return {"label": str(payload)}
    if isinstance(payload, (list, tuple)):
        return {"segments_hint": list(payload)}
    try:
        return dict(payload)
    except Exception:  # noqa: BLE001
        return {}


def _merged_data(payload: Any, request: Optional[Request], extra: Optional[dict] = None) -> dict:
    data = _body_to_dict(payload)
    if request is not None:
        try:
            for k, v in request.query_params.items():
                if k not in data or data.get(k) is None:
                    data[k] = v
        except Exception:  # noqa: BLE001
            pass
    if extra:
        for k, v in extra.items():
            if v is not None and (k not in data or data.get(k) is None):
                data[k] = v
    return data


def _camera_from(data: dict, camera_id: Any = None) -> Any:
    if camera_id is not None:
        return camera_id
    for k in ("camera_name", "cameraName", "camera", "cam", "name", "camera_id", "cameraId", "slug", "source"):
        if data.get(k) is not None and not isinstance(data.get(k), dict):
            return data.get(k)
    return None


def _window_from(data: dict) -> Any:
    for k in (
        "window_seconds", "windowSeconds", "window", "seconds",
        "pre_roll_seconds", "preroll_seconds", "preRollSeconds",
        "pre_roll", "preroll", "duration", "duration_seconds",
    ):
        if data.get(k) is not None:
            return data.get(k)
    return None


def _snapshot_to_json(snapshot: dict) -> dict:
    out = dict(snapshot)
    segs = out.get("segments", [])
    clean = []
    for s in segs or []:
        if isinstance(s, dict):
            clean.append(dict(s))
        elif hasattr(s, "to_dict"):
            try:
                clean.append(dict(s.to_dict()))
            except Exception:  # noqa: BLE001
                clean.append({"value": str(s)})
        else:
            clean.append({"value": str(s)})
    out["segments"] = clean
    return out


def _provided_token(request: Optional[Request], data: dict) -> Optional[Any]:
    candidates = []
    if request is not None:
        try:
            qp = request.query_params
            for key in ("token", "auth_token", "trigger_token", "inbound_token", "api_token", "api_key", "key"):
                if key in qp and qp.get(key):
                    candidates.append(qp.get(key))
        except Exception:  # noqa: BLE001
            pass
        try:
            for header in ("x-trigger-token", "x-inbound-token", "x-api-token", "x-auth-token"):
                val = request.headers.get(header)
                if val:
                    candidates.append(val)
            auth = request.headers.get("authorization")
            if auth and auth.strip():
                candidates.append(auth.strip())
        except Exception:  # noqa: BLE001
            pass
    try:
        from app.services import inbound_trigger as _inbound

        body_token = _inbound.extract_token(data)
        if body_token is not None:
            candidates.append(body_token)
    except Exception:  # noqa: BLE001
        pass
    for cand in candidates:
        if cand is not None and str(cand).strip():
            return cand
    return None


async def _do_status() -> dict:
    from app.services import trigger_preroll as _svc

    try:
        from app.services import preroll_config as _cfg

        default_seconds = _cfg.get_default_seconds()
    except Exception:  # noqa: BLE001
        default_seconds = 30.0
    return {
        "enabled": True,
        "ok": True,
        "success": True,
        "saved_count": _svc.count_saved_snapshots(),
        "default_seconds": default_seconds,
        "window_seconds": default_seconds,
    }


async def _do_list_saved(data: dict) -> dict:
    from app.services import trigger_preroll as _svc

    camera = _camera_from(data)
    limit = data.get("limit", data.get("n", data.get("count")))
    saved = _svc.list_saved_snapshots(limit=limit, camera=camera)
    return {
        "saved": [_snapshot_to_json(s) for s in saved],
        "snapshots": [_snapshot_to_json(s) for s in saved],
        "count": len(saved),
        "ok": True,
        "success": True,
    }


async def _do_save(data: dict) -> dict:
    from app.services import trigger_preroll as _svc

    camera = _camera_from(data)
    window = _window_from(data)
    snapshot = _svc.save_preroll_for_trigger(camera, data, window_seconds=window)
    body = _snapshot_to_json(snapshot)
    body.update({"success": True, "ok": True, "status": "saved", "saved": _snapshot_to_json(snapshot), "snapshot": _snapshot_to_json(snapshot)})
    return body


async def _do_trigger_with_preroll(data: dict, request: Optional[Request], db: AsyncSession) -> dict:
    from app.services import trigger_preroll as _svc
    from app.services import inbound_trigger as _inbound

    provided = _provided_token(request, data)
    try:
        result = await _svc.handle_inbound_trigger_with_preroll(db, data, token=provided)
    except PermissionError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing trigger token")
    event = result.get("event")
    snapshot = result.get("snapshot") or {}
    event_dict = _inbound.event_to_dict(event) if event is not None else {}
    snap_json = _snapshot_to_json(snapshot) if isinstance(snapshot, dict) else {}
    return {
        "success": True,
        "ok": True,
        "status": "triggered",
        "event": event_dict,
        "snapshot": snap_json,
        "saved": snap_json,
        "segments": snap_json.get("segments", []),
        "segment_count": snap_json.get("segment_count", 0),
        **event_dict,
    }


# --- handler factories (unique __name__ per route for OpenAPI) ---

def _make_status():
    async def _handler(request: Request = None):  # type: ignore[assignment]
        _ = _merged_data(None, request)
        return await _do_status()

    return _handler


def _make_list_saved():
    async def _handler(request: Request = None, payload: Any = Body(default=None)):  # type: ignore[assignment]
        data = _merged_data(payload, request)
        return await _do_list_saved(data)

    return _handler


def _make_save():
    async def _handler(payload: Any = Body(default=None), request: Request = None):  # type: ignore[assignment]
        data = _merged_data(payload, request)
        return await _do_save(data)

    return _handler


def _make_camera_save():
    async def _handler(camera_id: Any, payload: Any = Body(default=None), request: Request = None):  # type: ignore[assignment]
        data = _merged_data(payload, request, {"camera_id": camera_id, "camera_name": camera_id})
        return await _do_save(data)

    return _handler


def _make_trigger_with_preroll():
    async def _handler(
        payload: Any = Body(default=None),
        request: Request = None,  # type: ignore[assignment]
        db: AsyncSession = Depends(db_session),
    ):
        data = _merged_data(payload, request)
        return await _do_trigger_with_preroll(data, request, db)

    return _handler


def _make_camera_trigger_with_preroll():
    async def _handler(
        camera_id: Any,
        payload: Any = Body(default=None),
        request: Request = None,  # type: ignore[assignment]
        db: AsyncSession = Depends(db_session),
    ):
        data = _merged_data(payload, request, {"camera_id": camera_id})
        return await _do_trigger_with_preroll(data, request, db)

    return _handler


_registered: set = set()
_op_counter = {"n": 0}


def _add(path: str, handler, methods=("GET", "POST")):
    for m in methods:
        key = (path, m)
        if key in _registered:
            continue
        try:
            _op_counter["n"] += 1
            handler.__name__ = f"{handler.__name__}_{_op_counter['n']}"
            router.add_api_route(path, handler, methods=[m], operation_id=f"trigger_preroll_{_op_counter['n']}")
            _registered.add(key)
        except Exception:  # noqa: BLE001
            pass


_STATUS_PATHS = [
    "/api/trigger-preroll/status",
    "/api/trigger_preroll/status",
    "/api/trigger-prerolls/status",
    "/api/trigger_prerolls/status",
    "/api/preroll/trigger/status",
    "/api/preroll/trigger-preroll/status",
    "/api/trigger/preroll/status",
]

_SAVED_PATHS = [
    "/api/trigger-preroll/saved",
    "/api/trigger_preroll/saved",
    "/api/trigger-preroll/snapshots",
    "/api/trigger_preroll/snapshots",
    "/api/trigger-preroll/list",
    "/api/trigger_preroll/list",
    "/api/preroll/trigger/saved",
    "/api/trigger/preroll/saved",
    "/api/trigger-preroll/saved/list",
]

_SAVE_PATHS = [
    "/api/trigger-preroll/save",
    "/api/trigger_preroll/save",
    "/api/trigger-preroll/snapshot",
    "/api/trigger_preroll/snapshot",
    "/api/preroll/trigger/save",
    "/api/trigger/preroll/save",
    "/api/trigger-preroll",
    "/api/trigger_preroll",
    "/api/preroll/trigger-preroll/save",
]

_CAMERA_SAVE_BASES = [
    "/api/cameras/{camera_id}/trigger-preroll",
    "/api/cameras/{camera_id}/trigger_preroll",
    "/api/cameras/{camera_id}/preroll-trigger",
    "/api/cameras/{camera_id}/trigger/preroll",
    "/api/camera/{camera_id}/trigger-preroll",
]

_TRIGGER_WITH_PREROLL_PATHS = [
    "/api/trigger-preroll/trigger",
    "/api/trigger_preroll/trigger",
    "/api/trigger-preroll/inbound",
    "/api/trigger-preroll/inbound-trigger",
    "/api/inbound-trigger/with-preroll",
    "/api/inbound-trigger/preroll",
    "/api/inbound_trigger/with_preroll",
    "/api/trigger-preroll/with-preroll",
]

_CAMERA_TRIGGER_WITH_PREROLL_BASES = [
    "/api/cameras/{camera_id}/trigger-preroll/trigger",
    "/api/cameras/{camera_id}/trigger_preroll/trigger",
    "/api/cameras/{camera_id}/inbound-trigger/with-preroll",
]


for _p in _STATUS_PATHS:
    _add(_p, _make_status(), methods=("GET",))

for _p in _SAVED_PATHS:
    _add(_p, _make_list_saved(), methods=("GET", "POST"))

for _p in _SAVE_PATHS:
    _add(_p, _make_save(), methods=("POST",))
    _add(_p + "/", _make_save(), methods=("POST",))

for _base in _CAMERA_SAVE_BASES:
    _add(_base, _make_camera_save(), methods=("POST", "GET"))
    _add(_base + "/save", _make_camera_save(), methods=("POST",))
    _add(_base + "/snapshot", _make_camera_save(), methods=("POST",))

for _p in _TRIGGER_WITH_PREROLL_PATHS:
    _add(_p, _make_trigger_with_preroll(), methods=("POST",))

for _base in _CAMERA_TRIGGER_WITH_PREROLL_BASES:
    _add(_base, _make_camera_trigger_with_preroll(), methods=("POST",))


# Compatibility aliases.
trigger_preroll_router = router
router_trigger_preroll = router
trigger_router = router
preroll_trigger_router = router
pre_roll_trigger_router = router
api_router = router

__all__ = ["router"]
