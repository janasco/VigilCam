"""Note snapshot service — visual snapshots attached to operator notes.

Verification: :mod:`app.services.event_notes` and
:mod:`app.services.camera_notes` store only text fields (``text``/``body``
plus ``author``/ids) with no snapshot/thumbnail/image fields. This module
fills that gap by capturing a still frame and attaching it to a note
entry (``snapshot_path`` / ``thumbnail_base64`` / ``image``).

The store is in-memory and thread-safe so the feature works without DB
migrations and without editing ``app/main.py``. The API router
(``app/api/note_snaps.py``) is auto-registered by the ``pkgutil``
auto-loader in ``app/main.py``.
"""

from __future__ import annotations

import base64
import io
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

NOTE_SNAPS: Dict[str, Dict[str, Any]] = {}
NOTES = NOTE_SNAPS
SNAPS = NOTE_SNAPS
NOTE_SNAPSHOTS = NOTE_SNAPS
NOTE_STORE = NOTE_SNAPS
STORE = NOTE_SNAPS
ITEMS = NOTE_SNAPS
SNAPSHOTS = NOTE_SNAPS
note_snaps = NOTE_SNAPS
snaps = NOTE_SNAPS
snapshots = NOTE_SNAPS

_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _snapshot_dir(override: Any = None) -> Path:
    if override is not None:
        try:
            return Path(str(override)).expanduser()
        except Exception:  # noqa: BLE001
            pass
    try:
        from app.core import config as _config

        cand = getattr(_config, "SNAPSHOT_DIR", None)
        if cand is not None:
            return Path(str(cand)).expanduser()
    except Exception:  # noqa: BLE001
        pass
    try:
        import app.services.snapshot_now as _sn  # noqa: F401

        cand = getattr(_sn, "SNAPSHOT_DIR", None)
        if cand is not None:
            return Path(str(cand)).expanduser()
    except Exception:  # noqa: BLE001
        pass
    return Path("data/snapshots").expanduser()


SNAPSHOT_DIR = _snapshot_dir()


def _safe_name(base: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(base))
    safe = safe.strip("._") or "note"
    if len(safe) > 64:
        safe = safe[:64]
    return safe


