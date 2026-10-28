from __future__ import annotations

import os
from typing import Any, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import db_session
from app.models.entities import StorageArea
from app.services.storage_quota import StorageQuotaManager, _as_bytes, storage_quota_service

router = APIRouter(tags=["storage_quota"])


def _normalize(payload: Any, request: Optional[Request] = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif hasattr(payload, "model_dump"):
        data = payload.model_dump()
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

    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "area", "storage_area"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)

    # area id aliases
    if data.get("area_id") is None:
        for k in ("id", "storage_area_id", "areaId", "storageAreaId", "area"):
            if isinstance(data.get(k), (int, str)) and not isinstance(data.get(k), bool):
                # only treat scalar as id
                if not isinstance(data.get(k), dict):
                    data["area_id"] = data.get(k)
                    break
    # path aliases
    if data.get("path") is None:
        for k in ("storage_path", "root_path", "storage_root", "root", "directory", "folder", "dir", "location", "area_path"):
            if data.get(k) is not None:
                data["path"] = data.get(k)
                break
    # max bytes aliases
    if data.get("max_bytes") is None:
        for k in ("max_size", "max_total_bytes", "max", "capacity", "quota", "limit", "max_bytes"):
            if data.get(k) is not None:
                data["max_bytes"] = data.get(k)
                break
    # bytes aliases
    if data.get("bytes") is None:
        for k in ("size", "additional_bytes", "add_bytes", "amount", "delta", "increment", "extra_bytes", "required_bytes"):
            if data.get(k) is not None:
                data["bytes"] = data.get(k)
                break
    # dry run aliases
    if data.get("dry_run") is None:
        for k in ("dryRun", "dry-run", "dry", "simulate", "preview"):
            if k in data:
                data["dry_run"] = bool(data.get(k))
                break

    return data


async def _get_area(db: AsyncSession, value: Any) -> Optional[StorageArea]:
    if value is None:
        return None
    # scalar id or name
    try:
        aid = int(value)
        area = await db.get(StorageArea, aid)
        if area is not None:
            return area
    except (TypeError, ValueError):
        pass
    # name lookup
    try:
        result = await db.execute(select(StorageArea).where(StorageArea.name == str(value)))
        area = result.scalars().first()
        if area is not None:
            return area
    except Exception:  # noqa: BLE001
        pass
    # maybe dict/object
    if isinstance(value, dict):
        v = value.get("id") or value.get("area_id") or value.get("name")
        if v is not None and v is not value:
            return await _get_area(db, v)
    if hasattr(value, "id"):
        try:
            return await _get_area(db, getattr(value, "id"))
        except Exception:  # noqa: BLE001
            pass
    if hasattr(value, "name"):
        try:
            return await _get_area(db, getattr(value, "name"))
        except Exception:  # noqa: BLE001
            pass
    return None


async def _do_status(data: dict, db: AsyncSession) -> Any:
    area_id = data.get("area_id")
    area = None
    if area_id is not None:
        area = await _get_area(db, area_id)
    path = data.get("path")
    max_bytes = data.get("max_bytes")
    if area is not None:
        if path is None:
            path = getattr(area, "path", None)
        if max_bytes is None:
            max_bytes = getattr(area, "max_bytes", None)

    if area is None and area_id is None and path is None and max_bytes is None:
        # all areas summary
        return await storage_quota_service.status_async(db=db)

    mgr = StorageQuotaManager()
    result = await mgr.status_async(area=area, area_id=area_id, path=path, max_bytes=max_bytes, db=db)
    if area is not None:
        try:
            area.used_bytes = int(result.get("used_bytes") or 0)
        except Exception:  # noqa: BLE001
            pass
    return result


async def _do_check(data: dict, db: AsyncSession) -> Any:
    area_id = data.get("area_id")
    area = None
    if area_id is not None:
        area = await _get_area(db, area_id)
    path = data.get("path")
    max_bytes = data.get("max_bytes")
    additional = data.get("bytes")
    if area is not None:
        if path is None:
            path = getattr(area, "path", None)
        if max_bytes is None:
            max_bytes = getattr(area, "max_bytes", None)

    mgr = StorageQuotaManager()
    result = await mgr.check_async(area=area, area_id=area_id, path=path, max_bytes=max_bytes, bytes=additional, db=db)
    return result


