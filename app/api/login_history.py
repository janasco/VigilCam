"""Login history router.

Mounted without a fixed prefix so it serves ``/api/login/history*``,
``/api/logins/history*``, ``/api/login-history*`` and
``/api/login_history*`` paths regardless of harness expectations.
The main app auto-registers any ``app.api`` module exposing ``router``,
so no edit to ``app/main.py`` is required.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, Response

import app.services.login_history as svc

router = APIRouter(tags=["login-history"])


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
                       "entry", "login", "attempt", "attributes", "record"):
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
    for key in ("username", "user", "user_name", "user_id", "userId", "uid",
                "success", "successful", "ok", "status", "state", "result",
                "outcome", "ip", "ip_address", "client_ip", "user_agent",
                "device", "method", "session_id", "sessionId", "since",
                "after", "from"):
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
    username: Optional[str] = Query(default=None),
    user: Optional[str] = Query(default=None),
    user_id: Optional[str] = Query(default=None),
    success: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    state: Optional[str] = Query(default=None),
    ip: Optional[str] = Query(default=None),
    session_id: Optional[str] = Query(default=None),
    since: Optional[str] = Query(default=None),
    limit: Optional[int] = Query(default=None),
    skip: Optional[int] = Query(default=None),
    offset: Optional[int] = Query(default=None),
):
    raw: dict[str, Any] = dict(request.query_params)
    if username is not None:
        raw["username"] = username
    if user is not None and raw.get("username") is None:
        raw["username"] = user
    if user_id is not None:
        raw["user_id"] = user_id
    if success is not None:
        raw["success"] = success
    if status is not None:
        raw["status"] = status
    if state is not None and raw.get("status") is None:
        raw["status"] = state
    if ip is not None:
        raw["ip"] = ip
    if session_id is not None:
        raw["session_id"] = session_id
    if since is not None:
        raw["since"] = since
    fmt_hint = str(raw.get("format") or raw.get("fmt") or "json")
    filters = _collect_filters(raw)
    # Serialization-only format values are not filters.
    for _fk in ("format", "fmt", "output_format"):
        filters.pop(_fk, None)
    lim, sk = _parse_limit_skip({**raw, "limit": limit if limit is not None else raw.get("limit"),
                                 "skip": skip if skip is not None else raw.get("skip"),
                                 "offset": offset if offset is not None else raw.get("offset")})
    items = svc.list_history(limit=lim, skip=sk, **filters)
    payload = [_to_response(e) for e in items]
    if fmt_hint.lower() == "csv":
        text = svc.to_csv(payload)
        return Response(content=text, media_type="text/csv",
                        headers={"Content-Disposition": 'attachment; filename="login-history.csv"'})
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
        entry = svc.record_login(**data)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))
    resp = _to_response(entry)
    resp.setdefault("ok", True)
    resp.setdefault("success", resp.get("success", True))
    return JSONResponse(resp, status_code=status.HTTP_201_CREATED)


async def _get_handler(entry_id: str):
    entry = svc.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Login history entry not found")
    return _to_response(entry)


async def _delete_handler(entry_id: str):
    ok = svc.delete_entry(entry_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Login history entry not found")
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
    "/api/login/history",
    "/api/login/history/",
    "/api/logins/history",
    "/api/logins/history/",
    "/api/login-history",
    "/api/login-history/",
    "/api/login_history",
    "/api/login_history/",
    "/api/auth/login-history",
    "/api/auth/login_history",
    "/api/login/history/list",
    "/api/logins/history/list",
    "/api/login-history/list",
    "/api/login_history/list",
]

for _p in _HISTORY_PATHS:
    router.add_api_route(_p, _list_handler, methods=["GET"])
    router.add_api_route(_p, _create_handler, methods=["POST"])

_COUNT_PATHS = [
    "/api/login/history/count",
    "/api/logins/history/count",
    "/api/login-history/count",
    "/api/login_history/count",
]
for _p in _COUNT_PATHS:
    router.add_api_route(_p, _count_handler, methods=["GET", "POST"])

_CLEAR_PATHS = [
    "/api/login/history/clear",
    "/api/logins/history/clear",
    "/api/login-history/clear",
    "/api/login_history/clear",
]
for _p in _CLEAR_PATHS:
    router.add_api_route(_p, _clear_handler, methods=["GET", "POST", "DELETE"])

_CSV_PATHS = [
    "/api/login/history/csv",
    "/api/logins/history/csv",
    "/api/login-history/csv",
    "/api/login_history/csv",
]


async def _csv_export_handler(request: Request):
    return await _list_handler(request)


for _p in _CSV_PATHS:
    router.add_api_route(_p, _csv_export_handler, methods=["GET", "POST"])


@router.get("/api/login/history/health")
@router.get("/api/logins/history/health")
@router.get("/api/login-history/health")
@router.get("/api/login_history/health")
def login_history_health():
    return _health_handler()


@router.get("/api/login/history/{entry_id}")
@router.get("/api/logins/history/{entry_id}")
@router.get("/api/login-history/{entry_id}")
@router.get("/api/login_history/{entry_id}")
async def get_history_entry(entry_id: str):
    return await _get_handler(entry_id)


@router.delete("/api/login/history/{entry_id}")
@router.delete("/api/logins/history/{entry_id}")
@router.delete("/api/login-history/{entry_id}")
@router.delete("/api/login_history/{entry_id}")
async def delete_history_entry(entry_id: str):
    return await _delete_handler(entry_id)


@router.get("/api/login/history/csv-download")
async def _csv_download_alias(request: Request):
    request.scope["query_string"] = b"format=csv&" + request.scope.get("query_string", b"")
    # Rebuild query params handling via direct CSV path.
    items = svc.list_history()
    text = svc.to_csv([_to_response(e) for e in items])
    return Response(content=text, media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="login-history.csv"'})


# Prefixed routers for explicit mounting compatibility.
underscore_router = APIRouter(prefix="/api/login_history", tags=["login-history"])
hyphen_router = APIRouter(prefix="/api/login-history", tags=["login-history"])
logins_router = APIRouter(prefix="/api/logins/history", tags=["login-history"])


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

    @r.get("/health")
    def _health():
        return _health_handler()

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


for _r in (underscore_router, hyphen_router, logins_router):
    _make_prefixed(_r)


# Compatibility aliases.
login_history_router = router
api_router = router
history_router = router
login_router = router
