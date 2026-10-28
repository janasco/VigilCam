"""Agent DVR config import REST endpoints.

Accepts Agent DVR-style device configs (``getObjects``/``getObject`` JSON,
``MediaObjects`` config exports, or a single device dict) and imports them
as VigilCam cameras. Microphones (``ot=1``) are skipped.

No ``app/main.py`` changes are required: the application auto-loads any
``app.api`` module exposing ``router``.
"""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import db_session

router = APIRouter(tags=["agent-import"])

_BASES = (
    "/api/agent-import",
    "/api/agent_import",
)


def _to_camera_dict(camera: Any) -> Dict[str, Any]:
    if isinstance(camera, dict):
        return dict(camera)
    try:
        from app.schemas import CameraRead

        return CameraRead.model_validate(camera, from_attributes=True).model_dump(mode="json")
    except Exception:
        pass
    data: Dict[str, Any] = {}
    for key in ("id", "name", "kind", "source", "onvif_host", "ip_address", "onvif_port"):
        try:
            data[key] = getattr(camera, key, None)
        except Exception:
            data[key] = None
    return data


def _flags(payload: Any) -> tuple[bool, bool, bool]:
    skip_invalid = True
    update_existing = False
    skip_microphones = True
    if isinstance(payload, dict):
        if payload.get("skip_invalid") is not None:
            skip_invalid = bool(payload.get("skip_invalid"))
        elif payload.get("skipInvalid") is not None:
            skip_invalid = bool(payload.get("skipInvalid"))
        if payload.get("update_existing") is not None:
            update_existing = bool(payload.get("update_existing"))
        elif payload.get("updateExisting") is not None:
            update_existing = bool(payload.get("updateExisting"))
        if payload.get("skip_microphones") is not None:
            skip_microphones = bool(payload.get("skip_microphones"))
        elif payload.get("skipMicrophones") is not None:
            skip_microphones = bool(payload.get("skipMicrophones"))
        elif payload.get("include_microphones") is not None:
            skip_microphones = not bool(payload.get("include_microphones"))
        elif payload.get("includeMicrophones") is not None:
            skip_microphones = not bool(payload.get("includeMicrophones"))
    return skip_invalid, update_existing, skip_microphones


def _coerce_payload(payload: Any, request: Request | None = None) -> Any:
    if hasattr(payload, "model_dump"):
        try:
            payload = payload.model_dump()
        except Exception:
            pass
    if payload is None and request is not None:
        try:
            qp = dict(request.query_params)
        except Exception:
            qp = {}
        if qp.get("name") and (qp.get("source") or qp.get("url")):
            return [qp]
        return []
    if isinstance(payload, dict):
        # Direct envelope (devices/cameras/objects/...) passes through;
        # flag-only bodies carry no devices.
        flag_keys = {"skip_invalid", "skipInvalid", "update_existing", "updateExisting",
                     "skip_microphones", "skipMicrophones", "include_microphones",
                     "includeMicrophones"}
        rest = {k: v for k, v in payload.items() if k not in flag_keys}
        if not rest:
            return []
        return payload
    return payload


async def _do_parse(payload: Any) -> Dict[str, Any]:
    from app.services import agent_import as svc

    raw_payload = _coerce_payload(payload)
    raws = svc.extract_devices(raw_payload)
    normalized = [svc.normalize_agent_device(r, i) for i, r in enumerate(raws)]
    results = [
        {"index": i, "valid": svc.validate_device(n), "device": n}
        for i, n in enumerate(normalized)
    ]
    valid = sum(1 for r in results if r["valid"])
    return {
        "count": len(normalized),
        "valid": valid,
        "invalid": len(normalized) - valid,
        "devices": normalized,
        "results": results,
    }


async def _do_preview(db: AsyncSession, payload: Any) -> List[Dict[str, Any]]:
    from app.services import agent_import as svc

    return await svc.preview_agent_db(db, _coerce_payload(payload))