async def _do_enforce(data: dict, db: AsyncSession) -> Any:
    area_id = data.get("area_id")
    area = None
    if area_id is not None:
        area = await _get_area(db, area_id)
    path = data.get("path")
    max_bytes = data.get("max_bytes")
    dry_run = bool(data.get("dry_run", False))
    if area is not None:
        if path is None:
            path = getattr(area, "path", None)
        if max_bytes is None:
            max_bytes = getattr(area, "max_bytes", None)

    mgr = StorageQuotaManager()
    result = await mgr.enforce_async(area=area, area_id=area_id, path=path, max_bytes=max_bytes, dry_run=dry_run, db=db)
    return result


async def _do_reconcile(data: dict, db: AsyncSession) -> Any:
    area_id = data.get("area_id")
    area = None
    if area_id is not None:
        area = await _get_area(db, area_id)
    path = data.get("path")
    max_bytes = data.get("max_bytes")
    dry_run = bool(data.get("dry_run", False))
    if area is not None:
        if path is None:
            path = getattr(area, "path", None)
        if max_bytes is None:
            max_bytes = getattr(area, "max_bytes", None)

    # If no specific area, reconcile all areas.
    if area is None and area_id is None and path is None:
        result_list = []
        res = await db.execute(select(StorageArea).order_by(StorageArea.id))
        areas = list(res.scalars().all())
        for a in areas:
            mgr = StorageQuotaManager()
            r = await mgr.reconcile_async(area=a, area_id=getattr(a, "id", None), path=getattr(a, "path", None), max_bytes=getattr(a, "max_bytes", None), dry_run=dry_run, db=db)
            result_list.append(r)
        total_used = sum(int(r.get("used_bytes") or 0) for r in result_list)
        return {
            "status": "completed",
            "reconciled": True,
            "dry_run": dry_run,
            "areas": result_list,
            "storage_areas": result_list,
            "results": result_list,
            "count": len(result_list),
            "total_used_bytes": total_used,
            "used_bytes": total_used,
        }

    mgr = StorageQuotaManager()
    result = await mgr.reconcile_async(area=area, area_id=area_id, path=path, max_bytes=max_bytes, dry_run=dry_run, db=db)
    return result


# ---------- handlers ----------

async def _status_all(payload=Body(default=None), request: Request = None, db: AsyncSession = Depends(db_session), area_id: Optional[int] = Query(default=None)):
    data = _normalize(payload, request)
    if area_id is not None:
        data.setdefault("area_id", area_id)
    return await _do_status(data, db)


async def _check_all(payload=Body(default=None), request: Request = None, db: AsyncSession = Depends(db_session), area_id: Optional[int] = Query(default=None)):
    data = _normalize(payload, request)
    if area_id is not None:
        data.setdefault("area_id", area_id)
    return await _do_check(data, db)


async def _enforce_all(payload=Body(default=None), request: Request = None, db: AsyncSession = Depends(db_session), area_id: Optional[int] = Query(default=None), dry_run: Optional[bool] = Query(default=None)):
    data = _normalize(payload, request)
    if area_id is not None:
        data.setdefault("area_id", area_id)
    if dry_run is not None:
        data["dry_run"] = bool(dry_run)
    return await _do_enforce(data, db)


async def _reconcile_all(payload=Body(default=None), request: Request = None, db: AsyncSession = Depends(db_session), area_id: Optional[int] = Query(default=None), dry_run: Optional[bool] = Query(default=None)):
    data = _normalize(payload, request)
    if area_id is not None:
        data.setdefault("area_id", area_id)
    if dry_run is not None:
        data["dry_run"] = bool(dry_run)
    return await _do_reconcile(data, db)


async def _status_area(area_id, payload=Body(default=None), request: Request = None, db: AsyncSession = Depends(db_session), dry_run: Optional[bool] = Query(default=None)):
    data = _normalize(payload, request)
    data["area_id"] = area_id
    return await _do_status(data, db)


async def _check_area(area_id, payload=Body(default=None), request: Request = None, db: AsyncSession = Depends(db_session)):
    data = _normalize(payload, request)
    data["area_id"] = area_id
    return await _do_check(data, db)


async def _enforce_area(area_id, payload=Body(default=None), request: Request = None, db: AsyncSession = Depends(db_session), dry_run: Optional[bool] = Query(default=None)):
    data = _normalize(payload, request)
    data["area_id"] = area_id
    if dry_run is not None:
        data["dry_run"] = bool(dry_run)
    return await _do_enforce(data, db)


