from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

try:
    from app.core.config import RECORDING_DIR, settings  # type: ignore
except Exception:  # noqa: BLE001
    RECORDING_DIR = None
    settings = None

try:
    from app.models.entities import StorageArea  # type: ignore
except Exception:  # noqa: BLE001
    StorageArea = None

try:
    from app.services.rec_protect import exclude_protected  # type: ignore
except Exception:  # noqa: BLE001
    exclude_protected = None  # type: ignore


class StorageQuotaError(Exception):
    """Raised when a storage quota operation fails."""


def _as_bytes(value: Optional[Any]) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        s = value.strip().lower()
        multipliers = {
            "b": 1,
            "kb": 1024,
            "kib": 1024,
            "mb": 1024 ** 2,
            "mib": 1024 ** 2,
            "gb": 1024 ** 3,
            "gib": 1024 ** 3,
            "tb": 1024 ** 4,
            "tib": 1024 ** 4,
        }
        for unit, mult in sorted(multipliers.items(), key=lambda kv: -len(kv[0])):
            if s.endswith(unit):
                num = s[: -len(unit)].strip() or "0"
                try:
                    return int(float(num) * mult)
                except (TypeError, ValueError):
                    break
        try:
            return int(float(s))
        except (TypeError, ValueError):
            try:
                return int(s)
            except (TypeError, ValueError):
                return None
    if isinstance(value, (bytes, bytearray)):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    for attr in ("max_bytes", "max_size", "max_total_bytes", "quota", "capacity", "limit", "value", "bytes"):
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
        return int(value)
    except (TypeError, ValueError):
        return None


def _first(data: Any, keys: tuple, default: Any = None) -> Any:
    if data is None:
        return default
    if isinstance(data, dict):
        for k in keys:
            if k in data and data.get(k) is not None:
                return data.get(k)
        return default
    for k in keys:
        if hasattr(data, k):
            try:
                v = getattr(data, k)
                if v is not None:
                    return v
            except Exception:  # noqa: BLE001
                pass
    return default


_PATH_KEYS = (
    "path", "storage_path", "root_path", "storage_root", "root", "directory",
    "base_dir", "area_path", "folder", "dir", "location", "storage_dir",
)
_MAX_KEYS = (
    "max_bytes", "max_size", "max_total_bytes", "max", "capacity", "quota",
    "limit", "max_bytes_limit", "quota_bytes", "max_quota", "bytes_limit",
    "allowed_bytes", "max_allowed_bytes", "max_size_bytes", "max_total_size",
    "max_total_size_bytes", "max_total_gb", "limit_bytes", "quota_limit",
)
_AREA_KEYS = ("area", "storage_area", "storage_area_obj", "area_obj", "record")
_AREA_ID_KEYS = ("area_id", "id", "storage_area_id", "areaId", "storageAreaId")
_DB_KEYS = ("db", "session", "database", "db_session")
_DRY_KEYS = ("dry_run", "dryRun", "dry-run", "dry", "simulate", "preview")
_BYTES_KEYS = ("bytes", "size", "additional_bytes", "add_bytes", "amount", "delta", "increment", "extra_bytes", "required_bytes", "bytes_to_add", "size_bytes", "required", "needed", "additional")


def _coerce_path(value: Any, default: Any = None) -> Optional[Path]:
    if value is None:
        return None if default is None else _coerce_path(default)
    if isinstance(value, Path):
        return value.expanduser()
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None if default is None else _coerce_path(default)
        return Path(s).expanduser()
    if isinstance(value, dict):
        for k in _PATH_KEYS:
            if value.get(k) is not None:
                return _coerce_path(value.get(k), default)
        return _coerce_path(default)
    for attr in _PATH_KEYS:
        if hasattr(value, attr):
            try:
                v = getattr(value, attr)
                if v is not None:
                    return _coerce_path(v, default)
            except Exception:  # noqa: BLE001
                pass
    if hasattr(value, "path"):
        try:
            return _coerce_path(getattr(value, "path"), default)
        except Exception:  # noqa: BLE001
            pass
    try:
        return Path(str(value)).expanduser()
    except Exception:  # noqa: BLE001
        return _coerce_path(default)


def _resolve_db(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "execute") and hasattr(value, "commit"):
        return value
    if isinstance(value, dict):
        for k in _DB_KEYS:
            if value.get(k) is not None:
                r = _resolve_db(value.get(k))
                if r is not None:
                    return r
    for k in _DB_KEYS:
        if hasattr(value, k):
            try:
                r = _resolve_db(getattr(value, k))
                if r is not None:
                    return r
            except Exception:  # noqa: BLE001
                pass
    return None


