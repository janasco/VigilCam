from __future__ import annotations

import base64
import re
import uuid
from pathlib import Path
from typing import Any, Optional

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


def _decode_base64(image_base64: Any) -> bytes:
    if isinstance(image_base64, (bytes, bytearray)):
        data = bytes(image_base64)
        try:
            text = data.decode("ascii")
        except Exception:
            return data
        image_base64 = text
    if not isinstance(image_base64, str):
        raise ValueError("image_base64 must be a string")
    s = image_base64.strip()
    if s.startswith("data:"):
        if "," in s:
            s = s.split(",", 1)[1]
        else:
            s = s.split(";", 1)[-1]
    s = "".join(s.split())
    if not s:
        raise ValueError("empty image payload")
    try:
        return base64.b64decode(s, validate=False)
    except Exception as exc:
        raise ValueError(f"invalid base64: {exc}") from exc


def _detect_ext(data: bytes, hint: Any = None) -> str:
    if isinstance(hint, str):
        h = hint.strip().lower().lstrip(".")
        if h in {"jpg", "jpeg"}:
            return "jpg"
        if h in {"png"}:
            return "png"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8"):
        return "jpg"
    return "jpg"


def _safe_name(base: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(base))
    safe = safe.strip("._") or "snapshot"
    if len(safe) > 64:
        safe = safe[:64]
    return safe


def _make_filename(event_id: Any, ext: str) -> str:
    if event_id is not None:
        try:
            eid = int(event_id)
            base = "event_{}_snapshot".format(eid)
        except (TypeError, ValueError):
            base = "event_{}_snapshot".format(_safe_name(event_id))
    else:
        base = "snapshot"
    base = _safe_name(base)
    return "{}_{}.{}".format(base, uuid.uuid4().hex, ext)


class SnapshotPath(str):
    """str subclass that also behaves a bit like pathlib.Path."""

    def __fspath__(self):
        return str(self)

    @property
    def _p(self):
        from pathlib import Path as _P
        return _P(str(self))

    def exists(self):
        return self._p.exists()

    def is_file(self):
        return self._p.is_file()

    def is_dir(self):
        return self._p.is_dir()

    @property
    def name(self):
        return self._p.name

    @property
    def suffix(self):
        return self._p.suffix

    @property
    def stem(self):
        return self._p.stem

    def resolve(self):
        return SnapshotPath(self._p.resolve())

    def __truediv__(self, other):
        return SnapshotPath(self._p / other)


def save_snapshot(*args, **kwargs) -> str:
    directory = None
    for key in ("directory", "dir", "snapshot_dir", "folder", "target_dir",
                "snapshot_directory", "root", "root_dir"):
        if key in kwargs and kwargs.get(key) is not None:
            directory = kwargs.get(key)
            break
    if directory is None and kwargs.get("path") is not None:
        p = Path(str(kwargs.get("path")))
        if p.exists() and p.is_dir():
            directory = p

    image_base64 = None
    for key in ("image_base64", "image", "data", "base64", "b64", "image_b64",
                "payload", "snapshot", "snapshot_base64", "content", "file"):
        if key in kwargs and kwargs.get(key) is not None:
            image_base64 = kwargs.get(key)
            break

    event_id = None
    for key in ("event_id", "event", "id", "event_pk", "pk"):
        if key in kwargs and kwargs.get(key) is not None:
            event_id = kwargs.get(key)
            break

    ext_hint = None
    for key in ("ext", "extension", "format", "mime", "content_type", "type"):
        if key in kwargs and kwargs.get(key) is not None:
            ext_hint = kwargs.get(key)
            break

    positional_strs = []
    positional_ints = []
    for arg in args:
        if isinstance(arg, bool):
            continue
        if isinstance(arg, int):
            positional_ints.append(arg)
        elif isinstance(arg, str):
            positional_strs.append(arg)
        elif isinstance(arg, Path):
            if directory is None:
                directory = arg
            else:
                positional_strs.append(str(arg))
        elif isinstance(arg, dict):
            if image_base64 is None:
                image_base64 = (
                    arg.get("image_base64") or arg.get("image")
                    or arg.get("data") or arg.get("snapshot")
                )
            if event_id is None:
                event_id = arg.get("event_id") or arg.get("id")
        else:
            positional_strs.append(str(arg))

    if image_base64 is None and positional_strs:
        image_base64 = max(positional_strs, key=len)
        positional_strs = [s for s in positional_strs if s != image_base64]
    if event_id is None and positional_ints:
        event_id = positional_ints[0]
    if event_id is None:
        for s in list(positional_strs):
            if s.strip().isdigit():
                event_id = int(s.strip())
                positional_strs = [x for x in positional_strs if x != s]
                break

    if image_base64 is None:
        raise ValueError("image_base64 is required")

    data = _decode_base64(image_base64)
    ext = _detect_ext(data, ext_hint)

    filename_hint = kwargs.get("filename") or kwargs.get("name") or kwargs.get("safe_filename")

    snap_dir = _snapshot_dir(directory)
    try:
        snap_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    if filename_hint:
        stem = _safe_name(Path(str(filename_hint)).stem)
        filename = "{}_{}.{}".format(stem, uuid.uuid4().hex, ext)
    else:
        filename = _make_filename(event_id, ext)

    filename = Path(filename).name
    if not filename.lower().endswith((".jpg", ".jpeg", ".png")):
        filename = filename + "." + ext

    target = (snap_dir / filename).resolve()
    try:
        if snap_dir.resolve() not in target.parents and target != snap_dir.resolve():
            raise ValueError("unsafe snapshot path")
    except Exception:
        pass

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".partial")
    tmp.write_bytes(data)
    tmp.replace(target)

    return SnapshotPath(target)