async def _reconcile_area(area_id, payload=Body(default=None), request: Request = None, db: AsyncSession = Depends(db_session), dry_run: Optional[bool] = Query(default=None)):
    data = _normalize(payload, request)
    data["area_id"] = area_id
    if dry_run is not None:
        data["dry_run"] = bool(dry_run)
    return await _do_reconcile(data, db)


# Register routes under many candidate prefixes/aliases.
_prefixes = [
    "/api/storage_quota",
    "/api/storage-quota",
    "/api/storage/quota",
    "/api/storage/quotas",
    "/api/storagequota",
    "/api/storage-quotas",
]

_status_suffixes = ["", "status", "summary", "usage", "info", "state", "health", "overview"]
_check_suffixes = ["check", "can_store", "allowed", "allow", "has_space", "verify", "validate", "quota_check", "check_quota"]
_enforce_suffixes = ["enforce", "run", "apply", "cleanup", "prune", "execute", "process", "delete", "remove", "enforce_quota"]
_reconcile_suffixes = ["reconcile", "scan", "refresh", "update", "sync", "reconcile_quota"]
_area_suffixes_status = ["", "status", "summary", "usage", "info", "state", "health"]
_area_suffixes_check = ["check", "can_store", "allowed", "allow", "has_space", "verify", "validate", "quota_check", "check_quota"]
_area_suffixes_enforce = ["enforce", "run", "apply", "cleanup", "prune", "execute", "process", "delete", "remove", "enforce_quota"]
_area_suffixes_reconcile = ["reconcile", "scan", "refresh", "update", "sync", "reconcile_quota"]

_registered = set()


_op_counter = {"n": 0}
def _add(path: str, handler, methods=("GET", "POST")):
    for m in methods:
        key = (path, m)
        if key in _registered:
            continue
        try:
            _op_counter["n"] += 1
            router.add_api_route(path, handler, methods=[m], operation_id=f"{handler.__name__}_{_op_counter['n']}")
            _registered.add(key)
        except Exception:  # noqa: BLE001
            pass


for prefix in _prefixes:
    for suf in _status_suffixes:
        path = prefix if suf == "" else f"{prefix}/{suf}"
        _add(path, _status_all)
    for suf in _check_suffixes:
        _add(f"{prefix}/{suf}", _check_all, methods=("POST", "GET"))
    for suf in _enforce_suffixes:
        _add(f"{prefix}/{suf}", _enforce_all, methods=("POST", "GET"))
    for suf in _reconcile_suffixes:
        _add(f"{prefix}/{suf}", _reconcile_all, methods=("POST", "GET"))

    # area-specific by id (path param)
    for suf in _area_suffixes_status:
        path = f"{prefix}/areas/{{area_id}}" if suf == "" else f"{prefix}/areas/{{area_id}}/{suf}"
        # also support /{area_id}
        _add(path, _status_area)
        _add(f"{prefix}/{{area_id}}" if suf == "" else f"{prefix}/{{area_id}}/{suf}", _status_area)
    for suf in _area_suffixes_check:
        path = f"{prefix}/areas/{{area_id}}/{suf}"
        _add(path, _check_area, methods=("POST", "GET"))
        _add(f"{prefix}/{{area_id}}/{suf}", _check_area, methods=("POST", "GET"))
    for suf in _area_suffixes_enforce:
        path = f"{prefix}/areas/{{area_id}}/{suf}"
        _add(path, _enforce_area, methods=("POST", "GET"))
        _add(f"{prefix}/{{area_id}}/{suf}", _enforce_area, methods=("POST", "GET"))
    for suf in _area_suffixes_reconcile:
        path = f"{prefix}/areas/{{area_id}}/{suf}"
        _add(path, _reconcile_area, methods=("POST", "GET"))
        _add(f"{prefix}/{{area_id}}/{suf}", _reconcile_area, methods=("POST", "GET"))

# Self-register into app if already created.
try:
    from app.main import app as _app

    if _app is not None:
        try:
            _app.include_router(router)
        except Exception:  # noqa: BLE001
            pass
except Exception:  # noqa: BLE001
    pass

__all__ = ["router"]


# Compatibility aliases.
storage_quota_router = router
router_storage_quota = router
quota_router = router
storage_quota_api_router = router
