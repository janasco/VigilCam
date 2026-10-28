"""File bin / archive manager service (iSpy-style saved clips bin).

In-memory registry plus optional filesystem copy under ``DATA_DIR/filebin``.
All helpers are sync and dependency-free so tests run without a database
or real video files.

An entry represents a clip archived/saved into the bin for later review,
export or deletion. If ``source_path`` points at an existing file it is
copied into the bin directory; otherwise a metadata-only entry is kept
(``stored_path`` points at the would-be bin location).
"""

from __future__ import annotations

import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from app.core.config import DATA_DIR as _DATA_DIR
except Exception:  # noqa: BLE001
    _DATA_DIR = Path("data")


def _bin_dir(override: Any = None) -> Path:
    if override is not None:
        p = Path(str(override)).expanduser()
        try:
            p.mkdir(parents=True, exist_ok=True)
        except Exception:  # noqa: BLE001
            pass
        return p
    try:
        base = Path(str(_DATA_DIR)).expanduser()
    except Exception:  # noqa: BLE001
        base = Path("data")
    d = base / "filebin"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    return d


BIN_DIR = _bin_dir()
FILEBIN_DIR = BIN_DIR
FILE_BIN_DIR = BIN_DIR
ARCHIVE_DIR = BIN_DIR
BIN_PATH = BIN_DIR

FILEBIN: Dict[str, "FileBinEntry"] = {}
filebin = FILEBIN
FILE_BIN = FILEBIN
BIN = FILEBIN
ARCHIVE = FILEBIN
STORE = FILEBIN
ENTRIES = FILEBIN
FILEBIN_STORE = FILEBIN
ARCHIVE_STORE = FILEBIN

_jobs_lock = threading.Lock()
_lock = _jobs_lock


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, Path):
        return str(value)
    return repr(value)


