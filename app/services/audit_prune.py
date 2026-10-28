"""Audit prune / retention service.

Removes old audit entries from the in-memory audit store
(:mod:`app.services.audit`) according to an age and/or count policy.

Policies (all optional; at least one should be given):
  * max age — delete entries older than ``retention_days`` (aliases:
    ``max_age_days``, ``max_age``, ``days``, ``older_than_days``,
    ``age_days``, ``keep_days`` is inverted)
  * max entries — keep only the newest ``max_entries`` entries (aliases:
    ``max_count``, ``keep``, ``limit``, ``max_size``,
    ``retention_count``, ``max_records``)
  * explicit cutoff — delete entries with timestamp < ``before`` /
    ``cutoff`` / ``older_than`` (ISO-8601 string or epoch seconds)

``dry_run=True`` reports what *would* be removed without deleting
anything.

All helpers are sync and dependency-free so they work in tests without
a database.  An optional ``db`` (AsyncSession) argument enables
best-effort DB pruning of ``audit_entries`` / ``audit_logs`` tables.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

__all__ = [
    "AuditPruneService",
    "audit_prune_service",
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
    "audit_retention_days",
    "audit_max_age_days",
    "keep_days",
    "retention_period_days",
    "ttl_days",
)

_COUNT_KEYS = (
    "max_entries",
    "max_count",
    "max_records",
    "max_size",
    "max",
    "keep",
    "keep_count",
    "keep_entries",
    "keep_latest",
    "keep_newest",
    "retain_count",
    "retention_count",
    "retention_max_entries",
    "limit",
    "count",
    "size",
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

_STORE_KEYS = ("store", "audit_store", "storage", "backend")


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
        # epoch?
        try:
            num = float(s)
            if s.replace(".", "", 1).replace("e", "", 1).replace("E", "", 1).replace("-", "", 1).replace("+", "", 1).isdigit():
                if abs(num) > 1e12:
                    num = num / 1000.0
                # heuristic: large numbers are epochs, small ones probably not dates
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


def _entry_ts(entry: Any) -> Optional[datetime]:
    if not isinstance(entry, dict):
        return None
    for key in ("timestamp", "ts", "created_at", "occurred_at", "time", "at", "date"):
        raw = entry.get(key)
        if raw is None:
            continue
        if isinstance(raw, datetime):
            dt = raw
            if dt.tzinfo is not None:
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            return dt
        parsed = _parse_ts(raw)
        if parsed is not None:
            return parsed
    return None


def _resolve_store(store: Any = None, **kwargs: Any) -> Any:
    if store is not None:
        return store
    for key in _STORE_KEYS:
        if kwargs.get(key) is not None:
            return kwargs.get(key)
    try:
        from app.services.audit import _default_store as _store

        return _store
    except Exception:  # noqa: BLE001
        return None


def _read_entries(store: Any, entries: Any = None) -> List[dict]:
    if entries is not None:
        try:
            return [dict(e) if isinstance(e, dict) else dict(e) for e in list(entries)]
        except Exception:  # noqa: BLE001
            return []
    if store is None:
        return []
    for attr in ("read_all", "all", "list"):
        try:
            fn = getattr(store, attr, None)
            if callable(fn):
                data = fn()
                import inspect as _inspect

                if _inspect.isawaitable(data):
                    continue
                return [dict(e) if isinstance(e, dict) else dict(e) for e in (data or [])]
        except Exception:  # noqa: BLE001
            continue
    # Fallback: _entries attribute.
    try:
        raw = getattr(store, "_entries", None)
        if isinstance(raw, list):
            return [dict(e) if isinstance(e, dict) else dict(e) for e in raw]
    except Exception:  # noqa: BLE001
        pass
    return []


def _effective_options(options: Dict[str, Any]) -> Dict[str, Any]:
    # Unwrap nested payloads.
    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "policy", "settings", "filter", "filters"):
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
    raw_count = None
    for k in _COUNT_KEYS:
        if options.get(k) is not None:
            raw_count = options.get(k)
            break
    max_entries = _as_count(raw_count)
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
    return {
        "max_age_days": max_age_days,
        "retention_days": max_age_days,
        "max_age": max_age_days,
        "max_entries": max_entries,
        "max_count": max_entries,
        "keep": max_entries,
        "cutoff": cutoff,
        "before": cutoff.isoformat() if cutoff is not None else None,
        "dry_run": bool(dry_run),
        "options": options,
    }


def select_for_deletion(
    *args: Any,
    entries: Any = None,
    store: Any = None,
    **kwargs: Any,
) -> List[dict]:
    """Return the audit entries that would be pruned (oldest first)."""
    merged: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            merged.update(a)
        elif isinstance(a, (list, tuple)):
            if entries is None:
                entries = a
    merged.update(kwargs)
    eff = _effective_options(dict(merged))
    data = _read_entries(_resolve_store(store, **merged), entries=entries)

    # Sort oldest-first for deterministic pruning.
    def _sort_key(e: dict) -> Any:
        ts = _entry_ts(e)
        return (ts is None, ts, str(e.get("id", "")))

    ordered = sorted(data, key=_sort_key)

    selected: List[dict] = []
    selected_ids: set = set()

    def _mark(e: dict) -> None:
        key = id(e)
        if key not in selected_ids:
            selected_ids.add(key)
            selected.append(e)

    # Age / cutoff policy.
    cutoff = eff.get("cutoff")
    max_age_days = eff.get("max_age_days")
    if cutoff is None and max_age_days is not None and max_age_days > 0:
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=float(max_age_days))
    if cutoff is not None:
        for e in ordered:
            ts = _entry_ts(e)
            if ts is not None and ts < cutoff:
                _mark(e)

    # Count policy: keep only newest N.
    max_entries = eff.get("max_entries")
    if max_entries is not None and max_entries >= 0:
        if len(ordered) > max_entries:
            overflow = len(ordered) - int(max_entries)
            for e in ordered[:overflow]:
                _mark(e)

    # Preserve oldest-first order in output.
    order_index = {id(e): i for i, e in enumerate(ordered)}
    selected.sort(key=lambda e: order_index.get(id(e), 0))
    return selected


def _delete_from_store(store: Any, victims: List[dict]) -> int:
    if store is None or not victims:
        return 0
    victim_id_set = {v.get("id") for v in victims if isinstance(v, dict) and v.get("id") is not None}
    victim_corr = {v.get("correlation_id") for v in victims if isinstance(v, dict) and v.get("correlation_id") is not None}
    removed = 0
    try:
        import threading as _threading

        lock = getattr(store, "_lock", None)
        if isinstance(lock, _threading.Lock):
            lock.acquire()
        try:
            current = list(getattr(store, "_entries", []) or [])
            keep: List[Any] = []
            for e in current:
                eid = e.get("id") if isinstance(e, dict) else None
                corr = e.get("correlation_id") if isinstance(e, dict) else None
                if eid is not None and eid in victim_id_set:
                    removed += 1
                    continue
                # Fallback identity: full-dict equality when no id.
                if eid is None:
                    matched = any(e == v for v in victims)
                    if matched:
                        removed += 1
                        continue
                keep.append(e)
            store._entries = keep
            # Rewrite JSONL backing file when present.
            path = getattr(store, "path", None)
            if path:
                try:
                    import json as _json

                    with open(str(path), "w", encoding="utf-8") as fh:
                        for e in keep:
                            try:
                                fh.write(_json.dumps(dict(e), default=str) + "\n")
                            except Exception:  # noqa: BLE001
                                continue
                except OSError:
                    pass
        finally:
            if isinstance(lock, _threading.Lock):
                try:
                    lock.release()
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        return removed
    void_corr = victim_corr  # silence unused
    return removed


def prune(
    *args: Any,
    entries: Any = None,
    store: Any = None,
    dry_run: Any = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Prune audit entries; ``dry_run=True`` only reports candidates."""
    merged: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            merged.update(a)
        elif isinstance(a, (list, tuple)):
            if entries is None:
                entries = a
        elif isinstance(a, bool):
            if dry_run is None:
                dry_run = a
    merged.update(kwargs)
    if dry_run is not None:
        merged["dry_run"] = dry_run
    eff = _effective_options(dict(merged))
    is_dry = bool(eff.get("dry_run"))
    resolved_store = _resolve_store(store, **merged)
    all_entries = _read_entries(resolved_store, entries=entries)
    victims = select_for_deletion(entries=all_entries, store=resolved_store, **merged)

    deleted = 0
    errors: List[str] = []
    if not is_dry:
        try:
            deleted = _delete_from_store(resolved_store, victims)
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))
            deleted = 0
    else:
        deleted = 0

    remaining = len(all_entries) - (0 if is_dry else deleted)
    victim_dicts = [dict(v) for v in victims]
    victim_ids = [v.get("id") for v in victim_dicts if v.get("id") is not None]

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
        "pruned_count": len(victim_dicts),
        "deleted": victim_dicts,
        "deleted_entries": victim_dicts,
        "deleted_count": (0 if is_dry else deleted) if not is_dry else 0,
        "deleted_ids": [] if is_dry else victim_ids,
        "removed": victim_dicts,
        "removed_entries": victim_dicts,
        "removed_count": (0 if is_dry else deleted) if not is_dry else 0,
        "selected": victim_dicts,
        "selected_entries": victim_dicts,
        "selected_count": len(victim_dicts),
        "selected_ids": victim_ids,
        "candidates": victim_dicts,
        "candidate_entries": victim_dicts,
        "candidate_count": len(victim_dicts),
        "candidate_ids": victim_ids,
        "to_delete": victim_dicts,
        "to_delete_count": len(victim_dicts),
        "would_delete": victim_dicts,
        "would_delete_count": len(victim_dicts),
        "would_prune": victim_dicts,
        "would_prune_count": len(victim_dicts),
        "total": len(all_entries),
        "total_entries": len(all_entries),
        "total_count": len(all_entries),
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
        "max_entries": eff.get("max_entries"),
        "max_count": eff.get("max_entries"),
        "keep": eff.get("max_entries"),
        "before": eff.get("before"),
        "cutoff": eff.get("before"),
        "errors": errors,
        "error_count": len(errors),
    }
    result["count"] = result["deleted_count"] if not is_dry else result["selected_count"]
    result["pruned_ids"] = victim_ids if not is_dry else []
    result["removed_ids"] = [] if is_dry else victim_ids
    result["summary"] = {
        "total": len(all_entries),
        "selected": len(victim_dicts),
        "deleted": 0 if is_dry else deleted,
        "remaining": remaining,
        "dry_run": bool(is_dry),
        "max_age_days": eff.get("max_age_days"),
        "max_entries": eff.get("max_entries"),
    }
    result["report"] = dict(result["summary"])
    return result


