"""Camera QR onboarding codes API router."""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Query, Request, status
from fastapi.exceptions import HTTPException
from fastapi.responses import Response

import app.services.qr_onboard as svc

router = APIRouter(tags=["qr_onboard"])


def _normalize(payload: Any, request: Optional[Request] = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif isinstance(payload, str):
        data = {"code": payload}
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
    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "camera"):
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
    out.pop("png_bytes_raw", None)
    # Don't leak raw bytes objects; keep base64/data_url only.
    val = out.get("png_bytes")
    if isinstance(val, (bytes, bytearray)):
        out["png_bytes"] = len(val)
    out.setdefault("ok", True)
    return out


def _png_response(png: bytes) -> Response:
    return Response(content=bytes(png), media_type="image/png")


_BASES = (
    "/api/qr-onboard",
    "/api/qr_onboard",
)


def _register(r: APIRouter, base: str) -> None:
    @r.get(base + "/status")
    async def qr_status():
        return svc.get_status()

    @r.get(base + "/health")
    async def qr_health():
        result = svc.get_status()
        result.setdefault("ok", True)
        return result

    @r.post(base + "/build")
    async def build_qr(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        try:
            result = svc.generate_camera_qr(
                camera_id=data.get("camera_id") or data.get("cameraId") or data.get("id"),
                host=data.get("host") or data.get("ip") or data.get("ip_address"),
                port=data.get("port"),
                username=data.get("username") or data.get("user"),
                name=data.get("name") or data.get("camera_name"),
                server_url=data.get("server_url") or data.get("serverUrl") or data.get("base_url"),
                onvif_host=data.get("onvif_host") or data.get("onvifHost"),
                ec=data.get("ec") or data.get("ec_level") or "M",
                scale=data.get("scale"),
                border=data.get("border"),
                **{k: v for k, v in data.items() if k not in (
                    "camera_id", "cameraId", "id", "host", "ip", "ip_address",
                    "port", "username", "user", "name", "camera_name",
                    "server_url", "serverUrl", "base_url", "onvif_host",
                    "onvifHost", "ec", "ec_level", "scale", "border",
                )},
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        png = result.pop("png_bytes", b"")
        out = _public(result)
        # generate_camera_qr returns raw png bytes under png_bytes; expose length + base64.
        import base64 as _b64

        if isinstance(png, (bytes, bytearray)) and png:
            out["png_base64"] = _b64.b64encode(bytes(png)).decode("ascii")
            out["png_bytes"] = len(bytes(png))
        return out

    @r.get(base + "/png")
    async def qr_png_adhoc(
        request: Request,
        text: Optional[str] = Query(default=None),
        code: Optional[str] = Query(default=None),
        scale: Optional[int] = Query(default=None),
        border: Optional[int] = Query(default=None),
        ec: Optional[str] = Query(default=None),
    ):
        data = _normalize(None, request)
        raw = text or code or data.get("text") or data.get("code") or data.get("qr_text")
        if not raw:
            raise HTTPException(status_code=400, detail="text or code query param is required")
        # If the caller passed a stored code id, resolve to its QR text.
        rec = svc.get_onboarding_code(raw)
        qr_text = rec.get("text") or rec.get("code") if rec else raw
        try:
            png = svc.render_qr_png(qr_text, scale=scale or 8, border=border if border is not None else 4, ec=ec or "M")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return _png_response(png)

    @r.get(base + "/{code_id}/png")
    async def qr_png_by_id(
        code_id: str,
        scale: Optional[int] = Query(default=None),
        border: Optional[int] = Query(default=None),
    ):
        rec = svc.get_onboarding_code(code_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="QR onboarding code not found")
        qr_text = rec.get("text") or rec.get("code")
        try:
            png = svc.render_qr_png(
                qr_text,
                scale=scale or 8,
                border=border if border is not None else 4,
                ec=rec.get("ec") or "M",
                mask=rec.get("mask", 0),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return _png_response(png)

    @r.post(base + "/verify")
    async def verify_qr(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        candidate = (
            data.get("code") or data.get("text") or data.get("qr_text")
            or data.get("token") or data.get("code_id") or data.get("id")
        )
        if candidate is None:
            raise HTTPException(status_code=400, detail="code is required")
        record = svc.verify_onboarding_code(candidate)
        if record is None:
            return {"valid": False, "ok": False, "status": "invalid"}
        out = _public(record)
        out.update({"valid": True, "ok": True, "status": "active"})
        return out

    @r.get(base)
    async def list_codes(request: Request):
        try:
            include = str(request.query_params.get("include_revoked", "true")).lower() not in ("0", "false", "no", "off")
        except Exception:  # noqa: BLE001
            include = True
        items = svc.list_onboarding_codes(include_revoked=include)
        return [_public(item) for item in items]

    @r.get(base + "/")
    async def list_codes_slash(request: Request):
        return await list_codes(request)

    @r.get(base + "/list")
    async def list_codes_alias(request: Request):
        return await list_codes(request)

    @r.get(base + "/{code_id}")
    async def get_code(code_id: str):
        record = svc.get_onboarding_code(code_id)
        if record is None:
            # Try stateless decode: a raw QR text is itself a valid lookup.
            try:
                payload = svc.decode_onboarding_text(code_id)
            except ValueError:
                payload = None
            if payload is not None:
                verified = svc.verify_onboarding_code(code_id)
                if verified is not None:
                    return _public(verified)
            raise HTTPException(status_code=404, detail="QR onboarding code not found")
        return _public(record)

    @r.post(base, status_code=status.HTTP_200_OK)
    async def create_code(payload: Any = Body(default=None), request: Request = None):
        data = _normalize(payload, request)
        _explicit = {
            "camera_id", "cameraId", "id", "host", "ip", "ip_address",
            "port", "username", "user", "name", "camera_name",
            "server_url", "serverUrl", "base_url", "onvif_host",
            "onvifHost", "ttl_seconds", "ttl", "expires_in_seconds",
            "ec", "ec_level", "scale", "border",
        }
        try:
            result = svc.create_onboarding_code(
                camera_id=data.get("camera_id") or data.get("cameraId") or data.get("id"),
                host=data.get("host") or data.get("ip") or data.get("ip_address"),
                port=data.get("port"),
                username=data.get("username") or data.get("user"),
                name=data.get("name") or data.get("camera_name"),
                server_url=data.get("server_url") or data.get("serverUrl") or data.get("base_url"),
                onvif_host=data.get("onvif_host") or data.get("onvifHost"),
                ttl_seconds=data.get("ttl_seconds") or data.get("ttl") or data.get("expires_in_seconds"),
                ec=data.get("ec") or data.get("ec_level") or "M",
                scale=data.get("scale"),
                border=data.get("border"),
                **{k: v for k, v in data.items() if k not in _explicit},
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        out = _public(result)
        out.setdefault("status", "active")
        out.setdefault("ok", True)
        return out

    @r.post(base + "/")
    async def create_code_slash(payload: Any = Body(default=None), request: Request = None):
        return await create_code(payload, request)

    @r.post(base + "/create")
    async def create_code_alias(payload: Any = Body(default=None), request: Request = None):
        return await create_code(payload, request)

    @r.delete(base + "/{code_id}")
    async def revoke_code(code_id: str):
        record = svc.revoke_onboarding_code(code_id)
        if record is None:
            raise HTTPException(status_code=404, detail="QR onboarding code not found")
        return {
            "ok": True,
            "status": "revoked",
            "id": record.get("id"),
            "code_id": record.get("id"),
            "revoked": True,
        }

    @r.post(base + "/{code_id}/revoke")
    async def revoke_code_post(code_id: str):
        return await revoke_code(code_id)


for _base in _BASES:
    _register(router, _base)


qr_onboard_router = router
qr_router = router
onboard_router = router

__all__ = ["router"]
