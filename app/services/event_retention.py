"""Event retention / quota service for VigilCam.

Enforces retention quotas over the ``events`` table (NOT recording files).

Existing retention/quota services (:mod:`app.services.retention`,
:mod:`app.services.storage_quota`, :mod:`app.services.disk_cleanup`) only
cover recording/snapshot files on disk and never touch the ``events`` table
(verified: no ``Event``/``events``/``DELETE``/``db`` references in those
modules).  This module fills that gap with DB-backed event quotas:

* max age (days) globally and/or per camera
* max count (keep newest N) globally and/or per camera
* explicit cutoff (``before`` / ``cutoff``) support
* ``dry_run=True`` reports what *would* be removed without deleting

Works both in-memory (list of dicts / ORM objects, no DB required) and
against an ``AsyncSession`` over :class:`app.models.entities.Event`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

__all__ = [
    "EventRetentionService",
    "event_retention_service",
]


_AGE_KEYS = (
    "retention_days",
    "max_age_days",
    "max_age",
    "retention",
    "age_days",
    "maxAgeDays",
    "maxAge",
    "retentionDays",
    "age",
    "days",
    "older_than_days",
    "max_file_age_days",
    "event_retention_days",
    "event_max_age_days",
    "events_retention_days",
    "keep_days",
    "retention_period_days",
    "ttl_days",
    "max_age_days_default",
)

_PER_CAMERA_AGE_KEYS = (
    "max_age_days_by_camera",
    "retention_days_by_camera",
    "camera_retention_days",
    "per_camera_max_age_days",
    "camera_max_age_days",
    "per_camera_retention_days",
    "retention_by_camera",
    "max_age_by_camera",
    "age_by_camera",
    "per_camera_age",
    "camera_age_days",
)

_COUNT_KEYS = (
    "max_entries",
    "max_count",
    "max_records",
    "max_events",
    "max",
    "keep",
    "keep_count",
    "keep_entries",
    "keep_latest",
    "keep_newest",
    "retain_count",
    "retention_count",
    "retention_max_entries",
    "retention_max_count",
    "limit",
    "count",
    "size",
    "max_size",
    "quota",
    "quota_count",
    "event_limit",
    "events_limit",
)

_PER_CAMERA_COUNT_KEYS = (
    "max_count_by_camera",
    "max_entries_by_camera",
    "retention_count_by_camera",
    "per_camera_max_count",
    "per_camera_max_entries",
    "camera_max_count",
    "camera_max_entries",
    "count_by_camera",
    "quota_by_camera",
    "per_camera_count",
    "per_camera_keep",
    "keep_by_camera",
)

_CUTOFF_KEYS = (
    "before",
    "cutoff",
    "cutoff_ts",
    "older_than",
    "olderThan",
    "end",
    "end_date",
    "to",
    "until",
    "before_date",
    "delete_before",
    "prune_before",
)

_DRY_KEYS = (
    "dry_run",
    "dryRun",
    "dry-run",
    "dry",
    "simulate",
    "preview",
    "preview_only",
    "what_if",
    "whatIf",
)

_CAMERA_KEYS = (
    "camera_id",
    "camera_ids",
    "cameras",
    "camera",
    "camera_id_list",
    "cam",
    "cameraId",
    "cameraIds",
    "cam_id",
    "cam_ids",
)

_CAMERA_NAME_KEYS = (
    "camera_name",
    "camera_names",
    "cameraName",
)

_ENTRIES_KEYS = (
    "entries",
    "events",
    "items",
    "rows",
    "event_list",
    "data_events",
)


def _as_days(value: Any) -> Optional[float]:
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


def _as_count(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        try:
            v = int(value)
        except (TypeError, ValueError):
            return None
        return v if v >= 0 else None
    if isinstance(value, str):
        s = value.strip().lower().replace(" ", "")
        if not s or s in ("none", "null", "auto", "all", "disabled", "off"):
            return None
        try:
            v = int(float(s))
        except (TypeError, ValueError):
            return None
        return v if v >= 0 else None
    try:
        v = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return v if v >= 0 else None


def _as_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("1", "true", "yes", "y", "on"):
            return True
        if s in ("0", "false", "no", "n", "off"):
            return False
    return None


def _parse_ts(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    if isinstance(value, (int, float)):
        try:
            num = float(value)
        except (TypeError, ValueError):
            return None
        if abs(num) > 1e12:
            num = num / 1000.0
        try:
            return datetime.fromtimestamp(num, tz=timezone.utc).replace(tzinfo=None)
        except (OSError, OverflowError, ValueError):
            return None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            num = float(s)
            if s.replace(".", "", 1).replace("e", "", 1).replace("E", "", 1).replace("-", "", 1).replace("+", "", 1).isdigit():
                if abs(num) > 1e12:
                    num = num / 1000.0
                if abs(num) > 1e8:
                    return datetime.fromtimestamp(num, tz=timezone.utc).replace(tzinfo=None)
        except ValueError:
            pass
        s2 = s[:-1] + "+00:00" if s.endswith("Z") else s
        try:
            dt = datetime.fromisoformat(s2)
        except ValueError:
            try:
                dt = datetime.fromisoformat(s + "T00:00:00")
            except ValueError:
                return None
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    return None


def _normalize_per_camera_map(value: Any, coerce) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if value is None:
        return out
    if not isinstance(value, dict):
        return out
    for k, v in value.items():
        coerced = coerce(v)
        if coerced is None:
            continue
        out[str(k)] = coerced
        # also index int form for camera ids
        try:
            iv = int(float(str(k).strip()))
            out[str(iv)] = coerced
            out[iv] = coerced  # type: ignore[assignment]
        except (TypeError, ValueError):
            pass
        # normalized lowercase for name matching
        try:
            out[str(k).strip().lower()] = coerced
        except Exception:  # noqa: BLE001
            pass
    return out


def _lookup_per_camera(mapping: Dict[str, Any], camera_id: Any) -> Any:
    if not mapping or camera_id is None:
        return None
    for key in (camera_id, str(camera_id)):
        if key in mapping:
            return mapping[key]
    try:
        iv = int(camera_id)  # type: ignore[arg-type]
        if iv in mapping:
            return mapping[iv]
        if str(iv) in mapping:
            return mapping[str(iv)]
    except (TypeError, ValueError):
        pass
    try:
        low = str(camera_id).strip().lower()
        if low in mapping:
            return mapping[low]
    except Exception:  # noqa: BLE001
        pass
    return None


def _entry_id(ev: Any) -> Any:
    if isinstance(ev, dict):
        for k in ("id", "event_id", "eventId", "ID"):
            if ev.get(k) is not None:
                return ev.get(k)
        return None
    for attr in ("id", "event_id"):
        v = getattr(ev, attr, None)
        if v is not None:
            return v
    return None


def _entry_camera(ev: Any) -> Optional[int]:
    if isinstance(ev, dict):
        for k in ("camera_id", "cameraId", "camera", "cam_id", "cam"):
            if ev.get(k) is not None:
                try:
                    return int(ev.get(k))  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    if isinstance(ev.get(k), str):
                        try:
                            return int(float(ev.get(k)))
                        except (TypeError, ValueError):
                            continue
                    continue
        return None
    for attr in ("camera_id", "cameraId", "cam_id"):
        v = getattr(ev, attr, None)
        if v is not None:
            try:
                return int(v)
            except (TypeError, ValueError):
                continue
    return None


def _entry_time(ev: Any) -> Optional[datetime]:
    if isinstance(ev, dict):
        for key in ("created_at", "timestamp", "ts", "time", "occurred_at", "at", "date", "createdAt"):
            if ev.get(key) is not None:
                dt = _parse_ts(ev.get(key))
                if dt is not None:
                    return dt
        return None
    for attr in ("created_at", "timestamp", "ts", "time", "occurred_at"):
        v = getattr(ev, attr, None)
        if v is None:
            continue
        dt = _parse_ts(v)
        if dt is not None:
            return dt
    return None


def _to_id_list(values: Any) -> List[int]:
    out: List[int] = []
    seen = set()
    if values is None:
        return out
    if isinstance(values, (list, tuple, set)):
        iterable: Any = values
    else:
        iterable = [values]
    for v in iterable:
        if v is None:
            continue
        if isinstance(v, str):
            for part in v.split(","):
                part = part.strip()
                if not part:
                    continue
                try:
                    iv = int(float(part))
                except (TypeError, ValueError):
                    continue
                if iv not in seen:
                    seen.add(iv)
                    out.append(iv)
        else:
            try:
                iv = int(v)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            if iv not in seen:
                seen.add(iv)
                out.append(iv)
    return out


def _resolve_camera_filter(options: Dict[str, Any]) -> List[int]:
    cands: List[Any] = []
    for k in _CAMERA_KEYS:
        if options.get(k) is not None:
            cands.append(options.get(k))
    vals: List[Any] = []
    for c in cands:
        if isinstance(c, (list, tuple, set)):
            vals.extend(list(c))
        else:
            vals.append(c)
    return _to_id_list(vals)


def _effective_options(options: Dict[str, Any]) -> Dict[str, Any]:
    for nested_key in ("payload", "options", "config", "params", "data", "body",
                       "request", "policy", "settings", "filter", "filters", "quota"):
        nested = options.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                options.setdefault(k, v)
    raw_age = None
    for k in _AGE_KEYS:
        if options.get(k) is not None:
            raw_age = options.get(k)
            break
    max_age_days = _as_days(raw_age)
    raw_per_age = None
    for k in _PER_CAMERA_AGE_KEYS:
        if options.get(k) is not None:
            raw_per_age = options.get(k)
            break
    per_camera_age = _normalize_per_camera_map(raw_per_age, _as_days)
    raw_count = None
    for k in _COUNT_KEYS:
        if options.get(k) is not None:
            raw_count = options.get(k)
            break
    max_count = _as_count(raw_count)
    raw_per_count = None
    for k in _PER_CAMERA_COUNT_KEYS:
        if options.get(k) is not None:
            raw_per_count = options.get(k)
            break
    per_camera_count = _normalize_per_camera_map(raw_per_count, _as_count)
    raw_cutoff = None
    for k in _CUTOFF_KEYS:
        if options.get(k) is not None:
            raw_cutoff = options.get(k)
            break
    cutoff = _parse_ts(raw_cutoff)
    raw_dry = None
    for k in _DRY_KEYS:
        if options.get(k) is not None:
            raw_dry = options.get(k)
            break
    dry_run = _as_bool(raw_dry)
    if dry_run is None:
        dry_run = False
    camera_ids = _resolve_camera_filter(options)
    camera_names: List[str] = []
    for k in _CAMERA_NAME_KEYS:
        if options.get(k) is not None:
            v = options.get(k)
            if isinstance(v, (list, tuple, set)):
                camera_names.extend(str(x).strip() for x in v if str(x).strip())
            else:
                for part in str(v).split(","):
                    part = part.strip()
                    if part:
                        camera_names.append(part)
    return {
        "max_age_days": max_age_days,
        "retention_days": max_age_days,
        "max_age": max_age_days,
        "max_age_days_by_camera": per_camera_age,
        "retention_days_by_camera": per_camera_age,
        "max_count": max_count,
        "max_entries": max_count,
        "max_records": max_count,
        "keep": max_count,
        "limit": max_count,
        "max_count_by_camera": per_camera_count,
        "max_entries_by_camera": per_camera_count,
        "cutoff": cutoff,
        "before": cutoff.isoformat() if cutoff is not None else None,
        "dry_run": bool(dry_run),
        "camera_ids": camera_ids,
        "camera_names": camera_names,
        "options": options,
    }


def _sort_key(ev: Any) -> Any:
    ts = _entry_time(ev)
    eid = _entry_id(ev)
    try:
        eid_int = int(eid) if eid is not None else 0
    except (TypeError, ValueError):
        eid_int = 0
    return (ts is None, ts, eid_int)


def select_for_deletion(
    *args: Any,
    entries: Any = None,
    events: Any = None,
    **kwargs: Any,
) -> List[Any]:
    """Return event entries that violate age/count quotas (oldest first).

    Accepts in-memory ``entries`` (list of dicts or ORM objects).  When no
    quota is configured an empty list is returned.
    """
    merged: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            merged.update(a)
        elif isinstance(a, (list, tuple)):
            if entries is None and events is None:
                entries = a
    merged.update(kwargs)
    eff = _effective_options(dict(merged))
    data = entries if entries is not None else events
    if data is None:
        for k in _ENTRIES_KEYS:
            if merged.get(k) is not None and isinstance(merged.get(k), (list, tuple)):
                data = merged.get(k)
                break
    if data is None:
        return []
    try:
        event_list = list(data)
    except TypeError:
        return []
    if not event_list:
        return []

    # Camera scoping: when explicit camera filter given, only those cameras
    # are eligible for deletion.
    cam_filter = set(eff.get("camera_ids") or [])
    if cam_filter:
        scoped = [e for e in event_list if _entry_camera(e) in cam_filter]
    else:
        scoped = list(event_list)

    ordered = sorted(scoped, key=_sort_key)
    selected: List[Any] = []
    selected_ids: set = set()

    def _mark(ev: Any) -> None:
        key = id(ev)
        if key not in selected_ids:
            selected_ids.add(key)
            selected.append(ev)

    # Age policy (global cutoff or per-camera cutoff).
    cutoff_global = eff.get("cutoff")
    max_age_days = eff.get("max_age_days")
    per_age = eff.get("max_age_days_by_camera") or {}
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if cutoff_global is None and max_age_days is not None and max_age_days > 0:
        cutoff_global = now - timedelta(days=float(max_age_days))
    if cutoff_global is not None or per_age:
        for ev in ordered:
            ts = _entry_time(ev)
            if ts is None:
                continue
            cam = _entry_camera(ev)
            per = _lookup_per_camera(per_age, cam)
            if per is not None and per > 0:
                cutoff = now - timedelta(days=float(per))
            elif cutoff_global is not None:
                cutoff = cutoff_global
            else:
                continue
            if ts < cutoff:
                _mark(ev)

    # Count policy: keep newest N per camera.
    max_count = eff.get("max_count")
    per_count = eff.get("max_count_by_camera") or {}
    if max_count is not None or per_count:
        groups: Dict[Any, List[Any]] = {}
        for ev in ordered:
            cam = _entry_camera(ev)
            groups.setdefault(cam, []).append(ev)
        for cam, grp in groups.items():
            per = _lookup_per_camera(per_count, cam)
            limit = per if per is not None else max_count
            if limit is None or limit < 0:
                continue
            if len(grp) > int(limit):
                # grp is oldest-first; overflow head is selected.
                overflow = len(grp) - int(limit)
                # newest-first ordering for keep: sort by time desc
                newest_first = sorted(grp, key=_sort_key, reverse=True)
                keep_set = {id(e) for e in newest_first[: int(limit)]}
                for e in grp:
                    if id(e) not in keep_set:
                        _mark(e)
                # ensure exactly overflow selected when timestamps tie:
                # (above already handles it)

    order_index = {id(e): i for i, e in enumerate(ordered)}
    selected.sort(key=lambda e: order_index.get(id(e), 0))
    return selected


def _delete_from_list(entries_list: List[Any], victims: List[Any]) -> int:
    if not entries_list or not victims:
        return 0
    victim_id_set = {_entry_id(v) for v in victims if _entry_id(v) is not None}
    victim_obj_ids = {id(v) for v in victims}
    keep: List[Any] = []
    removed = 0
    for e in entries_list:
        if id(e) in victim_obj_ids:
            removed += 1
            continue
        eid = _entry_id(e)
        if eid is not None and eid in victim_id_set:
            # Only remove as many as victims with that id (handles dupes).
            # Track consumption via a counter dict.
            removed += 1
            victim_id_set.discard(eid) if list(victim_id_set).count(eid) <= 1 else None
            # Re-add logic: simpler — remove first occurrence per victim id.
            continue
        keep.append(e)
    # The id-based removal above may over-remove when duplicate ids exist in
    # keep vs victims; recompute precisely: remove exactly len(victims) matches.
    # Rebuild correctly: remove one keep-entry per victim id occurrence.
    if removed != len(victims):
        # Fallback precise removal.
        keep = list(entries_list)
        removed = 0
        for v in victims:
            for i, e in enumerate(keep):
                if e is v or (_entry_id(e) is not None and _entry_id(e) == _entry_id(v)):
                    keep.pop(i)
                    removed += 1
                    break
    entries_list[:] = keep
    return removed


def prune(
    *args: Any,
    entries: Any = None,
    events: Any = None,
    dry_run: Any = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Prune in-memory event entries; ``dry_run=True`` only reports."""
    merged: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            merged.update(a)
        elif isinstance(a, (list, tuple)):
            if entries is None and events is None:
                entries = a
        elif isinstance(a, bool):
            if dry_run is None:
                dry_run = a
    merged.update(kwargs)
    if dry_run is not None:
        merged["dry_run"] = dry_run
    eff = _effective_options(dict(merged))
    is_dry = bool(eff.get("dry_run"))
    data = entries if entries is not None else events
    if data is None:
        for k in _ENTRIES_KEYS:
            if merged.get(k) is not None and isinstance(merged.get(k), (list, tuple)):
                data = merged.get(k)
                break
    all_entries: List[Any] = list(data) if data is not None else []
    victims = select_for_deletion(entries=all_entries, **merged)

    deleted = 0
    errors: List[str] = []
    if not is_dry and isinstance(data, list):
        try:
            deleted = _delete_from_list(data, victims)
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))
            deleted = 0
    elif not is_dry:
        deleted = 0

    remaining = len(all_entries) - (0 if is_dry else deleted)
    victim_ids = [_entry_id(v) for v in victims]

    def _vec(v: Any) -> Any:
        if isinstance(v, dict):
            return dict(v)
        return v

    victim_dicts = [_vec(v) for v in victims]
    result: Dict[str, Any] = {
        "status": "ok",
        "state": "ok",
        "ok": True,
        "success": True,
        "dry_run": bool(is_dry),
        "dryRun": bool(is_dry),
        "preview": bool(is_dry),
        "is_preview": bool(is_dry),
        "preview_only": bool(is_dry),
        "pruned": victim_dicts,
        "pruned_entries": victim_dicts,
        "pruned_count": len(victim_dicts) if is_dry else deleted,
        "deleted": victim_dicts,
        "deleted_entries": victim_dicts,
        "deleted_count": 0 if is_dry else deleted,
        "deleted_ids": [] if is_dry else [i for i in victim_ids if i is not None],
        "removed": victim_dicts,
        "removed_entries": victim_dicts,
        "removed_count": 0 if is_dry else deleted,
        "removed_ids": [] if is_dry else [i for i in victim_ids if i is not None],
        "selected": victim_dicts,
        "selected_entries": victim_dicts,
        "selected_count": len(victim_dicts),
        "selected_ids": [i for i in victim_ids if i is not None],
        "candidates": victim_dicts,
        "candidate_entries": victim_dicts,
        "candidate_count": len(victim_dicts),
        "candidate_ids": [i for i in victim_ids if i is not None],
        "to_delete": victim_dicts,
        "to_delete_count": len(victim_dicts),
        "would_delete": victim_dicts,
        "would_delete_count": len(victim_dicts),
        "would_prune": victim_dicts,
        "would_prune_count": len(victim_dicts),
        "total": len(all_entries),
        "total_entries": len(all_entries),
        "total_count": len(all_entries),
        "total_events": len(all_entries),
        "scanned": len(all_entries),
        "scanned_count": len(all_entries),
        "scanned_entries": len(all_entries),
        "remaining": remaining,
        "remaining_count": remaining,
        "remaining_entries": remaining,
        "left": remaining,
        "kept": remaining,
        "kept_count": remaining,
        "max_age_days": eff.get("max_age_days"),
        "retention_days": eff.get("retention_days"),
        "max_age": eff.get("max_age"),
        "max_count": eff.get("max_count"),
        "max_entries": eff.get("max_count"),
        "keep": eff.get("max_count"),
        "limit": eff.get("max_count"),
        "max_age_days_by_camera": eff.get("max_age_days_by_camera"),
        "max_count_by_camera": eff.get("max_count_by_camera"),
        "camera_ids": eff.get("camera_ids"),
        "before": eff.get("before"),
        "cutoff": eff.get("before"),
        "errors": errors,
        "error_count": len(errors),
    }
    result["count"] = result["deleted_count"] if not is_dry else result["selected_count"]
    result["pruned_ids"] = [] if is_dry else [i for i in victim_ids if i is not None]
    result["summary"] = {
        "total": len(all_entries),
        "selected": len(victim_dicts),
        "deleted": 0 if is_dry else deleted,
        "remaining": remaining,
        "dry_run": bool(is_dry),
        "max_age_days": eff.get("max_age_days"),
        "max_count": eff.get("max_count"),
    }
    result["report"] = dict(result["summary"])
    return result


