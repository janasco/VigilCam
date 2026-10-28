"""Timelapse gallery service.

Lists finished timelapse outputs from the shared timelapse outputs
directory (reuses :mod:`app.services.timelapse` directory resolution)
without requiring any edit to ``app/main.py`` or templates.
"""

from __future__ import annotations

import fnmatch
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from app.services import timelapse as timelapse_service
except Exception:  # noqa: BLE001
    timelapse_service = None  # type: ignore[assignment]

SUPPORTED_VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".avi"}
SUPPORTED_EXTS = SUPPORTED_VIDEO_EXTS
VIDEO_EXTS = SUPPORTED_VIDEO_EXTS
ALLOWED_EXTS = SUPPORTED_VIDEO_EXTS
GALLERY_EXTS = SUPPORTED_VIDEO_EXTS


def _resolve_gallery_dir(override: Any = None, **kwargs: Any) -> Path:
    """Resolve the gallery directory, reusing the timelapse outputs dir."""
    for key in (
        "timelapse_dir",
        "gallery_dir",
        "gallery_directory",
        "directory",
        "dir",
        "folder",
        "root",
        "root_dir",
        "output_dir",
        "path",
    ):
        if kwargs.get(key) is not None:
            override = kwargs.get(key)
            break
    if override is not None:
        p = Path(str(override)).expanduser()
        try:
            p.mkdir(parents=True, exist_ok=True)
        except Exception:  # noqa: BLE001
            pass
        return p
    # Reuse timelapse outputs dir listing.
    try:
        if timelapse_service is not None and hasattr(timelapse_service, "_default_timelapse_dir"):
            return timelapse_service._default_timelapse_dir()  # type: ignore[no-any-return]
    except Exception:  # noqa: BLE001
        pass
    try:
        if timelapse_service is not None and getattr(timelapse_service, "TIMELAPSE_DIR", None) is not None:
            p = Path(str(timelapse_service.TIMELAPSE_DIR)).expanduser()
            try:
                p.mkdir(parents=True, exist_ok=True)
            except Exception:  # noqa: BLE001
                pass
            return p
    except Exception:  # noqa: BLE001
        pass
    try:
        from app.core.config import DATA_DIR as _DATA_DIR

        d = Path(str(_DATA_DIR)).expanduser() / "timelapses"
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:  # noqa: BLE001
            pass
        return d
    except Exception:  # noqa: BLE001
        pass
    d = Path("data/timelapses")
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    return d


try:
    GALLERY_DIR = _resolve_gallery_dir()
except Exception:  # noqa: BLE001
    GALLERY_DIR = Path("data/timelapses")

TIMELAPSE_GALLERY_DIR = GALLERY_DIR
GALLERY_DIRECTORY = GALLERY_DIR
TIMELAPSE_DIR = GALLERY_DIR


def _item_from_path(p: Path) -> Dict[str, Any]:
    p = Path(str(p))
    try:
        st = p.stat()
        size = int(st.st_size)
        mtime = float(st.st_mtime)
        ctime = float(getattr(st, "st_ctime", mtime))
    except OSError:
        size = 0
        mtime = 0.0
        ctime = 0.0
    try:
        mtime_iso = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
    except Exception:  # noqa: BLE001
        mtime_iso = ""
    try:
        ctime_iso = datetime.fromtimestamp(ctime, tz=timezone.utc).isoformat()
    except Exception:  # noqa: BLE001
        ctime_iso = mtime_iso
    name = p.name
    stem = p.stem
    suffix = p.suffix.lower()
    item: Dict[str, Any] = {
        "id": name,
        "item_id": name,
        "timelapse_id": name,
        "gallery_id": name,
        "entry_id": name,
        "name": name,
        "filename": name,
        "file": name,
        "title": stem,
        "stem": stem,
        "suffix": suffix,
        "extension": suffix,
        "ext": suffix,
        "format": suffix.lstrip("."),
        "path": str(p),
        "output_path": str(p),
        "output": str(p),
        "file_path": str(p),
        "filepath": str(p),
        "absolute_path": str(p.resolve()) if p.exists() else str(p),
        "size": size,
        "size_bytes": size,
        "bytes": size,
        "file_size": size,
        "mtime": mtime,
        "mtime_iso": mtime_iso,
        "modified": mtime,
        "modified_at": mtime_iso,
        "updated_at": mtime_iso,
        "created": ctime,
        "created_at": ctime_iso,
        "timestamp": mtime_iso,
        "ok": True,
        "success": True,
    }
    return item


