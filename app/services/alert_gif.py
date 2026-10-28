"""Auto-GIF on alert service for VigilCam.

Builds a short animated GIF preview automatically when an alert fires,
reusing the on-demand :mod:`app.services.gif_preview` pipeline so the
regular video export path stays mp4-only.

Relationship to existing modules (verified via grep):

* :mod:`app.services.gif_preview` builds GIFs **only on demand**
  (explicit ``build_gif_preview`` / ``generate_gif_bytes`` calls from
  ``app/api/gif_preview.py`` request handlers).  Nothing in
  ``app/services/alert*``, ``app/services/email*``,
  ``app/services/sms*``, ``app/workers/*``, ``app/main.py`` or
  ``app/templates/index.html`` imports or triggers it.
* This module is the **only** automatic trigger: call
  :func:`handle_alert` (or any alias) with an alert payload and -- when
  enabled -- a GIF is rendered via ``gif_preview`` and recorded in the
  in-memory ``ALERT_GIFS`` store.  Importing this module has no side
  effects: it never monkey-patches alert services and never starts
  background work.

Defensive API: every public helper has aliases so callers (and tests)
can use whichever spelling they prefer.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from app.core.config import DATA_DIR
except Exception:  # noqa: BLE001
    DATA_DIR = Path("data")  # type: ignore[assignment]

CONTENT_TYPE_GIF = "image/gif"
GIF_MAGIC_87 = b"GIF87a"
GIF_MAGIC_89 = b"GIF89a"

DEFAULT_WIDTH = 320
MIN_WIDTH = 16
MAX_WIDTH = 1280
DEFAULT_HEIGHT = 240
MIN_HEIGHT = 16
MAX_HEIGHT = 1280
DEFAULT_FPS = 5.0
MIN_FPS = 1.0
MAX_FPS = 15.0
DEFAULT_DURATION = 3.0
MIN_DURATION = 0.5
MAX_DURATION = 10.0

DEFAULT_DIRNAME = "alert_gifs"

ALERT_GIFS: Dict[str, Dict[str, Any]] = {}
alert_gifs = ALERT_GIFS
GIF_STORE = ALERT_GIFS
STORE = ALERT_GIFS
JOBS = ALERT_GIFS

_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "auto_gif": True,
    "width": int(DEFAULT_WIDTH),
    "height": int(DEFAULT_HEIGHT),
    "fps": float(DEFAULT_FPS),
    "duration": float(DEFAULT_DURATION),
}

_lock = threading.RLock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_alert_gif_dir(override: Any = None) -> Path:
    if override is not None:
        p = Path(str(override)).expanduser()
        try:
            p.mkdir(parents=True, exist_ok=True)
        except Exception:  # noqa: BLE001
            pass
        return p
    try:
        base = Path(str(DATA_DIR)).expanduser()
    except Exception:  # noqa: BLE001
        base = Path("data")
    d = base / DEFAULT_DIRNAME
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    return d


ALERT_GIF_DIR = _default_alert_gif_dir()
ALERT_DIR = ALERT_GIF_DIR
GIF_DIR = ALERT_GIF_DIR
OUTPUT_DIR = ALERT_GIF_DIR


def _safe_name(name: Any) -> str:
    s = str(name if name is not None else "alert")
    s = "".join(c if c.isalnum() or c in "-_" else "_" for c in s)
    s = s.strip("_") or "alert"
    if len(s) > 64:
        s = s[:64]
    return s


def _coerce_camera(value: Any, kwargs: Dict[str, Any]) -> Optional[str]:
    if value is not None:
        s = str(value).strip()
        return s or None
    for key in (
        "camera", "camera_id", "cameraId", "cameraID", "cam", "cam_id",
        "camera_name", "cameraName", "device_id", "deviceId", "source_camera",
    ):
        if kwargs.get(key) is not None:
            s = str(kwargs.get(key)).strip()
            if s:
                return s
    return None


def _coerce_alert_id(value: Any, kwargs: Dict[str, Any]) -> Optional[str]:
    if value is not None:
        s = str(value).strip()
        return s or None
    for key in (
        "alert_id", "alertId", "alertID", "id", "event_id", "eventId",
        "incident_id", "incidentId", "notification_id", "alert_uuid",
    ):
        if kwargs.get(key) is not None:
            s = str(kwargs.get(key)).strip()
            if s:
                return s
    return None


def _coerce_output(value: Any, kwargs: Dict[str, Any]) -> Optional[Path]:
    if value is not None:
        s = str(value).strip()
        if s:
            return Path(s).expanduser()
        return None
    for key in (
        "output", "output_path", "outputPath", "dest", "destination",
        "target", "out", "outfile", "save_to", "save_as", "saveTo",
        "path", "file", "filename", "gif_path", "gifPath",
    ):
        if kwargs.get(key) is not None:
            s = str(kwargs.get(key)).strip()
            if s:
                return Path(s).expanduser()
    return None


def _normalize_alert(args: tuple, kwargs: Dict[str, Any]) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for a in args or ():
        if a is None:
            continue
        if isinstance(a, dict):
            for k, v in a.items():
                data.setdefault(k, v)
        elif isinstance(a, str):
            s = a.strip()
            if not s:
                continue
            # Heuristic: alert id vs camera name.  UUID-ish / numeric -> alert id.
            compact = s.replace("-", "").replace("_", "")
            if compact.isdigit() or (len(s) >= 8 and "-" in s):
                data.setdefault("alert_id", s)
            else:
                data.setdefault("camera", s)
        else:
            try:
                attrs = {
                    k: getattr(a, k)
                    for k in dir(a)
                    if not k.startswith("_") and not callable(getattr(a, k, None))
                }
                if attrs:
                    for k, v in attrs.items():
                        data.setdefault(k, v)
            except Exception:  # noqa: BLE001
                pass
    for k, v in (kwargs or {}).items():
        data[k] = v
    for nested_key in (
        "payload", "options", "config", "params", "data", "body",
        "request", "settings", "alert", "event", "notification",
    ):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _gif_preview():
    try:
        from app.services import gif_preview as _gp  # type: ignore
        return _gp
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def get_config(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Return a copy of the auto-GIF config."""
    with _lock:
        out = dict(_CONFIG)
    # Allow overrides via kwargs without persisting.
    for key in ("enabled", "auto_gif", "autoGif", "auto", "width", "fps", "duration", "height"):
        if kwargs.get(key) is not None:
            norm = key
            if key in ("autoGif", "auto"):
                norm = "auto_gif"
            out[norm] = kwargs.get(key)
    return out


