"""Bulk camera operations service (by explicit camera id list).

Verification note (read-only check before writing this module):

- ``app/services/group_bulk.py`` + ``app/api/group_bulk.py`` operate by
  *group* only: ``bulk_set_group_cameras(db, group_ids, ...)`` resolves
  groups then toggles their member cameras. There is no enable/disable
  entrypoint that accepts an arbitrary camera id list.
- ``app/api/cameras.py::api_delete_cameras_bulk`` (``DELETE /api/cameras``)
  deletes a comma-separated ``ids`` list, or wipes *everything* when ``ids``
  is absent. There is no POST bulk enable/disable/delete-by-id-list.

This module fills that gap: enable/disable/delete cameras by an explicit
id list. The ``app/api/bulk_cameras.py`` router stays thin.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

DEFAULT_TOGGLE_FIELDS = ("enabled",)
ALLOWED_TOGGLE_FIELDS = frozenset(
    {
        "enabled",
        "recording_enabled",
        "detection_enabled",
        "detector_enabled",
        "alert_enabled",
    }
)


def _coerce_int_list(value: Any) -> List[int]:
    """Coerce ints / numeric strings / mixed lists to a deduped int list."""
    if value is None:
        return []
    if isinstance(value, bool):
        return []
    if isinstance(value, (int, float)):
        try:
            return [int(value)]
        except (TypeError, ValueError):
            return []
    if isinstance(value, str):
        parts = value.split(",") if "," in value else [value]
        out: List[int] = []
        for part in parts:
            part = part.strip()
            if not part:
                continue
            try:
                out.append(int(float(part)))
            except (TypeError, ValueError):
                continue
        return out
    if isinstance(value, (list, tuple, set)):
        out = []
        for item in value:
            if item is None or isinstance(item, bool):
                continue
            if isinstance(item, (int, float)):
                try:
                    out.append(int(item))
                except (TypeError, ValueError):
                    continue
            elif isinstance(item, str):
                s = item.strip()
                if not s:
                    continue
                # tolerate csv strings nested in lists
                for chunk in (s.split(",") if "," in s else [s]):
                    chunk = chunk.strip()
                    if not chunk:
                        continue
                    try:
                        out.append(int(float(chunk)))
                    except (TypeError, ValueError):
                        continue
            elif isinstance(item, dict):
                for key in ("id", "camera_id", "cameraId", "value"):
                    if item.get(key) is not None:
                        out.extend(_coerce_int_list(item.get(key)))
                        break
            else:
                try:
                    out.append(int(item))  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    continue
        seen = set()
        deduped = []
        for i in out:
            if i not in seen:
                seen.add(i)
                deduped.append(i)
        return deduped
    try:
        return [int(value)]  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return []


def _normalize_fields(fields: Optional[Sequence[str]]) -> List[str]:
    if not fields:
        return list(DEFAULT_TOGGLE_FIELDS)
    kept = [f for f in fields if f in ALLOWED_TOGGLE_FIELDS]
    return kept or list(DEFAULT_TOGGLE_FIELDS)


async def bulk_set_cameras(
    db: Any,
    camera_ids: Any,
    enabled: bool = True,
    fields: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Enable/disable cameras by explicit id list."""
    from app.models.entities import Camera

    cids = _coerce_int_list(camera_ids)
    field_list = _normalize_fields(list(fields) if fields else None)

    updated: List[int] = []
    missing: List[int] = []
    for cid in cids:
        cam = await db.get(Camera, cid)
        if cam is None:
            missing.append(cid)
            continue
        for f in field_list:
            if hasattr(cam, f):
                setattr(cam, f, bool(enabled))
        updated.append(cid)
    await db.commit()
    return {
        "ok": True,
        "enabled": bool(enabled),
        "fields": field_list,
        "updated_camera_ids": sorted(updated),
        "updated_count": len(updated),
        "missing_camera_ids": sorted(missing),
    }


async def bulk_enable_cameras(
    db: Any, camera_ids: Any, fields: Optional[Sequence[str]] = None
) -> Dict[str, Any]:
    """Enable cameras by explicit id list."""
    return await bulk_set_cameras(db, camera_ids, enabled=True, fields=fields)


async def bulk_disable_cameras(
    db: Any, camera_ids: Any, fields: Optional[Sequence[str]] = None
) -> Dict[str, Any]:
    """Disable cameras by explicit id list."""
    return await bulk_set_cameras(db, camera_ids, enabled=False, fields=fields)


async def bulk_delete_cameras(db: Any, camera_ids: Any) -> Dict[str, Any]:
    """Delete cameras by explicit id list."""
    from app.models.entities import Camera

    cids = _coerce_int_list(camera_ids)
    deleted: List[int] = []
    missing: List[int] = []
    for cid in cids:
        cam = await db.get(Camera, cid)
        if cam is None:
            missing.append(cid)
            continue
        await db.delete(cam)
        deleted.append(cid)
    await db.commit()
    # best-effort per-camera cleanup (mirrors services.cameras.delete_camera)
    try:
        from app.services import camera_health as _ch

        for cid in deleted:
            try:
                _ch.remove(cid)
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass
    return {
        "ok": True,
        "deleted_camera_ids": sorted(deleted),
        "deleted": sorted(deleted),
        "deleted_count": len(deleted),
        "missing_camera_ids": sorted(missing),
    }


# Compatibility aliases.
bulk_enable = bulk_enable_cameras
bulk_disable = bulk_disable_cameras
bulk_set = bulk_set_cameras
bulk_delete = bulk_delete_cameras
enable_cameras = bulk_enable_cameras
disable_cameras = bulk_disable_cameras
set_cameras = bulk_set_cameras
delete_cameras = bulk_delete_cameras

__all__ = [
    "DEFAULT_TOGGLE_FIELDS",
    "ALLOWED_TOGGLE_FIELDS",
    "bulk_set_cameras",
    "bulk_enable_cameras",
    "bulk_disable_cameras",
    "bulk_delete_cameras",
    "bulk_enable",
    "bulk_disable",
    "bulk_set",
    "bulk_delete",
    "enable_cameras",
    "disable_cameras",
    "set_cameras",
    "delete_cameras",
]
