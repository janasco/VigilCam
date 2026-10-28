from __future__ import annotations

from typing import Any, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import db_session
from app.models.entities import Layout
from app.schemas import LayoutCreate, LayoutRead, LayoutUpdate

router = APIRouter(tags=["layouts"])


def _normalize_create(payload: Any) -> dict:
    data: dict = {}
    if payload is None:
        return data
    if hasattr(payload, "model_dump"):
        data = payload.model_dump()
    elif isinstance(payload, dict):
        data = dict(payload)
    else:
        try:
            data = dict(payload)
        except Exception:  # noqa: BLE001
            data = {}

    # name aliases
    if data.get("name") is None:
        for k in ("title", "layout_name", "layoutName", "name"):
            if data.get(k) is not None:
                data["name"] = data.get(k)
                break
    # json_layout aliases
    if data.get("json_layout") is None:
        for k in ("layout", "json", "data", "config", "content", "definition",
                  "structure", "value", "json_layout"):
            if k in data and data.get(k) is not None:
                data["json_layout"] = data.get(k)
                break
    # Wave 50: fold camera positions into layout JSON
    cp_keys = ("camera_positions", "cameraPositions", "cameras", "positions", "markers")
    cp = None
    for k in cp_keys:
        if k in data and data.get(k) is not None:
            cp = data.get(k)
            break
    if cp is not None:
        try:
            from app.services import floorplan_editor as fe
            base = data.get("json_layout")
            data["json_layout"] = fe.apply_camera_positions(base if base is not None else {}, cp,
                                                          width=data.get("width"), height=data.get("height"), merge=True)
        except Exception:  # noqa: BLE001
            if data.get("json_layout") is None:
                data["json_layout"] = {"camera_positions": cp}
    # unwrap nested
    for nested_key in ("payload", "options", "config", "params", "data", "body", "request"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    if data.get("name") is None:
        for k in ("title", "layout_name", "layoutName"):
            if data.get(k) is not None:
                data["name"] = data.get(k)
                break
    if data.get("json_layout") is None:
        for k in ("layout", "json", "data", "config", "content", "definition",
                  "structure", "value"):
            if k in data and data.get(k) is not None:
                data["json_layout"] = data.get(k)
                break
    # Wave 50: fold camera positions into layout JSON (post-unwrap)
    cp_keys = ("camera_positions", "cameraPositions", "cameras", "positions", "markers")
    cp = None
    for k in cp_keys:
        if k in data and data.get(k) is not None:
            cp = data.get(k)
            break
    if cp is not None:
        try:
            from app.services import floorplan_editor as fe
            base = data.get("json_layout")
            # avoid double-wrapping if base already has same positions? merge handles
            data["json_layout"] = fe.apply_camera_positions(base if base is not None else {}, cp,
                                                          width=data.get("width"), height=data.get("height"), merge=True)
        except Exception:  # noqa: BLE001
            if data.get("json_layout") is None:
                data["json_layout"] = {"camera_positions": cp}
    # Normalize nested camera positions already inside json_layout
    try:
        jl = data.get("json_layout")
        if isinstance(jl, dict):
            nested_cp = None
            for k in cp_keys:
                if k in jl and jl.get(k) is not None:
                    nested_cp = jl.get(k)
                    break
            if nested_cp is not None:
                from app.services import floorplan_editor as fe
                data["json_layout"] = fe.apply_camera_positions(jl, nested_cp,
                                                              width=data.get("width") or jl.get("width") or jl.get("image_width"),
                                                              height=data.get("height") or jl.get("height") or jl.get("image_height"),
                                                              merge=True)
    except Exception:  # noqa: BLE001
        pass
    return data


def _normalize_update(payload: Any) -> dict:
    data = _normalize_create(payload if payload is not None else {})
    # Allow partial: if name not provided, drop
    if payload is not None and hasattr(payload, "model_dump"):
        raw = payload.model_dump(exclude_unset=True)
        # merge resolved
        data = {**raw, **{k: v for k, v in data.items() if v is not None}}
    return data


def _to_read(l: Layout) -> LayoutRead:
    jl = getattr(l, "json_layout", None)
    return LayoutRead(
        id=l.id,
        name=getattr(l, "name", None),
        title=getattr(l, "name", None),
        layout_name=getattr(l, "name", None),
        slug=getattr(l, "slug", None),
        json_layout=jl,
        layout=jl,
        json=jl,
        data=jl,
        config=jl,
        content=jl,
        definition=jl,
        structure=jl,
        value=jl,
        created_at=getattr(l, "created_at", None),
        updated_at=getattr(l, "updated_at", None),
    )


@router.get("/api/layouts")
async def list_layouts(db: AsyncSession = Depends(db_session)):
    result = await db.execute(select(Layout).order_by(Layout.id))
    return [_to_read(x) for x in result.scalars().all()]


@router.post("/api/layouts", status_code=status.HTTP_201_CREATED)
async def create_layout(payload: Any = Body(default=None), db: AsyncSession = Depends(db_session)):
    if payload is not None and hasattr(payload, "resolved_name"):
        try:
            rn = payload.resolved_name()
            if rn is not None:
                pass
        except Exception:  # noqa: BLE001
            pass
    data = _normalize_create(payload if payload is not None else {})
    # payload object resolution
    name = data.get("name")
    jl = data.get("json_layout")
    if payload is not None and hasattr(payload, "resolved_name"):
        try:
            rn = payload.resolved_name()
            if rn is not None:
                name = rn
        except Exception:  # noqa: BLE001
            pass
    if payload is not None and hasattr(payload, "resolved_layout"):
        try:
            rl = payload.resolved_layout()
            if rl is not None:
                jl = rl
        except Exception:  # noqa: BLE001
            pass
    if not name:
        raise HTTPException(status_code=422, detail="name is required")

    from app.services.slugs import ensure_unique_slug, slugify_name

    _slug = None
    try:
        _slug = (data.get("slug") if isinstance(data, dict) else None) or getattr(payload, "slug", None)
    except Exception:  # noqa: BLE001
        _slug = None
    _slug = await ensure_unique_slug(db, Layout, slugify_name(_slug or name))
    layout = Layout(name=str(name), json_layout=jl, slug=_slug)
    db.add(layout)
    try:
        await db.commit()
    except Exception:  # noqa: BLE001
        await db.rollback()
        raise
    await db.refresh(layout)
    return _to_read(layout)


@router.get("/api/layouts/by-slug/{slug}")
async def get_layout_by_slug(slug: str, db: AsyncSession = Depends(db_session)):
    from app.services.slugs import get_by_slug

    layout = await get_by_slug(db, Layout, slug)
    if layout is None:
        raise HTTPException(status_code=404, detail="Layout not found")
    return _to_read(layout)


@router.get("/api/layouts/{layout_id}")
async def get_layout(layout_id: int, db: AsyncSession = Depends(db_session)):
    layout = await db.get(Layout, layout_id)
    if layout is None:
        # allow lookup by name? try name
        result = await db.execute(select(Layout).where(Layout.name == str(layout_id)))
        layout = result.scalars().first()
    if layout is None:
        raise HTTPException(status_code=404, detail="Layout not found")
    return _to_read(layout)


@router.patch("/api/layouts/{layout_id}")
@router.put("/api/layouts/{layout_id}")
async def update_layout(layout_id: int, payload: Any = Body(default=None), db: AsyncSession = Depends(db_session)):
    layout = await db.get(Layout, layout_id)
    if layout is None:
        result = await db.execute(select(Layout).where(Layout.name == str(layout_id)))
        layout = result.scalars().first()
    if layout is None:
        raise HTTPException(status_code=404, detail="Layout not found")

    data = _normalize_create(payload if payload is not None else {})
    name = data.get("name")
    jl = data.get("json_layout")
    if payload is not None and hasattr(payload, "resolved_name"):
        try:
            rn = payload.resolved_name()
            if rn is not None:
                name = rn
        except Exception:  # noqa: BLE001
            pass
    if payload is not None and hasattr(payload, "resolved_layout"):
        try:
            rl = payload.resolved_layout()
            # only override if provided? For update, resolved_layout may return None if not provided
            # We can't distinguish; check raw data
            pass
        except Exception:  # noqa: BLE001
            pass

    # Determine if json_layout explicitly provided
    raw = {}
    if payload is not None and hasattr(payload, "model_dump"):
        try:
            raw = payload.model_dump(exclude_unset=True)
        except Exception:  # noqa: BLE001
            raw = payload.model_dump()
    elif isinstance(payload, dict):
        raw = dict(payload)

    if "name" in raw or "title" in raw or "layout_name" in raw:
        if name is not None:
            layout.name = str(name)
    if "slug" in raw and raw.get("slug"):
        from app.services.slugs import ensure_unique_slug, slugify_name

        layout.slug = await ensure_unique_slug(db, Layout, slugify_name(raw["slug"]), exclude_id=layout.id)
    # for json_layout, only update if any layout key present
    layout_keys = {"json_layout", "layout", "json", "data", "config", "content", "definition", "structure", "value", "camera_positions", "cameraPositions", "cameras", "positions", "markers"}
    if layout_keys & set(raw.keys()) or (jl is not None and any(k in raw for k in ("camera_positions", "cameraPositions", "cameras", "positions", "markers"))):
        # resolve from raw
        cand = None
        for k in ("json_layout", "layout", "json", "data", "config", "content", "definition", "structure", "value"):
            if k in raw and raw.get(k) is not None:
                cand = raw.get(k)
                break
        if cand is None:
            # fold camera positions
            cp = None
            for k in ("camera_positions", "cameraPositions", "cameras", "positions", "markers"):
                if k in raw and raw.get(k) is not None:
                    cp = raw.get(k)
                    break
            if cp is not None:
                try:
                    from app.services import floorplan_editor as fe
                    base = raw.get("json_layout")
                    cand = fe.apply_camera_positions(base if base is not None else layout.json_layout, cp,
                                                  width=raw.get("width"), height=raw.get("height"), merge=True)
                except Exception:  # noqa: BLE001
                    cand = {"camera_positions": cp}
        # merge camera positions if provided alongside another layout payload
        cp = None
        for k in ("camera_positions", "cameraPositions", "cameras", "positions", "markers"):
            if k in raw and raw.get(k) is not None:
                cp = raw.get(k)
                break
        if cp is not None and cand is not None:
            try:
                from app.services import floorplan_editor as fe
                cand = fe.apply_camera_positions(cand, cp, width=raw.get("width"), height=raw.get("height"), merge=True)
            except Exception:  # noqa: BLE001
                pass
        if cand is None and "json_layout" in raw:
            cand = raw.get("json_layout")
        layout.json_layout = cand
    elif jl is not None and not raw:
        layout.json_layout = jl

    from datetime import datetime
    layout.updated_at = datetime.utcnow()
    try:
        await db.commit()
    except Exception:  # noqa: BLE001
        await db.rollback()
        raise
    await db.refresh(layout)
    return _to_read(layout)


@router.delete("/api/layouts/{layout_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_layout(layout_id: int, db: AsyncSession = Depends(db_session)):
    layout = await db.get(Layout, layout_id)
    if layout is None:
        result = await db.execute(select(Layout).where(Layout.name == str(layout_id)))
        layout = result.scalars().first()
    if layout is None:
        raise HTTPException(status_code=404, detail="Layout not found")
    await db.delete(layout)
    await db.commit()
    return None


# Extra alias paths for compatibility (singular / alternate / trailing slash).
@router.get("/api/layouts/")
async def list_layouts_slash(db: AsyncSession = Depends(db_session)):
    return await list_layouts(db)

@router.post("/api/layouts/", status_code=status.HTTP_201_CREATED)
async def create_layout_slash(payload: Any = Body(default=None), db: AsyncSession = Depends(db_session)):
    return await create_layout(payload, db)

@router.get("/api/layout")
@router.get("/api/layout/{layout_id}")
async def get_layout_singular(layout_id: int = None, db: AsyncSession = Depends(db_session)):
    if layout_id is None:
        # list
        return await list_layouts(db)
    return await get_layout(layout_id, db)

@router.post("/api/layout", status_code=status.HTTP_201_CREATED)
async def create_layout_singular(payload: Any = Body(default=None), db: AsyncSession = Depends(db_session)):
    return await create_layout(payload, db)

@router.patch("/api/layout/{layout_id}")
@router.put("/api/layout/{layout_id}")
@router.patch("/api/layouts/{layout_id}/")
@router.put("/api/layouts/{layout_id}/")
async def update_layout_alias(layout_id: int, payload: Any = Body(default=None), db: AsyncSession = Depends(db_session)):
    return await update_layout(layout_id, payload, db)

@router.delete("/api/layout/{layout_id}", status_code=status.HTTP_204_NO_CONTENT)
@router.delete("/api/layouts/{layout_id}/", status_code=status.HTTP_204_NO_CONTENT)
async def delete_layout_alias(layout_id: int, db: AsyncSession = Depends(db_session)):
    return await delete_layout(layout_id, db)