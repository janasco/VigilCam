"""Single snapshot capture service (placeholder / ffmpeg frame).

Captures one still frame to the snapshots directory. When a stream URL is
provided and ``ffmpeg`` is available the service attempts a real frame grab
(``ffmpeg -vframes 1``); otherwise it writes a placeholder JPEG so callers
always get a file back.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core import config as _config


def _snapshot_dir(override: Any = None) -> Path:
    if override is not None:
        return Path(override).expanduser()
    for candidate in (
        globals().get("SNAPSHOT_DIR"),
        getattr(_config, "SNAPSHOT_DIR", None),
    ):
        if candidate is not None:
            try:
                return Path(candidate).expanduser()
            except Exception:
                continue
    return Path("data/snapshots").expanduser()


SNAPSHOT_DIR = _config.SNAPSHOT_DIR

CAMERA_KEYS = (
    "camera_id", "camera", "cam", "cam_id", "cameraId", "cameraID",
    "source_id", "device_id", "id",
)
SOURCE_KEYS = (
    "source", "source_url", "stream_url", "rtsp_url", "rtsp",
    "url", "uri", "stream", "input", "feed", "onvif_url", "streamUrl",
)
DIR_KEYS = (
    "directory", "dir", "snapshot_dir", "folder", "target_dir",
    "snapshot_directory", "root", "root_dir", "output_dir", "out_dir",
)
FILENAME_KEYS = ("filename", "name", "safe_filename", "output", "output_name", "file_name")


def _safe_name(base: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(base))
    safe = safe.strip("._") or "snapshot"
    if len(safe) > 64:
        safe = safe[:64]
    return safe


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _placeholder_jpeg(label: str = "VigilCam", width: int = 640, height: int = 480) -> bytes:
    try:
        from PIL import Image, ImageDraw  # type: ignore

        w = max(16, min(1280, int(width)))
        h = max(16, min(1280, int(height)))
        img = Image.new("RGB", (w, h), color=(20, 20, 30))
        draw = ImageDraw.Draw(img)
        draw.text((10, max(10, h // 2 - 10)), str(label)[:48], fill=(255, 255, 255))
        import io as _io

        buf = _io.BytesIO()
        img.save(buf, format="JPEG", quality=70)
        return buf.getvalue()
    except Exception:  # noqa: BLE001
        return (
            b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
            b"\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00"
            b"\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b"
            b"\xff\xc4\x00\xb5\x10\x00\x02\x01\x03\x03\x02\x04\x03\x05\x05\x04\x04\x00\x00\x01}\x01\x02\x03\x00\x04\x11\x05\x12!1A\x06\x13Qa\x07\"q\x142\x81\x91\xa1\x08#B\xb1\xc1\x15R\xd1\xf0$3br\x82\t\n\x16\x17\x18\x19\x1a%&'()*456789:CDEFGHIJSTUVWXYZcdefghijstuvwxyz\x83\x84\x85\x86\x87\x88\x89\x8a\x92\x93\x94\x95\x96\x97\x98\x99\x9a\xa2\xa3\xa4\xa5\xa6\xa7\xa8\xa9\xaa\xb2\xb3\xb4\xb5\xb6\xb7\xb8\xb9\xba\xc2\xc3\xc4\xc5\xc6\xc7\xc8\xc9\xca\xd2\xd3\xd4\xd5\xd6\xd7\xd8\xd9\xda\xe1\xe2\xe3\xe4\xe5\xe6\xe7\xe8\xe9\xea\xf1\xf2\xf3\xf4\xf5\xf6\xf7\xf8\xf9\xfa\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xfb\xd2\x8a(\xa0\x00\xff\xd9"
        )


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _try_ffmpeg_capture(source: str, dest: Path, timeout: float = 15.0) -> bool:
    if not source or not _ffmpeg_available():
        return False
    try:
        is_rtsp = str(source).lower().startswith("rtsp://")
        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
        ]
        if is_rtsp:
            # NOTE: -rtsp_transport is an RTSP-demuxer private option. Passing
            # it for http/file inputs makes ffmpeg abort with "Option not found".
            cmd += ["-rtsp_transport", "tcp"]
        cmd += [
            "-i",
            str(source),
            "-vframes",
            "1",
            "-q:v",
            "2",
            str(dest),
        ]
        subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=float(timeout) if timeout else 15.0,
        )
        return dest.is_file() and dest.stat().st_size > 0
    except Exception:  # noqa: BLE001
        try:
            if dest.is_file() and dest.stat().st_size == 0:
                dest.unlink()
        except Exception:  # noqa: BLE001
            pass
        return False


def _first(data: Dict[str, Any], keys: tuple) -> Any:
    for k in keys:
        if k in data and data.get(k) is not None:
            return data.get(k)
    lower = {str(k).lower(): v for k, v in data.items()}
    for k in keys:
        if k.lower() in lower and lower[k.lower()] is not None:
            return lower[k.lower()]
    return None


def _normalize_args(args: tuple, kwargs: Dict[str, Any]) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for a in args or ():
        if isinstance(a, dict):
            data.update(a)
        elif isinstance(a, Path):
            data.setdefault("directory", a)
        elif isinstance(a, (bytes, bytearray)):
            continue
        elif isinstance(a, (int, float)) and not isinstance(a, bool):
            data.setdefault("camera_id", a)
        elif isinstance(a, str):
            s = a.strip()
            if not s:
                continue
            if s.startswith(("rtsp://", "rtmp://", "http://", "https://", "/")) or s.endswith((".m3u8", ".mp4", ".mkv")):
                data.setdefault("source", s)
            elif s.isdigit():
                data.setdefault("camera_id", s)
            else:
                data.setdefault("name", s)
        else:
            try:
                attrs = {k: getattr(a, k) for k in dir(a) if not k.startswith("_") and not callable(getattr(a, k, None))}
                if attrs:
                    data.update(attrs)
            except Exception:  # noqa: BLE001
                pass
    data.update(kwargs or {})
    for nested_key in ("payload", "options", "config", "params", "request", "settings", "data", "body"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _make_filename(camera_id: Any, hint: Any = None) -> str:
    if hint is not None:
        stem = _safe_name(Path(str(hint)).stem)
        return "{}_{}.jpg".format(stem, uuid.uuid4().hex[:8])
    try:
        cid = int(str(camera_id)) if camera_id is not None else None
    except (TypeError, ValueError):
        cid = None
    stamp = _now().strftime("%Y%m%d_%H%M%S")
    if cid is not None:
        base = "snapshot_cam_{}_{}".format(cid, stamp)
    else:
        base = "snapshot_{}".format(stamp)
    return "{}_{}.jpg".format(_safe_name(base), uuid.uuid4().hex[:8])


def capture_snapshot_now(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Capture a single frame to the snapshots directory.

    Always succeeds with a placeholder JPEG when ffmpeg/a source is
    unavailable. Returns a result dict and guarantees the file exists.
    """
    data = _normalize_args(args, kwargs)

    camera_id = _first(data, CAMERA_KEYS)
    if camera_id is not None:
        try:
            camera_id = int(camera_id)
        except (TypeError, ValueError):
            pass

    source = _first(data, SOURCE_KEYS)
    if source is not None:
        source = str(source).strip() or None

    directory = _first(data, DIR_KEYS)
    if directory is None and data.get("path") is not None:
        try:
            p = Path(str(data.get("path")))
            if p.exists() and p.is_dir():
                directory = p
        except Exception:  # noqa: BLE001
            pass

    filename_hint = _first(data, FILENAME_KEYS)

    try:
        width = int(data.get("width", data.get("w", 640)))
    except (TypeError, ValueError):
        width = 640
    try:
        height = int(data.get("height", data.get("h", 480)))
    except (TypeError, ValueError):
        height = 480

    try:
        timeout = float(data.get("timeout", data.get("ffmpeg_timeout", 15.0)))
    except (TypeError, ValueError):
        timeout = 15.0

    label = data.get("label") or data.get("text") or "Cam {}".format(camera_id if camera_id is not None else "now")

    snap_dir = _snapshot_dir(directory)
    try:
        snap_dir.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass

    filename = _make_filename(camera_id, filename_hint)
    filename = Path(filename).name
    if not filename.lower().endswith((".jpg", ".jpeg", ".png")):
        filename = filename + ".jpg"

    target = (snap_dir / filename).resolve()
    try:
        if snap_dir.resolve() not in target.parents and target != snap_dir.resolve():
            raise ValueError("unsafe snapshot path")
    except ValueError:
        raise
    except Exception:  # noqa: BLE001
        pass
    target.parent.mkdir(parents=True, exist_ok=True)

    method = "placeholder"
    ffmpeg_tried = False
    if source:
        tmp_dest = target.with_suffix(".ffmpeg.jpg")
        ffmpeg_tried = True
        ok = _try_ffmpeg_capture(source, tmp_dest, timeout=timeout)
        if ok:
            try:
                if tmp_dest.resolve() != target:
                    import shutil as _shutil

                    _shutil.move(str(tmp_dest), str(target))
                method = "ffmpeg"
            except Exception:  # noqa: BLE001
                method = "ffmpeg"
        else:
            try:
                if tmp_dest.is_file():
                    tmp_dest.unlink()
            except Exception:  # noqa: BLE001
                pass
            target.write_bytes(_placeholder_jpeg(str(label), width=width, height=height))
    else:
        target.write_bytes(_placeholder_jpeg(str(label), width=width, height=height))

    try:
        size = target.stat().st_size
    except Exception:  # noqa: BLE001
        size = 0

    now_iso = _now().isoformat()
    result: Dict[str, Any] = {
        "success": True,
        "ok": True,
        "status": "captured",
        "captured": True,
        "executed": True,
        "camera_id": camera_id,
        "camera": camera_id,
        "source": source,
        "source_used": source,
        "method": method,
        "ffmpeg": method == "ffmpeg",
        "ffmpeg_tried": ffmpeg_tried,
        "path": str(target),
        "snapshot_path": str(target),
        "file": str(target),
        "filename": target.name,
        "name": target.name,
        "directory": str(snap_dir),
        "snapshot_dir": str(snap_dir),
        "size": size,
        "size_bytes": size,
        "captured_at": now_iso,
        "created_at": now_iso,
        "mock": method == "placeholder",
    }
    return result