def _coerce_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(float(str(value).strip())) if isinstance(value, str) else int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def list_gallery(
    timelapse_dir: Any = None,
    gallery_dir: Any = None,
    directory: Any = None,
    limit: Any = None,
    offset: Any = None,
    skip: Any = None,
    sort: Any = None,
    order: Any = None,
    reverse: Any = None,
    pattern: Any = None,
    extension: Any = None,
    *args: Any,
    **kwargs: Any,
) -> List[Dict[str, Any]]:
    """List finished timelapse files from the shared outputs directory."""
    # Positional alias: first positional may be a directory override.
    if timelapse_dir is None and args:
        for a in args:
            if isinstance(a, (str, Path)):
                timelapse_dir = a
                break
    # Kwarg aliases for directory.
    for key in ("dir", "folder", "root", "root_dir", "output_dir", "path", "gallery_directory"):
        if kwargs.get(key) is not None and timelapse_dir is None and gallery_dir is None and directory is None:
            directory = kwargs.get(key)
            break
    # Kwarg aliases for limit/offset/sort.
    if limit is None:
        for key in ("n", "count", "size", "max", "top", "per_page", "page_size"):
            if kwargs.get(key) is not None:
                limit = kwargs.get(key)
                break
    if offset is None and skip is None:
        for key in ("start_index", "page_offset"):
            if kwargs.get(key) is not None:
                offset = kwargs.get(key)
                break
        if kwargs.get("offset") is not None:
            offset = kwargs.get("offset")
        if kwargs.get("skip") is not None:
            skip = kwargs.get("skip")
    if sort is None:
        for key in ("sort_by", "sortBy", "order_by", "orderBy"):
            if kwargs.get(key) is not None:
                sort = kwargs.get(key)
                break
    if order is None:
        for key in ("direction", "dir_order"):
            if kwargs.get(key) is not None:
                order = kwargs.get(key)
                break
    if reverse is None:
        for key in ("descending", "desc", "reversed"):
            if kwargs.get(key) is not None:
                reverse = kwargs.get(key)
                break
    if pattern is None:
        for key in ("glob", "filter", "search", "q", "query", "match"):
            if kwargs.get(key) is not None:
                pattern = kwargs.get(key)
                break
    if extension is None:
        for key in ("ext", "suffix", "format", "fmt"):
            if kwargs.get(key) is not None:
                extension = kwargs.get(key)
                break

    override = timelapse_dir if timelapse_dir is not None else (gallery_dir if gallery_dir is not None else directory)
    target = _resolve_gallery_dir(override, **kwargs)
    try:
        if not target.exists() or not target.is_dir():
            return []
        entries = [p for p in target.iterdir() if p.is_file()]
    except OSError:
        return []

    # Filter by supported video extensions by default; allow override via extension="*".
    ext_filter: Optional[str] = None
    if extension is not None and str(extension).strip() not in ("", "*", "all"):
        e = str(extension).strip().lower()
        if not e.startswith("."):
            e = "." + e
        ext_filter = e

    items: List[Dict[str, Any]] = []
    for p in entries:
        suf = p.suffix.lower()
        if ext_filter is not None:
            if suf != ext_filter:
                continue
        elif suf not in SUPPORTED_VIDEO_EXTS:
            continue
        if pattern is not None and str(pattern).strip() not in ("", "*", "all"):
            pat = str(pattern).strip()
            # Support plain substring as well as glob.
            if any(ch in pat for ch in ("*", "?", "[")):
                if not fnmatch.fnmatch(p.name, pat):
                    continue
            elif pat.lower() not in p.name.lower():
                continue
        try:
            items.append(_item_from_path(p))
        except Exception:  # noqa: BLE001
            continue

    # Sorting: default newest first (mtime desc).
    sort_key = str(sort).strip().lower() if sort is not None else "mtime"
    order_s = str(order).strip().lower() if order is not None else ""
    rev = bool(reverse)
    if order_s in ("asc", "ascending", "oldest", "oldest_first"):
        rev = False if reverse is None else rev
        if reverse is None:
            rev = False
    elif order_s in ("desc", "descending", "newest", "newest_first"):
        rev = True if reverse is None else rev
        if reverse is None:
            rev = True
    elif reverse is None:
        rev = True if sort_key in ("mtime", "modified", "date", "time", "newest", "recent", "") else False
    elif isinstance(reverse, str):
        rev = reverse.strip().lower() in ("1", "true", "yes", "on", "desc", "descending")
    if isinstance(reverse, str):
        rev = reverse.strip().lower() in ("1", "true", "yes", "on", "desc", "descending")

    if sort_key in ("name", "filename", "file", "title", "id", "alpha", "alphabetical"):
        items.sort(key=lambda d: str(d.get("name", "")).lower(), reverse=rev)
    elif sort_key in ("size", "bytes", "size_bytes", "file_size", "largest", "smallest"):
        items.sort(key=lambda d: (int(d.get("size_bytes", 0) or 0), str(d.get("name", ""))), reverse=rev)
    else:
        items.sort(key=lambda d: (float(d.get("mtime", 0.0) or 0.0), str(d.get("name", ""))), reverse=rev)

    # Pagination.
    start = _coerce_int(skip if skip is not None else offset) or 0
    if start < 0:
        start = 0
    lim = _coerce_int(limit)
    if start:
        items = items[start:]
    if lim is not None and lim >= 0:
        items = items[:lim]
    return items


