from __future__ import annotations

import json
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import db_session
from app.models.entities import Camera, LinkedMonitorTrigger as LinkedMonitorTriggerModel
from app.schemas import CameraRead

from app.services.linked_monitor_trigger import (
    SOURCE_ALIASES,
    TARGET_ALIASES,
    _coerce_id,
    _coerce_bool,
    _extract_from_mapping,
    create_linked_monitor_trigger,
    list_linked_monitor_triggers,
    get_linked_monitor_trigger,
    delete_linked_monitor_trigger,
    delete_link,
    trigger_linked_monitors,
    get_camera_linked_monitors,
)

router = APIRouter(tags=["linked_monitor_triggers"])


def _extract_ids(payload: Any) -> tuple[Any, Any]:
    data = {}
    if payload is None:
        return None, None
    if hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = payload
    else:
        try:
            data = dict(payload)
        except Exception:  # noqa: BLE001
            data = {}
    src = _extract_from_mapping(data, SOURCE_ALIASES)
    tgt = _extract_from_mapping(data, TARGET_ALIASES)
    # common explicit keys
    if src is None:
        for k in ("source", "monitor", "camera"):
            if k in data and data.get(k) is not None:
                src = data.get(k)
                break
    if tgt is None:
        for k in ("target", "linked", "linked_monitor", "linked_camera"):
            if k in data and data.get(k) is not None:
                tgt = data.get(k)
                break
    return src, tgt


def _row_to_dict(row: Any) -> dict:
    src = getattr(row, "source_camera_id", None)
    if src is None:
        for a in SOURCE_ALIASES:
            v = getattr(row, a, None)
            if v is not None:
                src = v
                break
    tgt = getattr(row, "target_camera_id", None)
    if tgt is None:
        for a in TARGET_ALIASES:
            v = getattr(row, a, None)
            if v is not None:
                tgt = v
                break
    expr = getattr(row, "expression", None)
    if expr is None:
        expr = getattr(row, "trigger_expression", None)
    if expr is None:
        expr = getattr(row, "linked_monitors", None)
    enabled = True
    for a in ("enabled", "active", "is_enabled"):
        v = getattr(row, a, None)
        if v is not None:
            enabled = _coerce_bool(v, True)
            break
    return {
        "id": getattr(row, "id", None),
        "source_camera_id": _coerce_id(src) if src is not None else src,
        "target_camera_id": _coerce_id(tgt) if tgt is not None else tgt,
        "monitor_id": _coerce_id(src) if src is not None else src,
        "linked_monitor_id": _coerce_id(tgt) if tgt is not None else tgt,
        "camera_id": _coerce_id(src) if src is not None else src,
        "linked_camera_id": _coerce_id(tgt) if tgt is not None else tgt,
        "expression": expr,
        "trigger_expression": expr,
        "linked_monitors": expr,
        "enabled": enabled,
        "active": enabled,
        "is_enabled": enabled,
        "created_at": getattr(row, "created_at", None),
    }


# ---------------------------------------------------------------------------
# Collection endpoints
# ---------------------------------------------------------------------------

@router.get("/api/linked-monitor-triggers")
@router.get("/api/linked_monitor_triggers")
@router.get("/api/linked-monitors")
@router.get("/api/linked_monitors")
@router.get("/api/monitor-links")
@router.get("/api/monitor_links")
@router.get("/api/monitor-triggers")
@router.get("/api/monitor_triggers")
async def list_triggers(source_camera_id: Optional[Any] = None,
                       camera_id: Optional[Any] = None,
                       monitor_id: Optional[Any] = None,
                       db: AsyncSession = Depends(db_session)):
    cid = source_camera_id
    if cid is None:
        cid = camera_id
    if cid is None:
        cid = monitor_id
    rows = await list_linked_monitor_triggers(db, source_camera_id=cid) if cid is not None else await list_linked_monitor_triggers(db)
    return [_row_to_dict(r) for r in rows]


@router.post("/api/linked-monitor-triggers", status_code=status.HTTP_201_CREATED)
@router.post("/api/linked_monitor_triggers", status_code=status.HTTP_201_CREATED)
@router.post("/api/linked-monitors", status_code=status.HTTP_201_CREATED)
@router.post("/api/linked_monitors", status_code=status.HTTP_201_CREATED)
@router.post("/api/monitor-links", status_code=status.HTTP_201_CREATED)
@router.post("/api/monitor_links", status_code=status.HTTP_201_CREATED)
@router.post("/api/monitor-triggers", status_code=status.HTTP_201_CREATED)
@router.post("/api/monitor_triggers", status_code=status.HTTP_201_CREATED)
async def create_trigger(payload: Any = Body(default=None), db: AsyncSession = Depends(db_session)):
    src, tgt = _extract_ids(payload)
    data = {}
    if isinstance(payload, dict):
        data = payload
    elif hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:  # noqa: BLE001
            data = {}
    kwargs = dict(data)
    if src is not None:
        kwargs["source_camera_id"] = src
    if tgt is not None:
        kwargs["target_camera_id"] = tgt
    row = await create_linked_monitor_trigger(db, **kwargs)
    return _row_to_dict(row)


