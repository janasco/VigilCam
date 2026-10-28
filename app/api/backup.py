"""System backup/restore REST endpoints (DB + config export/import).

No ``app/main.py`` changes are required: the application auto-loads any
``app.api`` module exposing ``router``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import db_session
from app.services import backup as backup_service_module
from app.services.backup import BackupError

router = APIRouter(tags=["backup"])

_BASES = (
    "/api/backup",
    "/api/backups",
    "/api/system/backup",
    "/api/system/backups",
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
    for nested in ("options", "config", "params", "data", "body", "backup"):
        nested_val = data.get(nested)
        if isinstance(nested_val, dict):
            for k, v in nested_val.items():
                data.setdefault(k, v)
    if data.get("backup_id") is None:
        for k in ("id", "backupId", "backup", "name", "filename"):
            v = data.get(k)
            if isinstance(v, str) and v and not str(v).endswith(".zip"):
                data["backup_id"] = v
                break
            if isinstance(v, str) and v:
                data["backup_id"] = v
                break
    return data


async def _do_create(data: dict, db: AsyncSession) -> dict:  # noqa: ARG001
    try:
        return backup_service_module.create_backup(
            name=data.get("name") or data.get("label"),
            notes=data.get("notes") or data.get("description"),
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Backup failed: {exc}")


async def _do_list() -> Any:
    return backup_service_module.list_backups()


async def _do_get(backup_id: str) -> dict:
    info = backup_service_module.get_backup(backup_id)
    if info is None:
        raise HTTPException(status_code=404, detail="Backup not found")
    return info


async def _do_delete(backup_id: str) -> dict:
    ok = backup_service_module.delete_backup(backup_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Backup not found")
    return {"status": "deleted", "ok": True, "backup_id": backup_id}


def _do_download(backup_id: str) -> FileResponse:
    path = backup_service_module.backup_service.backup_path(backup_id)
    if path is None:
        raise HTTPException(status_code=404, detail="Backup not found")
    return FileResponse(
        path=str(path),
        media_type="application/zip",
        filename=Path(path).name,
    )


async def _do_restore(data: dict, db: AsyncSession) -> dict:  # noqa: ARG001
    backup_id = data.get("backup_id")
    if backup_id is None:
        raise HTTPException(status_code=400, detail="backup_id is required")
    restore_db = data.get("restore_db", True)
    restore_config = data.get("restore_config", True)
    if isinstance(restore_db, str):
        restore_db = restore_db.lower() not in ("0", "false", "no", "off")
    if isinstance(restore_config, str):
        restore_config = restore_config.lower() not in ("0", "false", "no", "off")
    try:
        return backup_service_module.restore_backup(
            backup_id,
            restore_db=bool(restore_db),
            restore_config=bool(restore_config),
        )
    except BackupError as exc:
        code = 404 if "not found" in str(exc).lower() else 400
        raise HTTPException(status_code=code, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Restore failed: {exc}")


def _register(r: APIRouter, base: str) -> None:
    @r.get(base, operation_id=f"list_backups_{base.strip('/').replace('/', '_')}")
    async def list_backups(db: AsyncSession = Depends(db_session)):  # noqa: ARG001
        return await _do_list()

    @r.get(base + "/", operation_id=f"list_backups_slash_{base.strip('/').replace('/', '_')}")
    async def list_backups_slash(db: AsyncSession = Depends(db_session)):  # noqa: ARG001
        return await _do_list()

    @r.post(base, operation_id=f"create_backup_{base.strip('/').replace('/', '_')}")
    async def create_backup(
        request: Request,
        payload: Any = Body(default=None),
        name: Optional[str] = Query(default=None),
        label: Optional[str] = Query(default=None),
        db: AsyncSession = Depends(db_session),
    ):
        data = _normalize(payload, request)
        if name is not None and data.get("name") is None:
            data["name"] = name
        if label is not None and data.get("label") is None:
            data["label"] = label
        return await _do_create(data, db)

    @r.post(base + "/", operation_id=f"create_backup_slash_{base.strip('/').replace('/', '_')}")
    async def create_backup_slash(
        request: Request,
        payload: Any = Body(default=None),
        name: Optional[str] = Query(default=None),
        label: Optional[str] = Query(default=None),
        db: AsyncSession = Depends(db_session),
    ):
        data = _normalize(payload, request)
        if name is not None and data.get("name") is None:
            data["name"] = name
        if label is not None and data.get("label") is None:
            data["label"] = label
        return await _do_create(data, db)

    @r.get(base + "/export", operation_id=f"export_backup_{base.strip('/').replace('/', '_')}")
    async def export_latest(db: AsyncSession = Depends(db_session)):  # noqa: ARG001
        items = backup_service_module.list_backups()
        if not items:
            info = await _do_create({}, db)
            return _do_download(str(info.get("backup_id") or info.get("id")))
        newest = items[0]
        return _do_download(str(newest.get("backup_id") or newest.get("id")))

    @r.post(base + "/restore", operation_id=f"restore_backup_{base.strip('/').replace('/', '_')}")
    async def restore_from_body(
        request: Request,
        payload: Any = Body(default=None),
        backup_id: Optional[str] = Query(default=None),
        db: AsyncSession = Depends(db_session),
    ):
        data = _normalize(payload, request)
        if backup_id is not None and data.get("backup_id") is None:
            data["backup_id"] = backup_id
        return await _do_restore(data, db)

    @r.post(base + "/import", operation_id=f"import_backup_{base.strip('/').replace('/', '_')}")
    async def import_upload(
        file: UploadFile = File(...),
        restore_config: bool = Query(default=True),
        db: AsyncSession = Depends(db_session),  # noqa: ARG001
    ):
        try:
            raw = await file.read()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"Could not read upload: {exc}")
        if not raw:
            raise HTTPException(status_code=400, detail="Uploaded file is empty")
        try:
            return backup_service_module.restore_backup(raw, restore_config=bool(restore_config))
        except BackupError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"Import failed: {exc}")

    @r.post(base + "/upload", operation_id=f"upload_backup_{base.strip('/').replace('/', '_')}")
    async def upload_backup(
        file: UploadFile = File(...),
        restore_config: bool = Query(default=True),
        db: AsyncSession = Depends(db_session),  # noqa: ARG001
    ):
        return await import_upload(file=file, restore_config=restore_config, db=db)

    @r.get(base + "/{backup_id}", operation_id=f"get_backup_{base.strip('/').replace('/', '_')}")
    async def get_backup(backup_id: str, db: AsyncSession = Depends(db_session)):  # noqa: ARG001
        return await _do_get(backup_id)

    @r.get(base + "/{backup_id}/download", operation_id=f"download_backup_{base.strip('/').replace('/', '_')}")
    async def download_backup(backup_id: str, db: AsyncSession = Depends(db_session)):  # noqa: ARG001
        return _do_download(backup_id)

    @r.delete(base + "/{backup_id}", operation_id=f"delete_backup_{base.strip('/').replace('/', '_')}")
    async def delete_backup(backup_id: str, db: AsyncSession = Depends(db_session)):  # noqa: ARG001
        return await _do_delete(backup_id)

    @r.post(base + "/{backup_id}/restore", operation_id=f"restore_backup_id_{base.strip('/').replace('/', '_')}")
    async def restore_by_id(
        backup_id: str,
        request: Request,
        payload: Any = Body(default=None),
        db: AsyncSession = Depends(db_session),
    ):
        data = _normalize(payload, request)
        data["backup_id"] = backup_id
        return await _do_restore(data, db)


for _base in _BASES:
    _register(router, _base)


# Compatibility aliases for varied import styles.
backup_router = router
backups_router = router
system_backup_router = router
router_backup = router

__all__ = [
    "router",
    "backup_router",
    "backups_router",
    "system_backup_router",
    "router_backup",
]
