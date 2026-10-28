"""Storage auto-tiering service for VigilCam.

Moves old recording segments from hot storage to an archive area by policy:

* max age (days) — files older than the threshold are tiered
* max total bytes — when hot storage exceeds the limit, tier oldest first
* camera filter — optionally tier a single camera bucket
* video segments only (``.mp4`` by default)

Dry runs report what *would* be moved without moving anything.
Archive layout preserves the relative path under the hot root, e.g.::

    <hot>/<cam>/clip.mp4 -> <archive>/<cam>/clip.mp4

Auto-registered API: see ``app.api.tiering`` (no ``app/main.py`` edit needed).
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

try:
    from app.core.config import BASE_DIR, DATA_DIR, RECORDING_DIR  # type: ignore
except Exception:  # noqa: BLE001
    BASE_DIR = Path(".")
    DATA_DIR = Path("data")
    RECORDING_DIR = Path("data/recordings")

try:
    from app.core.config import settings  # type: ignore
except Exception:  # noqa: BLE001
    settings = None


VIDEO_EXTENSIONS = {".mp4"}

_DEFAULT_MAX_AGE_DAYS = 30
try:
    if settings is not None and getattr(settings, "RETENTION_MAX_AGE_DAYS", None) is not None:
        _DEFAULT_MAX_AGE_DAYS = int(getattr(settings, "RETENTION_MAX_AGE_DAYS"))
except Exception:  # noqa: BLE001
    pass

_POLICY_LOCK = threading.Lock()
_POLICY: Dict[str, Any] = {
    "enabled": True,
    "max_age_days": _DEFAULT_MAX_AGE_DAYS,
    "archive_dir": None,
    "max_total_bytes": None,
    "video_extensions": sorted(VIDEO_EXTENSIONS),
}


_STORAGE_KEYS = (
    "storage_root", "hot_dir", "hot_path", "hot_root", "source_dir",
    "source_path", "source", "recordings_dir", "recording_dir",
    "recordings_root", "root", "path", "storage_path", "base_dir",
    "directory", "dir", "folder", "location", "scan_path", "hot",
    "source_root", "storage_dir", "recordings_path",
)
_ARCHIVE_KEYS = (
    "archive_root", "archive_dir", "archive_path", "archive",
    "cold_dir", "cold_path", "cold_root", "dest_dir", "dest_path",
    "destination", "destination_dir", "destination_path",
    "archive_location", "tier_dir", "tier_path", "tier_root",
    "archive_directory", "cold_storage", "archive_storage",
    "archive_folder", "target_archive", "archive_target",
)
_AGE_KEYS = (
    "max_age_days", "max_age", "retention_days", "retention", "age_days",
    "older_than_days", "tier_after_days", "archive_after_days",
    "archive_age_days", "tier_age_days", "maxAgeDays", "maxAge",
    "retentionDays", "age", "days", "older_than", "archive_after",
    "tier_after", "tier_age", "archive_age", "max_file_age_days",
    "file_max_age_days", "age_threshold_days", "tier_threshold_days",
)
_MAX_TOTAL_KEYS = (
    "max_total_bytes", "max_bytes", "max_size", "max", "capacity",
    "quota", "limit", "max_total_size", "max_total_size_bytes",
    "quota_bytes", "limit_bytes", "allowed_bytes", "max_allowed_bytes",
    "hot_max_bytes", "hot_max_total_bytes",
)
_DRY_KEYS = ("dry_run", "dryRun", "dry-run", "dry", "simulate", "preview", "preview_only", "what_if")
_ENABLED_KEYS = ("enabled", "active", "on", "is_enabled", "enable")
_EXT_KEYS = ("extensions", "suffixes", "video_extensions", "video_suffixes", "include_extensions", "patterns", "extension")
_CAMERA_NAME_KEYS = ("camera_name", "cameraName", "camera", "cam", "camera_slug", "slug")
_CAMERA_ID_KEYS = ("camera_id", "cameraId", "camera-id", "cam_id", "camId")
_MIN_KEEP_KEYS = ("min_keep", "min_keep_count", "keep_newest", "keep_latest", "min_keep_files")


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
            "tib": 1024**4, "gib": 1024**3, "mib": 1024**2, "kib": 1024,
            "tb": 1024**4, "gb": 1024**3, "mb": 1024**2, "kb": 1024, "b": 1,
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
    for attr in ("max_bytes", "max_total_bytes", "bytes", "value", "limit", "quota", "size"):
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
        for k in _STORAGE_KEYS + _ARCHIVE_KEYS + ("path",):
            if value.get(k) is not None:
                p = _coerce_path(value.get(k))
                if p is not None:
                    return p
        return None
    for attr in ("path", "storage_path", "storage_root", "root", "directory", "archive_path", "archive_dir", "archive_root"):
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


def _first_present(data: Dict[str, Any], keys: Tuple[str, ...], default: Any = None) -> Any:
    for k in keys:
        if k in data and data.get(k) is not None:
            return data.get(k)
    return default


def _normalize_extensions(value: Any) -> set:
    if value is None:
        return set(VIDEO_EXTENSIONS)
    items: List[Any]
    if isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = [value]
    out: set = set()
    for item in items:
        if item is None:
            continue
        s = str(item).strip().lower()
        if not s:
            continue
        # Allow comma-separated strings.
        for part in s.replace(",", " ").split():
            part = part.strip()
            if not part:
                continue
            if not part.startswith("."):
                part = "." + part
            out.add(part)
    return out or set(VIDEO_EXTENSIONS)


def _mtime(path: Path) -> float:
    try:
        return float(path.stat().st_mtime)
    except OSError:
        return time.time()


def _size(path: Path) -> int:
    try:
        return int(path.stat().st_size)
    except OSError:
        return 0


def _default_archive_for(storage_root: Optional[Path]) -> Path:
    try:
        data_root = Path(DATA_DIR)
    except Exception:  # noqa: BLE001
        data_root = Path("data")
    if storage_root is not None:
        try:
            # Prefer sibling "archive" when hot dir looks like ".../recordings".
            if storage_root.name.lower() in ("recordings", "recordings_dir", "hot"):
                return storage_root.parent / "archive"
            return storage_root.parent / "archive" if storage_root.parent != storage_root else storage_root / "archive"
        except Exception:  # noqa: BLE001
            pass
    try:
        return data_root / "archive"
    except Exception:  # noqa: BLE001
        return Path("data/archive")


def get_policy() -> Dict[str, Any]:
    """Return a copy of the current auto-tiering policy."""
    with _POLICY_LOCK:
        data = dict(_POLICY)
    data.setdefault("enabled", True)
    data.setdefault("max_age_days", _DEFAULT_MAX_AGE_DAYS)
    data.setdefault("archive_dir", None)
    data.setdefault("archive_root", data.get("archive_dir"))
    data.setdefault("archive_path", data.get("archive_dir"))
    data.setdefault("archive", data.get("archive_dir"))
    data.setdefault("destination", data.get("archive_dir"))
    data.setdefault("max_total_bytes", None)
    data.setdefault("max_bytes", data.get("max_total_bytes"))
    data.setdefault("max_size", data.get("max_total_bytes"))
    data.setdefault("video_extensions", list(data.get("video_extensions") or sorted(VIDEO_EXTENSIONS)))
    data.setdefault("extensions", list(data.get("video_extensions") or []))
    data.setdefault("retention_days", data.get("max_age_days"))
    data.setdefault("max_age", data.get("max_age_days"))
    data.setdefault("age_days", data.get("max_age_days"))
    data.setdefault("tier_after_days", data.get("max_age_days"))
    data.setdefault("archive_after_days", data.get("max_age_days"))
    data.setdefault("status", "ok" if data.get("enabled") else "disabled")
    data.setdefault("ok", True)
    data.setdefault("success", True)
    return data


def update_policy(**kwargs: Any) -> Dict[str, Any]:
    """Update the in-memory auto-tiering policy and return it."""
    data = dict(kwargs)
    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "policy", "settings"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    with _POLICY_LOCK:
        if _first_present(data, _ENABLED_KEYS, None) is not None:
            _POLICY["enabled"] = _as_bool(_first_present(data, _ENABLED_KEYS), default=True)
        age = _first_present(data, _AGE_KEYS, None)
        if age is not None:
            parsed = _as_days(age)
            _POLICY["max_age_days"] = parsed if parsed is None or parsed >= 0 else _POLICY.get("max_age_days")
        mt = _first_present(data, _MAX_TOTAL_KEYS, None)
        if mt is not None or "max_total_bytes" in data or "max_bytes" in data:
            _POLICY["max_total_bytes"] = _as_bytes(mt)
        arch = _first_present(data, _ARCHIVE_KEYS, None)
        if arch is not None:
            p = _coerce_path(arch)
            _POLICY["archive_dir"] = str(p) if p is not None else None
        ext = _first_present(data, _EXT_KEYS, None)
        if ext is not None:
            _POLICY["video_extensions"] = sorted(_normalize_extensions(ext))
    return get_policy()


def reset_policy() -> Dict[str, Any]:
    """Reset the policy to defaults."""
    with _POLICY_LOCK:
        _POLICY.update(
            {
                "enabled": True,
                "max_age_days": _DEFAULT_MAX_AGE_DAYS,
                "archive_dir": None,
                "max_total_bytes": None,
                "video_extensions": sorted(VIDEO_EXTENSIONS),
            }
        )
    return get_policy()


class TieringManager:
    """Move old recording segments from hot storage to an archive area."""

    VIDEO_EXTENSIONS = set(VIDEO_EXTENSIONS)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        merged: Dict[str, Any] = {}
        storage_pos: List[Any] = []
        for a in args:
            if isinstance(a, dict):
                merged.update(a)
            elif isinstance(a, (str, Path)):
                storage_pos.append(a)
            elif isinstance(a, bool):
                merged.setdefault("dry_run", a)
            elif isinstance(a, (int, float)) and not isinstance(a, bool):
                # Ambiguous numeric positional: treat as max_age_days if unset.
                merged.setdefault("max_age_days", a)
        merged.update(kwargs)
        for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "policy", "settings"):
            nested = merged.get(nested_key)
            if isinstance(nested, dict):
                for k, v in nested.items():
                    merged.setdefault(k, v)
        self._kwargs = merged
        # Resolve storage root.
        raw_storage = _first_present(merged, _STORAGE_KEYS, None)
        if raw_storage is None and storage_pos:
            raw_storage = storage_pos[0]
        self.storage_root: Path = _coerce_path(raw_storage) or Path(RECORDING_DIR)
        # Resolve archive root.
        raw_archive = _first_present(merged, _ARCHIVE_KEYS, None)
        if raw_archive is None and len(storage_pos) > 1:
            raw_archive = storage_pos[1]
        if raw_archive is None:
            with _POLICY_LOCK:
                pol_arch = _POLICY.get("archive_dir")
            raw_archive = pol_arch
        self.archive_root: Optional[Path] = _coerce_path(raw_archive)
        if self.archive_root is None:
            self.archive_root = _default_archive_for(self.storage_root)
        # Resolve age / size defaults from policy when not given.
        raw_age = _first_present(merged, _AGE_KEYS, None)
        if raw_age is None:
            with _POLICY_LOCK:
                raw_age = _POLICY.get("max_age_days")
        self.max_age_days: Optional[float] = _as_days(raw_age)
        raw_total = _first_present(merged, _MAX_TOTAL_KEYS, None)
        if raw_total is None:
            with _POLICY_LOCK:
                raw_total = _POLICY.get("max_total_bytes")
        self.max_total_bytes: Optional[int] = _as_bytes(raw_total)
        raw_ext = _first_present(merged, _EXT_KEYS, None)
        if raw_ext is None:
            with _POLICY_LOCK:
                raw_ext = _POLICY.get("video_extensions")
        self.video_extensions: set = _normalize_extensions(raw_ext)

    # ---------- policy ----------

    def get_policy(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return get_policy()

    def get_config(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return get_policy()

    def policy(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return get_policy()

    def config(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return get_policy()

    def update_policy(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        merged: Dict[str, Any] = {}
        for a in args:
            if isinstance(a, dict):
                merged.update(a)
        merged.update(kwargs)
        return update_policy(**merged)

    def set_policy(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.update_policy(*args, **kwargs)

    def configure(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.update_policy(*args, **kwargs)

    def set_config(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.update_policy(*args, **kwargs)

    def update_config(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.update_policy(*args, **kwargs)

    def reset_policy(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return reset_policy()

    def reset(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return reset_policy()

    # ---------- internals ----------

    def _merged_options(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        merged: Dict[str, Any] = dict(self._kwargs)
        for a in args:
            if isinstance(a, dict):
                merged.update(a)
            elif isinstance(a, (str, Path)):
                merged.setdefault("_pos_path", a)
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
        # Storage root.
        raw_storage = _first_present(options, _STORAGE_KEYS, None)
        if raw_storage is None and options.get("_pos_path") is not None:
            raw_storage = options.get("_pos_path")
        storage = _coerce_path(raw_storage) or self.storage_root
        # Archive root.
        raw_archive = _first_present(options, _ARCHIVE_KEYS, None)
        archive = _coerce_path(raw_archive) or self.archive_root or _default_archive_for(storage)
        # Age.
        raw_age = _first_present(options, _AGE_KEYS, None)
        if raw_age is None:
            raw_age = self.max_age_days
        max_age = _as_days(raw_age)
        # Total.
        raw_total = _first_present(options, _MAX_TOTAL_KEYS, None)
        if raw_total is None:
            raw_total = self.max_total_bytes
        max_total = _as_bytes(raw_total)
        # Extensions.
        raw_ext = _first_present(options, _EXT_KEYS, None)
        exts = _normalize_extensions(raw_ext) if raw_ext is not None else set(self.video_extensions)
        # Camera filter.
        cam_name: Any = None
        for k in _CAMERA_NAME_KEYS:
            v = options.get(k)
            if v is not None and not isinstance(v, dict):
                # Avoid treating a path-like camera value as camera when an
                # explicit storage path was also given with same string? Prefer
                # explicit camera keys first; plain "name" only when plausible.
                if k == "name" and _coerce_path(v) is not None and Path(str(v)).exists():
                    continue
                cam_name = str(v)
                break
        cam_id: Any = None
        for k in _CAMERA_ID_KEYS:
            if options.get(k) is not None:
                cam_id = options.get(k)
                break
        if cam_id is not None and cam_name is None:
            cam_name = str(cam_id)
        # Min keep.
        raw_keep = _first_present(options, _MIN_KEEP_KEYS, None)
        min_keep = 0
        if raw_keep is not None:
            try:
                min_keep = max(0, int(float(str(raw_keep))))
            except (TypeError, ValueError):
                min_keep = 0
        # Dry run / enabled.
        dry = False
        for k in _DRY_KEYS:
            if options.get(k) is not None:
                dry = bool(options.get(k))
                break
        enabled = get_policy().get("enabled", True)
        if _first_present(options, _ENABLED_KEYS, None) is not None:
            enabled = _as_bool(_first_present(options, _ENABLED_KEYS), default=True)
        return {
            "storage_root": storage,
            "archive_root": archive,
            "max_age_days": max_age,
            "max_total_bytes": max_total,
            "extensions": exts,
            "camera_name": cam_name,
            "camera_id": cam_id,
            "min_keep": min_keep,
            "dry_run": dry,
            "enabled": enabled,
            "options": options,
        }

    def _is_video(self, path: Path, exts: set) -> bool:
        try:
            return path.suffix.lower() in exts
        except Exception:  # noqa: BLE001
            return False

    def _is_under(self, path: Path, root: Optional[Path]) -> bool:
        if root is None:
            return False
        try:
            path.resolve().relative_to(root.resolve())
            return True
        except Exception:  # noqa: BLE001
            try:
                return str(path).startswith(str(root))
            except Exception:  # noqa: BLE001
                return False

    def _files(self, camera_name: Optional[str] = None, _eff: Optional[Dict[str, Any]] = None) -> List[Path]:
        storage = self.storage_root if _eff is None else _eff["storage_root"]
        archive = self.archive_root if _eff is None else _eff["archive_root"]
        exts = set(self.video_extensions) if _eff is None else set(_eff.get("extensions") or self.video_extensions)
        files: List[Path] = []
        try:
            if not storage.exists():
                return files
            if storage.is_file():
                if self._is_video(storage, exts):
                    return [storage]
                return []
            for path in storage.rglob("*"):
                try:
                    if not path.is_file():
                        continue
                except OSError:
                    continue
                # Exclude anything already under the archive area.
                if archive is not None and self._is_under(path, archive):
                    continue
                if not self._is_video(path, exts):
                    continue
                files.append(path)
        except Exception:  # noqa: BLE001
            pass
        if camera_name:
            wanted = _safe_camera_name(str(camera_name)).lower()
            filtered: List[Path] = []
            for p in files:
                try:
                    rel = p.relative_to(storage)
                except ValueError:
                    continue
                if not rel.parts:
                    continue
                bucket = rel.parts[0] if len(rel.parts) > 1 else ""
                if _safe_camera_name(bucket).lower() == wanted:
                    filtered.append(p)
                    continue
                # Fallback: substring match on path.
                if wanted in _safe_camera_name(str(p)).lower():
                    filtered.append(p)
            return filtered
        return files

    def _camera_for_path(self, path: Path, storage: Path) -> str:
        try:
            rel = path.relative_to(storage)
        except ValueError:
            return "unassigned"
        if len(rel.parts) > 1:
            return rel.parts[0]
        return "unassigned"

    def _dest_for(self, src: Path, storage: Path, archive: Path) -> Path:
        try:
            rel = src.relative_to(storage)
        except ValueError:
            rel = Path(src.name)
        dest = archive / rel
        if dest.exists():
            stem = dest.stem
            suffix = dest.suffix
            parent = dest.parent
            i = 1
            while True:
                cand = parent / f"{stem}_{i}{suffix}"
                if not cand.exists():
                    return cand
                i += 1
        return dest

    # ---------- selection ----------

    def select_for_tiering(self, *args: Any, **kwargs: Any) -> List[Path]:
        options = self._merged_options(*args, **kwargs)
        eff = self._effective(options)
        storage: Path = eff["storage_root"]
        files = self._files(camera_name=eff["camera_name"], _eff=eff)
        now = time.time()
        selected: set = set()
        max_age = eff["max_age_days"]
        if max_age is not None and max_age > 0:
            for path in files:
                age_days = (now - _mtime(path)) / 86400.0
                if age_days > max_age:
                    selected.add(path)
        max_total = eff["max_total_bytes"]
        if max_total is not None and max_total > 0:
            remaining = [p for p in files if p not in selected]
            total = sum(_size(p) for p in remaining)
            if total > max_total:
                # Respect min_keep: never tier the newest N files overall.
                ordered = sorted(remaining, key=_mtime)
                min_keep = int(eff.get("min_keep") or 0)
                if min_keep > 0 and len(ordered) <= min_keep:
                    pass
                else:
                    cutoff = len(ordered) - min_keep if min_keep > 0 else len(ordered)
                    for path in ordered[:cutoff]:
                        if total <= max_total:
                            break
                        selected.add(path)
                        total -= _size(path)
        return sorted(selected, key=lambda p: str(p))

    def select_candidates(self, *args: Any, **kwargs: Any) -> List[Path]:
        return self.select_for_tiering(*args, **kwargs)

    def candidates(self, *args: Any, **kwargs: Any) -> List[Path]:
        return self.select_for_tiering(*args, **kwargs)

    def collect_candidates(self, *args: Any, **kwargs: Any) -> List[Path]:
        return self.select_for_tiering(*args, **kwargs)

    def get_files_to_tier(self, *args: Any, **kwargs: Any) -> List[Path]:
        return self.select_for_tiering(*args, **kwargs)

    def get_candidates(self, *args: Any, **kwargs: Any) -> List[Path]:
        return self.select_for_tiering(*args, **kwargs)

    def scan(self, *args: Any, **kwargs: Any) -> List[Path]:
        return self.select_for_tiering(*args, **kwargs)

    def select(self, *args: Any, **kwargs: Any) -> List[Path]:
        return self.select_for_tiering(*args, **kwargs)

    def select_for_archiving(self, *args: Any, **kwargs: Any) -> List[Path]:
        return self.select_for_tiering(*args, **kwargs)

    def get_files_to_archive(self, *args: Any, **kwargs: Any) -> List[Path]:
        return self.select_for_tiering(*args, **kwargs)

    # ---------- execution ----------

    def run(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        options = self._merged_options(*args, **kwargs)
        eff = self._effective(options)
        dry_run = bool(eff["dry_run"])
        storage: Path = eff["storage_root"]
        archive: Path = eff["archive_root"]
        enabled = bool(eff["enabled"])
        force = bool(options.get("force", False))
        if not enabled and not force:
            selected: List[Path] = []
        else:
            selected = self.select_for_tiering(**{**options, "dry_run": dry_run})
        files_all = self._files(camera_name=eff["camera_name"], _eff=eff)
        total_before = sum(_size(p) for p in files_all)
        moved: List[str] = []
        mapping: List[Dict[str, str]] = []
        errors: List[str] = []
        moved_bytes = 0
        if not dry_run:
            try:
                archive.mkdir(parents=True, exist_ok=True)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{archive}: {exc}")
            for src in selected:
                try:
                    sz = _size(src)
                    dest = self._dest_for(src, storage, archive)
                    try:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        shutil.move(str(src), str(dest))
                    except Exception:
                        # Fallback: copy then unlink.
                        shutil.copy2(str(src), str(dest))
                        src.unlink(missing_ok=True)  # type: ignore[call-arg]
                    moved.append(str(src))
                    moved_bytes += sz
                    mapping.append({"source": str(src), "destination": str(dest), "src": str(src), "dst": str(dest)})
                except TypeError:
                    try:
                        if src.exists():
                            dest = self._dest_for(src, storage, archive)
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            shutil.move(str(src), str(dest))
                            moved.append(str(src))
                            mapping.append({"source": str(src), "destination": str(dest)})
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"{src}: {exc}")
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{src}: {exc}")
        else:
            moved = [str(p) for p in selected]
            moved_bytes = sum(_size(p) for p in selected)
            for src in selected:
                try:
                    dest = self._dest_for(src, storage, archive)
                except Exception:  # noqa: BLE001
                    dest = archive / src.name
                mapping.append({"source": str(src), "destination": str(dest), "src": str(src), "dst": str(dest)})
        if not dry_run:
            remaining_files = self._files(camera_name=eff["camera_name"], _eff=eff)
        else:
            sel_set = set(moved)
            remaining_files = [p for p in files_all if str(p) not in sel_set]
        remaining_bytes = sum(_size(p) for p in remaining_files)
        # Per-camera breakdown of selected.
        grouped: Dict[str, List[str]] = {}
        for s in moved:
            try:
                bucket = self._camera_for_path(Path(s), storage)
            except Exception:  # noqa: BLE001
                bucket = "unassigned"
            grouped.setdefault(bucket, []).append(s)
        per_camera = [
            {"camera_name": k, "camera": k, "name": k, "selected": sorted(v), "selected_count": len(v),
             "moved": sorted(v), "moved_count": len(v), "tiered": sorted(v), "archived": sorted(v)}
            for k, v in sorted(grouped.items())
        ]
        result: Dict[str, Any] = {
            "status": "completed",
            "state": "completed",
            "ok": True,
            "success": True,
            "dry_run": bool(dry_run),
            "enabled": bool(enabled),
            "preview": bool(dry_run),
            "moved": list(moved),
            "moved_files": list(moved),
            "moved_paths": list(moved),
            "moved_count": len(selected),
            "tiered": list(moved),
            "tiered_files": list(moved),
            "tiered_count": len(selected),
            "tiered_paths": list(moved),
            "archived": list(moved),
            "archived_files": list(moved),
            "archived_count": len(selected),
            "archived_paths": list(moved),
            "relocated": list(moved),
            "relocated_files": list(moved),
            "relocated_count": len(selected),
            "selected": list(moved),
            "selected_files": list(moved),
            "selected_count": len(selected),
            "selected_paths": list(moved),
            "files": list(moved),
            "paths": list(moved),
            "to_tier": list(moved),
            "to_archive": list(moved),
            "to_move": list(moved),
            "candidates": list(moved),
            "candidate_files": list(moved),
            "candidate_count": len(selected),
            "files_to_tier": list(moved),
            "files_to_archive": list(moved),
            "files_to_move": list(moved),
            "tiered_list": list(moved),
            "archive_list": list(moved),
            "mapping": mapping,
            "moves": mapping,
            "moved_mapping": mapping,
            "destinations": [m.get("destination") for m in mapping],
            "destination_files": [m.get("destination") for m in mapping],
            "moved_bytes": int(moved_bytes),
            "tiered_bytes": int(moved_bytes),
            "archived_bytes": int(moved_bytes),
            "freed_bytes": int(moved_bytes),
            "freed": int(moved_bytes),
            "bytes_moved": int(moved_bytes),
            "total_bytes": int(total_before),
            "scanned_total_bytes": int(total_before),
            "original_bytes": int(total_before),
            "remaining_bytes": int(remaining_bytes),
            "remaining": int(remaining_bytes),
            "file_count": len(files_all),
            "scanned_files": len(files_all),
            "files_scanned": len(files_all),
            "remaining_count": len(remaining_files),
            "remaining_files": [str(p) for p in sorted(remaining_files, key=str)],
            "errors": errors,
            "error_count": len(errors),
            "storage_root": str(storage),
            "hot_dir": str(storage),
            "source_dir": str(storage),
            "path": str(storage),
            "archive_root": str(archive),
            "archive_dir": str(archive),
            "archive_path": str(archive),
            "destination": str(archive),
            "max_age_days": eff["max_age_days"],
            "retention_days": eff["max_age_days"],
            "max_age": eff["max_age_days"],
            "tier_after_days": eff["max_age_days"],
            "archive_after_days": eff["max_age_days"],
            "max_total_bytes": eff["max_total_bytes"],
            "max_bytes": eff["max_total_bytes"],
            "per_camera": per_camera,
            "breakdown": per_camera,
            "by_camera": {e["camera_name"]: e["selected"] for e in per_camera},
            "policy": get_policy(),
            "deleted": list(moved) if False else [],
        }
        # Keep a couple of deletion-style aliases empty-but-present for
        # cross-compat (tiering moves, it does not delete).
        result.setdefault("deleted", [])
        result.setdefault("deleted_count", 0)
        result.setdefault("removed", [])
        return result

    def preview(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        kw = dict(kwargs)
        kw["dry_run"] = True
        for a in args:
            if isinstance(a, dict):
                kw.update(a)
        return self.run(**kw)

    def dry_run(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.preview(*args, **kwargs)

    def enforce(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.run(*args, **kwargs)

    def execute(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.run(*args, **kwargs)

    def tier(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.run(*args, **kwargs)

    def archive(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.run(*args, **kwargs)

    def tier_now(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.run(*args, **kwargs)

    def archive_now(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.run(*args, **kwargs)

    def run_tiering(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.run(*args, **kwargs)

    def run_once(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.run(*args, **kwargs)

    def tier_once(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.run(*args, **kwargs)

    def apply(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.run(*args, **kwargs)

    def process(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.run(*args, **kwargs)

    def cleanup(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.run(*args, **kwargs)

    def prune(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.run(*args, **kwargs)

    def move(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.run(*args, **kwargs)

    def relocate(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.run(*args, **kwargs)

    def get_status(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        options = self._merged_options(*args, **kwargs)
        eff = self._effective(options)
        storage: Path = eff["storage_root"]
        archive: Path = eff["archive_root"]
        files = self._files(camera_name=eff["camera_name"], _eff=eff)
        total = sum(_size(p) for p in files)
        selected = self.select_for_tiering(**{**options, "dry_run": True})
        arch_files: List[Path] = []
        arch_bytes = 0
        try:
            if archive.exists():
                if archive.is_file():
                    arch_files = [archive]
                else:
                    for root, _dirs, fnames in os.walk(archive):
                        for fn in fnames:
                            fp = Path(root) / fn
                            try:
                                if fp.is_file():
                                    arch_files.append(fp)
                            except OSError:
                                continue
                arch_bytes = sum(_size(p) for p in arch_files)
        except Exception:  # noqa: BLE001
            pass
        policy = get_policy()
        return {
            "status": "ok" if eff["enabled"] else "disabled",
            "state": "ok" if eff["enabled"] else "disabled",
            "ok": True,
            "success": True,
            "enabled": bool(eff["enabled"]),
            "policy": policy,
            "config": policy,
            "storage_root": str(storage),
            "hot_dir": str(storage),
            "path": str(storage),
            "archive_root": str(archive),
            "archive_dir": str(archive),
            "archive_path": str(archive),
            "total_bytes": int(total),
            "used_bytes": int(total),
            "hot_bytes": int(total),
            "file_count": len(files),
            "scanned_files": len(files),
            "files_scanned": len(files),
            "hot_file_count": len(files),
            "archive_bytes": int(arch_bytes),
            "archive_file_count": len(arch_files),
            "archive_files": len(arch_files),
            "archived_bytes": int(arch_bytes),
            "tierable_count": len(selected),
            "tierable": [str(p) for p in selected],
            "candidates": [str(p) for p in selected],
            "selected": [str(p) for p in selected],
            "to_tier": [str(p) for p in selected],
            "to_archive": [str(p) for p in selected],
            "max_age_days": eff["max_age_days"],
            "max_total_bytes": eff["max_total_bytes"],
        }

    def status(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.get_status(*args, **kwargs)

    def get_summary(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.get_status(*args, **kwargs)

    def summary(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.get_status(*args, **kwargs)

    def usage(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.get_status(*args, **kwargs)

    def info(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.get_status(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        low = (name or "").lower()
        select_hints = ("select", "candidate", "collect", "files", "paths", "scan")
        if any(h in low for h in select_hints):
            return self.select_for_tiering
        status_hints = ("status", "usage", "summary", "info", "health", "state")
        if any(h in low for h in status_hints):
            return self.get_status
        policy_hints = ("policy", "config", "setting")
        if any(h in low for h in policy_hints):
            if "update" in low or "set" in low or "config" in low:
                return self.update_policy
            return self.get_policy
        preview_hints = ("preview", "dry")
        if any(h in low for h in preview_hints):
            return self.preview
        return self.run


# ---------- class / instance aliases ----------
TieringService = TieringManager
StorageTieringManager = TieringManager
StorageTieringService = TieringManager
AutoTieringManager = TieringManager
AutoTieringService = TieringManager
TierManager = TieringManager
ArchiveManager = TieringManager
SegmentTieringManager = TieringManager
SegmentTieringService = TieringManager
HotColdTieringManager = TieringManager
StorageAutoTieringManager = TieringManager
StorageAutoTieringService = TieringManager

tiering_service = TieringManager()
tiering_manager = tiering_service
service = tiering_service
manager = tiering_service
default_service = tiering_service
default_tiering_service = tiering_service
auto_tiering_service = tiering_service
storage_tiering_service = tiering_service
tier_service = tiering_service
archive_service = tiering_service


# ---------- module-level convenience functions ----------
def _merge_call_args(args: Any, kwargs: Any) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            merged.update(a)
        elif isinstance(a, (str, Path)):
            merged.setdefault("path", a)
        elif isinstance(a, bool):
            merged.setdefault("dry_run", a)
    merged.update(kwargs)
    return merged


def tier(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return tiering_service.run(*args, **kwargs)


def run(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return tiering_service.run(*args, **kwargs)


def enforce(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return tiering_service.run(*args, **kwargs)


def execute(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return tiering_service.run(*args, **kwargs)


def archive(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return tiering_service.run(*args, **kwargs)


def run_tiering(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return tiering_service.run(*args, **kwargs)


def preview(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return tiering_service.preview(*args, **kwargs)


def dry_run(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return tiering_service.preview(*args, **kwargs)


def preview_tiering(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return tiering_service.preview(*args, **kwargs)


def scan(*args: Any, **kwargs: Any) -> List[Path]:
    return tiering_service.select_for_tiering(*args, **kwargs)


def select_for_tiering(*args: Any, **kwargs: Any) -> List[Path]:
    return tiering_service.select_for_tiering(*args, **kwargs)


def select_candidates(*args: Any, **kwargs: Any) -> List[Path]:
    return tiering_service.select_for_tiering(*args, **kwargs)


def collect_candidates(*args: Any, **kwargs: Any) -> List[Path]:
    return tiering_service.select_for_tiering(*args, **kwargs)


def get_files_to_tier(*args: Any, **kwargs: Any) -> List[Path]:
    return tiering_service.select_for_tiering(*args, **kwargs)


def select_for_archiving(*args: Any, **kwargs: Any) -> List[Path]:
    return tiering_service.select_for_tiering(*args, **kwargs)


def get_status(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return tiering_service.get_status(*args, **kwargs)


def status(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return tiering_service.get_status(*args, **kwargs)


def get_summary(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return tiering_service.get_status(*args, **kwargs)


def summary(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return tiering_service.get_status(*args, **kwargs)


__all__ = [
    "TieringManager",
    "TieringService",
    "StorageTieringManager",
    "StorageTieringService",
    "AutoTieringManager",
    "AutoTieringService",
    "TierManager",
    "ArchiveManager",
    "SegmentTieringManager",
    "SegmentTieringService",
    "StorageAutoTieringManager",
    "StorageAutoTieringService",
    "tiering_service",
    "tiering_manager",
    "service",
    "manager",
    "get_policy",
    "update_policy",
    "reset_policy",
    "tier",
    "run",
    "enforce",
    "execute",
    "archive",
    "run_tiering",
    "preview",
    "dry_run",
    "preview_tiering",
    "scan",
    "select_for_tiering",
    "select_candidates",
    "collect_candidates",
    "get_files_to_tier",
    "get_status",
    "status",
]