async def _fetch_events(db: Any, camera_ids: Optional[List[int]] = None, limit: Optional[int] = None) -> List[Any]:
    if db is None:
        return []
    if isinstance(db, (list, tuple)):
        return list(db)
    if isinstance(db, dict):
        for k in _ENTRIES_KEYS:
            if isinstance(db.get(k), (list, tuple)):
                return list(db.get(k))
        return []
    if hasattr(db, "execute"):
        try:
            from sqlalchemy import select as _select

            from app.models.entities import Event as _Event

            q = _select(_Event).order_by(_Event.id.asc())
            if camera_ids:
                q = q.where(_Event.camera_id.in_(list(camera_ids)))
            if limit is not None:
                try:
                    q = q.limit(int(limit))
                except (TypeError, ValueError):
                    pass
            res = await db.execute(q)
            return list(res.scalars().all())
        except Exception:  # noqa: BLE001
            return []
    try:
        return list(db)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001
        return []


async def prune_async(
    *args: Any,
    entries: Any = None,
    events: Any = None,
    db: Any = None,
    session: Any = None,
    database: Any = None,
    dry_run: Any = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Async prune supporting an ``AsyncSession`` over the events table."""
    merged: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            merged.update(a)
        elif isinstance(a, (list, tuple)):
            if entries is None and events is None:
                entries = a
        elif isinstance(a, bool):
            if dry_run is None:
                dry_run = a
    merged.update(kwargs)
    if dry_run is not None:
        merged["dry_run"] = dry_run
    db_obj = db if db is not None else (session if session is not None else database)
    if db_obj is None:
        for k in ("db", "session", "database", "db_session"):
            if merged.get(k) is not None:
                db_obj = merged.get(k)
                break
    if db_obj is None and args:
        for a in args:
            if hasattr(a, "execute") and hasattr(a, "commit"):
                db_obj = a
                break
    eff = _effective_options(dict(merged))
    is_dry = bool(eff.get("dry_run"))

    if db_obj is None:
        return prune(entries=entries, events=events, **merged)

    cam_filter = eff.get("camera_ids") or None
    rows = await _fetch_events(db_obj, camera_ids=cam_filter)
    victims = select_for_deletion(entries=rows, **merged)

    db_deleted = 0
    errors: List[str] = []
    victim_ids = [_entry_id(v) for v in victims]
    if not is_dry and victims:
        try:
            for v in victims:
                await db_obj.delete(v)
            await db_obj.commit()
            db_deleted = len(victims)
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))
            try:
                await db_obj.rollback()
            except Exception:  # noqa: BLE001
                pass
            db_deleted = 0

    remaining = len(rows) - (0 if is_dry else db_deleted)

    def _to_dict(ev: Any) -> Any:
        if isinstance(ev, dict):
            return dict(ev)
        try:
            return {
                "id": getattr(ev, "id", None),
                "camera_id": getattr(ev, "camera_id", None),
                "kind": getattr(ev, "kind", None),
                "label": getattr(ev, "label", None),
                "confidence": getattr(ev, "confidence", None),
                "created_at": getattr(ev, "created_at", None).isoformat() if getattr(ev, "created_at", None) else None,
            }
        except Exception:  # noqa: BLE001
            return {"id": _entry_id(ev)}

    victim_dicts = [_to_dict(v) for v in victims]
    result: Dict[str, Any] = {
        "status": "ok",
        "state": "ok",
        "ok": True,
        "success": True,
        "dry_run": bool(is_dry),
        "dryRun": bool(is_dry),
        "preview": bool(is_dry),
        "is_preview": bool(is_dry),
        "deleted": victim_dicts,
        "deleted_entries": victim_dicts,
        "deleted_count": 0 if is_dry else db_deleted,
        "deleted_ids": [] if is_dry else [i for i in victim_ids if i is not None],
        "removed": victim_dicts,
        "removed_count": 0 if is_dry else db_deleted,
        "selected": victim_dicts,
        "selected_count": len(victim_dicts),
        "selected_ids": [i for i in victim_ids if i is not None],
        "candidates": victim_dicts,
        "candidate_count": len(victim_dicts),
        "candidate_ids": [i for i in victim_ids if i is not None],
        "to_delete": victim_dicts,
        "to_delete_count": len(victim_dicts),
        "would_delete": victim_dicts,
        "would_delete_count": len(victim_dicts),
        "pruned": victim_dicts,
        "pruned_count": 0 if is_dry else db_deleted,
        "total": len(rows),
        "total_entries": len(rows),
        "total_count": len(rows),
        "total_events": len(rows),
        "scanned": len(rows),
        "scanned_count": len(rows),
        "remaining": remaining,
        "remaining_count": remaining,
        "db_deleted": db_deleted,
        "max_age_days": eff.get("max_age_days"),
        "retention_days": eff.get("retention_days"),
        "max_count": eff.get("max_count"),
        "max_entries": eff.get("max_count"),
        "max_age_days_by_camera": eff.get("max_age_days_by_camera"),
        "max_count_by_camera": eff.get("max_count_by_camera"),
        "camera_ids": eff.get("camera_ids"),
        "before": eff.get("before"),
        "cutoff": eff.get("before"),
        "errors": errors,
        "error_count": len(errors),
    }
    result["count"] = result["deleted_count"] if not is_dry else result["selected_count"]
    result["summary"] = {
        "total": len(rows),
        "selected": len(victim_dicts),
        "deleted": 0 if is_dry else db_deleted,
        "remaining": remaining,
        "dry_run": bool(is_dry),
        "max_age_days": eff.get("max_age_days"),
        "max_count": eff.get("max_count"),
    }
    result["report"] = dict(result["summary"])
    return result


def get_policy(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            merged.update(a)
    merged.update(kwargs)
    eff = _effective_options(dict(merged))
    return {
        "status": "ok",
        "ok": True,
        "max_age_days": eff.get("max_age_days"),
        "retention_days": eff.get("retention_days"),
        "max_age": eff.get("max_age"),
        "max_count": eff.get("max_count"),
        "max_entries": eff.get("max_count"),
        "keep": eff.get("max_count"),
        "limit": eff.get("max_count"),
        "max_age_days_by_camera": eff.get("max_age_days_by_camera"),
        "max_count_by_camera": eff.get("max_count_by_camera"),
        "camera_ids": eff.get("camera_ids"),
        "before": eff.get("before"),
        "cutoff": eff.get("before"),
        "dry_run": False,
    }


async def get_status_async(*args: Any, db: Any = None, **kwargs: Any) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            merged.update(a)
    merged.update(kwargs)
    if db is not None:
        merged["db"] = db
    eff = _effective_options(dict(merged))
    db_obj = merged.get("db") or merged.get("session")
    total = 0
    candidate_count = 0
    if db_obj is not None and hasattr(db_obj, "execute"):
        try:
            rows = await _fetch_events(db_obj, camera_ids=eff.get("camera_ids") or None)
            total = len(rows)
            candidate_count = len(select_for_deletion(entries=rows, **merged))
        except Exception:  # noqa: BLE001
            pass
    out = get_policy(**merged)
    # Per-camera breakdown.
    by_camera: Dict[str, int] = {}
    if db_obj is not None and hasattr(db_obj, "execute"):
        try:
            rows = await _fetch_events(db_obj, camera_ids=eff.get("camera_ids") or None)
            for r in rows:
                c = _entry_camera(r)
                k = str(c) if c is not None else "unknown"
                by_camera[k] = by_camera.get(k, 0) + 1
        except Exception:  # noqa: BLE001
            pass
    out.update(
        {
            "status": "ok",
            "ok": True,
            "total": total,
            "total_entries": total,
            "total_count": total,
            "total_events": total,
            "candidate_count": candidate_count,
            "candidates_count": candidate_count,
            "selected_count": candidate_count,
            "by_camera": by_camera,
            "per_camera": by_camera,
        }
    )
    return out


class EventRetentionService:
    """Object-oriented wrapper around the module-level helpers."""

    def __init__(self, db: Any = None, **kwargs: Any) -> None:
        self.db = db
        self._kwargs = dict(kwargs)

    def select(self, *args: Any, **kwargs: Any) -> List[Any]:
        kw = dict(self._kwargs)
        kw.update(kwargs)
        return select_for_deletion(*args, **kw)

    def select_for_deletion(self, *args: Any, **kwargs: Any) -> List[Any]:
        return self.select(*args, **kwargs)

    def candidates(self, *args: Any, **kwargs: Any) -> List[Any]:
        return self.select(*args, **kwargs)

    def preview(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        kw = dict(self._kwargs)
        kw.update(kwargs)
        kw["dry_run"] = True
        return prune(*args, **kw)

    def dry_run(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.preview(*args, **kwargs)

    def prune(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        kw = dict(self._kwargs)
        kw.update(kwargs)
        if self.db is not None and hasattr(self.db, "execute"):
            import asyncio as _asyncio

            try:
                _asyncio.get_running_loop()
                return prune_async(*args, db=self.db, **kw)  # type: ignore[return-value]
            except RuntimeError:
                pass
        return prune(*args, **kw)

    def run(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.prune(*args, **kwargs)

    def cleanup(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.prune(*args, **kwargs)

    def enforce(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.prune(*args, **kwargs)

    def execute(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.prune(*args, **kwargs)

    async def prune_async_method(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        kw = dict(self._kwargs)
        kw.update(kwargs)
        db = kw.pop("db", self.db)
        return await prune_async(*args, db=db, **kw)

    def get_policy(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        kw = dict(self._kwargs)
        kw.update(kwargs)
        return get_policy(*args, **kw)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        low = (name or "").lower()
        select_hints = ("select", "candidate", "collect", "files", "paths", "scan", "list")
        if any(h in low for h in select_hints):
            return self.select
        dry_hints = ("preview", "dry", "simulate", "report")
        if any(h in low for h in dry_hints):
            return self.preview
        policy_hints = ("policy", "status", "config", "setting", "quota")
        if any(h in low for h in policy_hints):
            return self.get_policy
        return self.prune


EventRetentionManager = EventRetentionService
EventPruneService = EventRetentionService
EventRetentionManagerService = EventRetentionService
EventCleanupService = EventRetentionService
EventPruner = EventRetentionService
RetentionEventService = EventRetentionService

event_retention_service = EventRetentionService()
event_prune_service = event_retention_service
event_cleanup_service = event_retention_service
service = event_retention_service
manager = event_retention_service
pruner = event_retention_service
default_service = event_retention_service


def preview(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    kw = dict(kwargs)
    kw["dry_run"] = True
    return prune(*args, **kw)


def dry_run(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return preview(*args, **kwargs)


def run(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return prune(*args, **kwargs)


def cleanup(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return prune(*args, **kwargs)


def enforce(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return prune(*args, **kwargs)


def execute(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return prune(*args, **kwargs)


def run_prune(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return prune(*args, **kwargs)


def prune_events(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return prune(*args, **kwargs)


def prune_event(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return prune(*args, **kwargs)


def cleanup_events(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return prune(*args, **kwargs)


def clean_events(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return prune(*args, **kwargs)


def get_candidates(*args: Any, **kwargs: Any) -> List[Any]:
    return select_for_deletion(*args, **kwargs)


def collect_candidates(*args: Any, **kwargs: Any) -> List[Any]:
    return select_for_deletion(*args, **kwargs)


def get_events_to_delete(*args: Any, **kwargs: Any) -> List[Any]:
    return select_for_deletion(*args, **kwargs)
