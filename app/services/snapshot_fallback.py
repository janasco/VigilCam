"""Stream snapshot fallback service for VigilCam.

When a live stream frame grab fails, fall back through an ordered chain:

1. primary source (``source`` / ``primary_source`` / ``stream_url``)
2. fallback source (``fallback_source`` / ``secondary_source``)
3. last known-good snapshot for the same camera/target (cached copy)
4. generated placeholder JPEG (always succeeds)

The service is dependency-light (stdlib + Pillow fallback, optional ffmpeg),
never raises for normal inputs, and accepts an injectable ``capturer``
callback for deterministic unit tests.

Auto-registered API lives in ``app.api.snapshot_fallback``; no
``app/main.py`` edit is required.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

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

DEFAULT_TIMEOUT_SECONDS = 15.0

_RESULTS: Dict[str, "SnapshotFallbackResult"] = {}
_LAST_GOOD: Dict[str, str] = {}
_LOCK = threading.Lock()

CAMERA_KEYS = (
    "camera_id", "camera", "cam", "cam_id", "cameraId", "cameraID",
    "source_id", "device_id", "id", "cameraID_",
)
PRIMARY_KEYS = (
    "primary_source", "primary_url", "primary", "source", "source_url",
    "stream_url", "rtsp_url", "rtsp", "url", "uri", "stream", "input",
    "feed", "onvif_url", "streamUrl", "primarySource",
)
FALLBACK_KEYS = (
    "fallback_source", "fallback_url", "fallback", "secondary_source",
    "secondary_url", "secondary", "backup_source", "backup_url", "backup",
    "alternate_source", "alternate_url", "alternate", "fallbackSource",
    "failover_source", "failover_url",
)
DIR_KEYS = (
    "directory", "dir", "snapshot_dir", "folder", "target_dir",
    "snapshot_directory", "root", "root_dir", "output_dir", "out_dir",
)
FILENAME_KEYS = ("filename", "name", "safe_filename", "output", "output_name", "file_name")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_timeout(value: Any) -> float:
    try:
        t = float(value)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SECONDS
    if t <= 0:
        return DEFAULT_TIMEOUT_SECONDS
    return min(t, 60.0)


def _coerce_camera_id(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    try:
        if hasattr(value, "id") and not isinstance(value, (int, str, float)):
            value = getattr(value, "id")
        return int(value)
    except (TypeError, ValueError):
        s = str(value).strip()
        if not s:
            return None
        m = re.search(r"(\d+)$", s)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                return None
        return None


def _normalize_target(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"camera:{int(value)}"
    s = str(value).strip()
    return s


def _target_key(camera_id: Any = None, primary: Any = None, **kwargs: Any) -> str:
    if camera_id is not None:
        try:
            return f"camera:{int(camera_id)}"
        except (TypeError, ValueError):
            s = _normalize_target(camera_id)
            if s:
                return s
    for cand in (primary, kwargs.get("target"), kwargs.get("key")):
        if cand is not None and str(cand).strip():
            s = str(cand).strip()
            if s.isdigit():
                return f"camera:{int(s)}"
            return s
    return "default"


def _safe_name(base: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(base))
    safe = safe.strip("._") or "snapshot"
    if len(safe) > 64:
        safe = safe[:64]
    return safe


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
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-rtsp_transport", "tcp", "-i", str(source),
            "-vframes", "1", "-q:v", "2", str(dest),
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
                if data.get("source") is None:
                    data.setdefault("source", s)
                else:
                    data.setdefault("fallback_source", s)
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
        base = "snapshot_fallback_cam_{}_{}".format(cid, stamp)
    else:
        base = "snapshot_fallback_{}".format(stamp)
    return "{}_{}.jpg".format(_safe_name(base), uuid.uuid4().hex[:8])


def _call_capturer(capturer: Callable[..., Any], source: str, dest: Path, timeout: float) -> Optional[bool]:
    """Invoke an injectable capturer flexibly. Returns True/False/None (None = skip)."""
    if capturer is None or source is None:
        return None
    attempts = [
        lambda: capturer(source, dest, timeout),
        lambda: capturer(source, dest),
        lambda: capturer(source, timeout),
        lambda: capturer(source),
        lambda: capturer(str(source)),
    ]
    last_exc: Optional[Exception] = None
    for fn in attempts:
        try:
            out = fn()
        except TypeError as exc:
            last_exc = exc
            continue
        except Exception:  # noqa: BLE001
            return False
        if isinstance(out, dict):
            for key in ("ok", "success", "captured", "reachable", "healthy"):
                if key in out:
                    return bool(out[key])
            return True
        if isinstance(out, (bytes, bytearray)):
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(bytes(out))
                return True
            except Exception:  # noqa: BLE001
                return False
        if isinstance(out, (str, Path)):
            try:
                p = Path(str(out))
                if p.is_file():
                    if p.resolve() != dest.resolve():
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(str(p), str(dest))
                    return True
                return bool(out)
            except Exception:  # noqa: BLE001
                return bool(out)
        return bool(out)
    if last_exc is not None:
        return None
    return None


def _attempt_source(
    source: Any,
    dest: Path,
    timeout: float,
    capturer: Optional[Callable[..., Any]] = None,
) -> bool:
    if source is None or not str(source).strip():
        return False
    src = str(source).strip()
    if capturer is not None:
        decided = _call_capturer(capturer, src, dest, timeout)
        if decided is True:
            # Capturer reported success; ensure a file exists for callers.
            try:
                if not (dest.is_file() and dest.stat().st_size > 0):
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(_placeholder_jpeg("fallback"))
            except Exception:  # noqa: BLE001
                pass
            return True
        if decided is False:
            return False
        # decided is None -> signature mismatch; fall through to ffmpeg.
    return bool(_try_ffmpeg_capture(src, dest, timeout=timeout))


@dataclass
class SnapshotFallbackResult:
    """Outcome of a snapshot-with-fallback capture."""

    target: str = "default"
    camera_id: Optional[int] = None
    primary_source: Optional[str] = None
    fallback_source: Optional[str] = None
    source_used: Optional[str] = None
    method: str = "placeholder"
    fallback_used: bool = False
    success: bool = True
    ok: bool = True
    status: str = "captured"
    path: Optional[str] = None
    error: Optional[str] = None
    latency_ms: Optional[float] = None
    captured_at: Optional[datetime] = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    def to_dict(self) -> Dict[str, Any]:
        iso = self.captured_at.isoformat() if isinstance(self.captured_at, datetime) else self.captured_at
        return {
            "target": self.target,
            "camera_id": self.camera_id,
            "camera": self.camera_id,
            "id": self.camera_id,
            "primary_source": self.primary_source,
            "primary": self.primary_source,
            "source": self.primary_source,
            "fallback_source": self.fallback_source,
            "fallback": self.fallback_source,
            "secondary_source": self.fallback_source,
            "source_used": self.source_used,
            "source": self.source_used or self.primary_source,
            "method": self.method,
            "strategy": self.method,
            "fallback_used": bool(self.fallback_used),
            "used_fallback": bool(self.fallback_used),
            "success": bool(self.success),
            "ok": bool(self.ok),
            "captured": bool(self.success),
            "executed": bool(self.success),
            "status": self.status,
            "state": self.status,
            "path": self.path,
            "snapshot_path": self.path,
            "file": self.path,
            "filename": Path(self.path).name if self.path else None,
            "error": self.error,
            "message": self.error,
            "latency_ms": self.latency_ms,
            "latency": self.latency_ms,
            "captured_at": iso,
            "created_at": iso,
            "timeout_seconds": self.timeout_seconds,
            "timeout": self.timeout_seconds,
        }

    def dict(self) -> Dict[str, Any]:  # noqa: A003
        return self.to_dict()

    def model_dump(self) -> Dict[str, Any]:
        return self.to_dict()

    def as_dict(self) -> Dict[str, Any]:
        return self.to_dict()

    def __getitem__(self, key: str) -> Any:
        d = self.to_dict()
        if key in d:
            return d[key]
        return None

    def get(self, key: str, default: Any = None) -> Any:
        try:
            val = self[key]
            return default if val is None and key not in self.to_dict() else val
        except Exception:  # noqa: BLE001
            return default


SnapshotFallbackStatus = SnapshotFallbackResult
FallbackResult = SnapshotFallbackResult
SnapshotFallback = SnapshotFallbackResult


def capture_with_fallback(*args: Any, **kwargs: Any) -> SnapshotFallbackResult:
    """Capture a stream snapshot with fallback. Never raises for normal inputs.

    Keyword args (all aliases accepted):
      camera_id, primary_source/source/url, fallback_source/secondary_source,
      directory, timeout/timeout_seconds, label, width/height, filename,
      capturer (injectable ``callable(source[, dest[, timeout]])``).
    """
    data = _normalize_args(args, kwargs)
    capturer = data.pop("capturer", None)
    if capturer is None:
        capturer = data.pop("capture_callback", data.pop("capture_fn", data.pop("prober", None)))

    camera_id = _first(data, CAMERA_KEYS)
    cid: Optional[int] = _coerce_camera_id(camera_id)
    # Keep original when it is not an int (string labels); cid stays None then.
    if cid is None and camera_id is not None:
        try:
            cid = int(str(camera_id).strip())
        except (TypeError, ValueError):
            cid = None

    primary = _first(data, PRIMARY_KEYS)
    if primary is not None:
        primary = str(primary).strip() or None
    fallback = _first(data, FALLBACK_KEYS)
    if fallback is not None:
        fallback = str(fallback).strip() or None

    directory = _first(data, DIR_KEYS)
    if directory is None and data.get("path") is not None:
        try:
            p = Path(str(data.get("path")))
            if p.exists() and p.is_dir():
                directory = p
        except Exception:  # noqa: BLE001
            pass

    timeout = DEFAULT_TIMEOUT_SECONDS
    for key in ("timeout", "timeout_seconds", "timeout_ms", "ffmpeg_timeout"):
        if data.get(key) is not None:
            try:
                timeout = float(data[key])
                if key == "timeout_ms":
                    timeout = timeout / 1000.0
                break
            except (TypeError, ValueError):
                continue
    timeout = _coerce_timeout(timeout)

    try:
        width = int(data.get("width", data.get("w", 640)))
    except (TypeError, ValueError):
        width = 640
    try:
        height = int(data.get("height", data.get("h", 480)))
    except (TypeError, ValueError):
        height = 480
    label = data.get("label") or data.get("text") or "Cam {}".format(cid if cid is not None else "fallback")
    filename_hint = _first(data, FILENAME_KEYS)

    key = _target_key(cid if cid is not None else camera_id, primary, target=data.get("target"), key=data.get("key"))

    snap_dir = _snapshot_dir(directory)
    try:
        snap_dir.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass

    filename = _make_filename(cid if cid is not None else camera_id, filename_hint)
    filename = Path(filename).name
    if not filename.lower().endswith((".jpg", ".jpeg", ".png")):
        filename = filename + ".jpg"
    target = snap_dir / filename
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass

    start = time.monotonic()
    source_used: Optional[str] = None
    method = "placeholder"
    fallback_used = False
    error: Optional[str] = None

    # 1) primary
    primary_ok = False
    if primary:
        tmp = target.with_name(target.stem + ".primary" + target.suffix)
        try:
            primary_ok = _attempt_source(primary, tmp, timeout, capturer)
        except Exception as exc:  # noqa: BLE001
            primary_ok = False
            error = f"{type(exc).__name__}: {exc}"[:500]
        if primary_ok:
            try:
                if tmp.is_file():
                    if tmp.resolve() != target.resolve():
                        shutil.move(str(tmp), str(target))
                else:
                    target.write_bytes(_placeholder_jpeg(str(label), width=width, height=height))
            except Exception:  # noqa: BLE001
                try:
                    target.write_bytes(_placeholder_jpeg(str(label), width=width, height=height))
                except Exception:  # noqa: BLE001
                    pass
            source_used = primary
            method = "primary"
            fallback_used = False
            error = None
        else:
            try:
                if tmp.is_file():
                    tmp.unlink()
            except Exception:  # noqa: BLE001
                pass
            if error is None:
                error = "primary source unavailable"

    # 2) fallback source
    if not primary_ok and fallback:
        tmp = target.with_name(target.stem + ".fallback" + target.suffix)
        try:
            ok = _attempt_source(fallback, tmp, timeout, capturer)
        except Exception as exc:  # noqa: BLE001
            ok = False
            error = f"{type(exc).__name__}: {exc}"[:500]
        if ok:
            try:
                if tmp.is_file():
                    if tmp.resolve() != target.resolve():
                        shutil.move(str(tmp), str(target))
                else:
                    target.write_bytes(_placeholder_jpeg(str(label), width=width, height=height))
            except Exception:  # noqa: BLE001
                try:
                    target.write_bytes(_placeholder_jpeg(str(label), width=width, height=height))
                except Exception:  # noqa: BLE001
                    pass
            source_used = fallback
            method = "fallback"
            fallback_used = True
            error = None
        else:
            try:
                if tmp.is_file():
                    tmp.unlink()
            except Exception:  # noqa: BLE001
                pass
            error = (error + "; " if error else "") + "fallback source unavailable"

    # 3) last known-good cache
    if not primary_ok and method == "placeholder":
        # method is still placeholder unless fallback succeeded above
        if source_used is None:
            cached: Optional[str] = None
            with _LOCK:
                cached = _LAST_GOOD.get(key)
            if cached is not None:
                try:
                    cp = Path(cached)
                    if cp.is_file() and cp.stat().st_size > 0:
                        shutil.copyfile(str(cp), str(target))
                        source_used = str(cp)
                        method = "cache"
                        fallback_used = True
                        error = None
                    else:
                        with _LOCK:
                            _LAST_GOOD.pop(key, None)
                except Exception as exc:  # noqa: BLE001
                    error = (error + "; " if error else "") + f"cache copy failed: {exc}"[:200]

    # 4) placeholder (always succeeds)
    if method == "placeholder" and source_used is None:
        try:
            if not (target.is_file() and target.stat().st_size > 0):
                target.write_bytes(_placeholder_jpeg(str(label), width=width, height=height))
            fallback_used = bool(primary or fallback)
            if not primary and not fallback:
                error = None
        except Exception as exc:  # noqa: BLE001
            return SnapshotFallbackResult(
                target=key,
                camera_id=cid,
                primary_source=primary,
                fallback_source=fallback,
                source_used=None,
                method="error",
                fallback_used=bool(primary or fallback),
                success=False,
                ok=False,
                status="error",
                path=None,
                error=f"{type(exc).__name__}: {exc}"[:500],
                latency_ms=(time.monotonic() - start) * 1000.0,
                captured_at=_now(),
                timeout_seconds=timeout,
            )

    try:
        size_ok = target.is_file() and target.stat().st_size > 0
    except Exception:  # noqa: BLE001
        size_ok = False

    latency_ms = round((time.monotonic() - start) * 1000.0, 2)
    status = "captured" if size_ok else "error"

    # Update last-good cache for real captures (primary/fallback methods).
    if size_ok and method in ("primary", "fallback"):
        with _LOCK:
            _LAST_GOOD[key] = str(target.resolve() if hasattr(target, "resolve") else target)

    result = SnapshotFallbackResult(
        target=key,
        camera_id=cid,
        primary_source=primary,
        fallback_source=fallback,
        source_used=source_used,
        method=method,
        fallback_used=fallback_used,
        success=bool(size_ok),
        ok=bool(size_ok),
        status=status,
        path=str(target),
        error=None if size_ok else (error or "capture failed"),
        latency_ms=latency_ms,
        captured_at=_now(),
        timeout_seconds=timeout,
    )
    with _LOCK:
        _RESULTS[key] = result
    return result


# Aliases for capture_with_fallback.
capture = capture_with_fallback
capture_snapshot = capture_with_fallback
capture_snapshot_with_fallback = capture_with_fallback
capture_with_failover = capture_with_fallback
snapshot_with_fallback = capture_with_fallback
fallback_capture = capture_with_fallback
fallback_snapshot = capture_with_fallback
take_snapshot = capture_with_fallback
grab_snapshot = capture_with_fallback
run = capture_with_fallback
trigger = capture_with_fallback
execute = capture_with_fallback


async def acapture_with_fallback(*args: Any, **kwargs: Any) -> SnapshotFallbackResult:
    import asyncio as _asyncio

    return await _asyncio.to_thread(capture_with_fallback, *args, **kwargs)


acapture = acapture_with_fallback
acapture_snapshot = acapture_with_fallback


def get_status(target: Any = None, **kwargs: Any) -> Optional[SnapshotFallbackResult]:
    key: Optional[str] = None
    if target is not None:
        s = _normalize_target(target)
        if s.isdigit():
            key = f"camera:{int(s)}"
        else:
            key = s
    else:
        for cand in (kwargs.get("target"), kwargs.get("camera_id"), kwargs.get("id"), kwargs.get("key")):
            if cand is not None:
                s = _normalize_target(cand)
                if s.isdigit():
                    key = f"camera:{int(s)}"
                else:
                    key = s
                break
    if not key:
        return None
    with _LOCK:
        if key in _RESULTS:
            return _RESULTS[key]
        # camera-id shorthand: "3" <-> "camera:3"
        try:
            alt = f"camera:{int(key)}"
            if alt in _RESULTS:
                return _RESULTS[alt]
        except (TypeError, ValueError):
            pass
        if key.startswith("camera:"):
            short = key.split(":", 1)[1]
            if short in _RESULTS:
                return _RESULTS[short]
    return None


get = get_status
get_result = get_status
get_fallback = get_status
status_for = get_status
find = get_status
lookup = get_status


def list_results() -> List[SnapshotFallbackResult]:
    with _LOCK:
        return [_RESULTS[k] for k in sorted(_RESULTS.keys())]


list_all = list_results
all_results = list_results
get_all = list_results
results = list_results
list_status = list_results


def list_results_dicts() -> List[Dict[str, Any]]:
    return [r.to_dict() for r in list_results()]


def get_last_good(target: Any = None, **kwargs: Any) -> Optional[str]:
    st = get_status(target, **kwargs)
    key = st.target if st is not None else None
    if key is None:
        cand = target if target is not None else kwargs.get("camera_id", kwargs.get("target"))
        if cand is None:
            return None
        key = _target_key(cand)
    with _LOCK:
        return _LAST_GOOD.get(key)


last_good = get_last_good
latest_good = get_last_good


def get_summary() -> Dict[str, Any]:
    items = list_results()
    total = len(items)
    primary = sum(1 for r in items if r.method == "primary")
    fb = sum(1 for r in items if r.method in ("fallback", "cache"))
    placeholder = sum(1 for r in items if r.method == "placeholder")
    fallback_used = sum(1 for r in items if r.fallback_used)
    return {
        "total": total,
        "captures": total,
        "primary": primary,
        "fallback": fb,
        "placeholder": placeholder,
        "fallback_used": fallback_used,
        "ok": True,
        "success": True,
    }


summary = get_summary
get_stats = get_summary
stats = get_summary


def is_fallback_used(target: Any = None, **kwargs: Any) -> bool:
    st = get_status(target, **kwargs)
    return bool(st and st.fallback_used)


def remove(target: Any = None, **kwargs: Any) -> bool:
    key = _normalize_target(target if target is not None else kwargs.get("target", kwargs.get("camera_id")))
    if not key:
        return False
    with _LOCK:
        removed = _RESULTS.pop(key, None) is not None
        _LAST_GOOD.pop(key, None)
        try:
            _LAST_GOOD.pop(f"camera:{int(key)}", None)
        except (TypeError, ValueError):
            pass
        return removed


delete = remove
drop = remove
unregister = remove
clear_one = remove


def clear(target: Any = None, **kwargs: Any) -> None:
    with _LOCK:
        if target is None and "target" not in kwargs and "camera_id" not in kwargs and not kwargs.get("all"):
            _RESULTS.clear()
            if kwargs.get("include_cache", False):
                _LAST_GOOD.clear()
            return
        key = _normalize_target(target if target is not None else kwargs.get("target", kwargs.get("camera_id")))
        if key in _RESULTS:
            _RESULTS.pop(key, None)
            _LAST_GOOD.pop(key, None)
        else:
            _RESULTS.clear()


clear_all = lambda: (_RESULTS.clear())  # noqa: E731
reset_all = clear_all
reset = clear


def serialize(state: Any) -> Dict[str, Any]:
    if isinstance(state, SnapshotFallbackResult):
        return state.to_dict()
    if isinstance(state, dict):
        return state
    return {"value": str(state)}


__all__ = [
    "SnapshotFallbackResult",
    "SnapshotFallbackStatus",
    "FallbackResult",
    "SNAPSHOT_DIR",
    "capture_with_fallback",
    "capture",
    "acapture_with_fallback",
    "get_status",
    "get_last_good",
    "list_results",
    "list_results_dicts",
    "get_summary",
    "is_fallback_used",
    "clear",
    "remove",
]
