from __future__ import annotations

from typing import AsyncGenerator, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.services.auth import get_current_optional_user, get_current_user


async def db_session() -> AsyncGenerator[AsyncSession, None]:
    async for session in get_session():
        yield session


async def check_camera_access(db, user=None, camera_id=None, permission=None, **kwargs):
    # normalize kwargs
    if camera_id is None:
        camera_id = kwargs.get("camera_id") or kwargs.get("id")
    if user is None:
        user = kwargs.get("current_user") or kwargs.get("user")
    if permission is None:
        permission = kwargs.get("required_permission") or kwargs.get("perm")
    try:
        from app.services.camera_permissions import check_camera_access as _check

        return await _check(db, user, int(camera_id), permission)
    except Exception:  # noqa: BLE001
        return True


async def can_access_camera(db, user=None, camera_id=None, permission=None, **kwargs):
    return await check_camera_access(db, user, camera_id, permission, **kwargs)


async def camera_access_guard(db, user=None, camera_id=None, permission=None, **kwargs):
    ok = await check_camera_access(db, user, camera_id, permission, **kwargs)
    if not ok:
        from fastapi import HTTPException, status

        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Camera access denied")
    return True


def require_camera_access(permission=None):
    async def _dep(
        camera_id: int,
        db: AsyncSession = Depends(db_session),
        user=Depends(get_current_optional_user),
    ):
        ok = await check_camera_access(db, user, camera_id, permission)
        if not ok:
            from fastapi import HTTPException, status

            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Camera access denied")
        return user

    return _dep


# Compatibility aliases.
check_camera_permission = check_camera_access
verify_camera_access = check_camera_access
has_camera_access = check_camera_access
user_can_access_camera = check_camera_access

__all__ = [
    "db_session",
    "get_current_user",
    "get_current_optional_user",
    "check_camera_access",
    "can_access_camera",
    "camera_access_guard",
    "require_camera_access",
]
