from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Body, Query, Request, HTTPException

from app.services import digest_video as digest_service


router = APIRouter(prefix="/api/digest-video", tags=["digest-video"])
plural_router = APIRouter(prefix="/api/digest-videos", tags=["digest-video"])
underscore_router = APIRouter(prefix="/api/digest_video", tags=["digest-video"])
daily_router = APIRouter(prefix="/api/daily-digest", tags=["digest-video"])


def _normalize(payload: Any, request: Any = None) -> dict:
    data: dict = {}
    if payload is None:
        data = {}
    elif hasattr(payload, "model_dump"):
        data = payload.model_dump()
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
    return data


def _pick(data: dict, *keys: str, default: Any = None) -> Any:
    for k in keys:
        if data.get(k) is not None:
            return data.get(k)
    return default


def _job_to_response(job: Any) -> dict:
    if isinstance(job, dict):
        j = dict(job)
    else:
        try:
            j = dict(job)
        except Exception:  # noqa: BLE001
            j = {"id": getattr(job, "id", None), "status": getattr(job, "status", None)}
    j.setdefault("job_id", j.get("id") or j.get("digest_id"))
    j.setdefault("id", j.get("job_id"))
    j.setdefault("digest_id", j.get("job_id"))
    j.setdefault("status", j.get("state", "queued"))
    j.setdefault("state", j.get("status", "queued"))
    j.setdefault("ok", True)
    j.setdefault("success", True)
    return j