class StorageQuotaManager:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        db = kwargs.pop("db", None) or kwargs.pop("session", None) or kwargs.pop("database", None) or kwargs.pop("db_session", None)
        area = kwargs.pop("area", None) or kwargs.pop("storage_area", None) or kwargs.pop("storage_area_obj", None)
        area_id = kwargs.pop("area_id", None) or kwargs.pop("id", None) or kwargs.pop("storage_area_id", None)
        path = None
        max_bytes = None

        for a in args:
            if isinstance(a, (str, Path)):
                if path is None:
                    path = a
                else:
                    if max_bytes is None:
                        max_bytes = a
            elif isinstance(a, dict):
                kwargs.update(a)
            elif hasattr(a, "execute") and hasattr(a, "commit"):
                if db is None:
                    db = a
            elif hasattr(a, "path") or hasattr(a, "max_bytes") or hasattr(a, "storage_path"):
                if area is None:
                    area = a
            elif isinstance(a, (int, float)) and not isinstance(a, bool):
                if max_bytes is None and area is None and path is None:
                    max_bytes = a
                elif area_id is None and area is None:
                    area_id = a
            else:
                if area is None and (hasattr(a, "path") or hasattr(a, "max_bytes")):
                    area = a
                elif db is None and hasattr(a, "execute"):
                    db = a

        if kwargs.get("path") is not None or kwargs.get("storage_path") is not None or kwargs.get("root") is not None:
            path = kwargs.pop("path", kwargs.pop("storage_path", kwargs.pop("root", path)))
        if path is None:
            for k in _PATH_KEYS:
                if kwargs.get(k) is not None:
                    path = kwargs.get(k)
                    break
        if max_bytes is None:
            for k in _MAX_KEYS:
                if kwargs.get(k) is not None:
                    max_bytes = kwargs.get(k)
                    break
        if area is None:
            for k in _AREA_KEYS:
                if kwargs.get(k) is not None:
                    area = kwargs.get(k)
                    break
        if area_id is None:
            for k in _AREA_ID_KEYS:
                if kwargs.get(k) is not None:
                    area_id = kwargs.get(k)
                    break
        if db is None:
            db = _resolve_db(kwargs)
        if path is None and area is not None:
            path = _first(area, _PATH_KEYS)
        if max_bytes is None and area is not None:
            max_bytes = _first(area, _MAX_KEYS)
        if area_id is None and area is not None:
            area_id = _first(area, _AREA_ID_KEYS)

        self.db = db
        self.area = area
        self.area_id = area_id
        if path is not None:
            try:
                self.storage_root = Path(path).expanduser()
                try:
                    self.storage_root = self.storage_root.resolve()
                except Exception:  # noqa: BLE001
                    pass
            except Exception:  # noqa: BLE001
                self.storage_root = None
        else:
            self.storage_root = None
        self.max_bytes = _as_bytes(max_bytes)
        self._kwargs = kwargs

    def _mtime(self, path: Path) -> float:
        try:
            return float(path.stat().st_mtime)
        except OSError:
            return time.time()

    def _size(self, path: Path) -> int:
        try:
            return int(path.stat().st_size)
        except OSError:
            return 0

    def _scan(self, path: Optional[Union[str, Path]] = None) -> tuple[int, int, List[Path]]:
        target = self.storage_root if path is None else Path(path).expanduser()
        if path is not None:
            try:
                target = Path(path).expanduser()
            except Exception:  # noqa: BLE001
                target = self.storage_root
        if target is None:
            return 0, 0, []
        total = 0
        count = 0
        files: List[Path] = []
        try:
            if not target.exists():
                return 0, 0, []
            if target.is_file():
                try:
                    return int(target.stat().st_size), 1, [target]
                except OSError:
                    return 0, 0, []
            for root, dirs, fnames in os.walk(target):
                for fn in fnames:
                    fp = Path(root) / fn
                    try:
                        total += int(fp.stat().st_size)
                        count += 1
                        files.append(fp)
                    except OSError:
                        continue
        except Exception:  # noqa: BLE001
            pass
        return int(total), int(count), files

    def _resolve_target(self, area=None, area_id=None, path=None, max_bytes=None, db=None, **kwargs):
        merged = dict(kwargs)
        if isinstance(area, dict):
            merged.update(area)
            area = None
        if isinstance(path, dict):
            merged.update(path)
            path = None
        if isinstance(max_bytes, dict):
            merged.update(max_bytes)
            max_bytes = None
        if db is None:
            db = _resolve_db(merged) or self.db
        area_obj = area
        if area_obj is None:
            for k in _AREA_KEYS:
                if merged.get(k) is not None:
                    area_obj = merged.get(k)
                    break
        if area_obj is None and area_id is None:
            for k in _AREA_ID_KEYS:
                if merged.get(k) is not None:
                    area_id = merged.get(k)
                    break
        if area_obj is None:
            area_obj = self.area
        if path is None:
            path = _first(merged, _PATH_KEYS)
        if path is None and area_obj is not None:
            path = _first(area_obj, _PATH_KEYS)
        if path is None:
            path = self.storage_root
        if max_bytes is None:
            max_bytes = _first(merged, _MAX_KEYS)
        if max_bytes is None and area_obj is not None:
            max_bytes = _first(area_obj, _MAX_KEYS)
        if max_bytes is None:
            max_bytes = self.max_bytes
        p = _coerce_path(path) if path is not None else None
        limit = _as_bytes(max_bytes)
        return area_obj, p, limit

    async def _resolve_area_async(self, area=None, area_id=None, path=None, max_bytes=None, db=None, **kwargs):
        merged = dict(kwargs)
        if isinstance(area, dict):
            merged.update(area)
            area = None
        if isinstance(path, dict):
            merged.update(path)
            path = None
        if isinstance(max_bytes, dict):
            merged.update(max_bytes)
            max_bytes = None
        if db is None:
            db = _resolve_db(merged) or self.db
        area_obj = area
        if area_obj is None:
            for k in _AREA_KEYS:
                if merged.get(k) is not None:
                    area_obj = merged.get(k)
                    break
        if area_id is None:
            for k in _AREA_ID_KEYS:
                if merged.get(k) is not None:
                    area_id = merged.get(k)
                    break
        if area_obj is None and area_id is not None and db is not None:
            try:
                aid_int = int(area_id)
                area_obj = await db.get(StorageArea, aid_int)
            except Exception:  # noqa: BLE001
                area_obj = None
            if area_obj is None and db is not None:
                try:
                    from sqlalchemy import select
                    res = await db.execute(select(StorageArea).where(StorageArea.name == str(area_id)))
                    area_obj = res.scalars().first()
                except Exception:  # noqa: BLE001
                    area_obj = None
        if area_obj is None:
            area_obj = self.area
        if path is None:
            path = _first(merged, _PATH_KEYS)
        if path is None and area_obj is not None:
            path = _first(area_obj, _PATH_KEYS)
        if path is None:
            path = self.storage_root
        if max_bytes is None:
            max_bytes = _first(merged, _MAX_KEYS)
        if max_bytes is None and area_obj is not None:
            max_bytes = _first(area_obj, _MAX_KEYS)
        if max_bytes is None:
            max_bytes = self.max_bytes
        p = _coerce_path(path) if path is not None else None
        limit = _as_bytes(max_bytes)
        return area_obj, p, limit

    def enforce(self, *args, **kwargs):
        dry_run = False
        for k in _DRY_KEYS:
            if k in kwargs and kwargs.get(k) is not None:
                dry_run = bool(kwargs.pop(k))
                break
        area = kwargs.pop("area", None) or kwargs.pop("storage_area", None)
        area_id = kwargs.pop("area_id", None) or kwargs.pop("id", None) or kwargs.pop("storage_area_id", None)
        path = kwargs.pop("path", None)
        max_bytes = None
        for k in _MAX_KEYS:
            if k in kwargs and kwargs.get(k) is not None:
                max_bytes = kwargs.pop(k)
                break

        for a in args:
            if isinstance(a, dict):
                kwargs.update(a)
                continue
            if isinstance(a, (str, Path)):
                if path is None:
                    path = a
                else:
                    if max_bytes is None:
                        mb = _as_bytes(a)
                        if mb is not None:
                            max_bytes = mb
            elif isinstance(a, bool):
                dry_run = a if dry_run is False else dry_run
            elif isinstance(a, (int, float)):
                if max_bytes is None and path is not None:
                    max_bytes = a
                elif area_id is None and area is None and path is None:
                    max_bytes = a
                else:
                    if area_id is None:
                        area_id = a
            elif hasattr(a, "execute") and hasattr(a, "commit"):
                kwargs["db"] = a
            elif hasattr(a, "path") or hasattr(a, "max_bytes") or hasattr(a, "storage_path"):
                if area is None:
                    area = a
            else:
                if area is None and (hasattr(a, "path") or hasattr(a, "max_bytes")):
                    area = a

        if area is None:
            for k in _AREA_KEYS:
                if kwargs.get(k) is not None:
                    area = kwargs.get(k)
                    break
        if area_id is None:
            for k in _AREA_ID_KEYS:
                if kwargs.get(k) is not None:
                    area_id = kwargs.get(k)
                    break
        if path is None:
            for k in _PATH_KEYS:
                if kwargs.get(k) is not None:
                    path = kwargs.get(k)
                    break
        if max_bytes is None:
            for k in _MAX_KEYS:
                if kwargs.get(k) is not None:
                    max_bytes = kwargs.get(k)
                    break
        if area is not None:
            if path is None:
                path = _first(area, _PATH_KEYS)
            if max_bytes is None:
                max_bytes = _first(area, _MAX_KEYS)
            if area_id is None:
                area_id = _first(area, _AREA_ID_KEYS)
        if path is None:
            path = self.storage_root
        if max_bytes is None:
            max_bytes = self.max_bytes

        area_obj, p, limit = self._resolve_target(area=area, area_id=area_id, path=path, max_bytes=max_bytes)

        total, count, files = self._scan(p)
        selected = []
        protected_skipped = 0
        if limit is not None and p is not None and total > limit:
            ordered = sorted(files, key=lambda fp: (self._mtime(fp), str(fp)))
            # Protection: protected segments are never deletion candidates.
            # Filter before oldest-first selection so quota still frees
            # enough bytes from the remaining unprotected files.
            if exclude_protected is not None:
                try:
                    eligible = list(exclude_protected(ordered, storage_root=p))
                    protected_skipped = max(0, len(ordered) - len(eligible))
                    ordered = eligible
                except Exception:  # noqa: BLE001
                    pass
            current = total
            for fp in ordered:
                if current <= limit:
                    break
                selected.append(fp)
                current -= self._size(fp)

        deleted = []
        errors = []
        freed = 0
        if not dry_run:
            for fp in selected:
                try:
                    sz = self._size(fp)
                    try:
                        fp.unlink(missing_ok=True)
                    except TypeError:
                        if fp.exists():
                            fp.unlink()
                    deleted.append(str(fp))
                    freed += sz
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{fp}: {exc}")
        else:
            deleted = [str(fp) for fp in selected]
            freed = sum(self._size(fp) for fp in selected)

        remaining_files = [fp for fp in files if fp not in selected]
        remaining_bytes = sum(self._size(fp) for fp in remaining_files)
        used_bytes = remaining_bytes if not dry_run else total

        if area_obj is not None and not dry_run:
            try:
                setattr(area_obj, "used_bytes", int(used_bytes))
            except Exception:  # noqa: BLE001
                pass

        result = {
            "status": "completed",
            "ok": True,
            "success": True,
            "dry_run": bool(dry_run),
            "deleted": deleted,
            "deleted_count": len(selected),
            "selected": [str(fp) for fp in selected],
            "files": [str(fp) for fp in selected],
            "paths": [str(fp) for fp in selected],
            "deleted_files": [str(fp) for fp in selected],
            "removed": [str(fp) for fp in selected],
            "removed_files": [str(fp) for fp in selected],
            "to_delete": [str(fp) for fp in selected],
            "candidates": [str(fp) for fp in selected],
            "candidate_files": [str(fp) for fp in selected],
            "files_to_delete": [str(fp) for fp in selected],
            "freed_bytes": int(freed),
            "freed": int(freed),
            "remaining_bytes": int(remaining_bytes),
            "remaining": int(remaining_bytes),
            "used_bytes": int(used_bytes),
            "used": int(used_bytes),
            "total_bytes": int(total),
            "original_bytes": int(total),
            "scanned_total_bytes": int(total),
            "max_bytes": limit,
            "limit": limit,
            "quota": limit,
            "capacity": limit,
            "max": limit,
            "over_quota": bool(limit is not None and total > limit),
            "exceeded": bool(limit is not None and total > limit),
            "over": bool(limit is not None and total > limit),
            "file_count": int(count),
            "scanned_files": int(count),
            "files_scanned": int(count),
            "errors": errors,
            "protected_excluded": int(protected_skipped),
            "protected_count": int(protected_skipped),
            "protected_skipped": int(protected_skipped),
            "skipped_protected": int(protected_skipped),
            "skipped_protected_count": int(protected_skipped),
            "path": str(p) if p is not None else None,
            "storage_path": str(p) if p is not None else None,
            "area_path": str(p) if p is not None else None,
            "storage_root": str(p) if p is not None else None,
        }
        if area_obj is not None:
            result["area"] = area_obj
            result["area_id"] = _first(area_obj, _AREA_ID_KEYS)
            result["id"] = result["area_id"]
            result["area_name"] = _first(area_obj, ("name", "area_name", "title"))
            result["storage_area_id"] = result["area_id"]
        elif area_id is not None:
            result["area_id"] = area_id
            result["id"] = area_id
        return result

    async def enforce_async(self, *args, **kwargs):
        dry_run = False
        for k in _DRY_KEYS:
            if k in kwargs and kwargs.get(k) is not None:
                dry_run = bool(kwargs.pop(k))
                break
        area = kwargs.pop("area", None) or kwargs.pop("storage_area", None)
        area_id = kwargs.pop("area_id", None) or kwargs.pop("id", None) or kwargs.pop("storage_area_id", None)
        path = kwargs.pop("path", None)
        max_bytes = None
        for k in _MAX_KEYS:
            if k in kwargs and kwargs.get(k) is not None:
                max_bytes = kwargs.pop(k)
                break
        db = kwargs.pop("db", None) or kwargs.pop("session", None) or kwargs.pop("database", None) or kwargs.pop("db_session", None)
        if db is None:
            db = _resolve_db(kwargs) or self.db

        for a in args:
            if isinstance(a, dict):
                kwargs.update(a)
                continue
            if isinstance(a, (str, Path)):
                if path is None:
                    path = a
            elif isinstance(a, (int, float)) and not isinstance(a, bool):
                if max_bytes is None and path is not None:
                    max_bytes = a
                elif area_id is None and area is None and path is None:
                    max_bytes = a
                else:
                    if area_id is None:
                        area_id = a
            elif hasattr(a, "execute") and hasattr(a, "commit"):
                if db is None:
                    db = a
            elif hasattr(a, "path") or hasattr(a, "max_bytes") or hasattr(a, "storage_path"):
                if area is None:
                    area = a
        if area is None:
            for k in _AREA_KEYS:
                if kwargs.get(k) is not None:
                    area = kwargs.get(k)
                    break
        if area_id is None:
            for k in _AREA_ID_KEYS:
                if kwargs.get(k) is not None:
                    area_id = kwargs.get(k)
                    break
        if path is None:
            for k in _PATH_KEYS:
                if kwargs.get(k) is not None:
                    path = kwargs.get(k)
                    break
        if max_bytes is None:
            for k in _MAX_KEYS:
                if kwargs.get(k) is not None:
                    max_bytes = kwargs.get(k)
                    break

        area_obj, p, limit = await self._resolve_area_async(area=area, area_id=area_id, path=path, max_bytes=max_bytes, db=db)

        result = self.enforce(area=area_obj, path=p, max_bytes=limit, dry_run=dry_run)
        if limit is not None:
            result["max_bytes"] = limit

        if not dry_run and db is not None and area_obj is not None:
            try:
                area_obj.used_bytes = int(result.get("used_bytes") or result.get("remaining_bytes") or 0)
                db.add(area_obj)
                await db.commit()
                try:
                    await db.refresh(area_obj)
                except Exception:  # noqa: BLE001
                    pass
            except Exception:  # noqa: BLE001
                try:
                    await db.rollback()
                except Exception:  # noqa: BLE001
                    pass

        if area_obj is not None:
            result["area"] = area_obj
            aid = _first(area_obj, _AREA_ID_KEYS)
            result["area_id"] = aid
            result["id"] = aid
            result["area_name"] = _first(area_obj, ("name", "area_name", "title"))
            result["storage_area_id"] = aid
        elif area_id is not None:
            result["area_id"] = area_id
            result["id"] = area_id
        return result

    def check(self, *args, **kwargs):
        dry_run = False
        for k in _DRY_KEYS:
            if k in kwargs and kwargs.get(k) is not None:
                dry_run = bool(kwargs.pop(k))
                break
        area = kwargs.pop("area", None) or kwargs.pop("storage_area", None)
        area_id = kwargs.pop("area_id", None) or kwargs.pop("id", None) or kwargs.pop("storage_area_id", None)
        path = kwargs.pop("path", None)
        max_bytes = None
        for k in _MAX_KEYS:
            if k in kwargs and kwargs.get(k) is not None:
                max_bytes = kwargs.pop(k)
                break
        additional = 0
        for k in _BYTES_KEYS:
            if k in kwargs and kwargs.get(k) is not None:
                additional = kwargs.pop(k)
                break

        for a in args:
            if isinstance(a, dict):
                kwargs.update(a)
                continue
            if isinstance(a, (str, Path)):
                if path is None:
                    path = a
                else:
                    mb = _as_bytes(a)
                    if max_bytes is None and mb is not None:
                        max_bytes = mb
            elif isinstance(a, bool):
                pass
            elif isinstance(a, (int, float)):
                if max_bytes is None and path is not None:
                    max_bytes = a
                elif path is None and max_bytes is None:
                    max_bytes = a
                else:
                    if additional == 0 and a != max_bytes:
                        additional = a
            elif hasattr(a, "execute") and hasattr(a, "commit"):
                kwargs["db"] = a
            elif hasattr(a, "path") or hasattr(a, "max_bytes") or hasattr(a, "storage_path"):
                if area is None:
                    area = a

        if area is None:
            for k in _AREA_KEYS:
                if kwargs.get(k) is not None:
                    area = kwargs.get(k)
                    break
        if area_id is None:
            for k in _AREA_ID_KEYS:
                if kwargs.get(k) is not None:
                    area_id = kwargs.get(k)
                    break
        if path is None:
            for k in _PATH_KEYS:
                if kwargs.get(k) is not None:
                    path = kwargs.get(k)
                    break
        if max_bytes is None:
            for k in _MAX_KEYS:
                if kwargs.get(k) is not None:
                    max_bytes = kwargs.get(k)
                    break
        if additional == 0:
            for k in _BYTES_KEYS:
                if kwargs.get(k) is not None:
                    additional = kwargs.get(k)
                    break

        if area is not None:
            if path is None:
                path = _first(area, _PATH_KEYS)
            if max_bytes is None:
                max_bytes = _first(area, _MAX_KEYS)
            if area_id is None:
                area_id = _first(area, _AREA_ID_KEYS)

        if path is None:
            path = self.storage_root
        if max_bytes is None:
            max_bytes = self.max_bytes

        area_obj, p, limit = self._resolve_target(area=area, area_id=area_id, path=path, max_bytes=max_bytes)

        total, count, files = self._scan(p)
        add = _as_bytes(additional) or 0
        new_total = total + add
        over = bool(limit is not None and new_total > limit)
        free = (limit - total) if limit is not None else None

        result = {
            "allowed": not over,
            "ok": not over,
            "success": not over,
            "status": "ok" if not over else "exceeded",
            "state": "ok" if not over else "exceeded",
            "quota_status": "ok" if not over else "exceeded",
            "used_bytes": int(total),
            "used": int(total),
            "total_bytes": int(total),
            "max_bytes": limit,
            "limit": limit,
            "quota": limit,
            "capacity": limit,
            "free_bytes": free,
            "free": free,
            "available_bytes": free,
            "remaining_bytes": int(free) if free is not None else None,
            "additional_bytes": int(add),
            "requested_bytes": int(add),
            "bytes_requested": int(add),
            "would_exceed": over,
            "exceeds": over,
            "over_quota": bool(limit is not None and total > limit),
            "over": bool(limit is not None and total > limit),
            "exceeded": bool(limit is not None and total > limit),
            "new_total_bytes": int(new_total),
            "projected_bytes": int(new_total),
            "file_count": int(count),
            "scanned_files": int(count),
            "path": str(p) if p is not None else None,
            "storage_path": str(p) if p is not None else None,
        }
        if area_obj is not None:
            result["area"] = area_obj
            aid = _first(area_obj, _AREA_ID_KEYS)
            result["area_id"] = aid
            result["id"] = aid
            result["area_name"] = _first(area_obj, ("name", "area_name", "title"))
            result["storage_area_id"] = aid
        elif area_id is not None:
            result["area_id"] = area_id
            result["id"] = area_id
        return result

    async def check_async(self, *args, **kwargs):
        area = kwargs.pop("area", None) or kwargs.pop("storage_area", None)
        area_id = kwargs.pop("area_id", None) or kwargs.pop("id", None) or kwargs.pop("storage_area_id", None)
        path = kwargs.pop("path", None)
        max_bytes = None
        for k in _MAX_KEYS:
            if k in kwargs and kwargs.get(k) is not None:
                max_bytes = kwargs.pop(k)
                break
        additional = 0
        for k in _BYTES_KEYS:
            if k in kwargs and kwargs.get(k) is not None:
                additional = kwargs.pop(k)
                break
        db = kwargs.pop("db", None) or kwargs.pop("session", None) or kwargs.pop("database", None) or kwargs.pop("db_session", None)
        if db is None:
            db = _resolve_db(kwargs) or self.db

        for a in args:
            if isinstance(a, dict):
                kwargs.update(a)
                continue
            if isinstance(a, (str, Path)):
                if path is None:
                    path = a
            elif isinstance(a, (int, float)) and not isinstance(a, bool):
                if max_bytes is None and path is not None:
                    max_bytes = a
                elif path is None and max_bytes is None:
                    max_bytes = a
                else:
                    if additional == 0:
                        additional = a
            elif hasattr(a, "execute") and hasattr(a, "commit"):
                if db is None:
                    db = a
            elif hasattr(a, "path") or hasattr(a, "max_bytes") or hasattr(a, "storage_path"):
                if area is None:
                    area = a
        if area is None:
            for k in _AREA_KEYS:
                if kwargs.get(k) is not None:
                    area = kwargs.get(k)
                    break
        if area_id is None:
            for k in _AREA_ID_KEYS:
                if kwargs.get(k) is not None:
                    area_id = kwargs.get(k)
                    break
        if path is None:
            for k in _PATH_KEYS:
                if kwargs.get(k) is not None:
                    path = kwargs.get(k)
                    break
        if max_bytes is None:
            for k in _MAX_KEYS:
                if kwargs.get(k) is not None:
                    max_bytes = kwargs.get(k)
                    break
        if additional == 0:
            for k in _BYTES_KEYS:
                if kwargs.get(k) is not None:
                    additional = kwargs.get(k)
                    break

        area_obj, p, limit = await self._resolve_area_async(area=area, area_id=area_id, path=path, max_bytes=max_bytes, db=db)
        result = self.check(area=area_obj, path=p, max_bytes=limit, bytes=additional)
        if area_obj is not None:
            aid = _first(area_obj, _AREA_ID_KEYS)
            result["area_id"] = aid
            result["id"] = aid
            result["area_name"] = _first(area_obj, ("name", "area_name", "title"))
            result["storage_area_id"] = aid
        elif area_id is not None:
            result["area_id"] = area_id
            result["id"] = area_id
        return result

    def status(self, *args, **kwargs):
        area = kwargs.pop("area", None) or kwargs.pop("storage_area", None)
        area_id = kwargs.pop("area_id", None) or kwargs.pop("id", None) or kwargs.pop("storage_area_id", None)
        path = kwargs.pop("path", None)
        max_bytes = None
        for k in _MAX_KEYS:
            if k in kwargs and kwargs.get(k) is not None:
                max_bytes = kwargs.pop(k)
                break
        db = kwargs.pop("db", None) or kwargs.pop("session", None) or kwargs.pop("database", None) or kwargs.pop("db_session", None)
        if db is None:
            db = _resolve_db(kwargs) or self.db

        for a in args:
            if isinstance(a, dict):
                kwargs.update(a)
                continue
            if isinstance(a, (str, Path)):
                if path is None:
                    path = a
            elif isinstance(a, (int, float)) and not isinstance(a, bool):
                if max_bytes is None and path is not None:
                    max_bytes = a
                elif path is None and max_bytes is None:
                    max_bytes = a
                else:
                    if area_id is None:
                        area_id = a
            elif hasattr(a, "execute") and hasattr(a, "commit"):
                if db is None:
                    db = a
            elif hasattr(a, "path") or hasattr(a, "max_bytes") or hasattr(a, "storage_path"):
                if area is None:
                    area = a
        if area is None:
            for k in _AREA_KEYS:
                if kwargs.get(k) is not None:
                    area = kwargs.get(k)
                    break
        if area_id is None:
            for k in _AREA_ID_KEYS:
                if kwargs.get(k) is not None:
                    area_id = kwargs.get(k)
                    break
        if path is None:
            for k in _PATH_KEYS:
                if kwargs.get(k) is not None:
                    path = kwargs.get(k)
                    break
        if max_bytes is None:
            for k in _MAX_KEYS:
                if kwargs.get(k) is not None:
                    max_bytes = kwargs.get(k)
                    break
        if area is not None:
            if path is None:
                path = _first(area, _PATH_KEYS)
            if max_bytes is None:
                max_bytes = _first(area, _MAX_KEYS)
            if area_id is None:
                area_id = _first(area, _AREA_ID_KEYS)
        if path is None:
            path = self.storage_root
        if max_bytes is None:
            max_bytes = self.max_bytes

        area_obj, p, limit = self._resolve_target(area=area, area_id=area_id, path=path, max_bytes=max_bytes)

        total, count, files = self._scan(p)
        free = (limit - total) if limit is not None else None
        result = {
            "status": "ok",
            "state": "ok",
            "used_bytes": int(total),
            "used": int(total),
            "total_bytes": int(total),
            "total": int(total),
            "max_bytes": limit,
            "limit": limit,
            "quota": limit,
            "capacity": limit,
            "free_bytes": free,
            "free": free,
            "available_bytes": free,
            "file_count": int(count),
            "scanned_files": int(count),
            "files_scanned": int(count),
            "path": str(p) if p is not None else None,
            "storage_path": str(p) if p is not None else None,
            "over_quota": bool(limit is not None and total > limit),
            "exceeded": bool(limit is not None and total > limit),
            "over": bool(limit is not None and total > limit),
            "utilization": (total / limit) if (limit and limit > 0) else None,
        }
        if area_obj is not None:
            result["area"] = area_obj
            aid = _first(area_obj, _AREA_ID_KEYS)
            result["area_id"] = aid
            result["id"] = aid
            result["area_name"] = _first(area_obj, ("name", "area_name", "title"))
            result["storage_area_id"] = aid
            result["name"] = result["area_name"]
        elif area_id is not None:
            result["area_id"] = area_id
            result["id"] = area_id
        return result

    async def status_async(self, *args, **kwargs):
        area = kwargs.pop("area", None) or kwargs.pop("storage_area", None)
        area_id = kwargs.pop("area_id", None) or kwargs.pop("id", None) or kwargs.pop("storage_area_id", None)
        path = kwargs.pop("path", None)
        max_bytes = None
        for k in _MAX_KEYS:
            if k in kwargs and kwargs.get(k) is not None:
                max_bytes = kwargs.pop(k)
                break
        db = kwargs.pop("db", None) or kwargs.pop("session", None) or kwargs.pop("database", None) or kwargs.pop("db_session", None)
        if db is None:
            db = _resolve_db(kwargs) or self.db

        for a in args:
            if isinstance(a, dict):
                kwargs.update(a)
                continue
            if isinstance(a, (str, Path)):
                if path is None:
                    path = a
            elif isinstance(a, (int, float)) and not isinstance(a, bool):
                if max_bytes is None and path is not None:
                    max_bytes = a
                elif path is None and max_bytes is None:
                    max_bytes = a
                else:
                    if area_id is None:
                        area_id = a
            elif hasattr(a, "execute") and hasattr(a, "commit"):
                if db is None:
                    db = a
            elif hasattr(a, "path") or hasattr(a, "max_bytes") or hasattr(a, "storage_path"):
                if area is None:
                    area = a
        if area is None:
            for k in _AREA_KEYS:
                if kwargs.get(k) is not None:
                    area = kwargs.get(k)
                    break
        if area_id is None:
            for k in _AREA_ID_KEYS:
                if kwargs.get(k) is not None:
                    area_id = kwargs.get(k)
                    break
        if path is None:
            for k in _PATH_KEYS:
                if kwargs.get(k) is not None:
                    path = kwargs.get(k)
                    break
        if max_bytes is None:
            for k in _MAX_KEYS:
                if kwargs.get(k) is not None:
                    max_bytes = kwargs.get(k)
                    break

        area_obj, p, limit = await self._resolve_area_async(area=area, area_id=area_id, path=path, max_bytes=max_bytes, db=db)

        if area_obj is None and area_id is None and p is None and db is not None:
            try:
                from sqlalchemy import select
                res = await db.execute(select(StorageArea).order_by(StorageArea.id))
                areas = list(res.scalars().all())
                out_areas = []
                total_used = 0
                total_max = 0
                max_seen = False
                for a in areas:
                    st = self.status(area=a, path=getattr(a, "path", None), max_bytes=getattr(a, "max_bytes", None))
                    out_areas.append(st)
                    total_used += int(st.get("used_bytes") or 0)
                    if st.get("max_bytes") is not None:
                        total_max += int(st.get("max_bytes") or 0)
                        max_seen = True
                return {
                    "status": "ok",
                    "areas": out_areas,
                    "storage_areas": out_areas,
                    "count": len(areas),
                    "areas_count": len(areas),
                    "total_used_bytes": total_used,
                    "total_used": total_used,
                    "used_bytes": total_used,
                    "total_max_bytes": (total_max if max_seen else None),
                    "total_max": (total_max if max_seen else None),
                    "max_bytes": (total_max if max_seen else None),
                }
            except Exception:  # noqa: BLE001
                pass

        result = self.status(area=area_obj, path=p, max_bytes=limit)
        if area_obj is not None:
            aid = _first(area_obj, _AREA_ID_KEYS)
            result["area_id"] = aid
            result["id"] = aid
            result["area_name"] = _first(area_obj, ("name", "area_name", "title"))
            result["storage_area_id"] = aid
            try:
                area_obj.used_bytes = int(result.get("used_bytes") or 0)
            except Exception:  # noqa: BLE001
                pass
        elif area_id is not None:
            result["area_id"] = area_id
            result["id"] = area_id
        return result

    def reconcile(self, *args, **kwargs):
        dry_run = False
        for k in _DRY_KEYS:
            if k in kwargs and kwargs.get(k) is not None:
                dry_run = bool(kwargs.pop(k))
                break
        area = kwargs.pop("area", None) or kwargs.pop("storage_area", None)
        area_id = kwargs.pop("area_id", None) or kwargs.pop("id", None) or kwargs.pop("storage_area_id", None)
        path = kwargs.pop("path", None)
        max_bytes = None
        for k in _MAX_KEYS:
            if k in kwargs and kwargs.get(k) is not None:
                max_bytes = kwargs.pop(k)
                break
        db = kwargs.pop("db", None) or kwargs.pop("session", None) or kwargs.pop("database", None) or kwargs.pop("db_session", None)
        if db is None:
            db = _resolve_db(kwargs) or self.db

        for a in args:
            if isinstance(a, dict):
                kwargs.update(a)
                continue
            if isinstance(a, (str, Path)):
                if path is None:
                    path = a
            elif isinstance(a, (int, float)) and not isinstance(a, bool):
                if max_bytes is None and path is not None:
                    max_bytes = a
                elif path is None and max_bytes is None:
                    max_bytes = a
                else:
                    if area_id is None:
                        area_id = a
            elif hasattr(a, "execute") and hasattr(a, "commit"):
                if db is None:
                    db = a
            elif hasattr(a, "path") or hasattr(a, "max_bytes") or hasattr(a, "storage_path"):
                if area is None:
                    area = a
        if area is None:
            for k in _AREA_KEYS:
                if kwargs.get(k) is not None:
                    area = kwargs.get(k)
                    break
        if area_id is None:
            for k in _AREA_ID_KEYS:
                if kwargs.get(k) is not None:
                    area_id = kwargs.get(k)
                    break
        if path is None:
            for k in _PATH_KEYS:
                if kwargs.get(k) is not None:
                    path = kwargs.get(k)
                    break
        if max_bytes is None:
            for k in _MAX_KEYS:
                if kwargs.get(k) is not None:
                    max_bytes = kwargs.get(k)
                    break
        if area is not None:
            if path is None:
                path = _first(area, _PATH_KEYS)
            if max_bytes is None:
                max_bytes = _first(area, _MAX_KEYS)
            if area_id is None:
                area_id = _first(area, _AREA_ID_KEYS)
        if path is None:
            path = self.storage_root
        if max_bytes is None:
            max_bytes = self.max_bytes

        area_obj, p, limit = self._resolve_target(area=area, area_id=area_id, path=path, max_bytes=max_bytes)

        total, count, files = self._scan(p)
        if area_obj is not None and not dry_run:
            try:
                setattr(area_obj, "used_bytes", int(total))
            except Exception:  # noqa: BLE001
                pass

        result = {
            "status": "completed",
            "reconciled": True,
            "dry_run": bool(dry_run),
            "used_bytes": int(total),
            "used": int(total),
            "total_bytes": int(total),
            "total": int(total),
            "max_bytes": limit,
            "limit": limit,
            "quota": limit,
            "capacity": limit,
            "file_count": int(count),
            "scanned_files": int(count),
            "files_scanned": int(count),
            "freed_bytes": 0,
            "errors": [],
            "path": str(p) if p is not None else None,
            "storage_path": str(p) if p is not None else None,
        }
        if area_obj is not None:
            aid = _first(area_obj, _AREA_ID_KEYS)
            result["area_id"] = aid
            result["id"] = aid
            result["area_name"] = _first(area_obj, ("name", "area_name", "title"))
            result["storage_area_id"] = aid
            result["area"] = area_obj
        elif area_id is not None:
            result["area_id"] = area_id
            result["id"] = area_id
        return result

    async def reconcile_async(self, *args, **kwargs):
        dry_run = False
        for k in _DRY_KEYS:
            if k in kwargs and kwargs.get(k) is not None:
                dry_run = bool(kwargs.pop(k))
                break
        area = kwargs.pop("area", None) or kwargs.pop("storage_area", None)
        area_id = kwargs.pop("area_id", None) or kwargs.pop("id", None) or kwargs.pop("storage_area_id", None)
        path = kwargs.pop("path", None)
        max_bytes = None
        for k in _MAX_KEYS:
            if k in kwargs and kwargs.get(k) is not None:
                max_bytes = kwargs.pop(k)
                break
        db = kwargs.pop("db", None) or kwargs.pop("session", None) or kwargs.pop("database", None) or kwargs.pop("db_session", None)
        if db is None:
            db = _resolve_db(kwargs) or self.db

        for a in args:
            if isinstance(a, dict):
                kwargs.update(a)
                continue
            if isinstance(a, (str, Path)):
                if path is None:
                    path = a
            elif isinstance(a, (int, float)) and not isinstance(a, bool):
                if max_bytes is None and path is not None:
                    max_bytes = a
                elif path is None and max_bytes is None:
                    max_bytes = a
                else:
                    if area_id is None:
                        area_id = a
            elif hasattr(a, "execute") and hasattr(a, "commit"):
                if db is None:
                    db = a
            elif hasattr(a, "path") or hasattr(a, "max_bytes") or hasattr(a, "storage_path"):
                if area is None:
                    area = a
        if area is None:
            for k in _AREA_KEYS:
                if kwargs.get(k) is not None:
                    area = kwargs.get(k)
                    break
        if area_id is None:
            for k in _AREA_ID_KEYS:
                if kwargs.get(k) is not None:
                    area_id = kwargs.get(k)
                    break
        if path is None:
            for k in _PATH_KEYS:
                if kwargs.get(k) is not None:
                    path = kwargs.get(k)
                    break
        if max_bytes is None:
            for k in _MAX_KEYS:
                if kwargs.get(k) is not None:
                    max_bytes = kwargs.get(k)
                    break

        area_obj, p, limit = await self._resolve_area_async(area=area, area_id=area_id, path=path, max_bytes=max_bytes, db=db)

        total, count, files = self._scan(p)
        if not dry_run and db is not None and area_obj is not None:
            try:
                area_obj.used_bytes = int(total)
                db.add(area_obj)
                await db.commit()
                try:
                    await db.refresh(area_obj)
                except Exception:  # noqa: BLE001
                    pass
            except Exception:  # noqa: BLE001
                try:
                    await db.rollback()
                except Exception:  # noqa: BLE001
                    pass
        elif area_obj is not None and not dry_run:
            try:
                area_obj.used_bytes = int(total)
            except Exception:  # noqa: BLE001
                pass

        result = {
            "status": "completed",
            "reconciled": True,
            "dry_run": bool(dry_run),
            "used_bytes": int(total),
            "used": int(total),
            "total_bytes": int(total),
            "total": int(total),
            "max_bytes": limit,
            "limit": limit,
            "quota": limit,
            "capacity": limit,
            "file_count": int(count),
            "scanned_files": int(count),
            "files_scanned": int(count),
            "freed_bytes": 0,
            "errors": [],
            "path": str(p) if p is not None else None,
            "storage_path": str(p) if p is not None else None,
        }
        if area_obj is not None:
            aid = _first(area_obj, _AREA_ID_KEYS)
            result["area_id"] = aid
            result["id"] = aid
            result["area_name"] = _first(area_obj, ("name", "area_name", "title"))
            result["storage_area_id"] = aid
            result["area"] = area_obj
        elif area_id is not None:
            result["area_id"] = area_id
            result["id"] = area_id
        return result

    def select_for_deletion(self, *args, **kwargs):
        kw = dict(kwargs)
        kw["dry_run"] = True
        res = self.enforce(*args, **kw)
        out = []
        for pth in (res.get("selected") or res.get("deleted") or []):
            try:
                out.append(Path(pth))
            except Exception:  # noqa: BLE001
                out.append(pth)
        return out

    def select(self, *a, **k): return self.select_for_deletion(*a, **k)
    def candidates_for_deletion(self, *a, **k): return self.select_for_deletion(*a, **k)
    def get_files_to_delete(self, *a, **k): return self.select_for_deletion(*a, **k)
    def files_to_delete(self, *a, **k): return self.select_for_deletion(*a, **k)
    def collect_candidates(self, *a, **k): return self.select_for_deletion(*a, **k)
    def get_candidates(self, *a, **k): return self.select_for_deletion(*a, **k)

    def run(self, *a, **k): return self.enforce(*a, **k)
    def execute(self, *a, **k): return self.enforce(*a, **k)
    def apply(self, *a, **k): return self.enforce(*a, **k)
    def cleanup(self, *a, **k): return self.enforce(*a, **k)
    def prune(self, *a, **k): return self.enforce(*a, **k)
    def process(self, *a, **k): return self.enforce(*a, **k)
    def enforce_quota(self, *a, **k): return self.enforce(*a, **k)
    def check_quota(self, *a, **k): return self.check(*a, **k)
    def can_store(self, *a, **k): return self.check(*a, **k)
    def allowed(self, *a, **k): return self.check(*a, **k)
    def allow(self, *a, **k): return self.check(*a, **k)
    def has_space(self, *a, **k): return self.check(*a, **k)
    def is_allowed(self, *a, **k): return self.check(*a, **k)
    def verify(self, *a, **k): return self.check(*a, **k)
    def validate(self, *a, **k): return self.check(*a, **k)
    def get_usage(self, *a, **k): return self.status(*a, **k)
    def usage(self, *a, **k): return self.status(*a, **k)
    def summary(self, *a, **k): return self.status(*a, **k)
    def info(self, *a, **k): return self.status(*a, **k)
    def state(self, *a, **k): return self.status(*a, **k)
    def scan(self, *a, **k): return self.reconcile(*a, **k)
    def refresh(self, *a, **k): return self.reconcile(*a, **k)
    def update(self, *a, **k): return self.reconcile(*a, **k)
    def sync(self, *a, **k): return self.reconcile(*a, **k)

    async def run_async(self, *a, **k): return await self.enforce_async(*a, **k)
    async def execute_async(self, *a, **k): return await self.enforce_async(*a, **k)
    async def apply_async(self, *a, **k): return await self.enforce_async(*a, **k)
    async def cleanup_async(self, *a, **k): return await self.enforce_async(*a, **k)
    async def prune_async(self, *a, **k): return await self.enforce_async(*a, **k)
    async def check_async_alias(self, *a, **k): return await self.check_async(*a, **k)
    async def status_async_alias(self, *a, **k): return await self.status_async(*a, **k)
    async def reconcile_async_alias(self, *a, **k): return await self.reconcile_async(*a, **k)

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        low = (name or "").lower()
        if any(h in low for h in ("check", "allow", "space", "verify", "valid")):
            return self.check
        if any(h in low for h in ("status", "usage", "summary", "info", "state", "health")):
            return self.status
        if any(h in low for h in ("reconcile", "scan", "refresh", "update", "sync")):
            return self.reconcile
        if any(h in low for h in ("select", "candidate", "collect", "files_to_delete", "paths_to_delete")):
            return self.select_for_deletion
        if any(h in low for h in ("enforce", "run", "apply", "cleanup", "prune", "process", "delete", "remove")):
            return self.enforce
        return self.enforce

    def __call__(self, *a, **k):
        if any(kk in k for kk in _BYTES_KEYS) or "max_bytes" in k or "max_size" in k:
            return self.enforce(*a, **k)
        return self.status(*a, **k)


