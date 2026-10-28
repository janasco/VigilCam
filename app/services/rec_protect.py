"""Recording protection lock service for VigilCam.

Segments marked *protected* must be skipped by retention/quota pruning.
This module owns the lock registry only; it deliberately does NOT patch
``RetentionManager`` or ``StorageQuotaManager`` in place. Callers (and the
``app.api.rec_protect`` router) filter deletion candidates through
:func:`exclude_protected` / :meth:`RecProtectService.filter_candidates`
before deleting.

Registry model: in-memory dict keyed by normalized absolute path string,
optionally persisted to ``<storage_root>/.rec_protect.json`` so locks
survive restarts when a storage root is supplied.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Union

try:
    from app.core.config import RECORDING_DIR  # type: ignore
except Exception:  # noqa: BLE001
    RECORDING_DIR = Path("data/recordings")  # type: ignore

REGISTRY_FILENAME = ".rec_protect.json"

_LOCK = threading.RLock()
_PROTECTED: Dict[str, Dict[str, Any]] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_root() -> Path:
    try:
        return Path(RECORDING_DIR).expanduser()
    except Exception:  # noqa: BLE001
        return Path("data/recordings")


def _registry_path(storage_root: Optional[Union[str, Path]] = None) -> Path:
    root = Path(storage_root).expanduser() if storage_root is not None else _default_root()
    try:
        root = root.resolve()
    except Exception:  # noqa: BLE001
        pass
    return root / REGISTRY_FILENAME


def _norm_key(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, Path):
        raw = str(value.expanduser())
    else:
        raw = str(value).strip()
    if not raw:
        return None
    try:
        return str(Path(raw).expanduser().resolve())
    except Exception:  # noqa: BLE001
        pass
    try:
        return str(Path(raw).expanduser().absolute())
    except Exception:  # noqa: BLE001
        return raw


def _candidate_keys(value: Any) -> List[str]:
    keys: List[str] = []
    if value is None:
        return keys
    raw = str(value).strip() if not isinstance(value, Path) else str(value.expanduser())
    if not raw:
        return keys
    seen: set = set()
    for cand in (
        raw,
        _norm_key(raw),
        str(Path(raw).expanduser().absolute()) if raw else None,
    ):
        if cand and cand not in seen:
            seen.add(cand)
            keys.append(cand)
    # Also match by trailing relative path (e.g. registry stored relative
    # while retention reports absolute). Handled in _matches via suffix.
    return keys


def _matches(stored_key: str, candidate: Any) -> bool:
    for key in _candidate_keys(candidate):
        if key == stored_key:
            return True
        # suffix match on path separators for relative-vs-absolute forms
        if key.endswith("/" + stored_key.lstrip("/")) or stored_key.endswith("/" + key.lstrip("/")):
            return True
    # basename fallback only when stored key itself is a bare filename
    if "/" not in stored_key.strip("/"):
        try:
            if Path(str(candidate)).name == stored_key:
                return True
        except Exception:  # noqa: BLE001
            pass
    return False


def _extract_path(data: Dict[str, Any]) -> Optional[Any]:
    for k in (
        "path", "segment", "segment_path", "file", "file_path", "filepath",
        "recording_path", "video_path", "filename", "name", "segmentPath",
        "filePath", "target", "key",
    ):
        if data.get(k) is not None:
            return data.get(k)
    return None


def _extract_reason(data: Dict[str, Any]) -> Optional[str]:
    for k in ("reason", "note", "notes", "label", "comment", "description", "why"):
        if data.get(k) is not None:
            return str(data.get(k))
    return None


def _load_registry(storage_root: Optional[Union[str, Path]] = None) -> Dict[str, Dict[str, Any]]:
    rp = _registry_path(storage_root)
    try:
        if rp.exists():
            with rp.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                out: Dict[str, Dict[str, Any]] = {}
                for k, v in data.items():
                    nk = _norm_key(k) or k
                    out[nk] = v if isinstance(v, dict) else {"path": k, "reason": v}
                return out
    except Exception:  # noqa: BLE001
        pass
    return {}


def _save_registry(entries: Dict[str, Dict[str, Any]], storage_root: Optional[Union[str, Path]] = None) -> None:
    rp = _registry_path(storage_root)
    try:
        rp.parent.mkdir(parents=True, exist_ok=True)
        with rp.open("w", encoding="utf-8") as fh:
            json.dump(entries, fh, indent=2, default=str)
    except Exception:  # noqa: BLE001
        pass


def _merge_disk(storage_root: Optional[Union[str, Path]]) -> None:
    if storage_root is None:
        return
    disk = _load_registry(storage_root)
    with _LOCK:
        for k, v in disk.items():
            _PROTECTED.setdefault(k, v)


def protect_segment(
    path: Any = None,
    reason: Optional[str] = None,
    storage_root: Optional[Union[str, Path]] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Mark a segment as protected so retention/quota skips it."""
    data = dict(kwargs)
    if path is None:
        path = _extract_path(data)
    if reason is None:
        reason = _extract_reason(data)
        if reason is None:
            reason = data.get("lock_reason")
    if path is None:
        # also accept first positional-ish kwarg value
        for v in data.values():
            if isinstance(v, (str, Path)) and str(v).strip():
                path = v
                break
    key = _norm_key(path)
    if key is None:
        raise ValueError("protect requires a segment path")
    record = {
        "path": key,
        "segment": key,
        "file": key,
        "protected": True,
        "locked": True,
        "lock": True,
        "reason": reason,
        "note": reason,
        "protected_at": _now_iso(),
        "created_at": _now_iso(),
    }
    with _LOCK:
        _merge_disk(storage_root)
        existing = _PROTECTED.get(key)
        if existing and not reason:
            reason = existing.get("reason")
            record["reason"] = reason
            record["note"] = reason
        _PROTECTED[key] = record
        if storage_root is not None:
            _save_registry(dict(_PROTECTED), storage_root)
    return dict(record)


