"""Group bulk operations router (auto-loaded by ``app.main``).

No edits to ``app/main.py`` are needed: this module exposes ``router``
and the pkgutil auto-loader in ``app.main`` picks it up.

Endpoints (POST, JSON bodies are tolerant of alias keys):

- ``/api/groups/bulk-enable`` -- enable member cameras of groups
- ``/api/groups/bulk-disable`` -- disable member cameras of groups
- ``/api/groups/bulk-assign`` -- assign many cameras to one group
- ``/api/groups/bulk-unassign`` -- unassign cameras (by camera_ids or group_ids)
- ``/api/groups/bulk-delete`` -- delete many groups
- ``/api/groups/bulk-create`` -- create many groups

Hyphen/underscore and ``/api/camera-groups/...`` aliases are registered
for every endpoint.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import db_session
from app.services import group_bulk as svc

router = APIRouter(tags=["group_bulk"])


class BulkEnableRequest(BaseModel):
    group_ids: Optional[Any] = None
    ids: Optional[Any] = None
    group_id: Optional[Any] = None
    groups: Optional[Any] = None
    names: Optional[Any] = None
    enabled: Optional[bool] = None
    active: Optional[bool] = None
    fields: Optional[Any] = None

    model_config = {"extra": "allow"}


class BulkAssignRequest(BaseModel):
    camera_ids: Optional[Any] = None
    camera_id: Optional[Any] = None
    cameras: Optional[Any] = None
    ids: Optional[Any] = None
    group_id: Optional[Any] = None
    group_ids: Optional[Any] = None
    group: Optional[Any] = None
    group_name: Optional[Any] = None

    model_config = {"extra": "allow"}


class BulkUnassignRequest(BaseModel):
    camera_ids: Optional[Any] = None
    camera_id: Optional[Any] = None
    cameras: Optional[Any] = None
    ids: Optional[Any] = None
    group_ids: Optional[Any] = None
    group_id: Optional[Any] = None
    groups: Optional[Any] = None

    model_config = {"extra": "allow"}


class BulkDeleteRequest(BaseModel):
    group_ids: Optional[Any] = None
    ids: Optional[Any] = None
    group_id: Optional[Any] = None
    groups: Optional[Any] = None

    model_config = {"extra": "allow"}


class BulkCreateRequest(BaseModel):
    groups: Optional[Any] = None
    items: Optional[Any] = None
    names: Optional[Any] = None
    cameras: Optional[Any] = None  # tolerate misrouted key

    model_config = {"extra": "allow"}


def _payload_dict(payload: Any, request: Request | None = None) -> Dict[str, Any]:
    if payload is None:
        data: Dict[str, Any] = {}
    elif hasattr(payload, "model_dump"):
        try:
            data = dict(payload.model_dump(exclude_unset=False))
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    else:
        try:
            data = dict(payload)
        except Exception:  # noqa: BLE001
            data = {}
    data = {k: v for k, v in data.items() if v is not None}
    if request is not None:
        try:
            for k, v in request.query_params.items():
                if k not in data or data.get(k) is None:
                    data[k] = v
        except Exception:  # noqa: BLE001
            pass
    for nested_key in ("payload", "params", "data", "body"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _extract_group_ids(data: Dict[str, Any]) -> Any:
    for key in ("group_ids", "ids", "groups", "names", "group_id"):
        if data.get(key) is not None:
            return data.get(key)
    # bare list body, e.g. {"items": [...]} handled by callers; fallback: whole dict
    return data


def _extract_enabled(data: Dict[str, Any], default: bool) -> bool:
    for key in ("enabled", "active", "is_enabled", "enable"):
        if data.get(key) is not None:
            val = data.get(key)
            if isinstance(val, str):
                return val.strip().lower() in ("1", "true", "yes", "y", "on")
            return bool(val)
    return default


def _extract_fields(data: Dict[str, Any]) -> Optional[List[str]]:
    for key in ("fields", "field", "flags", "toggles"):
        if data.get(key) is not None:
            val = data.get(key)
            if isinstance(val, str):
                return [p.strip() for p in val.split(",") if p.strip()]
            if isinstance(val, (list, tuple)):
                return [str(x).strip() for x in val if str(x).strip()]
            return [str(val)]
    # single-flag shortcuts: {"recording": true} etc.
    out = []
    for flag, fields in (
        ("recording", ["recording_enabled"]),
        ("recording_enabled", ["recording_enabled"]),
        ("detection", ["detection_enabled", "detector_enabled"]),
        ("detection_enabled", ["detection_enabled", "detector_enabled"]),
        ("alerts", ["alert_enabled"]),
        ("alert_enabled", ["alert_enabled"]),
    ):
        if data.get(flag) is not None:
            out.extend(fields)
    if out:
        # also keep base enabled flag unless explicitly excluded
        out = ["enabled"] + [f for f in out if f != "enabled"]
        seen: List[str] = []
        for f in out:
            if f not in seen:
                seen.append(f)
        return seen
    return None


def _extract_camera_ids(data: Dict[str, Any]) -> Any:
    for key in ("camera_ids", "camera_id", "cameras", "ids", "items", "cameraIds"):
        if data.get(key) is not None:
            return data.get(key)
    return []


def _extract_single_group(data: Dict[str, Any]) -> Any:
    for key in ("group_id", "group_ids", "group", "group_name", "groupName", "groups", "ids"):
        if data.get(key) is not None:
            val = data.get(key)
            if isinstance(val, (list, tuple)) and len(val) == 1:
                return val[0]
            if isinstance(val, (list, tuple)) and val:
                return val[0]
            return val
    return None


async def _enable_impl(db: AsyncSession, data: Dict[str, Any], default: bool) -> Dict[str, Any]:
    group_ids = _extract_group_ids(data)
    enabled = _extract_enabled(data, default)
    fields = _extract_fields(data)
    return await svc.bulk_set_group_cameras(db, group_ids, enabled=enabled, fields=fields)


async def _assign_impl(db: AsyncSession, data: Dict[str, Any]) -> Dict[str, Any]:
    camera_ids = _extract_camera_ids(data)
    group = _extract_single_group(data)
    if group is None:
        raise HTTPException(status_code=400, detail="group_id is required")
    result = await svc.bulk_assign_cameras(db, camera_ids, group)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error", "Group not found"))
    return result


async def _unassign_impl(db: AsyncSession, data: Dict[str, Any]) -> Dict[str, Any]:
    camera_ids = _extract_camera_ids(data)
    group_ids = data.get("group_ids", data.get("group_id", data.get("groups")))
    if (not camera_ids or (isinstance(camera_ids, list) and not camera_ids)) and group_ids is None:
        raise HTTPException(status_code=400, detail="camera_ids or group_ids required")
    return await svc.bulk_unassign_cameras(db, camera_ids=camera_ids, group_ids=group_ids)


async def _delete_impl(db: AsyncSession, data: Dict[str, Any]) -> Dict[str, Any]:
    group_ids = _extract_group_ids(data)
    cids = svc._coerce_int_list(group_ids)  # noqa: SLF001
    if not cids and not (isinstance(group_ids, list) and group_ids):
        # allow names too; resolve will handle
        if not group_ids:
            raise HTTPException(status_code=400, detail="group_ids is required")
    return await svc.bulk_delete_groups(db, group_ids)


async def _create_impl(db: AsyncSession, data: Dict[str, Any]) -> Dict[str, Any]:
    items = data.get("groups", data.get("items", data.get("names", data.get("names_list"))))
    if items is None:
        # maybe the body itself is a list-like {"0": ...}? fallback: single name
        if data.get("name"):
            items = [data]
        else:
            raise HTTPException(status_code=400, detail="groups is required")
    result = await svc.bulk_create_groups(db, items)
    from app.schemas import GroupRead

    # serialize created groups for response convenience
    created_ids = result.get("created_group_ids", [])
    groups_out = []
    for gid in created_ids:
        from app.models.entities import Group

        g = await db.get(Group, gid)
        if g is not None:
            try:
                groups_out.append(GroupRead.model_validate(g).model_dump(mode="json"))
            except Exception:  # noqa: BLE001
                groups_out.append({"id": g.id, "name": g.name})
    result["created"] = groups_out
    return result


def _make_enable_endpoint(default: bool):
    async def _endpoint(
        request: Request,
        payload: Any = Body(default=None),
        db: AsyncSession = Depends(db_session),
    ):
        data = _payload_dict(payload, request)
        return await _enable_impl(db, data, default)

    return _endpoint


def _make_assign_endpoint():
    async def _endpoint(
        request: Request,
        payload: Any = Body(default=None),
        db: AsyncSession = Depends(db_session),
    ):
        data = _payload_dict(payload, request)
        return await _assign_impl(db, data)

    return _endpoint


def _make_unassign_endpoint():
    async def _endpoint(
        request: Request,
        payload: Any = Body(default=None),
        db: AsyncSession = Depends(db_session),
    ):
        data = _payload_dict(payload, request)
        return await _unassign_impl(db, data)

    return _endpoint


def _make_delete_endpoint():
    async def _endpoint(
        request: Request,
        payload: Any = Body(default=None),
        db: AsyncSession = Depends(db_session),
    ):
        data = _payload_dict(payload, request)
        return await _delete_impl(db, data)

    return _endpoint


def _make_create_endpoint():
    async def _endpoint(
        request: Request,
        payload: Any = Body(default=None),
        db: AsyncSession = Depends(db_session),
    ):
        data = _payload_dict(payload, request)
        return await _create_impl(db, data)

    return _endpoint


_ENABLE_PATHS = (
    "/api/groups/bulk-enable",
    "/api/groups/bulk_enable",
    "/api/groups/bulk/enable",
    "/api/camera-groups/bulk-enable",
    "/api/camera-groups/bulk_enable",
    "/api/camera-groups/bulk/enable",
    "/api/cameras/groups/bulk-enable",
    "/api/cameras/groups/bulk/enable",
)

_DISABLE_PATHS = (
    "/api/groups/bulk-disable",
    "/api/groups/bulk_disable",
    "/api/groups/bulk/disable",
    "/api/camera-groups/bulk-disable",
    "/api/camera-groups/bulk_disable",
    "/api/camera-groups/bulk/disable",
    "/api/cameras/groups/bulk-disable",
    "/api/cameras/groups/bulk/disable",
)

_ASSIGN_PATHS = (
    "/api/groups/bulk-assign",
    "/api/groups/bulk_assign",
    "/api/groups/bulk/assign",
    "/api/camera-groups/bulk-assign",
    "/api/camera-groups/bulk_assign",
    "/api/cameras/groups/bulk-assign",
)

_UNASSIGN_PATHS = (
    "/api/groups/bulk-unassign",
    "/api/groups/bulk_unassign",
    "/api/groups/bulk/unassign",
    "/api/camera-groups/bulk-unassign",
    "/api/camera-groups/bulk_unassign",
    "/api/cameras/groups/bulk-unassign",
)

_DELETE_PATHS = (
    "/api/groups/bulk-delete",
    "/api/groups/bulk_delete",
    "/api/groups/bulk/delete",
    "/api/camera-groups/bulk-delete",
    "/api/camera-groups/bulk_delete",
    "/api/cameras/groups/bulk-delete",
)

_CREATE_PATHS = (
    "/api/groups/bulk-create",
    "/api/groups/bulk_create",
    "/api/groups/bulk/create",
    "/api/camera-groups/bulk-create",
)

def _slug(p: str) -> str:
    return p.strip("/").replace("/", "_").replace("-", "_")


for _p in _ENABLE_PATHS:
    router.add_api_route(_p, _make_enable_endpoint(default=True), methods=["POST"], operation_id=f"group_bulk_enable_{_slug(_p)}")
for _p in _DISABLE_PATHS:
    router.add_api_route(_p, _make_enable_endpoint(default=False), methods=["POST"], operation_id=f"group_bulk_disable_{_slug(_p)}")
for _p in _ASSIGN_PATHS:
    router.add_api_route(_p, _make_assign_endpoint(), methods=["POST"], operation_id=f"group_bulk_assign_{_slug(_p)}")
for _p in _UNASSIGN_PATHS:
    router.add_api_route(_p, _make_unassign_endpoint(), methods=["POST"], operation_id=f"group_bulk_unassign_{_slug(_p)}")
for _p in _DELETE_PATHS:
    router.add_api_route(_p, _make_delete_endpoint(), methods=["POST"], operation_id=f"group_bulk_delete_{_slug(_p)}")
for _p in _CREATE_PATHS:
    router.add_api_route(_p, _make_create_endpoint(), methods=["POST"], operation_id=f"group_bulk_create_{_slug(_p)}")

# Generic bulk cameras toggle (enable + recording + detection in one call).
@router.post("/api/groups/bulk-cameras")
@router.post("/api/groups/bulk_cameras")
@router.post("/api/groups/bulk/cameras")
@router.post("/api/camera-groups/bulk-cameras")
async def bulk_cameras_toggle(
    request: Request,
    payload: Any = Body(default=None),
    db: AsyncSession = Depends(db_session),
):
    data = _payload_dict(payload, request)
    group_ids = _extract_group_ids(data)
    fields = _extract_fields(data)
    # If explicit per-flag values given, apply them one group at a time.
    # Otherwise fall back to single enabled toggle.
    if fields and len(fields) > 1:
        # Apply `enabled` from body, plus recording/detection flags if present.
        enabled = _extract_enabled(data, True)
        return await svc.bulk_set_group_cameras(db, group_ids, enabled=enabled, fields=fields)
    enabled = _extract_enabled(data, True)
    return await svc.bulk_set_group_cameras(db, group_ids, enabled=enabled, fields=fields)


__all__ = [
    "router",
    "BulkEnableRequest",
    "BulkAssignRequest",
    "BulkUnassignRequest",
    "BulkDeleteRequest",
    "BulkCreateRequest",
]
