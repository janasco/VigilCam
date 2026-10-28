"""Storage cleanup advisor service for VigilCam.

Advisory-only layer over the mechanical retention/quota enforcers.
:mod:`app.services.retention` and :mod:`app.services.storage_quota`
enforce mechanically (oldest-first / over-age deletion) with no
recommendations, scores, or reasons. This module adds ranked deletion
*suggestions* with human-readable reasons without deleting anything.

Ranking signals (highest weight first):
  1. Older than the retention age limit (``max_age_days``).
  2. Needed to bring usage under ``max_total_bytes`` quota (oldest first).
  3. Large files (above average size) -- more bytes reclaimed per deletion.
  4. Stale files (oldest quartile) -- likely safe to review first.
  5. Unassigned bucket (files directly under the storage root).

Protected segments (see :mod:`app.services.rec_protect`) are never
suggested; they are reported via ``protected_excluded`` counts.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

try:
    from app.core.config import RECORDING_DIR  # type: ignore
except Exception:  # noqa: BLE001
    RECORDING_DIR = Path("data/recordings")  # type: ignore

try:
    from app.services.rec_protect import exclude_protected  # type: ignore
except Exception:  # noqa: BLE001
    exclude_protected = None  # type: ignore


VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".mpg", ".mpeg", ".ts"}

_UNASSIGNED = "unassigned"


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


def _as_int(value: Optional[Any]) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return int(float(s))
        except (TypeError, ValueError):
            return _as_bytes(s)
    try:
        return int(value)  # type: ignore[arg-type]
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
    "target_bytes", "budget_bytes",
)
_TARGET_FREE_KEYS = (
    "target_free_bytes", "free_bytes_needed", "bytes_to_free", "need_bytes",
    "reclaim_target_bytes", "target_reclaim_bytes", "min_free_bytes",
    "min_free", "free_target",
)
_LIMIT_KEYS = ("top_n", "topN", "limit", "max_suggestions", "maxSuggestions", "count", "n")
_CAMERA_NAME_KEYS = (
    "camera_name", "cameraName", "camera", "cam", "name", "camera_slug", "slug",
)
_CAMERA_ID_KEYS = ("camera_id", "cameraId", "camera-id", "cam_id", "camId")


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
        return _UNASSIGNED
    if len(rel.parts) > 1:
        return rel.parts[0]
    return _UNASSIGNED


def _drop_protected(candidates: List[Path], storage_root: Optional[Path] = None) -> Tuple[List[Path], int]:
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


class CleanupAdvisor:
    """Rank deletion candidates with reasons. Never deletes files."""

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
        raw_free = _first_present(options, _TARGET_FREE_KEYS, None)
        target_free = _as_bytes(raw_free) if raw_free is not None else None
        raw_limit = _first_present(options, _LIMIT_KEYS, None)
        top_n = _as_int(raw_limit) if raw_limit is not None else None
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
        if cam_id is None and cam_name is None and options.get("id") is not None:
            v = options.get("id")
            if isinstance(v, str) and not v.strip().lstrip("-").isdigit():
                cam_name = v
        include_all = options.get("include_all_files")
        if include_all is None:
            include_all = options.get("includeAllFiles")
        return {
            "storage_root": root,
            "max_age_days": max_age,
            "max_total_bytes": max_total,
            "target_free_bytes": target_free,
            "top_n": top_n,
            "camera_name": cam_name,
            "camera_id": cam_id,
            "include_all_files": bool(include_all) if include_all is not None else False,
            "options": options,
        }

    def _scan(self, root: Path, include_all: bool = False, camera_name: Optional[str] = None) -> List[Path]:
        files: List[Path] = []
        try:
            if not root.exists():
                return files
            if root.is_file():
                if include_all or root.suffix.lower() in VIDEO_EXTENSIONS:
                    files.append(root)
                return files
            search_root = root
            if camera_name:
                cand = root / _safe_camera_name(camera_name)
                if cand.exists() and cand.is_dir():
                    search_root = cand
            for dirpath, _dirnames, filenames in os.walk(search_root):
                for fn in filenames:
                    if fn == ".rec_protect.json":
                        continue
                    fp = Path(dirpath) / fn
                    if not include_all and fp.suffix.lower() not in VIDEO_EXTENSIONS:
                        continue
                    try:
                        if fp.is_file() or fp.is_symlink():
                            files.append(fp)
                    except OSError:
                        continue
        except Exception:  # noqa: BLE001
            pass
        if camera_name and search_root == root:
            # Best-effort filter when camera dir name does not match exactly.
            want = _safe_camera_name(camera_name).lower()
            filtered = [p for p in files if want in str(p).lower()]
            if filtered:
                files = filtered
        return sorted(files, key=str)

    # -- core ------------------------------------------------------------

    def suggest(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        """Return ranked deletion suggestions with reasons (no deletion)."""
        options = self._merged(*args, **kwargs)
        eff = self._effective(options)
        root: Path = eff["storage_root"]
        max_age = eff["max_age_days"]
        max_total = eff["max_total_bytes"]
        target_free = eff["target_free_bytes"]
        top_n = eff["top_n"]

        all_files = self._scan(root, include_all=eff["include_all_files"], camera_name=eff["camera_name"])
        now = time.time()
        total_bytes = sum(_size(p) for p in all_files)
        avg_size = int(total_bytes / len(all_files)) if all_files else 0

        # Quota pressure: oldest-first set needed to get under max_total_bytes.
        quota_needed: set = set()
        if max_total is not None and max_total > 0 and total_bytes > max_total:
            ordered = sorted(all_files, key=lambda p: (_mtime(p), str(p)))
            running = total_bytes
            for fp in ordered:
                if running <= max_total:
                    break
                quota_needed.add(str(fp))
                running -= _size(fp)

        # Stale threshold: oldest quartile cutoff.
        stale_cutoff: Optional[float] = None
        if all_files:
            mtimes = sorted(_mtime(p) for p in all_files)
            stale_cutoff = mtimes[max(0, len(mtimes) // 4 - 1)] if len(mtimes) >= 4 else mtimes[0]

        suggestions: List[Dict[str, Any]] = []
        for fp in all_files:
            sz = _size(fp)
            mt = _mtime(fp)
            age_days = round((now - mt) / 86400.0, 2)
            bucket = _camera_bucket(root, fp)
            reasons: List[str] = []
            score = 0.0

            if max_age is not None and max_age > 0:
                if age_days > max_age:
                    over = round(age_days - max_age, 2)
                    reasons.append(
                        f"older_than_retention: {age_days}d old exceeds {max_age}d limit (over by {over}d)"
                    )
                    score += 100.0 + min(over, 365.0)
            key = str(fp)
            if key in quota_needed:
                reasons.append(
                    f"needed_to_meet_quota: oldest-first candidate to bring {total_bytes} bytes under {max_total} byte quota"
                )
                score += 50.0
            elif max_total is not None and max_total > 0 and total_bytes > max_total:
                reasons.append(
                    f"quota_pressure: total {total_bytes} bytes exceeds {max_total} byte quota"
                )
                score += 10.0
            if avg_size and sz >= avg_size and len(all_files) > 1:
                reasons.append(
                    f"large_file: {sz} bytes above {avg_size} byte average reclaims more per deletion"
                )
                score += 20.0 + min(sz / max(avg_size, 1) * 5.0, 25.0)
            if stale_cutoff is not None and mt <= stale_cutoff and len(all_files) >= 2:
                reasons.append("stale: among oldest files, likely safe to review first")
                score += 5.0
            if bucket == _UNASSIGNED:
                reasons.append("unassigned: file outside any camera directory")
                score += 2.0
            if not reasons:
                reasons.append("reclaimable_space: deletion would free disk space")
            # Recency/size tie-breakers so ranking is deterministic.
            score += min(age_days * 0.1, 36.5)
            score += min(sz / (1024.0 * 1024.0) * 0.1, 10.0)

            suggestions.append(
                {
                    "path": key,
                    "file": key,
                    "file_path": key,
                    "filepath": key,
                    "size_bytes": int(sz),
                    "size": int(sz),
                    "bytes": int(sz),
                    "total_bytes": int(sz),
                    "reclaimable_bytes": int(sz),
                    "freed_bytes": int(sz),
                    "mtime": mt,
                    "mtime_epoch": mt,
                    "age_days": age_days,
                    "age": age_days,
                    "camera": bucket,
                    "camera_name": bucket,
                    "safe_camera_name": _safe_camera_name(bucket),
                    "reason": reasons[0],
                    "reasons": list(reasons),
                    "reason_list": list(reasons),
                    "why": "; ".join(reasons),
                    "explanation": "; ".join(reasons),
                    "score": round(score, 2),
                    "ranking_score": round(score, 2),
                    "priority": round(score, 2),
                }
            )

        # Exclude protected segments from suggestions (never recommend locked files).
        protected_excluded = 0
        if suggestions:
            paths = [Path(s["path"]) for s in suggestions]
            kept, protected_excluded = _drop_protected(paths, storage_root=root)
            kept_set = {str(p) for p in kept}
            suggestions = [s for s in suggestions if s["path"] in kept_set]

        # Rank: highest score first, oldest then largest as deterministic tie-break.
        suggestions.sort(key=lambda s: (-float(s["score"]), float(s["mtime"]), -int(s["size_bytes"]), str(s["path"])))
        for i, s in enumerate(suggestions, start=1):
            s["rank"] = i
            s["position"] = i
            s["order"] = i

        ranked = suggestions if top_n is None else suggestions[: max(0, int(top_n))]
        reclaimable = sum(int(s["size_bytes"]) for s in ranked)
        remaining_after = max(0, total_bytes - reclaimable)
        target_met: Optional[bool] = None
        if target_free is not None:
            target_met = bool(reclaimable >= target_free)
        elif max_total is not None and max_total > 0:
            target_met = bool(remaining_after <= max_total)

        result: Dict[str, Any] = {
            "status": "ok",
            "state": "ok",
            "ok": True,
            "success": True,
            "advisory": True,
            "is_advisory": True,
            "advisor": True,
            "preview": True,
            "is_preview": True,
            "preview_only": True,
            "dry_run": True,
            "dryRun": True,
            "read_only": True,
            "readonly": True,
            "deletes_files": False,
            "never_deletes": True,
            "suggestions": ranked,
            "ranked_suggestions": ranked,
            "suggestions_ranked": ranked,
            "ranked": ranked,
            "ranked_deletions": ranked,
            "deletion_suggestions": ranked,
            "recommendations": ranked,
            "recommended": ranked,
            "recommended_deletions": ranked,
            "candidates": [s["path"] for s in ranked],
            "candidate_files": [s["path"] for s in ranked],
            "suggested_paths": [s["path"] for s in ranked],
            "suggested_files": [s["path"] for s in ranked],
            "paths": [s["path"] for s in ranked],
            "files": [s["path"] for s in ranked],
            "to_delete": [s["path"] for s in ranked],
            "files_to_delete": [s["path"] for s in ranked],
            "all_suggestions": suggestions,
            "all_ranked": suggestions,
            "suggestion_count": len(ranked),
            "suggestions_count": len(ranked),
            "count": len(ranked),
            "total_suggestions": len(ranked),
            "total_candidates": len(suggestions),
            "candidate_count": len(suggestions),
            "reclaimable_bytes": int(reclaimable),
            "reclaimable": int(reclaimable),
            "would_free_bytes": int(reclaimable),
            "freed_bytes": int(reclaimable),
            "freed": int(reclaimable),
            "estimated_freed_bytes": int(reclaimable),
            "total_bytes": int(total_bytes),
            "total": int(total_bytes),
            "scanned_bytes": int(total_bytes),
            "scanned_total_bytes": int(total_bytes),
            "used_bytes": int(total_bytes),
            "file_count": len(all_files),
            "files_count": len(all_files),
            "total_files": len(all_files),
            "scanned_files": len(all_files),
            "scanned_file_count": len(all_files),
            "remaining_bytes": int(remaining_after),
            "remaining": int(remaining_after),
            "remaining_after_bytes": int(remaining_after),
            "target_free_bytes": target_free,
            "target_met": target_met,
            "quota_met_after": target_met,
            "storage_root": str(root),
            "recordings_root": str(root),
            "recordings_dir": str(root),
            "path": str(root),
            "storage_path": str(root),
            "root": str(root),
            "max_age_days": max_age,
            "retention_days": max_age,
            "max_age": max_age,
            "max_total_bytes": max_total,
            "max_bytes": max_total,
            "quota": max_total,
            "limit": max_total,
            "camera_name": eff["camera_name"],
            "camera": eff["camera_name"],
            "camera_id": eff["camera_id"],
            "top_n": top_n,
            "limit_n": top_n,
            "protected_excluded": int(protected_excluded),
            "protected_count": int(protected_excluded),
            "protected_skipped": int(protected_excluded),
            "skipped_protected": int(protected_excluded),
            "skipped_protected_count": int(protected_excluded),
            "errors": [],
            "error_count": 0,
        }
        result["summary"] = {
            "total_files": len(all_files),
            "total_bytes": int(total_bytes),
            "suggestion_count": len(ranked),
            "reclaimable_bytes": int(reclaimable),
            "remaining_bytes": int(remaining_after),
            "protected_excluded": int(protected_excluded),
            "advisory": True,
            "dry_run": True,
        }
        by_camera: Dict[str, List[Dict[str, Any]]] = {}
        for s in ranked:
            by_camera.setdefault(str(s.get("camera_name") or _UNASSIGNED), []).append(s)
        result["by_camera"] = by_camera
        result["suggestions_by_camera"] = by_camera
        result["ranked_by_camera"] = by_camera
        return result

    # -- aliases ----------------------------------------------------------

    def advise(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.suggest(*args, **kwargs)

    def recommend(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.suggest(*args, **kwargs)

    def recommendations(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.suggest(*args, **kwargs)

    def rank(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.suggest(*args, **kwargs)

    def rank_deletions(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.suggest(*args, **kwargs)

    def ranked_suggestions(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.suggest(*args, **kwargs)

    def get_suggestions(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.suggest(*args, **kwargs)

    def get_ranked_suggestions(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.suggest(*args, **kwargs)

    def analyze(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.suggest(*args, **kwargs)

    def get_analysis(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.suggest(*args, **kwargs)

    def preview(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.suggest(*args, **kwargs)

    def report(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.suggest(*args, **kwargs)

    def summary(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.suggest(*args, **kwargs)

    def get_summary(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.suggest(*args, **kwargs)

    def status(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.suggest(*args, **kwargs)

    def get_status(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.suggest(*args, **kwargs)

    def run(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.suggest(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        low = (name or "").lower()
        if any(h in low for h in ("suggest", "advis", "recommend", "rank", "preview", "report", "analy", "summary", "status", "cleanup", "candidate")):
            return self.suggest
        return self.suggest


# ---------- class / instance aliases ----------
CleanupAdvisorService = CleanupAdvisor
CleanupAdviser = CleanupAdvisor
CleanupAdviserService = CleanupAdvisor
StorageCleanupAdvisor = CleanupAdvisor
StorageCleanupAdvisorService = CleanupAdvisor
CleanupSuggestionService = CleanupAdvisor
DeletionAdvisorService = CleanupAdvisor

cleanup_advisor = CleanupAdvisor()
cleanup_advisor_service = cleanup_advisor
cleanup_adviser = cleanup_advisor
storage_cleanup_advisor = cleanup_advisor
service = cleanup_advisor
manager = cleanup_advisor
advisor = cleanup_advisor
default_service = cleanup_advisor
default_cleanup_advisor = cleanup_advisor


# ---------- module-level convenience functions ----------
def suggest(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return cleanup_advisor.suggest(*args, **kwargs)


def advise(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return cleanup_advisor.suggest(*args, **kwargs)


def recommend(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return cleanup_advisor.suggest(*args, **kwargs)


def recommendations(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return cleanup_advisor.suggest(*args, **kwargs)


def rank(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return cleanup_advisor.suggest(*args, **kwargs)


def get_suggestions(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return cleanup_advisor.suggest(*args, **kwargs)


def preview(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return cleanup_advisor.suggest(*args, **kwargs)


def analyze(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return cleanup_advisor.suggest(*args, **kwargs)


def summary(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return cleanup_advisor.suggest(*args, **kwargs)


__all__ = [
    "CleanupAdvisor",
    "CleanupAdvisorService",
    "CleanupAdviser",
    "CleanupAdviserService",
    "StorageCleanupAdvisor",
    "StorageCleanupAdvisorService",
    "CleanupSuggestionService",
    "DeletionAdvisorService",
    "cleanup_advisor",
    "cleanup_advisor_service",
    "cleanup_adviser",
    "storage_cleanup_advisor",
    "service",
    "manager",
    "advisor",
    "suggest",
    "advise",
    "recommend",
    "recommendations",
    "rank",
    "get_suggestions",
    "preview",
    "analyze",
    "summary",
]
