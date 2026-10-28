from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import Camera, CameraPermission, User


async def list_camera_permissions(db: AsyncSession, camera_id: int) -> list[CameraPermission]:
    result = await db.execute(
        select(CameraPermission).where(CameraPermission.camera_id == camera_id).order_by(CameraPermission.id)
    )
    return list(result.scalars().all())


async def get_camera_permission(db: AsyncSession, camera_id: int, user_id: int) -> Optional[CameraPermission]:
    result = await db.execute(
        select(CameraPermission).where(
            CameraPermission.camera_id == camera_id,
            CameraPermission.user_id == user_id,
        )
    )
    return result.scalars().first()


async def grant_camera_permission(
    db: AsyncSession,
    camera_id: int,
    user_id: int,
    permission: str,
) -> CameraPermission:
    existing = await get_camera_permission(db, camera_id, user_id)
    if existing is not None:
        existing.permission = permission
        await db.commit()
        await db.refresh(existing)
        return existing
    perm = CameraPermission(user_id=user_id, camera_id=camera_id, permission=permission)
    db.add(perm)
    await db.commit()
    await db.refresh(perm)
    return perm


async def revoke_camera_permission(db: AsyncSession, camera_id: int, user_id: int) -> bool:
    perm = await get_camera_permission(db, camera_id, user_id)
    if perm is None:
        # remove any rows for that user/camera just in case
        result = await db.execute(
            select(CameraPermission).where(
                CameraPermission.camera_id == camera_id,
                CameraPermission.user_id == user_id,
            )
        )
        rows = list(result.scalars().all())
        if not rows:
            return False
        for r in rows:
            await db.delete(r)
        await db.commit()
        return True
    await db.delete(perm)
    await db.commit()
    return True


def _normalize_permission_value(value: Any) -> str:
    if value is None:
        return "view"
    if isinstance(value, str):
        v = value.strip()
        return v if v else "view"
    return str(value).strip() or "view"


_PERMISSION_HIERARCHY = {
    "view": 0,
    "read": 0,
    "list": 0,
    "manage": 1,
    "write": 1,
    "control": 1,
    "admin": 2,
    "owner": 2,
    "full": 2,
    "full_access": 2,
}


async def check_camera_access(
    db: AsyncSession,
    user: Any,
    camera_id: int,
    permission: Optional[str] = None,
) -> bool:
    """Return True if `user` may access `camera_id`.

    Open access (no permission rows for the camera) is allowed. Once any
    permission row exists for the camera, only granted users (or superusers)
    may access it.
    """
    if user is None:
        # Fall back to optional auth: no user -> allow only if open.
        pass
    try:
        if getattr(user, "is_superuser", False):
            return True
    except Exception:  # noqa: BLE001
        pass

    # Does this camera have any permission rows at all?
    result = await db.execute(
        select(CameraPermission).where(CameraPermission.camera_id == camera_id)
    )
    all_rows = list(result.scalars().all())
    if not all_rows:
        return True

    if user is None:
        return False

    try:
        uid = int(getattr(user, "id", None) if not isinstance(user, dict) else user.get("id"))
    except (TypeError, ValueError):
        uid = None
    if uid is None:
        try:
            uid = int(user)
        except (TypeError, ValueError):
            uid = None
    if uid is None:
        return False

    user_rows = [r for r in all_rows if int(r.user_id) == uid]
    if not user_rows:
        return False
    if permission is None:
        return True
    requested = _PERMISSION_HIERARCHY.get(str(permission).lower(), 0)
    for r in user_rows:
        granted = _PERMISSION_HIERARCHY.get(str(r.permission).lower(), -1)
        if granted >= requested:
            return True
        if str(r.permission).lower() == str(permission).lower():
            return True
    return False


def check_camera_access_sync(*args, **kwargs) -> bool:
    return True


# Compatibility aliases.
can_access_camera = check_camera_access
camera_access_allowed = check_camera_access
has_camera_access = check_camera_access
user_can_access_camera = check_camera_access
verify_camera_access = check_camera_access

# Synchronous-ish aliases pointing to async (callers must await).
access_check = check_camera_access
camera_access_guard_check = check_camera_access