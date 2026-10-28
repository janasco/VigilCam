from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Request

try:
    from app.api.deps import db_session
except Exception:  # noqa: BLE001
    db_session = None  # type: ignore[assignment]

import app.services.snapshot_now as snapshot_now_service_mod

router = APIRouter(tags=["snapshot_now"])


def _normalize(payload: Any, request: Optional[Request] = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif isinstance(payload, str):
        s = payload.strip()
        if s.startswith(("rtsp://", "rtmp://", "http://", "https://", "/")):
            data = {"source": s}
        elif s.isdigit():
            data = {"camera_id": s}
        else:
            data = {"label": s}
    elif hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
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

    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "settings", "message"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)

    return data


async def _resolve_source(camera_id: Any, db: Any = None) -> Optional[str]:
    if camera_id is None or db is None:
        return None
    try:
        cid = int(str(camera_id).strip())
    except (TypeError, ValueError):
        return None
    try:
        from app.models.entities import Camera

        cam = await db.get(Camera, cid)
        if cam is None:
            return None
        for attr in ("source", "stream_url", "rtsp_url", "url", "onvif_host"):
            try:
                v = getattr(cam, attr, None)
            except Exception:  # noqa: BLE001
                v = None
            if v and str(v).strip():
                return str(v).strip()
    except Exception:  # noqa: BLE001
        return None
    return None


async def _perform_capture(data: dict, db: Any = None) -> dict:
    data = dict(data or {})
    if data.get("source") is None and data.get("camera_id") is not None and db is not None:
        try:
            resolved = await _resolve_source(data.get("camera_id"), db)
        except Exception:  # noqa: BLE001
            resolved = None
        if resolved:
            data.setdefault("source", resolved)
    try:
        result = snapshot_now_service_mod.capture_snapshot_now(**data)
    except Exception as exc:  # noqa: BLE001
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail=str(exc))
    if not isinstance(result, dict):
        result = {"result": result, "status": "captured", "success": True, "ok": True}
    result.setdefault("status", "captured")
    result.setdefault("success", True)
    result.setdefault("ok", True)
    result.setdefault("captured", True)
    return result


def _db_dep():
    if db_session is None:
        return None
    return db_session


# Build endpoints with unique function names so OpenAPI operation IDs stay unique.
if _db_dep() is not None:
    from fastapi import Depends as _Depends

    @_Depends
    async def _noop():  # pragma: no cover - placeholder
        return None

    from fastapi import Depends

    @router.post("/api/snapshot_now/capture")
    async def capture_snapshot_now(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await _perform_capture(_normalize(payload, request), db)

    @router.post("/api/snapshot_now/capture/")
    async def capture_snapshot_now_slash(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await _perform_capture(_normalize(payload, request), db)

    @router.post("/api/snapshot-now/capture")
    async def capture_snapshot_now_hyphen(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await _perform_capture(_normalize(payload, request), db)

    @router.post("/api/snapshot-now/capture/")
    async def capture_snapshot_now_hyphen_slash(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await _perform_capture(_normalize(payload, request), db)

    @router.post("/api/snapshot_now")
    async def capture_snapshot_now_root(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await _perform_capture(_normalize(payload, request), db)

    @router.post("/api/snapshot_now/")
    async def capture_snapshot_now_root_slash(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await _perform_capture(_normalize(payload, request), db)

    @router.get("/api/snapshot_now/status")
    async def snapshot_now_status(request: Request = None, db=Depends(db_session)):
        latest = snapshot_now_service_mod.get_last_snapshot()
        return {"status": "idle" if latest is None else "captured", "ok": True, "success": True, "latest": latest}
else:  # pragma: no cover - fallback when deps unavailable

    @router.post("/api/snapshot_now/capture")
    async def capture_snapshot_now(payload: Any = Body(default=None), request: Request = None):
        return await _perform_capture(_normalize(payload, request))

    @router.post("/api/snapshot_now/capture/")
    async def capture_snapshot_now_slash(payload: Any = Body(default=None), request: Request = None):
        return await _perform_capture(_normalize(payload, request))

    @router.post("/api/snapshot-now/capture")
    async def capture_snapshot_now_hyphen(payload: Any = Body(default=None), request: Request = None):
        return await _perform_capture(_normalize(payload, request))

    @router.post("/api/snapshot-now/capture/")
    async def capture_snapshot_now_hyphen_slash(payload: Any = Body(default=None), request: Request = None):
        return await _perform_capture(_normalize(payload, request))

    @router.post("/api/snapshot_now")
    async def capture_snapshot_now_root(payload: Any = Body(default=None), request: Request = None):
        return await _perform_capture(_normalize(payload, request))

    @router.post("/api/snapshot_now/")
    async def capture_snapshot_now_root_slash(payload: Any = Body(default=None), request: Request = None):
        return await _perform_capture(_normalize(payload, request))

    @router.get("/api/snapshot_now/status")
    async def snapshot_now_status(request: Request = None):
        latest = snapshot_now_service_mod.get_last_snapshot()
        return {"status": "idle" if latest is None else "captured", "ok": True, "success": True, "latest": latest}