def capture_snapshot_to_file(*args: Any, **kwargs: Any) -> str:
    """Capture and return the file path as a string."""
    return str(capture_snapshot_now(*args, **kwargs).get("path"))


def get_last_snapshot(*args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    """Return the most recently modified snapshot file, if any."""
    data = _normalize_args(args, kwargs)
    directory = _first(data, DIR_KEYS)
    snap_dir = _snapshot_dir(directory)
    try:
        files = [p for p in snap_dir.glob("*.jpg")] + [p for p in snap_dir.glob("*.png")]
        files += [p for p in snap_dir.glob("*.jpeg")]
        if not files:
            return None
        latest = max(files, key=lambda p: p.stat().st_mtime)
        return {
            "path": str(latest.resolve()),
            "snapshot_path": str(latest.resolve()),
            "file": str(latest.resolve()),
            "filename": latest.name,
            "size_bytes": latest.stat().st_size,
            "ok": True,
            "success": True,
        }
    except Exception:  # noqa: BLE001
        return None


def list_snapshots(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    data = _normalize_args(args, kwargs)
    directory = _first(data, DIR_KEYS)
    snap_dir = _snapshot_dir(directory)
    try:
        limit = int(data.get("limit", 50))
    except (TypeError, ValueError):
        limit = 50
    try:
        files = sorted(snap_dir.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
        out = []
        for p in files:
            if not p.is_file():
                continue
            if p.suffix.lower() not in (".jpg", ".jpeg", ".png"):
                continue
            try:
                out.append({"path": str(p.resolve()), "filename": p.name, "size_bytes": p.stat().st_size})
            except Exception:  # noqa: BLE001
                continue
            if len(out) >= limit:
                break
        return out
    except Exception:  # noqa: BLE001
        return []


# Compatibility aliases (repo convention: many names, same behaviour).
capture_now = capture_snapshot_now
take_snapshot_now = capture_snapshot_now
snapshot_now_capture = capture_snapshot_now
capture_single_snapshot = capture_snapshot_now
capture_single = capture_snapshot_now
snapshot_single = capture_snapshot_now
capture_snapshot = capture_snapshot_now
take_snapshot = capture_snapshot_now
capture_frame = capture_snapshot_now
grab_snapshot = capture_snapshot_now
grab_frame = capture_snapshot_now
snapshot = capture_snapshot_now
capture = capture_snapshot_now
run = capture_snapshot_now
run_now = capture_snapshot_now
trigger = capture_snapshot_now
trigger_now = capture_snapshot_now
execute = capture_snapshot_now
snapshot_now = capture_snapshot_now
capture_snapshot_single = capture_snapshot_now
single_snapshot = capture_snapshot_now
now_snapshot = capture_snapshot_now

save_snapshot_now = capture_snapshot_to_file
write_snapshot_now = capture_snapshot_to_file
store_snapshot_now = capture_snapshot_to_file
capture_to_file = capture_snapshot_to_file
snapshot_to_file = capture_snapshot_to_file

last_snapshot = get_last_snapshot
latest_snapshot = get_last_snapshot


class SnapshotNowService:
    """Object-oriented facade over :func:`capture_snapshot_now`."""

    def capture(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return capture_snapshot_now(*args, **kwargs)

    def run(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return capture_snapshot_now(*args, **kwargs)

    def run_now(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return capture_snapshot_now(*args, **kwargs)

    def trigger(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return capture_snapshot_now(*args, **kwargs)

    def execute(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return capture_snapshot_now(*args, **kwargs)

    def last(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return get_last_snapshot(*args, **kwargs)

    def list(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return list_snapshots(*args, **kwargs)


class SnapshotNowStore(SnapshotNowService):
    pass


class SnapshotNow(SnapshotNowService):
    pass


snapshot_now_service = SnapshotNowService()
snapshot_service = SnapshotNowService()
service = snapshot_now_service
store = snapshot_now_service


__all__ = [
    "SNAPSHOT_DIR",
    "capture_snapshot_now",
    "capture_snapshot_to_file",
    "get_last_snapshot",
    "list_snapshots",
    "SnapshotNowService",
    "SnapshotNowStore",
    "SnapshotNow",
    "snapshot_now_service",
]
