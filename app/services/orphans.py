"""Orphaned-files finder for VigilCam.

Finds recording files present on disk but missing from the DB.

This is intentionally distinct from neighbouring services:

* ``app.services.integrity`` checks file validity (headers, sizes, ffprobe).
* ``app.services.gap_detector`` finds *missing time ranges* in segment coverage.
* ``app.services.retention`` enforces quotas (max age / max total bytes).

This module does none of that. It only answers the reference question:
given the files on disk under the recording root and the paths referenced
by the ``recordings`` table, which disk files are *orphaned* (not referenced)?

A disk file counts as *referenced* when any DB path:

* equals the file path (absolute-normalized comparison), or
* equals the file name (basename comparison, covers relative DB paths), or
* is a directory that contains the file (``Recording.path`` is sometimes a
  camera directory, see ``app.api.recordings.start_recording``).

Callers may supply the DB reference set explicitly (``known_paths`` /
``db_records`` / ``db_paths``) for pure-sync use, or pass an async SQLAlchemy
session to the ``*_async`` helpers which query ``Recording.path`` themselves.
When no DB information is available the scan still runs but reports
``db_available=False`` and treats every disk file as an orphan candidate.

Standard library only in the sync path; SQLAlchemy imports are lazy.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

try:
    from app.core.config import RECORDING_DIR  # type: ignore
except Exception:  # noqa: BLE001
    RECORDING_DIR = Path("data/recordings")  # type: ignore

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_EXTENSIONS = {".mp4"}
VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".mkv", ".avi"}

_PATH_KEYS = (
    "path", "file", "filepath", "filename", "storage_path", "root_path",
    "storage_root", "root", "directory", "dir", "folder", "location",
    "target_dir", "target", "scan_path", "recordings_dir", "recordings_path",
    "base_dir",
)

_KNOWN_KEYS = (
    "known_paths", "knownPaths", "known_files", "knownFiles", "known",
    "db_paths", "dbPaths", "db_records", "dbRecords", "db_rows",
    "referenced_paths", "referencedPaths", "referenced_files",
    "referenced", "references", "recordings", "recording_paths",
    "recordingPaths", "whitelist", "allowlist", "expected_paths",
    "expectedPaths", "indexed_paths", "indexedPaths",
)

_DELETE_KEYS = (
    "delete", "remove", "cleanup", "prune", "purge", "erase",
    "delete_orphans", "deleteOrphans", "remove_orphans", "auto_delete",
    "autoDelete", "destroy",
)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _coerce_path(value: Any) -> Optional[Path]:
    if value is None:
        return None
    if isinstance(value, Path):
        return value.expanduser()
    if isinstance(value, (bytes, bytearray)):
        try:
            return Path(value.decode())
        except Exception:  # noqa: BLE001
            return Path(str(value))
    if isinstance(value, dict):
        for k in _PATH_KEYS:
            if value.get(k) is not None:
                p = _coerce_path(value.get(k))
                if p is not None:
                    return p
        return None
    for attr in ("path", "file", "filepath", "storage_path", "root", "directory"):
        if hasattr(value, attr):
            try:
                v = getattr(value, attr)
                if v is not None and v is not value:
                    p = _coerce_path(v)
                    if p is not None:
                        return p
            except Exception:  # noqa: BLE001
                pass
    try:
        s = str(value).strip()
    except Exception:  # noqa: BLE001
        return None
    if not s:
        return None
    return Path(s).expanduser()


def _default_root() -> Path:
    try:
        return Path(str(RECORDING_DIR)).expanduser()
    except Exception:  # noqa: BLE001
        return Path("data/recordings")


def _safe_root(value: Any = None) -> Path:
    p = _coerce_path(value)
    if p is None:
        p = _default_root()
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    return p


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("1", "true", "yes", "y", "on", "enabled", "enable"):
            return True
        if s in ("0", "false", "no", "n", "off", "disabled", "disable"):
            return False
        return default
    return bool(value)


def _norm_abs(value: Any) -> Optional[str]:
    p = _coerce_path(value)
    if p is None:
        return None
    try:
        s = str(p.expanduser())
    except Exception:  # noqa: BLE001
        return None
    if not s.strip():
        return None
    # Normalize without requiring existence: absolute + normpath + casefold
    # trailing separators stripped. Do not resolve() (would touch FS).
    try:
        if not os.path.isabs(s):
            s = os.path.abspath(s)
        s = os.path.normpath(s)
    except Exception:  # noqa: BLE001
        s = s.strip()
    return s


def _basename(value: Any) -> Optional[str]:
    try:
        s = str(value).strip() if not isinstance(value, Path) else value.name
    except Exception:  # noqa: BLE001
        return None
    if not s:
        return None
    # DB may store full paths; compare by final component.
    base = s.replace("\\", "/").rstrip("/").split("/")[-1].strip()
    return base or None


# ---------------------------------------------------------------------------
# Filesystem scanning (no validity/quota checks)
# ---------------------------------------------------------------------------

def _iter_files(root: Path, extensions: Optional[Sequence[str]]) -> List[Path]:
    exts = {str(e).lower() for e in (extensions or DEFAULT_EXTENSIONS)}
    out: List[Path] = []
    try:
        if root.is_file():
            if root.suffix.lower() in exts:
                out.append(root)
            return out
        if not root.exists():
            return out
        for dirpath, _dirnames, filenames in os.walk(root):
            for fn in sorted(filenames):
                fp = Path(dirpath) / fn
                try:
                    if fp.suffix.lower() in exts and fp.is_file():
                        out.append(fp)
                except OSError:
                    continue
    except Exception:  # noqa: BLE001
        pass
    return sorted(out)


def _file_size(path: Path) -> int:
    try:
        return int(path.stat().st_size)
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# Known-path normalization
# ---------------------------------------------------------------------------

def _extract_known_paths(*candidates: Any, extra: Optional[Dict[str, Any]] = None) -> List[str]:
    """Collect raw known-path strings from explicit values + kwarg dict."""
    raw: List[Any] = []
    for cand in candidates:
        if cand is None:
            continue
        if isinstance(cand, (list, tuple, set)):
            raw.extend(list(cand))
        elif isinstance(cand, dict):
            # A single DB row mapping?
            if any(k in cand for k in ("path", "file", "filepath", "filename",
                                       "segment_path", "file_path", "storage_path")):
                for k in ("path", "file", "filepath", "filename",
                          "segment_path", "file_path", "storage_path"):
                    if cand.get(k) is not None:
                        raw.append(cand.get(k))
                        break
            else:
                for v in cand.values():
                    if isinstance(v, (list, tuple, set)):
                        raw.extend(list(v))
                    elif v is not None and not isinstance(v, dict):
                        raw.append(v)
        else:
            raw.append(cand)
    if extra:
        for k in _KNOWN_KEYS:
            if k in extra and extra.get(k) is not None:
                v = extra.get(k)
                if isinstance(v, (list, tuple, set)):
                    raw.extend(list(v))
                elif isinstance(v, dict):
                    for vv in v.values():
                        if isinstance(vv, (list, tuple, set)):
                            raw.extend(list(vv))
                        elif vv is not None:
                            raw.append(vv)
                else:
                    raw.append(v)
    out: List[str] = []
    for item in raw:
        if item is None:
            continue
        if isinstance(item, dict):
            for k in ("path", "file", "filepath", "filename",
                      "segment_path", "file_path", "storage_path"):
                if item.get(k) is not None:
                    item = item.get(k)
                    break
            else:
                continue
        if hasattr(item, "path") and not isinstance(item, (str, Path)):
            try:
                item = getattr(item, "path")
            except Exception:  # noqa: BLE001
                continue
        s = _norm_abs(item)
        if s is None:
            # Fall back to basename for odd values so they still count.
            b = _basename(item)
            if b:
                out.append(b)
            continue
        out.append(s)
        b = _basename(s)
        if b and b not in out:
            # Keep basename too so relative DB paths match; matching logic
            # uses both sets explicitly, but storing both is harmless.
            pass
    # De-duplicate preserving order.
    seen: set = set()
    uniq: List[str] = []
    for s in out:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq


def _build_known_sets(known_abs: Sequence[str]) -> tuple:
    abs_set = set(known_abs)
    base_set = set()
    dir_prefixes: List[str] = []
    for s in known_abs:
        b = _basename(s)
        if b:
            base_set.add(b)
        # Directory prefixes: the known value itself plus its parent dirs.
        # A DB row pointing at a camera directory references everything below.
        dir_prefixes.append(s.rstrip(os.sep) + os.sep)
    return abs_set, base_set, dir_prefixes


def _is_referenced(
    fp: Path,
    abs_set: set,
    base_set: set,
    dir_prefixes: Sequence[str],
) -> bool:
    norm = _norm_abs(fp)
    if norm is not None and norm in abs_set:
        return True
    base = _basename(fp.name)
    if base is not None and base in base_set:
        # Basename match: covers DB rows storing relative names. This is
        # intentionally lenient; absolute mismatches across different
        # directories with identical names are rare for timestamped segments.
        # To reduce false-negatives from directory moves, accept it.
        # Exact-directory callers can pass absolute DB paths for strictness.
        return True
    if norm is not None:
        with_sep = norm + os.sep
        for prefix in dir_prefixes:
            # prefix already ends with sep; match if file is under it or
            # if a DB dir path equals the file's parent chain.
            if norm.startswith(prefix) or prefix.startswith(with_sep):
                # Second clause: DB value is a file under the same dir is
                # already covered by abs/base; this handles DB dir == file's
                # ancestor via string prefix on normalized absolute paths.
                if norm.startswith(prefix):
                    return True
    # Also check: any known abs value that is an existing directory containing fp.
    # Covered by prefix check above when known values are absolute.
    return False


# ---------------------------------------------------------------------------
# DB access (lazy; never raises)
# ---------------------------------------------------------------------------

async def _fetch_db_paths_async(db: Any = None) -> Optional[List[str]]:
    """Return ``Recording.path`` values from the DB, or ``None`` when unavailable."""
    sess = db
    close_after = False
    if sess is None:
        try:
            from app.core.database import SessionLocal as _SessionLocal
            sess = _SessionLocal()
            close_after = True
        except Exception:  # noqa: BLE001
            return None
    try:
        from sqlalchemy import select as _select
        from app.models.entities import Recording as _Recording
        result = await sess.execute(_select(_Recording.path))
        paths = [r for r in result.scalars().all() if r]
        return [str(p) for p in paths]
    except Exception:  # noqa: BLE001
        return None
    finally:
        try:
            if close_after and sess is not None and hasattr(sess, "close"):
                await sess.close()
        except Exception:  # noqa: BLE001
            pass


def _coerce_db_records_sync(value: Any) -> Optional[List[str]]:
    if value is None:
        return None
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        # Async session objects are not iterable record lists; bail.
        if items and hasattr(items[0], "execute"):
            return None
        return [str(v) if not isinstance(v, str) else v for v in items if v is not None]
    if hasattr(value, "execute"):
        return None
    return None


# ---------------------------------------------------------------------------
# Report building
# ---------------------------------------------------------------------------

def _build_report(
    root: Path,
    files: Sequence[Path],
    orphan_paths: List[str],
    referenced_paths: List[str],
    known_abs: List[str],
    db_available: bool,
    db_count: Optional[int],
    deleted: Optional[List[str]] = None,
) -> Dict[str, Any]:
    deleted = list(deleted or [])
    total = len(files)
    orphan_count = len(orphan_paths)
    referenced_count = len(referenced_paths)
    total_bytes = sum(_file_size(Path(p)) for p in [str(f) for f in files])
    orphan_bytes = 0
    for p in orphan_paths:
        try:
            orphan_bytes += int(Path(p).stat().st_size)
        except OSError:
            continue
    status = "ok" if orphan_count == 0 else "has-orphans"
    results = [
        {
            "path": str(f),
            "file": str(f),
            "orphan": str(f) in set(orphan_paths),
            "referenced": str(f) in set(referenced_paths),
            "size_bytes": _file_size(f),
            "size": _file_size(f),
        }
        for f in files
    ]
    report: Dict[str, Any] = {
        "status": status,
        "state": status,
        "ok": orphan_count == 0,
        "success": True,
        "healthy": orphan_count == 0,
        "has_orphans": orphan_count > 0,
        "orphans_found": orphan_count > 0,
        "root": str(root),
        "storage_root": str(root),
        "directory": str(root),
        "path": str(root),
        "total": total,
        "count": total,
        "scanned": total,
        "file_count": total,
        "orphan_count": orphan_count,
        "orphans_count": orphan_count,
        "num_orphans": orphan_count,
        "total_orphans": orphan_count,
        "referenced_count": referenced_count,
        "referenced_files_count": referenced_count,
        "known_count": len(known_abs),
        "db_count": db_count,
        "db_available": db_available,
        "db": db_available,
        "orphans": list(orphan_paths),
        "orphan_files": list(orphan_paths),
        "orphan_paths": list(orphan_paths),
        "unreferenced": list(orphan_paths),
        "unreferenced_files": list(orphan_paths),
        "missing_from_db": list(orphan_paths),
        "files_missing_from_db": list(orphan_paths),
        "referenced": list(referenced_paths),
        "referenced_files": list(referenced_paths),
        "known_paths": list(known_abs),
        "results": results,
        "files": results,
        "details": results,
        "total_bytes": int(total_bytes),
        "total_size": int(total_bytes),
        "orphan_bytes": int(orphan_bytes),
        "orphan_size": int(orphan_bytes),
        "orphan_total_bytes": int(orphan_bytes),
        "deleted": list(deleted),
        "deleted_files": list(deleted),
        "deleted_count": len(deleted),
        "removed": list(deleted),
        "removed_count": len(deleted),
    }
    return report


# ---------------------------------------------------------------------------
# Core scan
# ---------------------------------------------------------------------------

def find_orphans(
    root: Any = None,
    known_paths: Any = None,
    known_files: Any = None,
    db_records: Any = None,
    db_paths: Any = None,
    referenced_paths: Any = None,
    db: Any = None,
    session: Any = None,
    extensions: Optional[Sequence[str]] = None,
    delete: Any = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Find disk files under ``root`` that are missing from the DB.

    Provide the DB reference set explicitly via any of ``known_paths``,
    ``db_records``, ``db_paths`` or ``referenced_paths`` (each accepts a list
    of path strings and/or ``{"path": ...}`` row mappings). Async sessions
    must use :func:`find_orphans_async` instead; passing one here is ignored
    (reported as ``db_available=False``) and never raises.
    """
    if root is None:
        for k in _PATH_KEYS:
            if k in kwargs and kwargs.get(k) is not None:
                root = kwargs.get(k)
                break
    scan_root = _safe_root(root)
    if extensions is None:
        for k in ("extensions", "exts", "suffixes", "video_extensions"):
            if k in kwargs and kwargs[k] is not None:
                extensions = kwargs[k]
                break
    if isinstance(extensions, str):
        extensions = [extensions]

    # Gather explicit known paths from every alias.
    explicit_cands: List[Any] = []
    for cand in (known_paths, known_files, db_records, db_paths, referenced_paths):
        coerced = _coerce_db_records_sync(cand)
        if coerced is not None:
            explicit_cands.extend(coerced)
        elif cand is not None and not hasattr(cand, "execute"):
            # Single string / mapping.
            explicit_cands.append(cand)
    for k in _KNOWN_KEYS:
        if k in kwargs and kwargs.get(k) is not None:
            coerced = _coerce_db_records_sync(kwargs.get(k))
            if coerced is not None:
                explicit_cands.extend(coerced)
            else:
                v = kwargs.get(k)
                if v is not None and not hasattr(v, "execute"):
                    explicit_cands.append(v)
    # db/session passed synchronously: only honour when they look like lists.
    for cand in (db, session, kwargs.get("db"), kwargs.get("session")):
        coerced = _coerce_db_records_sync(cand)
        if coerced is not None:
            explicit_cands.extend(coerced)

    known_abs = _extract_known_paths(*explicit_cands)
    db_available = len(known_abs) > 0
    # Explicit empty list means "DB queried, nothing referenced".
    explicit_empty = any(
        v is not None and isinstance(v, (list, tuple, set)) and len(v) == 0
        for v in (known_paths, known_files, db_records, db_paths,
                  referenced_paths, kwargs.get("known_paths"),
                  kwargs.get("db_records"), kwargs.get("db_paths"))
    )
    if explicit_empty:
        db_available = True
    db_count: Optional[int] = len(known_abs) if db_available else None

    do_delete = bool(delete)
    for k in _DELETE_KEYS:
        if k in kwargs and kwargs[k] is not None:
            do_delete = _as_bool(kwargs[k], default=False) or do_delete
            break
    if isinstance(delete, str):
        do_delete = _as_bool(delete)

    files = _iter_files(scan_root, list(extensions) if extensions else None)
    abs_set, base_set, dir_prefixes = _build_known_sets(known_abs)

    orphan_paths: List[str] = []
    referenced: List[str] = []
    if not db_available:
        # No DB info: every file is an orphan candidate.
        orphan_paths = [str(f) for f in files]
    else:
        for fp in files:
            if _is_referenced(fp, abs_set, base_set, dir_prefixes):
                referenced.append(str(fp))
            else:
                orphan_paths.append(str(fp))

    deleted: List[str] = []
    if do_delete:
        for p in list(orphan_paths):
            try:
                Path(p).unlink()
                deleted.append(p)
            except OSError:
                continue
        # Recompute remaining orphans after deletion.
        remaining = [p for p in orphan_paths if p not in set(deleted)]
        return _build_report(scan_root, files, remaining, referenced,
                             known_abs, db_available, db_count, deleted)

    return _build_report(scan_root, files, orphan_paths, referenced,
                         known_abs, db_available, db_count, deleted)


