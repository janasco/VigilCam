"""Cross-camera RECORDING trigger API.

Extends linked monitors with actual RECORDING fan-out. Read-only link
resolution lives in ``app.services.linked_monitor_trigger``; RECORDING
side-effects live in ``app.services.cross_record``. This router only
imports from those modules (it never modifies them).
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import db_session
from app.services.cross_record import (
    get_cross_record_targets,
    trigger_cross_recording,
)

router = APIRouter(tags=["cross_record"])


def _payload_dict(payload: Any) -> dict:
    if payload is None:
        return {}
    if isinstance(payload, dict):
        return payload
    if hasattr(payload, "model_dump"):
        try:
            return dict(payload.model_dump())
        except Exception:  # noqa: BLE001
            return {}
    try:
        return dict(payload)
    except Exception:  # noqa: BLE001
        return {}


def _extract_source(payload: dict, camera_id: Any = None) -> Any:
    if camera_id is not None:
        return camera_id
    from app.services.linked_monitor_trigger import SOURCE_ALIASES, _extract_from_mapping

    v = _extract_from_mapping(payload, SOURCE_ALIASES)
    if v is not None:
        return v
    for k in ("camera_id", "monitor_id", "source", "camera", "monitor", "id",
              "source_camera_id", "trigger_camera_id"):
        if payload.get(k) is not None:
            return payload.get(k)
    return None


def _result_payload(res: Any) -> dict:
    if hasattr(res, "to_dict"):
        try:
            return res.to_dict()
        except Exception:  # noqa: BLE001
            pass
    if isinstance(res, dict):
        return res
    return {"result": res}


# ---------------------------------------------------------------------------
# Target resolution (no recording side-effects)
# ---------------------------------------------------------------------------

@router.get("/api/cross-record/targets")
@router.get("/api/cross_record/targets")
@router.get("/api/cross-record-targets")
@router.get("/api/cross_record_targets")
@router.get("/api/cameras/{camera_id}/cross-record-targets")
@router.get("/api/cameras/{camera_id}/cross_record_targets")
async def cross_record_targets(
    camera_id: Optional[Any] = None,
    source_camera_id: Optional[Any] = None,
    monitor_id: Optional[Any] = None,
    db: AsyncSession = Depends(db_session),
):
    src = camera_id if camera_id is not None else source_camera_id
    if src is None:
        src = monitor_id
    targets = await get_cross_record_targets(db, src)
    return {"source_camera_id": src, "camera_id": src, "targets": targets,
            "linked_monitors": targets, "linked_cameras": targets}


# ---------------------------------------------------------------------------
# RECORDING fan-out
# ---------------------------------------------------------------------------

async def _do_trigger(db: AsyncSession, source: Any, payload: dict) -> dict:
    dry = payload.get("dry_run", payload.get("dryrun", payload.get("preview", False)))
    if isinstance(dry, str):
        dry = dry.strip().lower() in {"1", "true", "yes", "on"}
    res = await trigger_cross_recording(
        db,
        source,
        dry_run=bool(dry),
        duration_seconds=payload.get("duration_seconds", payload.get("duration",
                         payload.get("record_duration", payload.get("seconds")))),
        reason=payload.get("reason", payload.get("cause", payload.get("label"))),
        **{k: v for k, v in payload.items()
           if k not in {"duration_seconds", "duration", "record_duration",
                        "seconds", "reason", "cause", "label", "dry_run",
                        "dryrun", "preview"}},
    )
    return _result_payload(res)


@router.post("/api/cameras/{camera_id}/cross-record", status_code=status.HTTP_200_OK)
@router.post("/api/cameras/{camera_id}/cross_record", status_code=status.HTTP_200_OK)
@router.post("/api/cameras/{camera_id}/trigger-cross-record", status_code=status.HTTP_200_OK)
@router.post("/api/cameras/{camera_id}/trigger_cross_record", status_code=status.HTTP_200_OK)
@router.post("/api/cameras/{camera_id}/cross-record-trigger", status_code=status.HTTP_200_OK)
@router.post("/api/cameras/{camera_id}/trigger-recording", status_code=status.HTTP_200_OK)
@router.post("/api/cameras/{camera_id}/trigger_recording", status_code=status.HTTP_200_OK)
@router.post("/api/cameras/{camera_id}/record-cross", status_code=status.HTTP_200_OK)
async def camera_cross_record(
    camera_id: int,
    payload: Any = Body(default=None),
    dry_run: Optional[bool] = Query(default=None),
    db: AsyncSession = Depends(db_session),
):
    data = _payload_dict(payload)
    if dry_run is not None:
        data.setdefault("dry_run", dry_run)
    return await _do_trigger(db, camera_id, data)


@router.post("/api/cross-record/trigger", status_code=status.HTTP_200_OK)
@router.post("/api/cross_record/trigger", status_code=status.HTTP_200_OK)
@router.post("/api/cross-record", status_code=status.HTTP_200_OK)
@router.post("/api/cross_record", status_code=status.HTTP_200_OK)
@router.post("/api/cross-record-trigger", status_code=status.HTTP_200_OK)
@router.post("/api/cross_record_trigger", status_code=status.HTTP_200_OK)
@router.post("/api/linked-monitor-triggers/cross-record", status_code=status.HTTP_200_OK)
@router.post("/api/linked_monitor_triggers/cross_record", status_code=status.HTTP_200_OK)
async def cross_record_trigger(
    payload: Any = Body(default=None),
    db: AsyncSession = Depends(db_session),
):
    data = _payload_dict(payload)
    src = _extract_source(data)
    if src is None:
        raise HTTPException(status_code=422, detail="source_camera_id is required")
    return await _do_trigger(db, src, data)
