"""UI-ready per-camera storage breakdown with trend data.

Wraps the on-disk recording/snapshot layout (see
``app.services.storage_analyzer``) and returns a frontend-friendly payload:

- per-camera ``total_bytes`` + ``share %`` (share of grand total)
- per-camera ``trend`` (direction + daily sparkline over a trailing window)
- top-level totals + aliases so dashboards can bind without transformation.

Layout convention: each camera gets a subdirectory under the recording root
named after a sanitized camera name::

    <RECORDING_DIR>/<safe_camera_name>/YYYYMMDD_HHMMSS_main.mp4

Files directly under the storage root are grouped under ``"unassigned"``.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from app.core.config import RECORDING_DIR, SNAPSHOT_DIR  # type: ignore
except Exception:  # noqa: BLE001
    RECORDING_DIR = Path("data/recordings")  # type: ignore
    SNAPSHOT_DIR = Path("data/snapshots")  # type: ignore


_UNASSIGNED = "unassigned"
_DEFAULT_DAYS = 7
_MAX_DAYS = 31


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
            "recordings_root",
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


def _as_days(value: Any, default: int = _DEFAULT_DAYS) -> int:
    n = _as_int(value)
    if n is None:
        return default
    try:
        n = int(n)
    except (TypeError, ValueError):
        return default
    if n < 1:
        return 1
    if n > _MAX_DAYS:
        return _MAX_DAYS
    return n


def _human_bytes(num: Any) -> str:
    try:
        n = float(num or 0)
    except (TypeError, ValueError):
        return "0 B"
    if n < 0:
        n = 0
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    idx = 0
    while n >= 1024.0 and idx < len(units) - 1:
        n /= 1024.0
        idx += 1
    if idx == 0:
        return f"{int(n)} {units[idx]}"
    return f"{n:.1f} {units[idx]}"


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


def _camera_bucket_for(rel: Path) -> str:
    if len(rel.parts) == 0:
        return _UNASSIGNED
    if len(rel.parts) == 1:
        return _UNASSIGNED
    return rel.parts[0]


def _day_label(ts: float) -> str:
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        return ""


def _slope_per_day(daily: List[int]) -> float:
    n = len(daily)
    if n < 2:
        return 0.0
    mean_x = (n - 1) / 2.0
    mean_y = sum(daily) / float(n)
    num = 0.0
    den = 0.0
    for i, y in enumerate(daily):
        dx = float(i) - mean_x
        num += dx * (float(y) - mean_y)
        den += dx * dx
    if den == 0:
        return 0.0
    return num / den


def _compute_trend(file_infos: List[Tuple[int, Optional[float]]], days: int, now: float) -> Dict[str, Any]:
    """Build trailing-window trend from (size, mtime) pairs.

    ``daily_bytes[0]`` is the oldest day in the window, ``[-1]`` the most
    recent 24h. ``direction`` is derived from last-24h vs previous-24h.
    """
    days = _as_days(days, _DEFAULT_DAYS)
    daily_bytes: List[int] = [0] * days
    daily_counts: List[int] = [0] * days
    for size, mt in file_infos:
        if mt is None:
            continue
        try:
            age_days = (float(now) - float(mt)) / 86400.0
        except (TypeError, ValueError):
            continue
        if age_days < 0:
            age_days = 0.0
        if age_days >= float(days):
            continue
        bucket = int(age_days)  # 0 = most recent 24h
        idx = days - 1 - bucket  # 0 = oldest
        if 0 <= idx < days:
            try:
                daily_bytes[idx] += int(size)
            except (TypeError, ValueError):
                pass
            daily_counts[idx] += 1

    # Day labels oldest -> newest.
    daily_labels: List[str] = []
    for i in range(days):
        # Start-of-bucket timestamp: now - (days-1-i)*86400.
        ts = float(now) - float(days - 1 - i) * 86400.0
        daily_labels.append(_day_label(ts))

    bytes_last = daily_bytes[-1] if days >= 1 else 0
    bytes_prev = daily_bytes[-2] if days >= 2 else 0
    window_total = sum(daily_bytes)
    delta = int(bytes_last) - int(bytes_prev)
    if bytes_prev > 0:
        try:
            delta_percent = round((float(delta) / float(bytes_prev)) * 100.0, 2)
        except ZeroDivisionError:
            delta_percent = 0.0
    else:
        delta_percent = 100.0 if bytes_last > 0 else 0.0
    if bytes_prev == 0 and bytes_last == 0 and window_total == 0:
        direction = "stable"
    elif delta_percent > 5.0:
        direction = "up"
    elif delta_percent < -5.0:
        direction = "down"
    else:
        direction = "stable"

    slope = _slope_per_day(daily_bytes)
    avg_daily = float(window_total) / float(days) if days else 0.0

    trend: Dict[str, Any] = {
        "direction": direction,
        "trend": direction,
        "delta_bytes": int(delta),
        "delta": int(delta),
        "change_bytes": int(delta),
        "delta_percent": float(delta_percent),
        "delta_pct": float(delta_percent),
        "change_percent": float(delta_percent),
        "growth_bytes_per_day": float(round(slope, 2)),
        "slope_per_day": float(round(slope, 2)),
        "avg_bytes_per_day": float(round(avg_daily, 2)),
        "bytes_last_24h": int(bytes_last),
        "bytes_prev_24h": int(bytes_prev),
        "bytes_last_day": int(bytes_last),
        "bytes_prev_day": int(bytes_prev),
        "window_total_bytes": int(window_total),
        "window_days": int(days),
        "days": int(days),
        "daily_bytes": list(daily_bytes),
        "daily_counts": list(daily_counts),
        "daily_labels": list(daily_labels),
        "sparkline": list(daily_bytes),
        "sparkline_bytes": list(daily_bytes),
        "series": list(daily_bytes),
    }
    return trend


class StorageBreakdownService:
    """UI-ready per-camera storage breakdown with trend."""

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

    # -- internal helpers -------------------------------------------------

    def _merged(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        merged: Dict[str, Any] = dict(self._kwargs)
        for a in args:
            if isinstance(a, dict):
                merged.update(a)
            elif isinstance(a, (str, Path)):
                merged.setdefault("path", a)
            elif isinstance(a, (int, float)) and not isinstance(a, bool):
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

    def _resolve_days(self, options: Dict[str, Any]) -> int:
        for k in ("days", "window_days", "window", "trend_days", "trend_window", "range_days", "last_days", "ndays", "trendDays", "windowDays"):
            if options.get(k) is not None:
                return _as_days(options.get(k), _DEFAULT_DAYS)
        return _DEFAULT_DAYS

    def _camera_filter(self, options: Dict[str, Any]) -> Tuple[Optional[str], Any]:
        name: Any = None
        for k in ("camera_name", "cameraName", "camera", "name", "cam", "camera_slug", "slug"):
            if options.get(k) is not None and not isinstance(options.get(k), dict):
                name = options.get(k)
                break
        cid: Any = None
        for k in ("camera_id", "cameraId", "camera-id", "cam_id", "camId"):
            if options.get(k) is not None:
                cid = options.get(k)
                break
        if isinstance(name, dict):
            cid = cid if cid is not None else name.get("id", name.get("camera_id"))
            name = name.get("name", name.get("camera_name", name.get("camera")))
        if name is not None:
            name = str(name)
        return name, cid

    # -- core -------------------------------------------------------------

    def analyze(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        options = self._merged(*args, **kwargs)
        rec_root, snap_root, _include_snap = self._resolve_roots(options)
        days = self._resolve_days(options)
        cam_name, cam_id = self._camera_filter(options)

        sort_by: Any = options.get("sort_by") or options.get("sortBy") or options.get("sort") or "total_bytes"
        order: str = str(options.get("order") or options.get("sort_order") or "desc").lower()
        top_n: Any = options.get("top_n") if options.get("top_n") is not None else options.get("topN")
        if top_n is None:
            top_n = options.get("limit") if isinstance(options.get("limit"), (int, float)) and not isinstance(options.get("limit"), bool) else None
        min_bytes = _as_int(options.get("min_bytes") if options.get("min_bytes") is not None else options.get("minBytes"))

        now = time.time()

        grouped: Dict[str, List[Path]] = {}
        if rec_root is not None:
            all_files = _scan_dir_tree(rec_root)
            for fp in all_files:
                try:
                    rel = fp.relative_to(rec_root)
                except (ValueError, AttributeError):
                    grouped.setdefault(_UNASSIGNED, []).append(fp)
                    continue
                grouped.setdefault(_camera_bucket_for(rel), []).append(fp)

        if snap_root is not None and snap_root.exists():
            for fp in _scan_dir_tree(snap_root):
                try:
                    rel = fp.relative_to(snap_root)
                except (ValueError, AttributeError):
                    grouped.setdefault(_UNASSIGNED, []).append(fp)
                    continue
                grouped.setdefault(_camera_bucket_for(rel), []).append(fp)

        # Camera filter (name only; id mapping requires DB which this
        # UI service intentionally avoids).
        if cam_name is not None:
            wanted = _safe_camera_name(str(cam_name)).lower()
            filtered = {lab: fl for lab, fl in grouped.items() if _safe_camera_name(lab).lower() == wanted or lab == str(cam_name)}
            if not filtered:
                filtered = {str(cam_name): []}
            grouped = filtered
        elif cam_id is not None and not grouped:
            grouped = {}

        entries: List[Dict[str, Any]] = []
        for label, flist in grouped.items():
            infos: List[Tuple[int, Optional[float]]] = []
            total = 0
            oldest: Optional[float] = None
            newest: Optional[float] = None
            oldest_file: Optional[str] = None
            newest_file: Optional[str] = None
            for fp in flist:
                sz = _size(fp)
                mt = _mtime(fp)
                infos.append((sz, mt))
                total += sz
                if mt is not None:
                    if oldest is None or mt < oldest:
                        oldest = mt
                        oldest_file = str(fp)
                    if newest is None or mt > newest:
                        newest = mt
                        newest_file = str(fp)
            count = len(flist)
            avg = int(total / count) if count else 0
            trend = _compute_trend(infos, days, now)
            share_placeholder = 0.0
            entry: Dict[str, Any] = {
                "camera_name": label,
                "camera": label,
                "name": label,
                "safe_name": _safe_camera_name(label),
                "safe_camera_name": _safe_camera_name(label),
                "camera_id": None,
                "id": None,
                "total_bytes": int(total),
                "total": int(total),
                "bytes": int(total),
                "bytes_total": int(total),
                "size_bytes": int(total),
                "size": int(total),
                "used_bytes": int(total),
                "used": int(total),
                "usage_bytes": int(total),
                "total_human": _human_bytes(total),
                "human": _human_bytes(total),
                "human_size": _human_bytes(total),
                "size_human": _human_bytes(total),
                "file_count": int(count),
                "files_count": int(count),
                "count": int(count),
                "num_files": int(count),
                "files": int(count),
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
                "share": share_placeholder,
                "share_percent": share_placeholder,
                "share_pct": share_placeholder,
                "percent": share_placeholder,
                "usage_percent": share_placeholder,
                "share_fraction": 0.0,
                "trend": trend,
                "usage_trend": trend,
                "storage_trend": trend,
                "trend_direction": trend.get("direction"),
                "trend_delta_bytes": trend.get("delta_bytes"),
                "trend_delta_percent": trend.get("delta_percent"),
                "daily_bytes": list(trend.get("daily_bytes") or []),
                "daily_counts": list(trend.get("daily_counts") or []),
                "daily_labels": list(trend.get("daily_labels") or []),
                "sparkline": list(trend.get("sparkline") or []),
                "sparkline_bytes": list(trend.get("sparkline") or []),
            }
            entries.append(entry)

        if min_bytes is not None and min_bytes > 0:
            entries = [e for e in entries if int(e.get("total_bytes") or 0) >= int(min_bytes)]

        reverse = order != "asc"
        key_map = {
            "total_bytes": lambda e: int(e.get("total_bytes") or 0),
            "bytes": lambda e: int(e.get("total_bytes") or 0),
            "size": lambda e: int(e.get("total_bytes") or 0),
            "file_count": lambda e: int(e.get("file_count") or 0),
            "name": lambda e: str(e.get("camera_name") or ""),
            "camera_name": lambda e: str(e.get("camera_name") or ""),
            "trend": lambda e: float((e.get("trend") or {}).get("delta_bytes") or 0),
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
            share_r = round(share, 2)
            e["share"] = share_r
            e["share_percent"] = share_r
            e["share_pct"] = share_r
            e["percent"] = share_r
            e["usage_percent"] = share_r
            e["share_fraction"] = round((share / 100.0), 4) if total_bytes else 0.0

        usage_by_camera = {e["camera_name"]: int(e.get("total_bytes") or 0) for e in entries}
        largest_entry = entries[0] if entries else None

        try:
            generated_at = datetime.fromtimestamp(now, tz=timezone.utc).isoformat()
        except Exception:  # noqa: BLE001
            generated_at = ""

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
            "total_human": _human_bytes(total_bytes),
            "human": _human_bytes(total_bytes),
            "file_count": int(total_files),
            "files_count": int(total_files),
            "count": len(entries),
            "num_cameras": len(entries),
            "camera_count": len(entries),
            "cameras_count": len(entries),
            "total_cameras": len(entries),
            "num_buckets": len(entries),
            "days": int(days),
            "window_days": int(days),
            "trend_days": int(days),
            "window": int(days),
            "generated_at": generated_at,
            "timestamp": generated_at,
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
            "average_bytes_per_camera": int(total_bytes / len(entries)) if entries else 0,
            "average_per_camera": int(total_bytes / len(entries)) if entries else 0,
        }
        if (cam_name is not None or cam_id is not None) and len(entries) == 1:
            single = entries[0]
            for k in ("camera_name", "camera", "name", "camera_id", "id", "total_bytes", "file_count"):
                result.setdefault(k, single.get(k))
            result["camera_entry"] = single
            result["entry"] = single
            result["trend"] = single.get("trend")
        return result

    async def analyze_async(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.analyze(*args, **kwargs)

    # -- aliases ----------------------------------------------------------

    def get_breakdown(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.analyze(*args, **kwargs)

    def get_storage_breakdown(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.analyze(*args, **kwargs)

    def breakdown(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.analyze(*args, **kwargs)

    def get_analysis(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.analyze(*args, **kwargs)

    def get_per_camera_usage(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.analyze(*args, **kwargs)

    def per_camera_usage(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.analyze(*args, **kwargs)

    def per_camera_breakdown(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.analyze(*args, **kwargs)

    def summary(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.analyze(*args, **kwargs)

    def get_summary(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.analyze(*args, **kwargs)

    def status(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.analyze(*args, **kwargs)

    def get_status(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.analyze(*args, **kwargs)

    def usage(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.analyze(*args, **kwargs)

    def get_usage(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.analyze(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        low = (name or "").lower()
        if "async" in low:
            async def _async_wrapper(*a: Any, **k: Any) -> Dict[str, Any]:
                return self.analyze(*a, **k)
            return _async_wrapper
        if any(h in low for h in ("camera", "per", "breakdown", "usage", "analy", "summary", "status", "disk", "storage", "trend")):
            return self.analyze
        return self.analyze


StorageBreakdownServiceAlias = StorageBreakdownService
StorageBreakdownManager = StorageBreakdownService
StorageBreakdownAnalyzer = StorageBreakdownService
PerCameraStorageBreakdown = StorageBreakdownService
CameraStorageBreakdown = StorageBreakdownService

storage_breakdown = StorageBreakdownService()
storage_breakdown_service = storage_breakdown
service = storage_breakdown
manager = storage_breakdown
analyzer = storage_breakdown
default_service = storage_breakdown
default_storage_breakdown = storage_breakdown


def analyze(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return storage_breakdown.analyze(*args, **kwargs)


def get_breakdown(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return storage_breakdown.analyze(*args, **kwargs)


def get_storage_breakdown(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return storage_breakdown.analyze(*args, **kwargs)


def breakdown(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return storage_breakdown.analyze(*args, **kwargs)


def get_per_camera_usage(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return storage_breakdown.analyze(*args, **kwargs)


def per_camera_usage(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return storage_breakdown.analyze(*args, **kwargs)


def summary(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return storage_breakdown.analyze(*args, **kwargs)


def get_summary(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return storage_breakdown.analyze(*args, **kwargs)


def status(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return storage_breakdown.analyze(*args, **kwargs)


def get_status(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return storage_breakdown.analyze(*args, **kwargs)


def usage(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return storage_breakdown.analyze(*args, **kwargs)


def get_usage(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return storage_breakdown.analyze(*args, **kwargs)


async def analyze_async(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return await storage_breakdown.analyze_async(*args, **kwargs)


__all__ = [
    "StorageBreakdownService",
    "StorageBreakdownServiceAlias",
    "StorageBreakdownManager",
    "StorageBreakdownAnalyzer",
    "PerCameraStorageBreakdown",
    "CameraStorageBreakdown",
    "storage_breakdown",
    "storage_breakdown_service",
    "service",
    "manager",
    "analyzer",
    "analyze",
    "get_breakdown",
    "get_storage_breakdown",
    "breakdown",
    "get_per_camera_usage",
    "per_camera_usage",
    "summary",
    "get_summary",
    "status",
    "get_status",
    "usage",
    "get_usage",
    "analyze_async",
]
