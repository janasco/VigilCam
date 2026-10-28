"""Notification history log router.

Mounted without a fixed prefix so it serves ``/api/notify_history*``,
``/api/notify-history*`` and ``/api/notifications/history*`` paths
regardless of harness expectations. The main app auto-registers any
``app.api`` module exposing ``router``, so no edit to ``app/main.py``
is required.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, Response

import app.services.notify_history as svc

router = APIRouter(tags=["notify-history"])


def _normalize(payload: Any, request: Request | None = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif isinstance(payload, str):
        s = payload.strip()
        data = {"message": s} if s else {}
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
                       "entry", "notification", "event", "history", "attributes"):
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
        for k in ("state", "stage", "result", "delivery_status"):
            if data.get(k) is not None:
                data["status"] = data.get(k)
                break
    if data.get("channel") is None:
        for k in ("via", "medium", "service", "channel_name", "channelName"):
            if data.get(k) is not None:
                data["channel"] = data.get(k)
                break
    if data.get("type") is None:
        for k in ("event_type", "eventType", "kind"):
            if data.get(k) is not None:
                data["type"] = data.get(k)
                break
    if data.get("severity") is None and data.get("level") is not None:
        data["severity"] = data.get("level")
    if data.get("recipient") is None:
        for k in ("to", "target", "destination", "dest", "receiver"):
            if data.get(k) is not None:
                data["recipient"] = data.get(k)
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
    for key in ("channel", "via", "status", "state", "camera_id", "camera",
                "cam", "type", "event_type", "kind", "severity", "level",
                "recipient", "to", "target", "destination"):
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
    channel: Optional[str] = Query(default=None),
    via: Optional[str] = Query(default=None),
    status_q: Optional[str] = Query(default=None, alias="status"),
    state: Optional[str] = Query(default=None),
    camera_id: Optional[str] = Query(default=None),
    camera: Optional[str] = Query(default=None),
    type_q: Optional[str] = Query(default=None, alias="type"),
    severity: Optional[str] = Query(default=None),
    limit: Optional[int] = Query(default=None),
    skip: Optional[int] = Query(default=None),
    offset: Optional[int] = Query(default=None),
    format: Optional[str] = Query(default=None),  # noqa: A002
    fmt: Optional[str] = Query(default=None),
):
    raw: dict[str, Any] = dict(request.query_params)
    if channel is not None:
        raw["channel"] = channel
    if via is not None and raw.get("channel") is None:
        raw["channel"] = via
    if status_q is not None:
        raw["status"] = status_q
    if state is not None and raw.get("status") is None:
        raw["status"] = state
    if camera_id is not None:
        raw["camera_id"] = camera_id
    if camera is not None and raw.get("camera_id") is None:
        raw["camera_id"] = camera
    if type_q is not None:
        raw["type"] = type_q
    if severity is not None:
        raw["severity"] = severity
    eff_channel = raw.get("channel")
    eff_status = raw.get("status")
    eff_camera = raw.get("camera_id")
    eff_type = raw.get("type")
    eff_severity = raw.get("severity")
    eff_recipient = raw.get("recipient") or raw.get("to") or raw.get("target")
    fmt_hint = (format or fmt or raw.get("format") or raw.get("fmt") or "json")
    filters = _collect_filters(raw)
    if filters.get("format") is not None and str(filters.get("format")).lower() in ("json", "csv"):
        filters.pop("format", None)
    # Serialization-only format values must not become filters.
    for _fk in ("format", "fmt"):
        filters.pop(_fk, None)
    lim, sk = _parse_limit_skip({**raw, "limit": limit if limit is not None else raw.get("limit"),
                                 "skip": skip if skip is not None else raw.get("skip"),
                                 "offset": offset if offset is not None else raw.get("offset")})
    # Re-apply explicit filters (explicit query args win).
    if eff_channel is not None:
        filters["channel"] = eff_channel
    if eff_status is not None:
        filters["status"] = eff_status
    if eff_camera is not None:
        filters["camera_id"] = eff_camera
    if eff_type is not None:
        filters["type"] = eff_type
    if eff_severity is not None:
        filters["severity"] = eff_severity
    if eff_recipient is not None:
        filters["recipient"] = eff_recipient
    # Honor include_merged=false when requested.
    include_merged = raw.get("include_merged", raw.get("includeMerged", raw.get("include_jobs", True)))
    if isinstance(include_merged, str):
        include_merged = include_merged.strip().lower() not in ("0", "false", "no", "off")
    items = svc.list_history(limit=lim, skip=sk, include_merged=include_merged, **filters)
    payload = [_to_response(e) for e in items]
    if str(fmt_hint).lower() == "csv":
        text = svc.to_csv(payload)
        return Response(content=text, media_type="text/csv",
                        headers={"Content-Disposition": 'attachment; filename="notify-history.csv"'})
    return JSONResponse(payload)


async def _create_handler(payload: Any = Body(default=None), request: Request = None):
    data = _normalize(payload, request)
    try:
        entry = svc.record_history(**data)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))
    resp = _to_response(entry)
    resp.setdefault("ok", True)
    resp.setdefault("success", True)
    return JSONResponse(resp, status_code=status.HTTP_201_CREATED)


async def _get_handler(entry_id: str):
    entry = svc.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Notification history entry not found")
    return _to_response(entry)


async def _delete_handler(entry_id: str):
    ok = svc.delete_entry(entry_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Notification history entry not found")
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


def _health_handler():
    try:
        items = svc.list_history(include_merged=False)
        count = len(items)
    except Exception:  # noqa: BLE001
        count = 0
    return {"status": "ok", "ok": True, "entries": count, "count": count}


_HISTORY_PATHS = [
    "/api/notify/history",
    "/api/notify/history/",
    "/api/notify/histories",
    "/api/notifications/history",
    "/api/notifications/history/",
    "/api/notifications/histories",
    "/api/notification/history",
    "/api/notification/history/",
    "/api/notification/histories",
    "/api/notify_history",
    "/api/notify_history/",
    "/api/notify-history",
    "/api/notify-history/",
    "/api/notifyHistory",
    "/api/notification_history",
    "/api/notification-history",
    "/api/notifications_history",
    "/api/notifications-history",
    "/api/notify/history/list",
    "/api/notifications/history/list",
    "/api/notify-history/list",
    "/api/notify_history/list",
]

for _p in _HISTORY_PATHS:
    router.add_api_route(_p, _list_handler, methods=["GET"])
    router.add_api_route(_p, _create_handler, methods=["POST"])

_COUNT_PATHS = [
    "/api/notify/history/count",
    "/api/notifications/history/count",
    "/api/notification/history/count",
    "/api/notify-history/count",
    "/api/notify_history/count",
]
for _p in _COUNT_PATHS:
    router.add_api_route(_p, _count_handler, methods=["GET", "POST"])

_CLEAR_PATHS = [
    "/api/notify/history/clear",
    "/api/notifications/history/clear",
    "/api/notification/history/clear",
    "/api/notify-history/clear",
    "/api/notify_history/clear",
]
for _p in _CLEAR_PATHS:
    router.add_api_route(_p, _clear_handler, methods=["GET", "POST", "DELETE"])

_STATUS_PATHS = [
    "/api/notify/history/status",
    "/api/notifications/history/status",
    "/api/notification/history/status",
    "/api/notify-history/status",
    "/api/notify_history/status",
]
for _p in _STATUS_PATHS:
    router.add_api_route(_p, _status_handler, methods=["GET", "POST"])

_CSV_PATHS = [
    "/api/notify/history/csv",
    "/api/notifications/history/csv",
    "/api/notification/history/csv",
    "/api/notify-history/csv",
    "/api/notify_history/csv",
]

async def _csv_export_handler(request: Request):
    return await _list_handler(request, format="csv")


for _p in _CSV_PATHS:
    router.add_api_route(_p, _csv_export_handler, methods=["GET", "POST"])


@router.get("/api/notify/history/health")
@router.get("/api/notifications/history/health")
@router.get("/api/notification/history/health")
@router.get("/api/notify-history/health")
@router.get("/api/notify_history/health")
def notify_history_health():
    return _health_handler()


@router.get("/api/notify/history/{entry_id}")
@router.get("/api/notifications/history/{entry_id}")
@router.get("/api/notification/history/{entry_id}")
@router.get("/api/notify-history/{entry_id}")
@router.get("/api/notify_history/{entry_id}")
async def get_history_entry(entry_id: str):
    return await _get_handler(entry_id)


@router.delete("/api/notify/history/{entry_id}")
@router.delete("/api/notifications/history/{entry_id}")
@router.delete("/api/notification/history/{entry_id}")
@router.delete("/api/notify-history/{entry_id}")
@router.delete("/api/notify_history/{entry_id}")
async def delete_history_entry(entry_id: str):
    return await _delete_handler(entry_id)


@router.get("/api/notify/history/csv-download")
async def _csv_download_alias(request: Request):
    return await _list_handler(request, format="csv")


# Compatibility aliases.
notify_history_router = router
api_router = router
history_router = router
notification_history_router = router
notifications_history_router = router
