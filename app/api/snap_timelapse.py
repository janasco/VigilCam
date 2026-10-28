from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Query, Request, HTTPException

from app.services import snap_timelapse as snap_service


router = APIRouter(prefix="/api/snap-timelapse", tags=["snap-timelapse"])
plural_router = APIRouter(prefix="/api/snap-timelapses", tags=["snap-timelapse"])
underscore_router = APIRouter(prefix="/api/snap_timelapse", tags=["snap-timelapse"])
snapshot_router = APIRouter(prefix="/api/snapshot-timelapse", tags=["snap-timelapse"])


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
    snapshots = _pick(
        data, "snapshots", "snapshot", "snaps", "source", "source_path", "sourcePath",
        "input", "input_path", "inputPath", "images", "image_dir", "imageDir",
        "dir", "directory", "folder", "glob", "pattern", "path", "file", "files",
        "src", "filename",
    )
    output = _pick(
        data, "output", "output_path", "outputPath", "dest", "destination",
        "target", "out", "outfile", "save_to", "save_as",
    )
    snapshot_dir = _pick(
        data, "snapshot_dir", "snapshots_dir", "snapshotsDir", "snapshotDir",
    )
    camera = _pick(
        data, "camera", "camera_id", "cameraId", "cameraID", "cam", "cam_id",
        "camera_name", "cameraName", "cameras", "device_id",
    )
    snap_date = _pick(
        data, "snap_date", "snapDate", "snapshot_date", "snapshotDate",
        "date", "day", "for_date", "on_date", "target_date", "targetDate",
    )
    event_id = _pick(data, "event_id", "event", "event_pk", "eid")
    fps = _pick(data, "fps", "frame_rate", "frameRate", "framerate", "output_fps", "out_fps", "rate")
    factor = _pick(
        data, "factor", "timelapse_factor", "timelapseFactor", "timelapse",
        "speed", "speedup", "speed_up", "acceleration", "time_lapse",
    )
    width = _pick(data, "width", "w", "width_px", "scale_width", "video_width")
    overwrite = _pick(data, "overwrite", default=True)
    if isinstance(overwrite, str):
        overwrite = overwrite.strip().lower() not in {"0", "false", "no", "off"}

    if snapshots is None and camera is None and snapshot_dir is None and event_id is None and snap_date is None:
        raise HTTPException(status_code=400, detail="snapshots/source or camera/snapshot_dir/date is required")

    # Early 404 when explicit files/dirs do not exist.
    if snapshots is not None and not isinstance(snapshots, (list, tuple)):
        s = str(snapshots)
        has_glob = any(ch in s for ch in ("*", "?", "["))
        if has_glob:
            import glob as _glob

            if not _glob.glob(s):
                raise HTTPException(status_code=404, detail=f"No snapshots matched pattern: {s}")
        else:
            p = Path(s).expanduser()
            if p.is_file():
                pass
            elif p.is_dir():
                pass
            elif not p.exists():
                # May be a snapshot_dir override passed as source; check store scan later.
                # Only 404 when it looks like a filesystem path.
                if "/" in s or "." in Path(s).suffix or s.startswith("~"):
                    raise HTTPException(status_code=404, detail=f"Snapshots not found: {s}")
    if isinstance(snapshots, (list, tuple)):
        for item in snapshots:
            if not Path(str(item)).exists() and not any(ch in str(item) for ch in ("*", "?", "[")):
                raise HTTPException(status_code=404, detail=f"Snapshot not found: {item}")

    try:
        normalized = snap_service.validate_snap_timelapse_params(
            snapshots, output=output, fps=fps, factor=factor, width=width,
            camera=camera, snap_date=snap_date, event_id=event_id,
            snapshot_dir=snapshot_dir,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    try:
        out = snap_service.build_snap_timelapse(
            snapshots,
            str(output) if output is not None else None,
            fps=normalized["fps"],
            factor=normalized["factor"],
            width=normalized["width"],
            camera=normalized.get("cameras") or normalized.get("camera"),
            snap_date=normalized.get("snap_day") or normalized.get("date"),
            event_id=normalized.get("event_id"),
            snapshot_dir=snapshot_dir,
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
        "snapshots": normalized["snapshots"] if not isinstance(normalized["snapshots"], list) else list(normalized["snapshots"]),
        "source": normalized["snapshots"] if not isinstance(normalized["snapshots"], list) else list(normalized["snapshots"]),
        "camera": normalized.get("camera"),
        "cameras": normalized.get("cameras"),
        "date": normalized.get("date"),
        "snap_date": normalized.get("date"),
        "event_id": normalized.get("event_id"),
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
        snapshots: Any = Query(default=None),
        source: Any = Query(default=None),
        snapshot_dir: Any = Query(default=None),
        camera: Any = Query(default=None),
        camera_id: Any = Query(default=None),
        date: Any = Query(default=None),
        snap_date: Any = Query(default=None),
        event_id: Any = Query(default=None),
        output: Any = Query(default=None),
        output_path: Any = Query(default=None),
        fps: Any = Query(default=None),
        factor: Any = Query(default=None),
        width: Any = Query(default=None),
        overwrite: Any = Query(default=None),
    ):
        data = _normalize(payload, request)
        for k, v in (
            ("snapshots", snapshots), ("source", source), ("snapshot_dir", snapshot_dir),
            ("camera", camera), ("camera_id", camera_id), ("date", date),
            ("snap_date", snap_date), ("event_id", event_id), ("output", output),
            ("output_path", output_path), ("fps", fps), ("factor", factor),
            ("width", width), ("overwrite", overwrite),
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
        snapshots: Any = Query(default=None),
        source: Any = Query(default=None),
        camera: Any = Query(default=None),
        date: Any = Query(default=None),
        output: Any = Query(default=None),
        fps: Any = Query(default=None),
        factor: Any = Query(default=None),
    ):
        data = _normalize(payload, request)
        for k, v in (
            ("snapshots", snapshots), ("source", source), ("camera", camera),
            ("date", date), ("output", output), ("fps", fps), ("factor", factor),
        ):
            if v is not None and data.get(k) is None:
                data[k] = v
        return _do_build(data)

    @r.post("/jobs")
    async def create_job_post(request: Request, payload: Any = Body(default=None)):
        data = _normalize(payload, request)
        snapshots = _pick(data, "snapshots", "snapshot", "snaps", "source", "source_path", "input", "path", "images")
        camera = _pick(data, "camera", "camera_id", "cam", "camera_name", "cameras")
        if snapshots is None and camera is None and _pick(data, "snapshot_dir", "snapshots_dir") is None and _pick(data, "snap_date", "date", "day") is None and _pick(data, "event_id", "event") is None:
            raise HTTPException(status_code=400, detail="snapshots/source or camera/snapshot_dir/date is required")
        try:
            job = snap_service.create_job(
                snapshots,
                output=_pick(data, "output", "output_path", "dest", "target", "out"),
                fps=_pick(data, "fps", "frame_rate", "framerate"),
                factor=_pick(data, "factor", "timelapse_factor", "timelapse", "speed"),
                width=_pick(data, "width", "w"),
                camera=camera,
                snap_date=_pick(data, "snap_date", "snapshot_date", "date", "day"),
                event_id=_pick(data, "event_id", "event"),
                snapshot_dir=_pick(data, "snapshot_dir", "snapshots_dir"),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return _job_to_response(job)

    @r.get("/jobs")
    async def list_jobs_get():
        return [_job_to_response(j) for j in snap_service.list_jobs()]

    @r.get("/jobs/{job_id}")
    async def get_job_get(job_id: str):
        job = snap_service.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Snapshot timelapse job not found")
        return _job_to_response(job)

    @r.get("/snapshots")
    async def list_snapshots_get(
        snapshot_dir: Any = Query(default=None),
        camera: Any = Query(default=None),
        date: Any = Query(default=None),
        snap_date: Any = Query(default=None),
        event_id: Any = Query(default=None),
        pattern: Any = Query(default=None),
        limit: Any = Query(default=None),
    ):
        items = snap_service.find_snapshots(
            snapshot_dir=snapshot_dir, camera=camera,
            snap_date=snap_date if snap_date is not None else date,
            event_id=event_id, pattern=pattern, limit=limit,
        )
        return {
            "ok": True,
            "success": True,
            "count": len(items),
            "snapshots": [str(p) for p in items],
            "items": [str(p) for p in items],
        }

    @r.get("/status")
    async def build_status():
        return {"status": "ok", "service": "snap-timelapse", "ok": True, "success": True}

    @r.get("/health")
    async def build_health():
        return {"status": "ok", "service": "snap-timelapse", "ok": True, "success": True}

    @r.get("")
    @r.get("/")
    async def build_info():
        return {
            "service": "snap-timelapse",
            "status": "ok",
            "ok": True,
            "usage": "POST with {snapshots|snapshot_dir|camera, date?, event_id?, output?, fps?, width?}",
            "endpoints": ["POST /build", "POST /", "GET /snapshots", "GET /status", "POST /jobs", "GET /jobs"],
        }

    @r.get("/{job_id}")
    async def get_job_alt(job_id: str):
        job = snap_service.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Snapshot timelapse job not found")
        return _job_to_response(job)


_register(router)
_register(plural_router)
_register(underscore_router)
_register(snapshot_router)


# Self-register so no edit to app/main.py is required. The auto-loader in
# main.py also picks up `router`, so this is belt-and-braces.
try:
    from app.main import app as _main_app

    for _candidate in (router, plural_router, underscore_router, snapshot_router):
        try:
            _ids = getattr(_main_app, "_registered_router_ids", set())
            if id(_candidate) not in _ids:
                _main_app.include_router(_candidate)
                _main_app._registered_router_ids = _ids | {id(_candidate)}
        except Exception:  # noqa: BLE001
            pass
except Exception:  # noqa: BLE001
    pass