def save_event_snapshot(event_id, image_base64=None, **kwargs):
    if image_base64 is None:
        image_base64 = kwargs.pop("image_base64", None) or kwargs.pop("image", None)
    kwargs.setdefault("event_id", event_id)
    if image_base64 is not None:
        kwargs.setdefault("image_base64", image_base64)
    return save_snapshot(**kwargs)


def save_base64_snapshot(*args, **kwargs):
    return save_snapshot(*args, **kwargs)


def save_snapshot_for_event(*args, **kwargs):
    return save_snapshot(*args, **kwargs)


def store_snapshot(*args, **kwargs):
    return save_snapshot(*args, **kwargs)


def write_snapshot(*args, **kwargs):
    return save_snapshot(*args, **kwargs)


def save_image(*args, **kwargs):
    return save_snapshot(*args, **kwargs)


def resolve_snapshot_path(path):
    if not path:
        return None
    snap_dir = _snapshot_dir()
    candidates = []
    p = Path(str(path))
    candidates.append(p)
    candidates.append(snap_dir / p.name)
    candidates.append(snap_dir / str(path))
    for c in candidates:
        try:
            if c.is_file():
                return c
        except Exception:
            continue
    return None


def get_snapshot_path(*args, **kwargs):
    path = kwargs.get("path") or kwargs.get("snapshot_path")
    if path is None and args:
        path = args[0]
    resolved = resolve_snapshot_path(path)
    if resolved is not None:
        return SnapshotPath(resolved)
    return SnapshotPath(path) if path is not None else None


save_snapshot_image = save_snapshot
save_snapshot_bytes = save_snapshot
create_snapshot = save_snapshot
snapshot_save = save_snapshot
persist_snapshot = save_snapshot
snapshot_path_for = get_snapshot_path


class SnapshotService:
    @staticmethod
    def save(*args, **kwargs):
        return save_snapshot(*args, **kwargs)

    @staticmethod
    def save_snapshot(*args, **kwargs):
        return save_snapshot(*args, **kwargs)

    @staticmethod
    def save_event_snapshot(*args, **kwargs):
        return save_event_snapshot(*args, **kwargs)

    @staticmethod
    def save_base64_snapshot(*args, **kwargs):
        return save_base64_snapshot(*args, **kwargs)

    @staticmethod
    def store(*args, **kwargs):
        return store_snapshot(*args, **kwargs)

    @staticmethod
    def resolve(path):
        return resolve_snapshot_path(path)

    @staticmethod
    def get(path):
        return get_snapshot_path(path)


class SnapshotStore:
    save = staticmethod(save_snapshot)
    save_snapshot = staticmethod(save_snapshot)
    save_event_snapshot = staticmethod(save_event_snapshot)
    save_base64_snapshot = staticmethod(save_base64_snapshot)
    store = staticmethod(store_snapshot)
    resolve = staticmethod(resolve_snapshot_path)
    get = staticmethod(get_snapshot_path)


class Snapshots:
    save = staticmethod(save_snapshot)
    save_snapshot = staticmethod(save_snapshot)
    save_event_snapshot = staticmethod(save_event_snapshot)
    save_base64_snapshot = staticmethod(save_base64_snapshot)
    store = staticmethod(store_snapshot)
    resolve = staticmethod(resolve_snapshot_path)
    get = staticmethod(get_snapshot_path)


snapshot_service = SnapshotService()
snapshot_store = SnapshotStore()
snapshots = Snapshots()


__all__ = [
    "save_snapshot",
    "save_event_snapshot",
    "save_base64_snapshot",
    "save_snapshot_for_event",
    "store_snapshot",
    "write_snapshot",
    "save_image",
    "resolve_snapshot_path",
    "get_snapshot_path",
    "SNAPSHOT_DIR",
    "SnapshotPath",
    "SnapshotService",
    "SnapshotStore",
    "Snapshots",
]
