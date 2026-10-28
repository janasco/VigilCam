"""Camera config history router.

Versioned snapshots of per-camera configuration with diffs. Distinct from
``/api/audit`` (flat action log): every snapshot stores the full normalized
config plus the field-level diff vs the previous version.

Auto-registered by ``app/main.py`` (pkgutil auto-loader), so no edits to
``app/main.py`` are required.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse

import app.services.config_history as svc

router = APIRouter(tags=["config-history"])


def _normalize(payload: Any, request: Request | None = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
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
    for nested_key in ("config", "camera_config", "settings", "snapshot", "data", "body", "entry", "payload"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    if data.get("camera_id") is None:
        for k in ("camera", "cam", "cameraId", "cam_id"):
            if data.get(k) is not None:
                data["camera_id"] = data.get(k)
                break
    return data


def _to_response(entry: Any) -> dict:
    if entry is None:
        return {}
    if isinstance(entry, dict):
        return dict(entry)
    if hasattr(entry, "model_dump"):
        try:
            return dict(entry.model_dump(mode="json"))
        except Exception:  # noqa: BLE001
            try:
                return dict(entry.model_dump())
            except Exception:  # noqa: BLE001
                pass
    try:
        return dict(entry)
    except Exception:  # noqa: BLE001
        return {"value": entry}


def _parse_limit_skip(raw: dict) -> tuple[Any, Any]:
    limit = raw.get("limit", raw.get("n", raw.get("count", raw.get("size", raw.get("max", raw.get("top"))))))
    skip = raw.get("skip", raw.get("offset", raw.get("start_index", 0)))
    try:
        limit = int(limit) if limit is not None else None
    except (TypeError, ValueError):
        limit = None
    try:
        skip = int(skip or 0)
    except (TypeError, ValueError):
        skip = 0
    return limit, skip


async def _snapshot_handler(camera_id: str, payload: Any = Body(default=None), request: Request = None):
    data = _normalize(payload, request)
    data["camera_id"] = camera_id
    try:
        entry = svc.snapshot_camera(**data)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))
    resp = _to_response(entry)
    resp.setdefault("ok", True)
    resp.setdefault("success", True)
    return JSONResponse(resp, status_code=status.HTTP_201_CREATED)


async def _list_handler(
    camera_id: str,
    request: Request,
    limit: Optional[int] = Query(default=None),
    skip: Optional[int] = Query(default=None),
    offset: Optional[int] = Query(default=None),
):
    raw: dict[str, Any] = dict(request.query_params)
    lim, sk = _parse_limit_skip({**raw, "limit": limit if limit is not None else raw.get("limit"),
                                 "skip": skip if skip is not None else raw.get("skip"),
                                 "offset": offset if offset is not None else raw.get("offset")})
    items = svc.list_history(camera_id=camera_id, limit=lim, skip=sk)
    return [_to_response(e) for e in items]


async def _latest_handler(camera_id: str):
    entry = svc.latest(camera_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="No config history for camera")
    return _to_response(entry)


async def _diff_pair_handler(first_id: str, second_id: str):
    try:
        return svc.diff_versions(first_id, second_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


_HISTORY_LIST_PATHS = [
    "/api/cameras/{camera_id}/config/history",
    "/api/cameras/{camera_id}/config/history/",
    "/api/cameras/{camera_id}/config-history",
    "/api/cameras/{camera_id}/config_history",
    "/api/camera/{camera_id}/config/history",
]

for _p in _HISTORY_LIST_PATHS:
    router.add_api_route(_p, _list_handler, methods=["GET"])
    router.add_api_route(_p, _snapshot_handler, methods=["POST"])

_LATEST_PATHS = [
    "/api/cameras/{camera_id}/config/history/latest",
    "/api/cameras/{camera_id}/config-history/latest",
    "/api/camera/{camera_id}/config/history/latest",
]

for _p in _LATEST_PATHS:
    router.add_api_route(_p, _latest_handler, methods=["GET"])


@router.get("/api/config/history/health")
@router.get("/api/config-health")
@router.get("/api/config-history/health")
def config_history_health():
    try:
        count = len(svc.list_history())
    except Exception:  # noqa: BLE001
        count = 0
    return {"status": "ok", "entries": count, "count": count}


@router.get("/api/config/history/{version_id}")
@router.get("/api/config-history/{version_id}")
@router.get("/api/camera-config/history/{version_id}")
async def get_config_version(version_id: str):
    entry = svc.get_version(version_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Config history version not found")
    return _to_response(entry)


@router.get("/api/config/history/{first_id}/diff/{second_id}")
@router.get("/api/config-history/{first_id}/diff/{second_id}")
async def diff_config_versions(first_id: str, second_id: str):
    return await _diff_pair_handler(first_id, second_id)


@router.get("/api/config/history/{version_id}/diff")
@router.get("/api/config-history/{version_id}/diff")
async def diff_config_version_against(version_id: str, against: Optional[str] = Query(default=None)):
    entry = svc.get_version(version_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Config history version not found")
    if against is not None:
        other = svc.get_version(against)
        if other is None:
            raise HTTPException(status_code=404, detail="Comparison version not found")
        return svc.diff_configs(other.get("config", {}), entry.get("config", {}))
    try:
        return svc.config_history_service.diff_against_previous(version_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.delete("/api/config/history/{version_id}")
@router.delete("/api/config-history/{version_id}")
async def delete_config_version(version_id: str):
    ok = svc.delete_version(version_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Config history version not found")
    return {"status": "deleted", "ok": True, "success": True, "id": version_id}


@router.get("/api/config/history")
@router.get("/api/config-history")
@router.get("/api/camera-config/history")
async def list_all_config_history(
    request: Request,
    camera_id: Optional[str] = Query(default=None),
    camera: Optional[str] = Query(default=None),
    limit: Optional[int] = Query(default=None),
    skip: Optional[int] = Query(default=None),
    offset: Optional[int] = Query(default=None),
):
    raw: dict[str, Any] = dict(request.query_params)
    cam = camera_id or camera or raw.get("camera_id") or raw.get("cam")
    lim, sk = _parse_limit_skip({**raw, "limit": limit if limit is not None else raw.get("limit"),
                                 "skip": skip if skip is not None else raw.get("skip"),
                                 "offset": offset if offset is not None else raw.get("offset")})
    items = svc.list_history(camera_id=cam, limit=lim, skip=sk)
    return [_to_response(e) for e in items]


# Compatibility aliases.
config_history_router = router
api_router = router
history_router = router
config_router = router