def _normalize_inputs(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for a in args or ():
        if a is None:
            continue
        if isinstance(a, dict):
            data.update(a)
        elif isinstance(a, (str, Path)) and "source_path" not in data:
            # Bare string positional -> treat as source path unless it looks
            # like a label-only call handled by caller.
            data.setdefault("source_path", str(a))
        elif hasattr(a, "model_dump"):
            try:
                data.update(dict(a.model_dump()))
            except Exception:  # noqa: BLE001
                pass
        else:
            try:
                data.update(dict(a))
            except Exception:  # noqa: BLE001
                pass
    data.update(kwargs or {})
    for nested_key in ("payload", "options", "config", "params", "data", "body", "entry", "file", "clip", "attributes"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _pick(data: Dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if data.get(k) is not None:
            return data.get(k)
    lower = {str(k).lower(): v for k, v in data.items()}
    for k in keys:
        if lower.get(k.lower()) is not None:
            return lower.get(k.lower())
    return None


NAME_KEYS = ("name", "filename", "file_name", "title", "label", "clip_name", "display_name", "bin_name")
SOURCE_KEYS = (
    "source_path", "source", "src", "path", "file_path", "filePath",
    "recording_path", "recording", "input", "input_path", "original_path",
    "original", "clip_path", "video_path", "file",
)
CAMERA_KEYS = ("camera_id", "camera", "cam", "cameraId", "cameraID", "cam_id", "source_id")
NOTE_KEYS = ("note", "notes", "description", "comment", "memo", "caption", "detail", "details")
LOCKED_KEYS = ("locked", "protected", "pinned", "lock", "preserve", "is_locked", "is_protected")
SIZE_KEYS = ("size_bytes", "size", "bytes", "file_size", "filesize", "total_bytes")


def _coerce_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "y", "on", "locked", "protected", "pinned"):
        return True
    if s in ("0", "false", "no", "n", "off", "unlocked"):
        return False
    return bool(value)


def _safe_filename(name: Any, default: str = "clip.mp4") -> str:
    s = str(name).strip() if name is not None else ""
    if not s:
        return default
    # Keep basename only; sanitize.
    s = Path(s).name
    s = "".join(c if c.isalnum() or c in "-_. " else "_" for c in s).strip()
    s = s or default
    if "." not in s:
        s += ".mp4"
    if len(s) > 128:
        stem, _, ext = s.rpartition(".")
        s = stem[: 120 - len(ext)] + "." + ext
    return s


def _entry_to_dict(entry: Any) -> Dict[str, Any]:
    if isinstance(entry, dict):
        return dict(entry)
    if hasattr(entry, "model_dump"):
        try:
            return dict(entry.model_dump(mode="json"))
        except Exception:  # noqa: BLE001
            try:
                return dict(entry.model_dump())
            except Exception:  # noqa: BLE001
                pass
    try:
        return dict(entry)
    except Exception:  # noqa: BLE001
        return {"value": _json_safe(entry)}


class FileBinEntry(dict):
    """Dict-like archived-clip record with attribute access."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value

    def to_dict(self) -> Dict[str, Any]:
        return dict(self)

    def dict(self) -> Dict[str, Any]:
        return self.to_dict()

    def model_dump(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.to_dict()

    def as_dict(self) -> Dict[str, Any]:
        return self.to_dict()


FileBinRecord = FileBinEntry
BinEntry = FileBinEntry
ArchiveEntry = FileBinEntry
SavedClip = FileBinEntry
ClipEntry = FileBinEntry
FileBinModel = FileBinEntry
FileBinItem = FileBinEntry


def _build_entry(data: Dict[str, Any], bin_dir: Any = None) -> FileBinEntry:
    entry_id = _pick(data, "id", "entry_id", "file_id", "fileId", "bin_id", "archive_id", "clip_id") or str(uuid.uuid4())
    entry_id = str(entry_id)
    camera_id = _pick(data, *CAMERA_KEYS)
    name = _pick(data, *NAME_KEYS)
    note = _pick(data, *NOTE_KEYS)
    source = _pick(data, *SOURCE_KEYS)
    locked_raw = _pick(data, *LOCKED_KEYS)
    locked = _coerce_bool(locked_raw)
    if locked is None:
        locked = False
    tags = _pick(data, "tags", "labels", "tag")
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    elif tags is not None and not isinstance(tags, list):
        tags = [tags]

    target_dir = _bin_dir(bin_dir if bin_dir is not None else data.get("bin_dir", data.get("binDir")))
    filename = _safe_filename(
        name or (Path(str(source)).name if source else None) or _pick(data, "stored_filename", "stored_name"),
        default=f"clip_{entry_id[:8]}.mp4",
    )
    stored_path = _pick(data, "stored_path", "storedPath", "bin_path", "archive_path", "dest", "destination")
    if stored_path is None:
        stored_path = str(target_dir / filename)
    else:
        stored_path = str(stored_path)

    size = _pick(data, *SIZE_KEYS)
    try:
        size_val = int(float(size)) if size is not None else None
    except (TypeError, ValueError):
        size_val = None

    now = _pick(data, "created_at", "createdAt", "timestamp", "ts", "time", "date") or _now_iso()

    entry = FileBinEntry()
    entry["id"] = entry_id
    entry["entry_id"] = entry_id
    entry["file_id"] = str(_pick(data, "file_id", "fileId") or entry_id)
    entry["bin_id"] = entry_id
    entry["archive_id"] = entry_id
    entry["clip_id"] = str(_pick(data, "clip_id") or entry_id)
    entry["name"] = str(name).strip() if name is not None else filename
    entry["filename"] = filename
    entry["file_name"] = filename
    entry["title"] = str(_pick(data, "title", "label", "clip_name") or entry["name"])
    entry["label"] = str(_pick(data, "label", "title") or entry["name"])
    entry["camera_id"] = camera_id
    entry["camera"] = camera_id
    entry["note"] = str(note) if note is not None else ""
    entry["notes"] = entry["note"]
    entry["description"] = str(_pick(data, "description", "comment") or entry["note"])
    entry["comment"] = str(_pick(data, "comment", "description") or entry["note"])
    entry["source_path"] = str(source) if source is not None else None
    entry["source"] = entry["source_path"]
    entry["original_path"] = entry["source_path"]
    entry["stored_path"] = stored_path
    entry["storedPath"] = stored_path
    entry["bin_path"] = stored_path
    entry["archive_path"] = stored_path
    entry["path"] = stored_path
    entry["file_path"] = stored_path
    entry["locked"] = bool(locked)
    entry["protected"] = bool(locked)
    entry["pinned"] = bool(_pick(data, "pinned") if _pick(data, "pinned") is not None else locked)
    entry["tags"] = tags or []
    entry["size_bytes"] = size_val
    entry["size"] = size_val
    entry["bytes"] = size_val
    entry["created_at"] = now
    entry["updated_at"] = _pick(data, "updated_at", "updatedAt") or now
    entry["timestamp"] = now
    entry["createdAt"] = entry["created_at"]
    entry["updatedAt"] = entry["updated_at"]
    for k, v in data.items():
        if k not in entry:
            entry[k] = v
    return entry


def _copy_into_bin(source: Any, stored_path: str) -> Optional[int]:
    """Copy existing source file into the bin. Returns size or None."""
    if source is None:
        return None
    try:
        src = Path(str(source)).expanduser()
    except Exception:  # noqa: BLE001
        return None
    if not src.exists() or not src.is_file():
        return None
    try:
        dest = Path(str(stored_path)).expanduser()
        if src.resolve() == dest.resolve():
            try:
                return int(dest.stat().st_size)
            except OSError:
                return None
    except Exception:  # noqa: BLE001
        pass
    try:
        dest = Path(str(stored_path)).expanduser()
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(dest))
        return int(dest.stat().st_size)
    except Exception:  # noqa: BLE001
        return None


def _is_inside_bin(path: Any, bin_dir: Any = None) -> bool:
    try:
        base = Path(str(bin_dir or BIN_DIR)).expanduser().resolve()
        target = Path(str(path)).expanduser().resolve()
    except Exception:  # noqa: BLE001
        return False
    try:
        target.relative_to(base)
        return True
    except ValueError:
        return False


class FileBinService:
    """In-memory file-bin store with optional on-disk copies."""

    def __init__(self, bin_dir: Any = None) -> None:
        self.bin_dir = _bin_dir(bin_dir)
        self.store = FILEBIN
        self.bin = FILEBIN
        self.archive = FILEBIN
        self.entries = FILEBIN
        self._lock = _jobs_lock

    # -- archive / save --
    def archive_file(self, *args: Any, bin_dir: Any = None, copy: bool = True, **kwargs: Any) -> FileBinEntry:
        data = _normalize_inputs(*args, **kwargs)
        # Bare positional string ambiguous between source path and label:
        # if it names an existing file keep as source, else treat as name.
        if args and isinstance(args[0], (str, Path)) and not isinstance(args[0], dict):
            raw = str(args[0]).strip()
            p = Path(raw).expanduser()
            if not p.exists() and data.get("source_path") == raw:
                # No such file: reinterpret as label/name.
                data.pop("source_path", None)
                data.setdefault("name", raw)
        target = _bin_dir(bin_dir or data.get("bin_dir") or self.bin_dir)
        entry = _build_entry(data, bin_dir=target)
        if copy:
            src = entry.get("source_path")
            copied = _copy_into_bin(src, entry["stored_path"])
            if copied is not None:
                entry["size_bytes"] = copied
                entry["size"] = copied
                entry["bytes"] = copied
            else:
                # Refresh size if stored file already exists (e.g. re-archive).
                try:
                    sp = Path(str(entry["stored_path"])).expanduser()
                    if sp.exists() and sp.is_file():
                        sz = int(sp.stat().st_size)
                        if entry.get("size_bytes") is None:
                            entry["size_bytes"] = sz
                            entry["size"] = sz
                            entry["bytes"] = sz
                except Exception:  # noqa: BLE001
                    pass
        with self._lock:
            FILEBIN[entry["id"]] = entry
        return entry

    def save_to_bin(self, *args: Any, **kwargs: Any) -> FileBinEntry:
        return self.archive_file(*args, **kwargs)

    def save_file(self, *args: Any, **kwargs: Any) -> FileBinEntry:
        return self.archive_file(*args, **kwargs)

    def add_file(self, *args: Any, **kwargs: Any) -> FileBinEntry:
        return self.archive_file(*args, **kwargs)

    def add_entry(self, *args: Any, **kwargs: Any) -> FileBinEntry:
        return self.archive_file(*args, **kwargs)

    def create_entry(self, *args: Any, **kwargs: Any) -> FileBinEntry:
        return self.archive_file(*args, **kwargs)

    def add(self, *args: Any, **kwargs: Any) -> FileBinEntry:
        return self.archive_file(*args, **kwargs)

    def create(self, *args: Any, **kwargs: Any) -> FileBinEntry:
        return self.archive_file(*args, **kwargs)

    def save(self, *args: Any, **kwargs: Any) -> FileBinEntry:
        return self.archive_file(*args, **kwargs)

    def archive(self, *args: Any, **kwargs: Any) -> FileBinEntry:
        return self.archive_file(*args, **kwargs)

    def bin_file(self, *args: Any, **kwargs: Any) -> FileBinEntry:
        return self.archive_file(*args, **kwargs)

    # -- get --
    def get_entry(self, entry_id: Any) -> Optional[FileBinEntry]:
        if entry_id is None:
            return None
        key = str(entry_id)
        with self._lock:
            found = FILEBIN.get(key)
            if found is not None:
                return found
            for e in FILEBIN.values():
                for k in ("file_id", "bin_id", "archive_id", "clip_id", "filename", "name"):
                    if str(e.get(k)) == key:
                        return e
        return None

    def get(self, entry_id: Any) -> Optional[FileBinEntry]:
        return self.get_entry(entry_id)

    def fetch_entry(self, entry_id: Any) -> Optional[FileBinEntry]:
        return self.get_entry(entry_id)

    def fetch(self, entry_id: Any) -> Optional[FileBinEntry]:
        return self.get_entry(entry_id)

    # -- list --
    def list_bin(
        self,
        camera_id: Any = None,
        locked: Any = None,
        search: Any = None,
        limit: Any = None,
        offset: Any = None,
        skip: Any = None,
        **filters: Any,
    ) -> List[FileBinEntry]:
        if isinstance(camera_id, dict):
            filters.update(camera_id)
            camera_id = None
        if camera_id is None:
            camera_id = filters.pop("camera", filters.pop("cam", filters.pop("cameraId", None)))
        if locked is None:
            locked = filters.pop("protected", filters.pop("pinned", filters.pop("is_locked", None)))
        if search is None:
            search = filters.pop("q", filters.pop("query", filters.pop("name", filters.pop("label", None))))
        lim = filters.pop("limit", limit)
        for alias in ("n", "count", "size", "max", "top", "per_page", "page_size"):
            if lim is None and filters.get(alias) is not None:
                lim = filters.pop(alias)
        sk = filters.pop("skip", skip)
        if sk is None:
            sk = filters.pop("offset", offset if offset is not None else 0)
        with self._lock:
            items = [_entry_to_dict(e) for e in list(FILEBIN.values())]
        out: List[Dict[str, Any]] = []
        for ev in items:
            if camera_id is not None and ev.get("camera_id") is not None:
                if str(ev.get("camera_id")) != str(camera_id):
                    continue
            elif camera_id is not None and ev.get("camera_id") is None:
                continue
            if locked is not None:
                want = _coerce_bool(locked)
                if want is not None and bool(ev.get("locked")) != want:
                    continue
            if search is not None and str(search).strip():
                needle = str(search).strip().lower()
                hay = " ".join(
                    str(ev.get(k) or "") for k in ("name", "filename", "title", "label", "note", "description", "camera_id", "id")
                ).lower()
                if needle not in hay:
                    continue
            # Extra exact-match filters (tolerant: unknown keys skipped).
            ok = True
            for key, expected in (filters or {}).items():
                if expected is None:
                    continue
                if key in ("format", "fmt") and str(expected).lower() in ("json", "csv"):
                    continue
                ev_val = ev.get(key)
                if ev_val is None:
                    continue
                if str(ev_val) != str(expected):
                    ok = False
                    break
            if ok:
                out.append(ev)  # type: ignore[arg-type]
        try:
            sk_i = int(sk or 0)
        except (TypeError, ValueError):
            sk_i = 0
        if sk_i:
            out = out[sk_i:]
        if lim is not None:
            try:
                lim_i = int(lim)
            except (TypeError, ValueError):
                lim_i = None
            if lim_i is not None and lim_i >= 0:
                out = out[:lim_i]
        return out  # type: ignore[return-value]

    def list(self, *args: Any, **kwargs: Any) -> List[FileBinEntry]:
        return self.list_bin(*args, **kwargs)

    def list_files(self, *args: Any, **kwargs: Any) -> List[FileBinEntry]:
        return self.list_bin(*args, **kwargs)

    def list_archive(self, *args: Any, **kwargs: Any) -> List[FileBinEntry]:
        return self.list_bin(*args, **kwargs)

    def get_bin(self, *args: Any, **kwargs: Any) -> List[FileBinEntry]:
        return self.list_bin(*args, **kwargs)

    def all(self, *args: Any, **kwargs: Any) -> List[FileBinEntry]:
        return self.list_bin(*args, **kwargs)

    # -- update --
    def update_entry(self, entry_id: Any, *args: Any, **kwargs: Any) -> Optional[FileBinEntry]:
        entry = self.get_entry(entry_id)
        if entry is None:
            return None
        data = _normalize_inputs(*args, **kwargs)
        new_name = _pick(data, *NAME_KEYS)
        if new_name is not None:
            s = str(new_name).strip()
            if s:
                entry["name"] = s
                if "title" in data or "label" not in data:
                    entry["title"] = str(data.get("title", s))
                if "label" in data or "title" not in data:
                    entry["label"] = str(data.get("label", s))
                if "filename" in data or "file_name" in data:
                    entry["filename"] = _safe_filename(data.get("filename", data.get("file_name")))
                    entry["file_name"] = entry["filename"]
        new_note = _pick(data, *NOTE_KEYS)
        note_keys_present = any(k in data for k in NOTE_KEYS)
        if new_note is not None or note_keys_present:
            s = str(new_note) if new_note is not None else ""
            entry["note"] = s
            entry["notes"] = s
            entry["description"] = str(data.get("description", data.get("comment", s)))
            entry["comment"] = str(data.get("comment", data.get("description", s)))
        new_cam = _pick(data, *CAMERA_KEYS)
        if new_cam is not None:
            entry["camera_id"] = new_cam
            entry["camera"] = new_cam
        new_locked = _pick(data, *LOCKED_KEYS)
        if new_locked is not None:
            coerced = _coerce_bool(new_locked)
            if coerced is not None:
                entry["locked"] = coerced
                entry["protected"] = coerced
                if "pinned" in data or coerced is False:
                    entry["pinned"] = bool(data.get("pinned", coerced))
        if "tags" in data or "tag" in data:
            tags = data.get("tags", data.get("tag"))
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(",") if t.strip()]
            entry["tags"] = list(tags) if isinstance(tags, (list, tuple)) else []
        for k, v in data.items():
            if k not in entry and k not in (
                "payload", "options", "config", "params", "data", "body",
                "entry", "file", "clip", "attributes", "bin_dir",
            ):
                entry[k] = v
        entry["updated_at"] = _now_iso()
        entry["updatedAt"] = entry["updated_at"]
        return entry

    def update(self, entry_id: Any, *args: Any, **kwargs: Any) -> Optional[FileBinEntry]:
        return self.update_entry(entry_id, *args, **kwargs)

    def rename(self, entry_id: Any, name: Any = None, **kwargs: Any) -> Optional[FileBinEntry]:
        if name is None:
            name = kwargs.pop("name", kwargs.pop("filename", kwargs.pop("title", kwargs.pop("label", None))))
        return self.update_entry(entry_id, name=name, **kwargs)

    def lock(self, entry_id: Any) -> Optional[FileBinEntry]:
        return self.update_entry(entry_id, locked=True)

    def unlock(self, entry_id: Any) -> Optional[FileBinEntry]:
        return self.update_entry(entry_id, locked=False)

    def protect(self, entry_id: Any) -> Optional[FileBinEntry]:
        return self.lock(entry_id)

    def unprotect(self, entry_id: Any) -> Optional[FileBinEntry]:
        return self.unlock(entry_id)

    # -- delete --
    def delete_entry(self, entry_id: Any, delete_file: bool = True, **kwargs: Any) -> bool:
        if entry_id is None:
            return False
        if kwargs.get("deleteFile") is not None:
            delete_file = bool(kwargs.get("deleteFile"))
        if kwargs.get("remove_file") is not None:
            delete_file = bool(kwargs.get("remove_file"))
        key = str(entry_id)
        with self._lock:
            target_key: Optional[str] = None
            target: Optional[FileBinEntry] = None
            if FILEBIN.get(key) is not None:
                target_key, target = key, FILEBIN.get(key)
            else:
                for k, e in list(FILEBIN.items()):
                    if any(str(e.get(f)) == key for f in ("file_id", "bin_id", "archive_id", "clip_id", "filename", "name")):
                        target_key, target = k, e
                        break
            if target_key is None or target is None:
                return False
            # Refuse to delete locked entries unless forced.
            force = bool(kwargs.get("force", kwargs.get("unlocked", False)))
            if bool(target.get("locked")) and not force:
                return False
            FILEBIN.pop(target_key, None)
        if delete_file:
            try:
                sp = target.get("stored_path")
                if sp and _is_inside_bin(sp, self.bin_dir):
                    p = Path(str(sp)).expanduser()
                    if p.exists() and p.is_file():
                        p.unlink()
            except Exception:  # noqa: BLE001
                pass
        return True

    def delete(self, entry_id: Any, **kwargs: Any) -> bool:
        return self.delete_entry(entry_id, **kwargs)

    def remove(self, entry_id: Any, **kwargs: Any) -> bool:
        return self.delete_entry(entry_id, **kwargs)

    def remove_entry(self, entry_id: Any, **kwargs: Any) -> bool:
        return self.delete_entry(entry_id, **kwargs)

    def clear_bin(self, **filters: Any) -> int:
        force = bool(filters.pop("force", False))
        with self._lock:
            if not filters:
                if not force:
                    # Keep locked entries unless forced.
                    locked_keys = [k for k, e in FILEBIN.items() if bool(e.get("locked"))]
                    if locked_keys:
                        targets = [k for k in list(FILEBIN.keys()) if k not in locked_keys]
                        for k in targets:
                            entry = FILEBIN.pop(k, None)
                            try:
                                sp = (entry or {}).get("stored_path")
                                if sp and _is_inside_bin(sp, self.bin_dir):
                                    p = Path(str(sp)).expanduser()
                                    if p.exists() and p.is_file():
                                        p.unlink()
                            except Exception:  # noqa: BLE001
                                pass
                        return len(targets)
                n = len(FILEBIN)
                victims = list(FILEBIN.values())
                FILEBIN.clear()
                for entry in victims:
                    try:
                        sp = entry.get("stored_path")
                        if sp and _is_inside_bin(sp, self.bin_dir):
                            p = Path(str(sp)).expanduser()
                            if p.exists() and p.is_file():
                                p.unlink()
                    except Exception:  # noqa: BLE001
                        pass
                return n
        targets = self.list_bin(**filters)
        n = 0
        for t in targets:
            if self.delete_entry(t.get("id"), force=force):
                n += 1
        return n

    def clear(self, **filters: Any) -> int:
        return self.clear_bin(**filters)

    def reset(self, **filters: Any) -> int:
        filters.setdefault("force", True)
        return self.clear_bin(**filters)

    def purge(self, **filters: Any) -> int:
        return self.clear_bin(**filters)

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            items = list(FILEBIN.values())
        total = 0
        for e in items:
            try:
                sz = e.get("size_bytes")
                if sz is None:
                    sp = e.get("stored_path")
                    if sp:
                        p = Path(str(sp)).expanduser()
                        if p.exists() and p.is_file():
                            sz = int(p.stat().st_size)
                if sz is not None:
                    total += int(sz)
            except Exception:  # noqa: BLE001
                continue
        locked = sum(1 for e in items if bool(e.get("locked")))
        return {
            "count": len(items),
            "total": len(items),
            "entries": len(items),
            "files": len(items),
            "total_bytes": total,
            "total_size": total,
            "size_bytes": total,
            "locked": locked,
            "locked_count": locked,
            "protected": locked,
            "bin_dir": str(self.bin_dir),
            "ok": True,
            "status": "ok",
        }

    def count(self, **filters: Any) -> int:
        if not filters:
            with self._lock:
                return len(FILEBIN)
        return len(self.list_bin(**filters))

    def __len__(self) -> int:
        with self._lock:
            return len(FILEBIN)


filebin_service = FileBinService()
bin_service = filebin_service
archive_service = filebin_service
service = filebin_service
manager = filebin_service
bin_manager = filebin_service
archive_manager = filebin_service
store = filebin_service
default_service = filebin_service


def archive_file(*args: Any, **kwargs: Any) -> FileBinEntry:
    return filebin_service.archive_file(*args, **kwargs)


def save_to_bin(*args: Any, **kwargs: Any) -> FileBinEntry:
    return filebin_service.save_to_bin(*args, **kwargs)


def save_file(*args: Any, **kwargs: Any) -> FileBinEntry:
    return filebin_service.save_file(*args, **kwargs)


def add_file(*args: Any, **kwargs: Any) -> FileBinEntry:
    return filebin_service.add_file(*args, **kwargs)


def add_entry(*args: Any, **kwargs: Any) -> FileBinEntry:
    return filebin_service.add_entry(*args, **kwargs)


def create_entry(*args: Any, **kwargs: Any) -> FileBinEntry:
    return filebin_service.create_entry(*args, **kwargs)


def get_entry(entry_id: Any) -> Optional[FileBinEntry]:
    return filebin_service.get_entry(entry_id)


def list_bin(*args: Any, **kwargs: Any) -> List[FileBinEntry]:
    return filebin_service.list_bin(*args, **kwargs)


def list_files(*args: Any, **kwargs: Any) -> List[FileBinEntry]:
    return filebin_service.list_bin(*args, **kwargs)


def update_entry(*args: Any, **kwargs: Any) -> Optional[FileBinEntry]:
    if args:
        return filebin_service.update_entry(args[0], *args[1:], **kwargs)
    bid = kwargs.pop("entry_id", kwargs.pop("id", None))
    return filebin_service.update_entry(bid, **kwargs)


def delete_entry(entry_id: Any, **kwargs: Any) -> bool:
    return filebin_service.delete_entry(entry_id, **kwargs)


def clear_bin(**filters: Any) -> int:
    return filebin_service.clear_bin(**filters)


def clear(**filters: Any) -> int:
    return filebin_service.clear_bin(**filters)


def stats() -> Dict[str, Any]:
    return filebin_service.stats()


def count(**filters: Any) -> int:
    return filebin_service.count(**filters)


# Compatibility aliases.
save = save_to_bin
archive = archive_file
add = add_file
create = create_entry
get = get_entry
fetch = get_entry
list_all = list_bin
all_entries = list_bin
all_files = list_bin
remove = delete_entry
delete = delete_entry
reset = filebin_service.reset
purge = filebin_service.purge
lock_entry = filebin_service.lock
unlock_entry = filebin_service.unlock

FileBinServiceClass = FileBinService
FileBinManager = FileBinService
ArchiveService = FileBinService
ArchiveManager = FileBinService
BinService = FileBinService
BinManager = FileBinService

__all__ = [
    "FILEBIN",
    "BIN_DIR",
    "FileBinEntry",
    "FileBinService",
    "filebin_service",
    "archive_file",
    "save_to_bin",
    "save_file",
    "add_file",
    "add_entry",
    "create_entry",
    "get_entry",
    "list_bin",
    "list_files",
    "update_entry",
    "delete_entry",
    "clear_bin",
    "clear",
    "stats",
]
