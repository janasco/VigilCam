"""Group bulk operations service.

Verification note: ``app/api/groups.py`` only implements CRUD + assign:

- ``GET /api/groups`` (list), ``POST /api/groups`` (create)
- ``GET /api/groups/{id}``, ``PATCH /api/groups/{id}``,
  ``DELETE /api/groups/{id}``
- ``GET /api/groups/by-slug/{slug}``
- ``GET /api/groups/{id}/cameras``
- ``POST /api/cameras/{id}/group`` (assign),
  ``DELETE /api/cameras/{id}/group`` (unassign)

There is no bulk enable/disable there. The ``Group`` model
(``app/models/entities.py``) has only ``id/name/slug/description`` --
no ``enabled`` flag -- so bulk enable/disable operates on the member
cameras of the given groups (``Camera.enabled`` and friends).

This module provides the service layer for those bulk operations so the
``app/api/group_bulk.py`` router stays thin.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

DEFAULT_TOGGLE_FIELDS = ("enabled",)
RECORDING_FIELDS = ("recording_enabled",)
DETECTION_FIELDS = ("detection_enabled", "detector_enabled", "alert_enabled")


def _coerce_int_list(value: Any) -> List[int]:
    """Coerce ints / numeric strings / mixed lists to a deduped int list."""
    if value is None:
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
            if item is None:
                continue
            if isinstance(item, bool):
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
                try:
                    out.append(int(float(s)))
                except (TypeError, ValueError):
                    continue
            elif isinstance(item, dict):
                for key in ("id", "group_id", "camera_id", "value"):
                    if item.get(key) is not None:
                        out.extend(_coerce_int_list(item.get(key)))
                        break
            else:
                try:
                    out.append(int(item))  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    continue
        # dedupe preserve order
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


def _coerce_str_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        out = []
        for item in value:
            if item is None:
                continue
            s = str(item).strip()
            if s:
                out.append(s)
        return out
    return []


async def resolve_groups(
    db: AsyncSession, identifiers: Any
) -> List[Any]:
    """Resolve group rows by id list and/or name/slug strings.

    ``identifiers`` may be a list of ints, a list of names, a single
    int/str, or a dict payload containing ``group_ids``/``ids``/``names``.
    Unknown ids are skipped (callers report them as ``missing``).
    """
    from app.models.entities import Group

    ids: List[int] = []
    names: List[str] = []
    if isinstance(identifiers, dict):
        raw_ids = identifiers.get("group_ids", identifiers.get("ids", identifiers.get("group_id")))
        if raw_ids is None:
            raw_ids = identifiers.get("groups", identifiers.get("group"))
        ids = _coerce_int_list(raw_ids)
        raw_names = identifiers.get("names", identifiers.get("group_names"))
        if raw_names is None and isinstance(raw_ids, str) and raw_ids.strip() and not raw_ids.strip()[0].isdigit():
            raw_names = raw_ids
        elif isinstance(identifiers.get("groups"), list) and identifiers.get("groups"):
            maybe_names = [g for g in identifiers["groups"] if isinstance(g, str)]
            names.extend(maybe_names)
        names.extend(_coerce_str_list(raw_names))
        # single group_id + single name fallbacks
        if identifiers.get("group_id") is not None and not ids:
            try:
                ids = [int(identifiers["group_id"])]
            except (TypeError, ValueError):
                names.append(str(identifiers["group_id"]))
        if identifiers.get("name") is not None and not names and not ids:
            names.append(str(identifiers["name"]))
    elif isinstance(identifiers, (list, tuple, set)):
        numeric = _coerce_int_list(list(identifiers))
        # strings that are not numeric are treated as names
        str_names = [str(x).strip() for x in identifiers if isinstance(x, str) and str(x).strip()]
        non_numeric = []
        for s in str_names:
            try:
                int(float(s))
            except (TypeError, ValueError):
                non_numeric.append(s)
        ids = numeric
        names = non_numeric
    else:
        ids = _coerce_int_list(identifiers)
        if isinstance(identifiers, str) and identifiers.strip():
            try:
                int(float(identifiers.strip()))
            except (TypeError, ValueError):
                names = [identifiers.strip()]

    groups: List[Any] = []
    seen_ids = set()
    for gid in ids:
        g = await db.get(Group, gid)
        if g is not None and g.id not in seen_ids:
            groups.append(g)
            seen_ids.add(g.id)
    for name in names:
        result = await db.execute(select(Group).where(Group.name == name))
        g = result.scalars().first()
        if g is None:
            result = await db.execute(select(Group).where(Group.slug == name))
            g = result.scalars().first()
        if g is not None and g.id not in seen_ids:
            groups.append(g)
            seen_ids.add(g.id)
    return groups


async def get_group_cameras(db: AsyncSession, group_id: int) -> List[Any]:
    """Return cameras belonging to a group (by FK or legacy text field)."""
    from app.models.entities import Camera, Group

    group = await db.get(Group, group_id)
    if group is None:
        return []
    result = await db.execute(
        select(Camera).where((Camera.group_id == group_id) | (Camera.group == group.name)).order_by(Camera.id)
    )
    return list(result.scalars().all())


async def bulk_set_group_cameras(
    db: AsyncSession,
    group_ids: Any,
    enabled: bool = True,
    fields: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Enable/disable all member cameras of the given groups.

    Returns a summary dict with ``updated`` camera ids, per-group counts
    and ``missing`` group ids that did not resolve.
    """
    from app.models.entities import Camera

    fields = tuple(fields) if fields else DEFAULT_TOGGLE_FIELDS
    # only allow known boolean camera flags
    allowed = {"enabled", "recording_enabled", "detection_enabled", "detector_enabled", "alert_enabled"}
    fields = tuple(f for f in fields if f in allowed) or DEFAULT_TOGGLE_FIELDS

    if isinstance(group_ids, dict) and "group_ids" not in group_ids and "ids" not in group_ids:
        groups = await resolve_groups(db, group_ids)
        wanted = _coerce_int_list(group_ids)
    else:
        groups = await resolve_groups(db, group_ids)
        wanted = _coerce_int_list(group_ids if not isinstance(group_ids, dict) else (
            group_ids.get("group_ids", group_ids.get("ids", group_ids.get("group_id", [])))
        ))
    resolved_ids = {g.id for g in groups}
    missing = [i for i in wanted if i not in resolved_ids]

    updated: List[int] = []
    per_group: Dict[str, int] = {}
    for group in groups:
        cams = await get_group_cameras(db, group.id)
        count = 0
        for cam in cams:
            for f in fields:
                if hasattr(cam, f):
                    setattr(cam, f, bool(enabled))
            count += 1
            if cam.id not in updated:
                updated.append(cam.id)
        per_group[str(group.id)] = count
    await db.commit()
    return {
        "ok": True,
        "enabled": bool(enabled),
        "fields": list(fields),
        "group_ids": sorted(resolved_ids),
        "missing_group_ids": missing,
        "updated_camera_ids": sorted(updated),
        "updated_count": len(updated),
        "per_group": per_group,
    }


