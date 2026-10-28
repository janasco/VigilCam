"""Backup compare REST endpoints (diff two backup manifests).

No ``app/main.py`` changes are required: the application auto-loads any
``app.api`` module exposing ``router``.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import JSONResponse

import app.services.backup_compare as svc

router = APIRouter(tags=["backup-compare"])


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
    for nested in ("options", "params", "data", "body", "backup", "backups", "compare"):
        nested_val = data.get(nested)
        if isinstance(nested_val, dict):
            for k, v in nested_val.items():
                data.setdefault(k, v)
    return data


def _pick(data: dict, *keys: str) -> Any:
    for k in keys:
        v = data.get(k)
        if v is not None and (not isinstance(v, str) or v.strip()):
            return v
    return None


def _resolve_ids(data: dict, first_path: str | None = None, second_path: str | None = None) -> tuple[Any, Any, Any]:
    first = _pick(
        data,
        "first_id", "first", "a", "backup_a", "backup_first",
        "source_id", "source", "from_id", "from",
        "backup_id_a", "left_id", "left",
    )
    second = _pick(
        data,
        "second_id", "second", "b", "backup_b", "backup_second",
        "target_id", "target", "to_id", "to",
        "backup_id_b", "right_id", "right",
    )
    # Fallback single-key style: {"backup_id": ..., "against": ...}.
    if first is None:
        first = _pick(data, "backup_id", "backupId", "backup", "id")
    if second is None:
        second = _pick(data, "against", "other_id", "other", "compare_with", "with")
    if first is None and first_path is not None:
        first = first_path
    if second is None and second_path is not None:
        second = second_path
    backup_dir = _pick(data, "backup_dir", "dir", "backups_dir")
    return first, second, backup_dir


async def _do_compare(data: dict, first_path: str | None = None, second_path: str | None = None) -> dict:
    first, second, backup_dir = _resolve_ids(data, first_path, second_path)
    if first is None or second is None:
        raise HTTPException(status_code=400, detail="Two backup ids are required (first_id and second_id)")
    try:
        result = svc.compare_backups(str(first), str(second), backup_dir=backup_dir)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Compare failed: {exc}")
    return result


async def _query_handler(
    request: Request,
    payload: Any = Body(default=None),
    first_id: Optional[str] = Query(default=None),
    second_id: Optional[str] = Query(default=None),
    first: Optional[str] = Query(default=None),
    second: Optional[str] = Query(default=None),
    against: Optional[str] = Query(default=None),
    backup_id: Optional[str] = Query(default=None),
) -> JSONResponse:
    data = _normalize(payload, request)
    for k, v in (
        ("first_id", first_id), ("second_id", second_id),
        ("first", first), ("second", second),
        ("against", against), ("backup_id", backup_id),
    ):
        if v is not None and data.get(k) is None:
            data[k] = v
    return JSONResponse(await _do_compare(data))


async def _diff_pair_handler(first_id: str, second_id: str, request: Request) -> dict:
    data = _normalize(None, request)
    return await _do_compare(data, first_path=first_id, second_path=second_id)


_QUERY_PATHS = [
    "/api/backup-compare",
    "/api/backup-compare/",
    "/api/backups-compare",
    "/api/backups-compare/",
    "/api/system/backup-compare",
    "/api/backup/compare",
    "/api/backups/compare",
    "/api/system/backup/compare",
    "/api/system/backups/compare",
]

for _p in _QUERY_PATHS:
    router.add_api_route(_p, _query_handler, methods=["GET"])
    router.add_api_route(_p, _query_handler, methods=["POST"])

_DIFF_PATHS = [
    "/api/backup/{first_id}/diff/{second_id}",
    "/api/backups/{first_id}/diff/{second_id}",
    "/api/system/backup/{first_id}/diff/{second_id}",
    "/api/system/backups/{first_id}/diff/{second_id}",
    "/api/backup-compare/{first_id}/diff/{second_id}",
    "/api/backup-compare/{first_id}/{second_id}",
]

for _p in _DIFF_PATHS:
    router.add_api_route(_p, _diff_pair_handler, methods=["GET"])


@router.get("/api/backup-compare/health")
@router.get("/api/backups-compare/health")
@router.get("/api/backup/compare/health")
def backup_compare_health():
    try:
        from app.services import backup as _backup_mod

        count = len(_backup_mod.list_backups())
    except Exception:  # noqa: BLE001
        count = 0
    return {"status": "ok", "backups": count, "count": count}


# Compatibility aliases.
backup_compare_router = router
compare_router = router
backup_diff_router = router
api_router = router

__all__ = [
    "router",
    "backup_compare_router",
    "compare_router",
    "backup_diff_router",
    "api_router",
]
