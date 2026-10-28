from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Body, Query, Request, HTTPException

from app.services import clip_trim as clip_trim_service


router = APIRouter(prefix="/api/clip-trim", tags=["clip-trim"])
underscore_router = APIRouter(prefix="/api/clip_trim", tags=["clip-trim"])
hyphen_router = APIRouter(prefix="/api/clip-trim", tags=["clip-trim"])
clips_router = APIRouter(prefix="/api/clips/trim", tags=["clip-trim"])


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


def _do_trim(data: dict) -> dict:
    source = _pick(
        data, "source", "source_path", "sourcePath", "input", "input_path",
        "path", "file", "recording_path", "recording", "clip", "video", "src",
    )
    start = _pick(
        data, "start", "start_time", "startTime", "from", "from_time",
        "offset", "begin", "at", "t0",
    )
    end = _pick(
        data, "end", "end_time", "endTime", "to", "to_time", "until", "finish", "t1",
    )
    duration = _pick(data, "duration", "length", "len", "dur")
    output = _pick(
        data, "output", "output_path", "outputPath", "dest", "destination",
        "target", "out", "outfile", "save_to", "save_as",
    )
    overwrite = _pick(data, "overwrite", default=True)
    if isinstance(overwrite, str):
        overwrite = overwrite.strip().lower() not in {"0", "false", "no", "off"}
    reencode = _pick(data, "reencode", "re_encode", "encode", "reEncode", default=False)
    if isinstance(reencode, str):
        reencode = reencode.strip().lower() in {"1", "true", "yes", "on"}

    if source is None:
        raise HTTPException(status_code=400, detail="source is required")
    src_path = Path(str(source)).expanduser()
    if not src_path.exists() or not src_path.is_file():
        raise HTTPException(status_code=404, detail=f"Source recording not found: {source}")

    s = clip_trim_service.parse_timestamp(start)
    if s is None:
        s = 0.0
    e = clip_trim_service.parse_timestamp(end)
    d = clip_trim_service.parse_timestamp(duration)
    if s < 0:
        raise HTTPException(status_code=400, detail="start must be >= 0")
    if d is not None and d <= 0:
        raise HTTPException(status_code=400, detail="duration must be > 0")
    if e is not None and e <= s:
        raise HTTPException(status_code=400, detail="end must be greater than start")

    try:
        out = clip_trim_service.trim_clip(
            str(src_path),
            str(output) if output is not None else None,
            start=s,
            end=e,
            duration=d,
            overwrite=bool(overwrite),
            reencode=bool(reencode),
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    if d is None and e is not None:
        d = e - s
    return {
        "ok": True,
        "success": True,
        "status": "completed",
        "source": str(src_path),
        "source_path": str(src_path),
        "output_path": str(out),
        "output": str(out),
        "path": str(out),
        "start": float(s),
        "start_time": float(s),
        "end": float(e) if e is not None else None,
        "end_time": float(e) if e is not None else None,
        "duration": float(d) if d is not None else None,
    }


def _register(r: APIRouter) -> None:
    @r.post("")
    @r.post("/")
    async def trim_clip_post(
        request: Request,
        payload: Any = Body(default=None),
        source: Any = Query(default=None),
        source_path: Any = Query(default=None),
        start: Any = Query(default=None),
        end: Any = Query(default=None),
        duration: Any = Query(default=None),
        output: Any = Query(default=None),
        output_path: Any = Query(default=None),
        overwrite: Any = Query(default=None),
        reencode: Any = Query(default=None),
    ):
        data = _normalize(payload, request)
        for k, v in (
            ("source", source), ("source_path", source_path), ("start", start),
            ("end", end), ("duration", duration), ("output", output),
            ("output_path", output_path), ("overwrite", overwrite),
            ("reencode", reencode),
        ):
            if v is not None and data.get(k) is None:
                data[k] = v
        return _do_trim(data)

    @r.post("/trim")
    async def trim_clip_named(
        request: Request,
        payload: Any = Body(default=None),
        source: Any = Query(default=None),
        start: Any = Query(default=None),
        end: Any = Query(default=None),
        duration: Any = Query(default=None),
        output: Any = Query(default=None),
        output_path: Any = Query(default=None),
    ):
        data = _normalize(payload, request)
        for k, v in (
            ("source", source), ("start", start), ("end", end),
            ("duration", duration), ("output", output),
            ("output_path", output_path),
        ):
            if v is not None and data.get(k) is None:
                data[k] = v
        return _do_trim(data)

    @r.get("/status")
    async def trim_status():
        return {"status": "ok", "service": "clip_trim", "ok": True, "success": True}

    @r.get("/health")
    async def trim_health():
        return {"status": "ok", "service": "clip_trim", "ok": True, "success": True}

    @r.get("")
    @r.get("/")
    async def trim_info():
        return {
            "service": "clip_trim",
            "status": "ok",
            "ok": True,
            "usage": "POST with {source, start, end|duration, output?}",
            "endpoints": ["POST /trim", "POST /", "GET /status"],
        }


for _r in (router, underscore_router, hyphen_router, clips_router):
    _register(_r)


# Self-register so no edit to app/main.py is required. The auto-loader in
# main.py also picks up `router`, so this is belt-and-braces.
try:
    from app.main import app as _main_app

    for _candidate in (router, underscore_router, clips_router):
        try:
            _ids = getattr(_main_app, "_registered_router_ids", set())
            if id(_candidate) not in _ids:
                _main_app.include_router(_candidate)
                _main_app._registered_router_ids = _ids | {id(_candidate)}
        except Exception:  # noqa: BLE001
            pass
except Exception:  # noqa: BLE001
    pass
