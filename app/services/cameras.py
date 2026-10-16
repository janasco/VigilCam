from __future__ import annotations

import random
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import Camera, Event
from app.schemas import CameraCreate


LABELS = ["person", "car", "truck", "bicycle", "dog", "cat", "motorcycle", "bus"]
MODEL_NAME = "vigilcam-yolov8n-stub"


async def list_cameras(db: AsyncSession) -> list[Camera]:
    result = await db.execute(select(Camera).order_by(Camera.id))
    return list(result.scalars().all())


async def get_camera(db: AsyncSession, camera_id: int) -> Optional[Camera]:
    return await db.get(Camera, camera_id)


async def create_camera(db: AsyncSession, payload: CameraCreate) -> Camera:
    camera = Camera(**payload.model_dump())
    db.add(camera)
    await db.commit()
    await db.refresh(camera)
    return camera


async def update_camera(db: AsyncSession, camera_id: int, payload: dict) -> Optional[Camera]:
    camera = await db.get(Camera, camera_id)
    if not camera:
        return None
    for key, value in payload.items():
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
    return event


async def recent_events(db: AsyncSession, limit: int = 50) -> list[Event]:
    result = await db.execute(select(Event).order_by(Event.id.desc()).limit(limit))
    return list(result.scalars().all())
