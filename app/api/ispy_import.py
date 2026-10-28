"""Classic iSpy config import REST endpoints.

Accepts classic iSpy ``objects.xml`` (``<objects><cameras><camera .../>``),
``{"xml": ...}`` envelopes, or a single camera dict, and imports entries
as VigilCam cameras. Clones (``sourceindex=10``) and deleted entries are
skipped.

No ``app/main.py`` changes are required: the application auto-loads any
``app.api`` module exposing ``router``.
"""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import db_session

router = APIRouter(tags=["ispy-import"])

_BASES = (
    "/api/ispy-import",
    "/api/ispy_import",
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


def _flags(payload: Any) -> tuple[bool, bool, bool, bool]:
    skip_invalid = True
    update_existing = False
    skip_clones = True
    skip_deleted = True
    if isinstance(payload, dict):
        if payload.get("skip_invalid") is not None:
            skip_invalid = bool(payload.get("skip_invalid"))
        elif payload.get("skipInvalid") is not None:
            skip_invalid = bool(payload.get("skipInvalid"))
        if payload.get("update_existing") is not None:
            update_existing = bool(payload.get("update_existing"))
        elif payload.get("updateExisting") is not None:
            update_existing = bool(payload.get("updateExisting"))
        if payload.get("skip_clones") is not None:
            skip_clones = bool(payload.get("skip_clones"))
        elif payload.get("skipClones") is not None:
            skip_clones = bool(payload.get("skipClones"))
        if payload.get("skip_deleted") is not None:
            skip_deleted = bool(payload.get("skip_deleted"))
        elif payload.get("skipDeleted") is not None:
            skip_deleted = bool(payload.get("skipDeleted"))
    return skip_invalid, update_existing, skip_clones, skip_deleted


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
        # Direct envelope (xml/cameras/objects/...) passes through;
        # flag-only bodies carry no cameras.
        flag_keys = {"skip_invalid", "skipInvalid", "update_existing", "updateExisting",
                     "skip_clones", "skipClones", "skip_deleted", "skipDeleted"}
        rest = {k: v for k, v in payload.items() if k not in flag_keys}
        if not rest:
            return []
        return payload
    return payload


async def _do_parse(payload: Any) -> Dict[str, Any]:
    from app.services import ispy_import as svc

    raw_payload = _coerce_payload(payload)
    raws = svc.extract_cameras(raw_payload)
    normalized = [svc.normalize_ispy_camera(r, i) for i, r in enumerate(raws)]
    results = [
        {"index": i, "valid": svc.validate_camera(n), "device": n}
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
    from app.services import ispy_import as svc

    return await svc.preview_ispy_db(db, _coerce_payload(payload))


async def _do_import(db: AsyncSession, payload: Any) -> Dict[str, Any]:
    from app.services import ispy_import as svc

    flags_payload = payload if isinstance(payload, dict) else {}
    if hasattr(payload, "model_dump"):
        try:
            flags_payload = payload.model_dump()
        except Exception:
            flags_payload = {}
    skip_invalid, update_existing, skip_clones, skip_deleted = _flags(flags_payload)
    result = await svc.import_ispy_db(
        db,
        _coerce_payload(payload),
        skip_invalid=skip_invalid,
        update_existing=update_existing,
        skip_clones=skip_clones,
        skip_deleted=skip_deleted,
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


def _sample_xml() -> str:
    return """<?xml version="1.0" encoding="utf-8"?>
<objects>
  <cameras>
    <camera id="1" name="Front Door" description="Entry" directory="video\\FrontDoor" ptz="-1" resolution="1280x720">
      <settings>
        <sourceindex>2</sourceindex>
        <videosourcestring>rtsp://admin:password@192.168.1.10:554/live</videosourcestring>
        <login>admin</login>
        <password>password</password>
        <active>true</active>
        <framerate>15</framerate>
        <maxframerate>15</maxframerate>
      </settings>
      <detector type="TwoFrames" recordondetect="true" recordonalert="false" sensitivity="70">
        <motionzones>
          <zone left="0" top="0" width="320" height="240" />
        </motionzones>
      </detector>
      <recorder bufferseconds="5" inactiverecord="10" maxrecordtime="3600" timelapse="10" timelapseframes="100" />
      <alerts minimuminterval="30" objectcountalert="0" active="true" mode="Sound" />
      <notifications emailgrabinterval="60" sendemail="false" sendsms="false" />
      <schedule active="true" />
    </camera>
  </cameras>
  <microphones />
  <floorplans />
  <remotecommands/>
</objects>"""


def _register(r: APIRouter, base: str) -> None:
    @r.get(base + "/template")
    async def get_template():
        return JSONResponse({"xml": _sample_xml()})

    @r.get(base + "/sample")
    async def get_sample():
        return JSONResponse({"xml": _sample_xml()})

    @r.get(base + "/example")
    async def get_example():
        return JSONResponse({"xml": _sample_xml()})

    @r.post(base + "/parse")
    async def parse_ispy_endpoint(request: Request, payload: Any = Body(default=None)):
        body = payload
        if body is None:
            try:
                raw = await request.body()
                body = raw.decode("utf-8", "ignore") if raw else []
            except Exception:
                body = []
        return await _do_parse(body)

    @r.post(base + "/validate")
    async def validate_ispy_endpoint(request: Request, payload: Any = Body(default=None)):
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
    async def preview_ispy_endpoint(
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
    async def import_ispy_endpoint(
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
    async def import_ispy_root(
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
ispy_import_router = router
ispyImportRouter = router
camera_ispy_router = router
camera_ispy_import_router = router

__all__ = [
    "router",
    "ispy_import_router",
    "ispyImportRouter",
    "camera_ispy_router",
    "camera_ispy_import_router",
]
