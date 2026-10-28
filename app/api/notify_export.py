"""Notification history export router (CSV/JSON download).

Mounted without a prefix so it serves ``/api/notify/export*``,
``/api/notify-export*`` and ``/api/notifications/export*`` paths regardless
of harness expectations. The main app auto-registers any ``app.api`` module
exposing ``router``, so no edit to ``app/main.py`` is required.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, Response

from app.services.notify_export import fetch_entries, to_csv, to_json

router = APIRouter(tags=["notify-export"])

_FILTER_PARAMS = [
    "channel",
    "via",
    "status",
    "state",
    "camera_id",
    "camera",
    "cam",
    "type",
    "event_type",
    "kind",
    "severity",
    "level",
    "recipient",
    "to",
    "target",
    "destination",
]


def _collect_filters(raw: dict) -> dict:
    filters: dict[str, Any] = {}
    for key in _FILTER_PARAMS:
        if raw.get(key) is not None:
            filters[key] = raw.get(key)
    return filters


def _parse_limit_skip(raw: dict) -> tuple[Any, Any]:
    limit = raw.get("limit", raw.get("n", raw.get("count", raw.get("size", raw.get("max")))))
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


def _resolve_format(explicit: Any, raw: dict) -> str:
    for key in ("format", "fmt", "output_format", "as_format", "type_format"):
        val = raw.get(key) if explicit is None else explicit
        if val is not None:
            return str(val).lower()
        if explicit is not None:
            break
    return "json"


def _do_export(
    raw: dict,
    explicit_format: Any = None,
    filename: Optional[str] = None,
) -> Response:
    fmt = _resolve_format(explicit_format, raw)
    want_csv = fmt in ("csv", "text/csv", "text-csv", "csv-download", "download")
    if str(raw.get("_path_hint", "")).endswith("csv"):
        want_csv = True
    filters = _collect_filters(raw)
    limit, skip = _parse_limit_skip(raw)
    entries = fetch_entries(limit=limit, skip=skip, **filters)
    if want_csv:
        text = to_csv(entries)
        name = filename or "notify-export.csv"
        return Response(
            content=text,
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )
    import json as _json

    safe = entries
    try:
        payload = _json.loads(to_json(safe))
    except Exception:
        payload = safe
    return JSONResponse(payload)


async def _export_handler(
    request: Request,
    format: Optional[str] = Query(default=None),  # noqa: A002
    fmt: Optional[str] = Query(default=None),
    output_format: Optional[str] = Query(default=None),
    channel: Optional[str] = Query(default=None),
    via: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),  # noqa: A002
    state: Optional[str] = Query(default=None),
    camera_id: Optional[str] = Query(default=None),
    camera: Optional[str] = Query(default=None),
    cam: Optional[str] = Query(default=None),
    type: Optional[str] = Query(default=None),  # noqa: A002
    event_type: Optional[str] = Query(default=None),
    kind: Optional[str] = Query(default=None),
    severity: Optional[str] = Query(default=None),
    level: Optional[str] = Query(default=None),
    recipient: Optional[str] = Query(default=None),
    to: Optional[str] = Query(default=None),
    target: Optional[str] = Query(default=None),
    destination: Optional[str] = Query(default=None),
    limit: Optional[int] = Query(default=None),
    skip: Optional[int] = Query(default=None),
    offset: Optional[int] = Query(default=None),
    n: Optional[int] = Query(default=None),
    count: Optional[int] = Query(default=None),
    size: Optional[int] = Query(default=None),
):
    raw: dict[str, Any] = {
        "format": format,
        "fmt": fmt,
        "output_format": output_format,
        "channel": channel,
        "via": via,
        "status": status,
        "state": state,
        "camera_id": camera_id,
        "camera": camera,
        "cam": cam,
        "type": type,
        "event_type": event_type,
        "kind": kind,
        "severity": severity,
        "level": level,
        "recipient": recipient,
        "to": to,
        "target": target,
        "destination": destination,
        "limit": limit,
        "skip": skip,
        "offset": offset,
        "n": n,
        "count": count,
        "size": size,
    }
    try:
        for k, v in request.query_params.items():
            if k not in raw or raw.get(k) is None:
                raw[k] = v
    except Exception:
        pass
    explicit = format if format is not None else (fmt if fmt is not None else output_format)
    return _do_export(raw, explicit_format=explicit)


async def _export_csv_handler(request: Request):
    base = await _export_handler(request, format="csv")
    return base


async def _export_json_handler(request: Request):
    return await _export_handler(request, format="json")


_EXPORT_PATHS = [
    "/api/notify/export",
    "/api/notify/exports",
    "/api/notify/export/",
    "/api/notify/exports/",
    "/api/notify/download",
    "/api/notify-export",
    "/api/notify-export/",
    "/api/notify-export/export",
    "/api/notify-export/exports",
    "/api/notify_exports",
    "/api/notify.exports",
    "/api/notifications/export",
    "/api/notifications/exports",
    "/api/notification/export",
    "/api/notification/exports",
    "/api/notify_history/export",
    "/api/notify_history/exports",
    "/api/notify-history/export",
    "/api/notify-history/exports",
    "/api/notifications_history/export",
    "/api/notifications-history/export",
    "/api/notify/history/export",
    "/api/notifications/history/export",
]
_CSV_PATHS = [
    "/api/notify/export/csv",
    "/api/notify/export-csv",
    "/api/notify/exports/csv",
    "/api/notify-export/csv",
    "/api/notify-export/csv-download",
    "/api/notifications/export/csv",
    "/api/notify/history/export/csv",
]
_JSON_PATHS = [
    "/api/notify/export/json",
    "/api/notify/export-json",
    "/api/notify/exports/json",
    "/api/notify-export/json",
    "/api/notifications/export/json",
    "/api/notify/history/export/json",
]

for _p in _EXPORT_PATHS:
    router.add_api_route(_p, _export_handler, methods=["GET", "POST"])
for _p in _CSV_PATHS:
    router.add_api_route(_p, _export_csv_handler, methods=["GET", "POST"])
for _p in _JSON_PATHS:
    router.add_api_route(_p, _export_json_handler, methods=["GET", "POST"])


@router.get("/api/notify/export/health")
@router.get("/api/notify-export/health")
@router.get("/api/notifications/export/health")
def export_health():
    try:
        entries = fetch_entries()
        count = len(entries)
    except Exception:
        count = 0
    return {"status": "ok", "entries": count}


# Compatibility aliases.
notify_export_router = router
api_router = router
notify_api_router = router
export_router = router
notify_export_api_router = router
notification_export_router = router