async def _do_import(db: AsyncSession, payload: Any) -> Dict[str, Any]:
    from app.services import agent_import as svc

    flags_payload = payload if isinstance(payload, dict) else {}
    if hasattr(payload, "model_dump"):
        try:
            flags_payload = payload.model_dump()
        except Exception:
            flags_payload = {}
    skip_invalid, update_existing, skip_microphones = _flags(flags_payload)
    result = await svc.import_agent_db(
        db,
        _coerce_payload(payload),
        skip_invalid=skip_invalid,
        update_existing=update_existing,
        skip_microphones=skip_microphones,
    )
    created = result.get("imported", [])
    return {
        "imported": len(created),
        "count": result.get("count", len(created)),
        "total": result.get("total", len(created)),
        "cameras": created,
        "created": created,
        "errors": result.get("errors", []),
        "total_errors": result.get("total_errors", len(result.get("errors", []))),
        "skipped": result.get("skipped", []),
        "total_skipped": result.get("total_skipped", len(result.get("skipped", []))),
    }


def _sample_device() -> Dict[str, Any]:
    return {
        "oid": 1,
        "ot": 2,
        "name": "Front Door",
        "group": "Outdoor",
        "location": "Home",
        "enabled": True,
        "settings": {
            "videosource": "rtsp://admin:password@192.168.1.10:554/live",
            "username": "admin",
            "password": "password",
            "width": 1920,
            "height": 1080,
        },
    }


def _register(r: APIRouter, base: str) -> None:
    @r.get(base + "/template")
    async def get_template():
        return JSONResponse({"devices": [_sample_device()]})

    @r.get(base + "/sample")
    async def get_sample():
        return JSONResponse({"devices": [_sample_device()]})

    @r.get(base + "/example")
    async def get_example():
        return JSONResponse({"MediaObjects": [_sample_device()]})

    @r.post(base + "/parse")
    async def parse_agent_endpoint(request: Request, payload: Any = Body(default=None)):
        body = payload
        if body is None:
            try:
                raw = await request.body()
                body = raw.decode("utf-8", "ignore") if raw else []
            except Exception:
                body = []
        return await _do_parse(body)

    @r.post(base + "/validate")
    async def validate_agent_endpoint(request: Request, payload: Any = Body(default=None)):
        body = payload
        if body is None:
            try:
                raw = await request.body()
                body = raw.decode("utf-8", "ignore") if raw else []
            except Exception:
                body = []
        parsed = await _do_parse(body)
        return {
            "count": parsed["count"],
            "valid": parsed["valid"],
            "invalid": parsed["invalid"],
            "results": parsed["results"],
        }

    @r.post(base + "/preview")
    async def preview_agent_endpoint(
        request: Request,
        payload: Any = Body(default=None),
        db: AsyncSession = Depends(db_session),
    ):
        body = payload
        if body is None:
            try:
                raw = await request.body()
                body = raw.decode("utf-8", "ignore") if raw else []
            except Exception:
                body = []
        return await _do_preview(db, body)

    @r.post(base + "/import")
    async def import_agent_endpoint(
        request: Request,
        payload: Any = Body(default=None),
        db: AsyncSession = Depends(db_session),
    ):
        body = payload
        if body is None:
            try:
                raw = await request.body()
                body = raw.decode("utf-8", "ignore") if raw else []
            except Exception:
                body = []
        return await _do_import(db, body)

    @r.post(base)
    async def import_agent_root(
        request: Request,
        payload: Any = Body(default=None),
        db: AsyncSession = Depends(db_session),
    ):
        body = payload
        if body is None:
            try:
                raw = await request.body()
                body = raw.decode("utf-8", "ignore") if raw else []
            except Exception:
                body = []
        return await _do_import(db, body)


for _base in _BASES:
    _register(router, _base)


# Compatibility aliases for varied import styles.
agent_import_router = router
agentImportRouter = router
camera_agent_router = router
camera_agent_import_router = router

__all__ = [
    "router",
    "agent_import_router",
    "agentImportRouter",
    "camera_agent_router",
    "camera_agent_import_router",
]