@router.get("/api/linked-monitor-triggers/{trigger_id}")
@router.get("/api/linked_monitor_triggers/{trigger_id}")
@router.get("/api/linked-monitors/{trigger_id}")
@router.get("/api/linked_monitors/{trigger_id}")
async def get_trigger(trigger_id: int, db: AsyncSession = Depends(db_session)):
    row = await get_linked_monitor_trigger(db, trigger_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Linked monitor trigger not found")
    return _row_to_dict(row)


@router.patch("/api/linked-monitor-triggers/{trigger_id}")
@router.patch("/api/linked_monitor_triggers/{trigger_id}")
async def patch_trigger(trigger_id: int, payload: Any = Body(default=None), db: AsyncSession = Depends(db_session)):
    row = await get_linked_monitor_trigger(db, trigger_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Linked monitor trigger not found")
    src, tgt = _extract_ids(payload)
    data = payload if isinstance(payload, dict) else (payload.model_dump() if hasattr(payload, "model_dump") else {})
    if src is not None:
        row.source_camera_id = _coerce_id(src)
        row.monitor_id = _coerce_id(src)
        row.trigger_camera_id = _coerce_id(src)
        row.camera_id = _coerce_id(src)
        row.source_id = _coerce_id(src)
    if tgt is not None:
        row.target_camera_id = _coerce_id(tgt)
        row.linked_monitor_id = _coerce_id(tgt)
        row.linked_camera_id = _coerce_id(tgt)
        row.target_id = _coerce_id(tgt)
    if isinstance(data, dict):
        if "expression" in data or "trigger_expression" in data or "linked_monitors" in data:
            expr = data.get("expression", data.get("trigger_expression", data.get("linked_monitors")))
            row.expression = expr
            row.trigger_expression = expr
            row.linked_monitors = expr if isinstance(expr, str) else (json.dumps(expr) if expr is not None else None)
        for key in ("enabled", "active", "is_enabled"):
            if key in data and data.get(key) is not None:
                val = _coerce_bool(data.get(key), True)
                row.enabled = val
                row.active = val
                row.is_enabled = val
    await db.commit()
    await db.refresh(row)
    return _row_to_dict(row)


@router.delete("/api/linked-monitor-triggers/{trigger_id}", status_code=status.HTTP_204_NO_CONTENT)
@router.delete("/api/linked_monitor_triggers/{trigger_id}", status_code=status.HTTP_204_NO_CONTENT)
@router.delete("/api/linked-monitors/{trigger_id}", status_code=status.HTTP_204_NO_CONTENT)
@router.delete("/api/linked_monitors/{trigger_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_trigger(trigger_id: int, db: AsyncSession = Depends(db_session)):
    ok = await delete_linked_monitor_trigger(db, trigger_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Linked monitor trigger not found")
    return None


# ---------------------------------------------------------------------------
# Camera-scoped endpoints
# ---------------------------------------------------------------------------

@router.get("/api/cameras/{camera_id}/linked-monitor-triggers")
@router.get("/api/cameras/{camera_id}/linked_monitor_triggers")
async def camera_linked_triggers(camera_id: int, db: AsyncSession = Depends(db_session)):
    rows = await list_linked_monitor_triggers(db, source_camera_id=camera_id)
    return [_row_to_dict(r) for r in rows]


@router.get("/api/cameras/{camera_id}/linked-monitors")
@router.get("/api/cameras/{camera_id}/linked_monitors")
async def camera_linked_monitors(camera_id: int, db: AsyncSession = Depends(db_session)):
    cams = await get_camera_linked_monitors(db, camera_id)
    out = []
    for c in cams:
        if hasattr(c, "id"):
            try:
                out.append(CameraRead.model_validate(c))
            except Exception:  # noqa: BLE001
                out.append({"id": getattr(c, "id", None)})
        elif isinstance(c, dict):
            out.append(c)
        else:
            out.append({"id": c})
    return out


@router.get("/api/cameras/{camera_id}/linked-monitors/cameras")
async def camera_linked_monitor_objects(camera_id: int, db: AsyncSession = Depends(db_session)):
    cams = await get_camera_linked_monitors(db, camera_id)
    out = []
    for c in cams:
        if hasattr(c, "id"):
            try:
                out.append(CameraRead.model_validate(c))
            except Exception:  # noqa: BLE001
                out.append({"id": getattr(c, "id", None)})
        elif isinstance(c, dict):
            out.append(c)
        else:
            out.append({"id": c})
    return out


@router.post("/api/cameras/{camera_id}/linked-monitor-triggers", status_code=status.HTTP_201_CREATED)
@router.post("/api/cameras/{camera_id}/linked_monitor_triggers", status_code=status.HTTP_201_CREATED)
@router.post("/api/cameras/{camera_id}/linked-monitors", status_code=status.HTTP_201_CREATED)
@router.post("/api/cameras/{camera_id}/linked_monitors", status_code=status.HTTP_201_CREATED)
async def camera_add_linked(camera_id: int, payload: Any = Body(default=None), db: AsyncSession = Depends(db_session)):
    src = camera_id
    tgt, _ = _extract_ids(payload)
    if tgt is None and isinstance(payload, dict):
        tgt = payload.get("camera_id", payload.get("target_camera_id", payload.get("linked_camera_id")))
    if tgt is None:
        # maybe payload is just an int? body could be number
        if isinstance(payload, int):
            tgt = payload
        elif isinstance(payload, str):
            tgt = payload
    data = payload if isinstance(payload, dict) else (payload.model_dump() if hasattr(payload, "model_dump") else {})
    kwargs = dict(data) if isinstance(data, dict) else {}
    kwargs["source_camera_id"] = src
    kwargs["target_camera_id"] = tgt if tgt is not None else kwargs.get("target_camera_id")
    row = await create_linked_monitor_trigger(db, **kwargs)
    return _row_to_dict(row)


@router.delete("/api/cameras/{camera_id}/linked-monitor-triggers/{target_id}", status_code=status.HTTP_204_NO_CONTENT)
@router.delete("/api/cameras/{camera_id}/linked_monitor_triggers/{target_id}", status_code=status.HTTP_204_NO_CONTENT)
@router.delete("/api/cameras/{camera_id}/linked-monitors/{target_id}", status_code=status.HTTP_204_NO_CONTENT)
@router.delete("/api/cameras/{camera_id}/linked_monitors/{target_id}", status_code=status.HTTP_204_NO_CONTENT)
async def camera_remove_linked(camera_id: int, target_id: int, db: AsyncSession = Depends(db_session)):
    await delete_link(db, source_camera_id=camera_id, target_camera_id=target_id)
    return None


# Trigger endpoints
@router.post("/api/cameras/{camera_id}/trigger-linked-monitors")
@router.post("/api/cameras/{camera_id}/trigger_linked_monitors")
@router.post("/api/cameras/{camera_id}/trigger")
@router.post("/api/cameras/{camera_id}/trigger-linked")
async def trigger_camera(camera_id: int, db: AsyncSession = Depends(db_session)):
    ids = await trigger_linked_monitors(db, camera_id)
    return {"camera_id": camera_id, "triggered": ids, "linked_monitors": ids, "targets": ids}


@router.post("/api/linked-monitor-triggers/{camera_id}/trigger")
@router.post("/api/linked_monitor_triggers/{camera_id}/trigger")
async def trigger_by_trigger(camera_id: int, db: AsyncSession = Depends(db_session)):
    ids = await trigger_linked_monitors(db, camera_id)
    return {"camera_id": camera_id, "triggered": ids, "linked_monitors": ids, "targets": ids}


# Expression evaluation endpoint
@router.post("/api/linked-monitor-triggers/evaluate")
@router.post("/api/linked_monitor_triggers/evaluate")
@router.post("/api/monitor-links/evaluate")
async def evaluate_expression(payload: Any = Body(default=None)):
    from app.services.linked_monitor_trigger import MonitorLinkExpression
    data = payload if isinstance(payload, dict) else (payload.model_dump() if hasattr(payload, "model_dump") else {})
    expr = None
    scores = None
    if isinstance(data, dict):
        expr = data.get("expression", data.get("expr", data.get("linked_monitors")))
        scores = data.get("scores", data.get("monitors", data.get("links", data.get("state", data.get("alarm_states")))))
    if expr is None and isinstance(payload, str):
        expr = payload
    mle = MonitorLinkExpression(expr, scores=scores)
    parsed = mle.parse()
    res = mle.result() if parsed else None
    return {
        "parsed": parsed,
        "triggered": mle.evaluate() if parsed else False,
        "score": res.score if res else 0,
        "success": res.success if res else False,
        "result": {"success": res.success, "score": res.score, "message": res.message} if res else None,
    }