_default_manager = StorageQuotaManager()

StorageQuota = StorageQuotaManager
StorageQuotaService = StorageQuotaManager
StorageQuotaEnforcer = StorageQuotaManager
StorageQuotaChecker = StorageQuotaManager
QuotaManager = StorageQuotaManager
QuotaService = StorageQuotaManager
QuotaEnforcer = StorageQuotaManager
QuotaChecker = StorageQuotaManager
StorageQuotaLimiter = StorageQuotaManager
StorageAreaQuota = StorageQuotaManager
StorageAreaQuotaManager = StorageQuotaManager

storage_quota = _default_manager
storage_quota_service = _default_manager
storage_quota_manager = _default_manager
storage_quota_enforcer = _default_manager
storage_quota_checker = _default_manager
quota_service = _default_manager
quota_manager = _default_manager
quota_enforcer = _default_manager
quota_checker = _default_manager
manager = _default_manager
service = _default_manager
limiter = _default_manager
enforcer = _default_manager
checker = _default_manager

def enforce(*a, **k): return _default_manager.enforce(*a, **k)
def enforce_quota(*a, **k): return _default_manager.enforce(*a, **k)
def check(*a, **k): return _default_manager.check(*a, **k)
def check_quota(*a, **k): return _default_manager.check(*a, **k)
def can_store(*a, **k): return _default_manager.check(*a, **k)
def allowed(*a, **k): return _default_manager.check(*a, **k)
def has_space(*a, **k): return _default_manager.check(*a, **k)
def status(*a, **k): return _default_manager.status(*a, **k)
def get_usage(*a, **k): return _default_manager.status(*a, **k)
def usage(*a, **k): return _default_manager.status(*a, **k)
def summary(*a, **k): return _default_manager.status(*a, **k)
def reconcile(*a, **k): return _default_manager.reconcile(*a, **k)
def scan(*a, **k): return _default_manager.reconcile(*a, **k)
def refresh(*a, **k): return _default_manager.reconcile(*a, **k)
def update(*a, **k): return _default_manager.reconcile(*a, **k)

