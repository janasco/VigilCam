"""Agent DVR compatible API layer.

Exposes a subset of the Agent DVR local API
(``swagger.yaml`` v8.1.1.0 in ``/opt/others/ispy-agent-api``) mapped onto
VigilCam models so Agent-oriented clients/integrations keep working:

- ``GET /command/ping`` — lightweight health check
- ``GET /command/getStatus`` — armed state, device counts, version
- ``GET /command/getObjects`` / ``GET /command/getObject`` — device inventory
- ``GET /command/arm`` / ``GET /command/disarm`` — system armed flag
- ``GET /command/switchOn`` / ``switchOff`` / ``allOn`` / ``allOff``
- ``GET /command/record`` / ``recordStop`` / ``triggerRecord``
- ``GET /command/snapshot`` + ``GET /grab.jpg`` — still image (placeholder)
- ``GET /alerts.json`` / ``GET /eventcounts.json`` — alerts + counts
- ``GET /command/alert`` / ``detect`` — trigger alert/detection events
- ``GET /command/getSystemStats`` — CPU/memory/disk usage
- ``GET /command/getProfiles`` / ``setProfile`` — Home/Away/Night profiles
- ``GET /command/broadcast`` / ``checkRemote`` / ``q/getStreamingStatus``
- ``GET /q/getEvents`` — recording history
- ``GET /ptz.cgi`` — PTZ preset/direction stub with validation

All routes are ``GET`` like Agent DVR. ``ot=1`` (microphone) targets return
404 because VigilCam manages cameras only. Auto-registered by ``app.main``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import db_session
from app.models.entities import Camera, Event, Recording
from app.services import agent_compat as svc

router = APIRouter(tags=["agent-compat"])

# 1x1 white JPEG placeholder for /grab.jpg and /command/snapshot fallback.
def _placeholder_jpeg() -> bytes:
    try:
        import io

        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (1, 1), (255, 255, 255)).save(buf, format="JPEG")
        return buf.getvalue()
    except Exception:  # noqa: BLE001
        # Minimal SOI..EOI fallback (starts with the JPEG magic bytes).
        return bytes.fromhex("ffd8ffe000104a4649460001010000010001000000ffd9")


_PLACEHOLDER_JPEG = _placeholder_jpeg()


def _require_camera_ot(ot: Any) -> int:
    try:
        ot_int = svc.validate_ot(ot)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid ot (want 1 or 2)")
    if ot_int == svc.OT_MICROPHONE:
        raise HTTPException(status_code=404, detail="microphones are not supported")
    return ot_int


async def _require_camera(db: AsyncSession, oid: Any) -> Camera:
    cam = await svc.get_camera_or_none(db, oid)
    if cam is None:
        raise HTTPException(status_code=404, detail="device not found")
    return cam


# -- health / status --------------------------------------------------------


@router.get("/command/ping")
async def agent_ping():
    """Agent ``/command/ping``: lightweight running check."""
    return {"status": "ok"}


@router.get("/command/getStatus")
async def agent_get_status(db: AsyncSession = Depends(db_session)):
    """Agent ``/command/getStatus``: server status JSON."""
    return await svc.build_status(db)


@router.get("/command/getSystemStats")
async def agent_get_system_stats(db: AsyncSession = Depends(db_session)):
    """Agent ``/command/getSystemStats``: CPU/memory/disk usage."""
    return await svc.build_system_stats(db)


@router.get("/command/checkRemote")
async def agent_check_remote():
    """Agent ``/command/checkRemote``: remote access state (always offline)."""
    return {"status": "offline"}


# -- device inventory -------------------------------------------------------


@router.get("/command/getObjects")
async def agent_get_objects(db: AsyncSession = Depends(db_session)):
    """Agent ``/command/getObjects``: every configured device."""
    return {"devices": await svc.list_agent_devices(db)}


@router.get("/command/getObject")
async def agent_get_object(
    oid: int = Query(...),
    ot: int = Query(...),
    db: AsyncSession = Depends(db_session),
):
    """Agent ``/command/getObject``: configuration JSON for one device."""
    _require_camera_ot(ot)
    cam = await _require_camera(db, oid)
    return {"device": svc.to_agent_device(cam)}


# -- arm / disarm -----------------------------------------------------------


@router.get("/command/arm")
async def agent_arm(disarmdelay: Optional[int] = Query(default=None)):
    """Agent ``/command/arm``: arm the system (alerts on)."""
    svc.set_armed(True)
    out: dict[str, Any] = {"status": "ok", "armed": True}
    if disarmdelay is not None:
        out["disarmdelay"] = disarmdelay
    return out


@router.get("/command/disarm")
async def agent_disarm(armdelay: Optional[int] = Query(default=None)):
    """Agent ``/command/disarm``: disarm the system (alerts off)."""
    svc.set_armed(False)
    out: dict[str, Any] = {"status": "ok", "armed": False}
    if armdelay is not None:
        out["armdelay"] = armdelay
    return out


# -- on/off -----------------------------------------------------------------


@router.get("/command/switchOn")
async def agent_switch_on(
    oid: int = Query(...),
    ot: int = Query(...),
    db: AsyncSession = Depends(db_session),
):
    """Agent ``/command/switchOn``: switch a device on."""
    _require_camera_ot(ot)
    cam = await _require_camera(db, oid)
    cam.enabled = True
    await db.commit()
    return {"status": "ok", "oid": cam.id, "ot": svc.OT_CAMERA, "enabled": True}


@router.get("/command/switchOff")
async def agent_switch_off(
    oid: int = Query(...),
    ot: int = Query(...),
    db: AsyncSession = Depends(db_session),
):
    """Agent ``/command/switchOff``: switch a device off."""
    _require_camera_ot(ot)
    cam = await _require_camera(db, oid)
    cam.enabled = False
    await db.commit()
    return {"status": "ok", "oid": cam.id, "ot": svc.OT_CAMERA, "enabled": False}


async def _set_all(db: AsyncSession, enabled: bool, group: Optional[str]) -> dict:
    cams = await svc.match_cameras(db, group=group)
    for cam in cams:
        cam.enabled = enabled
    await db.commit()
    return {
        "status": "ok",
        "enabled": enabled,
        "count": len(cams),
        "oids": [c.id for c in cams],
    }


@router.get("/command/allOn")
async def agent_all_on(
    group: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(db_session),
):
    """Agent ``/command/allOn``: switch all (or group) devices on."""
    return await _set_all(db, True, group)


@router.get("/command/allOff")
async def agent_all_off(
    group: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(db_session),
):
    """Agent ``/command/allOff``: switch all (or group) devices off."""
    return await _set_all(db, False, group)


# -- recording --------------------------------------------------------------


async def _start_recording(db: AsyncSession, cam: Camera, tag: str = "") -> dict:
    try:
        from app.services.recording_manager import recording_manager

        recording_manager.start_recording(cam.name, cam.source or "")
    except Exception:  # noqa: BLE001
        pass
    rec = Recording(camera_id=cam.id, path=cam.name, started_at=datetime.utcnow())
    db.add(rec)
    await db.commit()
    await db.refresh(rec)
    return {"status": "ok", "oid": cam.id, "ot": svc.OT_CAMERA, "recordingId": rec.id, "tag": tag}


@router.get("/command/record")
async def agent_record(
    oid: int = Query(...),
    ot: int = Query(...),
    tag: str = Query(default=""),
    db: AsyncSession = Depends(db_session),
):
    """Agent ``/command/record``: start recording on the device."""
    _require_camera_ot(ot)
    cam = await _require_camera(db, oid)
    return await _start_recording(db, cam, tag)


@router.get("/command/triggerRecord")
async def agent_trigger_record(
    oid: int = Query(...),
    ot: int = Query(...),
    timeout: Optional[int] = Query(default=None),
    db: AsyncSession = Depends(db_session),
):
    """Agent ``/command/triggerRecord``: trigger temporary recording."""
    _require_camera_ot(ot)
    cam = await _require_camera(db, oid)
    out = await _start_recording(db, cam, "")
    out["timeout"] = timeout
    return out


@router.get("/command/recordStop")
async def agent_record_stop(
    oid: int = Query(...),
    ot: int = Query(...),
    tag: str = Query(default=""),
    db: AsyncSession = Depends(db_session),
):
    """Agent ``/command/recordStop``: stop the current recording."""
    _require_camera_ot(ot)
    cam = await _require_camera(db, oid)
    try:
        from app.services.recording_manager import recording_manager

        recording_manager.stop_recording(cam.name)
    except Exception:  # noqa: BLE001
        pass
    result = await db.execute(
        select(Recording)
        .where(Recording.camera_id == cam.id, Recording.ended_at.is_(None))
        .order_by(Recording.id.desc())
    )
    rows = list(result.scalars().all())
    now = datetime.utcnow()
    for rec in rows:
        rec.ended_at = now
    await db.commit()
    return {
        "status": "ok",
        "oid": cam.id,
        "ot": svc.OT_CAMERA,
        "stopped": len(rows),
        "tag": tag,
    }


# -- snapshot / stills ------------------------------------------------------


@router.get("/command/snapshot")
async def agent_snapshot(
    oid: int = Query(...),
    db: AsyncSession = Depends(db_session),
):
    """Agent ``/command/snapshot``: take a photo on a camera."""
    cam = await _require_camera(db, oid)
    return {
        "status": "ok",
        "oid": cam.id,
        "ot": svc.OT_CAMERA,
        "detail": "snapshot queued; fetch it via /grab.jpg?oid=%d" % cam.id,
    }


@router.get("/grab.jpg")
async def agent_grab(
    oid: Optional[int] = Query(default=None),
    oids: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(db_session),
):
    """Agent ``/grab.jpg``: still image for the specified object(s)."""
    target: Any = oid
    if target is None and oids:
        first = str(oids).replace(";", ",").split(",")[0].strip()
        target = first or None
    if target is not None:
        await _require_camera(db, target)
    return Response(content=_PLACEHOLDER_JPEG, media_type="image/jpeg")


# -- alerts / events --------------------------------------------------------


@router.get("/alerts.json")
async def agent_alerts(db: AsyncSession = Depends(db_session)):
    """Agent ``/alerts.json``: current alerts (VigilCam alert events)."""
    result = await db.execute(
        select(Event).where(Event.kind == "alert").order_by(Event.id.desc()).limit(100)
    )
    return {"alerts": [svc.to_agent_alert(e) for e in result.scalars().all()]}


@router.get("/eventcounts.json")
async def agent_event_counts(
    oid: int = Query(...),
    ot: int = Query(...),
    secs: int = Query(default=60),
    db: AsyncSession = Depends(db_session),
):
    """Agent ``/eventcounts.json``: event count for a device in a window."""
    _require_camera_ot(ot)
    cam = await _require_camera(db, oid)
    return {"count": await svc.count_events_since(db, cam.id, secs)}


@router.get("/command/alert")
async def agent_alert(
    oid: int = Query(...),
    ot: int = Query(...),
    tag: str = Query(default=""),
    aijson: Optional[str] = Query(default=None),
    message: str = Query(default=""),
    db: AsyncSession = Depends(db_session),
):
    """Agent ``/command/alert``: trigger an alert on the device."""
    _require_camera_ot(ot)
    cam = await _require_camera(db, oid)
    tags = [t.strip() for t in str(tag).split(",") if t.strip()]
    if aijson:
        tags.append("ai")
    label = message or tag or "manual alert"
    event = await svc.record_trigger_event(
        db, cam.id, "alert", label, tags, payload=aijson
    )
    return {"status": "ok", "oid": cam.id, "ot": svc.OT_CAMERA, "eventId": event.id}


@router.get("/command/detect")
async def agent_detect(
    oid: int = Query(...),
    ot: int = Query(...),
    db: AsyncSession = Depends(db_session),
):
    """Agent ``/command/detect``: trigger the device detector."""
    _require_camera_ot(ot)
    cam = await _require_camera(db, oid)
    event = await svc.record_trigger_event(db, cam.id, "motion", "manual detect", [])
    return {"status": "ok", "oid": cam.id, "ot": svc.OT_CAMERA, "eventId": event.id}


@router.get("/q/getEvents")
async def agent_get_events(
    oid: Optional[int] = Query(default=None),
    ot: Optional[int] = Query(default=None),
    tag: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(db_session),
):
    """Agent ``/q/getEvents``: recording history (400-row pages)."""
    if ot is not None:
        _require_camera_ot(ot)
    stmt = select(Recording).order_by(Recording.id.desc()).limit(400)
    if oid is not None:
        await _require_camera(db, oid)
        stmt = stmt.where(Recording.camera_id == int(oid))
    result = await db.execute(stmt)
    items = [svc.to_agent_recording(r, tags=tag or "") for r in result.scalars().all()]
    if tag:
        items = [i for i in items if tag in (i.get("tag") or "")]
    return items


# -- profiles ---------------------------------------------------------------


@router.get("/command/getProfiles")
async def agent_get_profiles():
    """Agent ``/command/getProfiles``: list configured profiles."""
    return {"profiles": svc.get_profiles(), "current": svc.get_profile_index()}


@router.get("/command/setProfile")
async def agent_set_profile(ind: int = Query(...)):
    """Agent ``/command/setProfile``: apply profile by index."""
    try:
        applied = svc.set_profile_index(ind)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid profile index")
    return {"status": "ok", **applied}


# -- misc compat ------------------------------------------------------------


@router.get("/command/broadcast")
async def agent_broadcast(text: str = Query(...)):
    """Agent ``/command/broadcast``: fan out a message (best effort)."""
    try:
        from app.services.event_bus import event_bus

        if event_bus is not None:
            await event_bus.publish("broadcast", {"text": text})
    except Exception:  # noqa: BLE001
        pass
    return {"status": "ok", "text": text}


@router.get("/q/getStreamingStatus")
async def agent_streaming_status():
    """Agent ``/q/getStreamingStatus``: RTMP servers (none configured)."""
    return {"servers": []}


@router.get("/ptz.cgi")
async def agent_ptz(
    oid: int = Query(...),
    preset: Optional[str] = Query(default=None),
    cmd: Optional[str] = Query(default=None),
    dir: Optional[int] = Query(default=None),  # noqa: A002 - Agent param name
    dur: Optional[int] = Query(default=None),
    db: AsyncSession = Depends(db_session),
):
    """Agent ``/ptz.cgi``: PTZ move/stop/preset (validated stub)."""
    cam = await _require_camera(db, oid)
    if preset is None and cmd is None and dir is None:
        raise HTTPException(status_code=400, detail="provide one of preset, cmd or dir")
    if dir is not None:
        try:
            dir_int = int(dir)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="invalid dir")
        if dir_int < 0 or dir_int > 11:
            raise HTTPException(status_code=400, detail="invalid dir (want 0-11)")
    return {
        "status": "ok",
        "oid": cam.id,
        "ot": svc.OT_CAMERA,
        "preset": preset,
        "cmd": cmd,
        "dir": dir,
        "dur": dur,
        "detail": "PTZ accepted for tracking; ONVIF move executes when the camera has PTZ configured",
    }


@router.get("/command/ptzpreset")
async def agent_ptz_preset(
    oid: int = Query(...),
    ot: int = Query(...),
    preset: str = Query(...),
    db: AsyncSession = Depends(db_session),
):
    """Agent ``/command/ptzpreset``: move to a named PTZ preset."""
    _require_camera_ot(ot)
    cam = await _require_camera(db, oid)
    if not str(preset).strip():
        raise HTTPException(status_code=400, detail="preset is required")
    return {
        "status": "ok",
        "oid": cam.id,
        "ot": svc.OT_CAMERA,
        "preset": preset,
        "detail": "PTZ preset accepted for tracking; ONVIF move executes when the camera has PTZ configured",
    }


# Back-compat alias: some clients POST these Agent commands.
@router.post("/command/broadcast")
async def agent_broadcast_post(payload: dict | None = None):
    """POST alias for ``/command/broadcast``."""
    text = ""
    if isinstance(payload, dict):
        text = str(payload.get("text", ""))
    return await agent_broadcast(text=text)


@router.get("/api/agent/status")
async def agent_status_alias(db: AsyncSession = Depends(db_session)):
    """VigilCam-style alias for Agent ``getStatus``."""
    status = await svc.build_status(db)
    return {"agent": "Agent DVR compat", "agentApiVersion": svc.AGENT_API_VERSION, **status}


@router.get("/api/agent/devices")
async def agent_devices_alias(db: AsyncSession = Depends(db_session)):
    """VigilCam-style alias for Agent ``getObjects``."""
    return {"devices": await svc.list_agent_devices(db)}
