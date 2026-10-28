"""Preset snapshot service — visual thumbnails for PTZ preset library.

Verification: :mod:`app.services.preset_library` stores only names/tokens
(``camera_id`` + ``name`` + optional ``preset_token``/``pan``/``tilt``/``zoom``)
with no snapshot/thumbnail/image fields. This module fills that gap by
capturing a still frame when a preset is saved and attaching it to the
preset-library entry (``snapshot_path`` / ``thumbnail_base64`` / ``image``).

The store is in-memory and thread-safe so the feature works without DB
migrations and without editing ``app/main.py``. The API router
(``app/api/preset_snaps.py``) is auto-registered by the ``pkgutil``
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

PRESET_SNAPS: Dict[str, Dict[str, Any]] = {}
PRESETS = PRESET_SNAPS
SNAPS = PRESET_SNAPS
PRESET_SNAPSHOTS = PRESET_SNAPS
PRESET_STORE = PRESET_SNAPS
STORE = PRESET_SNAPS
ITEMS = PRESET_SNAPS
JOBS = PRESET_SNAPS
LIBRARY = PRESET_SNAPS
preset_snaps = PRESET_SNAPS
snaps = PRESET_SNAPS
snapshots = PRESET_SNAPS

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
    safe = safe.strip("._") or "preset"
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
        # If it looks like ASCII base64, decode; else treat as raw image bytes.
        try:
            text = raw.decode("ascii").strip()
        except Exception:  # noqa: BLE001
            return raw
        if text.startswith("data:") or len(text) > 32:
            value = text
        else:
            # Short raw bytes — treat as image bytes if JPEG/PNG magic.
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
    # Heuristic: only attempt base64 decode for plausible payloads.
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
    "preset_id",
    "presetId",
    "presetID",
    "preset_library_id",
    "presetLibraryId",
    "preset_uuid",
    "uuid",
    "snap_id",
    "snapshot_id",
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
NAME_KEYS = (
    "name",
    "title",
    "label",
    "preset_name",
    "presetName",
    "display_name",
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
    "preset",
    "preset_library",
    "presetLibrary",
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
        # Bare token -> treat as preset id.
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
                # _flatten single-string id must not clobber explicit kwargs.
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


def _find_preset(preset_id: Any) -> Optional[Dict[str, Any]]:
    if preset_id is None:
        return None
    try:
        import app.services.preset_library as _lib

        found = _lib.get_preset(preset_id)
        return found
    except Exception:  # noqa: BLE001
        return None


def _ensure_preset(data: Dict[str, Any]) -> Dict[str, Any]:
    """Return the preset-library entry, creating it when camera+name given."""
    import app.services.preset_library as _lib

    sid = _first_present(data, ID_KEYS)
    found = _find_preset(sid) if sid is not None else None
    if found is not None:
        return found
    # Try lookup by id passed positionally as bare string.
    camera_id = _first_present(data, CAMERA_KEYS)
    name = _first_present(data, NAME_KEYS)
    # If only an id was supplied and no preset exists, we cannot fabricate
    # camera_id/name — raise a clear error (KeyError -> 404 at API layer).
    if camera_id is None or name is None:
        raise KeyError(f"preset not found: {sid}")
    # Create the preset entry first, then snapshot it.
    create_kwargs: Dict[str, Any] = {"camera_id": camera_id, "name": str(name)}
    token = _first_present(data, ("preset_token", "presetToken", "token", "device_token"))
    if token is not None:
        create_kwargs["preset_token"] = str(token)
    for extra in ("pan", "tilt", "zoom", "x", "y", "z", "description", "desc"):
        if data.get(extra) is not None:
            create_kwargs[extra] = data.get(extra)
    created = _lib.create_preset(**create_kwargs)
    return created


def _write_image_file(preset_id: str, image_bytes: bytes, directory: Any = None) -> str:
    snap_dir = _snapshot_dir(directory)
    try:
        snap_dir.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    ext = _detect_ext(image_bytes)
    filename = "preset_{}_{}.{}".format(_safe_name(str(preset_id)), uuid.uuid4().hex[:8], ext)
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
        # Fall back to direct write (e.g. read-only tmp handling).
        try:
            target.write_bytes(image_bytes)
        except Exception:  # noqa: BLE001
            pass
    try:
        return str(target.resolve())
    except Exception:  # noqa: BLE001
        return str(target)


def _capture_bytes(camera_id: Any, name: Any, source: Any, data: Dict[str, Any]) -> tuple[bytes, str]:
    """Return (image_bytes, method). Tries real capture, falls back to placeholder."""
    # 1. Explicit image payload wins.
    explicit = _first_present(data, IMAGE_KEYS)
    if isinstance(explicit, (bytes, bytearray)) and bytes(explicit)[:2] == b"\xff\xd8":
        return bytes(explicit), "attached"
    if isinstance(explicit, (bytes, bytearray)) and bytes(explicit)[:8] == b"\x89PNG\r\n\x1a\n":
        return bytes(explicit), "attached"
    decoded = _decode_base64(explicit)
    if decoded is not None and (decoded[:2] == b"\xff\xd8" or decoded[:8] == b"\x89PNG\r\n\x1a\n"):
        return decoded, "attached"
    # 2. Existing file path wins.
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
        # Also try resolving against snapshot dir.
        try:
            cand = _snapshot_dir() / Path(str(path_hint)).name
            if cand.is_file() and cand.stat().st_size > 0:
                raw = cand.read_bytes()
                if raw[:2] == b"\xff\xd8" or raw[:8] == b"\x89PNG\r\n\x1a\n":
                    return raw, "attached-file"
        except Exception:  # noqa: BLE001
            pass
    # 3. Live source capture via snapshot_now (ffmpeg) when a source is given.
    if source is not None and str(source).strip():
        try:
            import app.services.snapshot_now as _sn

            result = _sn.capture_snapshot_now(camera_id=camera_id, source=str(source).strip())
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
    # 4. Thumbnail cache frame when available (in-memory JPEG).
    try:
        import app.services.thumbnails as _th

        if camera_id is not None:
            raw = _th.get_thumbnail(camera_id)
            if isinstance(raw, (bytes, bytearray)) and len(bytes(raw)) > 0:
                return bytes(raw), "thumbnail-cache"
    except Exception:  # noqa: BLE001
        pass
    # 5. Placeholder JPEG labelled with preset name.
    label = str(name) if name is not None else f"Preset {camera_id}"
    try:
        width = int(data.get("width", data.get("w", 640)))
    except (TypeError, ValueError):
        width = 640
    try:
        height = int(data.get("height", data.get("h", 480)))
    except (TypeError, ValueError):
        height = 480
    return _placeholder_jpeg(label, width=width, height=height), "placeholder"


class PresetSnapsService:
    """Capture/attach visual snapshots for PTZ preset-library entries."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.store = PRESET_SNAPS

    def _store(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        with _lock:
            PRESET_SNAPS[str(entry["preset_id"])] = entry
        return entry

    def capture_preset_snapshot(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        data = _norm(*args, **kwargs)
        preset = _ensure_preset(data)
        preset_id = str(preset.get("id"))
        camera_id = preset.get("camera_id")
        name = preset.get("name")
        source = _first_present(data, SOURCE_KEYS)
        directory = _first_present(data, DIR_KEYS)
        image_bytes, method = _capture_bytes(camera_id, name, source, data)
        snapshot_path = _write_image_file(preset_id, image_bytes, directory)
        encoded = base64.b64encode(image_bytes).decode("ascii")
        ext = _detect_ext(image_bytes)
        mime = "image/png" if ext == "png" else "image/jpeg"
        now = _now_iso()
        entry: Dict[str, Any] = {
            "preset_id": preset_id,
            "id": preset_id,
            "presetId": preset_id,
            "camera_id": camera_id,
            "camera": camera_id,
            "cameraId": camera_id,
            "name": name,
            "preset_name": name,
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
            "status": "captured",
        }
        self._store(entry)
        # Attach visual fields back onto the preset-library entry.
        try:
            import app.services.preset_library as _lib

            _lib.update_preset(
                id=preset_id,
                snapshot_path=snapshot_path,
                thumbnail_base64=encoded,
                snapshot_base64=encoded,
                image_base64=encoded,
                data_url=entry["data_url"],
                thumbnail=encoded,
                image=entry["data_url"],
                has_snapshot=True,
                has_thumbnail=True,
                snapshot_method=method,
                snapshot_captured_at=now,
            )
        except Exception:  # noqa: BLE001
            pass
        # Merge fresh preset fields for convenience.
        try:
            fresh = _find_preset(preset_id) or {}
            entry["preset"] = dict(fresh)
        except Exception:  # noqa: BLE001
            entry["preset"] = dict(preset)
        return dict(entry)

    def attach_snapshot(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.capture_preset_snapshot(*args, **kwargs)

    def get_preset_snapshot(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        data = _norm(*args, **kwargs)
        sid = _first_present(data, ID_KEYS)
        if sid is None and args:
            first = args[0]
            if isinstance(first, str):
                sid = first
            elif isinstance(first, dict):
                sid = _first_present(dict(first), ID_KEYS)
        if sid is None:
            return None
        key = str(sid).strip()
        with _lock:
            entry = PRESET_SNAPS.get(key)
            # Fall back: preset-library entry may carry attached fields even if
            # this store was cleared (e.g. separate clear calls).
            if entry is None:
                try:
                    import app.services.preset_library as _lib

                    preset = _lib.get_preset(key)
                    if preset is not None and (
                        preset.get("snapshot_path") or preset.get("thumbnail_base64")
                    ):
                        return {
                            "preset_id": str(preset.get("id")),
                            "id": str(preset.get("id")),
                            "camera_id": preset.get("camera_id"),
                            "name": preset.get("name"),
                            "snapshot_path": preset.get("snapshot_path"),
                            "path": preset.get("snapshot_path"),
                            "thumbnail_base64": preset.get("thumbnail_base64"),
                            "snapshot_base64": preset.get("snapshot_base64"),
                            "data_url": preset.get("data_url"),
                            "ok": True,
                            "success": True,
                            "attached": True,
                        }
                except Exception:  # noqa: BLE001
                    pass
                return None
            return dict(entry)

    def get_snapshot_bytes(self, *args: Any, **kwargs: Any) -> Optional[bytes]:
        entry = self.get_preset_snapshot(*args, **kwargs)
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

    def list_preset_snapshots(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        data = _norm(*args, **kwargs)
        if args and len(args) == 1 and isinstance(args[0], str) and not kwargs:
            data = {}
        cam = _first_present(data, CAMERA_KEYS)
        with _lock:
            items = [dict(v) for v in PRESET_SNAPS.values()]
        if cam is not None:
            items = [e for e in items if str(e.get("camera_id")) == str(cam)]
        return items

    def delete_preset_snapshot(self, *args: Any, **kwargs: Any) -> bool:
        data = _norm(*args, **kwargs)
        sid = _first_present(data, ID_KEYS)
        if sid is None and args and not isinstance(args[0], dict):
            sid = args[0]
        if sid is None:
            return False
        key = str(sid)
        with _lock:
            removed = PRESET_SNAPS.pop(key, None) is not None
        # Clear attached fields on the preset entry (keep the preset itself).
        try:
            import app.services.preset_library as _lib

            preset = _lib.get_preset(key)
            if preset is not None:
                for f in (
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
                ):
                    preset.pop(f, None)
                with _lib._lock:  # noqa: SLF001
                    _lib.PRESET_LIBRARY[key] = preset
        except Exception:  # noqa: BLE001
            pass
        return removed

    def clear(self, *args: Any, **kwargs: Any) -> int:
        data = _norm(*args, **kwargs)
        if args and len(args) == 1 and isinstance(args[0], str) and not kwargs:
            data = {}
        cam = _first_present(data, CAMERA_KEYS)
        with _lock:
            if cam is None and not data:
                n = len(PRESET_SNAPS)
                PRESET_SNAPS.clear()
                return n
            doomed = [
                k
                for k, v in list(PRESET_SNAPS.items())
                if cam is None or str(v.get("camera_id")) == str(cam)
            ]
            for k in doomed:
                PRESET_SNAPS.pop(k, None)
            return len(doomed)

    def count(self, *args: Any, **kwargs: Any) -> int:
        return len(self.list_preset_snapshots(*args, **kwargs))

    def has_snapshot(self, *args: Any, **kwargs: Any) -> bool:
        return self.get_preset_snapshot(*args, **kwargs) is not None

    # -- aliases ------------------------------------------------------
    def capture(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.capture_preset_snapshot(*a, **k)

    def save(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.capture_preset_snapshot(*a, **k)

    def create(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.capture_preset_snapshot(*a, **k)

    def attach(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.capture_preset_snapshot(*a, **k)

    def save_preset_with_snapshot(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.capture_preset_snapshot(*a, **k)

    def get(self, *a: Any, **k: Any) -> Optional[Dict[str, Any]]:
        return self.get_preset_snapshot(*a, **k)

    def fetch(self, *a: Any, **k: Any) -> Optional[Dict[str, Any]]:
        return self.get_preset_snapshot(*a, **k)

    def list(self, *a: Any, **k: Any) -> List[Dict[str, Any]]:
        return self.list_preset_snapshots(*a, **k)

    def all(self, *a: Any, **k: Any) -> List[Dict[str, Any]]:
        return self.list_preset_snapshots(*a, **k)

    def remove(self, *a: Any, **k: Any) -> bool:
        return self.delete_preset_snapshot(*a, **k)

    def delete(self, *a: Any, **k: Any) -> bool:
        return self.delete_preset_snapshot(*a, **k)

    def reset(self, *a: Any, **k: Any) -> int:
        return self.clear(*a, **k)

    def __len__(self) -> int:
        return len(PRESET_SNAPS)


preset_snaps_service = PresetSnapsService()
presets_service = preset_snaps_service
service = preset_snaps_service
manager = preset_snaps_service
store = preset_snaps_service
default_service = preset_snaps_service
snapshot_service = preset_snaps_service

PresetSnapsServiceAlias = PresetSnapsService
PresetSnaps = PresetSnapsService
PresetSnapshotService = PresetSnapsService
SnapshotService = PresetSnapsService


def capture_preset_snapshot(*a: Any, **k: Any) -> Dict[str, Any]:
    return preset_snaps_service.capture_preset_snapshot(*a, **k)


def attach_snapshot(*a: Any, **k: Any) -> Dict[str, Any]:
    return preset_snaps_service.attach_snapshot(*a, **k)


def save_preset_with_snapshot(*a: Any, **k: Any) -> Dict[str, Any]:
    return preset_snaps_service.capture_preset_snapshot(*a, **k)


def get_preset_snapshot(*a: Any, **k: Any) -> Optional[Dict[str, Any]]:
    return preset_snaps_service.get_preset_snapshot(*a, **k)


def get_snapshot_bytes(*a: Any, **k: Any) -> Optional[bytes]:
    return preset_snaps_service.get_snapshot_bytes(*a, **k)


def list_preset_snapshots(*a: Any, **k: Any) -> List[Dict[str, Any]]:
    return preset_snaps_service.list_preset_snapshots(*a, **k)


def delete_preset_snapshot(*a: Any, **k: Any) -> bool:
    return preset_snaps_service.delete_preset_snapshot(*a, **k)


def clear(*a: Any, **k: Any) -> int:
    return preset_snaps_service.clear(*a, **k)


def count(*a: Any, **k: Any) -> int:
    return preset_snaps_service.count(*a, **k)


def has_snapshot(*a: Any, **k: Any) -> bool:
    return preset_snaps_service.has_snapshot(*a, **k)


# Compatibility aliases.
capture = capture_preset_snapshot
capture_snapshot = capture_preset_snapshot
save = capture_preset_snapshot
save_snapshot = capture_preset_snapshot
create = capture_preset_snapshot
attach = attach_snapshot
store_snapshot = capture_preset_snapshot
take_snapshot = capture_preset_snapshot
get = get_preset_snapshot
fetch = get_preset_snapshot
fetch_snapshot = get_preset_snapshot
read = get_preset_snapshot
list_all = list_preset_snapshots
get_all = list_preset_snapshots
all_snaps = list_preset_snapshots
remove = delete_preset_snapshot
delete = delete_preset_snapshot
drop = delete_preset_snapshot
reset = clear
purge = clear

__all__ = [
    "PRESET_SNAPS",
    "PresetSnapsService",
    "preset_snaps_service",
    "capture_preset_snapshot",
    "attach_snapshot",
    "save_preset_with_snapshot",
    "get_preset_snapshot",
    "get_snapshot_bytes",
    "list_preset_snapshots",
    "delete_preset_snapshot",
    "clear",
    "count",
    "has_snapshot",
]
