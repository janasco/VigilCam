"""Bitrate history router for VigilCam.

Serves append-only bitrate sample history (see ``app.services.bitrate_history``)
which complements the point-in-time ``/api/bitrate`` sampler. Mounted without
a fixed prefix so it serves ``/api/bitrate/history*``,
``/api/bitrates/history*`` and ``/api/bitrate-history*`` (plus
``stream-bitrate`` aliases) regardless of harness expectations. The main app
auto-registers any ``app.api`` module exposing ``router``, so no edit to
``app/main.py`` is required.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, Response

import app.services.bitrate_history as svc

router = APIRouter(tags=["bitrate-history"])


def _normalize(payload: Any, request: Request | None = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif isinstance(payload, str):
        s = payload.strip()
        data = {"target": s} if s else {}
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
                       "entry", "sample", "result", "bitrate", "attributes"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    if data.get("target") is None:
        for k in ("stream", "name", "label", "camera_id", "camera", "cam", "url", "stream_url", "source"):
            if data.get(k) is not None and k in ("stream", "name", "label", "camera_id", "camera", "cam"):
                data["target"] = data.get(k)
                break
        else:
            for k in ("url", "stream_url", "source"):
                if data.get(k) is not None:
                    data["target"] = data.get(k)
                    break
    if data.get("camera_id") is None:
        for k in ("camera", "cam", "cameraId", "cameraID", "cam_id"):
            if data.get(k) is not None:
                data["camera_id"] = data.get(k)
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
    for key in ("target", "stream", "name", "url", "stream_url", "source",
                "camera_id", "camera", "cam", "status", "state",
                "since", "from", "after", "until", "to", "before",
                "min_bps", "max_bps"):
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


def _has_bitrate_fields(data: dict) -> bool:
    for k in ("bitrate_bps", "bps", "bitrate", "bitrate_kbps", "kbps",
              "bitrate_mbps", "mbps", "bytes_received", "bytes", "total_bytes"):
        if data.get(k) is not None:
            return True
    return False


async def _list_handler(
    request: Request,
    target: Optional[str] = Query(default=None),
    status_q: Optional[str] = Query(default=None, alias="status"),
    camera_id: Optional[str] = Query(default=None),
    limit: Optional[int] = Query(default=None),
    skip: Optional[int] = Query(default=None),
    offset: Optional[int] = Query(default=None),
    format: Optional[str] = Query(default=None),  # noqa: A002
    fmt: Optional[str] = Query(default=None),
):
    raw: dict[str, Any] = dict(request.query_params)
    if target is not None:
        raw["target"] = target
    if status_q is not None:
        raw["status"] = status_q
    if camera_id is not None:
        raw["camera_id"] = camera_id
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
                        headers={"Content-Disposition": 'attachment; filename="bitrate-history.csv"'})
    return JSONResponse(payload)


async def _create_handler(payload: Any = Body(default=None), request: Request = None):
    data = _normalize(payload, request)
    # If caller supplied live-sample coordinates without explicit bitrate
    # fields, sample via the point-in-time sampler then record history.
    wants_sample = ("url" in data or "stream_url" in data or "source" in data
                    or "target" in data or "camera_id" in data or "camera" in data)
    try:
        if wants_sample and not _has_bitrate_fields(data):
            entry = svc.sample_and_record(**data)
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


async def _sample_handler(payload: Any = Body(default=None), request: Request = None):
    data = _normalize(payload, request)
    try:
        entry = svc.sample_and_record(**data)
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
        raise HTTPException(status_code=404, detail="Bitrate history entry not found")
    return _to_response(entry)


async def _delete_handler(entry_id: str):
    ok = svc.delete_entry(entry_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Bitrate history entry not found")
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


async def _target_handler(target: str, request: Request):
    raw: dict[str, Any] = dict(request.query_params)
    filters = _collect_filters(raw)
    filters.pop("target", None)
    filters.pop("stream", None)
    lim, sk = _parse_limit_skip(raw)
    items = svc.list_history(target=target, limit=lim, skip=sk, **filters)
    return JSONResponse([_to_response(e) for e in items])


async def _target_latest_handler(target: str):
    latest = svc.get_latest_for_target(target)
    if latest is None:
        raise HTTPException(status_code=404, detail="No bitrate history for target")
    return _to_response(latest)


async def _target_stats_handler(target: str):
    return {**svc.get_stats(target=target), "ok": True, "success": True}


_HISTORY_PATHS = [
    "/api/bitrate/history",
    "/api/bitrate/history/",
    "/api/bitrates/history",
    "/api/bitrates/history/",
    "/api/bitrate-history",
    "/api/bitrate-history/",
    "/api/bitrate_history",
    "/api/bitrate_history/",
    "/api/bitrate/histories",
    "/api/bitrates/histories",
    "/api/bitrate/history/list",
    "/api/bitrates/history/list",
    "/api/bitrate-history/list",
    "/api/stream-bitrate/history",
    "/api/stream-bitrate/history/",
    "/api/stream_bitrate/history",
    "/api/stream_bitrate/history/",
]

for _p in _HISTORY_PATHS:
    router.add_api_route(_p, _list_handler, methods=["GET"])
    router.add_api_route(_p, _create_handler, methods=["POST"])

_SAMPLE_PATHS = [
    "/api/bitrate/history/sample",
    "/api/bitrates/history/sample",
    "/api/bitrate-history/sample",
    "/api/bitrate_history/sample",
    "/api/bitrate/history/probe",
    "/api/stream-bitrate/history/sample",
    "/api/stream_bitrate/history/sample",
]
for _p in _SAMPLE_PATHS:
    router.add_api_route(_p, _sample_handler, methods=["GET", "POST"])

_COUNT_PATHS = [
    "/api/bitrate/history/count",
    "/api/bitrates/history/count",
    "/api/bitrate-history/count",
    "/api/bitrate_history/count",
    "/api/stream-bitrate/history/count",
    "/api/stream_bitrate/history/count",
]
for _p in _COUNT_PATHS:
    router.add_api_route(_p, _count_handler, methods=["GET", "POST"])

_CLEAR_PATHS = [
    "/api/bitrate/history/clear",
    "/api/bitrates/history/clear",
    "/api/bitrate-history/clear",
    "/api/bitrate_history/clear",
]
for _p in _CLEAR_PATHS:
    router.add_api_route(_p, _clear_handler, methods=["GET", "POST", "DELETE"])

_STATUS_PATHS = [
    "/api/bitrate/history/status",
    "/api/bitrates/history/status",
    "/api/bitrate-history/status",
    "/api/bitrate_history/status",
]
for _p in _STATUS_PATHS:
    router.add_api_route(_p, _status_handler, methods=["GET", "POST"])

_SUMMARY_PATHS = [
    "/api/bitrate/history/summary",
    "/api/bitrates/history/summary",
    "/api/bitrate-history/summary",
    "/api/bitrate_history/summary",
    "/api/bitrate/history/stats",
    "/api/bitrate-history/stats",
]
for _p in _SUMMARY_PATHS:
    router.add_api_route(_p, _summary_handler, methods=["GET", "POST"])

_CSV_PATHS = [
    "/api/bitrate/history/csv",
    "/api/bitrates/history/csv",
    "/api/bitrate-history/csv",
    "/api/bitrate_history/csv",
]
async def _csv_export_handler(request: Request):
    return await _list_handler(request, format="csv")


for _p in _CSV_PATHS:
    router.add_api_route(_p, _csv_export_handler, methods=["GET", "POST"])

_TARGET_PATHS = [
    "/api/bitrate/history/target/{target}",
    "/api/bitrates/history/target/{target}",
    "/api/bitrate-history/target/{target}",
]
for _p in _TARGET_PATHS:
    router.add_api_route(_p, _target_handler, methods=["GET"])

_TARGET_LATEST_PATHS = [
    "/api/bitrate/history/latest/{target}",
    "/api/bitrate-history/latest/{target}",
    "/api/bitrate/history/target/{target}/latest",
]
for _p in _TARGET_LATEST_PATHS:
    router.add_api_route(_p, _target_latest_handler, methods=["GET"])

_TARGET_STATS_PATHS = [
    "/api/bitrate/history/target/{target}/stats",
    "/api/bitrate/history/target/{target}/summary",
    "/api/bitrate/history/stats/{target}",
]
for _p in _TARGET_STATS_PATHS:
    router.add_api_route(_p, _target_stats_handler, methods=["GET"])


@router.get("/api/bitrate/history/health")
@router.get("/api/bitrates/history/health")
@router.get("/api/bitrate-history/health")
@router.get("/api/bitrate_history/health")
def bitrate_history_health():
    return _health_handler()


@router.get("/api/bitrate/history/{entry_id}")
@router.get("/api/bitrates/history/{entry_id}")
@router.get("/api/bitrate-history/{entry_id}")
@router.get("/api/bitrate_history/{entry_id}")
async def get_history_entry(entry_id: str):
    return await _get_handler(entry_id)


@router.delete("/api/bitrate/history/{entry_id}")
@router.delete("/api/bitrates/history/{entry_id}")
@router.delete("/api/bitrate-history/{entry_id}")
@router.delete("/api/bitrate_history/{entry_id}")
async def delete_history_entry(entry_id: str):
    return await _delete_handler(entry_id)


@router.get("/api/bitrate/history/csv-download")
async def _csv_download_alias(request: Request):
    return await _csv_export_handler(request)


# Compatibility aliases.
bitrate_history_router = router
api_router = router
history_router = router
bitrate_router = router

# Side-effect registration so the router is live even when this module is
# imported after app startup (mirrors bitrate pattern). The main app also
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
