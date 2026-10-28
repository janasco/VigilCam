"""Stream thumbnail cache service for VigilCam.

In-memory per-camera JPEG thumbnail cache with TTL, used by live grids
and stream previews to avoid re-encoding a frame on every poll.
"""

from __future__ import annotations

import io
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

# Tunables.
DEFAULT_TTL_SECONDS = 10.0
MAX_CACHE_ENTRIES = 256
DEFAULT_WIDTH = 320
DEFAULT_HEIGHT = 240
MIN_DIMENSION = 16
MAX_DIMENSION = 1280

CONTENT_TYPE_JPEG = "image/jpeg"

_LOCK = threading.Lock()
# key (str) -> entry dict {data, content_type, updated_at, hits, width, height}
_CACHE: Dict[str, Dict[str, Any]] = {}


def _now() -> float:
    return time.time()


def _coerce_key(camera_id: Any) -> str:
    if camera_id is None:
        return "0"
    if isinstance(camera_id, bool):
        return str(int(camera_id))
    if hasattr(camera_id, "id") and not isinstance(camera_id, (int, str, float)):
        try:
            camera_id = getattr(camera_id, "id")
        except Exception:  # noqa: BLE001
            pass
    s = str(camera_id).strip()
    if not s:
        return "0"
    # Strip image suffixes used by thumbnail URLs (/1.jpg -> 1).
    low = s.lower()
    for suffix in (".jpg", ".jpeg", ".png"):
        if low.endswith(suffix):
            s = s[: -len(suffix)]
            break
    s = s.strip() or "0"
    if len(s) > 64:
        s = s[:64]
    return s


def _coerce_ttl(ttl: Any = None, **kwargs: Any) -> float:
    for alias in ("ttl_seconds", "ttl_secs", "ttl_secs_", "max_age", "maxage",
                  "cache_ttl", "cache_ttl_seconds", "age", "expires_in"):
        if ttl is None and kwargs.get(alias) is not None:
            ttl = kwargs.get(alias)
    if ttl is None:
        return float(DEFAULT_TTL_SECONDS)
    try:
        return float(ttl)
    except (TypeError, ValueError):
        return float(DEFAULT_TTL_SECONDS)


def _coerce_dims(width: Any = None, height: Any = None, **kwargs: Any) -> Tuple[int, int]:
    if width is None:
        for alias in ("w", "cols", "width_px", "thumbnail_width"):
            if kwargs.get(alias) is not None:
                width = kwargs.get(alias)
                break
    if height is None:
        for alias in ("h", "rows", "height_px", "thumbnail_height"):
            if kwargs.get(alias) is not None:
                height = kwargs.get(alias)
                break
    try:
        w = int(width) if width is not None else DEFAULT_WIDTH
    except (TypeError, ValueError):
        w = DEFAULT_WIDTH
    try:
        h = int(height) if height is not None else DEFAULT_HEIGHT
    except (TypeError, ValueError):
        h = DEFAULT_HEIGHT
    w = max(MIN_DIMENSION, min(MAX_DIMENSION, w))
    h = max(MIN_DIMENSION, min(MAX_DIMENSION, h))
    return w, h