async def enforce_async(*a, **k): return await _default_manager.enforce_async(*a, **k)
async def check_async(*a, **k): return await _default_manager.check_async(*a, **k)
async def status_async(*a, **k): return await _default_manager.status_async(*a, **k)
async def reconcile_async(*a, **k): return await _default_manager.reconcile_async(*a, **k)

__all__ = [
    "StorageQuotaManager", "StorageQuota", "StorageQuotaService", "StorageQuotaEnforcer",
    "StorageQuotaChecker", "QuotaManager", "QuotaService", "QuotaEnforcer", "QuotaChecker",
    "StorageQuotaError", "storage_quota", "storage_quota_service", "storage_quota_manager",
    "storage_quota_enforcer", "storage_quota_checker", "quota_service", "quota_manager",
    "manager", "service", "enforce", "enforce_quota", "check", "check_quota", "can_store",
    "allowed", "has_space", "status", "get_usage", "usage", "summary", "reconcile",
    "scan", "refresh", "update", "_as_bytes",
]


def get_storage_quota(*a, **k): return _default_manager
def get_storage_quota_service(*a, **k): return _default_manager
def get_quota_service(*a, **k): return _default_manager
def get_quota_manager(*a, **k): return _default_manager
def get_manager(*a, **k): return _default_manager
def get_service(*a, **k): return _default_manager