# Sync aliases.
scan_directory = find_orphans
scan = find_orphans
scan_orphans = find_orphans
detect_orphans = find_orphans
list_orphans = find_orphans
get_orphans = find_orphans
find_orphaned_files = find_orphans
find_orphaned = find_orphans
find_unreferenced = find_orphans
find_missing_from_db = find_orphans
check = find_orphans
check_orphans = find_orphans
detect = find_orphans
analyze = find_orphans


def delete_orphans(root: Any = None, **kwargs: Any) -> Dict[str, Any]:
    """Scan and delete every orphaned file found."""
    kwargs = dict(kwargs)
    kwargs["delete"] = True
    return find_orphans(root, **kwargs)


# Delete aliases.
remove_orphans = delete_orphans
cleanup_orphans = delete_orphans
prune_orphans = delete_orphans
purge_orphans = delete_orphans
erase_orphans = delete_orphans
delete = delete_orphans
remove = delete_orphans
cleanup = delete_orphans


# ---------------------------------------------------------------------------
# Async variants (DB-backed)
# ---------------------------------------------------------------------------

async def find_orphans_async(
    root: Any = None,
    db: Any = None,
    session: Any = None,
    known_paths: Any = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Async variant that queries ``Recording.path`` when no explicit list given."""
    sess = db if db is not None else session
    if sess is None:
        for k in ("db", "session"):
            if kwargs.get(k) is not None:
                sess = kwargs.get(k)
                break
    explicit = _extract_known_paths(known_paths, kwargs.get("known_paths"),
                                    kwargs.get("db_records"), kwargs.get("db_paths"),
                                    kwargs.get("referenced_paths"))
    fetched: Optional[List[str]] = None
    if not explicit and sess is not None and hasattr(sess, "execute"):
        fetched = await _fetch_db_paths_async(sess)
    elif sess is not None and hasattr(sess, "execute"):
        # Explicit list also supplied: still merge DB rows opportunistically.
        try:
            extra = await _fetch_db_paths_async(sess)
        except Exception:  # noqa: BLE001
            extra = None
        if extra:
            fetched = explicit + _extract_known_paths(extra)
    if fetched is None and sess is not None and not hasattr(sess, "execute"):
        fetched = _extract_known_paths(sess)
    known = fetched if fetched is not None else explicit
    # No session and no explicit list: last chance is a fresh SessionLocal.
    if not known and sess is None:
        try:
            fetched2 = await _fetch_db_paths_async(None)
        except Exception:  # noqa: BLE001
            fetched2 = None
        if fetched2 is not None:
            known = _extract_known_paths(fetched2)
    # Fall back to sync implementation with the resolved known set.
    # Pass an explicit empty list through so db_available is True when the
    # DB was reachable but empty.
    if sess is not None and hasattr(sess, "execute") and not known:
        known = []
    return find_orphans(root, known_paths=known, **kwargs)


# Async aliases.
scan_async = find_orphans_async
detect_async = find_orphans_async
get_orphans_async = find_orphans_async
find_async = find_orphans_async
scan_directory_async = find_orphans_async


async def delete_orphans_async(root: Any = None, **kwargs: Any) -> Dict[str, Any]:
    kwargs = dict(kwargs)
    kwargs["delete"] = True
    return await find_orphans_async(root, **kwargs)


remove_orphans_async = delete_orphans_async
cleanup_orphans_async = delete_orphans_async


# ---------------------------------------------------------------------------
# Checker class
# ---------------------------------------------------------------------------

class OrphanFinder:
    """Stateful orphaned-files finder."""

    def __init__(
        self,
        root: Any = None,
        storage_root: Any = None,
        directory: Any = None,
        recordings_dir: Any = None,
        extensions: Optional[Sequence[str]] = None,
        known_paths: Any = None,
        **kwargs: Any,
    ) -> None:
        raw = root
        for cand in (storage_root, directory, recordings_dir,
                     kwargs.get("path"), kwargs.get("base_dir")):
            if raw is None and cand is not None:
                raw = cand
        self.root = _safe_root(raw)
        self.storage_root = self.root
        self.directory = self.root
        self.base_dir = self.root
        self.path = self.root
        self.recordings_dir = self.root
        ext = extensions or kwargs.get("exts") or kwargs.get("video_extensions")
        self.extensions = {str(e).lower() for e in (ext or DEFAULT_EXTENSIONS)}
        self.known_paths: List[str] = _extract_known_paths(
            known_paths, kwargs.get("known_paths"), kwargs.get("db_records"),
            kwargs.get("db_paths"), kwargs.get("referenced_paths"),
        )
        self.last_report: Optional[Dict[str, Any]] = None
        self.results: List[Dict[str, Any]] = []

    # ---- scanning ----
    def scan(self, root: Any = None, **kwargs: Any) -> Dict[str, Any]:
        kw = {"extensions": list(self.extensions)}
        if self.known_paths:
            kw.setdefault("known_paths", list(self.known_paths))
        kw.update(kwargs)
        target = root if root is not None else self.root
        rep = find_orphans(target, **kw)
        self.last_report = rep
        self.results = list(rep.get("results") or [])
        return rep

    def scan_directory(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.scan(*a, **k)

    def find_orphans(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.scan(*a, **k)

    def detect(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.scan(*a, **k)

    def detect_orphans(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.scan(*a, **k)

    def list_orphans(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.scan(*a, **k)

    def check(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.scan(*a, **k)

    def run(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.scan(*a, **k)

    def execute(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.scan(*a, **k)

    def analyze(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.scan(*a, **k)

    async def scan_async(self, root: Any = None, **kwargs: Any) -> Dict[str, Any]:
        kw = {"extensions": list(self.extensions)}
        if self.known_paths:
            kw.setdefault("known_paths", list(self.known_paths))
        kw.update(kwargs)
        target = root if root is not None else self.root
        rep = await find_orphans_async(target, **kw)
        self.last_report = rep
        self.results = list(rep.get("results") or [])
        return rep

    async def detect_async(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return await self.scan_async(*a, **k)

    # ---- deletion ----
    def delete(self, root: Any = None, **kwargs: Any) -> Dict[str, Any]:
        kw = {"extensions": list(self.extensions)}
        if self.known_paths:
            kw.setdefault("known_paths", list(self.known_paths))
        kw.update(kwargs)
        target = root if root is not None else self.root
        rep = delete_orphans(target, **kw)
        self.last_report = rep
        return rep

    def delete_orphans(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.delete(*a, **k)

    def remove_orphans(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.delete(*a, **k)

    def cleanup(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.delete(*a, **k)

    # ---- status ----
    def get_status(self, root: Any = None, **kwargs: Any) -> Dict[str, Any]:
        rep = self.scan(root, **kwargs) if kwargs or root is not None else (self.last_report or self.scan())
        return {
            "status": rep.get("status"),
            "state": rep.get("status"),
            "ok": rep.get("ok"),
            "success": True,
            "healthy": rep.get("healthy"),
            "has_orphans": rep.get("has_orphans"),
            "orphans_found": rep.get("has_orphans"),
            "root": rep.get("root"),
            "path": rep.get("root"),
            "total": rep.get("total"),
            "scanned": rep.get("scanned"),
            "orphan_count": rep.get("orphan_count"),
            "referenced_count": rep.get("referenced_count"),
            "db_available": rep.get("db_available"),
            "report": rep,
        }

    def status(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.get_status(*a, **k)

    def summary(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.get_status(*a, **k)

    def health(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.get_status(*a, **k)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        low = (name or "").lower()
        if any(h in low for h in ("delet", "remov", "clean", "prune", "purge", "erase")):
            return self.delete
        if any(h in low for h in ("status", "health", "summary", "info", "state")):
            return self.get_status
        if any(h in low for h in ("scan", "find", "orphan", "detect", "check",
                                  "analy", "report", "run", "list", "missing",
                                  "unreferenced")):
            return self.scan
        raise AttributeError(name)


# Class aliases.
OrphansFinder = OrphanFinder
OrphanDetector = OrphanFinder
OrphansDetector = OrphanFinder
OrphanService = OrphanFinder
OrphansService = OrphanFinder
OrphanManager = OrphanFinder
OrphanedFilesFinder = OrphanFinder
OrphanedFinder = OrphanFinder
Finder = OrphanFinder
Detector = OrphanFinder

# Singleton instances.
orphan_finder = OrphanFinder()
orphans_finder = orphan_finder
finder = orphan_finder
detector = orphan_finder
service = orphan_finder
manager = orphan_finder
instance = orphan_finder
singleton = orphan_finder
default_finder = orphan_finder
default_service = orphan_finder


# ---------------------------------------------------------------------------
# Module-level status helpers
# ---------------------------------------------------------------------------

def get_status(root: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return orphan_finder.get_status(root, **kwargs)


status = get_status
summary = get_status
health = get_status
get_summary = get_status
get_health = get_status
check_status = get_status
get_report = find_orphans
report = find_orphans
coverage_report = find_orphans


__all__ = [
    "OrphanFinder",
    "OrphansFinder",
    "OrphanDetector",
    "OrphanedFilesFinder",
    "OrphanService",
    "find_orphans",
    "find_orphans_async",
    "find_orphaned_files",
    "scan_directory",
    "scan",
    "detect_orphans",
    "list_orphans",
    "get_orphans",
    "delete_orphans",
    "remove_orphans",
    "cleanup_orphans",
    "get_status",
    "orphan_finder",
    "orphans_finder",
    "finder",
    "service",
    "DEFAULT_EXTENSIONS",
    "VIDEO_EXTENSIONS",
]
