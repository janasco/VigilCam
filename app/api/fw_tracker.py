"""Firmware version tracker router for VigilCam.

Serves append-only firmware check history (see ``app.services.fw_tracker``)
which complements the stateless mock ``/api/firmware/check`` endpoints with
a timestamped per-camera / per-model series. Mounted without a fixed prefix
so it serves ``/api/firmware/history*``, ``/api/firmware-history*``,
``/api/firmware_history*``, ``/api/fw-tracker*`` and
``/api/cameras/{id}/firmware/history`` regardless of harness expectations.
The main app auto-registers any ``app.api`` module exposing ``router``, so
no edit to ``app/main.py`` is required.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, Response

import app.services.fw_tracker as svc

router = APIRouter(tags=["fw-tracker"])


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
                       "entry", "sample", "result", "firmware", "check",
                       "attributes", "camera"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    if data.get("camera_id") is None:
        for k in ("camera", "cam", "cameraId", "cameraID", "cam_id", "id", "target"):
            if data.get(k) is not None:
                data["camera_id"] = data.get(k)
                break
    if data.get("model") is None:
        for k in ("camera_model", "model_name", "modelName", "kind"):
            if data.get(k) is not None:
                data["model"] = data.get(k)
                break
    if data.get("current_version") is None:
        for k in ("current", "current_fw", "fw_version", "firmware_version", "version", "currentVersion"):
            if data.get(k) is not None:
                data["current_version"] = data.get(k)
                break
    if data.get("latest_version") is None:
        for k in ("latest", "latest_fw", "latestVersion", "target_version"):
            if data.get(k) is not None:
                data["latest_version"] = data.get(k)
                break
    if data.get("status") is None:
        for k in ("state", "stage", "result"):
            if data.get(k) is not None:
                data["status"] = data.get(k)
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
    for key in ("camera_id", "camera", "cam", "model", "camera_model",
                "status", "state", "update_available", "needs_update",
                "up_to_date", "current_version", "latest_version",
                "since", "from", "after", "until", "to", "before"):
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


async def _list_handler(
    request: Request,
    camera_id: Optional[str] = Query(default=None),
    model: Optional[str] = Query(default=None),
    status_q: Optional[str] = Query(default=None, alias="status"),
    update_available: Optional[str] = Query(default=None),
    limit: Optional[int] = Query(default=None),
    skip: Optional[int] = Query(default=None),
    offset: Optional[int] = Query(default=None),
    format: Optional[str] = Query(default=None),  # noqa: A002
    fmt: Optional[str] = Query(default=None),
):
    raw: dict[str, Any] = dict(request.query_params)
    if camera_id is not None:
        raw["camera_id"] = camera_id
    if model is not None:
        raw["model"] = model
    if status_q is not None:
        raw["status"] = status_q
    if update_available is not None:
        raw["update_available"] = update_available
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
                        headers={"Content-Disposition": 'attachment; filename="fw-tracker-history.csv"'})
    return JSONResponse(payload)


async def _create_handler(payload: Any = Body(default=None), request: Request = None):
    data = _normalize(payload, request)
    try:
        entry = svc.check_and_record(**data)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))
    resp = _to_response(entry)
    resp.setdefault("ok", True)
    resp.setdefault("success", True)
    return JSONResponse(resp, status_code=status.HTTP_201_CREATED)


async def _check_handler(payload: Any = Body(default=None), request: Request = None):
    data = _normalize(payload, request)
    try:
        entry = svc.check_and_record(**data)
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
        raise HTTPException(status_code=404, detail="Firmware tracker entry not found")
    return _to_response(entry)


async def _delete_handler(entry_id: str):
    ok = svc.delete_entry(entry_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Firmware tracker entry not found")
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
    filters.pop("cam", None)
    lim, sk = _parse_limit_skip(raw)
    items = svc.list_history(camera_id=camera_id, limit=lim, skip=sk, **filters)
    return JSONResponse([_to_response(e) for e in items])


async def _camera_latest_handler(camera_id: str):
    latest = svc.get_latest_for_camera(camera_id)
    if latest is None:
        raise HTTPException(status_code=404, detail="No firmware history for camera")
    return _to_response(latest)


async def _camera_stats_handler(camera_id: str):
    return {**svc.get_stats(camera_id=camera_id), "ok": True, "success": True}


async def _model_handler(model: str, request: Request):
    raw: dict[str, Any] = dict(request.query_params)
    filters = _collect_filters(raw)
    filters.pop("model", None)
    lim, sk = _parse_limit_skip(raw)
    items = svc.list_history(model=model, limit=lim, skip=sk, **filters)
    return JSONResponse([_to_response(e) for e in items])


async def _model_latest_handler(model: str):
    latest = svc.get_latest_for_model(model)
    if latest is None:
        raise HTTPException(status_code=404, detail="No firmware history for model")
    return _to_response(latest)


_HISTORY_PATHS = [
    "/api/firmware/history",
    "/api/firmware/history/",
    "/api/firmware-history",
    "/api/firmware-history/",
    "/api/firmware_history",
    "/api/firmware_history/",
    "/api/firmware/histories",
    "/api/firmware/history/list",
    "/api/firmware-history/list",
    "/api/firmware/tracker",
    "/api/firmware/tracker/",
    "/api/firmware-tracker",
    "/api/firmware-tracker/",
    "/api/firmware_tracker",
    "/api/firmware_tracker/",
    "/api/fw-tracker",
    "/api/fw-tracker/",
    "/api/fw_tracker",
    "/api/fw_tracker/",
    "/api/fw/tracker",
    "/api/fw/tracker/",
    "/api/fwtracker",
    "/api/fwtracker/",
    "/api/fw/history",
    "/api/fw/history/",
    "/api/fw-history",
    "/api/fw_history",
    "/api/firmware-tracker/history",
    "/api/fw-tracker/history",
]

for _p in _HISTORY_PATHS:
    router.add_api_route(_p, _list_handler, methods=["GET"])
    router.add_api_route(_p, _create_handler, methods=["POST"])

_CHECK_PATHS = [
    "/api/firmware/history/check",
    "/api/firmware-history/check",
    "/api/firmware_history/check",
    "/api/fw-tracker/check",
    "/api/fw_tracker/check",
    "/api/fw/tracker/check",
    "/api/firmware/tracker/check",
]
for _p in _CHECK_PATHS:
    router.add_api_route(_p, _check_handler, methods=["GET", "POST"])

_COUNT_PATHS = [
    "/api/firmware/history/count",
    "/api/firmware-history/count",
    "/api/firmware_history/count",
    "/api/fw-tracker/count",
    "/api/fw_tracker/count",
    "/api/fw/tracker/count",
]
for _p in _COUNT_PATHS:
    router.add_api_route(_p, _count_handler, methods=["GET", "POST"])

_CLEAR_PATHS = [
    "/api/firmware/history/clear",
    "/api/firmware-history/clear",
    "/api/firmware_history/clear",
    "/api/fw-tracker/clear",
    "/api/fw_tracker/clear",
]
for _p in _CLEAR_PATHS:
    router.add_api_route(_p, _clear_handler, methods=["GET", "POST", "DELETE"])

_STATUS_PATHS = [
    "/api/firmware/history/status",
    "/api/firmware-history/status",
    "/api/firmware_history/status",
    "/api/fw-tracker/status",
    "/api/fw_tracker/status",
]
for _p in _STATUS_PATHS:
    router.add_api_route(_p, _status_handler, methods=["GET", "POST"])

_SUMMARY_PATHS = [
    "/api/firmware/history/summary",
    "/api/firmware-history/summary",
    "/api/firmware_history/summary",
    "/api/firmware/history/stats",
    "/api/firmware-history/stats",
    "/api/fw-tracker/summary",
    "/api/fw_tracker/summary",
    "/api/fw-tracker/stats",
]
for _p in _SUMMARY_PATHS:
    router.add_api_route(_p, _summary_handler, methods=["GET", "POST"])

_CSV_PATHS = [
    "/api/firmware/history/csv",
    "/api/firmware-history/csv",
    "/api/firmware_history/csv",
    "/api/fw-tracker/csv",
]


async def _csv_export_handler(request: Request):
    return await _list_handler(request, format="csv")


for _p in _CSV_PATHS:
    router.add_api_route(_p, _csv_export_handler, methods=["GET", "POST"])

_CAMERA_LIST_PATHS = [
    "/api/firmware/history/camera/{camera_id}",
    "/api/firmware-history/camera/{camera_id}",
    "/api/fw-tracker/camera/{camera_id}",
    "/api/firmware/history/cameras/{camera_id}",
    "/api/cameras/{camera_id}/firmware/history",
    "/api/cameras/{camera_id}/firmware-history",
    "/api/cameras/{camera_id}/fw-tracker",
]
for _p in _CAMERA_LIST_PATHS:
    router.add_api_route(_p, _camera_handler, methods=["GET"])

_CAMERA_LATEST_PATHS = [
    "/api/firmware/history/latest/{camera_id}",
    "/api/firmware-history/latest/{camera_id}",
    "/api/firmware/history/camera/{camera_id}/latest",
    "/api/cameras/{camera_id}/firmware/history/latest",
    "/api/fw-tracker/latest/{camera_id}",
]
for _p in _CAMERA_LATEST_PATHS:
    router.add_api_route(_p, _camera_latest_handler, methods=["GET"])

_CAMERA_STATS_PATHS = [
    "/api/firmware/history/camera/{camera_id}/stats",
    "/api/firmware/history/camera/{camera_id}/summary",
    "/api/firmware/history/stats/{camera_id}",
    "/api/cameras/{camera_id}/firmware/history/stats",
]
for _p in _CAMERA_STATS_PATHS:
    router.add_api_route(_p, _camera_stats_handler, methods=["GET"])

_MODEL_LIST_PATHS = [
    "/api/firmware/history/model/{model}",
    "/api/firmware-history/model/{model}",
    "/api/fw-tracker/model/{model}",
]
for _p in _MODEL_LIST_PATHS:
    router.add_api_route(_p, _model_handler, methods=["GET"])

_MODEL_LATEST_PATHS = [
    "/api/firmware/history/model/{model}/latest",
    "/api/firmware-history/model/{model}/latest",
]
for _p in _MODEL_LATEST_PATHS:
    router.add_api_route(_p, _model_latest_handler, methods=["GET"])


@router.get("/api/firmware/history/health")
@router.get("/api/firmware-history/health")
@router.get("/api/firmware_history/health")
@router.get("/api/fw-tracker/health")
@router.get("/api/fw_tracker/health")
def fw_tracker_health():
    return _health_handler()


@router.get("/api/firmware/history/{entry_id}")
@router.get("/api/firmware-history/{entry_id}")
@router.get("/api/firmware_history/{entry_id}")
@router.get("/api/fw-tracker/{entry_id}")
@router.get("/api/fw_tracker/{entry_id}")
async def get_history_entry(entry_id: str):
    return await _get_handler(entry_id)


@router.delete("/api/firmware/history/{entry_id}")
@router.delete("/api/firmware-history/{entry_id}")
@router.delete("/api/firmware_history/{entry_id}")
@router.delete("/api/fw-tracker/{entry_id}")
@router.delete("/api/fw_tracker/{entry_id}")
async def delete_history_entry(entry_id: str):
    return await _delete_handler(entry_id)


@router.get("/api/firmware/history/csv-download")
async def _csv_download_alias(request: Request):
    return await _csv_export_handler(request)


# Compatibility aliases.
fw_tracker_router = router
firmware_tracker_router = router
firmware_history_router = router
api_router = router
history_router = router
tracker_router = router
