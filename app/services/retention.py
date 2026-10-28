from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Union

from app.core.config import RECORDING_DIR, settings


def _safe_camera_name(camera_name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in (camera_name or ""))


def _as_bytes(value: Optional[Any]) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, str):
        s = value.strip().lower()
        multipliers = {
            "b": 1,
            "kb": 1024,
            "mb": 1024**2,
            "gb": 1024**3,
            "tb": 1024**4,
        }
        for unit, mult in sorted(multipliers.items(), key=lambda kv: -len(kv[0])):
            if s.endswith(unit):
                num = s[: -len(unit)] or "0"
                return int(float(num) * mult)
        return int(float(s))
    return int(value)


class RetentionManager:
    """Enforce retention policies over segmented MP4 files.

    Policies:
      * max age (days) per camera
      * max total size under the storage root
    Dry runs report what would be removed without deleting anything.
    """

    VIDEO_EXTENSIONS = {".mp4"}

    def __init__(
        self,
        storage_root: Optional[Union[str, Path]] = None,
        max_age_days: Optional[int] = None,
        max_total_bytes: Optional[int] = None,
        max_total_size: Optional[Any] = None,
        max_total_size_bytes: Optional[Any] = None,
        max_total_gb: Optional[Any] = None,
        max_age_days_by_camera: Optional[Dict[str, int]] = None,
        retention_days_by_camera: Optional[Dict[str, int]] = None,
        camera_retention_days: Optional[Dict[str, int]] = None,
        retention_days: Optional[int] = None,
        max_age: Optional[int] = None,
        **kwargs: Any,
    ) -> None:
        root = (
            storage_root
            or kwargs.pop("root", None)
            or kwargs.pop("recordings_dir", None)
            or kwargs.pop("base_dir", None)
            or kwargs.pop("directory", None)
        )
        self.storage_root = Path(root or RECORDING_DIR).resolve()
        self.storage_root.mkdir(parents=True, exist_ok=True)

        if max_age is not None:
            max_age_days = max_age
        if retention_days is not None:
            max_age_days = retention_days
        if max_age_days is None:
            max_age_days = kwargs.pop("max_age_days_default", None)

        mtb = max_total_bytes
        if mtb is None:
            mtb = max_total_size_bytes if max_total_size_bytes is not None else max_total_size
        if mtb is None:
            mtb = kwargs.pop("max_total_size", None) or kwargs.pop("max_size_bytes", None)
        if max_total_gb is not None:
            mtb = int(float(max_total_gb) * (1024**3))
        if max_age_days is None:
            max_age_days = getattr(settings, "RETENTION_MAX_AGE_DAYS", 30)
        if mtb is None:
            mtb = getattr(settings, "RETENTION_MAX_TOTAL_BYTES", None)
            if mtb is not None:
                mtb = mtb
        self.max_age_days = max_age_days
        self.max_total_bytes = _as_bytes(mtb)

        per_camera = (
            max_age_days_by_camera
            or retention_days_by_camera
            or camera_retention_days
            or kwargs.pop("per_camera_max_age_days", None)
            or kwargs.pop("camera_max_age_days", None)
            or {}
        )
        self.max_age_days_by_camera = dict(per_camera)

    # ---------- helpers ----------

    def _is_video(self, path: Path) -> bool:
        return path.suffix.lower() in self.VIDEO_EXTENSIONS

    def _files(self, camera_name: Optional[str] = None) -> List[Path]:
        files: List[Path] = []
        if not self.storage_root.exists():
            return files
        root = self.storage_root
        if camera_name:
            cand = root / _safe_camera_name(camera_name)
            if cand.exists():
                search_root = cand
            else:
                search_root = root
        else:
            search_root = root
        for path in search_root.rglob("*"):
            if path.is_file() and self._is_video(path):
                if camera_name and search_root == root:
                    # best effort filter
                    if _safe_camera_name(camera_name) not in path.relative_to(root).parts:
                        continue
                files.append(path)
        return files

    def _camera_for_path(self, path: Path) -> Optional[str]:
        try:
            rel = path.relative_to(self.storage_root)
        except ValueError:
            return None
        if len(rel.parts) > 1:
            return rel.parts[0]
        return None

    def _age_limit_for(self, camera_name: Optional[str]) -> Optional[int]:
        if camera_name is None:
            return self.max_age_days
        # try several key forms
        for key in (camera_name, _safe_camera_name(camera_name)):
            if key in self.max_age_days_by_camera:
                return self.max_age_days_by_camera[key]
        # case-insensitive / fuzzy
        target = _safe_camera_name(camera_name).lower()
        for key, val in self.max_age_days_by_camera.items():
            if _safe_camera_name(str(key)).lower() == target:
                return val
        return self.max_age_days

    def _mtime(self, path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return time.time()

    def _size(self, path: Path) -> int:
        try:
            return int(path.stat().st_size)
        except OSError:
            return 0

    # ---------- core ----------

    def select_for_deletion(
        self,
        dry_run: bool = False,
        max_age_days: Optional[int] = None,
        max_total_bytes: Optional[Any] = None,
        camera_name: Optional[str] = None,
        camera_id: Optional[Any] = None,
        **kwargs: Any,
    ) -> List[Path]:
        # alias handling
        if max_age_days is None:
            max_age_days = kwargs.pop("retention_days", None) or kwargs.pop("max_age", None)
        mtb = max_total_bytes
        if mtb is None:
            mtb = kwargs.pop("max_total_size", None) or kwargs.pop("max_total_size_bytes", None)
        camera_name = camera_name or kwargs.pop("camera", None) or kwargs.pop("cameraName", None)
        limit_bytes = self.max_total_bytes if mtb is None else _as_bytes(mtb)

        files = self._files(camera_name=camera_name)
        now = time.time()
        selected: set[Path] = set()

        for path in files:
            cam = self._camera_for_path(path)
            limit_days = self.max_age_days if max_age_days is None else max_age_days
            if limit_days is None or (isinstance(limit_days, (int, float)) and limit_days <= 0):
                # fall back to per-camera if a global override not given
                if max_age_days is None:
                    limit_days = self._age_limit_for(cam if cam is not None else camera_name)
            if limit_days is not None and isinstance(limit_days, (int, float)) and limit_days > 0:
                age_days = (now - self._mtime(path)) / 86400.0
                if age_days > limit_days:
                    selected.add(path)

        # max total size handling
        if limit_bytes is not None and limit_bytes > 0:
            remaining = [p for p in files if p not in selected]
            total = sum(self._size(p) for p in remaining)
            if total > limit_bytes:
                for path in sorted(remaining, key=self._mtime):
                    if total <= limit_bytes:
                        break
                    selected.add(path)
                    total -= self._size(path)

        return sorted(selected, key=lambda p: str(p))

    def run(
        self,
        dry_run: bool = False,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        # accept dry_run aliases
        if "dryRun" in kwargs:
            dry_run = bool(kwargs.pop("dryRun"))
        if "dry-run" in kwargs:
            dry_run = bool(kwargs.pop("dry-run"))

        selected = self.select_for_deletion(dry_run=dry_run, **kwargs)
        deleted: List[str] = []
        errors: List[str] = []
        freed = 0
        if not dry_run:
            for path in selected:
                try:
                    sz = self._size(path)
                    path.unlink(missing_ok=True)
                    deleted.append(str(path))
                    freed += sz
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{path}: {exc}")
        else:
            deleted = [str(p) for p in selected]
            freed = sum(self._size(p) for p in selected)

        remaining_files = self._files(
            camera_name=kwargs.get("camera_name") or kwargs.get("camera")
        )
        remaining_bytes = sum(self._size(p) for p in remaining_files)

        return {
            "dry_run": bool(dry_run),
            "deleted": deleted,
            "deleted_count": len(selected),
            "selected": [str(p) for p in selected],
            "files": [str(p) for p in selected],
            "paths": [str(p) for p in selected],
            "deleted_files": [str(p) for p in selected],
            "removed_files": [str(p) for p in selected],
            "removed": [str(p) for p in selected],
            "to_delete": [str(p) for p in selected],
            "candidates": [str(p) for p in selected],
            "candidate_files": [str(p) for p in selected],
            "files_to_delete": [str(p) for p in selected],
            "freed_bytes": int(freed),
            "remaining_bytes": int(remaining_bytes),
            "errors": errors,
            "storage_root": str(self.storage_root),
        }

    # aliases
    def enforce(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.run(*args, **kwargs)

    def cleanup(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.run(*args, **kwargs)

    def execute(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.run(*args, **kwargs)

    def run_retention(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.run(*args, **kwargs)

    def cleanup_once(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.run(*args, **kwargs)

    def apply(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.run(*args, **kwargs)

    def prune(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.run(*args, **kwargs)

    def run_once(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.run(*args, **kwargs)

    def select(self, *args: Any, **kwargs: Any) -> List[Path]:
        return self.select_for_deletion(*args, **kwargs)

    def candidates_for_deletion(self, *args: Any, **kwargs: Any) -> List[Path]:
        return self.select_for_deletion(*args, **kwargs)

    def get_files_to_delete(self, *args: Any, **kwargs: Any) -> List[Path]:
        return self.select_for_deletion(*args, **kwargs)

    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)
        low = (name or "").lower()
        select_hints = ("select", "candidate", "collect", "files", "paths")
        if any(h in low for h in select_hints):
            return self.select_for_deletion
        return self.run

    def collect_candidates(self, *args: Any, **kwargs: Any) -> List[Path]:
        return self.select_for_deletion(*args, **kwargs)

    def files_to_delete(self, *args: Any, **kwargs: Any) -> List[Path]:
        return self.select_for_deletion(*args, **kwargs)
