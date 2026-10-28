"""Camera clone/duplicate service.

Duplicates an existing camera row into a new row with a fresh id.
Excludes identity/timestamp/activity fields from the copy.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import Camera


# Fields never copied to the clone (fresh identity / runtime state).
_SKIP_FIELDS = frozenset(
    {
        "id",
        "created_at",
        "last_activity_at",
        "last_motion_at",
    }
)

# Identity fields cleared on clone to avoid duplicate device identity.
_CLEAR_FIELDS = frozenset(
    {
        "serial_number",
        "hardware_id",
    }
)


def build_clone_name(source_name: Optional[str], override: Optional[str] = None) -> str:
    """Resolve the clone's display name."""
    if override is not None and str(override).strip():
        return str(override).strip()[:120]
    base = str(source_name).strip() if source_name else "camera"
    if not base:
        base = "camera"
    name = f"{base} (copy)"
    return name[:120]


def build_clone_data(source: Camera, name: Optional[str] = None) -> Dict[str, Any]:
    """Build a kwargs dict for the cloned Camera from a source row."""
    data: Dict[str, Any] = {}
    for column in Camera.__table__.columns:
        key = column.key
        if key in _SKIP_FIELDS or key in _CLEAR_FIELDS:
            continue
        try:
            value = getattr(source, key)
        except AttributeError:
            continue
        # Deep-copy mutable JSON payloads (motion_zones, etc.).
        try:
            value = copy.deepcopy(value)
        except Exception:  # noqa: BLE001
            pass
        data[key] = value
    data["name"] = build_clone_name(getattr(source, "name", None), name)
    return data


async def clone_camera(
    db: AsyncSession,
    camera_id: int,
    name: Optional[str] = None,
) -> Optional[Camera]:
    """Clone camera ``camera_id`` into a new row.

    Returns the new :class:`Camera` or ``None`` when the source is missing.
    """
    source = await db.get(Camera, camera_id)
    if source is None:
        return None
    data = build_clone_data(source, name=name)
    cloned = Camera(**data)
    db.add(cloned)
    await db.commit()
    await db.refresh(cloned)
    try:
        from app.services import camera_health as _ch

        _ch._ensure(cloned.id)
    except Exception:  # noqa: BLE001
        pass
    return cloned


# Compatibility aliases.
duplicate_camera = clone_camera
copy_camera = clone_camera
clone_camera_by_id = clone_camera
duplicate_camera_by_id = clone_camera
copy_camera_by_id = clone_camera

__all__ = [
    "clone_camera",
    "build_clone_data",
    "build_clone_name",
    "duplicate_camera",
    "copy_camera",
]