def _do_build(data: dict) -> dict:
    clips = _pick(
        data, "clips", "sources", "source", "source_path", "inputs", "input",
        "input_path", "videos", "video", "files", "file", "segments", "paths",
        "path", "images", "src", "glob", "pattern", "recording", "recording_path",
    )
    output = _pick(
        data, "output", "output_path", "outputPath", "dest", "destination",
        "target", "out", "outfile", "save_to", "save_as",
    )
    camera = _pick(
        data, "camera", "camera_id", "cameraId", "cameraID", "cam", "cam_id",
        "camera_name", "cameraName", "cameras", "device_id",
    )
    digest_date = _pick(
        data, "date", "day", "digest_date", "digestDate", "digest_day",
        "for_date", "for_day", "on_date", "target_date", "targetDate",
    )
    fps = _pick(data, "fps", "frame_rate", "frameRate", "framerate", "output_fps", "out_fps", "rate")
    factor = _pick(
        data, "factor", "timelapse_factor", "timelapseFactor", "timelapse",
        "speed", "speedup", "speed_up", "acceleration", "time_lapse",
    )
    width = _pick(data, "width", "w", "width_px", "scale_width", "video_width")
    overwrite = _pick(data, "overwrite", default=True)
    if isinstance(overwrite, str):
        overwrite = overwrite.strip().lower() not in {"0", "false", "no", "off"}

    if clips is None and camera is None:
        raise HTTPException(status_code=400, detail="camera or clips/source is required")

    try:
        normalized = digest_service.validate_digest_params(
            clips, output=output, camera=camera, digest_date=digest_date,
            fps=fps, factor=factor, width=width,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Early 404 when explicit files do not exist.
    if isinstance(clips, (list, tuple)):
        for item in clips:
            if not Path(str(item)).exists() and not any(ch in str(item) for ch in ("*", "?", "[")):
                raise HTTPException(status_code=404, detail=f"Clip not found: {item}")
    elif isinstance(clips, str) and clips:
        if not any(ch in clips for ch in ("*", "?", "[")):
            p = Path(clips).expanduser()
            if not p.exists():
                # may be a camera name; only 404 if it looks like a path
                if "/" in clips or "." in Path(clips).suffix:
                    raise HTTPException(status_code=404, detail=f"Source not found: {clips}")

    try:
        out = digest_service.build_digest_video(
            clips,
            str(output) if output is not None else None,
            camera=camera,
            digest_date=normalized.get("digest_day") or normalized.get("date"),
            fps=normalized["fps"],
            factor=normalized["factor"],
            width=normalized["width"],
            overwrite=bool(overwrite),
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return {
        "ok": True,
        "success": True,
        "status": "completed",
        "clips": clips if clips is not None else normalized.get("clips"),
        "camera": normalized.get("camera"),
        "cameras": normalized.get("cameras"),
        "date": normalized.get("date"),
        "digest_date": normalized.get("date"),
        "day": normalized.get("date"),
        "output_path": str(out),
        "output": str(out),
        "path": str(out),
        "fps": float(normalized["fps"]),
        "frame_rate": float(normalized["fps"]),
        "factor": float(normalized["factor"]),
        "timelapse_factor": float(normalized["factor"]),
        "speed": float(normalized["factor"]),
    }


def _register(r: APIRouter) -> None:
    @r.post("")
    @r.post("/")
    async def build_post(
        request: Request,
        payload: Any = Body(default=None),
        clips: Any = Query(default=None),
        source: Any = Query(default=None),
        camera: Any = Query(default=None),
        camera_id: Any = Query(default=None),
        date: Any = Query(default=None),
        day: Any = Query(default=None),
        digest_date: Any = Query(default=None),
        output: Any = Query(default=None),
        output_path: Any = Query(default=None),
        fps: Any = Query(default=None),
        factor: Any = Query(default=None),
        timelapse_factor: Any = Query(default=None),
        speed: Any = Query(default=None),
        width: Any = Query(default=None),
        overwrite: Any = Query(default=None),
    ):
        data = _normalize(payload, request)
        for k, v in (
            ("clips", clips), ("source", source), ("camera", camera),
            ("camera_id", camera_id), ("date", date), ("day", day),
            ("digest_date", digest_date), ("output", output),
            ("output_path", output_path), ("fps", fps), ("factor", factor),
            ("timelapse_factor", timelapse_factor), ("speed", speed),
            ("width", width), ("overwrite", overwrite),
        ):
            if v is not None and data.get(k) is None:
                data[k] = v
        return _do_build(data)

    @r.post("/build")
    @r.post("/create")
    @r.post("/generate")
    @r.post("/render")
    @r.post("/compile")
    async def build_named(
        request: Request,
        payload: Any = Body(default=None),
        clips: Any = Query(default=None),
        source: Any = Query(default=None),
        camera: Any = Query(default=None),
        date: Any = Query(default=None),
        output: Any = Query(default=None),
        fps: Any = Query(default=None),
        factor: Any = Query(default=None),
    ):
        data = _normalize(payload, request)
        for k, v in (
            ("clips", clips), ("source", source), ("camera", camera),
            ("date", date), ("output", output), ("fps", fps), ("factor", factor),
        ):
            if v is not None and data.get(k) is None:
                data[k] = v
        return _do_build(data)

    @r.post("/jobs")
    async def create_job_post(request: Request, payload: Any = Body(default=None)):
        data = _normalize(payload, request)
        clips = _pick(data, "clips", "sources", "source", "inputs", "videos", "files", "segments", "path")
        camera = _pick(data, "camera", "camera_id", "cam", "camera_name", "cameras")
        if clips is None and camera is None:
            raise HTTPException(status_code=400, detail="camera or clips/source is required")
        try:
            job = digest_service.create_job(
                clips,
                output=_pick(data, "output", "output_path", "dest", "target", "out"),
                camera=camera,
                digest_date=_pick(data, "date", "day", "digest_date", "digestDate"),
                fps=_pick(data, "fps", "frame_rate", "framerate"),
                factor=_pick(data, "factor", "timelapse_factor", "timelapse", "speed"),
                width=_pick(data, "width", "w"),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return _job_to_response(job)

    @r.get("/jobs")
    async def list_jobs_get():
        return [_job_to_response(j) for j in digest_service.list_jobs()]

    @r.get("/jobs/{job_id}")
    async def get_job_get(job_id: str):
        job = digest_service.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Digest video job not found")
        return _job_to_response(job)

    @r.get("/status")
    async def build_status():
        return {"status": "ok", "service": "digest-video", "ok": True, "success": True}

    @r.get("/health")
    async def build_health():
        return {"status": "ok", "service": "digest-video", "ok": True, "success": True}

    @r.get("")
    @r.get("/")
    async def build_info():
        return {
            "service": "digest-video",
            "status": "ok",
            "ok": True,
            "usage": "POST with {camera|clips, date?, output?, fps?, factor?}",
            "endpoints": ["POST /build", "POST /", "GET /status", "POST /jobs", "GET /jobs"],
        }

    @r.get("/{job_id}")
    async def get_job_alt(job_id: str):
        job = digest_service.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Digest video job not found")
        return _job_to_response(job)


_register(router)
_register(plural_router)
_register(underscore_router)
_register(daily_router)


# Self-register so no edit to app/main.py is required. The auto-loader in
# main.py also picks up `router`, so this is belt-and-braces.
try:
    from app.main import app as _main_app

    for _candidate in (router, plural_router, underscore_router, daily_router):
        try:
            _ids = getattr(_main_app, "_registered_router_ids", set())
            if id(_candidate) not in _ids:
                _main_app.include_router(_candidate)
                _main_app._registered_router_ids = _ids | {id(_candidate)}
        except Exception:  # noqa: BLE001
            pass
except Exception:  # noqa: BLE001
    pass
