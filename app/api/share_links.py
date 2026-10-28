from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status

from app.api.deps import db_session
import app.services.share_links as share_links_mod

router = APIRouter(tags=["share_links"])


def _normalize(payload: Any, request: Optional[Request] = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif isinstance(payload, str):
        data = {"token": payload}
    elif hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
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

    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "link", "share"):
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
    out.pop("payload", None)
    out.setdefault("ok", True)
    out.setdefault("success", True)
    return out


_BASES = (
    "/api/share_links",
    "/api/share-links",
)


def _register(r: APIRouter, base: str) -> None:
    @r.get(base + "/status")
    async def links_status():
        return share_links_mod.get_status()

    @r.get(base + "/health")
    async def links_health():
        result = share_links_mod.get_status()
        result.setdefault("ok", True)
        return result

    @r.post(base + "/verify")
    async def verify_link(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        candidate = (
            data.get("token")
            or data.get("share_token")
            or data.get("shareToken")
            or data.get("link_token")
            or data.get("code")
        )
        if candidate is None:
            raise HTTPException(status_code=400, detail="token is required")
        record = share_links_mod.verify_share_token(candidate)
        if record is None:
            return {"valid": False, "ok": False, "success": False, "status": "invalid"}
        out = _public(record)
        out.update({"valid": True, "ok": True, "success": True, "status": "active"})
        return out

    @r.get(base)
    async def list_links(request: Request):
        try:
            include_revoked = request.query_params.get("include_revoked", "true")
            include = str(include_revoked).lower() not in ("0", "false", "no", "off")
        except Exception:  # noqa: BLE001
            include = True
        resource_type = request.query_params.get("resource_type") or request.query_params.get("type")
        items = share_links_mod.list_share_links(include_revoked=include, resource_type=resource_type)
        return [_public(item) for item in items]

    @r.get(base + "/")
    async def list_links_slash(request: Request):
        return await list_links(request)

    @r.get(base + "/list")
    async def list_links_alias(request: Request):
        return await list_links(request)

    @r.get(base + "/{link_id}")
    async def get_link(link_id: str):
        record = share_links_mod.get_share_link(link_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Share link not found")
        return _public(record)

    @r.post(base, status_code=status.HTTP_200_OK)
    async def create_link(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        try:
            result = share_links_mod.create_share_link(**data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
        if not isinstance(result, dict):
            result = {"result": result, "status": "active", "success": True, "ok": True}
        result.setdefault("status", "active")
        result.setdefault("ok", True)
        result.setdefault("success", True)
        return result

    @r.post(base + "/")
    async def create_link_slash(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await create_link(payload, request, db)

    @r.post(base + "/create")
    async def create_link_alias(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await create_link(payload, request, db)

    @r.delete(base + "/{link_id}")
    async def revoke_link(link_id: str):
        record = share_links_mod.revoke_share_link(link_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Share link not found")
        return {
            "ok": True,
            "success": True,
            "status": "revoked",
            "id": record.get("id"),
            "link_id": record.get("id"),
            "revoked": True,
        }

    @r.post(base + "/{link_id}/revoke")
    async def revoke_link_post(link_id: str):
        return await revoke_link(link_id)


for _base in _BASES:
    _register(router, _base)


@router.get("/api/share/{token:path}")
async def resolve_share(token: str):
    """Public time-limited share resolver (no auth; token is the credential)."""
    record = share_links_mod.verify_share_token(token)
    if record is None:
        # Distinguish expired/tampered from unknown: try a raw decode for status.
        try:
            payload = share_links_mod._decode_token(token)
            import time as _time

            if int(payload.get("exp", 0)) <= int(_time.time()):
                raise HTTPException(status_code=410, detail="Share link expired")
        except HTTPException:
            raise
        except Exception:  # noqa: BLE001
            pass
        raise HTTPException(status_code=404, detail="Invalid share link")
    out = _public(record)
    out.update({"valid": True, "ok": True, "success": True})
    return out