def _placeholder_jpeg(label: str = "VigilCam", width: int = DEFAULT_WIDTH,
                      height: int = DEFAULT_HEIGHT) -> bytes:
    w, h = _coerce_dims(width, height)
    try:
        from PIL import Image, ImageDraw  # type: ignore

        img = Image.new("RGB", (w, h), color=(20, 20, 30))
        draw = ImageDraw.Draw(img)
        draw.text((10, max(10, h // 2 - 10)), str(label)[:48], fill=(255, 255, 255))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=70)
        return buf.getvalue()
    except Exception:  # noqa: BLE001
        # Minimal 1x1 JPEG fallback if PIL is unavailable.
        return (
            b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
            b"\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00"
            b"\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b"
            b"\xff\xc4\x00\xb5\x10\x00\x02\x01\x03\x03\x02\x04\x03\x05\x05\x04\x04\x00\x00\x01}\x01\x02\x03\x00\x04\x11\x05\x12!1A\x06\x13Qa\x07\"q\x142\x81\x91\xa1\x08#B\xb1\xc1\x15R\xd1\xf0$3br\x82\t\n\x16\x17\x18\x19\x1a%&'()*456789:CDEFGHIJSTUVWXYZcdefghijstuvwxyz\x83\x84\x85\x86\x87\x88\x89\x8a\x92\x93\x94\x95\x96\x97\x98\x99\x9a\xa2\xa3\xa4\xa5\xa6\xa7\xa8\xa9\xaa\xb2\xb3\xb4\xb5\xb6\xb7\xb8\xb9\xba\xc2\xc3\xc4\xc5\xc6\xc7\xc8\xc9\xca\xd2\xd3\xd4\xd5\xd6\xd7\xd8\xd9\xda\xe1\xe2\xe3\xe4\xe5\xe6\xe7\xe8\xe9\xea\xf1\xf2\xf3\xf4\xf5\xf6\xf7\xf8\xf9\xfa\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xfb\xd2\x8a(\xa0\x00\xff\xd9"
        )


def _capture_frame(camera_id: Any, width: int = DEFAULT_WIDTH,
                   height: int = DEFAULT_HEIGHT) -> bytes:
    """Produce a fresh thumbnail frame for a camera.

    Currently renders a placeholder labelled with the camera key. Kept as a
    separate hook so a real frame-grabber (RTSP/shared-memory) can replace
    it without changing cache semantics.
    """
    key = _coerce_key(camera_id)
    w, h = _coerce_dims(width, height)
    return _placeholder_jpeg("Cam {}".format(key), width=w, height=h)


def _is_fresh(entry: Dict[str, Any], now: float, ttl: float) -> bool:
    try:
        age = float(now) - float(entry.get("updated_at", 0.0))
    except (TypeError, ValueError):
        return False
    try:
        ttl_f = float(ttl)
    except (TypeError, ValueError):
        ttl_f = float(DEFAULT_TTL_SECONDS)
    if ttl_f <= 0:
        return False
    return age < ttl_f


def _evict_if_needed() -> None:
    if len(_CACHE) <= MAX_CACHE_ENTRIES:
        return
    # Evict oldest entries first.
    ordered = sorted(_CACHE.items(), key=lambda kv: float(kv[1].get("updated_at", 0.0)))
    overflow = len(_CACHE) - MAX_CACHE_ENTRIES
    for k, _ in ordered[:overflow]:
        _CACHE.pop(k, None)


def _entry_to_info(key: str, entry: Dict[str, Any], ttl: float | None = None) -> Dict[str, Any]:
    now = _now()
    data = entry.get("data", b"")
    ttl_f = float(ttl) if ttl is not None else float(DEFAULT_TTL_SECONDS)
    try:
        age = float(now) - float(entry.get("updated_at", 0.0))
    except (TypeError, ValueError):
        age = 0.0
    return {
        "camera_id": key,
        "id": key,
        "size": len(data) if isinstance(data, (bytes, bytearray)) else 0,
        "size_bytes": len(data) if isinstance(data, (bytes, bytearray)) else 0,
        "content_type": entry.get("content_type", CONTENT_TYPE_JPEG),
        "updated_at": float(entry.get("updated_at", 0.0)),
        "age_seconds": age,
        "ttl_seconds": ttl_f,
        "fresh": _is_fresh(entry, now, ttl_f),
        "hits": int(entry.get("hits", 0)),
        "width": entry.get("width", DEFAULT_WIDTH),
        "height": entry.get("height", DEFAULT_HEIGHT),
    }


def get_thumbnail(camera_id: Any = None, *args: Any, ttl: Any = None,
                  refresh: bool = False, width: Any = None,
                  height: Any = None, **kwargs: Any) -> bytes:
    """Return cached thumbnail bytes, generating on miss/stale/refresh."""
    data, _meta = get_thumbnail_with_meta(
        camera_id if camera_id is not None else kwargs.get("camera_id", "0"),
        *args, ttl=ttl, refresh=refresh, width=width, height=height, **kwargs,
    )
    return data


def get_thumbnail_with_meta(camera_id: Any = None, *args: Any, ttl: Any = None,
                            refresh: bool = False, width: Any = None,
                            height: Any = None, **kwargs: Any) -> Tuple[bytes, Dict[str, Any]]:
    if camera_id is None:
        for alias in ("camera", "cam", "cam_id", "id", "cameraId"):
            if kwargs.get(alias) is not None:
                camera_id = kwargs.get(alias)
                break
    if args and camera_id in (None, "0", 0):
        camera_id = args[0]
    key = _coerce_key(camera_id if camera_id is not None else "0")
    ttl_f = _coerce_ttl(ttl, **kwargs)
    if refresh is False:
        for alias in ("force", "force_refresh", "no_cache", "nocache",
                      "fresh", "reload", "regenerate"):
            if kwargs.get(alias) is not None:
                refresh = bool(kwargs.get(alias))
                break
    w, h = _coerce_dims(width, height, **kwargs)
    now = _now()
    with _LOCK:
        entry = _CACHE.get(key)
        if entry is not None and not refresh and _is_fresh(entry, now, ttl_f):
            # Size-aware: regenerate if requested dims differ from cached dims.
            if int(entry.get("width", w)) == w and int(entry.get("height", h)) == h:
                entry["hits"] = int(entry.get("hits", 0)) + 1
                meta = _entry_to_info(key, entry, ttl_f)
                meta["cache"] = "HIT"
                return bytes(entry["data"]), meta
        # Miss / stale / refresh / resize -> capture fresh frame.
        fresh = _capture_frame(key, width=w, height=h)
        new_entry: Dict[str, Any] = {
            "data": fresh,
            "content_type": CONTENT_TYPE_JPEG,
            "updated_at": now,
            "hits": 0,
            "width": w,
            "height": h,
        }
        _CACHE[key] = new_entry
        _evict_if_needed()
        meta = _entry_to_info(key, new_entry, ttl_f)
        meta["cache"] = "MISS"
        return bytes(fresh), meta


def peek(camera_id: Any = None, **kwargs: Any) -> Optional[Dict[str, Any]]:
    """Return cache entry without generating (None on miss)."""
    key = _coerce_key(camera_id if camera_id is not None else kwargs.get("camera_id", "0"))
    with _LOCK:
        entry = _CACHE.get(key)
        if entry is None:
            return None
        return dict(entry)


def get_info(camera_id: Any = None, ttl: Any = None, **kwargs: Any) -> Optional[Dict[str, Any]]:
    key = _coerce_key(camera_id if camera_id is not None else kwargs.get("camera_id", "0"))
    ttl_f = _coerce_ttl(ttl, **kwargs)
    with _LOCK:
        entry = _CACHE.get(key)
        if entry is None:
            return None
        return _entry_to_info(key, entry, ttl_f)


def is_fresh(camera_id: Any = None, ttl: Any = None, **kwargs: Any) -> bool:
    key = _coerce_key(camera_id if camera_id is not None else kwargs.get("camera_id", "0"))
    ttl_f = _coerce_ttl(ttl, **kwargs)
    with _LOCK:
        entry = _CACHE.get(key)
        if entry is None:
            return False
        return _is_fresh(entry, _now(), ttl_f)


def set_thumbnail(camera_id: Any = None, data: Any = None, *args: Any,
                  content_type: str = CONTENT_TYPE_JPEG, **kwargs: Any) -> Dict[str, Any]:
    if camera_id is None:
        camera_id = kwargs.get("camera_id", "0")
    if data is None:
        for alias in ("image", "frame", "bytes", "payload", "content", "jpeg", "thumbnail"):
            if kwargs.get(alias) is not None:
                data = kwargs.get(alias)
                break
    if data is None and args:
        for a in args:
            if isinstance(a, (bytes, bytearray)):
                data = bytes(a)
                break
    if data is None:
        raise ValueError("thumbnail data is required")
    if isinstance(data, (bytearray, memoryview)):
        data = bytes(data)
    if not isinstance(data, (bytes,)):
        raise ValueError("thumbnail data must be bytes")
    key = _coerce_key(camera_id)
    w, h = _coerce_dims(kwargs.get("width"), kwargs.get("height"), **kwargs)
    with _LOCK:
        entry: Dict[str, Any] = {
            "data": bytes(data),
            "content_type": content_type or CONTENT_TYPE_JPEG,
            "updated_at": _now(),
            "hits": 0,
            "width": w,
            "height": h,
        }
        _CACHE[key] = entry
        _evict_if_needed()
        return _entry_to_info(key, entry)


def invalidate(camera_id: Any = None, **kwargs: Any) -> bool:
    if camera_id is None:
        camera_id = kwargs.get("camera_id") or kwargs.get("id") or kwargs.get("camera")
    key = _coerce_key(camera_id if camera_id is not None else "0")
    with _LOCK:
        return _CACHE.pop(key, None) is not None


def clear(camera_id: Any = None, **kwargs: Any) -> None:
    """Clear one entry when a camera id is given, else clear all."""
    if camera_id is not None or kwargs.get("camera_id") is not None or kwargs.get("id") is not None:
        invalidate(camera_id if camera_id is not None else (kwargs.get("camera_id") or kwargs.get("id")))
        return
    with _LOCK:
        _CACHE.clear()


def list_thumbnails(ttl: Any = None, **kwargs: Any) -> List[Dict[str, Any]]:
    ttl_f = _coerce_ttl(ttl if ttl is not None else kwargs.get("ttl", DEFAULT_TTL_SECONDS))
    with _LOCK:
        return [_entry_to_info(k, v, ttl_f) for k, v in sorted(_CACHE.items())]


def get_stats() -> Dict[str, Any]:
    with _LOCK:
        total_hits = sum(int(v.get("hits", 0)) for v in _CACHE.values())
        total_bytes = sum(len(v.get("data", b"")) for v in _CACHE.values())
        return {
            "entries": len(_CACHE),
            "count": len(_CACHE),
            "total_hits": total_hits,
            "hits": total_hits,
            "total_bytes": total_bytes,
            "size_bytes": total_bytes,
            "ttl_seconds": float(DEFAULT_TTL_SECONDS),
            "default_ttl_seconds": float(DEFAULT_TTL_SECONDS),
        }


# ---- Compatibility aliases (repo convention: many names, same behaviour) ----
get_cached_thumbnail = get_thumbnail
fetch_thumbnail = get_thumbnail
fetch = get_thumbnail
get = get_thumbnail
thumbnail_for = get_thumbnail
cached_thumbnail = get_thumbnail
get_frame_thumbnail = get_thumbnail
capture_thumbnail = get_thumbnail
thumbnail = get_thumbnail

get_with_meta = get_thumbnail_with_meta
fetch_with_meta = get_thumbnail_with_meta

store_thumbnail = set_thumbnail
put_thumbnail = set_thumbnail
put = set_thumbnail
store = set_thumbnail
save_thumbnail = set_thumbnail
update_thumbnail = set_thumbnail
set_cached_thumbnail = set_thumbnail

invalidate_thumbnail = invalidate
evict = invalidate
remove = invalidate
delete = invalidate
drop = invalidate

clear_all = clear
reset = clear
flush = clear

list_cached = list_thumbnails
list_cache = list_thumbnails
cached_list = list_thumbnails
all_thumbnails = list_thumbnails

cache_info = get_stats
stats = get_stats
info = get_info


@dataclass
class Thumbnail:
    camera_id: str
    data: bytes
    content_type: str = CONTENT_TYPE_JPEG
    updated_at: float = 0.0
    width: int = DEFAULT_WIDTH
    height: int = DEFAULT_HEIGHT

    def to_dict(self) -> Dict[str, Any]:
        return {
            "camera_id": self.camera_id,
            "size": len(self.data),
            "content_type": self.content_type,
            "updated_at": self.updated_at,
            "width": self.width,
            "height": self.height,
        }


class ThumbnailCache:
    """Object-oriented facade over the module-level cache."""

    def get(self, camera_id: Any = None, **kwargs: Any) -> bytes:
        return get_thumbnail(camera_id, **kwargs)

    def get_with_meta(self, camera_id: Any = None, **kwargs: Any) -> Tuple[bytes, Dict[str, Any]]:
        return get_thumbnail_with_meta(camera_id, **kwargs)

    def set(self, camera_id: Any = None, data: Any = None, **kwargs: Any) -> Dict[str, Any]:
        return set_thumbnail(camera_id, data, **kwargs)

    def peek(self, camera_id: Any = None, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return peek(camera_id, **kwargs)

    def info(self, camera_id: Any = None, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return get_info(camera_id, **kwargs)

    def invalidate(self, camera_id: Any = None, **kwargs: Any) -> bool:
        return invalidate(camera_id, **kwargs)

    def clear(self, camera_id: Any = None, **kwargs: Any) -> None:
        return clear(camera_id, **kwargs)

    def list(self) -> List[Dict[str, Any]]:
        return list_thumbnails()

    def stats(self) -> Dict[str, Any]:
        return get_stats()


class ThumbnailService(ThumbnailCache):
    pass


class ThumbnailStore(ThumbnailCache):
    pass


thumbnail_cache = ThumbnailCache()
thumbnail_service = ThumbnailCache()
thumbnails = ThumbnailCache()
cache = ThumbnailCache()
service = ThumbnailCache()
store = ThumbnailCache()


__all__ = [
    "DEFAULT_TTL_SECONDS",
    "MAX_CACHE_ENTRIES",
    "CONTENT_TYPE_JPEG",
    "get_thumbnail",
    "get_thumbnail_with_meta",
    "set_thumbnail",
    "get_info",
    "peek",
    "is_fresh",
    "invalidate",
    "clear",
    "list_thumbnails",
    "get_stats",
    "Thumbnail",
    "ThumbnailCache",
    "ThumbnailService",
    "ThumbnailStore",
    "thumbnail_cache",
]
