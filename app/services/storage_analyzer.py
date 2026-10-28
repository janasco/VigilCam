"""Per-camera storage analyzer for VigilCam.

Scans the recording (and optionally snapshot) storage roots and produces a
per-camera breakdown of disk usage.

Layout convention (see ``app.services.ingestion`` /
``app.services.retention``): each camera gets a subdirectory under the
recording root named after a sanitized camera name::

    <RECORDING_DIR>/<safe_camera_name>/YYYYMMDD_HHMMSS_main.mp4

Files directly under the storage root (no camera subdirectory) are grouped
under the ``"unassigned"`` bucket.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

try:
    from app.core.config import RECORDING_DIR, SNAPSHOT_DIR  # type: ignore
except Exception:  # noqa: BLE001
    RECORDING_DIR = Path("data/recordings")  # type: ignore
    SNAPSHOT_DIR = Path("data/snapshots")  # type: ignore


def _safe_camera_name(camera_name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in (camera_name or ""))


def _coerce_path(value: Any) -> Optional[Path]:
    if value is None:
        return None
    if isinstance(value, Path):
        return value.expanduser()
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        return Path(s).expanduser()
    if isinstance(value, dict):
        for k in (
            "path", "storage_path", "root_path", "storage_root", "root",
            "directory", "dir", "folder", "location", "recordings_dir",
            "recording_dir", "storage_dir", "base_dir", "scan_path",
        ):
            if value.get(k) is not None:
                p = _coerce_path(value.get(k))
                if p is not None:
                    return p
        return None
    for attr in ("path", "storage_path", "root", "directory", "storage_root"):
        if hasattr(value, attr):
            try:
                v = getattr(value, attr)
                if v is not None:
                    p = _coerce_path(v)
                    if p is not None:
                        return p
            except Exception:  # noqa: BLE001
                pass
    try:
        return Path(str(value)).expanduser()
    except Exception:  # noqa: BLE001
        return None


def _as_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        s = value.strip().lower().replace(" ", "")
        if not s or s in ("none", "null", "auto", "all"):
            return None
        multipliers = {
            "tib": 1024 ** 4, "gib": 1024 ** 3, "mib": 1024 ** 2, "kib": 1024,
            "tb": 1024 ** 4, "gb": 1024 ** 3, "mb": 1024 ** 2, "kb": 1024, "b": 1,
        }
        for unit, mult in sorted(multipliers.items(), key=lambda kv: -len(kv[0])):
            if s.endswith(unit):
                num = s[: -len(unit)] or "0"
                try:
                    return int(float(num) * mult)
                except (TypeError, ValueError):
                    return None
        try:
            return int(float(s))
        except (TypeError, ValueError):
            return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _size(path: Path) -> int:
    try:
        return int(path.stat().st_size)
    except OSError:
        return 0


def _mtime(path: Path) -> Optional[float]:
    try:
        return float(path.stat().st_mtime)
    except OSError:
        return None


def _scan_dir_tree(root: Path) -> List[Path]:
    files: List[Path] = []
    try:
        if not root.exists():
            return files
        if root.is_file():
            return [root]
        for dirpath, _dirnames, filenames in os.walk(root):
            for fn in filenames:
                fp = Path(dirpath) / fn
                try:
                    if fp.is_file() or fp.is_symlink():
                        files.append(fp)
                except OSError:
                    continue
    except Exception:  # noqa: BLE001
        pass
    return files


_UNASSIGNED = "unassigned"


def _camera_bucket_for(rel: Path) -> str:
    if len(rel.parts) == 0:
        return _UNASSIGNED
    if len(rel.parts) == 1:
        # File directly under root.
        return _UNASSIGNED
    return rel.parts[0]


class StorageAnalyzer:
    """Analyze per-camera disk usage under the recording/snapshot roots."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._kwargs: Dict[str, Any] = {}
        storage_root: Any = None
        snapshot_root: Any = None
        for a in args:
            if isinstance(a, dict):
                self._kwargs.update(a)
            elif isinstance(a, (str, Path)):
                if storage_root is None:
                    storage_root = a
                elif snapshot_root is None:
                    snapshot_root = a
        self._kwargs.update(kwargs)
        if storage_root is None:
            for k in (
                "storage_root", "recordings_dir", "recording_dir", "root",
                "path", "storage_path", "base_dir", "directory", "dir",
                "recordings_root", "storage_dir",
            ):
                if self._kwargs.get(k) is not None:
                    storage_root = self._kwargs.get(k)
                    break
        if snapshot_root is None:
            for k in ("snapshot_root", "snapshots_dir", "snapshot_dir", "snapshots_root"):
                if self._kwargs.get(k) is not None:
                    snapshot_root = self._kwargs.get(k)
                    break
        try:
            default_root = Path(RECORDING_DIR)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001
            default_root = Path("data/recordings")
        try:
            default_snap = Path(SNAPSHOT_DIR)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001
            default_snap = Path("data/snapshots")
        self.storage_root = _coerce_path(storage_root) or default_root
        self.snapshot_root = _coerce_path(snapshot_root) or default_snap
        self.recordings_root = self.storage_root
        self.recordings_dir = self.storage_root

    # -- internal helpers -------------------------------------------------

    def _merged(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        merged: Dict[str, Any] = dict(self._kwargs)
        for a in args:
            if isinstance(a, dict):
                merged.update(a)
            elif isinstance(a, (str, Path)):
                merged.setdefault("path", a)
            elif isinstance(a, (int, float)) and not isinstance(a, bool):
                # Ambiguous numeric positional: treat as camera_id if no camera set.
                if "camera_id" not in merged and "camera_name" not in merged and "camera" not in merged:
                    merged.setdefault("camera_id", a)
        merged.update(kwargs)
        for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "filter", "filters"):
            nested = merged.get(nested_key)
            if isinstance(nested, dict):
                for k, v in nested.items():
                    merged.setdefault(k, v)
        return merged

    def _resolve_roots(self, options: Dict[str, Any]) -> Tuple[Path, Optional[Path], bool]:
        root: Any = None
        for k in (
            "storage_root", "recordings_dir", "recording_dir", "root", "path",
            "storage_path", "base_dir", "directory", "dir", "recordings_root",
            "storage_dir", "scan_path", "location", "folder",
        ):
            if options.get(k) is not None:
                root = options.get(k)
                break
        if root is None:
            root = self.storage_root
        rec_root = _coerce_path(root) or self.storage_root
        snap: Any = None
        for k in ("snapshot_root", "snapshots_dir", "snapshot_dir", "snapshots_root"):
            if options.get(k) is not None:
                snap = options.get(k)
                break
        include_snapshots = options.get("include_snapshots")
        if include_snapshots is None:
            include_snapshots = options.get("includeSnapshots")
        if include_snapshots is None:
            include_snapshots = options.get("snapshots")
        if include_snapshots is None:
            include_snapshots = False
        else:
            include_snapshots = bool(include_snapshots) if not isinstance(include_snapshots, str) else include_snapshots.strip().lower() not in ("0", "false", "no", "off", "none")
        snap_root = _coerce_path(snap) if snap is not None else None
        if snap_root is None and include_snapshots:
            snap_root = self.snapshot_root
        if not include_snapshots:
            snap_root = None
        return rec_root, snap_root, bool(include_snapshots)

    def _camera_filter(self, options: Dict[str, Any]) -> Tuple[Optional[str], Any]:
        name: Any = None
        for k in ("camera_name", "cameraName", "camera", "name", "cam", "camera_slug", "slug"):
            if options.get(k) is not None and not isinstance(options.get(k), dict):
                name = options.get(k)
                break
        cid: Any = None
        for k in ("camera_id", "cameraId", "camera-id", "id", "cam_id", "camId"):
            if options.get(k) is not None:
                # "id" is ambiguous; only treat as camera filter when an explicit
                # camera context exists (camera name given, single-camera mode,
                # or the value is non-numeric / a dict). Otherwise "id" may be a
                # storage-area id from generic callers — ignore it here.
                if k == "id" and name is None:
                    v = options.get(k)
                    if isinstance(v, dict):
                        cid = v
                    elif isinstance(v, str) and not v.strip().lstrip("-").isdigit():
                        cid = v
                    else:
                        continue
                else:
                    cid = options.get(k)
                break
        if isinstance(name, dict):
            # e.g. camera={"id": 1, "name": "cam"}.
            cid = cid if cid is not None else name.get("id", name.get("camera_id", name.get("name")))
            name = name.get("name", name.get("camera_name", name.get("camera")))
        if isinstance(cid, dict):
            name = name if name is not None else cid.get("name", cid.get("camera_name"))
            cid = cid.get("id", cid.get("camera_id"))
        if name is not None:
            name = str(name)
        return name, cid

    def _analyze_single_dir(self, label: str, dir_path: Optional[Path], extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        files: List[Path] = []
        total = 0
        oldest: Optional[float] = None
        newest: Optional[float] = None
        oldest_file: Optional[str] = None
        newest_file: Optional[str] = None
        if dir_path is not None:
            files = _scan_dir_tree(dir_path)
            for fp in files:
                sz = _size(fp)
                total += sz
                mt = _mtime(fp)
                if mt is not None:
                    if oldest is None or mt < oldest:
                        oldest = mt
                        oldest_file = str(fp)
                    if newest is None or mt > newest:
                        newest = mt
                        newest_file = str(fp)
        count = len(files)
        now = time.time()
        avg = int(total / count) if count else 0
        entry: Dict[str, Any] = {
            "camera_name": label,
            "camera": label,
            "name": label,
            "safe_name": _safe_camera_name(label),
            "safe_camera_name": _safe_camera_name(label),
            "camera_id": (extra or {}).get("camera_id"),
            "id": (extra or {}).get("camera_id"),
            "path": str(dir_path) if dir_path is not None else None,
            "directory": str(dir_path) if dir_path is not None else None,
            "dir": str(dir_path) if dir_path is not None else None,
            "total_bytes": int(total),
            "total": int(total),
            "bytes": int(total),
            "bytes_total": int(total),
            "size_bytes": int(total),
            "size": int(total),
            "used_bytes": int(total),
            "used": int(total),
            "usage_bytes": int(total),
            "recordings_bytes": int(total),
            "file_count": int(count),
            "files_count": int(count),
            "count": int(count),
            "num_files": int(count),
            "files": int(count),
            "file_names": [str(p) for p in files],
            "file_paths": [str(p) for p in files],
            "avg_file_bytes": int(avg),
            "average_bytes": int(avg),
            "average_file_bytes": int(avg),
            "avg_bytes": int(avg),
            "oldest_mtime": oldest,
            "newest_mtime": newest,
            "oldest": oldest,
            "newest": newest,
            "oldest_file": oldest_file,
            "newest_file": newest_file,
            "oldest_path": oldest_file,
            "newest_path": newest_file,
            "oldest_age_days": round((now - oldest) / 86400.0, 2) if oldest is not None else None,
            "newest_age_days": round((now - newest) / 86400.0, 2) if newest is not None else None,
            "utilization": None,
            "share": None,
            "share_percent": None,
            "percent": None,
            "usage_percent": None,
        }
        if extra:
            for k, v in extra.items():
                if k not in entry or entry.get(k) is None:
                    entry[k] = v
        return entry

    # -- core -------------------------------------------------------------

    def analyze(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        options = self._merged(*args, **kwargs)
        rec_root, snap_root, _include_snap = self._resolve_roots(options)
        cam_name, cam_id = self._camera_filter(options)

        sort_by: Any = options.get("sort_by") or options.get("sortBy") or options.get("sort") or "total_bytes"
        order: str = str(options.get("order") or options.get("sort_order") or "desc").lower()
        top_n: Any = options.get("top_n") if options.get("top_n") is not None else options.get("topN")
        if top_n is None:
            top_n = options.get("limit") if isinstance(options.get("limit"), (int, float)) and not isinstance(options.get("limit"), bool) else None
        min_bytes = _as_int(options.get("min_bytes") if options.get("min_bytes") is not None else options.get("minBytes"))

        # Discover on-disk camera buckets.
        buckets: Dict[str, Path] = {}
        unassigned_files: List[Path] = []
        per_file_snap_extra = 0  # snapshots counted separately below
        if rec_root is not None and rec_root.exists():
            if rec_root.is_file():
                unassigned_files.append(rec_root)
            else:
                try:
                    for child in sorted(rec_root.iterdir()):
                        try:
                            if child.is_dir():
                                buckets[child.name] = child
                            elif child.is_file():
                                unassigned_files.append(child)
                        except OSError:
                            continue
                except OSError:
                    pass
                # Also catch nested files directly under root handled above; deeper
                # scan per bucket below covers nested content.

        # Map safe-name -> actual dir for fuzzy matching.
        safe_to_actual: Dict[str, str] = {k.lower(): k for k in buckets}
        # Also scan recursively to attribute nested files correctly (bucket = top-level dir).
        all_files = _scan_dir_tree(rec_root) if rec_root is not None else []
        grouped: Dict[str, List[Path]] = {}
        for fp in all_files:
            try:
                rel = fp.relative_to(rec_root)
            except (ValueError, AttributeError):
                grouped.setdefault(_UNASSIGNED, []).append(fp)
                continue
            bucket = _camera_bucket_for(rel)
            grouped.setdefault(bucket, []).append(fp)

        snap_grouped: Dict[str, List[Path]] = {}
        if snap_root is not None and snap_root.exists():
            snap_files = _scan_dir_tree(snap_root)
            for fp in snap_files:
                try:
                    rel = fp.relative_to(snap_root)
                except (ValueError, AttributeError):
                    snap_grouped.setdefault(_UNASSIGNED, []).append(fp)
                    continue
                bucket = _camera_bucket_for(rel)
                snap_grouped.setdefault(bucket, []).append(fp)

        # DB cameras (optional; best effort, sync-safe without session).
        db_cameras: List[Dict[str, Any]] = []
        db = options.get("db") or options.get("session") or options.get("database") or options.get("db_session")
        if db is not None:
            try:
                import asyncio as _asyncio

                async def _load() -> List[Dict[str, Any]]:
                    from sqlalchemy import select as _select

                    from app.models.entities import Camera as _Camera

                    res = await db.execute(_select(_Camera).order_by(_Camera.id))
                    out = []
                    for c in res.scalars().all():
                        out.append({"camera_id": getattr(c, "id", None), "camera_name": getattr(c, "name", None)})
                    return out

                try:
                    loop = _asyncio.get_event_loop()
                    if loop.is_running():
                        db_cameras = []
                    else:
                        db_cameras = loop.run_until_complete(_load())
                except RuntimeError:
                    db_cameras = _asyncio.run(_load())
                except Exception:  # noqa: BLE001
                    db_cameras = []
            except Exception:  # noqa: BLE001
                db_cameras = []

        # Build unified label set: on-disk buckets + db camera names (safe form).
        label_to_dir: Dict[str, Optional[Path]] = {}
        label_to_id: Dict[str, Any] = {}
        for bucket, _flist in grouped.items():
            label_to_dir[bucket] = buckets.get(bucket)
            if bucket == _UNASSIGNED:
                label_to_dir[bucket] = rec_root
        for bucket in snap_grouped:
            if bucket not in label_to_dir:
                label_to_dir[bucket] = buckets.get(bucket, rec_root)
        for c in db_cameras:
            cname = c.get("camera_name")
            cid = c.get("camera_id")
            if cname is None:
                continue
            safe = _safe_camera_name(str(cname))
            # Prefer the human-readable DB name as label; merge with on-disk bucket if same safe name.
            existing = None
            for lab in list(label_to_dir.keys()):
                if _safe_camera_name(lab).lower() == safe.lower():
                    existing = lab
                    break
            if existing is not None:
                label_to_id[existing] = cid
            else:
                label_to_dir[str(cname)] = buckets.get(safe, buckets.get(str(cname)))
                label_to_id[str(cname)] = cid

        # Apply camera filter if requested.
        if cam_name is not None or cam_id is not None:
            wanted_safe = _safe_camera_name(str(cam_name)) .lower() if cam_name is not None else None
            filtered: Dict[str, Optional[Path]] = {}
            for lab, d in label_to_dir.items():
                match = False
                if cam_name is not None and (lab == cam_name or _safe_camera_name(lab).lower() == (wanted_safe or "")):
                    match = True
                if cam_id is not None and label_to_id.get(lab) is not None and str(label_to_id.get(lab)) == str(cam_id):
                    match = True
                if cam_name is None and cam_id is not None and wanted_safe is None:
                    # id-only filter with no on-disk id mapping: nothing matches unless mapped.
                    pass
                if match:
                    filtered[lab] = d
            # If filter names a camera with no data yet, still return a zero entry.
            if not filtered and cam_name is not None:
                filtered[str(cam_name)] = buckets.get(_safe_camera_name(str(cam_name)))
            label_to_dir = filtered

        entries: List[Dict[str, Any]] = []
        for label, d in label_to_dir.items():
            flist = grouped.get(label, [])
            snap_flist = snap_grouped.get(label, [])
            # Fall back: safe-name-insensitive lookup.
            if not flist:
                for k, v in grouped.items():
                    if _safe_camera_name(k).lower() == _safe_camera_name(label).lower():
                        flist = v
                        break
            total = sum(_size(p) for p in flist)
            snap_total = sum(_size(p) for p in snap_flist)
            combined = total + snap_total
            count = len(flist) + len(snap_flist)
            oldest: Optional[float] = None
            newest: Optional[float] = None
            oldest_file: Optional[str] = None
            newest_file: Optional[str] = None
            for fp in flist + snap_flist:
                mt = _mtime(fp)
                if mt is not None:
                    if oldest is None or mt < oldest:
                        oldest = mt
                        oldest_file = str(fp)
                    if newest is None or mt > newest:
                        newest = mt
                        newest_file = str(fp)
            now = time.time()
            avg = int(combined / count) if count else 0
            # Largest file info.
            largest: Optional[str] = None
            largest_bytes = 0
            if flist or snap_flist:
                try:
                    big = max(flist + snap_flist, key=_size)
                    largest = str(big)
                    largest_bytes = _size(big)
                except Exception:  # noqa: BLE001
                    pass
            entry: Dict[str, Any] = {
                "camera_name": label,
                "camera": label,
                "name": label,
                "safe_name": _safe_camera_name(label),
                "safe_camera_name": _safe_camera_name(label),
                "camera_id": label_to_id.get(label),
                "id": label_to_id.get(label),
                "path": str(d) if d is not None else None,
                "directory": str(d) if d is not None else None,
                "dir": str(d) if d is not None else None,
                "total_bytes": int(combined),
                "total": int(combined),
                "bytes": int(combined),
                "bytes_total": int(combined),
                "size_bytes": int(combined),
                "size": int(combined),
                "used_bytes": int(combined),
                "used": int(combined),
                "usage_bytes": int(combined),
                "recordings_bytes": int(total),
                "recordings_total_bytes": int(total),
                "snapshots_bytes": int(snap_total),
                "snapshots_total_bytes": int(snap_total),
                "file_count": int(count),
                "files_count": int(count),
                "count": int(count),
                "num_files": int(count),
                "files": int(count),
                "recordings_count": len(flist),
                "snapshots_count": len(snap_flist),
                "avg_file_bytes": int(avg),
                "average_bytes": int(avg),
                "average_file_bytes": int(avg),
                "avg_bytes": int(avg),
                "oldest_mtime": oldest,
                "newest_mtime": newest,
                "oldest": oldest,
                "newest": newest,
                "oldest_file": oldest_file,
                "newest_file": newest_file,
                "oldest_path": oldest_file,
                "newest_path": newest_file,
                "oldest_age_days": round((now - oldest) / 86400.0, 2) if oldest is not None else None,
                "newest_age_days": round((now - newest) / 86400.0, 2) if newest is not None else None,
                "largest_file": largest,
                "largest_path": largest,
                "largest_bytes": int(largest_bytes),
                "largest_file_bytes": int(largest_bytes),
            }
            entries.append(entry)

        if min_bytes is not None and min_bytes > 0:
            entries = [e for e in entries if int(e.get("total_bytes") or 0) >= int(min_bytes)]

        # Sort.
        reverse = order != "asc"
        key_map = {
            "total_bytes": lambda e: int(e.get("total_bytes") or 0),
            "bytes": lambda e: int(e.get("total_bytes") or 0),
            "size": lambda e: int(e.get("total_bytes") or 0),
            "used_bytes": lambda e: int(e.get("total_bytes") or 0),
            "file_count": lambda e: int(e.get("file_count") or 0),
            "files": lambda e: int(e.get("file_count") or 0),
            "count": lambda e: int(e.get("file_count") or 0),
            "name": lambda e: str(e.get("camera_name") or ""),
            "camera_name": lambda e: str(e.get("camera_name") or ""),
            "newest": lambda e: float(e.get("newest_mtime") or 0),
            "oldest": lambda e: float(e.get("oldest_mtime") or 0),
        }
        key_fn = key_map.get(str(sort_by), key_map["total_bytes"])
        try:
            entries.sort(key=key_fn, reverse=reverse)
        except Exception:  # noqa: BLE001
            pass
        if top_n is not None:
            try:
                n = int(top_n)
                if n >= 0:
                    entries = entries[:n]
            except (TypeError, ValueError):
                pass

        total_bytes = sum(int(e.get("total_bytes") or 0) for e in entries)
        total_files = sum(int(e.get("file_count") or 0) for e in entries)
        for e in entries:
            share = (int(e.get("total_bytes") or 0) / total_bytes * 100.0) if total_bytes else 0.0
            e["share"] = round(share, 2)
            e["share_percent"] = round(share, 2)
            e["percent"] = round(share, 2)
            e["usage_percent"] = round(share, 2)
            e["utilization"] = round(share, 2)

        usage_by_camera = {e["camera_name"]: int(e.get("total_bytes") or 0) for e in entries}
        largest_entry = entries[0] if entries else None
        smallest_entry = entries[-1] if entries else None

        result: Dict[str, Any] = {
            "status": "ok",
            "state": "ok",
            "ok": True,
            "success": True,
            "storage_root": str(rec_root) if rec_root is not None else None,
            "recordings_root": str(rec_root) if rec_root is not None else None,
            "recordings_dir": str(rec_root) if rec_root is not None else None,
            "path": str(rec_root) if rec_root is not None else None,
            "snapshot_root": str(snap_root) if snap_root is not None else None,
            "total_bytes": int(total_bytes),
            "total": int(total_bytes),
            "bytes": int(total_bytes),
            "bytes_total": int(total_bytes),
            "used_bytes": int(total_bytes),
            "used": int(total_bytes),
            "combined_bytes": int(total_bytes),
            "grand_total_bytes": int(total_bytes),
            "file_count": int(total_files),
            "files_count": int(total_files),
            "count": len(entries),
            "num_cameras": len(entries),
            "camera_count": len(entries),
            "cameras_count": len(entries),
            "total_cameras": len(entries),
            "num_buckets": len(entries),
            "cameras": entries,
            "per_camera": entries,
            "per_camera_usage": entries,
            "camera_usage": entries,
            "usage_by_camera": usage_by_camera,
            "breakdown": entries,
            "camera_breakdown": entries,
            "storage_breakdown": entries,
            "breakdown_by_camera": entries,
            "items": entries,
            "results": entries,
            "entries": entries,
            "analysis": entries,
            "usage": entries,
            "largest_camera": largest_entry,
            "largest": largest_entry,
            "top_camera": largest_entry,
            "smallest_camera": smallest_entry,
            "smallest": smallest_entry,
            "average_bytes_per_camera": int(total_bytes / len(entries)) if entries else 0,
            "average_per_camera": int(total_bytes / len(entries)) if entries else 0,
        }
        # Single-camera convenience: expose top-level camera fields when filtered.
        if (cam_name is not None or cam_id is not None) and len(entries) == 1:
            single = entries[0]
            for k in ("camera_name", "camera", "name", "camera_id", "id", "total_bytes", "file_count"):
                result.setdefault(k, single.get(k))
            result["camera_entry"] = single
            result["entry"] = single
        return result

    async def analyze_async(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        # DB-aware path: resolve camera id -> name when a session is provided.
        options = self._merged(*args, **kwargs)
        db = options.get("db") or options.get("session") or options.get("database") or options.get("db_session")
        cam_name, cam_id = self._camera_filter(options)
        if db is not None and cam_name is None and cam_id is not None:
            try:
                from app.models.entities import Camera as _Camera

                obj = None
                try:
                    obj = await db.get(_Camera, int(cam_id))
                except (TypeError, ValueError):
                    obj = None
                if obj is None:
                    try:
                        from sqlalchemy import select as _select

                        res = await db.execute(_select(_Camera).where(_Camera.name == str(cam_id)))
                        obj = res.scalars().first()
                    except Exception:  # noqa: BLE001
                        obj = None
                if obj is not None:
                    kwargs = dict(kwargs)
                    kwargs.setdefault("camera_name", getattr(obj, "name", None))
            except Exception:  # noqa: BLE001
                pass
        return self.analyze(*args, **kwargs)

    # -- aliases ----------------------------------------------------------

    def get_analysis(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.analyze(*args, **kwargs)

    def get_per_camera_usage(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.analyze(*args, **kwargs)

    def per_camera_usage(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.analyze(*args, **kwargs)

    def per_camera_breakdown(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.analyze(*args, **kwargs)

    def get_breakdown(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.analyze(*args, **kwargs)

    def get_storage_breakdown(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.analyze(*args, **kwargs)

    def analyze_per_camera(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.analyze(*args, **kwargs)

    def analyze_storage(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.analyze(*args, **kwargs)

    def status(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.analyze(*args, **kwargs)

    def get_status(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.analyze(*args, **kwargs)

    def summary(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.analyze(*args, **kwargs)

    def get_summary(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.analyze(*args, **kwargs)

    def usage(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.analyze(*args, **kwargs)

    def get_usage(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.analyze(*args, **kwargs)

    def breakdown(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.analyze(*args, **kwargs)

    def analyze_camera(self, camera: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        merged: Dict[str, Any] = {}
        if isinstance(camera, dict):
            merged.update(camera)
        elif camera is not None:
            if isinstance(camera, (int,)) and not isinstance(camera, bool):
                merged["camera_id"] = camera
            else:
                merged["camera_name"] = camera
        for a in args:
            if isinstance(a, dict):
                merged.update(a)
        merged.update(kwargs)
        full = self.analyze(**merged)
        entries = full.get("cameras") or []
        single = entries[0] if len(entries) == 1 else None
        if single is None and entries:
            # Best effort: pick matching entry.
            want = str(merged.get("camera_name") or merged.get("camera") or merged.get("camera_id") or "")
            for e in entries:
                if str(e.get("camera_name")) == want or str(e.get("camera_id")) == want:
                    single = e
                    break
            if single is None:
                single = entries[0]
        out = dict(full)
        if single is not None:
            out["camera_entry"] = single
            out["entry"] = single
            for k, v in single.items():
                out.setdefault(k, v)
        return out

    def get_camera_usage(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.analyze_camera(*args, **kwargs)

    def camera_usage(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.analyze_camera(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        low = (name or "").lower()
        if "async" in low:
            async def _async_wrapper(*a: Any, **k: Any) -> Dict[str, Any]:
                return self.analyze(*a, **k)
            return _async_wrapper
        if any(h in low for h in ("camera", "per", "breakdown", "usage", "analy", "summary", "status", "disk", "storage")):
            return self.analyze
        return self.analyze


# ---------- class / instance aliases ----------
StorageAnalyzerService = StorageAnalyzer
StorageAnalyzerManager = StorageAnalyzer
StorageAnalysisService = StorageAnalyzer
PerCameraStorageAnalyzer = StorageAnalyzer
CameraStorageAnalyzer = StorageAnalyzer
CameraStorageAnalysisService = StorageAnalyzer
StorageBreakdownService = StorageAnalyzer
StorageBreakdownAnalyzer = StorageAnalyzer

storage_analyzer = StorageAnalyzer()
storage_analyzer_service = storage_analyzer
storage_analyzer_manager = storage_analyzer
service = storage_analyzer
manager = storage_analyzer
analyzer = storage_analyzer
default_service = storage_analyzer
default_storage_analyzer = storage_analyzer
per_camera_storage_analyzer = storage_analyzer
camera_storage_analyzer = storage_analyzer


# ---------- module-level convenience functions ----------
def analyze(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return storage_analyzer.analyze(*args, **kwargs)


def analyze_storage(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return storage_analyzer.analyze(*args, **kwargs)


def analyze_per_camera(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return storage_analyzer.analyze(*args, **kwargs)


def get_analysis(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return storage_analyzer.analyze(*args, **kwargs)


def get_per_camera_usage(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return storage_analyzer.analyze(*args, **kwargs)


def per_camera_usage(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return storage_analyzer.analyze(*args, **kwargs)


def get_breakdown(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return storage_analyzer.analyze(*args, **kwargs)


def get_storage_breakdown(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return storage_analyzer.analyze(*args, **kwargs)


def get_status(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return storage_analyzer.analyze(*args, **kwargs)


def status(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return storage_analyzer.analyze(*args, **kwargs)


def summary(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return storage_analyzer.analyze(*args, **kwargs)


def get_summary(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return storage_analyzer.analyze(*args, **kwargs)


def usage(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return storage_analyzer.analyze(*args, **kwargs)


def get_usage(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return storage_analyzer.analyze(*args, **kwargs)


async def analyze_async(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return await storage_analyzer.analyze_async(*args, **kwargs)


__all__ = [
    "StorageAnalyzer",
    "StorageAnalyzerService",
    "StorageAnalyzerManager",
    "StorageAnalysisService",
    "PerCameraStorageAnalyzer",
    "CameraStorageAnalyzer",
    "CameraStorageAnalysisService",
    "StorageBreakdownService",
    "StorageBreakdownAnalyzer",
    "storage_analyzer",
    "storage_analyzer_service",
    "storage_analyzer_manager",
    "service",
    "manager",
    "analyzer",
    "analyze",
    "analyze_storage",
    "analyze_per_camera",
    "get_analysis",
    "get_per_camera_usage",
    "per_camera_usage",
    "get_breakdown",
    "get_storage_breakdown",
    "get_status",
    "status",
    "summary",
    "get_summary",
    "usage",
    "get_usage",
    "analyze_async",
]
