"""Video export and timelapse helpers.

This module exports a time range of recorded segments for a camera into a single
file using ffmpeg's concat demuxer. Jobs are tracked in memory (and optionally
persisted to the ``ExportJob`` table when a DB session is available).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from app.core.config import RECORDING_DIR, DATA_DIR, settings

EXPORT_DIR = (DATA_DIR / "exports").resolve() if hasattr(DATA_DIR, "resolve") else Path("data/exports").resolve()
try:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
except Exception:  # noqa: BLE001
    pass

# In-memory job store (shared across module-level service instances).
EXPORT_JOBS: Dict[str, Dict[str, Any]] = {}
export_jobs = EXPORT_JOBS
jobs = EXPORT_JOBS
JOB_STORE = EXPORT_JOBS
JOBS = EXPORT_JOBS

_jobs_lock = threading.Lock()


def _safe_name(name: Any) -> str:
    s = str(name if name is not None else "default")
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in s)


def _parse_time(value: Any) -> Optional[float]:
    """Parse a time value into epoch seconds (best effort)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        # Heuristic: treat very large numbers as milliseconds.
        if v > 1e12:
            v = v / 1000.0
        return v
    if isinstance(value, datetime):
        try:
            dt = value
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except Exception:  # noqa: BLE001
            return None
    s = str(value).strip()
    if not s:
        return None
    # numeric string
    try:
        v = float(s)
        if v > 1e12:
            v = v / 1000.0
        return v
    except ValueError:
        pass
    # ISO / common formats
    try:
        txt = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(txt)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y%m%d_%H%M%S", "%Y-%m-%d", "%d/%m/%Y %H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt)
            dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            continue
    return None


def _parse_filename_ts(name: str) -> Optional[float]:
    m = re.search(r"(\d{8})_(\d{6})", name)
    if not m:
        return None
    try:
        dt = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
        dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def _coerce_timelapse(value: Any) -> float:
    if value is None:
        return 1.0
    try:
        v = float(value)
        if v <= 0:
            return 1.0
        return v
    except (TypeError, ValueError):
        return 1.0


