"""Camera preset import/export REST endpoints.

No ``app/main.py`` changes are required: the application auto-loads any
``app.api`` module exposing ``router``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, UploadFile, File
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import db_session
from app.services import preset_io as preset_io_service

router = APIRouter(tags=["preset_io"])

_BASES = (
    "/api/preset-io",
    "/api/preset_io",
    "/api/presets-io",
    "/api/presets_io",
)


def _envelope(presets: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "version": preset_io_service.PRESET_IO_VERSION,
        "kind": preset_io_service.PRESET_KIND,
        "count": len(presets),
        "presets": presets,
    }


def _coerce_body(payload: Any, request: Request | None = None) -> Any:
    if payload is not None:
        if hasattr(payload, "model_dump"):
            try:
                return payload.model_dump()
            except Exception:  # noqa: BLE001
                pass
        return payload
    if request is not None:
        try:
            qp = dict(request.query_params)
        except Exception:  # noqa: BLE001
            qp = {}
        # Allow ?data=<json> or ?presets=<json> for convenience.
        for key in ("data", "presets", "cameras", "payload", "json"):
            if qp.get(key):
                return qp[key]
        if qp:
            # Single preset passed via query params.
            if qp.get("name") and qp.get("source"):
                return qp
    return []


async def _do_export(db: AsyncSession) -> Dict[str, Any]:
    presets = await preset_io_service.export_presets_db(db)
    return _envelope(presets)


async def _do_import(db: AsyncSession, payload: Any, update_existing: bool = False) -> Dict[str, Any]:
    items = preset_io_service.extract_presets(payload)
    if not items:
        raise HTTPException(status_code=400, detail="No presets found in import payload")
    result = await preset_io_service.import_presets_db(db, items, update_existing=update_existing)
    if result.get("count", 0) == 0:
        raise HTTPException(status_code=422, detail={"message": "No valid presets to import", "errors": result.get("errors", [])})
    return {
        "imported": result["count"],
        "count": result["count"],
        "cameras": result["imported"],
        "presets": result["imported"],
        "errors": result.get("errors", []),
    }


def _register(r: APIRouter, base: str) -> None:
    @r.get(base + "/export")
    async def export_presets(db: AsyncSession = Depends(db_session)):
        return await _do_export(db)

    @r.get(base + "/export/")
    async def export_presets_slash(db: AsyncSession = Depends(db_session)):
        return await _do_export(db)

    @r.get(base)
    async def export_presets_root(db: AsyncSession = Depends(db_session)):
        return await _do_export(db)

    @r.get(base + "/")
    async def export_presets_root_slash(db: AsyncSession = Depends(db_session)):
        return await _do_export(db)

    @r.post(base + "/import")
    async def import_presets(
        request: Request,
        payload: Any = Body(default=None),
        update_existing: bool = Query(default=False),
        db: AsyncSession = Depends(db_session),
    ):
        body = _coerce_body(payload, request)
        # Support file upload via form field is handled by dedicated endpoint;
        # here also accept {"file": {...}} style bodies transparently.
        return await _do_import(db, body, update_existing=update_existing)

    @r.post(base + "/import/")
    async def import_presets_slash(
        request: Request,
        payload: Any = Body(default=None),
        update_existing: bool = Query(default=False),
        db: AsyncSession = Depends(db_session),
    ):
        body = _coerce_body(payload, request)
        return await _do_import(db, body, update_existing=update_existing)

    @r.post(base)
    async def import_presets_root(
        request: Request,
        payload: Any = Body(default=None),
        update_existing: bool = Query(default=False),
        db: AsyncSession = Depends(db_session),
    ):
        body = _coerce_body(payload, request)
        return await _do_import(db, body, update_existing=update_existing)

    @r.post(base + "/validate")
    async def validate_presets(request: Request, payload: Any = Body(default=None)):
        body = _coerce_body(payload, request)
        items = preset_io_service.extract_presets(body)
        results = [
            {"index": i, "valid": preset_io_service.validate_preset(p)}
            for i, p in enumerate(items)
        ]
        valid = [i for i, res in enumerate(results) if res["valid"]]
        return {
            "count": len(items),
            "valid": len(valid),
            "invalid": len(items) - len(valid),
            "results": results,
        }


for _base in _BASES:
    _register(router, _base)


# File-upload import (registered once to avoid duplicate operation IDs).
@router.post("/api/preset-io/import-file")
@router.post("/api/preset_io/import-file")
async def import_presets_file(
    file: UploadFile = File(...),
    update_existing: bool = Query(default=False),
    db: AsyncSession = Depends(db_session),
):
    try:
        raw = await file.read()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Could not read upload: {exc}")
    try:
        import json as _json

        payload = _json.loads(raw.decode("utf-8", "ignore"))
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="Uploaded file is not valid JSON")
    return await _do_import(db, payload, update_existing=update_existing)


# Compatibility aliases for varied import styles.
preset_io_router = router
presets_io_router = router
presetIoRouter = router
camera_preset_io_router = router

__all__ = [
    "router",
    "preset_io_router",
    "presets_io_router",
    "presetIoRouter",
    "camera_preset_io_router",
]