def get_gallery_item(item_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    """Fetch a single gallery entry by filename, stem, or path."""
    if item_id is None:
        for key in ("id", "item", "entry_id", "timelapse_id", "gallery_id", "name", "filename", "file", "key", "title", "stem", "path", "output_path"):
            if kwargs.get(key) is not None:
                item_id = kwargs.get(key)
                break
    if item_id is None and args:
        item_id = args[0]
    if item_id is None:
        return None
    override = kwargs.get("timelapse_dir", kwargs.get("gallery_dir", kwargs.get("directory", kwargs.get("dir"))))
    target = _resolve_gallery_dir(override, **{k: v for k, v in kwargs.items() if k not in ("timelapse_dir", "gallery_dir", "directory", "dir")})
    raw = str(item_id).strip()
    if not raw:
        return None
    # Direct path handling (absolute or gallery-relative).
    candidates: List[Path] = []
    try:
        direct = Path(raw).expanduser()
        if direct.is_absolute():
            candidates.append(direct)
        candidates.append(target / Path(raw).name)
        candidates.append(target / raw)
    except Exception:  # noqa: BLE001
        candidates.append(target / raw)
    for cand in candidates:
        try:
            if cand.is_file():
                # Containment check: must live inside the gallery dir (or be the exact gallery file).
                try:
                    if cand.resolve() == (target / cand.name).resolve() or target.resolve() in cand.resolve().parents:
                        return _item_from_path(cand)
                except Exception:  # noqa: BLE001
                    return _item_from_path(cand)
        except OSError:
            continue
    # Fall back to stem match (e.g. lookup without extension).
    needle = Path(raw).stem.lower() if "." in Path(raw).name else raw.lower()
    try:
        for p in target.iterdir():
            if not p.is_file() or p.suffix.lower() not in SUPPORTED_VIDEO_EXTS:
                continue
            if p.stem.lower() == needle or p.name.lower() == raw.lower():
                return _item_from_path(p)
    except OSError:
        return None
    return None


def delete_gallery_item(item_id: Any = None, *args: Any, **kwargs: Any) -> bool:
    """Delete a gallery file. Returns True when a file was removed."""
    info = get_gallery_item(item_id, *args, **kwargs)
    if info is None:
        return False
    try:
        Path(str(info["path"])).unlink(missing_ok=True)  # type: ignore[attr-defined]
        return True
    except TypeError:
        # Python < 3.8 without missing_ok.
        try:
            p = Path(str(info["path"]))
            if p.exists():
                p.unlink()
                return True
            return False
        except OSError:
            return False
    except OSError:
        return False


def count_gallery(*args: Any, **kwargs: Any) -> int:
    """Count gallery entries."""
    try:
        return len(list_gallery(*args, **kwargs))
    except Exception:  # noqa: BLE001
        return 0


class TimelapseGalleryService:
    """Object-oriented facade over the gallery listing helpers."""

    def __init__(self, timelapse_dir: Any = None, gallery_dir: Any = None, **kwargs: Any) -> None:
        override = timelapse_dir if timelapse_dir is not None else gallery_dir
        if override is None:
            for key in ("directory", "dir", "folder", "root", "output_dir", "path"):
                if kwargs.get(key) is not None:
                    override = kwargs.get(key)
                    break
        self.gallery_dir = _resolve_gallery_dir(override)
        self.timelapse_dir = self.gallery_dir
        self.directory = self.gallery_dir

    def list_gallery(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        kwargs.setdefault("timelapse_dir", self.gallery_dir)
        return list_gallery(*args, **kwargs)

    def list(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return self.list_gallery(*args, **kwargs)

    def get_gallery_item(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        kwargs.setdefault("timelapse_dir", self.gallery_dir)
        return get_gallery_item(*args, **kwargs)

    def get(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return self.get_gallery_item(*args, **kwargs)

    def delete_gallery_item(self, *args: Any, **kwargs: Any) -> bool:
        kwargs.setdefault("timelapse_dir", self.gallery_dir)
        return delete_gallery_item(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> bool:
        return self.delete_gallery_item(*args, **kwargs)

    def count_gallery(self, *args: Any, **kwargs: Any) -> int:
        kwargs.setdefault("timelapse_dir", self.gallery_dir)
        return count_gallery(*args, **kwargs)

    def count(self, *args: Any, **kwargs: Any) -> int:
        return self.count_gallery(*args, **kwargs)

    # Aliases.
    def list_timelapses(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return self.list_gallery(*args, **kwargs)

    def fetch_gallery(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return self.list_gallery(*args, **kwargs)


class TimelapseGallery(TimelapseGalleryService):
    pass


class GalleryService(TimelapseGalleryService):
    pass


class Gallery(TimelapseGalleryService):
    pass


timelapse_gallery_service = TimelapseGalleryService()
gallery_service = timelapse_gallery_service
service = timelapse_gallery_service
default_service = timelapse_gallery_service
instance = timelapse_gallery_service
singleton = timelapse_gallery_service


# Functional aliases (defensive API).
list_timelapses = list_gallery
list_timelapse_gallery = list_gallery
list_gallery_items = list_gallery
list_items = list_gallery
list_entries = list_gallery
list_files = list_gallery
fetch_gallery = list_gallery
get_gallery = list_gallery
fetch_gallery_items = list_gallery
gallery_list = list_gallery
all_items = list_gallery
list_all = list_gallery

get_timelapse = get_gallery_item
get_item = get_gallery_item
fetch_item = get_gallery_item
get_entry = get_gallery_item
fetch_gallery_item = get_gallery_item
get = get_gallery_item

delete_timelapse = delete_gallery_item
delete_item = delete_gallery_item
remove_item = delete_gallery_item
remove_gallery_item = delete_gallery_item
delete = delete_gallery_item
remove = delete_gallery_item
delete_entry = delete_gallery_item

count = count_gallery
count_items = count_gallery
count_timelapses = count_gallery
gallery_count = count_gallery

TimelapseGalleryServiceClass = TimelapseGalleryService
TimelapseGalleryManager = TimelapseGalleryService
GalleryManager = TimelapseGalleryService

__all__ = [
    "GALLERY_DIR",
    "TIMELAPSE_GALLERY_DIR",
    "SUPPORTED_VIDEO_EXTS",
    "SUPPORTED_EXTS",
    "list_gallery",
    "list_timelapses",
    "list_timelapse_gallery",
    "list_gallery_items",
    "list_items",
    "get_gallery_item",
    "get_item",
    "delete_gallery_item",
    "delete_item",
    "count_gallery",
    "count",
    "TimelapseGalleryService",
    "TimelapseGallery",
    "GalleryService",
    "timelapse_gallery_service",
    "gallery_service",
]
