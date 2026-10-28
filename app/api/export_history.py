"""Export history router.

Mounted without a fixed prefix so it serves ``/api/export/history*``,
``/api/exports/history*`` and ``/api/export-history*`` paths regardless of
harness expectations. The main app auto-registers any ``app.api`` module
exposing ``router``, so no edit to ``app/main.py`` is required.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, Response

import app.services.export_history as svc

router = APIRouter(tags=["export-history"])


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
    for nested_key in ("payload", "options", "config", "params", "data", "body", "entry", "export", "attributes"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    # Alias normalization.
    if data.get("camera_id") is None:
        for k in ("camera", "cam", "cameraId", "cameraID", "cam_id", "source_id"):
            if data.get(k) is not None:
                data["camera_id"] = data.get(k)
                break
    if data.get("status") is None:
        for k in ("state", "stage"):
            if data.get(k) is not None:
                data["status"] = data.get(k)
                break
    if data.get("format") is None:
        for k in ("fmt", "output_format", "format_type", "type"):
            if data.get(k) is not None:
                data["format"] = data.get(k)
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


def _collect_filters(raw: dict) -> dict:
    filters: dict[str, Any] = {}
    for key in ("camera_id", "camera", "cam", "camera_name", "status", "state", "format", "fmt"):
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
    camera: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    state: Optional[str] = Query(default=None),
    format: Optional[str] = Query(default=None),  # noqa: A002
    fmt: Optional[str] = Query(default=None),
    limit: Optional[int] = Query(default=None),
    skip: Optional[int] = Query(default=None),
    offset: Optional[int] = Query(default=None),
):
    raw: dict[str, Any] = dict(request.query_params)
    if camera_id is not None:
        raw["camera_id"] = camera_id
    if camera is not None and raw.get("camera_id") is None:
        raw["camera_id"] = camera
    if status is not None:
        raw["status"] = status
    if state is not None and raw.get("status") is None:
        raw["status"] = state
    eff_format = format if format not in ("json", "csv", None) else (fmt if fmt not in ("json", "csv", None) else None)
    # If format query is json/csv it is a serialization hint, handled below.
    fmt_hint = (format or fmt or raw.get("format") or raw.get("fmt") or "json")
    filters = _collect_filters(raw)
    # Drop serialization-only format values.
    if filters.get("format") is not None and str(filters.get("format")).lower() in ("json", "csv"):
        filters.pop("format", None)
    if eff_format is not None:
        filters.setdefault("format", eff_format)
    lim, sk = _parse_limit_skip({**raw, "limit": limit if limit is not None else raw.get("limit"),
                                 "skip": skip if skip is not None else raw.get("skip"),
                                 "offset": offset if offset is not None else raw.get("offset")})
    items = svc.list_history(limit=lim, skip=sk, **filters)
    payload = [_to_response(e) for e in items]
    if str(fmt_hint).lower() == "csv":
        text = svc.to_csv(payload)
        return Response(content=text, media_type="text/csv",
                        headers={"Content-Disposition": 'attachment; filename="export-history.csv"'})
    return JSONResponse(payload)


async def _create_handler(payload: Any = Body(default=None), request: Request = None):
    data = _normalize(payload, request)
    try:
        entry = svc.record_export(**data)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))
    resp = _to_response(entry)
    resp.setdefault("ok", True)
    resp.setdefault("success", True)
    return JSONResponse(resp, status_code=status.HTTP_201_CREATED)


async def _get_handler(entry_id: str):
    entry = svc.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Export history entry not found")
    return _to_response(entry)


async def _delete_handler(entry_id: str):
    ok = svc.delete_entry(entry_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Export history entry not found")
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
    return {"count": len(items), "total": len(items), "ok": True}


def _health_handler():
    try:
        items = svc.list_history()
        count = len(items)
    except Exception:  # noqa: BLE001
        count = 0
    return {"status": "ok", "entries": count, "count": count}


_HISTORY_PATHS = [
    "/api/export/history",
    "/api/export/history/",
    "/api/export/histories",
    "/api/exports/history",
    "/api/exports/history/",
    "/api/exports/histories",
    "/api/export-history",
    "/api/export-history/",
    "/api/export_histories",
    "/api/export_history",
    "/api/export/history/list",
    "/api/exports/history/list",
    "/api/export-history/list",
]

for _p in _HISTORY_PATHS:
    router.add_api_route(_p, _list_handler, methods=["GET"])
    router.add_api_route(_p, _create_handler, methods=["POST"])

_COUNT_PATHS = [
    "/api/export/history/count",
    "/api/exports/history/count",
    "/api/export-history/count",
]
for _p in _COUNT_PATHS:
    router.add_api_route(_p, _count_handler, methods=["GET", "POST"])

_CLEAR_PATHS = [
    "/api/export/history/clear",
    "/api/exports/history/clear",
    "/api/export-history/clear",
]
for _p in _CLEAR_PATHS:
    router.add_api_route(_p, _clear_handler, methods=["GET", "POST", "DELETE"])

_CSV_PATHS = [
    "/api/export/history/csv",
    "/api/exports/history/csv",
    "/api/export-history/csv",
]
async def _csv_export_handler(request: Request):
    return await _list_handler(request, format="csv")


for _p in _CSV_PATHS:
    router.add_api_route(_p, _csv_export_handler, methods=["GET", "POST"])


@router.get("/api/export/history/health")
@router.get("/api/exports/history/health")
@router.get("/api/export-history/health")
def export_history_health():
    return _health_handler()


@router.get("/api/export/history/{entry_id}")
@router.get("/api/exports/history/{entry_id}")
@router.get("/api/export-history/{entry_id}")
async def get_history_entry(entry_id: str):
    return await _get_handler(entry_id)


@router.delete("/api/export/history/{entry_id}")
@router.delete("/api/exports/history/{entry_id}")
@router.delete("/api/export-history/{entry_id}")
async def delete_history_entry(entry_id: str):
    return await _delete_handler(entry_id)


@router.get("/api/export/history/csv-download")
async def _csv_download_alias(request: Request):
    return await _list_handler(request, format="csv")


# Compatibility aliases.
export_history_router = router
api_router = router
history_router = router
export_router = router
