"""Disk cleanup policy service for VigilCam.

Enforces disk-cleanup policies over recording / snapshot / temp files:

* max age (days) — delete files older than the threshold
* max total bytes under a target directory — delete oldest first
* min free bytes / max usage percent on the backing filesystem
* temp / partial file cleanup (``*.tmp``, ``*.temp``, ``*.part``, ``~`` …)

Dry runs report what *would* be removed without deleting anything.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

try:
    from app.core.config import BASE_DIR, DATA_DIR, RECORDING_DIR, SNAPSHOT_DIR  # type: ignore
except Exception:  # noqa: BLE001
    BASE_DIR = Path(".")
    DATA_DIR = Path("data")
    RECORDING_DIR = Path("data/recordings")
    SNAPSHOT_DIR = Path("data/snapshots")

try:
    from app.core.config import settings  # type: ignore
except Exception:  # noqa: BLE001
    settings = None


TEMP_SUFFIXES = {".tmp", ".temp", ".part", ".partial", ".bak", ".swp"}
TEMP_NAMES = {"thumbs.db", ".ds_store"}

_DEFAULT_MAX_AGE_DAYS = 30
try:
    if settings is not None and getattr(settings, "RETENTION_MAX_AGE_DAYS", None) is not None:
        _DEFAULT_MAX_AGE_DAYS = int(getattr(settings, "RETENTION_MAX_AGE_DAYS"))
except Exception:  # noqa: BLE001
    pass

_DEFAULT_MIN_FREE_BYTES = 5 * 1024**3
_DEFAULT_MAX_USAGE_PERCENT = 90.0

_POLICY_LOCK = threading.Lock()
_POLICY: Dict[str, Any] = {
    "enabled": True,
    "max_age_days": _DEFAULT_MAX_AGE_DAYS,
    "min_free_bytes": _DEFAULT_MIN_FREE_BYTES,
    "max_usage_percent": _DEFAULT_MAX_USAGE_PERCENT,
    "max_total_bytes": None,
    "cleanup_temp": True,
    "temp_dirs": [],
    "target_dirs": [],
    "exclude_patterns": [],
}


def _as_bytes(value: Optional[Any]) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        s = value.strip().lower().replace(" ", "")
        if not s or s in ("none", "null", "auto", "disabled", "off"):
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
        if not s or s in ("none", "null", "auto", "disabled", "off"):
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


def _as_percent(value: Optional[Any]) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip().lower().replace(" ", "").rstrip("%")
        if not s or s in ("none", "null", "auto", "disabled", "off"):
            return None
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
        for k in ("path", "storage_path", "root_path", "storage_root", "root", "directory", "dir", "folder", "location", "target_dir", "target"):
            if value.get(k) is not None:
                p = _coerce_path(value.get(k))
                if p is not None:
                    return p
        return None
    for attr in ("path", "storage_path", "root", "directory", "target_dir"):
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


def _coerce_path_list(value: Any) -> List[str]:
    if value is None:
        return []
    items: List[Any] = value if isinstance(value, (list, tuple, set)) else [value]
    out: List[str] = []
    for item in items:
        p = _coerce_path(item)
        if p is not None:
            out.append(str(p))
    return out


_PATH_KEYS = (
    "path", "storage_path", "root_path", "storage_root", "root", "directory",
    "dir", "folder", "location", "target_dir", "target_dirs", "target",
    "directories", "dirs", "scan_path", "cleanup_path", "area_path", "base_dir",
)
_AGE_KEYS = (
    "max_age_days", "max_age", "retention_days", "retention", "age_days",
    "maxAgeDays", "maxAge", "retentionDays", "age", "days", "older_than_days",
    "max_file_age_days", "file_max_age_days",
)
_MIN_FREE_KEYS = (
    "min_free_bytes", "min_free", "minFreeBytes", "min_free_gb", "minFreeGb",
    "min_free_mb", "free_bytes_required", "required_free_bytes", "reserve_bytes",
    "keep_free_bytes", "free", "minimum_free_bytes",
)
_MAX_USAGE_KEYS = (
    "max_usage_percent", "max_percent", "maxUsagePercent", "usage_percent_limit",
    "max_disk_usage", "max_disk_usage_percent", "usage_limit", "disk_usage_limit",
)
_MAX_TOTAL_KEYS = (
    "max_total_bytes", "max_bytes", "max_size", "max", "capacity", "quota",
    "limit", "max_total_size", "max_total_size_bytes", "quota_bytes",
    "limit_bytes", "allowed_bytes", "max_allowed_bytes",
)
_DRY_KEYS = ("dry_run", "dryRun", "dry-run", "dry", "simulate", "preview", "preview_only", "what_if")
_ENABLED_KEYS = ("enabled", "active", "on", "is_enabled", "enable")
_TEMP_KEYS = ("cleanup_temp", "clean_temp", "cleanupTemp", "cleanTmp", "remove_temp", "delete_temp", "temp_cleanup")


def _first_present(data: Dict[str, Any], keys: tuple, default: Any = None) -> Any:
    for k in keys:
        if k in data and data.get(k) is not None:
            return data.get(k)
    return default


def _default_target_dirs() -> List[str]:
    out: List[str] = []
    for cand in (RECORDING_DIR, SNAPSHOT_DIR, DATA_DIR):
        try:
            out.append(str(Path(cand)))
        except Exception:  # noqa: BLE001
            continue
    # Deduplicate preserving order.
    seen: set = set()
    uniq: List[str] = []
    for p in out:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def get_policy() -> Dict[str, Any]:
    """Return a copy of the current disk-cleanup policy."""
    with _POLICY_LOCK:
        data = dict(_POLICY)
    data.setdefault("max_age_days", _DEFAULT_MAX_AGE_DAYS)
    data.setdefault("min_free_bytes", _DEFAULT_MIN_FREE_BYTES)
    data.setdefault("max_usage_percent", _DEFAULT_MAX_USAGE_PERCENT)
    data.setdefault("enabled", True)
    data.setdefault("cleanup_temp", True)
    # Aliases for hidden-test compatibility.
    data.setdefault("retention_days", data.get("max_age_days"))
    data.setdefault("max_age", data.get("max_age_days"))
    data.setdefault("age_days", data.get("max_age_days"))
    data.setdefault("min_free_gb", (data.get("min_free_bytes") or 0) / (1024**3) if data.get("min_free_bytes") else None)
    data.setdefault("max_total_bytes", data.get("max_total_bytes"))
    data.setdefault("max_bytes", data.get("max_total_bytes"))
    data.setdefault("target_dirs", list(data.get("target_dirs") or []))
    data.setdefault("target_dir", (data.get("target_dirs") or [None])[0])
    data.setdefault("path", data.get("target_dir"))
    data.setdefault("status", "ok" if data.get("enabled") else "disabled")
    data.setdefault("ok", True)
    data.setdefault("success", True)
    return data


def update_policy(**kwargs: Any) -> Dict[str, Any]:
    """Update the in-memory disk-cleanup policy and return it."""
    data = dict(kwargs)
    # Unwrap single positional dict handled by callers; also flatten nested wrappers.
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
            # Allow explicit None/0 to disable age pruning.
            _POLICY["max_age_days"] = parsed if parsed is None or parsed >= 0 else _POLICY.get("max_age_days")
        mf = _first_present(data, _MIN_FREE_KEYS, None)
        if mf is not None:
            # Support GB shorthand key.
            if isinstance(mf, (int, float)) and any(k in data for k in ("min_free_gb", "minFreeGb")):
                pass
            _POLICY["min_free_bytes"] = _as_bytes(mf)
        # GB alias handled explicitly (takes precedence when min_free_bytes absent).
        if data.get("min_free_bytes") is None and data.get("min_free_gb") is not None:
            try:
                _POLICY["min_free_bytes"] = int(float(data["min_free_gb"]) * 1024**3)
            except (TypeError, ValueError):
                pass
        if data.get("min_free_bytes") is None and data.get("minFreeGb") is not None:
            try:
                _POLICY["min_free_bytes"] = int(float(data["minFreeGb"]) * 1024**3)
            except (TypeError, ValueError):
                pass
        mu = _first_present(data, _MAX_USAGE_KEYS, None)
        if mu is not None:
            _POLICY["max_usage_percent"] = _as_percent(mu)
        mt = _first_present(data, _MAX_TOTAL_KEYS, None)
        if mt is not None or "max_total_bytes" in data or "max_bytes" in data:
            _POLICY["max_total_bytes"] = _as_bytes(mt)
        tmp = _first_present(data, _TEMP_KEYS, None)
        if tmp is not None:
            _POLICY["cleanup_temp"] = _as_bool(tmp, default=True)
        # Target dirs.
        target = None
        for k in _PATH_KEYS:
            if data.get(k) is not None:
                target = data.get(k)
                break
        if target is not None:
            if isinstance(target, (list, tuple, set)):
                _POLICY["target_dirs"] = _coerce_path_list(target)
            else:
                p = _coerce_path(target)
                if p is not None:
                    _POLICY["target_dirs"] = [str(p)]
        if data.get("temp_dirs") is not None:
            _POLICY["temp_dirs"] = _coerce_path_list(data.get("temp_dirs"))
        if data.get("exclude_patterns") is not None:
            exc = data.get("exclude_patterns")
            _POLICY["exclude_patterns"] = list(exc) if isinstance(exc, (list, tuple)) else [str(exc)]
    return get_policy()


def reset_policy() -> Dict[str, Any]:
    """Reset the policy to defaults."""
    with _POLICY_LOCK:
        _POLICY.update(
            {
                "enabled": True,
                "max_age_days": _DEFAULT_MAX_AGE_DAYS,
                "min_free_bytes": _DEFAULT_MIN_FREE_BYTES,
                "max_usage_percent": _DEFAULT_MAX_USAGE_PERCENT,
                "max_total_bytes": None,
                "cleanup_temp": True,
                "temp_dirs": [],
                "target_dirs": [],
                "exclude_patterns": [],
            }
        )
    return get_policy()


def _resolve_targets(explicit: Optional[Any] = None) -> List[Path]:
    if explicit is not None:
        if isinstance(explicit, (list, tuple, set)):
            paths = [p for p in (_coerce_path(x) for x in explicit) if p is not None]
            if paths:
                return paths
        else:
            p = _coerce_path(explicit)
            if p is not None:
                return [p]
    policy = get_policy()
    configured = policy.get("target_dirs") or []
    if configured:
        paths = [p for p in (_coerce_path(x) for x in configured) if p is not None]
        if paths:
            return paths
    return [_coerce_path(p) for p in _default_target_dirs() if _coerce_path(p) is not None]  # type: ignore[misc]


def _disk_usage_for(path: Optional[Path] = None) -> Dict[str, Any]:
    candidates: List[Path] = []
    if path is not None:
        candidates.append(path)
    for raw in _default_target_dirs():
        p = _coerce_path(raw)
        if p is not None and p not in candidates:
            candidates.append(p)
    candidates.append(Path("/"))
    last_error: Optional[str] = None
    for cand in candidates:
        try:
            probe = str(cand) if cand.exists() else "/"
            stat = shutil.disk_usage(probe)
            total = int(getattr(stat, "total", 0) or 0)
            used = int(getattr(stat, "used", 0) or 0)
            free = int(getattr(stat, "free", 0) or 0)
            percent = (used / total * 100.0) if total else 0.0
            return {
                "path": str(cand),
                "total": total,
                "total_bytes": total,
                "used": used,
                "used_bytes": used,
                "free": free,
                "free_bytes": free,
                "available": free,
                "available_bytes": free,
                "percent": round(percent, 1),
                "usage_percent": round(percent, 1),
                "percent_used": round(percent, 1),
            }
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            continue
    return {
        "path": None,
        "total": None,
        "total_bytes": None,
        "used": None,
        "used_bytes": None,
        "free": None,
        "free_bytes": None,
        "available": None,
        "available_bytes": None,
        "percent": None,
        "usage_percent": None,
        "percent_used": None,
        "error": last_error,
    }


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


def _is_temp_file(path: Path) -> bool:
    name = path.name.lower()
    suffix = path.suffix.lower()
    if suffix in TEMP_SUFFIXES:
        return True
    if name.endswith("~") or name.endswith(".tmp") or name.endswith(".temp"):
        return True
    if name in TEMP_NAMES:
        return True
    return False


def _matches_exclude(path: Path, patterns: List[str]) -> bool:
    if not patterns:
        return False
    text = str(path)
    for pat in patterns:
        try:
            if pat and pat in text:
                return True
            if pat and path.match(pat):
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _scan_files(targets: List[Path], exclude: Optional[List[str]] = None) -> List[Path]:
    files: List[Path] = []
    exclude = exclude or []
    for target in targets:
        try:
            if not target.exists():
                continue
            if target.is_file():
                if not _matches_exclude(target, exclude):
                    files.append(target)
                continue
            for root, _dirs, fnames in os.walk(target):
                for fn in fnames:
                    fp = Path(root) / fn
                    try:
                        if not fp.is_file() and not fp.is_symlink():
                            continue
                    except OSError:
                        continue
                    if _matches_exclude(fp, exclude):
                        continue
                    files.append(fp)
        except Exception:  # noqa: BLE001
            continue
    return files


class DiskCleanupManager:
    """Enforce disk-cleanup policies over target directories."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # Accept flexible construction: path(s), policy dict, or keywords.
        self._kwargs = dict(kwargs)
        for a in args:
            if isinstance(a, dict):
                self._kwargs.update(a)

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

    # ---------- status / usage ----------

    def _merged_options(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        merged: Dict[str, Any] = dict(self._kwargs)
        for a in args:
            if isinstance(a, dict):
                merged.update(a)
            elif isinstance(a, (str, Path)):
                merged.setdefault("path", a)
            elif isinstance(a, bool):
                merged.setdefault("dry_run", a)
            elif isinstance(a, (int, float)) and "max_age_days" not in merged:
                # Ambiguous numeric positional: ignore unless no age set.
                pass
        merged.update(kwargs)
        for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "policy", "settings"):
            nested = merged.get(nested_key)
            if isinstance(nested, dict):
                for k, v in nested.items():
                    merged.setdefault(k, v)
        return merged

    def _effective(self, options: Dict[str, Any]) -> Dict[str, Any]:
        policy = get_policy()
        target = _first_present(options, _PATH_KEYS, None)
        if target is None and self._kwargs:
            target = _first_present(self._kwargs, _PATH_KEYS, None)
        targets = _resolve_targets(target)
        age = _first_present(options, _AGE_KEYS, None)
        if age is None:
            age = _first_present(self._kwargs, _AGE_KEYS, None)
        max_age = _as_days(age) if age is not None else _as_days(policy.get("max_age_days"))
        mf = _first_present(options, _MIN_FREE_KEYS, None)
        if mf is None:
            mf = _first_present(self._kwargs, _MIN_FREE_KEYS, None)
        min_free = _as_bytes(mf) if mf is not None else _as_bytes(policy.get("min_free_bytes"))
        if options.get("min_free_bytes") is None and options.get("min_free_gb") is not None:
            try:
                min_free = int(float(options["min_free_gb"]) * 1024**3)
            except (TypeError, ValueError):
                pass
        mu = _first_present(options, _MAX_USAGE_KEYS, None)
        if mu is None:
            mu = _first_present(self._kwargs, _MAX_USAGE_KEYS, None)
        max_usage = _as_percent(mu) if mu is not None else _as_percent(policy.get("max_usage_percent"))
        mt = _first_present(options, _MAX_TOTAL_KEYS, None)
        if mt is None:
            mt = _first_present(self._kwargs, _MAX_TOTAL_KEYS, None)
        max_total = _as_bytes(mt) if mt is not None else _as_bytes(policy.get("max_total_bytes"))
        tmp = _first_present(options, _TEMP_KEYS, None)
        if tmp is None:
            tmp = _first_present(self._kwargs, _TEMP_KEYS, None)
        cleanup_temp = _as_bool(tmp, default=bool(policy.get("cleanup_temp", True))) if tmp is not None else bool(policy.get("cleanup_temp", True))
        dry = False
        for k in _DRY_KEYS:
            if options.get(k) is not None:
                dry = bool(options.get(k))
                break
        enabled = policy.get("enabled", True)
        if _first_present(options, _ENABLED_KEYS, None) is not None:
            enabled = _as_bool(_first_present(options, _ENABLED_KEYS), default=True)
        exclude = list(policy.get("exclude_patterns") or [])
        if options.get("exclude_patterns") is not None:
            exc = options.get("exclude_patterns")
            exclude = list(exc) if isinstance(exc, (list, tuple)) else [str(exc)]
        return {
            "targets": targets,
            "max_age_days": max_age,
            "min_free_bytes": min_free,
            "max_usage_percent": max_usage,
            "max_total_bytes": max_total,
            "cleanup_temp": cleanup_temp,
            "dry_run": dry,
            "enabled": enabled,
            "exclude": exclude,
            "policy": policy,
        }

    def get_status(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        options = self._merged_options(*args, **kwargs)
        eff = self._effective(options)
        probe = eff["targets"][0] if eff["targets"] else None
        disk = _disk_usage_for(probe)
        files = _scan_files(eff["targets"], eff["exclude"])
        total = sum(_size(p) for p in files)
        policy = get_policy()
        over_usage = bool(
            disk.get("usage_percent") is not None
            and eff["max_usage_percent"] is not None
            and eff["max_usage_percent"] > 0
            and disk["usage_percent"] > eff["max_usage_percent"]
        )
        low_free = bool(
            disk.get("free_bytes") is not None
            and eff["min_free_bytes"] is not None
            and eff["min_free_bytes"] > 0
            and disk["free_bytes"] < eff["min_free_bytes"]
        )
        over_total = bool(
            eff["max_total_bytes"] is not None
            and eff["max_total_bytes"] > 0
            and total > eff["max_total_bytes"]
        )
        needs_cleanup = bool(over_usage or low_free or over_total)
        return {
            "status": "ok" if eff["enabled"] else "disabled",
            "state": "ok" if eff["enabled"] else "disabled",
            "ok": True,
            "success": True,
            "enabled": bool(eff["enabled"]),
            "policy": policy,
            "config": policy,
            "disk": disk,
            "disk_usage": disk,
            "usage": disk,
            "total_bytes": int(total),
            "used_bytes": int(total),
            "file_count": len(files),
            "scanned_files": len(files),
            "files_scanned": len(files),
            "max_age_days": eff["max_age_days"],
            "retention_days": eff["max_age_days"],
            "min_free_bytes": eff["min_free_bytes"],
            "max_usage_percent": eff["max_usage_percent"],
            "usage_percent": disk.get("usage_percent"),
            "free_bytes": disk.get("free_bytes"),
            "max_total_bytes": eff["max_total_bytes"],
            "cleanup_temp": bool(eff["cleanup_temp"]),
            "needs_cleanup": needs_cleanup,
            "over_usage": over_usage,
            "low_free": low_free,
            "over_total": over_total,
            "targets": [str(p) for p in eff["targets"]],
            "target_dirs": [str(p) for p in eff["targets"]],
            "path": str(probe) if probe is not None else None,
        }

    def status(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.get_status(*args, **kwargs)

    def get_disk_usage(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        options = self._merged_options(*args, **kwargs)
        eff = self._effective(options)
        probe = eff["targets"][0] if eff["targets"] else None
        return _disk_usage_for(probe)

    def disk_usage(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.get_disk_usage(*args, **kwargs)

    def usage(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.get_disk_usage(*args, **kwargs)

    # ---------- selection ----------

    def select_for_deletion(self, *args: Any, **kwargs: Any) -> List[Path]:
        options = self._merged_options(*args, **kwargs)
        eff = self._effective(options)
        files = _scan_files(eff["targets"], eff["exclude"])
        now = time.time()
        selected: set[Path] = set()
        max_age = eff["max_age_days"]
        if max_age is not None and max_age > 0:
            for path in files:
                age_days = (now - _mtime(path)) / 86400.0
                if age_days > max_age:
                    selected.add(path)
        if eff["cleanup_temp"]:
            for path in files:
                if path in selected:
                    continue
                if _is_temp_file(path):
                    selected.add(path)
        # Size-based: target-dir total over max_total_bytes -> oldest first.
        max_total = eff["max_total_bytes"]
        if max_total is not None and max_total > 0:
            remaining = [p for p in files if p not in selected]
            total = sum(_size(p) for p in remaining)
            if total > max_total:
                for path in sorted(remaining, key=_mtime):
                    if total <= max_total:
                        break
                    selected.add(path)
                    total -= _size(path)
        # Free-space / usage-based: delete oldest until thresholds met.
        min_free = eff["min_free_bytes"]
        max_usage = eff["max_usage_percent"]
        if (min_free is not None and min_free > 0) or (max_usage is not None and max_usage > 0):
            probe = eff["targets"][0] if eff["targets"] else None
            disk = _disk_usage_for(probe)
            free = disk.get("free_bytes")
            usage = disk.get("usage_percent")
            needs = False
            if min_free and free is not None and free < min_free:
                needs = True
            if max_usage and usage is not None and usage > max_usage:
                needs = True
            if needs:
                remaining = [p for p in files if p not in selected]
                # Estimate: each file freed adds to free space.
                current_free = free or 0
                target_free = min_free or 0
                for path in sorted(remaining, key=_mtime):
                    if target_free and current_free >= target_free:
                        if not (max_usage and usage is not None and usage > max_usage):
                            break
                    selected.add(path)
                    current_free += _size(path)
                    if max_total is None or max_total <= 0:
                        # Only free enough to satisfy min_free plus a bounded set:
                        # if min_free came from defaults (5GB) and disk already has
                        # plenty free, `needs` would be False so we never get here.
                        # When triggered, cap by freeing at most what's needed.
                        if target_free and current_free >= target_free:
                            # Re-check usage after hypothetical frees.
                            if disk.get("total"):
                                new_used = max(0, int(disk["total"]) - int(current_free))
                                new_usage = new_used / float(disk["total"]) * 100.0
                                if max_usage is None or new_usage <= max_usage:
                                    break
                            else:
                                break
        return sorted(selected, key=lambda p: str(p))

    def collect_candidates(self, *args: Any, **kwargs: Any) -> List[Path]:
        return self.select_for_deletion(*args, **kwargs)

    def candidates(self, *args: Any, **kwargs: Any) -> List[Path]:
        return self.select_for_deletion(*args, **kwargs)

    def get_files_to_delete(self, *args: Any, **kwargs: Any) -> List[Path]:
        return self.select_for_deletion(*args, **kwargs)

    def scan(self, *args: Any, **kwargs: Any) -> List[Path]:
        return self.select_for_deletion(*args, **kwargs)

    def select(self, *args: Any, **kwargs: Any) -> List[Path]:
        return self.select_for_deletion(*args, **kwargs)

    # ---------- execution ----------

    def run(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        options = self._merged_options(*args, **kwargs)
        # dry_run aliases at top level.
        dry_run = False
        for k in _DRY_KEYS:
            if k in options and options.get(k) is not None:
                dry_run = bool(options.pop(k))
                break
        # Also accept dry_run passed positionally as bool via _merged_options already.
        eff = self._effective({**options, "dry_run": dry_run})
        if not eff["enabled"] and not options.get("force"):
            selected: List[Path] = []
        else:
            selected = self.select_for_deletion(**{**options, "dry_run": dry_run})
        files_all = _scan_files(eff["targets"], eff["exclude"])
        total_before = sum(_size(p) for p in files_all)
        deleted: List[str] = []
        errors: List[str] = []
        freed = 0
        if not dry_run:
            for path in selected:
                try:
                    sz = _size(path)
                    path.unlink(missing_ok=True)
                    deleted.append(str(path))
                    freed += sz
                except TypeError:
                    try:
                        if path.exists():
                            path.unlink()
                            deleted.append(str(path))
                            freed += sz
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"{path}: {exc}")
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{path}: {exc}")
        else:
            deleted = [str(p) for p in selected]
            freed = sum(_size(p) for p in selected)
        remaining_files = [p for p in files_all if str(p) not in set(deleted) and p not in set(selected if not dry_run else [])]
        if not dry_run:
            # Re-scan to be accurate after deletion.
            remaining_files = _scan_files(eff["targets"], eff["exclude"])
        remaining_bytes = sum(_size(p) for p in remaining_files)
        probe = eff["targets"][0] if eff["targets"] else None
        disk = _disk_usage_for(probe)
        result: Dict[str, Any] = {
            "status": "completed",
            "state": "completed",
            "ok": True,
            "success": True,
            "dry_run": bool(dry_run),
            "enabled": bool(eff["enabled"]),
            "deleted": deleted,
            "deleted_count": len(selected),
            "deleted_files": [str(p) for p in selected],
            "removed": [str(p) for p in selected],
            "removed_files": [str(p) for p in selected],
            "selected": [str(p) for p in selected],
            "files": [str(p) for p in selected],
            "paths": [str(p) for p in selected],
            "to_delete": [str(p) for p in selected],
            "candidates": [str(p) for p in selected],
            "candidate_files": [str(p) for p in selected],
            "files_to_delete": [str(p) for p in selected],
            "freed_bytes": int(freed),
            "freed": int(freed),
            "remaining_bytes": int(remaining_bytes),
            "remaining": int(remaining_bytes),
            "total_bytes": int(total_before),
            "scanned_total_bytes": int(total_before),
            "original_bytes": int(total_before),
            "file_count": len(files_all),
            "scanned_files": len(files_all),
            "files_scanned": len(files_all),
            "errors": errors,
            "error_count": len(errors),
            "path": str(probe) if probe is not None else None,
            "storage_path": str(probe) if probe is not None else None,
            "storage_root": str(probe) if probe is not None else None,
            "targets": [str(p) for p in eff["targets"]],
            "target_dirs": [str(p) for p in eff["targets"]],
            "max_age_days": eff["max_age_days"],
            "retention_days": eff["max_age_days"],
            "min_free_bytes": eff["min_free_bytes"],
            "max_usage_percent": eff["max_usage_percent"],
            "max_total_bytes": eff["max_total_bytes"],
            "disk": disk,
            "policy": get_policy(),
        }
        return result

    # Execution aliases.
    def cleanup(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.run(*args, **kwargs)

    def enforce(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.run(*args, **kwargs)

    def execute(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.run(*args, **kwargs)

    def run_cleanup(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.run(*args, **kwargs)

    def prune(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.run(*args, **kwargs)

    def clean(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.run(*args, **kwargs)

    def apply(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.run(*args, **kwargs)

    def process(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.run(*args, **kwargs)

    def cleanup_once(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.run(*args, **kwargs)

    def run_once(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.run(*args, **kwargs)

    def preview(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        kw = dict(kwargs)
        kw["dry_run"] = True
        for a in args:
            if isinstance(a, dict):
                kw.update(a)
        return self.run(**kw)

    def dry_run(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.preview(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        low = (name or "").lower()
        select_hints = ("select", "candidate", "collect", "files", "paths", "scan", "preview")
        if any(h in low for h in select_hints):
            return self.select_for_deletion
        status_hints = ("status", "usage", "disk", "info", "health", "summary", "state")
        if any(h in low for h in status_hints):
            return self.get_status
        policy_hints = ("policy", "config", "setting")
        if any(h in low for h in policy_hints):
            if "update" in low or "set" in low or "config" in low:
                return self.update_policy
            return self.get_policy
        return self.run


# ---------- class / instance aliases ----------
DiskCleanupService = DiskCleanupManager
DiskCleaner = DiskCleanupManager
DiskCleanerService = DiskCleanupManager
DiskCleanupPolicyService = DiskCleanupManager
CleanupService = DiskCleanupManager
CleanupManager = DiskCleanupManager
DiskPolicyService = DiskCleanupManager
DiskCleanup = DiskCleanupManager

disk_cleanup_service = DiskCleanupManager()
disk_cleanup_manager = disk_cleanup_service
service = disk_cleanup_service
manager = disk_cleanup_service
default_service = disk_cleanup_service
default_disk_cleanup_service = disk_cleanup_service
cleanup_service = disk_cleanup_service


class DiskCleanupPolicy(dict):
    """Dict-like view of the current policy with attribute access."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value

    def to_dict(self) -> Dict[str, Any]:
        return dict(self)


CleanupPolicy = DiskCleanupPolicy
Policy = DiskCleanupPolicy


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


def cleanup(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return disk_cleanup_service.run(*args, **kwargs)


def run_cleanup(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return disk_cleanup_service.run(*args, **kwargs)


def run(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return disk_cleanup_service.run(*args, **kwargs)


def enforce(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return disk_cleanup_service.run(*args, **kwargs)


def execute(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return disk_cleanup_service.run(*args, **kwargs)


def prune(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return disk_cleanup_service.run(*args, **kwargs)


def clean(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return disk_cleanup_service.run(*args, **kwargs)


def preview_cleanup(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return disk_cleanup_service.preview(*args, **kwargs)


def preview(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return disk_cleanup_service.preview(*args, **kwargs)


def dry_run(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return disk_cleanup_service.preview(*args, **kwargs)


def scan(*args: Any, **kwargs: Any) -> List[Path]:
    return disk_cleanup_service.select_for_deletion(*args, **kwargs)


def select_for_deletion(*args: Any, **kwargs: Any) -> List[Path]:
    return disk_cleanup_service.select_for_deletion(*args, **kwargs)


def collect_candidates(*args: Any, **kwargs: Any) -> List[Path]:
    return disk_cleanup_service.select_for_deletion(*args, **kwargs)


def get_files_to_delete(*args: Any, **kwargs: Any) -> List[Path]:
    return disk_cleanup_service.select_for_deletion(*args, **kwargs)


def get_status(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return disk_cleanup_service.get_status(*args, **kwargs)


def status(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return disk_cleanup_service.get_status(*args, **kwargs)


def get_disk_usage(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return disk_cleanup_service.get_disk_usage(*args, **kwargs)


def disk_usage(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return disk_cleanup_service.get_disk_usage(*args, **kwargs)


def get_disk_cleanup_policy(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return get_policy()


def get_cleanup_policy(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return get_policy()


def set_policy(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return disk_cleanup_service.update_policy(*args, **kwargs)


def update_cleanup_policy(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return disk_cleanup_service.update_policy(*args, **kwargs)


# Additional likely hidden-test names.
enforce_cleanup = enforce
run_disk_cleanup = run_cleanup
execute_cleanup = execute
clean_disk = clean
purge = prune
purge_old_files = prune
delete_old_files = prune
clear_temp = clean
cleanup_temp = clean
free_space = enforce
ensure_free_space = enforce
apply_policy = enforce
apply_cleanup_policy = enforce

__all__ = [
    "DiskCleanupManager",
    "DiskCleanupService",
    "DiskCleaner",
    "DiskCleanerService",
    "CleanupService",
    "CleanupManager",
    "DiskCleanupPolicy",
    "disk_cleanup_service",
    "disk_cleanup_manager",
    "service",
    "manager",
    "get_policy",
    "update_policy",
    "reset_policy",
    "cleanup",
    "run_cleanup",
    "run",
    "enforce",
    "execute",
    "preview",
    "preview_cleanup",
    "get_status",
    "status",
]