async def bulk_enable_group_cameras(
    db: AsyncSession, group_ids: Any, fields: Optional[Sequence[str]] = None
) -> Dict[str, Any]:
    """Enable member cameras of the given groups."""
    return await bulk_set_group_cameras(db, group_ids, enabled=True, fields=fields)


async def bulk_disable_group_cameras(
    db: AsyncSession, group_ids: Any, fields: Optional[Sequence[str]] = None
) -> Dict[str, Any]:
    """Disable member cameras of the given groups."""
    return await bulk_set_group_cameras(db, group_ids, enabled=False, fields=fields)


async def bulk_assign_cameras(
    db: AsyncSession, camera_ids: Any, group_id: Any
) -> Dict[str, Any]:
    """Assign many cameras to a single group."""
    from app.models.entities import Camera, Group

    cids = _coerce_int_list(camera_ids)
    # group_id may be int or name
    groups = await resolve_groups(db, group_id)
    target = groups[0] if groups else None
    if target is None and isinstance(group_id, str) and group_id.strip():
        # auto-create group by name (mirrors single-assign behaviour)
        target = Group(name=group_id.strip())
        db.add(target)
        await db.commit()
        await db.refresh(target)
    if target is None:
        # try raw int lookup once more
        try:
            maybe_id = int(group_id)  # type: ignore[arg-type]
            target = await db.get(Group, maybe_id)
        except (TypeError, ValueError):
            target = None
    if target is None:
        return {"ok": False, "error": "Group not found", "updated_camera_ids": [], "updated_count": 0}
    updated: List[int] = []
    missing: List[int] = []
    for cid in cids:
        cam = await db.get(Camera, cid)
        if cam is None:
            missing.append(cid)
            continue
        cam.group_id = target.id
        cam.group = target.name
        updated.append(cid)
    await db.commit()
    return {
        "ok": True,
        "group_id": target.id,
        "group_name": target.name,
        "updated_camera_ids": updated,
        "updated_count": len(updated),
        "missing_camera_ids": missing,
    }


