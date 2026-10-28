"""System log viewer API.

Tails server logs with optional level filtering. Auto-registered by
``app.main`` (any module in ``app.api`` exposing ``router`` is included),
so no ``app/main.py`` edit is needed.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

router = APIRouter(tags=["logview", "logs"])

_PATHS = (
    "/api/logview",
    "/api/logs",
    "/api/system/logs",
    "/api/system-logs",
    "/api/system_logs",
    "/api/server-logs",
    "/api/server_logs",
    "/api/server/logs",
    "/api/log",
    "/api/log-view",
    "/api/log_view",
)


def _coerce_lines(
    lines: Any = None,
    n: Any = None,
    limit: Any = None,
    count: Any = None,
    size: Any = None,
) -> int:
    for cand in (lines, n, limit, count, size):
        if cand is None:
            continue
        try:
            value = int(cand)
        except (TypeError, ValueError):
            continue
        return max(1, min(value, 5000))
    return 100


def _first_str(*values: Any) -> Optional[str]:
    for v in values:
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return None


def _do_tail(
    lines: int = 100,
    level: Any = None,
    min_level: Any = None,
    search: Any = None,
    source: Any = None,
) -> dict[str, Any]:
    from app.services import logview as svc

    return svc.tail_server_logs(
        lines=lines,
        level=level,
        min_level=min_level,
        search=search,
        source=source,
    )


def _register(r: APIRouter, base: str) -> None:
    @r.get(base)
    def tail_root(
        request: Request,
        lines: Optional[int] = Query(default=None),
        n: Optional[int] = Query(default=None),
        limit: Optional[int] = Query(default=None),
        count: Optional[int] = Query(default=None),
        size: Optional[int] = Query(default=None),
        level: Optional[str] = Query(default=None),
        levels: Optional[str] = Query(default=None),
        min_level: Optional[str] = Query(default=None),
        minimum: Optional[str] = Query(default=None),
        severity: Optional[str] = Query(default=None),
        search: Optional[str] = Query(default=None),
        query: Optional[str] = Query(default=None),
        q: Optional[str] = Query(default=None),
        filter: Optional[str] = Query(default=None),  # noqa: A002
        source: Optional[str] = Query(default=None),
        file: Optional[str] = Query(default=None),
        name: Optional[str] = Query(default=None),
        path: Optional[str] = Query(default=None),
        log: Optional[str] = Query(default=None),
    ):
        wanted = _coerce_lines(lines, n, limit, count, size)
        lvl = _first_str(level, levels)
        # Also accept level aliases passed as other query keys.
        if lvl is None:
            try:
                qp = request.query_params
                for alias in ("log_level", "loglevel", "lvl", "severity_level"):
                    if alias in qp and str(qp[alias]).strip():
                        lvl = str(qp[alias]).strip()
                        break
            except Exception:  # noqa: BLE001
                pass
        min_lvl = _first_str(min_level, minimum, severity)
        needle = _first_str(search, query, q, filter)
        src = _first_str(source, file, name, path, log)
        data = _do_tail(lines=wanted, level=lvl, min_level=min_lvl, search=needle, source=src)
        return JSONResponse(data)

    @r.get(base + "/")
    def tail_root_slash(
        request: Request,
        lines: Optional[int] = Query(default=None),
        n: Optional[int] = Query(default=None),
        limit: Optional[int] = Query(default=None),
        level: Optional[str] = Query(default=None),
        min_level: Optional[str] = Query(default=None),
        search: Optional[str] = Query(default=None),
        source: Optional[str] = Query(default=None),
    ):
        return tail_root(
            request, lines, n, limit, None, None, level, None,
            min_level, None, None, search, None, None, None,
            source, None, None, None, None,
        )

    @r.get(base + "/tail")
    def tail_path(
        request: Request,
        lines: Optional[int] = Query(default=None),
        n: Optional[int] = Query(default=None),
        limit: Optional[int] = Query(default=None),
        count: Optional[int] = Query(default=None),
        size: Optional[int] = Query(default=None),
        level: Optional[str] = Query(default=None),
        levels: Optional[str] = Query(default=None),
        min_level: Optional[str] = Query(default=None),
        minimum: Optional[str] = Query(default=None),
        severity: Optional[str] = Query(default=None),
        search: Optional[str] = Query(default=None),
        query: Optional[str] = Query(default=None),
        q: Optional[str] = Query(default=None),
        filter: Optional[str] = Query(default=None),  # noqa: A002
        source: Optional[str] = Query(default=None),
        file: Optional[str] = Query(default=None),
        name: Optional[str] = Query(default=None),
        path: Optional[str] = Query(default=None),
        log: Optional[str] = Query(default=None),
    ):
        return tail_root(
            request, lines, n, limit, count, size, level, levels,
            min_level, minimum, severity, search, query, q, filter,
            source, file, name, path, log,
        )

    @r.get(base + "/file")
    def tail_file_alias(request: Request):
        return tail_root(request)

    @r.get(base + "/view")
    def tail_view_alias(request: Request):
        return tail_root(request)

    @r.get(base + "/files")
    def list_files():
        from app.services import logview as svc

        files = svc.list_log_files()
        return JSONResponse({"files": files, "count": len(files), "total": len(files)})

    @r.get(base + "/list")
    def list_alias():
        return list_files()

    @r.get(base + "/sources")
    def sources_alias():
        return list_files()

    @r.get(base + "/levels")
    def levels():
        from app.services import logview as svc

        return JSONResponse({"levels": list(svc.VALID_LEVELS), "count": len(svc.VALID_LEVELS)})

    @r.get(base + "/health")
    def health():
        return JSONResponse({"status": "ok"})


for _base in _PATHS:
    _register(router, _base)


# Prefixed routers for explicit mounting compatibility.
logview_router = APIRouter(prefix="/api/logview", tags=["logview"])
logs_router = APIRouter(prefix="/api/logs", tags=["logs"])
system_logs_router = APIRouter(prefix="/api/system/logs", tags=["logs"])


def _make_prefixed(r: APIRouter) -> None:
    @r.get("")
    def _tail(request: Request):
        return _do_tail()

    @r.get("/")
    def _tail_slash(request: Request):
        return _do_tail()

    @r.get("/tail")
    def _tail_path(
        request: Request,
        lines: Optional[int] = Query(default=None),
        level: Optional[str] = Query(default=None),
        min_level: Optional[str] = Query(default=None),
        search: Optional[str] = Query(default=None),
        source: Optional[str] = Query(default=None),
    ):
        wanted = _coerce_lines(lines)
        return _do_tail(lines=wanted, level=level, min_level=min_level, search=search, source=source)

    @r.get("/files")
    def _files():
        from app.services import logview as svc

        files = svc.list_log_files()
        return {"files": files, "count": len(files), "total": len(files)}

    @r.get("/levels")
    def _levels():
        from app.services import logview as svc

        return {"levels": list(svc.VALID_LEVELS)}

    @r.get("/health")
    def _health():
        return {"status": "ok"}


for _r in (logview_router, logs_router, system_logs_router):
    _make_prefixed(_r)


# Compatibility aliases.
logview_api_router = router
logs_api_router = router
api_router = router
system_log_router = router
server_logs_router = router
