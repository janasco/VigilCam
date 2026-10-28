from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

BOOKMARKS: Dict[str, "Bookmark"] = {}
bookmarks = BOOKMARKS
BOOKMARK_STORE = BOOKMARKS
STORE = BOOKMARKS
JOBS = BOOKMARKS

_jobs_lock = threading.Lock()
_lock = _jobs_lock


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _coerce_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return default


RECORDING_KEYS = (
    "recording_id",
    "recording",
    "recording_pk",
    "rec_id",
    "video_id",
    "media_id",
)
CAMERA_KEYS = (
    "camera_id",
    "camera",
    "camera_pk",
    "cam_id",
    "cam",
    "source_id",
)
OFFSET_KEYS = (
    "offset_seconds",
    "offset",
    "timestamp",
    "time",
    "position",
    "position_seconds",
    "seek",
    "seek_seconds",
    "at",
    "second",
    "seconds",
    "ts",
    "t",
)
LABEL_KEYS = (
    "label",
    "title",
    "name",
    "note",
    "notes",
    "description",
    "comment",
    "text",
    "memo",
    "caption",
)


def _extract(data: Dict[str, Any], keys: tuple) -> Any:
    for k in keys:
        if k in data and data.get(k) is not None:
            return data.get(k)
    lower = {str(k).lower(): v for k, v in data.items()}
    for k in keys:
        lk = k.lower()
        if lk in lower and lower[lk] is not None:
            return lower[lk]
    for nested_key in ("payload", "options", "config", "params", "data", "body", "bookmark", "attributes"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k in keys:
                if k in nested and nested.get(k) is not None:
                    return nested.get(k)
    return None


def _normalize_inputs(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for a in args or ():
        if isinstance(a, dict):
            data.update(a)
        elif isinstance(a, (int, float)) and "offset_seconds" not in data:
            data["offset_seconds"] = a
        elif isinstance(a, str):
            s = a.strip()
            if not s:
                continue
            if "label" not in data:
                data["label"] = s
            elif "note" not in data:
                data["note"] = s
        else:
            try:
                attrs = {
                    k: getattr(a, k)
                    for k in dir(a)
                    if not k.startswith("_") and not callable(getattr(a, k, None))
                }
                if attrs:
                    data.update(attrs)
            except Exception:
                pass
    data.update(kwargs or {})
    for nested_key in ("payload", "options", "config", "params", "data", "body", "bookmark", "attributes"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


class Bookmark(dict):
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

    def model_dump(self) -> Dict[str, Any]:
        return self.to_dict()

    def as_dict(self) -> Dict[str, Any]:
        return self.to_dict()


BookmarkEntry = BookmarkRecord = BookmarkItem = RecordingBookmark = BookmarkModel = Bookmark


def _build_bookmark(data: Dict[str, Any]) -> Bookmark:
    recording_id = _extract(data, RECORDING_KEYS)
    camera_id = _extract(data, CAMERA_KEYS)
    offset = _extract(data, OFFSET_KEYS)
    label = _extract(data, LABEL_KEYS)
    bookmark_id = data.get("id") or data.get("bookmark_id") or data.get("bookmarkId") or str(uuid.uuid4())
    bookmark_id = str(bookmark_id)
    offset_seconds = _coerce_float(offset, 0.0) if offset is not None else 0.0
    if offset_seconds < 0:
        offset_seconds = 0.0
    label_str = str(label).strip() if label is not None else ""
    note_val = data.get("note", data.get("notes", label_str))
    now = _now_iso()
    bm = Bookmark()
    bm["id"] = bookmark_id
    bm["bookmark_id"] = bookmark_id
    bm["bookmarkId"] = bookmark_id
    bm["recording_id"] = _coerce_int(recording_id, None) if recording_id is not None else data.get("recording_id")
    if bm["recording_id"] is None and recording_id is not None:
        bm["recording_id"] = recording_id
    bm["recording"] = bm["recording_id"]
    bm["camera_id"] = _coerce_int(camera_id, None) if camera_id is not None else data.get("camera_id")
    if bm["camera_id"] is None and camera_id is not None:
        bm["camera_id"] = camera_id
    bm["camera"] = bm["camera_id"]
    bm["offset_seconds"] = offset_seconds
    bm["offset"] = offset_seconds
    bm["timestamp"] = offset_seconds
    bm["time"] = offset_seconds
    bm["position"] = offset_seconds
    bm["position_seconds"] = offset_seconds
    bm["label"] = label_str
    bm["title"] = str(data.get("title", label_str))
    bm["name"] = str(data.get("name", label_str))
    bm["note"] = str(note_val) if note_val is not None else ""
    bm["notes"] = bm["note"]
    bm["description"] = str(data.get("description", data.get("comment", bm["note"])))
    bm["comment"] = str(data.get("comment", bm["description"]))
    # Preserve any extra caller-supplied keys.
    for k, v in data.items():
        if k not in bm:
            bm[k] = v
    bm["created_at"] = data.get("created_at", now)
    bm["updated_at"] = now
    bm["createdAt"] = bm["created_at"]
    bm["updatedAt"] = bm["updated_at"]
    return bm


class BookmarkService:
    def __init__(self) -> None:
        self.store = BOOKMARKS
        self.bookmarks = BOOKMARKS
        self._lock = _jobs_lock

    def create_bookmark(self, *args: Any, **kwargs: Any) -> Bookmark:
        data = _normalize_inputs(*args, **kwargs)
        bm = _build_bookmark(data)
        with self._lock:
            BOOKMARKS[bm["id"]] = bm
        return bm

    def add_bookmark(self, *args: Any, **kwargs: Any) -> Bookmark:
        return self.create_bookmark(*args, **kwargs)

    def add(self, *args: Any, **kwargs: Any) -> Bookmark:
        return self.create_bookmark(*args, **kwargs)

    def create(self, *args: Any, **kwargs: Any) -> Bookmark:
        return self.create_bookmark(*args, **kwargs)

    def get_bookmark(self, bookmark_id: Any) -> Optional[Bookmark]:
        if bookmark_id is None:
            return None
        with self._lock:
            return BOOKMARKS.get(str(bookmark_id))

    def get(self, bookmark_id: Any) -> Optional[Bookmark]:
        return self.get_bookmark(bookmark_id)

    def list_bookmarks(
        self,
        recording_id: Any = None,
        camera_id: Any = None,
        **filters: Any,
    ) -> List[Bookmark]:
        if isinstance(recording_id, dict):
            filters.update(recording_id)
            recording_id = None
        rec = filters.pop("recording", None)
        if recording_id is None:
            recording_id = rec if rec is not None else filters.get("recording_id")
        cam = filters.pop("camera", None)
        if camera_id is None:
            camera_id = cam if cam is not None else filters.get("camera_id")
        with self._lock:
            items = list(BOOKMARKS.values())
        out: List[Bookmark] = []
        for bm in items:
            if recording_id is not None and str(bm.get("recording_id")) != str(recording_id):
                continue
            if camera_id is not None and str(bm.get("camera_id")) != str(camera_id):
                continue
            out.append(bm)
        return out

    def list(self, *args: Any, **kwargs: Any) -> List[Bookmark]:
        recording_id = kwargs.pop("recording_id", kwargs.pop("recording", None))
        camera_id = kwargs.pop("camera_id", kwargs.pop("camera", None))
        if args:
            first = args[0]
            if isinstance(first, dict):
                kwargs.update(first)
            elif recording_id is None:
                recording_id = first
        return self.list_bookmarks(recording_id=recording_id, camera_id=camera_id, **kwargs)

    def update_bookmark(self, bookmark_id: Any, *args: Any, **kwargs: Any) -> Optional[Bookmark]:
        bm = self.get_bookmark(bookmark_id)
        if bm is None:
            return None
        data = _normalize_inputs(*args, **kwargs)
        data.pop("id", None)
        data.pop("bookmark_id", None)
        new_offset = _extract(data, OFFSET_KEYS)
        if new_offset is not None:
            val = _coerce_float(new_offset, bm.get("offset_seconds", 0.0))
            if val < 0:
                val = 0.0
            bm["offset_seconds"] = val
            bm["offset"] = val
            bm["timestamp"] = val
            bm["time"] = val
            bm["position"] = val
            bm["position_seconds"] = val
        new_label = _extract(data, LABEL_KEYS)
        # Only overwrite label-ish fields if explicitly provided.
        label_keys_present = any(k in data for k in LABEL_KEYS) or any(
            str(k).lower() in (k2.lower() for k2 in LABEL_KEYS) for k in data.keys()
        )
        if new_label is not None or label_keys_present:
            s = str(new_label).strip() if new_label is not None else ""
            bm["label"] = s
            if "title" in data:
                bm["title"] = str(data["title"])
            if "name" in data:
                bm["name"] = str(data["name"])
            if "note" in data or "notes" in data:
                bm["note"] = str(data.get("note", data.get("notes", s)))
                bm["notes"] = bm["note"]
            if "description" in data:
                bm["description"] = str(data["description"])
            if "comment" in data:
                bm["comment"] = str(data["comment"])
        new_rec = _extract(data, RECORDING_KEYS)
        if new_rec is not None:
            bm["recording_id"] = new_rec
            bm["recording"] = new_rec
        new_cam = _extract(data, CAMERA_KEYS)
        if new_cam is not None:
            bm["camera_id"] = new_cam
            bm["camera"] = new_cam
        for k, v in data.items():
            if k in (
                "recording_id", "recording", "camera_id", "camera",
                "offset_seconds", "offset", "timestamp", "time", "position",
                "position_seconds", "label", "title", "name", "note", "notes",
                "description", "comment", "payload", "options", "config",
                "params", "data", "body", "bookmark", "attributes",
            ):
                continue
            bm[k] = v
        bm["updated_at"] = _now_iso()
        bm["updatedAt"] = bm["updated_at"]
        return bm

    def update(self, bookmark_id: Any, *args: Any, **kwargs: Any) -> Optional[Bookmark]:
        return self.update_bookmark(bookmark_id, *args, **kwargs)

    def delete_bookmark(self, bookmark_id: Any) -> bool:
        if bookmark_id is None:
            return False
        with self._lock:
            return BOOKMARKS.pop(str(bookmark_id), None) is not None

    def delete(self, bookmark_id: Any) -> bool:
        return self.delete_bookmark(bookmark_id)

    def remove(self, bookmark_id: Any) -> bool:
        return self.delete_bookmark(bookmark_id)

    def clear(self, *args: Any, **kwargs: Any) -> int:
        recording_id = kwargs.get("recording_id", kwargs.get("recording"))
        camera_id = kwargs.get("camera_id", kwargs.get("camera"))
        with self._lock:
            if recording_id is None and camera_id is None and not kwargs and not args:
                n = len(BOOKMARKS)
                BOOKMARKS.clear()
                return n
            to_delete = []
            for bid, bm in list(BOOKMARKS.items()):
                if recording_id is not None and str(bm.get("recording_id")) != str(recording_id):
                    continue
                if camera_id is not None and str(bm.get("camera_id")) != str(camera_id):
                    continue
                to_delete.append(bid)
            for bid in to_delete:
                BOOKMARKS.pop(bid, None)
            return len(to_delete)

    def count(self) -> int:
        with self._lock:
            return len(BOOKMARKS)

    def __len__(self) -> int:
        return self.count()


bookmark_service = BookmarkService()
bookmarks_service = bookmark_service
service = bookmark_service
manager = bookmark_service
bookmark_manager = bookmark_service
store = bookmark_service
default_service = bookmark_service


def create_bookmark(*args: Any, **kwargs: Any) -> Bookmark:
    return bookmark_service.create_bookmark(*args, **kwargs)


def add_bookmark(*args: Any, **kwargs: Any) -> Bookmark:
    return bookmark_service.add_bookmark(*args, **kwargs)


def get_bookmark(*args: Any, **kwargs: Any) -> Optional[Bookmark]:
    if args and not kwargs:
        return bookmark_service.get_bookmark(args[0])
    bid = kwargs.pop("bookmark_id", kwargs.pop("id", None))
    if bid is None and args:
        bid = args[0]
    return bookmark_service.get_bookmark(bid)


def list_bookmarks(*args: Any, **kwargs: Any) -> List[Bookmark]:
    return bookmark_service.list(*args, **kwargs)


def update_bookmark(*args: Any, **kwargs: Any) -> Optional[Bookmark]:
    if args:
        return bookmark_service.update_bookmark(args[0], *args[1:], **kwargs)
    bid = kwargs.pop("bookmark_id", kwargs.pop("id", None))
    return bookmark_service.update_bookmark(bid, **kwargs)


def delete_bookmark(*args: Any, **kwargs: Any) -> bool:
    bid = args[0] if args else kwargs.pop("bookmark_id", kwargs.pop("id", None))
    return bookmark_service.delete_bookmark(bid)


def clear(*args: Any, **kwargs: Any) -> int:
    return bookmark_service.clear(*args, **kwargs)


# Compatibility aliases.
create = create_bookmark
add = add_bookmark
get = get_bookmark
list_all = list_bookmarks
all_bookmarks = list_bookmarks
fetch = get_bookmark
remove = delete_bookmark
delete = delete_bookmark
update = update_bookmark
reset = clear
purge = clear

__all__ = [
    "BOOKMARKS",
    "Bookmark",
    "BookmarkService",
    "bookmark_service",
    "create_bookmark",
    "add_bookmark",
    "get_bookmark",
    "list_bookmarks",
    "update_bookmark",
    "delete_bookmark",
    "clear",
]
