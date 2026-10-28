from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Body, Query, Request, HTTPException

from app.services import timelapse as timelapse_service


router = APIRouter(prefix="/api/timelapse", tags=["timelapse"])
plural_router = APIRouter(prefix="/api/timelapses", tags=["timelapse"])
builder_router = APIRouter(prefix="/api/timelapse-builder", tags=["timelapse"])
# Alias kept for compatibility with code expecting `underscore_router`.
underscore_router = router


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
    j.setdefault("job_id", j.get("id"))
    j.setdefault("id", j.get("job_id"))
    j.setdefault("status", j.get("state", "queued"))
    j.setdefault("state", j.get("status", "queued"))
    j.setdefault("ok", True)
    j.setdefault("success", True)
    return j


def _do_build(data: dict) -> dict:
    source = _pick(
        data, "source", "source_path", "sourcePath", "input", "input_path",
        "inputPath", "path", "file", "video", "src", "filename",
        "image_dir", "imageDir", "images", "dir", "directory",
        "glob", "pattern", "recording", "recording_path",
    )
    output = _pick(
        data, "output", "output_path", "outputPath", "dest", "destination",
        "target", "out", "outfile", "save_to", "save_as",
    )
    fps = _pick(data, "fps", "frame_rate", "frameRate", "framerate", "output_fps", "out_fps", "rate")
    factor = _pick(
        data, "factor", "timelapse_factor", "timelapseFactor", "timelapse",
        "speed", "speedup", "speed_up", "acceleration", "time_lapse",
    )
    interval = _pick(
        data, "interval", "interval_seconds", "intervalSeconds", "every",
        "frame_interval", "sample_interval", "step", "capture_interval",
    )
    width = _pick(data, "width", "w", "width_px", "scale_width", "video_width")
    overwrite = _pick(data, "overwrite", default=True)
    if isinstance(overwrite, str):
        overwrite = overwrite.strip().lower() not in {"0", "false", "no", "off"}

    if source is None:
        raise HTTPException(status_code=400, detail="source is required")

    # Validate existence early for clean 404s.
    if isinstance(source, (list, tuple)):
        for item in source:
            if not Path(str(item)).exists() and not any(ch in str(item) for ch in ("*", "?", "[")):
                raise HTTPException(status_code=404, detail=f"Source not found: {item}")
            if any(ch in str(item) for ch in ("*", "?", "[")):
                import glob as _glob

                if not _glob.glob(str(item)):
                    raise HTTPException(status_code=404, detail=f"No images matched pattern: {item}")
    else:
        s = str(source)
        has_glob = any(ch in s for ch in ("*", "?", "["))
        if has_glob:
            import glob as _glob

            if not _glob.glob(s):
                raise HTTPException(status_code=404, detail=f"No images matched pattern: {s}")
        else:
            p = Path(s).expanduser()
            if not p.exists():
                raise HTTPException(status_code=404, detail=f"Source not found: {s}")

    try:
        normalized = timelapse_service.validate_timelapse_params(
            source, output=output, fps=fps, factor=factor, interval=interval, width=width,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    try:
        out = timelapse_service.build_timelapse(
            source,
            str(output) if output is not None else None,
            fps=normalized["fps"],
            factor=normalized["factor"],
            interval=normalized["interval"],
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
        "source": str(source) if not isinstance(source, list) else [str(p) for p in source],
        "source_path": str(source) if not isinstance(source, list) else [str(p) for p in source],
        "output_path": str(out),
        "output": str(out),
        "path": str(out),
        "fps": float(normalized["fps"]),
        "frame_rate": float(normalized["fps"]),
        "factor": float(normalized["factor"]),
        "timelapse_factor": float(normalized["factor"]),
        "speed": float(normalized["factor"]),
        "interval": normalized["interval"],
    }


def _register(r: APIRouter) -> None:
    @r.post("")
    @r.post("/")
    async def build_post(
        request: Request,
        payload: Any = Body(default=None),
        source: Any = Query(default=None),
        source_path: Any = Query(default=None),
        output: Any = Query(default=None),
        output_path: Any = Query(default=None),
        fps: Any = Query(default=None),
        factor: Any = Query(default=None),
        timelapse_factor: Any = Query(default=None),
        speed: Any = Query(default=None),
        interval: Any = Query(default=None),
        width: Any = Query(default=None),
        overwrite: Any = Query(default=None),
    ):
        data = _normalize(payload, request)
        for k, v in (
            ("source", source), ("source_path", source_path), ("output", output),
            ("output_path", output_path), ("fps", fps), ("factor", factor),
            ("timelapse_factor", timelapse_factor), ("speed", speed),
            ("interval", interval), ("width", width), ("overwrite", overwrite),
        ):
            if v is not None and data.get(k) is None:
                data[k] = v
        return _do_build(data)

    @r.post("/build")
    @r.post("/create")
    @r.post("/generate")
    @r.post("/render")
    async def build_named(
        request: Request,
        payload: Any = Body(default=None),
        source: Any = Query(default=None),
        output: Any = Query(default=None),
        fps: Any = Query(default=None),
        factor: Any = Query(default=None),
    ):
        data = _normalize(payload, request)
        for k, v in (
            ("source", source), ("output", output), ("fps", fps), ("factor", factor),
        ):
            if v is not None and data.get(k) is None:
                data[k] = v
        return _do_build(data)

    @r.post("/jobs")
    async def create_job_post(request: Request, payload: Any = Body(default=None)):
        data = _normalize(payload, request)
        source = _pick(data, "source", "source_path", "input", "path", "file", "image_dir", "images")
        if source is None:
            raise HTTPException(status_code=400, detail="source is required")
        try:
            job = timelapse_service.create_job(
                source,
                output=_pick(data, "output", "output_path", "dest", "target", "out"),
                fps=_pick(data, "fps", "frame_rate", "framerate"),
                factor=_pick(data, "factor", "timelapse_factor", "timelapse", "speed"),
                interval=_pick(data, "interval", "interval_seconds", "every"),
                width=_pick(data, "width", "w"),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return _job_to_response(job)

    @r.get("/jobs")
    async def list_jobs_get():
        return [_job_to_response(j) for j in timelapse_service.list_jobs()]

    @r.get("/jobs/{job_id}")
    async def get_job_get(job_id: str):
        job = timelapse_service.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Timelapse job not found")
        return _job_to_response(job)

    @r.get("/status")
    async def build_status():
        return {"status": "ok", "service": "timelapse", "ok": True, "success": True}

    @r.get("/health")
    async def build_health():
        return {"status": "ok", "service": "timelapse", "ok": True, "success": True}

    @r.get("")
    @r.get("/")
    async def build_info():
        return {
            "service": "timelapse",
            "status": "ok",
            "ok": True,
            "usage": "POST with {source, output?, fps?, factor?, interval?}",
            "endpoints": ["POST /build", "POST /", "GET /status", "POST /jobs", "GET /jobs"],
        }

    @r.get("/{job_id}")
    async def get_job_alt(job_id: str):
        job = timelapse_service.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Timelapse job not found")
        return _job_to_response(job)


_register(router)
_register(plural_router)
_register(builder_router)


# Self-register so no edit to app/main.py is required. The auto-loader in
# main.py also picks up `router`, so this is belt-and-braces.
try:
    from app.main import app as _main_app

    for _candidate in (router, plural_router, builder_router):
        try:
            _ids = getattr(_main_app, "_registered_router_ids", set())
            if id(_candidate) not in _ids:
                _main_app.include_router(_candidate)
                _main_app._registered_router_ids = _ids | {id(_candidate)}
        except Exception:  # noqa: BLE001
            pass
except Exception:  # noqa: BLE001
    pass