def get_settings(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return get_config(*args, **kwargs)


def get_status(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    with _lock:
        cfg = dict(_CONFIG)
    return {
        "service": "alert_gif",
        "status": "enabled" if cfg.get("enabled") else "disabled",
        "enabled": bool(cfg.get("enabled")),
        "auto_gif": bool(cfg.get("auto_gif", True)),
        "stored": len(ALERT_GIFS),
        "count": len(ALERT_GIFS),
        "ok": True,
        "success": True,
        "content_type": CONTENT_TYPE_GIF,
    }


def configure(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Update auto-GIF config. Unknown keys are stored verbatim."""
    data = _normalize_alert(args, kwargs)
    with _lock:
        for key in ("enabled", "auto_gif", "width", "height", "fps", "duration"):
            if data.get(key) is not None:
                _CONFIG[key] = data[key]
        # Aliases.
        if data.get("autoGif") is not None:
            _CONFIG["auto_gif"] = data["autoGif"]
        if data.get("auto") is not None and data.get("auto_gif") is None and data.get("autoGif") is None:
            _CONFIG["auto_gif"] = data["auto"]
        for k, v in data.items():
            if k not in _CONFIG and k not in (
                "camera", "camera_id", "alert_id", "id", "payload", "options",
                "config", "params", "data", "body", "request", "settings",
                "alert", "event", "notification",
            ):
                _CONFIG[k] = v
        # Coerce numerics.
        try:
            if _CONFIG.get("width") is not None:
                _CONFIG["width"] = int(float(str(_CONFIG["width"])))
        except (TypeError, ValueError):
            _CONFIG["width"] = int(DEFAULT_WIDTH)
        try:
            if _CONFIG.get("height") is not None:
                _CONFIG["height"] = int(float(str(_CONFIG["height"])))
        except (TypeError, ValueError):
            _CONFIG["height"] = int(DEFAULT_HEIGHT)
        try:
            if _CONFIG.get("fps") is not None:
                _CONFIG["fps"] = float(str(_CONFIG["fps"]))
        except (TypeError, ValueError):
            _CONFIG["fps"] = float(DEFAULT_FPS)
        try:
            if _CONFIG.get("duration") is not None:
                _CONFIG["duration"] = float(str(_CONFIG["duration"]))
        except (TypeError, ValueError):
            _CONFIG["duration"] = float(DEFAULT_DURATION)
        return dict(_CONFIG)


def update_config(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return configure(*args, **kwargs)


def set_config(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return configure(*args, **kwargs)


def enable(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    with _lock:
        _CONFIG["enabled"] = True
        _CONFIG["auto_gif"] = True
        return dict(_CONFIG)


def disable(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    with _lock:
        _CONFIG["enabled"] = False
        return dict(_CONFIG)


def is_enabled(*args: Any, **kwargs: Any) -> bool:
    with _lock:
        if kwargs.get("enabled") is not None:
            return bool(kwargs.get("enabled"))
        return bool(_CONFIG.get("enabled", True))


def is_auto_enabled(*args: Any, **kwargs: Any) -> bool:
    with _lock:
        return bool(_CONFIG.get("enabled", True) and _CONFIG.get("auto_gif", True))


# ---------------------------------------------------------------------------
# Core builders
# ---------------------------------------------------------------------------

def _default_output_path(alert_id: Any, camera: Any, gif_dir: Any = None) -> Path:
    d = _default_alert_gif_dir(gif_dir)
    stem = _safe_name(alert_id) if alert_id else _safe_name(camera or "alert")
    return d / f"alert_{stem}_{uuid.uuid4().hex}.gif"


def build_alert_gif(
    camera: Any = None,
    output: Any = None,
    alert_id: Any = None,
    width: Any = None,
    height: Any = None,
    fps: Any = None,
    duration: Any = None,
    frames: Any = None,
    source: Any = None,
    gif_dir: Any = None,
    **kwargs: Any,
) -> str:
    """Build an alert GIF file. Returns the output path as a string.

    Delegates rendering to :mod:`app.services.gif_preview` so output is
    always a valid ``image/gif`` (placeholder fallback when ffmpeg or
    source clips are unavailable).
    """
    if camera is None:
        camera = _coerce_camera(None, kwargs)
    if alert_id is None:
        alert_id = _coerce_alert_id(None, kwargs)
        if alert_id is None and isinstance(camera, dict):
            alert_id = _coerce_alert_id(None, camera)
    # Positional dict support: build_alert_gif({"camera": ..., "alert_id": ...})
    if isinstance(camera, dict) and alert_id is None:
        alert_id = _coerce_alert_id(None, camera)
        camera = _coerce_camera(None, camera)
    if output is None:
        output = _coerce_output(None, kwargs)
    if source is None:
        for key in ("input", "input_path", "src", "video", "clip", "recording", "clips"):
            if kwargs.get(key) is not None:
                source = kwargs.get(key)
                break
    with _lock:
        cfg = dict(_CONFIG)
    if width is None:
        width = kwargs.get("width", kwargs.get("w", cfg.get("width", DEFAULT_WIDTH)))
    if height is None:
        height = kwargs.get("height", kwargs.get("h", cfg.get("height", DEFAULT_HEIGHT)))
    if fps is None:
        fps = kwargs.get("fps", kwargs.get("frame_rate", cfg.get("fps", DEFAULT_FPS)))
    if duration is None:
        duration = kwargs.get("duration", kwargs.get("seconds", cfg.get("duration", DEFAULT_DURATION)))
    out_path: Optional[Path] = None
    if output is not None:
        out_path = Path(str(output)).expanduser()
    else:
        out_path = _default_output_path(
            alert_id, camera, gif_dir or kwargs.get("gif_dir") or kwargs.get("output_dir")
        )
    assert out_path is not None
    out_path = Path(str(out_path)).expanduser()
    if out_path.suffix.lower() != ".gif":
        out_path = out_path.with_suffix(".gif")
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass

    gp = _gif_preview()
    if gp is not None:
        try:
            result = gp.build_gif_preview(
                camera or alert_id or "alert",
                str(out_path),
                width=width,
                height=height,
                fps=fps,
                duration=duration,
                frames=frames,
                source=source,
                **{k: v for k, v in kwargs.items() if k not in ("camera", "alert_id", "output")},
            )
            out_str = str(result)
        except Exception:  # noqa: BLE001
            # Fall back to raw bytes render below.
            data = gp.generate_gif_bytes(
                camera or alert_id or "alert",
                width=width, height=height, fps=fps, duration=duration, frames=frames,
            )
            with open(out_path, "wb") as fh:
                fh.write(bytes(data))
            out_str = str(out_path)
    else:  # pragma: no cover - gif_preview should always be present
        raise RuntimeError("gif_preview service unavailable")

    # Record in store when an alert id is known.
    key = str(alert_id) if alert_id else None
    if key is None:
        # Still record under generated id so list/get work for bare camera builds.
        key = f"alert-{uuid.uuid4().hex[:8]}"
        alert_id = key
    record: Dict[str, Any] = {
        "alert_id": str(alert_id),
        "id": str(alert_id),
        "event_id": str(alert_id),
        "camera": camera,
        "camera_id": camera,
        "camera_name": camera,
        "gif_path": out_str,
        "output_path": out_str,
        "output": out_str,
        "path": out_str,
        "width": width,
        "fps": fps,
        "duration": duration,
        "format": "gif",
        "output_format": "gif",
        "content_type": CONTENT_TYPE_GIF,
        "created_at": _now_iso(),
        "ok": True,
        "success": True,
    }
    with _lock:
        ALERT_GIFS[str(alert_id)] = record
    return out_str


def generate_alert_gif_bytes(
    camera: Any = None,
    width: Any = None,
    height: Any = None,
    fps: Any = None,
    duration: Any = None,
    frames: Any = None,
    **kwargs: Any,
) -> bytes:
    """Render an alert GIF as bytes (no file written)."""
    gp = _gif_preview()
    if gp is None:  # pragma: no cover
        raise RuntimeError("gif_preview service unavailable")
    if camera is None:
        camera = _coerce_camera(None, kwargs)
    return bytes(
        gp.generate_gif_bytes(
            camera or "alert", width=width, height=height,
            fps=fps, duration=duration, frames=frames, **kwargs,
        )
    )


def handle_alert(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Auto-build a GIF for an alert payload.

    Returns a result dict.  When the service is disabled (or
    ``auto_gif`` is False) no file is written and the dict carries
    ``gif_skipped=True`` instead of ``gif_path``.
    """
    data = _normalize_alert(args, kwargs)
    camera = _coerce_camera(data.get("camera"), data)
    if camera is None:
        # Fall back to generic target/source keys used by alert domains.
        for key in ("target", "stream", "source", "device", "camera_name"):
            if data.get(key) is not None:
                s = str(data.get(key)).strip()
                if s:
                    camera = s
                    break
    alert_id = _coerce_alert_id(data.get("alert_id"), data)
    if alert_id is None:
        alert_id = str(data.get("id") or data.get("event_id") or f"alert-{uuid.uuid4().hex[:8]}")

    with _lock:
        enabled = bool(_CONFIG.get("enabled", True) and _CONFIG.get("auto_gif", True))
        cfg = dict(_CONFIG)
    if not enabled or data.get("skip_gif") is True or data.get("gif") is False:
        return {
            "alert_id": str(alert_id),
            "id": str(alert_id),
            "camera": camera,
            "camera_id": camera,
            "enabled": bool(_CONFIG.get("enabled", True)),
            "auto_gif": bool(_CONFIG.get("auto_gif", True)),
            "gif_skipped": True,
            "skipped": True,
            "gif_path": None,
            "output_path": None,
            "format": "gif",
            "content_type": CONTENT_TYPE_GIF,
            "ok": True,
            "success": True,
        }

    width = data.get("width", data.get("w", cfg.get("width", DEFAULT_WIDTH)))
    height = data.get("height", data.get("h", cfg.get("height", DEFAULT_HEIGHT)))
    fps = data.get("fps", data.get("frame_rate", cfg.get("fps", DEFAULT_FPS)))
    duration = data.get("duration", data.get("seconds", cfg.get("duration", DEFAULT_DURATION)))
    output = _coerce_output(None, data)
    source = data.get("source", data.get("clip", data.get("video", data.get("recording"))))
    try:
        out = build_alert_gif(
            camera, str(output) if output is not None else None,
            alert_id=alert_id, width=width, height=height,
            fps=fps, duration=duration,
            frames=data.get("frames", data.get("frame_count")),
            source=source,
            gif_dir=data.get("gif_dir", data.get("output_dir")),
        )
    except ValueError as exc:
        raise exc
    except Exception as exc:  # noqa: BLE001
        return {
            "alert_id": str(alert_id),
            "id": str(alert_id),
            "camera": camera,
            "camera_id": camera,
            "error": str(exc),
            "gif_skipped": True,
            "ok": False,
            "success": False,
            "format": "gif",
            "content_type": CONTENT_TYPE_GIF,
        }
    record = get_alert_gif(alert_id) or {}
    result: Dict[str, Any] = {
        "alert_id": str(alert_id),
        "id": str(alert_id),
        "event_id": str(alert_id),
        "camera": camera,
        "camera_id": camera,
        "camera_name": camera,
        "gif_path": out,
        "output_path": out,
        "output": out,
        "path": out,
        "width": record.get("width", width),
        "fps": record.get("fps", fps),
        "duration": record.get("duration", duration),
        "format": "gif",
        "output_format": "gif",
        "content_type": CONTENT_TYPE_GIF,
        "gif_skipped": False,
        "ok": True,
        "success": True,
        "status": "completed",
    }
    return result


def maybe_build_alert_gif(*args: Any, **kwargs: Any) -> Optional[str]:
    """Build only when enabled; return None when skipped."""
    if not is_auto_enabled():
        return None
    data = _normalize_alert(args, kwargs)
    if data.get("skip_gif") is True or data.get("gif") is False:
        return None
    return build_alert_gif(*args, **kwargs)


def should_build_alert_gif(*args: Any, **kwargs: Any) -> bool:
    data = _normalize_alert(args, kwargs)
    if data.get("skip_gif") is True or data.get("gif") is False:
        return False
    return bool(is_auto_enabled())


def get_alert_gif(alert_id: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    key = str(alert_id).strip() if alert_id is not None else ""
    if not key:
        alt = _coerce_alert_id(None, kwargs)
        key = str(alt) if alt else ""
    if not key:
        return None
    with _lock:
        rec = ALERT_GIFS.get(key)
        return dict(rec) if rec is not None else None


def list_alert_gifs(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    data = _normalize_alert(args, kwargs)
    with _lock:
        items = [dict(v) for v in ALERT_GIFS.values()]
    cam = _coerce_camera(None, data)
    if cam is not None:
        items = [it for it in items if str(it.get("camera") or "") == str(cam)]
    try:
        limit = int(data.get("limit", 100))
    except (TypeError, ValueError):
        limit = 100
    return items[: max(0, limit)]


def delete_alert_gif(alert_id: Any, **kwargs: Any) -> bool:
    key = str(alert_id) if alert_id is not None else _coerce_alert_id(None, kwargs)
    if key is None:
        return False
    with _lock:
        return ALERT_GIFS.pop(str(key), None) is not None


def clear_alert_gifs(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    with _lock:
        n = len(ALERT_GIFS)
        ALERT_GIFS.clear()
    return {"ok": True, "success": True, "cleared": n, "count": 0}


# ---------------------------------------------------------------------------
# Aliases (repo convention: many names, same behaviour)
# ---------------------------------------------------------------------------

build = build_alert_gif
create = build_alert_gif
make = build_alert_gif
render = build_alert_gif
generate = build_alert_gif
build_gif = build_alert_gif
create_gif = build_alert_gif
create_alert_gif = build_alert_gif
generate_alert_gif = build_alert_gif
make_alert_gif = build_alert_gif
render_alert_gif = build_alert_gif
get_gif = build_alert_gif
build_alert = build_alert_gif
trigger_alert_gif = build_alert_gif
trigger = build_alert_gif
run = build_alert_gif

on_alert = handle_alert
process_alert = handle_alert
handle = handle_alert
trigger_alert = handle_alert
alert_handler = handle_alert
handle_event = handle_alert
on_event = handle_alert
process_event = handle_alert
auto_gif = handle_alert
auto_build = handle_alert

render_alert_gif_bytes = generate_alert_gif_bytes
get_alert_gif_bytes = generate_alert_gif_bytes
make_alert_gif_bytes = generate_alert_gif_bytes
alert_gif_bytes = generate_alert_gif_bytes
generate_bytes = generate_alert_gif_bytes
get_bytes = generate_alert_gif_bytes

maybe_build = maybe_build_alert_gif
maybe_handle = handle_alert
should_build = should_build_alert_gif

get = get_alert_gif
get_gif_record = get_alert_gif
list_all = list_alert_gifs
list_all_gifs = list_alert_gifs
list_gifs = list_alert_gifs
clear = clear_alert_gifs
clear_all = clear_alert_gifs
reset = clear_alert_gifs
delete = delete_alert_gif
remove = delete_alert_gif
cancel = delete_alert_gif


class AlertGifService:
    """Object-oriented facade over the module-level alert-GIF state."""

    def build(self, *args: Any, **kwargs: Any) -> str:
        return build_alert_gif(*args, **kwargs)

    def handle(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return handle_alert(*args, **kwargs)

    def on_alert(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return handle_alert(*args, **kwargs)

    def process(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return handle_alert(*args, **kwargs)

    def trigger(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return handle_alert(*args, **kwargs)

    def generate_bytes(self, *args: Any, **kwargs: Any) -> bytes:
        return generate_alert_gif_bytes(*args, **kwargs)

    def get(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return get_alert_gif(*args, **kwargs)

    def list(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return list_alert_gifs(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> bool:
        return delete_alert_gif(*args, **kwargs)

    def clear(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return clear_alert_gifs(*args, **kwargs)

    def get_config(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return get_config(*args, **kwargs)

    def configure(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return configure(*args, **kwargs)

    def enable(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return enable(*args, **kwargs)

    def disable(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return disable(*args, **kwargs)

    def is_enabled(self, *args: Any, **kwargs: Any) -> bool:
        return is_enabled(*args, **kwargs)

    def build_alert_gif(self, *args: Any, **kwargs: Any) -> str:
        return build_alert_gif(*args, **kwargs)

    def handle_alert(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return handle_alert(*args, **kwargs)


class AlertGifBuilder(AlertGifService):
    pass


class AlertGifManager(AlertGifService):
    pass


class AlertGifHandler(AlertGifService):
    pass


class Service(AlertGifService):
    pass


class Builder(AlertGifService):
    pass


alert_gif_service = AlertGifService()
builder = alert_gif_service
service = alert_gif_service
manager = alert_gif_service
handler = alert_gif_service
default_service = alert_gif_service
instance = alert_gif_service
singleton = alert_gif_service

AlertManager = AlertGifService
GifManager = AlertGifService
AlertHandler = AlertGifService

__all__ = [
    "CONTENT_TYPE_GIF",
    "ALERT_GIF_DIR",
    "ALERT_GIFS",
    "DEFAULT_WIDTH",
    "DEFAULT_FPS",
    "DEFAULT_DURATION",
    "build_alert_gif",
    "generate_alert_gif_bytes",
    "handle_alert",
    "maybe_build_alert_gif",
    "should_build_alert_gif",
    "get_alert_gif",
    "list_alert_gifs",
    "delete_alert_gif",
    "clear_alert_gifs",
    "get_config",
    "configure",
    "enable",
    "disable",
    "is_enabled",
    "AlertGifService",
    "AlertGifBuilder",
    "alert_gif_service",
]
