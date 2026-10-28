"""Manual storage area-to-area migration API router.

Explicit operator-driven file migration from one storage area (or
directory) to another, preserving relative paths. This is distinct from
the policy-driven auto-tiering in :mod:`app.api.tiering` (which moves
old segments hot -> archive by ``max_age_days`` / ``max_total_bytes``
policy only).

Auto-registered by ``app.main`` (it imports every ``app.api`` module and
includes ``router``), so no ``app/main.py`` edit is required.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request

from app.services.storage_migrate import StorageMigrateManager

router = APIRouter(tags=["storage_migrate"])

_SOURCE_ALIASES = (
    "source_root", "source_dir", "source_path", "source", "src",
    "src_root", "src_dir", "src_path",
    "from_root", "from_dir", "from_path", "from_location",
    "source_location", "source_folder", "source_directory",
    "source_storage", "origin", "origin_path", "origin_dir", "origin_root",
)
_DEST_ALIASES = (
    "dest_root", "dest_dir", "dest_path", "destination", "dst",
    "dst_root", "dst_dir", "dst_path",
    "to_root", "to_dir", "to_path", "to_location",
    "target", "target_root", "target_dir", "target_path",
    "target_location", "destination_dir", "destination_path",
    "destination_root", "dest_location", "dest_folder", "dest_directory",
)
_SOURCE_ID_ALIASES = (
    "source_area_id", "source_id", "src_id", "src_area_id",
    "from_id", "from_area_id", "origin_id", "origin_area_id",
)
_DEST_ID_ALIASES = (
    "dest_area_id", "dest_id", "dst_id", "dst_area_id",
    "to_id", "to_area_id", "target_id", "target_area_id",
    "destination_id", "destination_area_id",
)


def _normalize(payload: Any, request: Optional[Request] = None) -> dict:
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

    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "filter", "filters"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)

    # Source aliases -> source_root (+ source).
    if data.get("source_root") is None:
        for k in _SOURCE_ALIASES:
            if data.get(k) is not None:
                data["source_root"] = data.get(k)
                break
    if data.get("source_root") is None and data.get("path") is not None and data.get("dest_root") is not None:
        data["source_root"] = data.get("path")
    if data.get("source") is None and data.get("source_root") is not None:
        data["source"] = data.get("source_root")

    # Dest aliases -> dest_root (+ destination).
    if data.get("dest_root") is None:
        for k in _DEST_ALIASES:
            if data.get(k) is not None:
                data["dest_root"] = data.get(k)
                break
    if data.get("destination") is None and data.get("dest_root") is not None:
        data["destination"] = data.get("dest_root")
    if data.get("dest_root") is None and data.get("destination") is not None:
        data["dest_root"] = data.get("destination")

    # Also accept bare "from"/"to" keys (reserved-ish names may arrive as query params).
    if data.get("source_root") is None and data.get("from") is not None:
        data["source_root"] = data.get("from")
    if data.get("dest_root") is None and data.get("to") is not None:
        data["dest_root"] = data.get("to")

    # Area-id aliases (resolved to paths via DB when available).
    for k in _SOURCE_ID_ALIASES:
        if data.get("source_area_id") is None and data.get(k) is not None:
            data["source_area_id"] = data.get(k)
            break
    for k in _DEST_ID_ALIASES:
        if data.get("dest_area_id") is None and data.get(k) is not None:
            data["dest_area_id"] = data.get(k)
            break
    # Generic area_id handling: single area_id + explicit path fills the missing side.
    if data.get("area_id") is not None:
        if data.get("source_area_id") is None and data.get("dest_root") is not None and data.get("source_root") is None:
            data["source_area_id"] = data.get("area_id")
        elif data.get("dest_area_id") is None and data.get("source_root") is not None and data.get("dest_root") is None:
            data["dest_area_id"] = data.get("area_id")

    # Dry-run aliases.
    if data.get("dry_run") is None:
        for k in ("dryRun", "dry-run", "dry", "simulate", "preview", "preview_only", "what_if"):
            if k in data:
                data["dry_run"] = bool(data.get(k))
                break

    # Copy/mode aliases.
    if data.get("copy") is None:
        for k in ("copy_files", "copyFiles", "keep_source", "keepSource", "keep", "preserve_source"):
            if k in data:
                data["copy"] = bool(data.get(k))
                break
    if data.get("mode") is None and data.get("operation") is not None:
        data["mode"] = data.get("operation")
    if isinstance(data.get("mode"), str) and data.get("mode").strip().lower() in ("copy", "cp", "duplicate"):
        data["copy"] = True

    # Overwrite aliases.
    if data.get("overwrite") is None:
        for k in ("over_write", "clobber", "replace", "force_overwrite", "allow_overwrite"):
            if k in data:
                data["overwrite"] = bool(data.get(k))
                break

    # Camera aliases.
    if data.get("camera_name") is None:
        for k in ("cameraName", "camera", "cam", "name", "camera_slug", "slug"):
            if data.get(k) is not None and not isinstance(data.get(k), dict):
                data["camera_name"] = data.get(k)
                break
    if data.get("camera_id") is None:
        for k in ("cameraId", "camera-id", "cam_id", "camId"):
            if data.get(k) is not None:
                data["camera_id"] = data.get(k)
                break
    return data


async def _resolve_area_paths(data: dict, db: Any = None) -> dict:
    """Fill source_root/dest_root from StorageArea ids when provided."""
    src_id = data.get("source_area_id")
    dst_id = data.get("dest_area_id")
    if src_id is None and dst_id is None:
        return data
    if db is None:
        return data
    try:
        from sqlalchemy import select as _select

        from app.models.entities import StorageArea as _StorageArea
    except Exception:  # noqa: BLE001
        return data
    try:
        result = await db.execute(_select(_StorageArea).order_by(_StorageArea.id))
        areas = list(result.scalars().all())
    except Exception:  # noqa: BLE001
        return data

    def _find(ref: Any) -> Any:
        if ref is None:
            return None
        try:
            ri = int(ref)
        except (TypeError, ValueError):
            ri = None
        for a in areas:
            if ri is not None and getattr(a, "id", None) == ri:
                return a
            if str(getattr(a, "name", "")) == str(ref):
                return a
            if getattr(a, "slug", None) is not None and str(a.slug) == str(ref):
                return a
        return None

    if data.get("source_root") is None and src_id is not None:
        area = _find(src_id)
        if area is not None:
            data["source_root"] = area.path
            data["source"] = area.path
    if data.get("dest_root") is None and dst_id is not None:
        area = _find(dst_id)
        if area is not None:
            data["dest_root"] = area.path
            data["destination"] = area.path
    return data


async def _refresh_area_usage(db: Any, data: dict) -> None:
    if db is None:
        return
    refs = [data.get("source_area_id"), data.get("dest_area_id")]
    refs = [r for r in refs if r is not None]
    if not refs:
        return
    try:
        import os as _os
        from pathlib import Path as _Path

        from sqlalchemy import select as _select

        from app.models.entities import StorageArea as _StorageArea
    except Exception:  # noqa: BLE001
        return
    try:
        result = await db.execute(_select(_StorageArea).order_by(_StorageArea.id))
        areas = list(result.scalars().all())
    except Exception:  # noqa: BLE001
        return

    def _find(ref: Any) -> Any:
        try:
            ri = int(ref)
        except (TypeError, ValueError):
            ri = None
        for a in areas:
            if ri is not None and getattr(a, "id", None) == ri:
                return a
            if str(getattr(a, "name", "")) == str(ref):
                return a
        return None

    try:
        for ref in refs:
            area = _find(ref)
            if area is None:
                continue
            total = 0
            p = _Path(str(area.path)).expanduser()
            if p.exists():
                if p.is_file():
                    try:
                        total = int(p.stat().st_size)
                    except OSError:
                        total = 0
                else:
                    for root, _dirs, files in _os.walk(p):
                        for fn in files:
                            try:
                                total += int((_Path(root) / fn).stat().st_size)
                            except OSError:
                                continue
            area.used_bytes = int(total)
            db.add(area)
        await db.commit()
    except Exception:  # noqa: BLE001
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass


def _needs_paths(data: dict) -> Optional[str]:
    if data.get("source_root") is None:
        return "source is required"
    if data.get("dest_root") is None:
        return "destination is required"
    return None


async def _do_status(data: dict, db: Any = None) -> Any:
    data = await _resolve_area_paths(data, db)
    return StorageMigrateManager().get_status(**data)


async def _do_preview(data: dict, db: Any = None) -> Any:
    data = await _resolve_area_paths(data, db)
    missing = _needs_paths(data)
    if missing is not None:
        raise HTTPException(status_code=422, detail=missing)
    payload = dict(data)
    payload["dry_run"] = True
    return StorageMigrateManager().run(**payload)


async def _do_migrate(data: dict, db: Any = None) -> Any:
    data = await _resolve_area_paths(data, db)
    missing = _needs_paths(data)
    if missing is not None:
        raise HTTPException(status_code=422, detail=missing)
    result = StorageMigrateManager().run(**data)
    if result.get("dry_run") is not True and result.get("ok") and db is not None:
        try:
            await _refresh_area_usage(db, data)
        except Exception:  # noqa: BLE001
            pass
    return result


async def _status_all(payload: Any = Body(default=None), request: Request = None):  # type: ignore[assignment]
    from app.core.database import get_session as _get_session

    db = None
    try:
        async for session in _get_session():
            db = session
            break
    except Exception:  # noqa: BLE001
        db = None
    try:
        return await _do_status(_normalize(payload, request), db)
    finally:
        try:
            if db is not None:
                await db.close()
        except Exception:  # noqa: BLE001
            pass


async def _preview_all(payload: Any = Body(default=None), request: Request = None):  # type: ignore[assignment]
    from app.core.database import get_session as _get_session

    db = None
    try:
        async for session in _get_session():
            db = session
            break
    except Exception:  # noqa: BLE001
        db = None
    try:
        return await _do_preview(_normalize(payload, request), db)
    finally:
        try:
            if db is not None:
                await db.close()
        except Exception:  # noqa: BLE001
            pass


async def _migrate_all(
    payload: Any = Body(default=None),  # type: ignore[assignment]
    request: Request = None,  # type: ignore[assignment]
    dry_run: Optional[bool] = Query(default=None),
    source_root: Optional[str] = Query(default=None),
    dest_root: Optional[str] = Query(default=None),
    source: Optional[str] = Query(default=None),
    destination: Optional[str] = Query(default=None),
    copy: Optional[bool] = Query(default=None),
    overwrite: Optional[bool] = Query(default=None),
):
    from app.core.database import get_session as _get_session

    db = None
    try:
        async for session in _get_session():
            db = session
            break
    except Exception:  # noqa: BLE001
        db = None
    try:
        data = _normalize(payload, request)
        if dry_run is not None and data.get("dry_run") is None:
            data["dry_run"] = bool(dry_run)
        if source_root is not None and data.get("source_root") is None:
            data["source_root"] = source_root
        if source is not None and data.get("source_root") is None:
            data["source_root"] = source
        if dest_root is not None and data.get("dest_root") is None:
            data["dest_root"] = dest_root
        if destination is not None and data.get("dest_root") is None:
            data["dest_root"] = destination
        if copy is not None and data.get("copy") is None:
            data["copy"] = bool(copy)
        if overwrite is not None and data.get("overwrite") is None:
            data["overwrite"] = bool(overwrite)
        if data.get("dry_run") is True:
            return await _do_preview(data, db)
        return await _do_migrate(data, db)
    finally:
        try:
            if db is not None:
                await db.close()
        except Exception:  # noqa: BLE001
            pass


_prefixes = [
    "/api/storage/migrate",
    "/api/storage-migrate",
    "/api/storage_migrate",
    "/api/storage/migration",
    "/api/storage/migrations",
    "/api/storage/move",
    "/api/storage/relocate",
    "/api/storage/transfer",
]

_status_suffixes = ["", "/", "/status", "/info", "/summary", "/state"]
_preview_suffixes = ["/preview", "/dry-run", "/dry_run", "/check", "/scan", "/candidates"]
_migrate_suffixes = ["/run", "/migrate", "/move", "/copy", "/relocate", "/transfer", "/execute", "/apply", "/process", "/enforce"]

_registered: set = set()
_op_counter = {"n": 0}


def _add(path: str, handler, methods=("GET", "POST")):
    for m in methods:
        key = (path, m)
        if key in _registered:
            continue
        try:
            _op_counter["n"] += 1
            router.add_api_route(path, handler, methods=[m], operation_id=f"{handler.__name__}_migrate_{_op_counter['n']}")
            _registered.add(key)
        except Exception:  # noqa: BLE001
            pass


for _prefix in _prefixes:
    for _suf in _status_suffixes:
        _p = _prefix if _suf in ("", "/") else f"{_prefix}{_suf}"
        if _suf in ("", "/"):
            _add(_p, _status_all, methods=("GET",))
            _add(_p, _migrate_all, methods=("POST", "PUT"))
        else:
            _add(_p, _status_all)
    for _suf in _preview_suffixes:
        _add(f"{_prefix}{_suf}", _preview_all, methods=("GET", "POST"))
    for _suf in _migrate_suffixes:
        _add(f"{_prefix}{_suf}", _migrate_all, methods=("GET", "POST", "PUT", "DELETE"))


# Compatibility aliases.
storage_migrate_router = router
router_storage_migrate = router
migrate_router = router
migration_router = router
storage_migration_router = router
storage_migrate_api_router = router

__all__ = ["router"]