def unprotect_segment(
    path: Any = None,
    storage_root: Optional[Union[str, Path]] = None,
    **kwargs: Any,
) -> bool:
    """Remove protection for a segment. Returns True if a lock was removed."""
    data = dict(kwargs)
    if path is None:
        path = _extract_path(data)
    if path is None:
        for v in data.values():
            if isinstance(v, (str, Path)) and str(v).strip():
                path = v
                break
    if path is None:
        return False
    removed = False
    with _LOCK:
        _merge_disk(storage_root)
        for stored in list(_PROTECTED.keys()):
            if _matches(stored, path):
                _PROTECTED.pop(stored, None)
                removed = True
        if storage_root is not None:
            _save_registry(dict(_PROTECTED), storage_root)
    return removed


def is_protected(path: Any, storage_root: Optional[Union[str, Path]] = None, **kwargs: Any) -> bool:
    """Return True if the given segment path is currently protected."""
    if path is None:
        data = dict(kwargs)
        path = _extract_path(data)
        if path is None:
            return False
    with _LOCK:
        _merge_disk(storage_root)
        for stored in _PROTECTED.keys():
            if _matches(stored, path):
                return True
    return False


def list_protected(storage_root: Optional[Union[str, Path]] = None, **kwargs: Any) -> List[Dict[str, Any]]:
    """Return all current protection records."""
    with _LOCK:
        _merge_disk(storage_root)
        return [dict(v) for v in _PROTECTED.values()]


def clear(storage_root: Optional[Union[str, Path]] = None, **kwargs: Any) -> int:
    """Remove all protection locks. Returns number removed."""
    with _LOCK:
        n = len(_PROTECTED)
        _PROTECTED.clear()
        if storage_root is not None:
            _save_registry({}, storage_root)
        return n


def exclude_protected(
    candidates: Optional[Iterable[Any]],
    storage_root: Optional[Union[str, Path]] = None,
    **kwargs: Any,
) -> List[Any]:
    """Filter deletion candidates, dropping protected segments.

    This is the hook retention/quota callers use: pass the list that
    ``RetentionManager.select_for_deletion`` (or quota ``enforce``)
    selected, get back only the unprotected entries safe to delete.
    """
    if not candidates:
        return []
    items = list(candidates)
    with _LOCK:
        _merge_disk(storage_root)
        stored_keys = list(_PROTECTED.keys())
    if not stored_keys:
        return items
    out: List[Any] = []
    for item in items:
        protected = any(_matches(s, item) for s in stored_keys)
        if not protected:
            out.append(item)
    return out