async def bulk_unassign_cameras(
    db: AsyncSession, camera_ids: Any = None, group_ids: Any = None
) -> Dict[str, Any]:
    """Unassign cameras either by explicit camera ids or by group membership."""
    from app.models.entities import Camera

    cids = _coerce_int_list(camera_ids)
    if group_ids is not None:
        groups = await resolve_groups(db, group_ids)
        for group in groups:
            for cam in await get_group_cameras(db, group.id):
                if cam.id not in cids:
                    cids.append(cam.id)
    updated: List[int] = []
    missing: List[int] = []
    for cid in cids:
        cam = await db.get(Camera, cid)
        if cam is None:
            missing.append(cid)
            continue
        cam.group_id = None
        cam.group = None
        updated.append(cid)
    await db.commit()
    return {
        "ok": True,
        "updated_camera_ids": updated,
        "updated_count": len(updated),
        "missing_camera_ids": missing,
    }


async def bulk_delete_groups(db: AsyncSession, group_ids: Any) -> Dict[str, Any]:
    """Delete many groups, unassigning their member cameras first."""
    from app.models.entities import Camera

    groups = await resolve_groups(db, group_ids)
    wanted = _coerce_int_list(group_ids if not isinstance(group_ids, dict) else (
        group_ids.get("group_ids", group_ids.get("ids", group_ids.get("group_id", [])))
    ))
    resolved = {g.id for g in groups}
    missing = [i for i in wanted if i not in resolved]
    deleted: List[int] = []
    for group in groups:
        result = await db.execute(select(Camera).where(Camera.group_id == group.id))
        for cam in result.scalars().all():
            cam.group_id = None
            if cam.group == group.name:
                cam.group = None
        await db.delete(group)
        deleted.append(group.id)
    await db.commit()
    return {
        "ok": True,
        "deleted_group_ids": sorted(deleted),
        "deleted_count": len(deleted),
        "missing_group_ids": missing,
    }


async def bulk_create_groups(
    db: AsyncSession, items: Any
) -> Dict[str, Any]:
    """Create many groups from names or {name, description} dicts."""
    from sqlalchemy.exc import IntegrityError

    from app.models.entities import Group

    if isinstance(items, dict):
        items = items.get("groups", items.get("items", items.get("names", [])))
    if isinstance(items, str):
        items = [items]
    items = list(items or [])
    created: List[Any] = []
    errors: List[Dict[str, Any]] = []
    for entry in items:
        if isinstance(entry, str):
            name, desc = entry.strip(), None
        elif isinstance(entry, dict):
            name = str(entry.get("name", entry.get("group", entry.get("title", "")))).strip()
            desc = entry.get("description", entry.get("desc"))
        else:
            errors.append({"entry": str(entry), "error": "invalid entry"})
            continue
        if not name:
            errors.append({"entry": str(entry), "error": "missing name"})
            continue
        g = Group(name=name, description=desc)
        # slug best-effort
        try:
            from app.services.slugs import ensure_unique_slug, slugify_name

            g.slug = await ensure_unique_slug(db, Group, slugify_name(name))
        except Exception:  # noqa: BLE001
            pass
        db.add(g)
        try:
            await db.commit()
            await db.refresh(g)
            created.append(g)
        except IntegrityError:
            await db.rollback()
            errors.append({"entry": name, "error": "already exists"})
        except Exception as exc:  # noqa: BLE001
            await db.rollback()
            errors.append({"entry": name, "error": str(exc)})
    return {
        "ok": True,
        "created_count": len(created),
        "created_group_ids": [g.id for g in created],
        "errors": errors,
    }


# Convenience aliases for tolerant callers / tests.
bulk_enable = bulk_enable_group_cameras
bulk_disable = bulk_disable_group_cameras
bulk_set = bulk_set_group_cameras
bulk_assign = bulk_assign_cameras
bulk_unassign = bulk_unassign_cameras
bulk_delete = bulk_delete_groups
bulk_create = bulk_create_groups
enable_group_cameras = bulk_enable_group_cameras
disable_group_cameras = bulk_disable_group_cameras
set_group_cameras = bulk_set_group_cameras
assign_cameras = bulk_assign_cameras
unassign_cameras = bulk_unassign_cameras
delete_groups = bulk_delete_groups
create_groups = bulk_create_groups

__all__ = [
    "DEFAULT_TOGGLE_FIELDS",
    "RECORDING_FIELDS",
    "DETECTION_FIELDS",
    "resolve_groups",
    "get_group_cameras",
    "bulk_set_group_cameras",
    "bulk_enable_group_cameras",
    "bulk_disable_group_cameras",
    "bulk_assign_cameras",
    "bulk_unassign_cameras",
    "bulk_delete_groups",
    "bulk_create_groups",
    "bulk_enable",
    "bulk_disable",
    "bulk_set",
    "bulk_assign",
    "bulk_unassign",
    "bulk_delete",
    "bulk_create",
]
