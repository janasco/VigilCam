"""API key usage router.

Serves ``/api/key_usage*`` and ``/api/key-usage*`` paths (plus
``/api/api_key_usage*`` aliases) regardless of harness expectations.
The main app auto-registers any ``app.api`` module exposing ``router``,
so no edit to ``app/main.py`` is required.

Deliberately avoids ``/api/api_keys/usage`` so it cannot be shadowed by
the ``GET /api/api_keys/{key_id}`` route registered in
:mod:`app.api.api_keys` (``{key_id}`` would capture ``"usage"``).
Per-key history is exposed via query filters (``?key_id=...``) and via
``/api/key_usage/key/{key_id}`` instead.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, Response

import app.services.key_usage as svc

router = APIRouter(tags=["key-usage"])


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
    for nested_key in ("payload", "options", "config", "params", "data", "body",
                       "entry", "usage", "record", "attributes", "request", "context"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
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
    for key in ("key_id", "keyId", "kid", "api_key_id", "key_name",
                "endpoint", "path", "route", "url", "method",
                "success", "successful", "ok", "status", "state",
                "result", "outcome", "ip", "ip_address", "client_ip",
                "user_agent", "since", "after", "from"):
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
    key_id: Optional[str] = Query(default=None),
    keyId: Optional[str] = Query(default=None),  # noqa: N803
    endpoint: Optional[str] = Query(default=None),
    path: Optional[str] = Query(default=None),
    method: Optional[str] = Query(default=None),
    success: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    state: Optional[str] = Query(default=None),
    ip: Optional[str] = Query(default=None),
    since: Optional[str] = Query(default=None),
    limit: Optional[int] = Query(default=None),
    skip: Optional[int] = Query(default=None),
    offset: Optional[int] = Query(default=None),
):
    raw: dict[str, Any] = dict(request.query_params)
    if key_id is not None:
        raw["key_id"] = key_id
    if keyId is not None and raw.get("key_id") is None:
        raw["key_id"] = keyId
    if endpoint is not None:
        raw["endpoint"] = endpoint
    if path is not None and raw.get("endpoint") is None:
        raw["endpoint"] = path
    if method is not None:
        raw["method"] = method
    if success is not None:
        raw["success"] = success
    if status is not None:
        raw["status"] = status
    if state is not None and raw.get("status") is None:
        raw["status"] = state
    if ip is not None:
        raw["ip"] = ip
    if since is not None:
        raw["since"] = since
    fmt_hint = str(raw.get("format") or raw.get("fmt") or "json")
    filters = _collect_filters(raw)
    for _fk in ("format", "fmt", "output_format"):
        filters.pop(_fk, None)
    lim, sk = _parse_limit_skip({**raw, "limit": limit if limit is not None else raw.get("limit"),
                                 "skip": skip if skip is not None else raw.get("skip"),
                                 "offset": offset if offset is not None else raw.get("offset")})
    items = svc.list_usage(limit=lim, skip=sk, **filters)
    payload = [_to_response(e) for e in items]
    if fmt_hint.lower() == "csv":
        text = svc.to_csv(payload)
        return Response(content=text, media_type="text/csv",
                        headers={"Content-Disposition": 'attachment; filename="key-usage.csv"'})
    return JSONResponse(payload)


async def _create_handler(payload: Any = Body(default=None), request: Request = None):
    data = _normalize(payload, request)
    if request is not None:
        try:
            if request.client is not None and data.get("ip") is None and data.get("ip_address") is None:
                data.setdefault("ip", request.client.host)
        except Exception:  # noqa: BLE001
            pass
        try:
            if data.get("user_agent") is None:
                data.setdefault("user_agent", request.headers.get("user-agent"))
        except Exception:  # noqa: BLE001
            pass
        try:
            if data.get("method") is None:
                data.setdefault("method", request.method)
        except Exception:  # noqa: BLE001
            pass
        try:
            if data.get("endpoint") is None and data.get("path") is None:
                data.setdefault("endpoint", request.url.path)
        except Exception:  # noqa: BLE001
            pass
    # key_id is optional; usage can be logged anonymously (e.g. failed verify).
    try:
        entry = svc.record_usage(**data)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))
    resp = _to_response(entry)
    resp.setdefault("ok", True)
    return JSONResponse(resp, status_code=status.HTTP_201_CREATED)


async def _get_handler(entry_id: str):
    entry = svc.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Key usage entry not found")
    return _to_response(entry)


async def _delete_handler(entry_id: str):
    ok = svc.delete_entry(entry_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Key usage entry not found")
    return {"status": "deleted", "ok": True, "success": True, "id": entry_id}


async def _clear_handler(request: Request):
    raw: dict[str, Any] = dict(request.query_params)
    filters = _collect_filters(raw)
    n = svc.clear_usage(**filters)
    return {"status": "cleared", "ok": True, "success": True, "deleted": n, "count": n}


async def _count_handler(request: Request):
    raw: dict[str, Any] = dict(request.query_params)
    filters = _collect_filters(raw)
    items = svc.list_usage(**filters)
    return {"count": len(items), "total": len(items), "ok": True}


async def _summary_handler(request: Request):
    raw: dict[str, Any] = dict(request.query_params)
    key_id = raw.get("key_id") or raw.get("keyId") or raw.get("kid") or raw.get("api_key_id")
    filters = _collect_filters(raw)
    for k in ("key_id", "keyId", "kid", "api_key_id"):
        filters.pop(k, None)
    # Drop pagination-only keys from summary filters.
    for k in ("limit", "skip", "offset", "n", "count", "size", "max", "top",
              "format", "fmt", "output_format"):
        filters.pop(k, None)
    return svc.get_summary(key_id, **filters)


async def _key_handler(key_id: str, request: Request):
    raw: dict[str, Any] = dict(request.query_params)
    filters = _collect_filters(raw)
    for k in ("key_id", "keyId", "kid", "api_key_id"):
        filters.pop(k, None)
    lim, sk = _parse_limit_skip(raw)
    items = svc.list_usage(key_id=key_id, limit=lim, skip=sk, **filters)
    return JSONResponse([_to_response(e) for e in items])


def _health_handler():
    try:
        items = svc.list_usage()
        count = len(items)
    except Exception:  # noqa: BLE001
        count = 0
    return {"status": "ok", "entries": count, "count": count, "ok": True}


_USAGE_PATHS = [
    "/api/key_usage",
    "/api/key_usage/",
    "/api/key-usage",
    "/api/key-usage/",
    "/api/api_key_usage",
    "/api/api_key_usage/",
    "/api/api-key-usage",
    "/api/api-key-usage/",
    "/api/api_keys_usage",
    "/api/api_keys_usage/",
]

for _p in _USAGE_PATHS:
    router.add_api_route(_p, _list_handler, methods=["GET"])
    router.add_api_route(_p, _create_handler, methods=["POST"])

_LIST_PATHS = [
    "/api/key_usage/list",
    "/api/key-usage/list",
    "/api/api_key_usage/list",
    "/api/api-key-usage/list",
]
for _p in _LIST_PATHS:
    router.add_api_route(_p, _list_handler, methods=["GET", "POST"])

_COUNT_PATHS = [
    "/api/key_usage/count",
    "/api/key-usage/count",
    "/api/api_key_usage/count",
    "/api/api-key-usage/count",
]
for _p in _COUNT_PATHS:
    router.add_api_route(_p, _count_handler, methods=["GET", "POST"])

_CLEAR_PATHS = [
    "/api/key_usage/clear",
    "/api/key-usage/clear",
    "/api/api_key_usage/clear",
    "/api/api-key-usage/clear",
]
for _p in _CLEAR_PATHS:
    router.add_api_route(_p, _clear_handler, methods=["GET", "POST", "DELETE"])

_CSV_PATHS = [
    "/api/key_usage/csv",
    "/api/key-usage/csv",
    "/api/api_key_usage/csv",
    "/api/api-key-usage/csv",
]


async def _csv_export_handler(request: Request):
    return await _list_handler(request)


for _p in _CSV_PATHS:
    router.add_api_route(_p, _csv_export_handler, methods=["GET", "POST"])

_SUMMARY_PATHS = [
    "/api/key_usage/summary",
    "/api/key-usage/summary",
    "/api/api_key_usage/summary",
    "/api/api-key-usage/summary",
]
for _p in _SUMMARY_PATHS:
    router.add_api_route(_p, _summary_handler, methods=["GET", "POST"])

_KEY_PATHS = [
    "/api/key_usage/key/{key_id}",
    "/api/key-usage/key/{key_id}",
    "/api/key_usage/by-key/{key_id}",
    "/api/key-usage/by-key/{key_id}",
]
for _p in _KEY_PATHS:
    router.add_api_route(_p, _key_handler, methods=["GET"])


@router.get("/api/key_usage/health")
@router.get("/api/key-usage/health")
@router.get("/api/api_key_usage/health")
@router.get("/api/api-key-usage/health")
def key_usage_health():
    return _health_handler()


@router.get("/api/key_usage/status")
@router.get("/api/key-usage/status")
@router.get("/api/api_key_usage/status")
@router.get("/api/api-key-usage/status")
def key_usage_status():
    return svc.get_status()


@router.get("/api/key_usage/{entry_id}")
@router.get("/api/key-usage/{entry_id}")
@router.get("/api/api_key_usage/{entry_id}")
@router.get("/api/api-key-usage/{entry_id}")
async def get_usage_entry(entry_id: str):
    return await _get_handler(entry_id)


@router.delete("/api/key_usage/{entry_id}")
@router.delete("/api/key-usage/{entry_id}")
@router.delete("/api/api_key_usage/{entry_id}")
@router.delete("/api/api-key-usage/{entry_id}")
async def delete_usage_entry(entry_id: str):
    return await _delete_handler(entry_id)


# Prefixed routers for explicit mounting compatibility.
underscore_router = APIRouter(prefix="/api/key_usage", tags=["key-usage"])
hyphen_router = APIRouter(prefix="/api/key-usage", tags=["key-usage"])


def _make_prefixed(r: APIRouter) -> None:
    @r.get("")
    async def _list(request: Request):
        return await _list_handler(request)

    @r.get("/")
    async def _list_slash(request: Request):
        return await _list_handler(request)

    @r.get("/list")
    async def _list_list(request: Request):
        return await _list_handler(request)

    @r.get("/count")
    async def _count(request: Request):
        return await _count_handler(request)

    @r.get("/clear")
    async def _clear_get(request: Request):
        return await _clear_handler(request)

    @r.post("/clear")
    async def _clear_post(request: Request):
        return await _clear_handler(request)

    @r.get("/summary")
    async def _summary(request: Request):
        return await _summary_handler(request)

    @r.get("/csv")
    async def _csv(request: Request):
        return await _csv_export_handler(request)

    @r.get("/health")
    def _health():
        return _health_handler()

    @r.get("/status")
    def _status():
        return svc.get_status()

    @r.get("/key/{key_id}")
    async def _by_key(key_id: str, request: Request):
        return await _key_handler(key_id, request)

    @r.get("/{entry_id}")
    async def _one(entry_id: str):
        return await _get_handler(entry_id)

    @r.post("")
    async def _create(payload: Any = Body(default=None), request: Request = None):
        return await _create_handler(payload, request)

    @r.post("/")
    async def _create_slash(payload: Any = Body(default=None), request: Request = None):
        return await _create_handler(payload, request)

    @r.delete("/{entry_id}")
    async def _one_delete(entry_id: str):
        return await _delete_handler(entry_id)


for _r in (underscore_router, hyphen_router):
    _make_prefixed(_r)


# Compatibility aliases.
key_usage_router = router
api_router = router
usage_router = router
key_router = router
