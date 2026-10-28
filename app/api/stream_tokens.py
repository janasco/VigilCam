from __future__ import annotations

import asyncio
from typing import Any, AsyncGenerator, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from app.api.deps import db_session
import app.services.stream_tokens as stream_tokens_mod

router = APIRouter(tags=["stream_tokens"])


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

    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "token_request", "stream"):
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


def _extract_token(request: Request, explicit: Optional[str] = None) -> Optional[str]:
    if explicit:
        return explicit
    try:
        qp = request.query_params
        for key in ("token", "stream_token", "streamToken", "access_token", "accessToken", "auth", "t"):
            val = qp.get(key)
            if val:
                return val
    except Exception:  # noqa: BLE001
        pass
    try:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            return auth.split(" ", 1)[1].strip() or None
    except Exception:  # noqa: BLE001
        pass
    return None


async def _mjpeg_generator_for(camera_id: Any, fps: float = 1.0, frames: Optional[int] = None) -> AsyncGenerator[bytes, None]:
    """Yield MJPEG frames, delegating to app.api.streams when available."""
    try:
        from app.api.streams import _mjpeg_generator as _upstream

        async for chunk in _upstream(camera_id, fps=fps, frames=frames):
            yield chunk
        return
    except Exception:  # noqa: BLE001
        pass
    import io as _io

    def _placeholder(text: str = "VigilCam") -> bytes:
        try:
            from PIL import Image, ImageDraw

            img = Image.new("RGB", (320, 240), color=(20, 20, 30))
            draw = ImageDraw.Draw(img)
            draw.text((10, 110), text, fill=(255, 255, 255))
            buf = _io.BytesIO()
            img.save(buf, format="JPEG", quality=70)
            return buf.getvalue()
        except Exception:  # noqa: BLE001
            return b"\xff\xd8\xff\xd9"

    boundary = "frame"
    delay = 1.0 / max(0.1, float(fps or 1.0))
    count = 0
    while True:
        if frames is not None and count >= frames:
            break
        jpeg = _placeholder(f"Cam {camera_id}")
        yield (
            b"--" + boundary.encode() + b"\r\n"
            b"Content-Type: image/jpeg\r\n"
            b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n"
            + jpeg + b"\r\n"
        )
        count += 1
        await asyncio.sleep(delay)


def require_stream_token(camera_id: Any, token: Optional[str] = None, request: Optional[Request] = None) -> dict:
    """Dependency/helper gating MJPEG access on a temp stream token.

    Raises 401 when no token is supplied, 403 when invalid for the camera,
    and 410 when the token signature is valid but expired.
    """
    candidate = token
    if candidate is None and request is not None:
        candidate = _extract_token(request)
    if not candidate:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="stream token is required")
    record = stream_tokens_mod.verify_stream_token(candidate, camera_id=camera_id)
    if record is not None:
        return record
    # Distinguish expiry from invalid signature.
    try:
        payload = stream_tokens_mod._decode_token(str(candidate))
        import time as _time

        if int(payload.get("exp", 0)) <= int(_time.time()):
            raise HTTPException(status_code=status.HTTP_410_GONE, detail="Stream token expired")
        # Signature valid but camera mismatch or revoked.
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Stream token not valid for this camera")
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001
        pass
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid stream token")


_BASES = (
    "/api/stream_tokens",
    "/api/stream-tokens",
)


def _register(r: APIRouter, base: str) -> None:
    @r.get(base + "/status")
    async def tokens_status():
        return stream_tokens_mod.get_status()

    @r.get(base + "/health")
    async def tokens_health():
        result = stream_tokens_mod.get_status()
        result.setdefault("ok", True)
        return result

    @r.post(base + "/verify")
    async def verify_token(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        candidate = (
            data.get("token")
            or data.get("stream_token")
            or data.get("streamToken")
            or data.get("access_token")
            or data.get("accessToken")
            or data.get("value")
            or data.get("code")
        )
        if candidate is None:
            raise HTTPException(status_code=400, detail="token is required")
        camera_id = data.get("camera_id") or data.get("cameraId") or data.get("camera") or data.get("cam")
        record = stream_tokens_mod.verify_stream_token(candidate, camera_id=camera_id)
        if record is None:
            return {"valid": False, "ok": False, "success": False, "status": "invalid"}
        out = _public(record)
        out.update({"valid": True, "ok": True, "success": True, "status": "active"})
        return out

    @r.post(base + "/validate")
    async def validate_token(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await verify_token(payload, request, db)

    @r.get(base + "/mjpeg/{camera_id}")
    async def gated_mjpeg(
        camera_id: Any,
        request: Request,
        token: Optional[str] = Query(default=None),
        fps: float = Query(default=1.0),
        frames: Optional[int] = Query(default=None, alias="frames"),
        limit: Optional[int] = Query(default=None),
        max_frames: Optional[int] = Query(default=None),
        count: Optional[int] = Query(default=None),
    ):
        candidate = _extract_token(request, explicit=token)
        if candidate is None:
            # Also accept token passed under alternate query names.
            data = _normalize(None, request)
            candidate = (
                data.get("stream_token")
                or data.get("streamToken")
                or data.get("access_token")
                or data.get("accessToken")
            )
        require_stream_token(camera_id, token=candidate, request=None)
        if frames is None:
            frames = limit if limit is not None else (max_frames if max_frames is not None else count)
        return StreamingResponse(
            _mjpeg_generator_for(camera_id, fps=fps, frames=frames),
            media_type="multipart/x-mixed-replace; boundary=frame",
        )

    @r.get(base)
    async def list_tokens(request: Request):
        try:
            include_revoked = request.query_params.get("include_revoked", "true")
            include = str(include_revoked).lower() not in ("0", "false", "no", "off")
        except Exception:  # noqa: BLE001
            include = True
        camera_id = request.query_params.get("camera_id") or request.query_params.get("cameraId")
        items = stream_tokens_mod.list_stream_tokens(include_revoked=include, camera_id=camera_id) if camera_id else stream_tokens_mod.list_stream_tokens(include_revoked=include)
        return [_public(item) for item in items]

    @r.get(base + "/")
    async def list_tokens_slash(request: Request):
        return await list_tokens(request)

    @r.get(base + "/list")
    async def list_tokens_alias(request: Request):
        return await list_tokens(request)

    @r.get(base + "/{token_id}")
    async def get_token(token_id: str):
        record = stream_tokens_mod.get_stream_token(token_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Stream token not found")
        return _public(record)

    @r.post(base, status_code=status.HTTP_200_OK)
    async def create_token(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        try:
            result = stream_tokens_mod.create_stream_token(**data)
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
    async def create_token_slash(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await create_token(payload, request, db)

    @r.post(base + "/create")
    async def create_token_alias(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await create_token(payload, request, db)

    @r.delete(base + "/{token_id}")
    async def revoke_token(token_id: str):
        record = stream_tokens_mod.revoke_stream_token(token_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Stream token not found")
        return {
            "ok": True,
            "success": True,
            "status": "revoked",
            "id": record.get("id"),
            "token_id": record.get("id"),
            "revoked": True,
        }

    @r.post(base + "/{token_id}/revoke")
    async def revoke_token_post(token_id: str):
        return await revoke_token(token_id)


for _base in _BASES:
    _register(router, _base)
