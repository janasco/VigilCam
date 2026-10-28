"""Group activity leaderboard router (auto-loaded by ``app.main``).

No edits to ``app/main.py`` are needed: this module exposes ``router``
and the pkgutil auto-loader in ``app.main`` picks it up.

Endpoints (tolerant of alias keys and query/body placement):

- ``GET /api/leaderboard`` -- full ranked board
- ``GET /api/leaderboard/top?limit=3`` -- top-N slice
- ``GET /api/leaderboard/rank?group=<name>`` -- rank for one group
- ``GET /api/leaderboard/status`` / ``/health``
- ``GET /api/leaderboard/{group}`` -- rank entry for a named group
- ``POST`` variants of the above

Hyphen/underscore and ``/api/groups/leaderboard`` style aliases are
registered for every endpoint.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, Query, Request

from app.api.deps import db_session

router = APIRouter(tags=["leaderboard"])


def _normalize(payload: Any, request: Request | None = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif isinstance(payload, str):
        data = {"group": payload}
    elif hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        try:
            data = {"group": bytes(payload).decode("utf-8", errors="replace")}
        except Exception:  # noqa: BLE001
            data = {}
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
    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "filter", "filters"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


async def _do_board(data: dict, db: Any = None) -> Any:
    from app.services import leaderboard as _svc

    kwargs = dict(data)
    session = db if hasattr(db, "execute") else None
    if session is not None:
        try:
            return await _svc.build_leaderboard_for_db(session, **kwargs)
        except Exception:  # noqa: BLE001
            pass
    return _svc.build_leaderboard(**kwargs)


async def _do_top(data: dict, db: Any = None) -> Any:
    board = await _do_board(data, db)
    entries = board.get("leaderboard", []) if isinstance(board, dict) else []
    counts = board.get("counts_by_group", {}) if isinstance(board, dict) else {}
    return {
        "ok": True,
        "success": True,
        "status": "ok",
        "count": len(entries),
        "total": board.get("total", 0) if isinstance(board, dict) else 0,
        "total_groups": board.get("total_groups", len(entries)) if isinstance(board, dict) else len(entries),
        "top": entries,
        "top_groups": entries,
        "leaderboard": entries,
        "rankings": entries,
        "entries": entries,
        "counts_by_group": counts,
        "by_group": counts,
    }


async def _do_rank(data: dict, db: Any = None, path_group: Any = None) -> Any:
    from app.services import leaderboard as _svc

    merged = dict(data)
    if path_group is not None and merged.get("group") is None:
        merged["group"] = str(path_group)
    # Resolve without pagination so the rank reflects the full board.
    lookup = {k: v for k, v in merged.items() if k not in ("limit", "top_n", "top", "n", "offset")}
    session = db if hasattr(db, "execute") else None
    if session is not None:
        try:
            board = await _svc.build_leaderboard_for_db(session, **lookup)
        except Exception:  # noqa: BLE001
            board = _svc.build_leaderboard(**lookup)
    else:
        board = _svc.build_leaderboard(**lookup)
    wanted = None
    for key in ("group", "group_id", "group_name", "name"):
        if merged.get(key) is not None and str(merged.get(key)).strip():
            wanted = str(merged.get(key)).strip()
            break
    if wanted is not None and path_group is not None:
        wanted = str(path_group)
    entry = _svc.get_group_rank(**dict(lookup, group=wanted)) if wanted else None
    if entry is None:
        return {
            "ok": False,
            "success": False,
            "status": "not_found",
            "group": wanted if wanted is not None else path_group,
            "rank": None,
            "count": 0,
            "total": 0,
            "total_groups": board.get("total_groups", 0) if isinstance(board, dict) else 0,
        }
    out = dict(entry)
    out.setdefault("ok", True)
    out.setdefault("success", True)
    out.setdefault("status", "ok")
    if isinstance(board, dict):
        out.setdefault("total_groups", board.get("total_groups", 0))
    return out


async def _board_dep(
    request: Request,
    payload: Any = Body(default=None),
    db: Any = Depends(db_session),
    group: Optional[str] = Query(default=None),
    limit: Optional[int] = Query(default=None),
    top_n: Optional[int] = Query(default=None),
    offset: Optional[int] = Query(default=None),
    order: Optional[str] = Query(default=None),
):
    data = _normalize(payload, request)
    if group is not None and data.get("group") is None:
        data["group"] = group
    if limit is not None and data.get("limit") is None:
        data["limit"] = limit
    if top_n is not None and data.get("top_n") is None:
        data["top_n"] = top_n
    if offset is not None and data.get("offset") is None:
        data["offset"] = offset
    if order is not None and data.get("order") is None:
        data["order"] = order
    # Single-group query returns that group's digest-style rank entry
    # plus the full board for context.
    if data.get("group") is not None and data.get("limit") is None and data.get("top_n") is None:
        rank = await _do_rank(data, db)
        if isinstance(rank, dict) and rank.get("ok"):
            return rank
    return await _do_board(data, db)


async def _top_dep(
    request: Request,
    payload: Any = Body(default=None),
    db: Any = Depends(db_session),
    limit: Optional[int] = Query(default=None),
    top_n: Optional[int] = Query(default=None),
    offset: Optional[int] = Query(default=None),
    order: Optional[str] = Query(default=None),
):
    data = _normalize(payload, request)
    if limit is not None and data.get("limit") is None:
        data["limit"] = limit
    if top_n is not None and data.get("top_n") is None:
        data["top_n"] = top_n
    if offset is not None and data.get("offset") is None:
        data["offset"] = offset
    if order is not None and data.get("order") is None:
        data["order"] = order
    return await _do_top(data, db)


async def _rank_dep(
    request: Request,
    payload: Any = Body(default=None),
    db: Any = Depends(db_session),
    group: Optional[str] = Query(default=None),
):
    data = _normalize(payload, request)
    if group is not None and data.get("group") is None:
        data["group"] = group
    return await _do_rank(data, db)


async def _rank_path_dep(
    group: str,
    request: Request,
    payload: Any = Body(default=None),
    db: Any = Depends(db_session),
):
    data = _normalize(payload, request)
    return await _do_rank(data, db, path_group=group)


async def _status_dep(request: Request):
    from app.services import leaderboard as _svc

    return _svc.status(**_normalize(None, request))


async def _health_dep():
    return {"ok": True, "service": "leaderboard"}


_prefixes = [
    "/api/leaderboard",
    "/api/leaderboards",
    "/api/group_leaderboard",
    "/api/group-leaderboard",
    "/api/groupLeaderboard",
    "/api/groups/leaderboard",
    "/api/groups_leaderboard",
    "/api/groups-leaderboard",
    "/api/activity/leaderboard",
    "/api/activity_leaderboard",
    "/api/activity-leaderboard",
]

_board_suffixes = ["", "/", "/board", "/full", "/all", "/summary", "/build"]
_top_suffixes = ["/top", "/top_groups", "/top-groups", "/ranking", "/rankings", "/leaders"]
_rank_suffixes = ["/rank", "/group_rank", "/group-rank"]
_status_suffixes = ["/status", "/health"]

_registered: set = set()
_op_counter = {"n": 0}


def _add(path: str, handler, methods=("GET", "POST")):
    for m in methods:
        key = (path, m)
        if key in _registered:
            continue
        try:
            _op_counter["n"] += 1
            router.add_api_route(
                path, handler, methods=[m],
                operation_id=f"{handler.__name__}_{_op_counter['n']}",
            )
            _registered.add(key)
        except Exception:  # noqa: BLE001
            pass


for _prefix in _prefixes:
    for _suf in _board_suffixes:
        _p = _prefix if _suf in ("", "/") else f"{_prefix}{_suf}"
        _add(_p, _board_dep)
    for _suf in _top_suffixes:
        _add(f"{_prefix}{_suf}", _top_dep)
    for _suf in _rank_suffixes:
        _add(f"{_prefix}{_suf}", _rank_dep)
    for _suf in _status_suffixes:
        if _suf == "/health":
            _add(f"{_prefix}{_suf}", _health_dep, methods=("GET",))
        else:
            _add(f"{_prefix}{_suf}", _status_dep, methods=("GET",))
    _add(f"{_prefix}/{{group}}", _rank_path_dep, methods=("GET",))


# Compatibility aliases.
leaderboard_router = router
router_leaderboard = router
leaderboards_router = router
group_leaderboard_router = router
leaderboard_api_router = router

__all__ = ["router"]