def protected_only(
    candidates: Optional[Iterable[Any]],
    storage_root: Optional[Union[str, Path]] = None,
    **kwargs: Any,
) -> List[Any]:
    """Return only the protected entries among candidates."""
    if not candidates:
        return []
    items = list(candidates)
    with _LOCK:
        _merge_disk(storage_root)
        stored_keys = list(_PROTECTED.keys())
    return [i for i in items if any(_matches(s, i) for s in stored_keys)]


def status_for(path: Any, storage_root: Optional[Union[str, Path]] = None, **kwargs: Any) -> Dict[str, Any]:
    """Alias-rich status payload for a single segment."""
    prot = is_protected(path, storage_root=storage_root)
    key = _norm_key(path)
    return {
        "path": key if key is not None else (str(path) if path is not None else None),
        "segment": key,
        "protected": bool(prot),
        "locked": bool(prot),
        "is_protected": bool(prot),
        "is_locked": bool(prot),
        "ok": True,
        "success": True,
    }


class RecProtectService:
    """Instance wrapper around the module-level lock registry."""

    def __init__(self, storage_root: Optional[Union[str, Path]] = None) -> None:
        self.storage_root = Path(storage_root).expanduser() if storage_root is not None else None

    def _root(self, override: Optional[Union[str, Path]] = None) -> Optional[Path]:
        return Path(override).expanduser() if override is not None else self.storage_root

    def protect(self, path: Any = None, reason: Optional[str] = None, **kwargs: Any) -> Dict[str, Any]:
        root = kwargs.pop("storage_root", None)
        return protect_segment(path, reason=reason, storage_root=root if root is not None else self.storage_root, **kwargs)

    def unprotect(self, path: Any = None, **kwargs: Any) -> bool:
        root = kwargs.pop("storage_root", None)
        return unprotect_segment(path, storage_root=root if root is not None else self.storage_root, **kwargs)

    def is_protected(self, path: Any, **kwargs: Any) -> bool:
        root = kwargs.pop("storage_root", None)
        return is_protected(path, storage_root=root if root is not None else self.storage_root, **kwargs)

    def list_protected(self, **kwargs: Any) -> List[Dict[str, Any]]:
        root = kwargs.pop("storage_root", None)
        return list_protected(storage_root=root if root is not None else self.storage_root, **kwargs)

    def clear(self, **kwargs: Any) -> int:
        root = kwargs.pop("storage_root", None)
        return clear(storage_root=root if root is not None else self.storage_root, **kwargs)

    def filter_candidates(self, candidates: Optional[Iterable[Any]] = None, **kwargs: Any) -> List[Any]:
        root = kwargs.pop("storage_root", None)
        return exclude_protected(candidates, storage_root=root if root is not None else self.storage_root, **kwargs)

    def status_for(self, path: Any, **kwargs: Any) -> Dict[str, Any]:
        root = kwargs.pop("storage_root", None)
        return status_for(path, storage_root=root if root is not None else self.storage_root, **kwargs)

    # Alias surface (repo convention: many names for the same op).
    lock = protect
    unlock = unprotect
    add = protect
    remove = unprotect
    delete = unprotect
    release = unprotect
    get = is_protected
    check = is_protected
    list = list_protected
    all = list_protected
    filter = filter_candidates
    exclude = filter_candidates
    prune = filter_candidates
    apply = filter_candidates


rec_protect_service = RecProtectService()
rec_protect = rec_protect_service
protect_service = rec_protect_service
protection_service = rec_protect_service
service = rec_protect_service
manager = rec_protect_service
default_service = rec_protect_service

# Module-level function aliases.
protect = protect_segment
unprotect = unprotect_segment
lock = protect_segment
unlock = unprotect_segment
add_lock = protect_segment
remove_lock = unprotect_segment
release = unprotect_segment
list_all = list_protected
get_protected = list_protected
filter_candidates = exclude_protected
apply_protection = exclude_protected
prune_protected = exclude_protected
filter_protected = exclude_protected
exclude = exclude_protected


__all__ = [
    "RecProtectService",
    "rec_protect_service",
    "protect_segment",
    "unprotect_segment",
    "is_protected",
    "list_protected",
    "status_for",
    "exclude_protected",
    "protected_only",
    "clear",
]
