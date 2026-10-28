"""Stream snapshot fallback API for VigilCam.

Auto-registered by ``app.main`` (any module in ``app.api`` exposing
``router`` is included), so no ``app/main.py`` edit is needed.
Both ``/api/snapshot-fallback`` (canonical) and ``/api/snapshot_fallback``
(underscore alias) are served.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query

import app.services.snapshot_fallback as svc

router = APIRouter(prefix="/api/snapshot-fallback", tags=["snapshot-fallback"])
underscore_router = APIRouter(prefix="/api/snapshot_fallback", tags=["snapshot-fallback"])


def _payload_to_dict(payload: Any) -> dict:
    if payload is None:
        return {}
    if hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
            return dict(data) if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            return {}
    if isinstance(payload, dict):
        return dict(payload)
    return {}


def _timeout_from(data: dict, default: float = 15.0) -> float:
    for key in ("timeout", "timeout_seconds", "timeout_ms"):
        if data.get(key) is not None:
            try:
                val = float(data[key])
                if key == "timeout_ms":
                    val = val / 1000.0
                return val
            except (TypeError, ValueError):
                continue
    return default


def _do_capture(data: dict) -> dict:
    data = dict(data or {})
    # Normalize camera id aliases.
    camera_id = None
    for key in ("camera_id", "camera", "cam", "id"):
        if data.get(key) is not None:
            camera_id = data.get(key)
            break
    if camera_id is not None:
        data["camera_id"] = camera_id
    # Normalize source aliases into primary/fallback.
    if data.get("primary_source") is None:
        for key in ("source", "source_url", "stream_url", "url", "uri", "stream", "primary", "primary_url"):
            if data.get(key) is not None:
                data["primary_source"] = data.get(key)
                break
    if data.get("fallback_source") is None:
        for key in ("fallback", "fallback_url", "secondary_source", "secondary_url", "backup_source", "alternate_source"):
            if data.get(key) is not None:
                data["fallback_source"] = data.get(key)
                break
    data.setdefault("timeout", _timeout_from(data))
    try:
        result = svc.capture_with_fallback(**data)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))
    out = result.to_dict()
    out.setdefault("ok", True)
    out.setdefault("success", True)
    return out


def _summary_payload() -> dict:
    return {
        **svc.get_summary(),
        "results": svc.list_results_dicts(),
        "ok": True,
        "success": True,
    }


@router.get("/status")
@underscore_router.get("/status")
async def get_status():
    return _summary_payload()


@router.get("/summary")
@underscore_router.get("/summary")
async def get_summary():
    return {**svc.get_summary(), "ok": True, "success": True}


@router.get("/health")
@underscore_router.get("/health")
async def get_health():
    return _summary_payload()


@router.get("/results")
@underscore_router.get("/results")
async def list_results():
    return svc.list_results_dicts()


@router.delete("/results")
@underscore_router.delete("/results")
async def clear_results():
    svc.clear()
    return {"ok": True, "success": True, "cleared": True, **svc.get_summary()}


@router.get("/capture")
@underscore_router.get("/capture")
async def capture_via_query(
    camera_id: Optional[int] = Query(default=None),
    source: Optional[str] = Query(default=None),
    primary_source: Optional[str] = Query(default=None),
    fallback_source: Optional[str] = Query(default=None),
    timeout: Optional[float] = Query(default=None),
):
    data = {
        "camera_id": camera_id,
        "primary_source": primary_source or source,
        "fallback_source": fallback_source,
        "timeout": timeout or 15.0,
    }
    return _do_capture({k: v for k, v in data.items() if v is not None})


@router.post("/capture")
@underscore_router.post("/capture")
async def capture_snapshot(payload: Any = Body(default=None)):
    return _do_capture(_payload_to_dict(payload))


@router.post("/camera/{camera_id}/capture")
@underscore_router.post("/camera/{camera_id}/capture")
async def capture_camera_post(camera_id: int, payload: Any = Body(default=None)):
    data = _payload_to_dict(payload)
    data.setdefault("camera_id", camera_id)
    out = _do_capture(data)
    out["camera_id"] = camera_id
    out["id"] = camera_id
    return out


@router.get("/camera/{camera_id}")
@underscore_router.get("/camera/{camera_id}")
async def get_camera_status(camera_id: int):
    st = svc.get_status(f"camera:{camera_id}")
    if st is None:
        st = svc.get_status(camera_id)
    if st is None:
        raise HTTPException(status_code=404, detail="No fallback snapshot for camera")
    out = st.to_dict()
    out["camera_id"] = camera_id
    out["id"] = camera_id
    return out


@router.get("/{target}")
@underscore_router.get("/{target}")
async def get_fallback_target(target: str):
    st = svc.get_status(target)
    if st is None:
        try:
            st = svc.get_status(f"camera:{int(target)}")
        except (TypeError, ValueError):
            st = None
    if st is None:
        raise HTTPException(status_code=404, detail="No fallback snapshot for target")
    return st.to_dict()


# Side-effect registration so the routers are live even when this module is
# imported after app startup (mirrors stream-probe/watchdog pattern).
try:
    from app.main import app as _main_app

    _ids = getattr(_main_app, "_registered_router_ids", set())
    for _r in (router, underscore_router):
        if id(_r) not in _ids:
            _main_app.include_router(_r)
            try:
                _main_app._registered_router_ids = getattr(_main_app, "_registered_router_ids", set()) | {id(_r)}
                _ids = _main_app._registered_router_ids
            except Exception:  # noqa: BLE001
                pass
except Exception:  # noqa: BLE001
    pass
