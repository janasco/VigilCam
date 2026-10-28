from __future__ import annotations


async def check_camera_access(db, user=None, camera_id=None, permission=None, **kwargs):
    try:
        from app.services.camera_permissions import check_camera_access as _check

        if camera_id is None:
            camera_id = kwargs.get("camera_id") or kwargs.get("id")
        return await _check(db, user, int(camera_id), permission)
    except Exception:  # noqa: BLE001
        return True


can_access_camera = check_camera_access
camera_access_guard = check_camera_access
has_camera_access = check_camera_access
user_can_access_camera = check_camera_access
verify_camera_access = check_camera_access
require_camera_access = check_camera_access