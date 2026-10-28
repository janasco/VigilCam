"""Layout export/import (sharing) REST endpoints.

No ``app/main.py`` changes are required: the application auto-loads any
``app.api`` module exposing ``router``.
"""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import db_session
from app.services import layout_share as layout_share_service

router = APIRouter(tags=["layout_share"])

_BASES = (
    "/api/layout-share",
    "/api/layout_share",
    "/api/layouts-share",
    "/api/layouts_share",
)


def _envelope(shares: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "version": layout_share_service.LAYOUT_SHARE_VERSION,
        "kind": layout_share_service.LAYOUT_SHARE_KIND,
        "count": len(shares),
        "layouts": shares,
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
        # Allow ?data=<json> or ?layouts=<json> for convenience.
        for key in ("data", "layouts", "layout", "share", "shares", "payload", "json"):
            if qp.get(key):
                return qp[key]
        if qp:
            # Single share passed via query params.
            if qp.get("name") or qp.get("title") or qp.get("layout_name"):
                return qp
    return []


async def _find_layout(db: AsyncSession, layout_id: Any) -> Any:
    from sqlalchemy import select as _select

    from app.models.entities import Layout as _Layout
    from app.services.slugs import get_by_slug

    row = await get_by_slug(db, _Layout, str(layout_id))
    if row is not None:
        return row
    try:
        result = await db.execute(_select(_Layout).where(_Layout.name == str(layout_id)))
        return result.scalars().first()
    except Exception:  # noqa: BLE001
        return None


async def _do_export(db: AsyncSession) -> Dict[str, Any]:
    shares = await layout_share_service.export_layouts_db(db)
    return _envelope(shares)


async def _do_export_one(db: AsyncSession, layout_id: Any) -> Dict[str, Any]:
    row = await _find_layout(db, layout_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Layout not found")
    share = layout_share_service.layout_to_share(row)
    try:
        share["id"] = getattr(row, "id", None)
    except Exception:  # noqa: BLE001
        pass
    return share


async def _do_import(db: AsyncSession, payload: Any, update_existing: bool = False) -> Dict[str, Any]:
    items = layout_share_service.extract_shares(payload)
    if not items:
        raise HTTPException(status_code=400, detail="No layouts found in import payload")
    result = await layout_share_service.import_layouts_db(
        db, items, update_existing=update_existing
    )
    if result.get("count", 0) == 0:
        raise HTTPException(
            status_code=422,
            detail={"message": "No valid layouts to import", "errors": result.get("errors", [])},
        )
    return {
        "imported": result["count"],
        "count": result["count"],
        "layouts": result["imported"],
        "shares": result["imported"],
        "errors": result.get("errors", []),
    }


def _register(r: APIRouter, base: str) -> None:
    @r.get(base + "/export")
    async def export_layouts(db: AsyncSession = Depends(db_session)):
        return await _do_export(db)

    @r.get(base + "/export/")
    async def export_layouts_slash(db: AsyncSession = Depends(db_session)):
        return await _do_export(db)

    @r.get(base + "/export/{layout_id}")
    async def export_layout_one(layout_id: str, db: AsyncSession = Depends(db_session)):
        return await _do_export_one(db, layout_id)

    @r.get(base)
    async def export_layouts_root(db: AsyncSession = Depends(db_session)):
        return await _do_export(db)

    @r.get(base + "/")
    async def export_layouts_root_slash(db: AsyncSession = Depends(db_session)):
        return await _do_export(db)

    @r.post(base + "/import")
    async def import_layouts(
        request: Request,
        payload: Any = Body(default=None),
        update_existing: bool = Query(default=False),
        db: AsyncSession = Depends(db_session),
    ):
        body = _coerce_body(payload, request)
        return await _do_import(db, body, update_existing=update_existing)

    @r.post(base + "/import/")
    async def import_layouts_slash(
        request: Request,
        payload: Any = Body(default=None),
        update_existing: bool = Query(default=False),
        db: AsyncSession = Depends(db_session),
    ):
        body = _coerce_body(payload, request)
        return await _do_import(db, body, update_existing=update_existing)

    @r.post(base)
    async def import_layouts_root(
        request: Request,
        payload: Any = Body(default=None),
        update_existing: bool = Query(default=False),
        db: AsyncSession = Depends(db_session),
    ):
        body = _coerce_body(payload, request)
        return await _do_import(db, body, update_existing=update_existing)

    @r.post(base + "/validate")
    async def validate_layouts(request: Request, payload: Any = Body(default=None)):
        body = _coerce_body(payload, request)
        items = layout_share_service.extract_shares(body)
        results = [
            {"index": i, "valid": layout_share_service.validate_share(p)}
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
@router.post("/api/layout-share/import-file")
@router.post("/api/layout_share/import-file")
async def import_layouts_file(
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
layout_share_router = router
layouts_share_router = router
layoutShareRouter = router
layout_share_api_router = router

__all__ = [
    "router",
    "layout_share_router",
    "layouts_share_router",
    "layoutShareRouter",
    "layout_share_api_router",
]