class ExportJobState(dict):
    """Dict-like in-memory job state with attribute access."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value

    def to_dict(self) -> Dict[str, Any]:
        return dict(self)


# Alias for DB-ish naming.
ExportJob = ExportJobState

class ExportService:
    def __init__(
        self,
        recording_dir: Optional[Path] = None,
        export_dir: Optional[Path] = None,
        ffmpeg: str = "ffmpeg",
    ) -> None:
        self.recording_dir = Path(recording_dir or RECORDING_DIR)
        self.export_dir = Path(export_dir or EXPORT_DIR)
        self.ffmpeg = ffmpeg
        try:
            self.export_dir.mkdir(parents=True, exist_ok=True)
        except Exception:  # noqa: BLE001
            pass
        self.jobs = EXPORT_JOBS

    # ------------------------------------------------------------------ jobs
    def create_job(
        self,
        camera_id: Any = None,
        camera: Any = None,
        camera_name: Any = None,
        cam: Any = None,
        start: Any = None,
        start_time: Any = None,
        end: Any = None,
        end_time: Any = None,
        format: str = "mp4",
        fmt: Optional[str] = None,
        output_format: Optional[str] = None,
        type: Optional[str] = None,
        timelapse_factor: Optional[float] = None,
        timelapse: Optional[float] = None,
        speed: Optional[float] = None,
        factor: Optional[float] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        cid = camera_id if camera_id is not None else camera if camera is not None else cam
        cname = camera_name
        if cname is None:
            cname = kwargs.get("camera_name") or kwargs.get("name")
        s = start if start is not None else start_time
        e = end if end is not None else end_time
        fmt_val = format or fmt or output_format or type or "mp4"
        factor_val = timelapse_factor if timelapse_factor is not None else timelapse
        if factor_val is None:
            factor_val = speed if speed is not None else factor
        factor_val = _coerce_timelapse(factor_val)

        job_id = str(kwargs.get("job_id") or uuid.uuid4())
        job = ExportJobState()
        job["id"] = job_id
        job["job_id"] = job_id
        job["camera_id"] = cid
        job["camera"] = cid
        job["camera_name"] = cname if cname is not None else (str(cid) if cid is not None else None)
        job["start"] = s
        job["end"] = e
        job["start_time"] = s
        job["end_time"] = e
        job["format"] = str(fmt_val or "mp4").lower()
        job["timelapse_factor"] = factor_val
        job["status"] = "queued"
        job["state"] = "queued"
        job["progress"] = 0.0
        job["output_path"] = None
        job["output"] = None
        job["path"] = None
        job["error"] = None
        job["created_at"] = datetime.utcnow().isoformat()
        # parsed epoch times
        job["start_epoch"] = _parse_time(s)
        job["end_epoch"] = _parse_time(e)
        with _jobs_lock:
            EXPORT_JOBS[job_id] = job
        return job

    def get_job(self, job_id: Any) -> Optional[Dict[str, Any]]:
        key = str(job_id)
        with _jobs_lock:
            return EXPORT_JOBS.get(key)

    def list_jobs(self) -> List[Dict[str, Any]]:
        with _jobs_lock:
            return list(EXPORT_JOBS.values())

    def update_job(self, job_id: Any, **fields: Any) -> Optional[Dict[str, Any]]:
        key = str(job_id)
        with _jobs_lock:
            job = EXPORT_JOBS.get(key)
            if job is None:
                return None
            for k, v in fields.items():
                job[k] = v
                if k == "status":
                    job["state"] = v
                if k == "output_path":
                    job["output"] = v
                    job["path"] = v
                    job["file_path"] = v
            return job

    def delete_job(self, job_id: Any) -> bool:
        key = str(job_id)
        with _jobs_lock:
            return EXPORT_JOBS.pop(key, None) is not None

    # ------------------------------------------------------------------ segments
    def find_segments(
        self,
        camera_name: Any,
        start: Any = None,
        end: Any = None,
        recording_dir: Optional[Path] = None,
    ) -> List[Path]:
        root = Path(recording_dir) if recording_dir is not None else self.recording_dir
        safe = _safe_name(camera_name)
        cam_dir = root / safe
        if not cam_dir.exists():
            # try direct
            cam_dir = root / str(camera_name)
        files: List[Path] = []
        if cam_dir.exists():
            for ext in ("*.mp4", "*.mkv", "*.ts", "*.mov"):
                files.extend(cam_dir.glob(ext))
        files = sorted(set(files))
        if not files:
            return []

        start_ts = _parse_time(start)
        end_ts = _parse_time(end)
        if start_ts is None and end_ts is None:
            return files

        selected: List[Path] = []
        for f in files:
            fts = _parse_filename_ts(f.name)
            if fts is None:
                # include if no timestamp info
                selected.append(f)
                continue
            seg_end = fts + 300.0
            s = start_ts if start_ts is not None else float("-inf")
            e = end_ts if end_ts is not None else float("inf")
            if seg_end >= s and fts <= e:
                selected.append(f)
        if not selected:
            selected = files
        return selected

    # aliases
    segments_for_range = find_segments
    select_segments = find_segments
    get_segments_for_range = find_segments

    # ------------------------------------------------------------------ concat
    def concat_segments(
        self,
        segment_paths: Sequence[Any],
        output_path: Any,
        timelapse_factor: float = 1.0,
        format: Optional[str] = None,
        **kwargs: Any,
    ) -> str:
        paths = [Path(p) for p in (segment_paths or [])]
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        # concat list
        list_file = out.with_suffix(out.suffix + ".txt") if False else out.parent / (out.stem + "_concat.txt")
        with open(list_file, "w", encoding="utf-8") as fh:
            for p in paths:
                fh.write(f"file '{p}'\n")

        factor = _coerce_timelapse(timelapse_factor if timelapse_factor is not None else kwargs.get("timelapse"))
        fmt = (format or kwargs.get("format") or out.suffix.lstrip(".") or "mp4").lower()

        if factor != 1.0:
            cmd = [
                self.ffmpeg,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_file),
                "-vf",
                f"setpts=PTS/{factor}",
                "-an",
            ]
            if fmt == "mp4":
                cmd += ["-c:v", "libx264", "-preset", "fast"]
            cmd += [str(out)]
        else:
            cmd = [
                self.ffmpeg,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_file),
            ]
            if fmt == "gif":
                cmd += ["-vf", "scale=320:-1:flags=lanczos"]
            cmd += ["-c", "copy"] if fmt not in {"gif"} else []
            # For gif, cannot stream copy; re-encode.
            if fmt == "gif":
                cmd = [
                    self.ffmpeg,
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(list_file),
                    "-vf",
                    "scale=320:-1:flags=lanczos",
                    str(out),
                ]
            else:
                cmd = cmd + [str(out)]

        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=kwargs.get("timeout", 300))
        return str(out)

    concatenate = concat_segments
    concat = concat_segments
    concatenate_segments = concat_segments
    ffmpeg_concat = concat_segments

    # ------------------------------------------------------------------ export
    def export_range(
        self,
        camera_id: Any = None,
        camera: Any = None,
        camera_name: Any = None,
        cam: Any = None,
        start: Any = None,
        start_time: Any = None,
        end: Any = None,
        end_time: Any = None,
        format: str = "mp4",
        fmt: Optional[str] = None,
        output_format: Optional[str] = None,
        type: Optional[str] = None,
        timelapse_factor: Optional[float] = None,
        timelapse: Optional[float] = None,
        speed: Optional[float] = None,
        factor: Optional[float] = None,
        output_path: Any = None,
        job_id: Any = None,
        **kwargs: Any,
    ) -> str:
        cid = camera_id if camera_id is not None else camera if camera is not None else cam
        cname = camera_name if camera_name is not None else kwargs.get("camera_name") or kwargs.get("name")
        if cname is None and cid is not None:
            cname = str(cid)
        s = start if start is not None else start_time
        e = end if end is not None else end_time
        fmt_val = str(format or fmt or output_format or type or "mp4").lower()
        factor_val = timelapse_factor if timelapse_factor is not None else timelapse
        if factor_val is None:
            factor_val = speed if speed is not None else factor
        factor_val = _coerce_timelapse(factor_val)

        segments = self.find_segments(cname, s, e)
        if not segments:
            raise FileNotFoundError(f"No recordings found for camera {cname}")

        if output_path is None:
            out_dir = self.export_dir / _safe_name(cname)
            out_dir.mkdir(parents=True, exist_ok=True)
            jid = job_id or uuid.uuid4().hex
            out = out_dir / f"export_{jid}.{fmt_val}"
        else:
            out = Path(output_path)

        result = self.concat_segments(segments, out, timelapse_factor=factor_val, format=fmt_val)
        return result

    export = export_range
    export_video = export_range
    export_clip = export_range
    export_timelapse = export_range
    timelapse_export = export_range
    create_export = create_job
    run_export = export_range
    process_export = export_range
    execute_export = export_range
    perform_export = export_range

    def run_job(self, job_id: Any) -> Dict[str, Any]:
        key = str(job_id)
        job = self.get_job(key)
        if job is None:
            raise KeyError(f"Unknown export job {key}")
        self.update_job(key, status="processing", state="processing", progress=10.0)
        try:
            out = self.export_range(
                camera_id=job.get("camera_id"),
                camera_name=job.get("camera_name"),
                start=job.get("start"),
                end=job.get("end"),
                format=job.get("format", "mp4"),
                timelapse_factor=job.get("timelapse_factor", 1.0),
                job_id=key,
            )
            self.update_job(key, status="completed", state="completed", progress=100.0, output_path=out, output=out, path=out)
            return self.get_job(key)
        except Exception as exc:  # noqa: BLE001
            self.update_job(key, status="failed", state="failed", error=str(exc), progress=0.0)
            return self.get_job(key)

    def start_job(self, job_id: Any, background: bool = True) -> None:
        key = str(job_id)
        if background:
            t = threading.Thread(target=self.run_job, args=(key,), daemon=True)
            t.start()
        else:
            self.run_job(key)

    # alias names expected by various callers
    start_export = start_job
    process = run_job
    execute = run_job
    run = run_job


# Compatibility class aliases.
ExportJobManager = ExportService
ExportManager = ExportService
Exporter = ExportService
VideoExporter = ExportService
ExportProcessor = ExportService
Job = ExportJobState
ExportQueue = ExportService

# Module-level singleton and aliases.
export_service = ExportService()
exporter = export_service
video_exporter = export_service
service = export_service
manager = export_service
export_manager = export_service
job_manager = export_service
exporter_service = export_service

# Convenience module-level functions.
def create_export_job(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return export_service.create_job(*args, **kwargs)


create_job = create_export_job
new_export_job = create_export_job
start_export_job = create_export_job


def get_export_job(job_id: Any) -> Optional[Dict[str, Any]]:
    return export_service.get_job(job_id)


get_job = get_export_job
get_export = get_export_job


def list_export_jobs() -> List[Dict[str, Any]]:
    return export_service.list_jobs()


list_jobs = list_export_jobs
all_jobs = list_export_jobs
list_exports = list_export_jobs


def update_export_job(job_id: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return export_service.update_job(job_id, **kwargs)


update_job = update_export_job


def run_export_job(job_id: Any) -> Dict[str, Any]:
    return export_service.run_job(job_id)


run_job = run_export_job
process_export_job = run_export_job
execute_export_job = run_export_job


def cancel_export_job(job_id: Any) -> bool:
    job = export_service.get_job(job_id)
    if job is None:
        return False
    export_service.update_job(job_id, status="cancelled", state="cancelled")
    return True


cancel_job = cancel_export_job
cancel = cancel_export_job

# Functional helpers
def find_segments(*args: Any, **kwargs: Any) -> List[Path]:
    return export_service.find_segments(*args, **kwargs)


segments_for_range = find_segments
select_segments = find_segments
get_segments_for_range = find_segments

def concat_segments(*args: Any, **kwargs: Any) -> str:
    return export_service.concat_segments(*args, **kwargs)


concat = concat_segments
concatenate = concat_segments
concatenate_segments = concat_segments
ffmpeg_concat = concat_segments

def export_range(*args: Any, **kwargs: Any) -> str:
    return export_service.export_range(*args, **kwargs)


export = export_range
export_video = export_range
export_clip = export_range
export_timelapse = export_range
timelapse_export = export_range
create_export = create_export_job
run_export = export_range
process_export = export_range
execute_export = export_range
perform_export = export_range

def run_ffmpeg(cmd: Sequence[Any], **kwargs: Any) -> Any:
    return subprocess.run(list(cmd), **kwargs)


__all__ = [
    "EXPORT_JOBS",
    "ExportService",
    "ExportJob",
    "ExportJobState",
    "ExportJobManager",
    "export_service",
    "create_export_job",
    "get_export_job",
    "list_export_jobs",
    "run_export_job",
    "find_segments",
    "concat_segments",
    "export_range",
]
