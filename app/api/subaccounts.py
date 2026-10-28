from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status

from app.api.deps import db_session
import app.services.subaccounts as subaccounts_service_mod
from app.services.subaccounts import subaccounts_service

router = APIRouter(tags=["subaccounts"])


def _normalize(payload: Any, request: Optional[Request] = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif isinstance(payload, str):
        data = {"username": payload}
    elif hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        try:
            data = {"username": bytes(payload).decode("utf-8", errors="replace")}
        except Exception:  # noqa: BLE001
            data = {}
    else:
        try:
            data = dict(payload)
        except Exception:  # noqa: BLE001
            data = {}

    if request is not None:
        try:
            for k, v in request.query_params.items():
                if k not in data or data.get(k) is None:
                    data[k] = v
        except Exception:  # noqa: BLE001
            pass

    for nested_key in ("payload", "options", "config", "params", "data", "body",
                       "request", "settings", "subaccount", "account", "child", "user"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)

    return data


def _public(record: Any) -> dict:
    if isinstance(record, dict):
        out = dict(record)
    else:
        try:
            out = dict(record)
        except Exception:  # noqa: BLE001
            return {"value": str(record)}
    for secret_field in ("password_hash", "passwordHash", "hashed_password"):
        out.pop(secret_field, None)
    # alias camera scope fields so all naming styles are present
    cams = out.get("camera_ids")
    if cams is None:
        cams = out.get("cameras") or out.get("allowed_cameras") or []
    try:
        cams = list(cams)
    except Exception:  # noqa: BLE001
        cams = []
    out["camera_ids"] = cams
    out["cameraIds"] = cams
    out["cameras"] = cams
    out["allowed_cameras"] = cams
    out["camera_scope"] = cams
    out.setdefault("ok", True)
    out.setdefault("success", True)
    if out.get("status") is None:
        out["status"] = "revoked" if out.get("revoked") else ("active" if out.get("active", True) else "disabled")
    out.setdefault("state", out.get("status"))
    return out


_PATHS = (
    "/api/subaccounts",
    "/api/sub-accounts",
    "/api/sub_accounts",
)


def _register(r: APIRouter, base: str) -> None:
    @r.get(base)
    async def list_subaccounts(request: Request):
        parent = (
            request.query_params.get("parent_id")
            or request.query_params.get("parentId")
            or request.query_params.get("parent")
            or request.query_params.get("owner")
        )
        items = subaccounts_service_mod.list_subaccounts(parent_id=parent)
        return [_public(x) for x in items]

    @r.get(base + "/")
    async def list_subaccounts_slash(request: Request):
        return await list_subaccounts(request)

    @r.get(base + "/list")
    async def list_subaccounts_alias(request: Request):
        return await list_subaccounts(request)

    @r.get(base + "/status")
    async def subaccounts_status():
        return subaccounts_service_mod.get_status()

    @r.get(base + "/health")
    async def subaccounts_health():
        svc_status = subaccounts_service_mod.get_status()
        svc_status.setdefault("ok", True)
        return svc_status

    @r.get(base + "/{subaccount_id}")
    async def get_subaccount(subaccount_id: str):
        record = subaccounts_service_mod.get_subaccount(subaccount_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Sub-account not found")
        return _public(record)

    @r.get(base + "/{subaccount_id}/cameras")
    async def list_subaccount_cameras(subaccount_id: str):
        record = subaccounts_service_mod.get_subaccount(subaccount_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Sub-account not found")
        cams = subaccounts_service_mod.subaccounts_service.list_cameras(subaccount_id)
        perms = dict(record.get("permissions", {}) or {})
        return {
            "ok": True,
            "success": True,
            "id": record.get("id"),
            "subaccount_id": record.get("id"),
            "camera_ids": cams,
            "cameras": cams,
            "permissions": {str(k): v for k, v in perms.items()},
        }

    @r.post(base, status_code=status.HTTP_200_OK)
    async def create_subaccount(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        try:
            result = subaccounts_service_mod.create_subaccount(**data)
        except ValueError as exc:
            msg = str(exc)
            code = status.HTTP_409_CONFLICT if "already exists" in msg else status.HTTP_400_BAD_REQUEST
            raise HTTPException(status_code=code, detail=msg)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        if not isinstance(result, dict):
            result = {"result": result, "status": "active", "success": True, "ok": True}
        result.setdefault("status", "active")
        result.setdefault("state", "active")
        result.setdefault("ok", True)
        result.setdefault("success", True)
        return _public(result)

    @r.post(base + "/")
    async def create_subaccount_slash(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await create_subaccount(payload, request, db)

    @r.post(base + "/create")
    async def create_subaccount_alias(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await create_subaccount(payload, request, db)

    @r.put(base + "/{subaccount_id}")
    async def update_subaccount_put(subaccount_id: str, payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        try:
            record = subaccounts_service_mod.update_subaccount(subaccount_id, **data)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        if record is None:
            raise HTTPException(status_code=404, detail="Sub-account not found")
        return _public(record)

    @r.patch(base + "/{subaccount_id}")
    async def update_subaccount_patch(subaccount_id: str, payload: Any = Body(default=None), request: Request = None):
        return await update_subaccount_put(subaccount_id, payload, request)

    @r.post(base + "/{subaccount_id}/cameras")
    async def grant_subaccount_camera(subaccount_id: str, payload: Any = Body(default=None), request: Request = None):
        record = subaccounts_service_mod.get_subaccount(subaccount_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Sub-account not found")
        data = _normalize(payload, request)
        camera_id = (
            data.get("camera_id") or data.get("cameraId") or data.get("camera")
            or data.get("camera_ids") or data.get("cameras")
        )
        if isinstance(camera_id, list):
            # bulk grant
            permission = data.get("permission") or data.get("perm") or "view"
            for cid in camera_id:
                subaccounts_service_mod.grant_camera_access(subaccount_id, cid, permission)
            updated = subaccounts_service_mod.get_subaccount(subaccount_id)
            return _public(updated)
        permission = data.get("permission") or data.get("perm") or data.get("access") or "view"
        if camera_id is None:
            raise HTTPException(status_code=400, detail="camera_id is required")
        updated = subaccounts_service_mod.grant_camera_access(subaccount_id, camera_id, permission)
        if updated is None:
            raise HTTPException(status_code=400, detail="Could not grant camera access")
        return _public(updated)

    @r.put(base + "/{subaccount_id}/cameras")
    async def set_subaccount_cameras(subaccount_id: str, payload: Any = Body(default=None), request: Request = None):
        record = subaccounts_service_mod.get_subaccount(subaccount_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Sub-account not found")
        data = _normalize(payload, request)
        from app.services.subaccounts import _parse_camera_ids as _parse

        raw = (
            data.get("camera_ids") or data.get("cameraIds") or data.get("cameras")
            or data.get("camera_id") or data.get("allowed_cameras") or data.get("scope")
        )
        if raw is None and isinstance(payload, list):
            raw = payload
        cams = _parse(raw)
        updated = subaccounts_service_mod.subaccounts_service.set_cameras(subaccount_id, cams)
        if updated is None:
            raise HTTPException(status_code=404, detail="Sub-account not found")
        return _public(updated)

    @r.delete(base + "/{subaccount_id}/cameras/{camera_id}")
    async def revoke_subaccount_camera(subaccount_id: str, camera_id: str):
        record = subaccounts_service_mod.get_subaccount(subaccount_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Sub-account not found")
        updated = subaccounts_service_mod.revoke_camera_access(subaccount_id, camera_id)
        if updated is None:
            raise HTTPException(status_code=404, detail="Sub-account not found")
        return _public(updated)

    @r.delete(base + "/{subaccount_id}")
    async def delete_subaccount(subaccount_id: str):
        record = subaccounts_service_mod.get_subaccount(subaccount_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Sub-account not found")
        ok = subaccounts_service_mod.delete_subaccount(subaccount_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Sub-account not found")
        return {
            "ok": True,
            "success": True,
            "status": "revoked",
            "id": record.get("id"),
            "subaccount_id": record.get("id"),
            "revoked": True,
        }

    @r.post(base + "/{subaccount_id}/revoke")
    async def revoke_subaccount_post(subaccount_id: str):
        record = subaccounts_service_mod.get_subaccount(subaccount_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Sub-account not found")
        revoked = subaccounts_service_mod.subaccounts_service.revoke_subaccount(subaccount_id)
        return _public(revoked)

    @r.post(base + "/{subaccount_id}/disable")
    async def disable_subaccount_post(subaccount_id: str):
        record = subaccounts_service_mod.subaccounts_service.disable_subaccount(subaccount_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Sub-account not found")
        return _public(record)

    @r.post(base + "/{subaccount_id}/enable")
    async def enable_subaccount_post(subaccount_id: str):
        record = subaccounts_service_mod.subaccounts_service.enable_subaccount(subaccount_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Sub-account not found")
        return _public(record)


for _base in _PATHS:
    _register(router, _base)


# Prefixed alias routers for callers expecting router.prefix
underscore_router = APIRouter(prefix="/api/subaccounts", tags=["subaccounts"])
hyphen_router = APIRouter(prefix="/api/sub-accounts", tags=["subaccounts"])


def _make_prefixed(r: APIRouter) -> None:
    @r.get("")
    async def root_get(request: Request):
        return [_public(x) for x in subaccounts_service_mod.list_subaccounts()]

    @r.get("/")
    async def slash_get(request: Request):
        return [_public(x) for x in subaccounts_service_mod.list_subaccounts()]

    @r.get("/list")
    async def list_get(request: Request):
        return [_public(x) for x in subaccounts_service_mod.list_subaccounts()]

    @r.get("/status")
    async def status_get():
        return subaccounts_service_mod.get_status()

    @r.get("/health")
    async def health_get():
        svc_status = subaccounts_service_mod.get_status()
        svc_status.setdefault("ok", True)
        return svc_status

    @r.get("/{subaccount_id}")
    async def one_get(subaccount_id: str):
        record = subaccounts_service_mod.get_subaccount(subaccount_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Sub-account not found")
        return _public(record)

    @r.post("")
    async def root_post(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        try:
            result = subaccounts_service_mod.create_subaccount(**data)
        except ValueError as exc:
            msg = str(exc)
            code = status.HTTP_409_CONFLICT if "already exists" in msg else status.HTTP_400_BAD_REQUEST
            raise HTTPException(status_code=code, detail=msg)
        result.setdefault("status", "active")
        result.setdefault("ok", True)
        result.setdefault("success", True)
        return _public(result)

    @r.post("/")
    async def slash_post(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await root_post(payload, request, db)

    @r.delete("/{subaccount_id}")
    async def one_delete(subaccount_id: str):
        record = subaccounts_service_mod.get_subaccount(subaccount_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Sub-account not found")
        subaccounts_service_mod.delete_subaccount(subaccount_id)
        return {"ok": True, "success": True, "status": "revoked", "id": record.get("id"), "revoked": True}


_make_prefixed(underscore_router)
_make_prefixed(hyphen_router)
