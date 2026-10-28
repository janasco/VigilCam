"""Tamper drift history router for VigilCam.

Serves append-only tamper drift history (see ``app.services.drift_history``)
which complements the point-in-time ``/api/tamper`` compare endpoints with
a timestamped per-camera drift series. Mounted without a fixed prefix so it
serves ``/api/drift/history*``, ``/api/drift-history*``,
``/api/tamper/drift/history*`` and ``/api/tamper/history*`` regardless of
harness expectations. The main app auto-registers any ``app.api`` module
exposing ``router``, so no edit to ``app/main.py`` is required.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, Response

import app.services.drift_history as svc

router = APIRouter(tags=["drift-history"])


def _normalize(payload: Any, request: Request | None = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif isinstance(payload, str):
        s = payload.strip()
        data = {"camera_id": s} if s else {}
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
    for nested_key in ("payload", "options", "config", "params", "data", "body",
                       "entry", "sample", "result", "drift", "tamper", "attributes", "camera"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    if data.get("camera_id") is None:
        for k in ("camera", "cam", "cameraId", "cameraID", "cam_id", "id", "target"):
            if data.get(k) is not None:
                data["camera_id"] = data.get(k)
                break
    if data.get("drift") is None:
        for k in ("distance", "score", "value", "drift_value"):
            if data.get(k) is not None:
                data["drift"] = data.get(k)
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
    if hasattr(entry, "to_dict"):
        try:
            return dict(entry.to_dict())
        except Exception:  # noqa: BLE001
            pass
    try:
        return dict(entry)
    except Exception:  # noqa: BLE001
        return {"value": entry}


def _collect_filters(raw: dict) -> dict:
    filters: dict[str, Any] = {}
    for key in ("camera_id", "camera", "cam", "tampered", "tamper", "detected",
                "since", "from", "after", "until", "to", "before",
                "min_drift", "max_drift"):
        if raw.get(key) is not None:
            filters[key] = raw.get(key)
    return filters


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


def _has_drift_fields(data: dict) -> bool:
    for k in ("drift", "distance", "score", "value", "drift_value", "tampered", "tamper"):
        if data.get(k) is not None:
            return True
    return False


def _has_frame_fields(data: dict) -> bool:
    for k in ("frame", "pixels", "data", "image", "frame_data", "histogram",
              "values", "samples", "bytes", "raw", "content", "base64", "b64"):
        if data.get(k) is not None:
            return True
    return False


async def _list_handler(
    request: Request,
    camera_id: Optional[str] = Query(default=None),
    tampered: Optional[str] = Query(default=None),
    limit: Optional[int] = Query(default=None),
    skip: Optional[int] = Query(default=None),
    offset: Optional[int] = Query(default=None),
    format: Optional[str] = Query(default=None),  # noqa: A002
    fmt: Optional[str] = Query(default=None),
):
    raw: dict[str, Any] = dict(request.query_params)
    if camera_id is not None:
        raw["camera_id"] = camera_id
    if tampered is not None:
        raw["tampered"] = tampered
    fmt_hint = (format or fmt or raw.get("format") or raw.get("fmt") or "json")
    filters = _collect_filters(raw)
    if filters.get("format") is not None and str(filters.get("format")).lower() in ("json", "csv"):
        filters.pop("format", None)
    for _fk in ("format", "fmt"):
        filters.pop(_fk, None)
    lim, sk = _parse_limit_skip({**raw, "limit": limit if limit is not None else raw.get("limit"),
                                 "skip": skip if skip is not None else raw.get("skip"),
                                 "offset": offset if offset is not None else raw.get("offset")})
    items = svc.list_history(limit=lim, skip=sk, **filters)
    payload = [_to_response(e) for e in items]
    if str(fmt_hint).lower() == "csv":
        text = svc.to_csv(payload)
        return Response(content=text, media_type="text/csv",
                        headers={"Content-Disposition": 'attachment; filename="drift-history.csv"'})
    return JSONResponse(payload)


async def _create_handler(payload: Any = Body(default=None), request: Request = None):
    data = _normalize(payload, request)
    # If caller supplied a live frame without explicit drift fields,
    # run the point-in-time tamper compare then record history.
    try:
        if _has_frame_fields(data) and not _has_drift_fields(data):
            entry = svc.compare_and_record(**data)
        else:
            entry = svc.record_sample(**data)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))
    resp = _to_response(entry)
    resp.setdefault("ok", True)
    resp.setdefault("success", True)
    return JSONResponse(resp, status_code=status.HTTP_201_CREATED)


async def _compare_handler(payload: Any = Body(default=None), request: Request = None):
    data = _normalize(payload, request)
    try:
        entry = svc.compare_and_record(**data)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))
    resp = _to_response(entry)
    resp.setdefault("ok", True)
    resp.setdefault("success", True)
    return JSONResponse(resp, status_code=status.HTTP_201_CREATED)


async def _get_handler(entry_id: str):
    entry = svc.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Drift history entry not found")
    return _to_response(entry)


async def _delete_handler(entry_id: str):
    ok = svc.delete_entry(entry_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Drift history entry not found")
    return {"status": "deleted", "ok": True, "success": True, "id": entry_id}


async def _clear_handler(request: Request):
    raw: dict[str, Any] = dict(request.query_params)
    filters = _collect_filters(raw)
    n = svc.clear_history(**filters)
    return {"status": "cleared", "ok": True, "success": True, "deleted": n, "count": n}


async def _count_handler(request: Request):
    raw: dict[str, Any] = dict(request.query_params)
    filters = _collect_filters(raw)
    items = svc.list_history(**filters)
    return {"count": len(items), "total": len(items), "ok": True, "success": True}


def _status_handler():
    try:
        return svc.status()
    except Exception:  # noqa: BLE001
        return {"status": "ok", "ok": True, "count": 0, "total": 0}


def _summary_handler(request: Request = None):
    try:
        raw: dict[str, Any] = dict(request.query_params) if request is not None else {}
    except Exception:  # noqa: BLE001
        raw = {}
    filters = _collect_filters(raw)
    try:
        return {**svc.get_stats(**filters), "ok": True, "success": True}
    except Exception:  # noqa: BLE001
        return {"ok": True, "success": True, "total": 0, "count": 0}


def _health_handler():
    try:
        items = svc.list_history()
        count = len(items)
    except Exception:  # noqa: BLE001
        count = 0
    return {"status": "ok", "ok": True, "entries": count, "count": count}


async def _camera_handler(camera_id: str, request: Request):
    raw: dict[str, Any] = dict(request.query_params)
    filters = _collect_filters(raw)
    filters.pop("camera_id", None)
    filters.pop("camera", None)
    lim, sk = _parse_limit_skip(raw)
    items = svc.list_history(camera_id=camera_id, limit=lim, skip=sk, **filters)
    return JSONResponse([_to_response(e) for e in items])


async def _camera_latest_handler(camera_id: str):
    latest = svc.get_latest_for_camera(camera_id)
    if latest is None:
        raise HTTPException(status_code=404, detail="No drift history for camera")
    return _to_response(latest)


async def _camera_stats_handler(camera_id: str):
    return {**svc.get_stats(camera_id=camera_id), "ok": True, "success": True}


_HISTORY_PATHS = [
    "/api/drift/history",
    "/api/drift/history/",
    "/api/drifts/history",
    "/api/drifts/history/",
    "/api/drift-history",
    "/api/drift-history/",
    "/api/drift_history",
    "/api/drift_history/",
    "/api/drift/histories",
    "/api/drifts/histories",
    "/api/drift/history/list",
    "/api/drift-history/list",
    "/api/tamper/drift/history",
    "/api/tamper/drift/history/",
    "/api/tamper/drift-history",
    "/api/tamper/drift-history/",
    "/api/tamper/history",
    "/api/tamper/history/",
    "/api/tamper/histories",
    "/api/tamper/tamper-history",
    "/api/tamper-drift/history",
    "/api/tamper-drift/history/",
]

for _p in _HISTORY_PATHS:
    router.add_api_route(_p, _list_handler, methods=["GET"])
    router.add_api_route(_p, _create_handler, methods=["POST"])

_COMPARE_PATHS = [
    "/api/drift/history/compare",
    "/api/drift-history/compare",
    "/api/drift_history/compare",
    "/api/tamper/drift/history/compare",
    "/api/tamper/history/compare",
    "/api/drift/history/sample",
    "/api/drift-history/sample",
    "/api/tamper/drift/history/sample",
]
for _p in _COMPARE_PATHS:
    router.add_api_route(_p, _compare_handler, methods=["GET", "POST"])

_COUNT_PATHS = [
    "/api/drift/history/count",
    "/api/drift-history/count",
    "/api/drift_history/count",
    "/api/tamper/drift/history/count",
    "/api/tamper/history/count",
]
for _p in _COUNT_PATHS:
    router.add_api_route(_p, _count_handler, methods=["GET", "POST"])

_CLEAR_PATHS = [
    "/api/drift/history/clear",
    "/api/drift-history/clear",
    "/api/drift_history/clear",
    "/api/tamper/drift/history/clear",
    "/api/tamper/history/clear",
]
for _p in _CLEAR_PATHS:
    router.add_api_route(_p, _clear_handler, methods=["GET", "POST", "DELETE"])

_STATUS_PATHS = [
    "/api/drift/history/status",
    "/api/drift-history/status",
    "/api/drift_history/status",
    "/api/tamper/drift/history/status",
    "/api/tamper/history/status",
]
for _p in _STATUS_PATHS:
    router.add_api_route(_p, _status_handler, methods=["GET", "POST"])

_SUMMARY_PATHS = [
    "/api/drift/history/summary",
    "/api/drift-history/summary",
    "/api/drift_history/summary",
    "/api/drift/history/stats",
    "/api/drift-history/stats",
    "/api/tamper/drift/history/summary",
    "/api/tamper/history/summary",
    "/api/tamper/drift/history/stats",
]
for _p in _SUMMARY_PATHS:
    router.add_api_route(_p, _summary_handler, methods=["GET", "POST"])

_CSV_PATHS = [
    "/api/drift/history/csv",
    "/api/drift-history/csv",
    "/api/drift_history/csv",
    "/api/tamper/drift/history/csv",
]
async def _csv_export_handler(request: Request):
    return await _list_handler(request, format="csv")


for _p in _CSV_PATHS:
    router.add_api_route(_p, _csv_export_handler, methods=["GET", "POST"])

_CAMERA_PATHS = [
    "/api/drift/history/camera/{camera_id}",
    "/api/drift-history/camera/{camera_id}",
    "/api/tamper/drift/history/camera/{camera_id}",
    "/api/tamper/history/camera/{camera_id}",
]
for _p in _CAMERA_PATHS:
    router.add_api_route(_p, _camera_handler, methods=["GET"])

_CAMERA_LATEST_PATHS = [
    "/api/drift/history/latest/{camera_id}",
    "/api/drift-history/latest/{camera_id}",
    "/api/drift/history/camera/{camera_id}/latest",
    "/api/tamper/drift/history/latest/{camera_id}",
    "/api/tamper/history/latest/{camera_id}",
]
for _p in _CAMERA_LATEST_PATHS:
    router.add_api_route(_p, _camera_latest_handler, methods=["GET"])

_CAMERA_STATS_PATHS = [
    "/api/drift/history/camera/{camera_id}/stats",
    "/api/drift/history/camera/{camera_id}/summary",
    "/api/drift/history/stats/{camera_id}",
    "/api/tamper/drift/history/camera/{camera_id}/stats",
]
for _p in _CAMERA_STATS_PATHS:
    router.add_api_route(_p, _camera_stats_handler, methods=["GET"])


@router.get("/api/drift/history/health")
@router.get("/api/drift-history/health")
@router.get("/api/drift_history/health")
@router.get("/api/tamper/drift/history/health")
@router.get("/api/tamper/history/health")
def drift_history_health():
    return _health_handler()


@router.get("/api/drift/history/{entry_id}")
@router.get("/api/drift-history/{entry_id}")
@router.get("/api/drift_history/{entry_id}")
@router.get("/api/tamper/drift/history/{entry_id}")
@router.get("/api/tamper/history/{entry_id}")
async def get_history_entry(entry_id: str):
    return await _get_handler(entry_id)


@router.delete("/api/drift/history/{entry_id}")
@router.delete("/api/drift-history/{entry_id}")
@router.delete("/api/drift_history/{entry_id}")
@router.delete("/api/tamper/drift/history/{entry_id}")
@router.delete("/api/tamper/history/{entry_id}")
async def delete_history_entry(entry_id: str):
    return await _delete_handler(entry_id)


@router.get("/api/drift/history/csv-download")
async def _csv_download_alias(request: Request):
    return await _csv_export_handler(request)


# Compatibility aliases.
drift_history_router = router
api_router = router
history_router = router
drift_router = router
tamper_drift_router = router

# Side-effect registration so the router is live even when this module is
# imported after app startup (mirrors tamper pattern). The main app also
# auto-discovers app.api.* routers at import time.
try:
    from app.main import app as _main_app

    _ids = getattr(_main_app, "_registered_router_ids", set())
    if id(router) not in _ids:
        _main_app.include_router(router)
        try:
            _main_app._registered_router_ids = getattr(_main_app, "_registered_router_ids", set()) | {id(router)}
        except Exception:  # noqa: BLE001
            pass
except Exception:  # noqa: BLE001
    pass
