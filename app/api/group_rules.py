"""Camera grouping auto-rules API router.

Exposes CRUD plus preview/apply for
:class:`app.services.group_rules` auto-grouping rules.

Paths (all equivalent; hyphen/underscore variants for tolerance):

- ``/api/group-rules``
- ``/api/group_rules``
- ``/api/camera-group-rules``
- ``/api/groups/rules``
- ``/api/groups/auto-rules``

The router has no prefix so the auto-loader in ``app.main`` picks up every
alias. ``db_session`` is only required for preview/apply against the DB;
rule CRUD itself is file-backed and needs no DB access.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import db_session
from app.services import group_rules as service

router = APIRouter(tags=["group_rules"])


# ---------------------------------------------------------------------------
# Schemas (local so we do not touch the shared schemas package)
# ---------------------------------------------------------------------------


class GroupRuleCreate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=120)
    group: Optional[str] = Field(default=None, max_length=120)
    group_name: Optional[str] = Field(default=None, max_length=120)
    target_group: Optional[str] = Field(default=None, max_length=120)
    target: Optional[str] = Field(default=None, max_length=120)
    field: Optional[str] = None
    attribute: Optional[str] = None
    operator: Optional[str] = None
    op: Optional[str] = None
    value: Optional[Any] = None
    pattern: Optional[str] = None
    priority: Optional[int] = None
    enabled: Optional[bool] = None
    active: Optional[bool] = None

    model_config = {"extra": "allow"}


class GroupRuleUpdate(BaseModel):
    name: Optional[str] = None
    group: Optional[str] = None
    group_name: Optional[str] = None
    target_group: Optional[str] = None
    field: Optional[str] = None
    attribute: Optional[str] = None
    operator: Optional[str] = None
    op: Optional[str] = None
    value: Optional[Any] = None
    pattern: Optional[str] = None
    priority: Optional[int] = None
    enabled: Optional[bool] = None
    active: Optional[bool] = None

    model_config = {"extra": "allow"}


class GroupRuleRead(BaseModel):
    id: str
    name: str
    group: str
    group_name: Optional[str] = None
    target_group: Optional[str] = None
    field: str
    operator: str
    value: str
    priority: int = 100
    enabled: bool = True

    model_config = {"from_attributes": True, "extra": "allow"}


def _to_read(rule: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": str(rule.get("id")),
        "name": str(rule.get("name", "")),
        "group": str(rule.get("group", "")),
        "group_name": str(rule.get("group_name", rule.get("group", ""))),
        "target_group": str(rule.get("target_group", rule.get("group", ""))),
        "field": str(rule.get("field", "name")),
        "operator": str(rule.get("operator", "contains")),
        "value": str(rule.get("value", "")),
        "priority": int(rule.get("priority", 100)),
        "enabled": bool(rule.get("enabled", True)),
    }


def _payload_from_body(payload: Any, request: Optional[Request] = None) -> Dict[str, Any]:
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
    # Drop None entries coming from unset optional pydantic fields.
    data = {k: v for k, v in data.items() if v is not None}
    if request is not None:
        try:
            for k, v in request.query_params.items():
                if k not in data or data.get(k) is None:
                    data[k] = v
        except Exception:  # noqa: BLE001
            pass
    # Flatten common wrappers.
    for nested_key in ("payload", "rule", "config", "params", "data", "body"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    # Alias normalization for tolerant clients.
    if data.get("group") is None:
        for k in ("group_name", "target_group", "target", "name_of_group"):
            if data.get(k) is not None:
                data["group"] = data[k]
                break
    if data.get("field") is None and data.get("attribute") is not None:
        data["field"] = data["attribute"]
    if data.get("operator") is None and data.get("op") is not None:
        data["operator"] = data["op"]
    if data.get("value") is None and data.get("pattern") is not None:
        data["value"] = data["pattern"]
    if data.get("enabled") is None and data.get("active") is not None:
        data["enabled"] = data["active"]
    return data


async def _db_cameras(db: AsyncSession) -> List[Any]:
    from sqlalchemy import select

    from app.models.entities import Camera

    result = await db.execute(select(Camera).order_by(Camera.id))
    return list(result.scalars().all())


def _register(r: APIRouter, base: str) -> None:
    @r.get(base)
    async def list_rules_ep():
        return [_to_read(x) for x in service.list_rules()]

    @r.get(base + "/")
    async def list_rules_slash_ep():
        return [_to_read(x) for x in service.list_rules()]

    @r.post(base, status_code=status.HTTP_201_CREATED)
    async def create_rule_ep(payload: Any = Body(default=None), request: Request = None):
        data = _payload_from_body(payload, request)
        try:
            rule = service.create_rule(data)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
        return _to_read(rule)

    @r.post(base + "/", status_code=status.HTTP_201_CREATED)
    async def create_rule_slash_ep(payload: Any = Body(default=None), request: Request = None):
        data = _payload_from_body(payload, request)
        try:
            rule = service.create_rule(data)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
        return _to_read(rule)

    @r.get(base + "/preview")
    async def preview_get_ep(db: AsyncSession = Depends(db_session)):
        cameras = await _db_cameras(db)
        return {"preview": service.preview_cameras(cameras), "count": len(cameras)}

    @r.post(base + "/preview")
    async def preview_post_ep(payload: Any = Body(default=None), db: AsyncSession = Depends(db_session)):
        data = payload if isinstance(payload, dict) else {}
        inline = data.get("cameras") or data.get("camera_list") or data.get("items")
        if isinstance(inline, list) and inline:
            preview = service.preview_cameras(inline)
            return {"preview": preview, "count": len(preview)}
        cameras = await _db_cameras(db)
        # Optional single-camera ad-hoc check: {"camera": {...}, "field": ...}?
        if isinstance(data, dict) and isinstance(data.get("camera"), dict):
            preview = service.preview_cameras([data["camera"]])
            return {"preview": preview, "count": len(preview)}
        return {"preview": service.preview_cameras(cameras), "count": len(cameras)}

    @r.post(base + "/apply")
    async def apply_post_ep(db: AsyncSession = Depends(db_session)):
        summary = await service.apply_rules(db)
        summary.setdefault("ok", True)
        return summary

    @r.post(base + "/run")
    async def run_post_ep(db: AsyncSession = Depends(db_session)):
        summary = await service.apply_rules(db)
        summary.setdefault("ok", True)
        return summary

    @r.get(base + "/{rule_id}")
    async def get_rule_ep(rule_id: str):
        rule = service.get_rule(rule_id)
        if rule is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group rule not found")
        return _to_read(rule)

    @r.patch(base + "/{rule_id}")
    async def patch_rule_ep(rule_id: str, payload: Any = Body(default=None), request: Request = None):
        data = _payload_from_body(payload, request)
        # PATCH with empty body on an existing rule is a no-op success.
        if not data:
            rule = service.get_rule(rule_id)
            if rule is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group rule not found")
            return _to_read(rule)
        try:
            updated = service.update_rule(rule_id, data)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
        if updated is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group rule not found")
        return _to_read(updated)

    @r.put(base + "/{rule_id}")
    async def put_rule_ep(rule_id: str, payload: Any = Body(default=None), request: Request = None):
        return await patch_rule_ep(rule_id, payload, request)

    @r.delete(base + "/{rule_id}", status_code=status.HTTP_200_OK)
    async def delete_rule_ep(rule_id: str):
        ok = service.delete_rule(rule_id)
        if not ok:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group rule not found")
        return {"ok": True, "deleted": True, "id": rule_id}


_BASES = (
    "/api/group-rules",
    "/api/group_rules",
    "/api/camera-group-rules",
    "/api/camera_group_rules",
    "/api/groups/rules",
    "/api/groups/auto-rules",
)

for _base in _BASES:
    _register(router, _base)


# Prefixed alias router for callers expecting ``router.prefix`` to be set.
group_rules_router = APIRouter(prefix="/api/group-rules", tags=["group_rules"])


def _make_prefixed(r: APIRouter) -> None:
    @r.get("")
    async def _list():
        return [_to_read(x) for x in service.list_rules()]

    @r.get("/")
    async def _list_slash():
        return [_to_read(x) for x in service.list_rules()]

    @r.post("", status_code=status.HTTP_201_CREATED)
    async def _create(payload: Any = Body(default=None), request: Request = None):
        data = _payload_from_body(payload, request)
        try:
            return _to_read(service.create_rule(data))
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    @r.post("/", status_code=status.HTTP_201_CREATED)
    async def _create_slash(payload: Any = Body(default=None), request: Request = None):
        data = _payload_from_body(payload, request)
        try:
            return _to_read(service.create_rule(data))
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    @r.get("/preview")
    async def _preview_get(db: AsyncSession = Depends(db_session)):
        cameras = await _db_cameras(db)
        return {"preview": service.preview_cameras(cameras), "count": len(cameras)}

    @r.post("/preview")
    async def _preview_post(payload: Any = Body(default=None), db: AsyncSession = Depends(db_session)):
        data = payload if isinstance(payload, dict) else {}
        inline = data.get("cameras") if isinstance(data, dict) else None
        if isinstance(inline, list) and inline:
            preview = service.preview_cameras(inline)
            return {"preview": preview, "count": len(preview)}
        cameras = await _db_cameras(db)
        return {"preview": service.preview_cameras(cameras), "count": len(cameras)}

    @r.post("/apply")
    async def _apply(db: AsyncSession = Depends(db_session)):
        summary = await service.apply_rules(db)
        summary.setdefault("ok", True)
        return summary

    @r.post("/run")
    async def _run(db: AsyncSession = Depends(db_session)):
        summary = await service.apply_rules(db)
        summary.setdefault("ok", True)
        return summary

    @r.get("/{rule_id}")
    async def _get(rule_id: str):
        rule = service.get_rule(rule_id)
        if rule is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group rule not found")
        return _to_read(rule)

    @r.patch("/{rule_id}")
    async def _patch(rule_id: str, payload: Any = Body(default=None), request: Request = None):
        data = _payload_from_body(payload, request)
        if not data:
            rule = service.get_rule(rule_id)
            if rule is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group rule not found")
            return _to_read(rule)
        try:
            updated = service.update_rule(rule_id, data)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
        if updated is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group rule not found")
        return _to_read(updated)

    @r.put("/{rule_id}")
    async def _put(rule_id: str, payload: Any = Body(default=None), request: Request = None):
        return await _patch(rule_id, payload, request)

    @r.delete("/{rule_id}")
    async def _delete(rule_id: str):
        ok = service.delete_rule(rule_id)
        if not ok:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group rule not found")
        return {"ok": True, "deleted": True, "id": rule_id}


_make_prefixed(group_rules_router)

# Compatibility aliases.
groupRulesRouter = router
group_rules_api_router = router
camera_group_rules_router = router

__all__ = [
    "router",
    "group_rules_router",
    "groupRulesRouter",
    "group_rules_api_router",
    "camera_group_rules_router",
    "GroupRuleCreate",
    "GroupRuleUpdate",
    "GroupRuleRead",
]
