from __future__ import annotations

import json
from typing import Any, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import db_session
from app.models.entities import Layout
from app.schemas import LayoutRead, LayoutUpdate
from app.services import floorplan_editor as fe

router = APIRouter(tags=["floorplan_editor"])


def _get_layout_json(layout: Layout) -> Any:
    return getattr(layout, "json_layout", None)


def _find_layout(db_layout_or_id, db=None):
    return None


async def _load_layout(db: AsyncSession, layout_id: Any) -> Layout:
    layout = None
    try:
        layout = await db.get(Layout, int(layout_id))
    except (TypeError, ValueError):
        layout = None
    if layout is None:
        result = await db.execute(select(Layout).where(Layout.name == str(layout_id)))
        layout = result.scalars().first()
    if layout is None:
        raise HTTPException(status_code=404, detail="Layout not found")
    return layout


def _extract_positions_payload(data: dict) -> Any:
    for key in ("camera_positions", "cameraPositions", "positions", "markers", "cameras",
                "devices", "coords", "coordinates", "points"):
        if key in data and data.get(key) is not None:
            return data.get(key)
    # nested
    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "input"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for key in ("camera_positions", "cameraPositions", "positions", "markers", "cameras"):
                if key in nested and nested.get(key) is not None:
                    return nested.get(key)
    return data.get("positions", data.get("camera_positions"))


def _normalize_request(payload: Any, request: Optional[Request] = None) -> dict:
    data = {}
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
    # unwrap nested
    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "input"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    if request is not None:
        try:
            for k, v in dict(request.query_params).items():
                data.setdefault(k, v)
        except Exception:  # noqa: BLE001
            pass
    return data


async def _save_positions(db, layout_id, payload, request=None):
    data = _normalize_request(payload, request)
    layout = await _load_layout(db, layout_id)
    base = _get_layout_json(layout)
    width = data.get("width", data.get("image_width", data.get("floorplan_width")))
    height = data.get("height", data.get("image_height", data.get("floorplan_height")))
    positions = _extract_positions_payload(data)
    if positions is None:
        # maybe whole payload is positions list
        if isinstance(payload, (list, tuple)):
            positions = list(payload)
        elif isinstance(data, list):
            positions = data
    new_json = fe.apply_camera_positions(base, positions if positions is not None else [], width=width, height=height, merge=True)
    layout.json_layout = new_json
    from datetime import datetime
    layout.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(layout)
    return {
        "ok": True,
        "status": "ok",
        "layout_id": layout.id,
        "name": layout.name,
        "camera_positions": fe.extract_camera_positions(layout.json_layout),
        "positions": fe.extract_camera_positions(layout.json_layout),
        "markers": fe.extract_camera_positions(layout.json_layout),
        "layout": layout.json_layout,
        "json_layout": layout.json_layout,
    }


# Info endpoints
@router.get("/api/floorplan/editor")
@router.get("/api/floorplan/editor/")
@router.get("/api/floorplan-editor")
async def floorplan_editor_info():
    return {"status": "ok", "service": "floorplan_editor", "endpoints": [
        "/api/floorplan/editor/positions", "/api/layouts/{id}/camera-positions"]}


@router.post("/api/floorplan/editor/normalize")
@router.post("/api/floorplan/editor/normalize-positions")
async def floorplan_editor_normalize(payload: Any = Body(default=None), request: Request = None):
    data = _normalize_request(payload, request)
    width = data.get("width", data.get("image_width", data.get("floorplan_width")))
    height = data.get("height", data.get("image_height", data.get("floorplan_height")))
    positions = _extract_positions_payload(data)
    if positions is None:
        positions = []
    norm = fe.normalize_positions(positions, width=width, height=height)
    return {"ok": True, "status": "ok", "positions": norm, "camera_positions": norm, "markers": norm}


@router.post("/api/floorplan/editor/preview")
async def floorplan_editor_preview(payload: Any = Body(default=None), request: Request = None):
    data = _normalize_request(payload, request)
    width = data.get("width", data.get("image_width", data.get("floorplan_width", 1.0)))
    height = data.get("height", data.get("image_height", data.get("floorplan_height", 1.0)))
    positions = _extract_positions_payload(data)
    if positions is None:
        # maybe layout provided
        layout = data.get("layout", data.get("json_layout"))
        if layout is not None:
            positions = fe.extract_camera_positions(layout)
    if positions is None:
        positions = []
    return fe.positions_to_preview(positions, width, height)


# Layout camera position endpoints (many aliases)
async def _get_positions(db, layout_id):
    layout = await _load_layout(db, layout_id)
    return {
        "ok": True,
        "status": "ok",
        "layout_id": layout.id,
        "name": layout.name,
        "camera_positions": fe.extract_camera_positions(layout.json_layout),
        "positions": fe.extract_camera_positions(layout.json_layout),
        "markers": fe.extract_camera_positions(layout.json_layout),
        "cameras": fe.extract_camera_positions(layout.json_layout),
        "layout": layout.json_layout,
        "json_layout": layout.json_layout,
    }


_PATHS = [
    "/api/layouts/{layout_id}/camera-positions",
    "/api/layouts/{layout_id}/cameras",
    "/api/layouts/{layout_id}/positions",
    "/api/layouts/{layout_id}/markers",
    "/api/layout/{layout_id}/camera-positions",
    "/api/layout/{layout_id}/cameras",
    "/api/layout/{layout_id}/positions",
    "/api/layout/{layout_id}/markers",
]


def _register():
    for path in _PATHS:
        # GET
        async def _get(layout_id: int, db: AsyncSession = Depends(db_session)):
            return await _get_positions(db, layout_id)
        router.get(path)(_get)
        # POST (save/merge)
        async def _post(layout_id: int, payload: Any = Body(default=None), request: Request = None, db: AsyncSession = Depends(db_session)):
            return await _save_positions(db, layout_id, payload, request)
        router.post(path)(_post)
        # PUT
        async def _put(layout_id: int, payload: Any = Body(default=None), request: Request = None, db: AsyncSession = Depends(db_session)):
            return await _save_positions(db, layout_id, payload, request)
        router.put(path)(_put)
        # PATCH
        async def _patch(layout_id: int, payload: Any = Body(default=None), request: Request = None, db: AsyncSession = Depends(db_session)):
            return await _save_positions(db, layout_id, payload, request)
        router.patch(path)(_patch)


_register()


# Also register a router alias module name
floorplan_editor_router = router
layout_editor_router = router

# Self-register into app
try:
    from app.main import app as _app
    _app.include_router(router)
except Exception:  # noqa: BLE001
    pass
