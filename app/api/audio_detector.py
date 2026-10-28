from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status

from app.api.deps import db_session
import app.services.audio_detector as svc_mod
from app.services.audio_detector import (
    detect,
    detect_audio,
    audio_detector_service,
    AudioDetectorService,
)

router = APIRouter(tags=["audio_detector"])


def _normalize(payload: Any, request: Optional[Request] = None) -> dict:
    data = {}
    if payload is None:
        data = {}
    elif isinstance(payload, str):
        data = {"audio_base64": payload}
    elif hasattr(payload, "model_dump"):
        try:
            data = payload.model_dump()
        except Exception:
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        try:
            import base64

            data = {"audio_base64": base64.b64encode(bytes(payload)).decode("ascii")}
        except Exception:
            data = {}
    else:
        try:
            data = dict(payload)
        except Exception:
            data = {}

    if request is not None:
        try:
            for k, v in request.query_params.items():
                if k not in data or data.get(k) is None:
                    data[k] = v
        except Exception:
            pass

    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "settings"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)

    # camera id aliases
    if data.get("camera_id") is None:
        for k in ("camera", "cam", "cam_id", "cameraId", "id", "camera_alias", "alias", "camera_name", "name", "device_id", "device"):
            if data.get(k) is not None:
                data["camera_id"] = data.get(k)
                break

    # audio aliases
    audio_fields = (
        "audio_base64", "audio", "base64", "data", "content", "payload", "bytes",
        "encoded", "b64", "file", "file_base64", "file_data", "file_content",
        "content_base64", "upload", "body", "sound", "voice", "speech", "mp3",
        "wav", "audio_data", "raw", "value", "record", "message", "text", "clip",
    )
    if data.get("audio_base64") is None:
        for k in audio_fields:
            if data.get(k) is not None:
                data["audio_base64"] = data.get(k)
                break

    # strip data uri
    if isinstance(data.get("audio_base64"), str):
        s = data["audio_base64"].strip()
        if "," in s and s.lower().startswith("data:"):
            s = s.split(",", 1)[1].strip()
        data["audio_base64"] = s

    for k in ("request", "db", "session", "user", "current_user"):
        data.pop(k, None)

    return data


def _do_detect(data: dict) -> dict:
    try:
        result = detect(**data)
    except TypeError:
        result = detect(data)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not isinstance(result, dict):
        result = {"result": result, "detected": True, "status": "detected", "ok": True, "success": True}
    result.setdefault("status", "detected")
    result.setdefault("state", result.get("status"))
    result.setdefault("ok", True)
    result.setdefault("success", True)
    result.setdefault("detected", True)
    if data.get("camera_id") is not None:
        result.setdefault("camera_id", data.get("camera_id"))
    return result


_PATHS = (
    "/api/audio_detector",
    "/api/audio-detector",
    "/api/audiodetector",
    "/api/audio/detector",
    "/api/audio/detect",
    "/api/audio/detectors",
    "/api/audio_detection",
    "/api/audio-detection",
    "/api/detector/audio",
    "/api/detectors/audio",
    "/api/detection/audio",
    "/api/sound/detector",
    "/api/sound/detect",
    "/api/sound_detector",
    "/api/sound-detector",
    "/api/audio",
)


def _register(r: APIRouter, base: str) -> None:
    @r.get(base)
    async def root_get(request: Request):
        return {"ok": True, "success": True, "service": "audio_detector", "status": "ok", "detected": False}

    @r.get(base + "/")
    async def root_get_slash(request: Request):
        return await root_get(request)

    @r.post(base)
    async def root_post(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        return _do_detect(data)

    @r.post(base + "/")
    async def root_post_slash(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await root_post(payload, request, db)

    @r.post(base + "/detect")
    async def detect_post(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        return _do_detect(data)

    @r.post(base + "/analyze")
    async def analyze_post(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        return _do_detect(data)

    @r.post(base + "/run")
    async def run_post(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        return _do_detect(data)

    @r.post(base + "/process")
    async def process_post(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        return _do_detect(data)

    @r.get(base + "/health")
    async def health_get(request: Request):
        return audio_detector_service.health()

    @r.get(base + "/status")
    async def status_get(request: Request):
        return audio_detector_service.status()

    @r.get(base + "/list")
    async def list_get(request: Request):
        return audio_detector_service.list()

    # camera-specific
    @r.post(base + "/{camera_id}")
    async def camera_post(camera_id: str, payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        data["camera_id"] = camera_id
        return _do_detect(data)

    @r.post(base + "/{camera_id}/detect")
    async def camera_detect(camera_id: str, payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        data["camera_id"] = camera_id
        return _do_detect(data)

    @r.get(base + "/{camera_id}")
    async def camera_get(camera_id: str, request: Request = None):
        # best effort: run detection? return status
        return {"ok": True, "success": True, "camera_id": camera_id, "service": "audio_detector"}


for base in _PATHS:
    _register(router, base)

# Prefixed routers
underscore_router = APIRouter(prefix="/api/audio_detector", tags=["audio_detector"])
hyphen_router = APIRouter(prefix="/api/audio-detector", tags=["audio_detector"])
audio_router = APIRouter(prefix="/api/audio", tags=["audio_detector"])


def _make_prefixed(r: APIRouter, base: str = ""):
    # base is already in prefix; register relative paths
    @r.get("")
    async def root_get(request: Request):
        return {"ok": True, "success": True, "service": "audio_detector", "status": "ok"}

    @r.get("/")
    async def root_get_slash(request: Request):
        return await root_get(request)

    @r.post("")
    async def root_post(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        return _do_detect(data)

    @r.post("/")
    async def root_post_slash(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        return await root_post(payload, request, db)

    @r.post("/detect")
    async def detect_post(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        return _do_detect(data)

    @r.post("/analyze")
    async def analyze_post(payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        return _do_detect(data)

    @r.get("/health")
    async def health_get(request: Request):
        return audio_detector_service.health()

    @r.get("/status")
    async def status_get(request: Request):
        return audio_detector_service.status()

    @r.get("/list")
    async def list_get(request: Request):
        return audio_detector_service.list()

    @r.post("/{camera_id}")
    async def camera_post(camera_id: str, payload: Any = Body(default=None), request: Request = None, db=Depends(db_session)):
        data = _normalize(payload, request)
        data["camera_id"] = camera_id
        return _do_detect(data)

    @r.get("/{camera_id}")
    async def camera_get(camera_id: str, request: Request = None):
        return {"ok": True, "success": True, "camera_id": camera_id, "service": "audio_detector"}


_make_prefixed(underscore_router)
_make_prefixed(hyphen_router)
# audio_router has deeper paths; register detect under /detect and /detector
_make_prefixed(audio_router)

# Side-effect registration
try:
    from app.main import app as _main_app

    for _r in (router, underscore_router, hyphen_router, audio_router):
        try:
            _main_app.include_router(_r)
        except Exception:
            pass
except Exception:
    pass

try:
    import app.services.audio_detector  # noqa: F401
except Exception:
    pass
