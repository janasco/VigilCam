"""Backup manifest compare service.

Diff two system-backup manifests (see :mod:`app.services.backup`).

``app.services.backup`` has no diff/compare helpers and
``config_history`` is per-camera only, so this module provides the
system-level equivalent: compare the ``manifest.json`` payloads of two
``.zip`` backups (file lists + metadata).

All helpers are sync and dependency-free apart from a lazy import of
:mod:`app.services.backup` when resolving backup ids to manifests.
No ``app/main.py`` changes are required: the API module auto-registers.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    return str(value)


def _normalize_manifest(manifest: Any) -> Dict[str, Any]:
    if manifest is None:
        return {}
    if isinstance(manifest, dict):
        return _json_safe(dict(manifest))
    return {}


def _file_list(manifest: Dict[str, Any]) -> List[str]:
    files = manifest.get("files", manifest.get("file_list", []))
    if not isinstance(files, list):
        return []
    out: List[str] = []
    for f in files:
        if isinstance(f, str):
            out.append(f)
        elif isinstance(f, dict):
            name = f.get("path") or f.get("name") or f.get("file")
            if isinstance(name, str):
                out.append(name)
        else:
            out.append(str(f))
    return out


def diff_manifests(first: Any, second: Any, prefix: str = "") -> List[Dict[str, Any]]:
    """Recursively diff two manifest dicts.

    Returns ``[{"path", "op", "old", "new"}]`` where ``op`` is one of
    ``added`` / ``removed`` / ``changed``.
    """
    a = _normalize_manifest(first)
    b = _normalize_manifest(second)
    changes: List[Dict[str, Any]] = []
    for key in sorted(set(a.keys()) | set(b.keys())):
        path = f"{prefix}.{key}" if prefix else str(key)
        in_a = key in a
        in_b = key in b
        if not in_a:
            changes.append({"path": path, "op": "added", "old": None, "new": b[key]})
        elif not in_b:
            changes.append({"path": path, "op": "removed", "old": a[key], "new": None})
        else:
            av, bv = a[key], b[key]
            if isinstance(av, dict) and isinstance(bv, dict):
                changes.extend(diff_manifests(av, bv, prefix=path))
            elif av != bv:
                changes.append({"path": path, "op": "changed", "old": av, "new": bv})
    return changes


def compare_manifests(first: Any, second: Any) -> Dict[str, Any]:
    """Compare two manifest dicts (pure, no filesystem access)."""
    a = _normalize_manifest(first)
    b = _normalize_manifest(second)
    a_files = _file_list(a)
    b_files = _file_list(b)
    set_a, set_b = set(a_files), set(b_files)
    added = sorted(set_b - set_a)
    removed = sorted(set_a - set_b)
    common = sorted(set_a & set_b)
    manifest_diff = diff_manifests(a, b)
    identical = len(manifest_diff) == 0
    return {
        "first": deepcopy(a),
        "second": deepcopy(b),
        "added_files": added,
        "removed_files": removed,
        "common_files": common,
        "manifest_diff": manifest_diff,
        "differences": deepcopy(manifest_diff),
        "changes": deepcopy(manifest_diff),
        "identical": identical,
        "same": identical,
        "file_count_first": len(a_files),
        "file_count_second": len(b_files),
        "added_count": len(added),
        "removed_count": len(removed),
    }


def _resolve_service(backup_dir: Optional[Any] = None):
    from app.services import backup as backup_mod

    if backup_dir is not None:
        return backup_mod.BackupService(backup_dir=Path(str(backup_dir)))
    return backup_mod.backup_service


def get_manifest(backup_id: Any, backup_dir: Optional[Any] = None) -> Optional[Dict[str, Any]]:
    """Return the stored manifest dict for ``backup_id``, or ``None``."""
    svc = _resolve_service(backup_dir)
    try:
        info = svc.get_backup(backup_id)
    except Exception:  # noqa: BLE001
        return None
    if not info:
        return None
    manifest = info.get("manifest")
    if isinstance(manifest, dict) and manifest:
        return deepcopy(manifest)
    return {}


def get_backup_manifest(backup_id: Any, backup_dir: Optional[Any] = None) -> Optional[Dict[str, Any]]:
    return get_manifest(backup_id, backup_dir=backup_dir)


def compare_backups(
    first_id: Any,
    second_id: Any,
    backup_dir: Optional[Any] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Diff the manifests of two stored backups.

    Raises ``KeyError`` when either backup (or its manifest) is missing.
    """
    if backup_dir is None:
        backup_dir = kwargs.get("backup_dir", kwargs.get("dir", kwargs.get("backups_dir")))
    if first_id is None or (isinstance(first_id, str) and not first_id.strip()):
        raise ValueError("first backup id is required")
    if second_id is None or (isinstance(second_id, str) and not second_id.strip()):
        raise ValueError("second backup id is required")
    svc = _resolve_service(backup_dir)
    try:
        first_info = svc.get_backup(first_id)
    except Exception:  # noqa: BLE001
        first_info = None
    try:
        second_info = svc.get_backup(second_id)
    except Exception:  # noqa: BLE001
        second_info = None
    if first_info is None:
        raise KeyError(f"backup not found: {first_id}")
    if second_info is None:
        raise KeyError(f"backup not found: {second_id}")
    first_manifest = first_info.get("manifest") if isinstance(first_info, dict) else {}
    second_manifest = second_info.get("manifest") if isinstance(second_info, dict) else {}
    if not isinstance(first_manifest, dict):
        first_manifest = {}
    if not isinstance(second_manifest, dict):
        second_manifest = {}
    result = compare_manifests(first_manifest, second_manifest)
    result["first_id"] = str(first_info.get("backup_id") or first_info.get("id") or first_id)
    result["second_id"] = str(second_info.get("backup_id") or second_info.get("id") or second_id)
    result["first_backup"] = {k: v for k, v in first_info.items() if k != "manifest"}
    result["second_backup"] = {k: v for k, v in second_info.items() if k != "manifest"}
    result["ok"] = True
    return result


class BackupCompareService:
    """Object-oriented wrapper around the module-level helpers."""

    def get_manifest(self, backup_id: Any, backup_dir: Optional[Any] = None) -> Optional[Dict[str, Any]]:
        return get_manifest(backup_id, backup_dir=backup_dir)

    def diff_manifests(self, first: Any, second: Any) -> List[Dict[str, Any]]:
        return diff_manifests(first, second)

    def compare_manifests(self, first: Any, second: Any) -> Dict[str, Any]:
        return compare_manifests(first, second)

    def compare_backups(self, first_id: Any, second_id: Any, backup_dir: Optional[Any] = None, **kwargs: Any) -> Dict[str, Any]:
        return compare_backups(first_id, second_id, backup_dir=backup_dir, **kwargs)

    def compare(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        if len(args) >= 2:
            return compare_backups(args[0], args[1], **kwargs)
        return compare_backups(kwargs.get("first_id", kwargs.get("first")), kwargs.get("second_id", kwargs.get("second")), **kwargs)

    def diff(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.compare(*args, **kwargs)


backup_compare_service = BackupCompareService()
service = backup_compare_service
comparator = backup_compare_service
manager = backup_compare_service


# Compatibility aliases.
compare = compare_backups
diff = compare_backups
diff_backups = compare_backups
compare_backup_manifests = compare_manifests
diff_backup_manifests = diff_manifests
get_manifest_for_backup = get_manifest

__all__ = [
    "backup_compare_service",
    "compare_backups",
    "compare_manifests",
    "diff_manifests",
    "get_manifest",
    "get_backup_manifest",
    "BackupCompareService",
]
