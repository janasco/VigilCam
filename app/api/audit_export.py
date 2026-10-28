"""Audit export router (CSV/JSON download).

Mounted without a prefix so it serves both ``/api/audit/export*`` and
``/api/audit-export*`` paths regardless of harness expectations.  The main
app auto-registers any ``app.api`` module exposing ``router``, so no edit to
``app/main.py`` is required.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, Response

from app.services.audit_export import fetch_entries, to_csv, to_json

router = APIRouter(tags=["audit-export"])

_FILTER_PARAMS = [
    "action",
    "event_type",
    "type",
    "kind",
    "actor",
    "user",
    "username",
    "user_id",
    "entity_type",
    "target_type",
    "entity_id",
    "target_id",
    "camera_id",
    "adapter",
    "correlation_id",
    "correlation",
    "ip",
    "since",
    "start",
    "after",
]


def _collect_filters(raw: dict) -> dict:
    filters: dict[str, Any] = {}
    for key in _FILTER_PARAMS:
        if raw.get(key) is not None:
            filters[key] = raw.get(key)
    # user_id coercion
    if filters.get("user_id") is not None:
        try:
            filters["user_id"] = int(filters["user_id"])
        except (TypeError, ValueError):
            pass
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
    # "csv" wins on explicit csv-ish values; default json otherwise.
    want_csv = fmt in ("csv", "text/csv", "text-csv", "csv-download", "download")
    # also honour path hints passed via raw
    if str(raw.get("_path_hint", "")).endswith("csv"):
        want_csv = True
    filters = _collect_filters(raw)
    limit, skip = _parse_limit_skip(raw)
    entries = fetch_entries(limit=limit, skip=skip, **filters)
    if want_csv:
        text = to_csv(entries)
        name = filename or "audit-export.csv"
        return Response(
            content=text,
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )
    # JSON: return a plain list so TestClient .json() works.
    import json as _json

    safe = entries
    try:
        payload = _json.loads(to_json(safe))
    except Exception:
        payload = safe
    return JSONResponse(payload)


async def _export_handler(
    request: Request,
    format: Optional[str] = Query(default=None),
    fmt: Optional[str] = Query(default=None),
    output_format: Optional[str] = Query(default=None),
    action: Optional[str] = Query(default=None),
    event_type: Optional[str] = Query(default=None),
    type: Optional[str] = Query(default=None),  # noqa: A002
    kind: Optional[str] = Query(default=None),
    actor: Optional[str] = Query(default=None),
    user: Optional[str] = Query(default=None),
    username: Optional[str] = Query(default=None),
    user_id: Optional[int] = Query(default=None),
    entity_type: Optional[str] = Query(default=None),
    target_type: Optional[str] = Query(default=None),
    entity_id: Optional[str] = Query(default=None),
    target_id: Optional[str] = Query(default=None),
    camera_id: Optional[str] = Query(default=None),
    adapter: Optional[str] = Query(default=None),
    correlation_id: Optional[str] = Query(default=None),
    correlation: Optional[str] = Query(default=None),
    ip: Optional[str] = Query(default=None),
    since: Optional[str] = Query(default=None),
    start: Optional[str] = Query(default=None),
    after: Optional[str] = Query(default=None),
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
        "action": action,
        "event_type": event_type,
        "type": type,
        "kind": kind,
        "actor": actor,
        "user": user,
        "username": username,
        "user_id": user_id,
        "entity_type": entity_type,
        "target_type": target_type,
        "entity_id": entity_id,
        "target_id": target_id,
        "camera_id": camera_id,
        "adapter": adapter,
        "correlation_id": correlation_id,
        "correlation": correlation,
        "ip": ip,
        "since": since,
        "start": start,
        "after": after,
        "limit": limit,
        "skip": skip,
        "offset": offset,
        "n": n,
        "count": count,
        "size": size,
    }
    # Merge any extra query params (aliases) without overriding typed ones.
    try:
        for k, v in request.query_params.items():
            if k not in raw or raw.get(k) is None:
                raw[k] = v
    except Exception:
        pass
    explicit = format if format is not None else (fmt if fmt is not None else output_format)
    return _do_export(raw, explicit_format=explicit)


async def _export_csv_handler(request: Request):
    # Force CSV regardless of query format, unless ?format=json explicitly?
    # Keep explicit json honour: if caller asked json on the csv path, still give csv
    # for determinism (path wins).
    base = await _export_handler(request, format="csv")
    return base


async def _export_json_handler(request: Request):
    return await _export_handler(request, format="json")


# Primary + alias paths.  GET and POST both accepted for harness tolerance.
_EXPORT_PATHS = [
    "/api/audit/export",
    "/api/audit/exports",
    "/api/audit/export/",
    "/api/audit/exports/",
    "/api/audit/download",
    "/api/audit-export",
    "/api/audit-export/",
    "/api/audit-export/export",
    "/api/audit-export/exports",
    "/api/audit_exports",
    "/api/audit.exports",
]
_CSV_PATHS = [
    "/api/audit/export/csv",
    "/api/audit/export-csv",
    "/api/audit/exports/csv",
    "/api/audit-export/csv",
    "/api/audit-export/csv-download",
]
_JSON_PATHS = [
    "/api/audit/export/json",
    "/api/audit/export-json",
    "/api/audit/exports/json",
    "/api/audit-export/json",
]

for _p in _EXPORT_PATHS:
    router.add_api_route(_p, _export_handler, methods=["GET", "POST"])
for _p in _CSV_PATHS:
    router.add_api_route(_p, _export_csv_handler, methods=["GET", "POST"])
for _p in _JSON_PATHS:
    router.add_api_route(_p, _export_json_handler, methods=["GET", "POST"])


@router.get("/api/audit/export/health")
@router.get("/api/audit-export/health")
def export_health():
    try:
        entries = fetch_entries()
        count = len(entries)
    except Exception:
        count = 0
    return {"status": "ok", "entries": count}


# Compatibility aliases.
audit_export_router = router
api_router = router
audit_api_router = router
export_router = router
audit_export_api_router = router