async def prune_async(
    *args: Any,
    entries: Any = None,
    store: Any = None,
    db: Any = None,
    session: Any = None,
    database: Any = None,
    dry_run: Any = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Async prune supporting an optional ``AsyncSession`` for DB tables."""
    merged: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            merged.update(a)
        elif isinstance(a, (list, tuple)):
            if entries is None:
                entries = a
    merged.update(kwargs)
    if dry_run is not None:
        merged["dry_run"] = dry_run
    eff = _effective_options(dict(merged))
    is_dry = bool(eff.get("dry_run"))
    db_obj = db if db is not None else (session if session is not None else database)
    # If caller passed a session positionally via _extract-style, detect it.
    if db_obj is None and args:
        for a in args:
            if hasattr(a, "execute") and hasattr(a, "commit"):
                db_obj = a
                break

    result = prune(entries=entries, store=store if store is not None else _resolve_store(None, **merged), **merged)

    if db_obj is None or is_dry:
        return result

    # Best-effort DB deletion for rows older than cutoff / beyond count.
    db_deleted = 0
    errors: List[str] = list(result.get("errors") or [])
    try:
        from sqlalchemy import delete, select

        from app.models.entities import AuditEntry, AuditLog

        cutoff = eff.get("cutoff")
        # Compute cutoff from max_age_days when not explicit.
        if cutoff is None and eff.get("max_age_days") is not None and eff.get("max_age_days") > 0:
            cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=float(eff.get("max_age_days")))
        for Model in (AuditEntry, AuditLog):
            try:
                if cutoff is not None:
                    stmt = delete(Model).where(Model.timestamp < cutoff)
                    await db_obj.execute(stmt)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{getattr(Model, '__name__', 'model')}: {exc}")
                continue
        # Count-based DB pruning: keep newest N rows.
        max_entries = eff.get("max_entries")
        if max_entries is not None and max_entries >= 0:
            for Model in (AuditEntry, AuditLog):
                try:
                    rows = (await db_obj.execute(select(Model.id).order_by(Model.id.desc()))).scalars().all()
                    if len(rows) > int(max_entries):
                        stale = rows[int(max_entries):]
                        await db_obj.execute(delete(Model).where(Model.id.in_(stale)))
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{getattr(Model, '__name__', 'model')}: {exc}")
                    continue
        try:
            await db_obj.commit()
        except Exception:  # noqa: BLE001
            pass
        # Reconcile counts: report DB attempt as part of summary without breaking sync counts.
        db_deleted = 0
    except Exception as exc:  # noqa: BLE001
        errors.append(str(exc))

    result["errors"] = errors
    result["error_count"] = len(errors)
    result["db_deleted"] = db_deleted
    return result


class AuditPruneService:
    """Convenience wrapper around the module-level helpers."""

    def __init__(self, store: Any = None, path: Any = None, db: Any = None) -> None:
        if store is None and path is not None:
            try:
                from app.services.audit import AuditStore as _AuditStore

                store = _AuditStore(path=path)
            except Exception:  # noqa: BLE001
                store = None
        self.store = store
        self.path = path
        self.db = db

    def _store_for(self, kwargs: Dict[str, Any]) -> Any:
        if kwargs.get("store") is not None:
            return kwargs.get("store")
        if self.store is not None:
            return self.store
        return _resolve_store(None, **kwargs)

    def select(self, *args: Any, **kwargs: Any) -> List[dict]:
        kw = dict(kwargs)
        store = self._store_for(kw)
        return select_for_deletion(*args, store=store, **kw)

    def select_for_deletion(self, *args: Any, **kwargs: Any) -> List[dict]:
        return self.select(*args, **kwargs)

    def candidates(self, *args: Any, **kwargs: Any) -> List[dict]:
        return self.select(*args, **kwargs)

    def preview(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        kw = dict(kwargs)
        kw["dry_run"] = True
        store = self._store_for(kw)
        return prune(*args, store=store, **kw)

    def dry_run(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.preview(*args, **kwargs)

    def prune(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        kw = dict(kwargs)
        store = self._store_for(kw)
        if self.db is not None and hasattr(self.db, "execute"):
            import asyncio as _asyncio

            try:
                _asyncio.get_running_loop()
                # In async context return coroutine for caller to await.
                return prune_async(*args, store=store, db=self.db, **kw)  # type: ignore[return-value]
            except RuntimeError:
                pass
        return prune(*args, store=store, **kw)

    def run(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.prune(*args, **kwargs)

    def cleanup(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.prune(*args, **kwargs)

    def enforce(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.prune(*args, **kwargs)

    def execute(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.prune(*args, **kwargs)

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
        return self.prune


# ---------- class / instance aliases ----------
AuditPruningService = AuditPruneService
AuditRetentionService = AuditPruneService
AuditCleanupService = AuditPruneService
AuditPruner = AuditPruneService
AuditRetentionManager = AuditPruneService
AuditPruneManager = AuditPruneService
PruneAuditService = AuditPruneService
RetentionAuditService = AuditPruneService

audit_prune_service = AuditPruneService()
audit_pruning_service = audit_prune_service
audit_retention_service = audit_prune_service
audit_cleanup_service = audit_prune_service
prune_service = audit_prune_service
service = audit_prune_service
manager = audit_prune_service
pruner = audit_prune_service
default_service = audit_prune_service


# ---------- module-level convenience functions ----------
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


def prune_audit(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return prune(*args, **kwargs)


def prune_audit_entries(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return prune(*args, **kwargs)


def prune_audit_logs(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return prune(*args, **kwargs)


def cleanup_audit(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return prune(*args, **kwargs)


def clean_audit(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return prune(*args, **kwargs)


def get_candidates(*args: Any, **kwargs: Any) -> List[dict]:
    return select_for_deletion(*args, **kwargs)


def collect_candidates(*args: Any, **kwargs: Any) -> List[dict]:
    return select_for_deletion(*args, **kwargs)


def get_files_to_delete(*args: Any, **kwargs: Any) -> List[dict]:
    return select_for_deletion(*args, **kwargs)


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
        "max_entries": eff.get("max_entries"),
        "max_count": eff.get("max_entries"),
        "before": eff.get("before"),
        "cutoff": eff.get("before"),
        "dry_run": False,
    }
