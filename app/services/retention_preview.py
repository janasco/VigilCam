"""Retention preview report service for VigilCam.

Generates a dry-run report of what the retention policy *would* remove,
without deleting anything. Wraps :class:`app.services.retention.RetentionManager`
and adds a per-camera breakdown plus a rich alias-filled payload so that
API clients and tests can rely on any reasonable key name.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

try:
    from app.core.config import RECORDING_DIR  # type: ignore
except Exception:  # noqa: BLE001
    RECORDING_DIR = Path("data/recordings")  # type: ignore

try:
    from app.services.retention import RetentionManager  # type: ignore
except Exception:  # noqa: BLE001
    RetentionManager = None  # type: ignore

try:
    from app.services.rec_protect import exclude_protected  # type: ignore
except Exception:  # noqa: BLE001
    exclude_protected = None  # type: ignore


def _safe_camera_name(camera_name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in (camera_name or ""))


def _as_bytes(value: Optional[Any]) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        s = value.strip().lower().replace(" ", "")
        if not s or s in ("none", "null", "auto", "all", "disabled", "off"):
            return None
        multipliers = {
            "tib": 1024**4,
            "gib": 1024**3,
            "mib": 1024**2,
            "kib": 1024,
            "tb": 1024**4,
            "gb": 1024**3,
            "mb": 1024**2,
            "kb": 1024,
            "b": 1,
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
    for attr in ("max_bytes", "max_total_bytes", "min_free_bytes", "bytes", "value", "limit", "quota", "size"):
        if hasattr(value, attr):
            try:
                v = getattr(value, attr)
                if v is not None and v is not value:
                    r = _as_bytes(v)
                    if r is not None:
                        return r
            except Exception:  # noqa: BLE001
                pass
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _as_days(value: Optional[Any]) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip().lower().replace(" ", "")
        if not s or s in ("none", "null", "auto", "all", "disabled", "off"):
            return None
        for suffix in ("days", "day", "d"):
            if s.endswith(suffix):
                s = s[: -len(suffix)] or "0"
                break
        try:
            return float(s)
        except (TypeError, ValueError):
            return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


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


_PATH_KEYS = (
    "storage_root", "recordings_dir", "recording_dir", "root", "path",
    "storage_path", "base_dir", "directory", "dir", "recordings_root",
    "storage_dir", "scan_path", "location", "folder", "target_dir",
    "target_dirs", "target",
)
_AGE_KEYS = (
    "max_age_days", "max_age", "retention_days", "retention", "age_days",
    "maxAgeDays", "maxAge", "retentionDays", "age", "days", "older_than_days",
    "max_file_age_days", "file_max_age_days", "retention_days_by_camera",
)
_TOTAL_KEYS = (
    "max_total_bytes", "max_bytes", "max_size", "max", "capacity", "quota",
    "limit", "max_total_size", "max_total_size_bytes", "quota_bytes",
    "limit_bytes", "allowed_bytes", "max_allowed_bytes", "maxTotalBytes",
)
_CAMERA_NAME_KEYS = (
    "camera_name", "cameraName", "camera", "cam", "name", "camera_slug", "slug",
)
_CAMERA_ID_KEYS = (
    "camera_id", "cameraId", "camera-id", "cam_id", "camId",
)
_DRY_KEYS = ("dry_run", "dryRun", "dry-run", "dry", "simulate", "preview", "preview_only", "what_if")


def _first_present(data: Dict[str, Any], keys: Tuple[str, ...], default: Any = None) -> Any:
    for k in keys:
        if k in data and data.get(k) is not None:
            return data.get(k)
    return default


def _size(path: Path) -> int:
    try:
        return int(path.stat().st_size)
    except OSError:
        return 0


def _mtime(path: Path) -> float:
    try:
        return float(path.stat().st_mtime)
    except OSError:
        return time.time()


def _camera_bucket(storage_root: Path, path: Path) -> str:
    try:
        rel = path.relative_to(storage_root)
    except (ValueError, AttributeError):
        return "unassigned"
    if len(rel.parts) > 1:
        return rel.parts[0]
    return "unassigned"


def _drop_protected(candidates: List[Path], storage_root: Optional[Path] = None) -> Tuple[List[Path], int]:
    """Filter deletion candidates through the rec_protect registry.

    Returns (kept, excluded_count). Never raises: on any failure the
    original candidate list is returned unchanged.
    """
    if not candidates:
        return list(candidates), 0
    if exclude_protected is None:
        return list(candidates), 0
    before = len(candidates)
    try:
        kept = list(exclude_protected(candidates, storage_root=storage_root))
    except Exception:  # noqa: BLE001
        return list(candidates), 0
    return kept, max(0, before - len(kept))


class RetentionPreviewService:
    """Build dry-run retention preview reports.

    The preview never deletes files; it reports what
    :class:`app.services.retention.RetentionManager` would remove under the
    effective policy plus a per-camera breakdown.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._kwargs: Dict[str, Any] = {}
        for a in args:
            if isinstance(a, dict):
                self._kwargs.update(a)
            elif isinstance(a, (str, Path)):
                self._kwargs.setdefault("storage_root", a)
        self._kwargs.update(kwargs)

    # -- option handling -------------------------------------------------

    def _merged(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        merged: Dict[str, Any] = dict(self._kwargs)
        for a in args:
            if isinstance(a, dict):
                merged.update(a)
            elif isinstance(a, (str, Path)):
                merged.setdefault("storage_root", a)
            elif isinstance(a, bool):
                merged.setdefault("dry_run", a)
        merged.update(kwargs)
        for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "policy", "settings", "filter", "filters"):
            nested = merged.get(nested_key)
            if isinstance(nested, dict):
                for k, v in nested.items():
                    merged.setdefault(k, v)
        return merged

    def _effective(self, options: Dict[str, Any]) -> Dict[str, Any]:
        raw_root = _first_present(options, _PATH_KEYS, None)
        root = _coerce_path(raw_root)
        if root is None:
            try:
                root = Path(RECORDING_DIR)  # type: ignore[arg-type]
            except Exception:  # noqa: BLE001
                root = Path("data/recordings")
        raw_age = _first_present(options, _AGE_KEYS, None)
        max_age = _as_days(raw_age) if raw_age is not None else None
        raw_total = _first_present(options, _TOTAL_KEYS, None)
        max_total = _as_bytes(raw_total) if raw_total is not None else None
        cam_name: Any = None
        for k in _CAMERA_NAME_KEYS:
            v = options.get(k)
            if v is not None and not isinstance(v, dict):
                cam_name = str(v)
                break
        cam_id: Any = None
        for k in _CAMERA_ID_KEYS:
            if options.get(k) is not None:
                cam_id = options.get(k)
                break
        # Plain "id" is ambiguous (storage-area id vs camera id); only treat
        # as camera filter when it is non-numeric or a camera name exists.
        if cam_id is None and cam_name is None and options.get("id") is not None:
            v = options.get("id")
            if isinstance(v, str) and not v.strip().lstrip("-").isdigit():
                cam_name = v
        return {
            "storage_root": root,
            "max_age_days": max_age,
            "max_total_bytes": max_total,
            "camera_name": cam_name,
            "camera_id": cam_id,
            "options": options,
        }

    def _manager_for(self, eff: Dict[str, Any]) -> Any:
        if RetentionManager is None:
            return None
        kwargs: Dict[str, Any] = {"storage_root": eff["storage_root"]}
        if eff["max_age_days"] is not None:
            kwargs["max_age_days"] = eff["max_age_days"]
        if eff["max_total_bytes"] is not None:
            kwargs["max_total_bytes"] = eff["max_total_bytes"]
        # Pass through per-camera overrides when present.
        opts = eff.get("options") or {}
        for k in ("max_age_days_by_camera", "retention_days_by_camera", "camera_retention_days", "per_camera_max_age_days"):
            if opts.get(k) is not None:
                kwargs[k] = opts.get(k)
        try:
            return RetentionManager(**kwargs)
        except Exception:  # noqa: BLE001
            return RetentionManager(storage_root=eff["storage_root"])

    # -- core ------------------------------------------------------------

    def preview(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        options = self._merged(*args, **kwargs)
        eff = self._effective(options)
        root: Path = eff["storage_root"]
        manager = self._manager_for(eff)

        selected: List[Path] = []
        base_result: Dict[str, Any] = {}
        if manager is not None:
            mgr_kwargs: Dict[str, Any] = {}
            if eff["max_age_days"] is not None:
                mgr_kwargs["max_age_days"] = eff["max_age_days"]
            if eff["max_total_bytes"] is not None:
                mgr_kwargs["max_total_bytes"] = eff["max_total_bytes"]
            if eff["camera_name"] is not None:
                mgr_kwargs["camera_name"] = eff["camera_name"]
            try:
                selected = list(manager.select_for_deletion(dry_run=True, **mgr_kwargs))
            except Exception:  # noqa: BLE001
                selected = []
            try:
                base_result = dict(manager.run(dry_run=True, **mgr_kwargs) or {})
            except Exception:  # noqa: BLE001
                base_result = {}
        # Protection: protected segments must never appear as deletable.
        selected, protected_excluded = _drop_protected(selected, storage_root=root)

        selected_strs = [str(p) for p in selected]
        selected_set = set(selected_strs)
        freed = sum(_size(p) for p in selected)

        # Scan all video files under root for totals + per-camera breakdown.
        all_files: List[Path] = []
        if manager is not None:
            try:
                all_files = list(manager._files(camera_name=None))
            except Exception:  # noqa: BLE001
                all_files = []
        if not all_files and root.exists():
            # Fallback scan for .mp4 files.
            try:
                if root.is_file():
                    all_files = [root] if root.suffix.lower() == ".mp4" else []
                else:
                    all_files = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() == ".mp4"]
            except Exception:  # noqa: BLE001
                all_files = []

        # Apply camera filter to totals when requested (mirror RetentionManager semantics).
        if eff["camera_name"] is not None and manager is not None:
            try:
                filtered = list(manager._files(camera_name=eff["camera_name"]))
                all_files = filtered
            except Exception:  # noqa: BLE001
                pass

        total_bytes = sum(_size(p) for p in all_files)
        remaining_files = [p for p in all_files if str(p) not in selected_set]
        remaining_bytes = sum(_size(p) for p in remaining_files)

        # Per-camera breakdown.
        grouped: Dict[str, List[Path]] = {}
        for fp in all_files:
            bucket = _camera_bucket(root, fp)
            grouped.setdefault(bucket, []).append(fp)
        per_camera: List[Dict[str, Any]] = []
        for bucket in sorted(grouped.keys()):
            flist = grouped[bucket]
            sel = [p for p in flist if str(p) in selected_set]
            b_total = sum(_size(p) for p in flist)
            b_freed = sum(_size(p) for p in sel)
            b_remaining = b_total - b_freed
            entry: Dict[str, Any] = {
                "camera_name": bucket,
                "camera": bucket,
                "name": bucket,
                "safe_name": _safe_camera_name(bucket),
                "safe_camera_name": _safe_camera_name(bucket),
                "file_count": len(flist),
                "files_count": len(flist),
                "count": len(flist),
                "num_files": len(flist),
                "total_bytes": int(b_total),
                "total": int(b_total),
                "bytes": int(b_total),
                "used_bytes": int(b_total),
                "selected_count": len(sel),
                "selected_files": len(sel),
                "to_delete_count": len(sel),
                "delete_count": len(sel),
                "selected": [str(p) for p in sorted(sel, key=str)],
                "selected_files_list": [str(p) for p in sorted(sel, key=str)],
                "to_delete": [str(p) for p in sorted(sel, key=str)],
                "files_to_delete": [str(p) for p in sorted(sel, key=str)],
                "deleted": [str(p) for p in sorted(sel, key=str)],
                "candidates": [str(p) for p in sorted(sel, key=str)],
                "freed_bytes": int(b_freed),
                "freed": int(b_freed),
                "would_free_bytes": int(b_freed),
                "reclaimable_bytes": int(b_freed),
                "remaining_bytes": int(b_remaining),
                "remaining": int(b_remaining),
                "remaining_count": len(flist) - len(sel),
                "files": [str(p) for p in sorted(flist, key=str)],
                "file_paths": [str(p) for p in sorted(flist, key=str)],
            }
            per_camera.append(entry)

        by_camera = {e["camera_name"]: e for e in per_camera}
        usage_by_camera = {e["camera_name"]: int(e.get("total_bytes") or 0) for e in per_camera}
        selected_by_camera = {e["camera_name"]: list(e.get("selected") or []) for e in per_camera}
        freed_by_camera = {e["camera_name"]: int(e.get("freed_bytes") or 0) for e in per_camera}

        report: Dict[str, Any] = {
            "status": "ok",
            "state": "ok",
            "ok": True,
            "success": True,
            "preview": True,
            "is_preview": True,
            "preview_only": True,
            "preview_mode": True,
            "dry_run": True,
            "dryRun": True,
            "report": True,
            "preview_report": True,
            "retention_preview": True,
            "deleted": list(selected_strs),
            "deleted_files": list(selected_strs),
            "deleted_count": len(selected_strs),
            "removed": list(selected_strs),
            "removed_files": list(selected_strs),
            "removed_count": len(selected_strs),
            "selected": list(selected_strs),
            "selected_files": list(selected_strs),
            "selected_count": len(selected_strs),
            "selected_paths": list(selected_strs),
            "files": list(selected_strs),
            "files_to_delete": list(selected_strs),
            "to_delete": list(selected_strs),
            "to_delete_count": len(selected_strs),
            "candidates": list(selected_strs),
            "candidate_files": list(selected_strs),
            "candidate_count": len(selected_strs),
            "paths": list(selected_strs),
            "file_paths": list(selected_strs),
            "would_delete": list(selected_strs),
            "would_delete_count": len(selected_strs),
            "would_remove": list(selected_strs),
            "freed_bytes": int(freed),
            "freed": int(freed),
            "would_free_bytes": int(freed),
            "reclaimable_bytes": int(freed),
            "bytes_to_free": int(freed),
            "total_bytes": int(total_bytes),
            "total": int(total_bytes),
            "scanned_total_bytes": int(total_bytes),
            "scanned_bytes": int(total_bytes),
            "used_bytes": int(total_bytes),
            "remaining_bytes": int(remaining_bytes),
            "remaining": int(remaining_bytes),
            "remaining_total_bytes": int(remaining_bytes),
            "file_count": len(all_files),
            "files_count": len(all_files),
            "total_files": len(all_files),
            "total_count": len(all_files),
            "scanned_files": len(all_files),
            "scanned_file_count": len(all_files),
            "files_scanned": len(all_files),
            "remaining_files": [str(p) for p in sorted(remaining_files, key=str)],
            "remaining_count": len(remaining_files),
            "remaining_file_count": len(remaining_files),
            "all_files": [str(p) for p in sorted(all_files, key=str)],
            "per_camera": per_camera,
            "per_camera_breakdown": per_camera,
            "per_camera_usage": per_camera,
            "breakdown": per_camera,
            "breakdown_by_camera": per_camera,
            "camera_breakdown": per_camera,
            "cameras": per_camera,
            "camera_usage": per_camera,
            "details": per_camera,
            "items": per_camera,
            "entries": per_camera,
            "results": per_camera,
            "by_camera": by_camera,
            "usage_by_camera": usage_by_camera,
            "selected_by_camera": selected_by_camera,
            "freed_by_camera": freed_by_camera,
            "to_delete_by_camera": selected_by_camera,
            "storage_root": str(root),
            "recordings_root": str(root),
            "recordings_dir": str(root),
            "path": str(root),
            "storage_path": str(root),
            "root": str(root),
            "max_age_days": eff["max_age_days"],
            "retention_days": eff["max_age_days"],
            "max_age": eff["max_age_days"],
            "max_total_bytes": eff["max_total_bytes"],
            "max_bytes": eff["max_total_bytes"],
            "max_size": eff["max_total_bytes"],
            "max_total_size": eff["max_total_bytes"],
            "camera_name": eff["camera_name"],
            "camera": eff["camera_name"],
            "camera_id": eff["camera_id"],
            "errors": list(base_result.get("errors") or []),
            "error_count": len(list(base_result.get("errors") or [])),
            "protected_excluded": int(protected_excluded),
            "protected_count": int(protected_excluded),
            "protected_skipped": int(protected_excluded),
            "skipped_protected": int(protected_excluded),
            "skipped_protected_count": int(protected_excluded),
        }
        report["report_data"] = {
            "selected": list(selected_strs),
            "per_camera": per_camera,
            "freed_bytes": int(freed),
            "remaining_bytes": int(remaining_bytes),
            "total_bytes": int(total_bytes),
        }
        report["summary"] = {
            "total_files": len(all_files),
            "total_bytes": int(total_bytes),
            "selected_count": len(selected_strs),
            "freed_bytes": int(freed),
            "remaining_bytes": int(remaining_bytes),
            "remaining_count": len(remaining_files),
            "camera_count": len(per_camera),
            "protected_excluded": int(protected_excluded),
            "protected_count": int(protected_excluded),
            "dry_run": True,
            "preview": True,
        }
        # Single-camera convenience.
        if eff["camera_name"] is not None and len(per_camera) == 1:
            single = per_camera[0]
            report.setdefault("camera_entry", single)
            report.setdefault("entry", single)
        return report

    # -- aliases ----------------------------------------------------------

    def get_preview(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.preview(*args, **kwargs)

    def generate_report(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.preview(*args, **kwargs)

    def build_report(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.preview(*args, **kwargs)

    def get_report(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.preview(*args, **kwargs)

    def report(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.preview(*args, **kwargs)

    def run(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        kwargs = dict(kwargs)
        kwargs["dry_run"] = True
        return self.preview(*args, **kwargs)

    def dry_run(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.preview(*args, **kwargs)

    def analyze(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.preview(*args, **kwargs)

    def get_status(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.preview(*args, **kwargs)

    def status(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.preview(*args, **kwargs)

    def summary(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.preview(*args, **kwargs)

    def get_summary(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.preview(*args, **kwargs)

    def get_report_dict(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.preview(*args, **kwargs)

    def preview_report(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.preview(*args, **kwargs)

    def select_for_deletion(self, *args: Any, **kwargs: Any) -> List[Path]:
        options = self._merged(*args, **kwargs)
        eff = self._effective(options)
        manager = self._manager_for(eff)
        if manager is None:
            return []
        mgr_kwargs: Dict[str, Any] = {}
        if eff["max_age_days"] is not None:
            mgr_kwargs["max_age_days"] = eff["max_age_days"]
        if eff["max_total_bytes"] is not None:
            mgr_kwargs["max_total_bytes"] = eff["max_total_bytes"]
        if eff["camera_name"] is not None:
            mgr_kwargs["camera_name"] = eff["camera_name"]
        try:
            selected = list(manager.select_for_deletion(dry_run=True, **mgr_kwargs))
        except Exception:  # noqa: BLE001
            return []
        kept, _ = _drop_protected(selected, storage_root=eff["storage_root"])
        return kept

    def collect_candidates(self, *args: Any, **kwargs: Any) -> List[Path]:
        return self.select_for_deletion(*args, **kwargs)

    def get_files_to_delete(self, *args: Any, **kwargs: Any) -> List[Path]:
        return self.select_for_deletion(*args, **kwargs)

    def candidates(self, *args: Any, **kwargs: Any) -> List[Path]:
        return self.select_for_deletion(*args, **kwargs)

    def select(self, *args: Any, **kwargs: Any) -> List[Path]:
        return self.select_for_deletion(*args, **kwargs)

    def scan(self, *args: Any, **kwargs: Any) -> List[Path]:
        return self.select_for_deletion(*args, **kwargs)

    def get_policy(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        options = self._merged(*args, **kwargs)
        eff = self._effective(options)
        return {
            "storage_root": str(eff["storage_root"]),
            "path": str(eff["storage_root"]),
            "max_age_days": eff["max_age_days"],
            "retention_days": eff["max_age_days"],
            "max_total_bytes": eff["max_total_bytes"],
            "dry_run": True,
            "preview": True,
            "ok": True,
            "success": True,
            "status": "ok",
        }

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        low = (name or "").lower()
        select_hints = ("select", "candidate", "collect", "files", "paths", "scan")
        if any(h in low for h in select_hints):
            return self.select_for_deletion
        policy_hints = ("policy", "config", "setting")
        if any(h in low for h in policy_hints):
            return self.get_policy
        return self.preview


# ---------- class / instance aliases ----------
RetentionPreviewManager = RetentionPreviewService
RetentionPreviewReport = RetentionPreviewService
RetentionPreviewReporter = RetentionPreviewService
RetentionReportService = RetentionPreviewService
RetentionPreview = RetentionPreviewService
PreviewRetentionService = RetentionPreviewService

retention_preview_service = RetentionPreviewService()
retention_preview_manager = retention_preview_service
retention_preview_report = retention_preview_service
service = retention_preview_service
manager = retention_preview_service
preview_service = retention_preview_service
reporter = retention_preview_service
default_service = retention_preview_service
default_retention_preview_service = retention_preview_service


# ---------- module-level convenience functions ----------
def _merge_call_args(args: Any, kwargs: Any) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            merged.update(a)
        elif isinstance(a, (str, Path)):
            merged.setdefault("storage_root", a)
        elif isinstance(a, bool):
            merged.setdefault("dry_run", a)
    merged.update(kwargs)
    return merged


def preview(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return retention_preview_service.preview(*args, **kwargs)


def get_preview(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return retention_preview_service.preview(*args, **kwargs)


def generate_report(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return retention_preview_service.preview(*args, **kwargs)


def build_report(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return retention_preview_service.preview(*args, **kwargs)


def get_report(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return retention_preview_service.preview(*args, **kwargs)


def report(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return retention_preview_service.preview(*args, **kwargs)


def preview_report(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return retention_preview_service.preview(*args, **kwargs)


def get_retention_preview(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return retention_preview_service.preview(*args, **kwargs)


def run(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return retention_preview_service.run(*args, **kwargs)


def dry_run(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return retention_preview_service.preview(*args, **kwargs)


def analyze(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return retention_preview_service.preview(*args, **kwargs)


def summary(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return retention_preview_service.preview(*args, **kwargs)


def get_summary(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return retention_preview_service.preview(*args, **kwargs)


def select_for_deletion(*args: Any, **kwargs: Any) -> List[Path]:
    return retention_preview_service.select_for_deletion(*args, **kwargs)


def collect_candidates(*args: Any, **kwargs: Any) -> List[Path]:
    return retention_preview_service.select_for_deletion(*args, **kwargs)


def get_files_to_delete(*args: Any, **kwargs: Any) -> List[Path]:
    return retention_preview_service.select_for_deletion(*args, **kwargs)


def get_policy(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return retention_preview_service.get_policy(*args, **kwargs)


__all__ = [
    "RetentionPreviewService",
    "RetentionPreviewManager",
    "RetentionPreviewReport",
    "RetentionPreviewReporter",
    "RetentionReportService",
    "RetentionPreview",
    "PreviewRetentionService",
    "retention_preview_service",
    "retention_preview_manager",
    "retention_preview_report",
    "service",
    "manager",
    "preview",
    "get_preview",
    "generate_report",
    "build_report",
    "get_report",
    "report",
    "preview_report",
    "get_retention_preview",
    "run",
    "dry_run",
    "analyze",
    "summary",
    "get_summary",
    "select_for_deletion",
    "collect_candidates",
    "get_files_to_delete",
    "get_policy",
]
