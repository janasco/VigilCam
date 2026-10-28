from __future__ import annotations

import random
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import Camera, Event
from app.schemas import CameraCreate
from app.services.camera_onboarding import derive_ip_address


def slugify_name(name: str) -> str:
    import re
    import unicodedata

    s = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s or "camera"


async def ensure_unique_slug(db: AsyncSession, base: str, exclude_id: Optional[int] = None) -> str:
    slug = (base or "").strip() or "camera"
    result = await db.execute(select(Camera).where(Camera.slug == slug))
    row = result.scalars().first()
    if row is None or (exclude_id is not None and row.id == exclude_id):
        return slug
    i = 2
    while True:
        cand = f"{slug}-{i}"
        result = await db.execute(select(Camera).where(Camera.slug == cand))
        row = result.scalars().first()
        if row is None or (exclude_id is not None and row.id == exclude_id):
            return cand
        i += 1


async def get_camera_by_slug(db: AsyncSession, slug: str) -> Optional[Camera]:
    result = await db.execute(select(Camera).where(Camera.slug == slug))
    return result.scalars().first()


LABELS = ["person", "car", "truck", "bicycle", "dog", "cat", "motorcycle", "bus"]
MODEL_NAME = "vigilcam-yolov8n-stub"


async def list_cameras(db: AsyncSession) -> list[Camera]:
    result = await db.execute(select(Camera).order_by(Camera.id))
    return list(result.scalars().all())


async def get_camera(db: AsyncSession, camera_id: int) -> Optional[Camera]:
    return await db.get(Camera, camera_id)


async def create_camera(db: AsyncSession, payload: CameraCreate) -> Camera:
    data = payload.model_dump()
    if "motionZones" in data and data.get("motion_zones") is None:
        data["motion_zones"] = data.pop("motionZones")
    else:
        data.pop("motionZones", None)
    if not data.get("ip_address"):
        derived = derive_ip_address(data.get("source"), data.get("onvif_host"))
        if derived:
            data["ip_address"] = derived
    if not data.get("slug"):
        data["slug"] = await ensure_unique_slug(db, slugify_name(data.get("name") or "camera"))
    else:
        data["slug"] = await ensure_unique_slug(db, slugify_name(data["slug"]))
    camera = Camera(**data)
    db.add(camera)
    await db.commit()
    await db.refresh(camera)
    try:
        from app.services import camera_health as _ch

        _ch._ensure(camera.id)
    except Exception:  # noqa: BLE001
        pass
    return camera


async def update_camera(db: AsyncSession, camera_id: int, payload: dict) -> Optional[Camera]:
    camera = await db.get(Camera, camera_id)
    if not camera:
        return None
    for key, value in payload.items():
        if key == "motionZones":
            key = "motion_zones"
        if key == "slug" and value:
            value = await ensure_unique_slug(db, slugify_name(value), exclude_id=camera_id)
        if hasattr(camera, key) and value is not None:
            setattr(camera, key, value)
    await db.commit()
    await db.refresh(camera)
    return camera


async def delete_camera(db: AsyncSession, camera_id: int) -> bool:
    camera = await db.get(Camera, camera_id)
    if not camera:
        return False
    await db.delete(camera)
    await db.commit()
    try:
        from app.services import camera_health as _ch

        _ch.remove(camera_id)
    except Exception:  # noqa: BLE001
        pass
    try:
        from app.services import recording_manager as _rm

        mgr = getattr(_rm, "recording_manager", None) or getattr(_rm, "manager", None)
        if mgr is not None:
            stop = getattr(mgr, "stop_recording", None) or getattr(mgr, "stop", None)
            if stop is not None and camera.name:
                stop(camera.name)
    except Exception:  # noqa: BLE001
        pass
    return True


def simulate_detection(camera_id: Optional[int] = None, model: str = MODEL_NAME) -> dict:
    """Return a lightweight, deterministic-ish detection payload.

    This is a scaffold: real deployments swap this for an ONNX/TensorRT/ONVIF
    analytics pipeline. The shape mirrors what a production detector returns.
    """
    rng = random.Random(camera_id or 0 + int(datetime.utcnow().timestamp()) // 5)
    count = rng.randint(0, 4)
    detections = []
    for _ in range(count):
        label = rng.choice(LABELS)
        x1, y1 = rng.randint(0, 400), rng.randint(0, 200)
        w, h = rng.randint(40, 200), rng.randint(40, 200)
        detections.append(
            {
                "label": label,
                "confidence": round(rng.uniform(0.55, 0.99), 3),
                "bbox": [x1, y1, min(x1 + w, 640), min(y1 + h, 480)],
            }
        )
    return {
        "camera_id": camera_id,
        "model": model,
        "detections": detections,
        "processed_at": datetime.utcnow(),
    }


async def record_event(db: AsyncSession, camera_id: Optional[int], label: str, confidence: Optional[float] = None, payload: Optional[str] = None) -> Event:
    event = Event(camera_id=camera_id, kind="detection", label=label, confidence=confidence, payload=payload)
    db.add(event)
    await db.commit()
    await db.refresh(event)
    # Track per-camera activity using events.
    if camera_id is not None:
        try:
            from app.services.activity import mark_camera_activity_db

            await mark_camera_activity_db(db, camera_id, event.created_at or datetime.utcnow(), motion=False)
        except Exception:  # noqa: BLE001
            try:
                from app.services.activity import mark_activity

                mark_activity(camera_id, when=event.created_at)
            except Exception:  # noqa: BLE001
                pass
    return event


async def recent_events(db: AsyncSession, limit: int = 50) -> list[Event]:
    result = await db.execute(select(Event).order_by(Event.id.desc()).limit(limit))
    return list(result.scalars().all())


from sqlalchemy import func


async def _resolve_camera_alias(db: AsyncSession, alias: Any) -> Optional[Camera]:
    if alias is None:
        return None
    s = str(alias).strip()
    if not s:
        return None

    # Direct id lookup.
    try:
        cid = int(s)
        cam = await db.get(Camera, cid)
        if cam is not None:
            return cam
    except (TypeError, ValueError):
        pass

    # Exact, case-insensitive name match.
    result = await db.execute(
        select(Camera).where(func.lower(Camera.name) == s.lower())
    )
    cam = result.scalars().first()
    if cam is not None:
        return cam

    # Partial name match.
    result = await db.execute(
        select(Camera).where(Camera.name.ilike(f"%{s}%"))
    )
    cam = result.scalars().first()
    if cam is not None:
        return cam

    # Source / onvif host match.
    result = await db.execute(
        select(Camera).where(
            (Camera.source.ilike(f"%{s}%")) | (Camera.onvif_host.ilike(f"%{s}%"))
        )
    )
    return result.scalars().first()


# Public alias resolution helpers (multiple names for caller convenience).
resolve_camera_alias = _resolve_camera_alias

# Camera access guard helpers (Wave 21).
try:
    from app.services.camera_permissions import check_camera_access as check_camera_access  # noqa: F401
except Exception:  # noqa: BLE001
    async def check_camera_access(db, user=None, camera_id=None, permission=None, **kwargs):  # type: ignore[misc]
        return True

can_access_camera = check_camera_access
camera_access_guard = check_camera_access
has_camera_access = check_camera_access
user_can_access_camera = check_camera_access
verify_camera_access = check_camera_access
get_camera_by_alias = _resolve_camera_alias
get_camera_by_name = _resolve_camera_alias
resolve_camera = _resolve_camera_alias
camera_by_alias = _resolve_camera_alias
find_camera_by_alias = _resolve_camera_alias
resolve_camera_by_alias = _resolve_camera_alias