def _placeholder_jpeg(label: str = "VigilCam", width: int = 640, height: int = 480) -> bytes:
    try:
        w = max(16, min(1280, int(width)))
    except (TypeError, ValueError):
        w = 640
    try:
        h = max(16, min(1280, int(height)))
    except (TypeError, ValueError):
        h = 480
    try:
        from PIL import Image, ImageDraw  # type: ignore

        img = Image.new("RGB", (w, h), color=(20, 20, 30))
        draw = ImageDraw.Draw(img)
        draw.text((10, max(10, h // 2 - 10)), str(label)[:48], fill=(255, 255, 255))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=70)
        return buf.getvalue()
    except Exception:  # noqa: BLE001
        return (
            b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
            b"\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00"
            b"\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b"
            b"\xff\xc4\x00\xb5\x10\x00\x02\x01\x03\x03\x02\x04\x03\x05\x05\x04\x04\x00\x00\x01}\x01\x02\x03\x00\x04\x11\x05\x12!1A\x06\x13Qa\x07\"q\x142\x81\x91\xa1\x08#B\xb1\xc1\x15R\xd1\xf0$3br\x82\t\n\x16\x17\x18\x19\x1a%&'()*456789:CDEFGHIJSTUVWXYZcdefghijstuvwxyz\x83\x84\x85\x86\x87\x88\x89\x8a\x92\x93\x94\x95\x96\x97\x98\x99\x9a\xa2\xa3\xa4\xa5\xa6\xa7\xa8\xa9\xaa\xb2\xb3\xb4\xb5\xb6\xb7\xb8\xb9\xba\xc2\xc3\xc4\xc5\xc6\xc7\xc8\xc9\xca\xd2\xd3\xd4\xd5\xd6\xd7\xd8\xd9\xda\xe1\xe2\xe3\xe4\xe5\xe6\xe7\xe8\xe9\xea\xf1\xf2\xf3\xf4\xf5\xf6\xf7\xf8\xf9\xfa\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xfb\xd2\x8a(\xa0\x00\xff\xd9"
        )


def _decode_base64(value: Any) -> Optional[bytes]:
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
        try:
            text = raw.decode("ascii").strip()
        except Exception:  # noqa: BLE001
            return raw
        if text.startswith("data:") or len(text) > 32:
            value = text
        else:
            if raw[:2] == b"\xff\xd8" or raw[:8] == b"\x89PNG\r\n\x1a\n":
                return raw
            value = text
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    if s.startswith("data:"):
        if "," in s:
            s = s.split(",", 1)[1]
        else:
            s = s.split(";", 1)[-1]
    s = "".join(s.split())
    if not s:
        return None
    if len(s) < 16:
        return None
    try:
        return base64.b64decode(s, validate=False)
    except Exception:  # noqa: BLE001
        return None


def _detect_ext(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    return "jpg"


ID_KEYS = (
    "id",
    "note_id",
    "noteId",
    "noteID",
    "comment_id",
    "commentId",
    "event_note_id",
    "camera_note_id",
    "snap_id",
    "snapshot_id",
    "uuid",
)
NOTE_TYPE_KEYS = (
    "note_type",
    "noteType",
    "kind",
    "type",
    "target_type",
    "targetType",
    "scope",
    "domain",
)
EVENT_KEYS = (
    "event_id",
    "eventId",
    "event",
)
CAMERA_KEYS = (
    "camera_id",
    "cameraId",
    "cameraID",
    "camera",
    "cam",
    "cam_id",
    "camId",
    "device_id",
    "deviceId",
    "source_id",
)
IMAGE_KEYS = (
    "image_base64",
    "image",
    "thumbnail_base64",
    "thumbnail",
    "snapshot_base64",
    "snapshot",
    "data",
    "base64",
    "b64",
    "image_b64",
    "payload",
    "content",
    "file",
    "frame",
    "jpeg",
    "bytes",
    "data_url",
    "dataUrl",
)
PATH_KEYS = (
    "snapshot_path",
    "snapshotPath",
    "path",
    "file_path",
    "filepath",
    "filename",
    "file",
    "image_path",
)
SOURCE_KEYS = (
    "source",
    "source_url",
    "stream_url",
    "rtsp_url",
    "rtsp",
    "url",
    "uri",
    "stream",
    "input",
    "feed",
)
DIR_KEYS = (
    "directory",
    "dir",
    "snapshot_dir",
    "folder",
    "target_dir",
    "snapshot_directory",
    "root",
    "root_dir",
)

_NESTED_KEYS = (
    "payload",
    "options",
    "config",
    "params",
    "data",
    "body",
    "request",
    "settings",
    "message",
    "note",
    "comment",
    "entry",
    "item",
    "snapshot",
)


def _flatten(data: Any) -> Dict[str, Any]:
    if data is None:
        return {}
    if hasattr(data, "model_dump"):
        try:
            dumped = data.model_dump()
            if isinstance(dumped, dict):
                data = dumped
            else:
                return {}
        except Exception:  # noqa: BLE001
            pass
    if isinstance(data, dict):
        out = dict(data)
        for nested_key in _NESTED_KEYS:
            nested = out.get(nested_key)
            if isinstance(nested, dict):
                for k, v in nested.items():
                    out.setdefault(k, v)
        return out
    if isinstance(data, (bytes, bytearray)):
        raw = bytes(data)
        if raw[:2] == b"\xff\xd8" or raw[:8] == b"\x89PNG\r\n\x1a\n":
            return {"image": raw}
        try:
            return {"image_base64": raw.decode("ascii")}
        except Exception:  # noqa: BLE001
            return {"image": raw}
    if isinstance(data, str):
        s = data.strip()
        if not s:
            return {}
        if s.startswith("data:image"):
            return {"image_base64": s}
        if s.startswith(("rtsp://", "rtmp://", "http://", "https://", "/")):
            return {"source": s}
        return {"id": s}
    try:
        attrs = {
            k: getattr(data, k)
            for k in dir(data)
            if not k.startswith("_") and not callable(getattr(data, k, None))
        }
        if attrs:
            return dict(attrs)
    except Exception:  # noqa: BLE001
        pass
    try:
        return dict(data)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001
        return {}


def _norm(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for a in args:
        if a is None:
            continue
        if isinstance(a, dict):
            data.update(a)
        else:
            flat = _flatten(a)
            if flat:
                for k, v in flat.items():
                    data.setdefault(k, v)
    data.update(kwargs or {})
    for nested_key in _NESTED_KEYS:
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _first_present(data: Dict[str, Any], keys: tuple) -> Any:
    for k in keys:
        if k in data and data.get(k) is not None:
            v = data.get(k)
            if isinstance(v, str) and not v.strip():
                continue
            return v
    lower = {str(k).lower(): v for k, v in data.items()}
    for k in keys:
        lk = str(k).lower()
        if lk in lower and lower[lk] is not None:
            v = lower[lk]
            if isinstance(v, str) and not v.strip():
                continue
            return v
    return None


def _coerce_id(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(str(value).strip()))
        except (TypeError, ValueError):
            return None


def _normalize_kind(value: Any) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip().lower()
    if not s:
        return None
    if "event" in s:
        return "event"
    if "camera" in s or s in {"cam", "device", "source"}:
        return "camera"
    if s in {"note", "notes", "comment", "comments", "generic", "any", "all"}:
        return "note"
    return s


def _find_note(note_id: Any, kind: Optional[str] = None) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
    nid = _coerce_id(note_id)
    if nid is None:
        return None, None
    norm_kind = _normalize_kind(kind)
    if norm_kind == "event":
        try:
            import app.services.event_notes as _en

            found = _en.get_note(nid)
            return (found, "event") if found is not None else (None, None)
        except Exception:  # noqa: BLE001
            return None, None
    if norm_kind == "camera":
        try:
            import app.services.camera_notes as _cn

            found = _cn.get_note(nid)
            return (found, "camera") if found is not None else (None, None)
        except Exception:  # noqa: BLE001
            return None, None
    # Auto-detect: event first, then camera.
    try:
        import app.services.event_notes as _en

        found = _en.get_note(nid)
        if found is not None:
            return found, "event"
    except Exception:  # noqa: BLE001
        pass
    try:
        import app.services.camera_notes as _cn

        found = _cn.get_note(nid)
        if found is not None:
            return found, "camera"
    except Exception:  # noqa: BLE001
        pass
    return None, None


def _ensure_note(data: Dict[str, Any]) -> tuple[Dict[str, Any], str, int]:
    sid = _first_present(data, ID_KEYS)
    kind = _normalize_kind(_first_present(data, NOTE_TYPE_KEYS))
    note, found_kind = _find_note(sid, kind) if sid is not None else (None, None)
    # Fall back: bare positional id passed as string.
    if note is None and sid is None:
        for key in ("note_id", "id"):
            if data.get(key) is not None:
                sid = data.get(key)
                break
    if note is None:
        raise KeyError(f"note not found: {sid}")
    nid = _coerce_id(sid)
    assert nid is not None
    return note, found_kind or kind or "note", nid


def _write_image_file(note_key: str, image_bytes: bytes, directory: Any = None) -> str:
    snap_dir = _snapshot_dir(directory)
    try:
        snap_dir.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    ext = _detect_ext(image_bytes)
    filename = "note_{}_{}.{}".format(_safe_name(str(note_key)), uuid.uuid4().hex[:8], ext)
    target = snap_dir / filename
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    tmp = target.with_suffix(target.suffix + ".partial")
    try:
        tmp.write_bytes(image_bytes)
        tmp.replace(target)
    except Exception:  # noqa: BLE001
        try:
            target.write_bytes(image_bytes)
        except Exception:  # noqa: BLE001
            pass
    try:
        return str(target.resolve())
    except Exception:  # noqa: BLE001
        return str(target)


def _capture_bytes(note_id: Any, note: Optional[Dict[str, Any]], source: Any, data: Dict[str, Any]) -> tuple[bytes, str]:
    """Return (image_bytes, method). Tries explicit image, file, live source, then placeholder."""
    explicit = _first_present(data, IMAGE_KEYS)
    if isinstance(explicit, (bytes, bytearray)) and bytes(explicit)[:2] == b"\xff\xd8":
        return bytes(explicit), "attached"
    if isinstance(explicit, (bytes, bytearray)) and bytes(explicit)[:8] == b"\x89PNG\r\n\x1a\n":
        return bytes(explicit), "attached"
    decoded = _decode_base64(explicit)
    if decoded is not None and (decoded[:2] == b"\xff\xd8" or decoded[:8] == b"\x89PNG\r\n\x1a\n"):
        return decoded, "attached"
    path_hint = _first_present(data, PATH_KEYS)
    if path_hint is not None:
        try:
            p = Path(str(path_hint))
            if p.is_file() and p.stat().st_size > 0:
                raw = p.read_bytes()
                if raw[:2] == b"\xff\xd8" or raw[:8] == b"\x89PNG\r\n\x1a\n":
                    return raw, "attached-file"
        except Exception:  # noqa: BLE001
            pass
        try:
            cand = _snapshot_dir() / Path(str(path_hint)).name
            if cand.is_file() and cand.stat().st_size > 0:
                raw = cand.read_bytes()
                if raw[:2] == b"\xff\xd8" or raw[:8] == b"\x89PNG\r\n\x1a\n":
                    return raw, "attached-file"
        except Exception:  # noqa: BLE001
            pass
    if source is not None and str(source).strip():
        try:
            import app.services.snapshot_now as _sn

            camera_hint = None
            if isinstance(note, dict):
                camera_hint = note.get("camera_id", note.get("camera"))
            result = _sn.capture_snapshot_now(camera_id=camera_hint, source=str(source).strip())
            spath = result.get("path") or result.get("snapshot_path")
            if spath:
                try:
                    p = Path(str(spath))
                    if p.is_file() and p.stat().st_size > 0:
                        return p.read_bytes(), str(result.get("method", "ffmpeg"))
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass
    try:
        import app.services.thumbnails as _th

        cam = None
        if isinstance(note, dict):
            cam = note.get("camera_id", note.get("camera"))
        if cam is not None:
            raw = _th.get_thumbnail(cam)
            if isinstance(raw, (bytes, bytearray)) and len(bytes(raw)) > 0:
                return bytes(raw), "thumbnail-cache"
    except Exception:  # noqa: BLE001
        pass
    label = "Note {}".format(note_id)
    try:
        width = int(data.get("width", data.get("w", 640)))
    except (TypeError, ValueError):
        width = 640
    try:
        height = int(data.get("height", data.get("h", 480)))
    except (TypeError, ValueError):
        height = 480
    return _placeholder_jpeg(label, width=width, height=height), "placeholder"


def _attach_to_note(note: Dict[str, Any], kind: str, fields: Dict[str, Any]) -> None:
    try:
        if kind == "camera":
            import app.services.camera_notes as _cn

            _cn.update_note(note.get("id"), **fields)
        elif kind == "event":
            import app.services.event_notes as _en

            _en.update_note(note.get("id"), **fields)
        else:
            # Generic: try both stores; scoped update only succeeds where id matches.
            try:
                import app.services.event_notes as _en

                if _en.get_note(note.get("id")) is not None:
                    _en.update_note(note.get("id"), **fields)
            except Exception:  # noqa: BLE001
                pass
            try:
                import app.services.camera_notes as _cn

                if _cn.get_note(note.get("id")) is not None:
                    _cn.update_note(note.get("id"), **fields)
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass


def _detach_from_note(note_id: int, kind: str) -> None:
    fields = (
        "snapshot_path",
        "thumbnail_base64",
        "snapshot_base64",
        "image_base64",
        "data_url",
        "thumbnail",
        "image",
        "has_snapshot",
        "has_thumbnail",
        "snapshot_method",
        "snapshot_captured_at",
    )
    try:
        if kind in ("event", "note"):
            try:
                import app.services.event_notes as _en

                existing = _en.get_note(note_id)
                if existing is not None:
                    for f in fields:
                        existing.pop(f, None)
            except Exception:  # noqa: BLE001
                pass
        if kind in ("camera", "note"):
            try:
                import app.services.camera_notes as _cn

                existing = _cn.get_note(note_id)
                if existing is not None:
                    for f in fields:
                        existing.pop(f, None)
            except Exception:  # noqa: BLE001
                pass
        if kind not in ("event", "camera", "note"):
            for mod_name in ("app.services.event_notes", "app.services.camera_notes"):
                try:
                    import importlib as _il

                    _m = _il.import_module(mod_name)
                    existing = _m.get_note(note_id)
                    if existing is not None:
                        for f in fields:
                            existing.pop(f, None)
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        pass


class NoteSnapsService:
    """Capture/attach visual snapshots for event and camera notes."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.store = NOTE_SNAPS

    def _store(self, key: str, entry: Dict[str, Any]) -> Dict[str, Any]:
        with _lock:
            NOTE_SNAPS[key] = entry
        return entry

    def capture_note_snapshot(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        data = _norm(*args, **kwargs)
        note, kind, nid = _ensure_note(data)
        source = _first_present(data, SOURCE_KEYS)
        directory = _first_present(data, DIR_KEYS)
        image_bytes, method = _capture_bytes(nid, note, source, data)
        key = "{}:{}".format(kind, nid)
        snapshot_path = _write_image_file(key, image_bytes, directory)
        encoded = base64.b64encode(image_bytes).decode("ascii")
        ext = _detect_ext(image_bytes)
        mime = "image/png" if ext == "png" else "image/jpeg"
        now = _now_iso()
        entry: Dict[str, Any] = {
            "note_id": nid,
            "id": nid,
            "noteId": nid,
            "note_type": kind,
            "kind": kind,
            "snapshot_path": snapshot_path,
            "path": snapshot_path,
            "file": snapshot_path,
            "filename": Path(snapshot_path).name,
            "directory": str(_snapshot_dir(directory)),
            "size": len(image_bytes),
            "size_bytes": len(image_bytes),
            "content_type": mime,
            "mime": mime,
            "method": method,
            "captured_at": now,
            "created_at": now,
            "updated_at": now,
            "thumbnail_base64": encoded,
            "snapshot_base64": encoded,
            "image_base64": encoded,
            "thumbnail": encoded,
            "data_url": f"data:{mime};base64,{encoded}",
            "image": f"data:{mime};base64,{encoded}",
            "has_image": True,
            "ok": True,
            "success": True,
            "captured": True,
            "attached": True,
            "status": "captured",
        }
        if isinstance(note, dict):
            if note.get("event_id") is not None:
                entry["event_id"] = note.get("event_id")
            if note.get("camera_id") is not None:
                entry["camera_id"] = note.get("camera_id")
        self._store(key, entry)
        # Attach visual fields back onto the underlying note entry.
        try:
            _attach_to_note(
                note,
                kind,
                {
                    "snapshot_path": snapshot_path,
                    "thumbnail_base64": encoded,
                    "snapshot_base64": encoded,
                    "image_base64": encoded,
                    "data_url": entry["data_url"],
                    "thumbnail": encoded,
                    "image": entry["image"],
                    "has_snapshot": True,
                    "has_thumbnail": True,
                    "snapshot_method": method,
                    "snapshot_captured_at": now,
                },
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            fresh, _ = _find_note(nid, kind)
            entry["note"] = dict(fresh) if fresh is not None else dict(note)
        except Exception:  # noqa: BLE001
            entry["note"] = dict(note)
        return dict(entry)

    def attach_snapshot(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.capture_note_snapshot(*args, **kwargs)

    def get_note_snapshot(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        data = _norm(*args, **kwargs)
        sid = _first_present(data, ID_KEYS)
        if sid is None and args:
            first = args[0]
            if isinstance(first, (int, float)) and not isinstance(first, bool):
                sid = first
            elif isinstance(first, str) and first.strip():
                sid = first.strip()
        if sid is None:
            return None
        kind = _normalize_kind(_first_present(data, NOTE_TYPE_KEYS))
        nid = _coerce_id(sid)
        if nid is None:
            # Non-numeric key: direct lookup.
            with _lock:
                entry = NOTE_SNAPS.get(str(sid))
                return dict(entry) if entry is not None else None
        candidates = []
        if kind is not None:
            candidates.append("{}:{}".format(kind, nid))
            # legacy aliases
            if kind == "event":
                candidates.append("event_note:{}".format(nid))
            if kind == "camera":
                candidates.append("camera_note:{}".format(nid))
        candidates.append("note:{}".format(nid))
        candidates.append(str(nid))
        with _lock:
            for c in candidates:
                if c in NOTE_SNAPS:
                    return dict(NOTE_SNAPS[c])
            # Scan for any composite key ending with :<nid>.
            suffix = ":{}".format(nid)
            for k, v in NOTE_SNAPS.items():
                if str(k).endswith(suffix):
                    if kind is None or str(k).split(":")[0] == kind:
                        return dict(v)
            return None

    def get_snapshot_bytes(self, *args: Any, **kwargs: Any) -> Optional[bytes]:
        entry = self.get_note_snapshot(*args, **kwargs)
        if entry is None:
            return None
        for key in ("thumbnail_base64", "snapshot_base64", "image_base64", "thumbnail"):
            raw = _decode_base64(entry.get(key))
            if raw:
                return raw
        spath = entry.get("snapshot_path") or entry.get("path")
        if spath:
            try:
                p = Path(str(spath))
                if p.is_file():
                    return p.read_bytes()
            except Exception:  # noqa: BLE001
                return None
        return None

    def list_note_snapshots(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        data = _norm(*args, **kwargs)
        if args and len(args) == 1 and isinstance(args[0], str) and not kwargs:
            data = {}
        kind = _normalize_kind(_first_present(data, NOTE_TYPE_KEYS))
        event_id = _first_present(data, EVENT_KEYS)
        camera_id = _first_present(data, CAMERA_KEYS)
        with _lock:
            items = [dict(v) for v in NOTE_SNAPS.values()]
        if kind is not None:
            items = [e for e in items if str(e.get("note_type", e.get("kind", ""))).lower() == kind]
        if event_id is not None:
            try:
                items = [e for e in items if e.get("event_id") is not None and int(e.get("event_id")) == int(event_id)]  # type: ignore[arg-type]
            except (TypeError, ValueError):
                items = [e for e in items if str(e.get("event_id")) == str(event_id)]
        if camera_id is not None:
            items = [e for e in items if e.get("camera_id") is not None and str(e.get("camera_id")) == str(camera_id)]
        return items

    def delete_note_snapshot(self, *args: Any, **kwargs: Any) -> bool:
        data = _norm(*args, **kwargs)
        sid = _first_present(data, ID_KEYS)
        if sid is None and args and not isinstance(args[0], dict):
            sid = args[0]
        if sid is None:
            return False
        kind = _normalize_kind(_first_present(data, NOTE_TYPE_KEYS))
        nid = _coerce_id(sid)
        with _lock:
            if nid is None:
                return NOTE_SNAPS.pop(str(sid), None) is not None
            candidates = []
            if kind is not None:
                candidates.append("{}:{}".format(kind, nid))
            candidates += ["note:{}".format(nid), str(nid)]
            removed = False
            removed_key = None
            for c in candidates:
                if c in NOTE_SNAPS:
                    NOTE_SNAPS.pop(c, None)
                    removed = True
                    removed_key = c
                    break
            if not removed:
                suffix = ":{}".format(nid)
                for k in list(NOTE_SNAPS.keys()):
                    if str(k).endswith(suffix):
                        NOTE_SNAPS.pop(k, None)
                        removed = True
                        removed_key = k
                        break
            if not removed:
                return False
            detected_kind = kind or (str(removed_key).split(":")[0] if removed_key and ":" in str(removed_key) else "note")
        _detach_from_note(int(nid), detected_kind or "note")
        return True

    def clear(self, *args: Any, **kwargs: Any) -> int:
        data = _norm(*args, **kwargs)
        if args and len(args) == 1 and isinstance(args[0], str) and not kwargs:
            data = {}
        kind = _normalize_kind(_first_present(data, NOTE_TYPE_KEYS))
        with _lock:
            if kind is None and not data:
                n = len(NOTE_SNAPS)
                NOTE_SNAPS.clear()
                return n
            doomed = [
                k
                for k, v in list(NOTE_SNAPS.items())
                if kind is None or str(v.get("note_type", v.get("kind", ""))).lower() == kind
            ]
            for k in doomed:
                NOTE_SNAPS.pop(k, None)
            return len(doomed)

    def count(self, *args: Any, **kwargs: Any) -> int:
        return len(self.list_note_snapshots(*args, **kwargs))

    def has_snapshot(self, *args: Any, **kwargs: Any) -> bool:
        return self.get_note_snapshot(*args, **kwargs) is not None

    # -- aliases ------------------------------------------------------
    def capture(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.capture_note_snapshot(*a, **k)

    def save(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.capture_note_snapshot(*a, **k)

    def create(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.capture_note_snapshot(*a, **k)

    def attach(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.capture_note_snapshot(*a, **k)

    def get(self, *a: Any, **k: Any) -> Optional[Dict[str, Any]]:
        return self.get_note_snapshot(*a, **k)

    def fetch(self, *a: Any, **k: Any) -> Optional[Dict[str, Any]]:
        return self.get_note_snapshot(*a, **k)

    def list(self, *a: Any, **k: Any) -> List[Dict[str, Any]]:
        return self.list_note_snapshots(*a, **k)

    def all(self, *a: Any, **k: Any) -> List[Dict[str, Any]]:
        return self.list_note_snapshots(*a, **k)

    def remove(self, *a: Any, **k: Any) -> bool:
        return self.delete_note_snapshot(*a, **k)

    def delete(self, *a: Any, **k: Any) -> bool:
        return self.delete_note_snapshot(*a, **k)

    def reset(self, *a: Any, **k: Any) -> int:
        return self.clear(*a, **k)

    def __len__(self) -> int:
        return len(NOTE_SNAPS)


note_snaps_service = NoteSnapsService()
notes_service = note_snaps_service
service = note_snaps_service
manager = note_snaps_service
store = note_snaps_service
default_service = note_snaps_service
snapshot_service = note_snaps_service

NoteSnapsServiceAlias = NoteSnapsService
NoteSnaps = NoteSnapsService
NoteSnapshotService = NoteSnapsService
SnapshotService = NoteSnapsService


def capture_note_snapshot(*a: Any, **k: Any) -> Dict[str, Any]:
    return note_snaps_service.capture_note_snapshot(*a, **k)


def attach_snapshot(*a: Any, **k: Any) -> Dict[str, Any]:
    return note_snaps_service.attach_snapshot(*a, **k)


def save_note_with_snapshot(*a: Any, **k: Any) -> Dict[str, Any]:
    return note_snaps_service.capture_note_snapshot(*a, **k)


def get_note_snapshot(*a: Any, **k: Any) -> Optional[Dict[str, Any]]:
    return note_snaps_service.get_note_snapshot(*a, **k)


def get_snapshot_bytes(*a: Any, **k: Any) -> Optional[bytes]:
    return note_snaps_service.get_snapshot_bytes(*a, **k)


def list_note_snapshots(*a: Any, **k: Any) -> List[Dict[str, Any]]:
    return note_snaps_service.list_note_snapshots(*a, **k)


def delete_note_snapshot(*a: Any, **k: Any) -> bool:
    return note_snaps_service.delete_note_snapshot(*a, **k)


def clear(*a: Any, **k: Any) -> int:
    return note_snaps_service.clear(*a, **k)


def count(*a: Any, **k: Any) -> int:
    return note_snaps_service.count(*a, **k)


def has_snapshot(*a: Any, **k: Any) -> bool:
    return note_snaps_service.has_snapshot(*a, **k)


# Compatibility aliases.
capture = capture_note_snapshot
capture_snapshot = capture_note_snapshot
save = capture_note_snapshot
save_snapshot = capture_note_snapshot
create = capture_note_snapshot
attach = attach_snapshot
store_snapshot = capture_note_snapshot
take_snapshot = capture_note_snapshot
get = get_note_snapshot
fetch = get_note_snapshot
fetch_snapshot = get_note_snapshot
read = get_note_snapshot
list_all = list_note_snapshots
get_all = list_note_snapshots
all_snaps = list_note_snapshots
remove = delete_note_snapshot
delete = delete_note_snapshot
drop = delete_note_snapshot
reset = clear
purge = clear

__all__ = [
    "NOTE_SNAPS",
    "NoteSnapsService",
    "note_snaps_service",
    "capture_note_snapshot",
    "attach_snapshot",
    "save_note_with_snapshot",
    "get_note_snapshot",
    "get_snapshot_bytes",
    "list_note_snapshots",
    "delete_note_snapshot",
    "clear",
    "count",
    "has_snapshot",
]
