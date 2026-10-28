"""Protection expiry service for VigilCam.

Verified ground truth (``app/services/rec_protect.py``):

* ``protect_segment`` creates a **permanent** lock record with keys
  ``path/segment/file/protected/locked/lock/reason/note/protected_at/
  created_at`` -- no ``expires_at`` / ``ttl`` / ``timeout`` field.
* ``is_protected`` / ``exclude_protected`` perform no time check.
* The only removal paths are manual ``unprotect_segment`` / ``clear``.

This module layers opt-in expiry on top without touching ``rec_protect``:

* ``protect_with_expiry`` -- lock a segment with ``expires_in_days``,
  ``ttl_*`` or an absolute ``expires_at``. No expiry given means
  permanent (preserves ``rec_protect`` behaviour) unless a default
  expiry policy is configured.
* ``sweep_expired`` -- auto-unlock every lock whose ``expires_at`` passed.
  Returns what was unlocked so schedulers / routers can report it.
* ``is_protected`` / ``status_for`` / ``exclude_protected`` are
  expiry-aware: expired locks report ``protected=False`` and are
  auto-removed (unless ``auto_sweep`` is disabled).
* Expiry policies: module-level default/max/auto-sweep policy via
  ``get_policy`` / ``set_policy`` / ``reset_policy``.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Union

import app.services.rec_protect as rec_protect_mod

__all__ = [
    "ProtectExpiryService",
    "protect_expiry_service",
    "protect_with_expiry",
    "is_protected",
    "is_expired",
    "status_for",
    "list_protected",
    "list_expired",
    "list_expiring",
    "sweep_expired",
    "exclude_protected",
    "set_expiry",
    "clear_expiry",
    "extend_protection",
    "get_expiry",
    "get_policy",
    "set_policy",
    "reset_policy",
]

_EXPIRY_AT_KEYS = (
    "expires_at",
    "expiresAt",
    "expiry",
    "expiry_date",
    "expiryDate",
    "expires",
    "expire_at",
    "expireAt",
    "until",
    "valid_until",
    "validUntil",
)

_TTL_DAYS_KEYS = (
    "expires_in_days",
    "expiry_days",
    "ttl_days",
    "ttlDays",
    "expires_days",
    "expiryDays",
    "valid_days",
    "keep_days",
    "days",
)

_TTL_SECONDS_KEYS = (
    "ttl_seconds",
    "ttlSeconds",
    "expires_in_seconds",
    "expires_in",
    "ttl",
    "expiry_seconds",
    "valid_seconds",
)

_TTL_HOURS_KEYS = (
    "ttl_hours",
    "ttlHours",
    "expires_in_hours",
    "expiry_hours",
)

_TTL_MINUTES_KEYS = (
    "ttl_minutes",
    "ttlMinutes",
    "expires_in_minutes",
    "expiry_minutes",
)

_PERMANENT_TOKENS = {"", "never", "none", "null", "permanent", "no-expiry", "no_expiry", "noexpiry", "infinite", "inf"}

_POLICY_LOCK = threading.RLock()
_POLICY: Dict[str, Any] = {
    "default_expires_in_days": None,
    "max_expires_in_days": None,
    "auto_sweep": True,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _coerce_dt(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        try:
            num = float(value)
        except (TypeError, ValueError):
            return None
        if num <= 0:
            return _now()
        # Heuristic: epoch seconds vs milliseconds.
        if abs(num) > 1e12:
            num = num / 1000.0
        try:
            return datetime.fromtimestamp(num, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    if isinstance(value, str):
        s = value.strip()
        if not s or s.lower() in _PERMANENT_TOKENS:
            return None
        try:
            num = float(s)
            if num > 0:
                if abs(num) > 1e12:
                    num = num / 1000.0
                # Large magnitudes are epoch timestamps; small ones are
                # ambiguous here (caller decides days vs seconds), so only
                # treat epoch-plausible values numerically.
                if abs(num) > 1e8:
                    return datetime.fromtimestamp(num, tz=timezone.utc)
        except (TypeError, ValueError):
            pass
        iso = s[:-1] + "+00:00" if s.endswith("Z") else s
        try:
            dt = datetime.fromisoformat(iso)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    return None


def _coerce_days(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None if value is False else None
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    if isinstance(value, str):
        s = value.strip().lower().replace(" ", "")
        if not s or s in _PERMANENT_TOKENS:
            return None
        for suffix in ("days", "day", "d"):
            if s.endswith(suffix) and len(s) > len(suffix):
                s = s[: -len(suffix)]
                break
        try:
            return float(s)
        except (TypeError, ValueError):
            return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _coerce_seconds(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    if isinstance(value, str):
        s = value.strip().lower().replace(" ", "")
        if not s or s in _PERMANENT_TOKENS:
            return None
        for suffix in ("seconds", "second", "secs", "sec", "s"):
            if s.endswith(suffix) and len(s) > len(suffix):
                s = s[: -len(suffix)]
                break
        try:
            return float(s)
        except (TypeError, ValueError):
            return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _first_present(data: Dict[str, Any], keys: Iterable[str]) -> Any:
    for k in keys:
        if data.get(k) is not None:
            return data.get(k)
    lower = {str(k).lower(): v for k, v in data.items()}
    for k in keys:
        if lower.get(k.lower()) is not None:
            return lower.get(k.lower())
    return None


def _resolve_expiry(data: Dict[str, Any], now: Optional[datetime] = None) -> Optional[str]:
    """Resolve ``expires_at`` ISO string from alias-rich kwargs.

    Returns ``None`` for permanent (no expiry). Explicit ``never``-style
    values force permanent even when a default policy exists.
    """
    now = now or _now()
    explicit_permanent = False
    for k in list(_EXPIRY_AT_KEYS) + list(_TTL_DAYS_KEYS) + list(_TTL_SECONDS_KEYS) + list(
        _TTL_HOURS_KEYS
    ) + list(_TTL_MINUTES_KEYS):
        v = data.get(k)
        if isinstance(v, str) and v.strip().lower() in _PERMANENT_TOKENS:
            # Distinguish "explicitly permanent" from "absent".
            if k in data:
                explicit_permanent = True
    # Absolute timestamp wins.
    raw_at = _first_present(data, _EXPIRY_AT_KEYS)
    if raw_at is not None:
        if isinstance(raw_at, str) and raw_at.strip().lower() in _PERMANENT_TOKENS:
            return None
        dt = _coerce_dt(raw_at)
        if dt is not None:
            return dt.isoformat()
        # Unparseable expiry strings are ignored (fall through to TTL).
    raw_days = _first_present(data, _TTL_DAYS_KEYS)
    if raw_days is not None:
        if isinstance(raw_days, str) and raw_days.strip().lower() in _PERMANENT_TOKENS:
            return None
        days = _coerce_days(raw_days)
        if days is not None:
            if days <= 0:
                return now.isoformat()  # immediate expiry
            return (now + timedelta(days=float(days))).isoformat()
    raw_hours = _first_present(data, _TTL_HOURS_KEYS)
    if raw_hours is not None:
        secs = _coerce_seconds(raw_hours)
        if secs is not None:
            if secs <= 0:
                return now.isoformat()
            return (now + timedelta(hours=float(secs))).isoformat()
    raw_minutes = _first_present(data, _TTL_MINUTES_KEYS)
    if raw_minutes is not None:
        secs = _coerce_seconds(raw_minutes)
        if secs is not None:
            if secs <= 0:
                return now.isoformat()
            return (now + timedelta(minutes=float(secs))).isoformat()
    raw_secs = _first_present(data, _TTL_SECONDS_KEYS)
    if raw_secs is not None:
        if isinstance(raw_secs, str) and raw_secs.strip().lower() in _PERMANENT_TOKENS:
            return None
        secs = _coerce_seconds(raw_secs)
        if secs is not None:
            if secs <= 0:
                return now.isoformat()
            return (now + timedelta(seconds=float(secs))).isoformat()
    if explicit_permanent:
        return None
    # Fall back to the configured default policy.
    with _POLICY_LOCK:
        default_days = _POLICY.get("default_expires_in_days")
        max_days = _POLICY.get("max_expires_in_days")
    if default_days is not None:
        try:
            dd = float(default_days)
        except (TypeError, ValueError):
            return None
        if dd <= 0:
            return now.isoformat()
        if max_days is not None:
            try:
                dd = min(dd, float(max_days))
            except (TypeError, ValueError):
                pass
        return (now + timedelta(days=dd)).isoformat()
    return None


def _is_expired_record(record: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    exp = record.get("expires_at") or record.get("expiresAt") or record.get("expiry")
    if not exp:
        return False
    dt = _coerce_dt(exp)
    if dt is None:
        return False
    current = now or _now()
    return dt <= current


def _remaining_seconds(record: Dict[str, Any], now: Optional[datetime] = None) -> Optional[float]:
    exp = record.get("expires_at") or record.get("expiresAt") or record.get("expiry")
    if not exp:
        return None
    dt = _coerce_dt(exp)
    if dt is None:
        return None
    current = now or _now()
    return (dt - current).total_seconds()


def _annotate(record: Dict[str, Any], now: Optional[datetime] = None) -> Dict[str, Any]:
    out = dict(record)
    now = now or _now()
    expired = _is_expired_record(out, now)
    remaining = _remaining_seconds(out, now)
    out["expired"] = bool(expired)
    out["is_expired"] = bool(expired)
    out["remaining_seconds"] = remaining
    out["expires_in_seconds"] = remaining
    if out.get("expires_at") is None and out.get("expiresAt") is not None:
        out["expires_at"] = out.get("expiresAt")
    if out.get("expiry_policy") is None:
        out["expiry_policy"] = "timed" if out.get("expires_at") else "never"
    out.setdefault("ok", True)
    out.setdefault("success", True)
    return out


def _clamp_to_max(expires_at: Optional[str]) -> Optional[str]:
    if expires_at is None:
        return None
    with _POLICY_LOCK:
        max_days = _POLICY.get("max_expires_in_days")
    if max_days is None:
        return expires_at
    try:
        cap = float(max_days)
    except (TypeError, ValueError):
        return expires_at
    if cap <= 0:
        return expires_at
    dt = _coerce_dt(expires_at)
    if dt is None:
        return expires_at
    latest = _now() + timedelta(days=cap)
    if dt > latest:
        return latest.isoformat()
    return expires_at


def _apply_expiry_fields(
    key: str,
    expires_at: Optional[str],
    storage_root: Optional[Union[str, Path]] = None,
    policy_label: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    with rec_protect_mod._LOCK:
        rec = rec_protect_mod._PROTECTED.get(key)
        if rec is None:
            return None
        if expires_at is None:
            rec.pop("expires_at", None)
            rec.pop("expiresAt", None)
            rec.pop("expiry", None)
            rec.pop("expires_in_days", None)
            rec.pop("ttl_seconds", None)
            rec["expiry_policy"] = policy_label or "never"
            rec["permanent"] = True
        else:
            rec["expires_at"] = expires_at
            rec["expiresAt"] = expires_at
            rec["expiry"] = expires_at
            rec["expiry_policy"] = policy_label or "timed"
            rec["permanent"] = False
            try:
                dt = _coerce_dt(expires_at)
                if dt is not None:
                    rec["expires_in_days"] = (dt - _now()).total_seconds() / 86400.0
            except Exception:  # noqa: BLE001
                pass
        if storage_root is not None:
            try:
                rec_protect_mod._save_registry(dict(rec_protect_mod._PROTECTED), storage_root)
            except Exception:  # noqa: BLE001
                pass
        return dict(rec)


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


def get_policy(**kwargs: Any) -> Dict[str, Any]:
    """Return the current protection-expiry policy."""
    with _POLICY_LOCK:
        policy = dict(_POLICY)
    out: Dict[str, Any] = {
        "ok": True,
        "success": True,
        "status": "ok",
        "default_expires_in_days": policy.get("default_expires_in_days"),
        "max_expires_in_days": policy.get("max_expires_in_days"),
        "auto_sweep": bool(policy.get("auto_sweep", True)),
    }
    out["default_ttl_days"] = out["default_expires_in_days"]
    out["default_expiry_days"] = out["default_expires_in_days"]
    out["max_ttl_days"] = out["max_expires_in_days"]
    out["policy"] = {
        "default_expires_in_days": policy.get("default_expires_in_days"),
        "max_expires_in_days": policy.get("max_expires_in_days"),
        "auto_sweep": bool(policy.get("auto_sweep", True)),
    }
    return out


def set_policy(
    data: Optional[Dict[str, Any]] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Create/replace the expiry policy.

    Keys: ``default_expires_in_days`` (None = permanent by default),
    ``max_expires_in_days``, ``auto_sweep``. Alias-rich.
    """
    merged: Dict[str, Any] = {}
    if isinstance(data, dict):
        merged.update(data)
    merged.update(kwargs)
    for nested in ("policy", "config", "options", "params", "payload", "body", "settings"):
        nested_val = merged.get(nested)
        if isinstance(nested_val, dict):
            for k, v in nested_val.items():
                merged.setdefault(k, v)
    default_raw = _first_present(
        merged,
        ("default_expires_in_days", "default_ttl_days", "default_expiry_days", "default_days", "default_ttl", "default_expiry", "default"),
    )
    max_raw = _first_present(
        merged,
        ("max_expires_in_days", "max_ttl_days", "max_expiry_days", "max_days", "max_ttl", "max_expiry", "max"),
    )
    auto_raw = _first_present(merged, ("auto_sweep", "autoSweep", "auto_unlock", "autoUnlock", "sweep"))
    with _POLICY_LOCK:
        if default_raw is not None or "default_expires_in_days" in merged:
            if isinstance(default_raw, str) and default_raw.strip().lower() in _PERMANENT_TOKENS:
                _POLICY["default_expires_in_days"] = None
            elif default_raw is None and "default_expires_in_days" in merged:
                _POLICY["default_expires_in_days"] = None
            else:
                days = _coerce_days(default_raw)
                _POLICY["default_expires_in_days"] = float(days) if days is not None and days > 0 else None
        if max_raw is not None or "max_expires_in_days" in merged:
            if isinstance(max_raw, str) and max_raw.strip().lower() in _PERMANENT_TOKENS:
                _POLICY["max_expires_in_days"] = None
            elif max_raw is None and "max_expires_in_days" in merged:
                _POLICY["max_expires_in_days"] = None
            else:
                days = _coerce_days(max_raw)
                _POLICY["max_expires_in_days"] = float(days) if days is not None and days > 0 else None
        if auto_raw is not None:
            if isinstance(auto_raw, str):
                _POLICY["auto_sweep"] = auto_raw.strip().lower() in ("1", "true", "yes", "y", "on")
            else:
                _POLICY["auto_sweep"] = bool(auto_raw)
    return get_policy()


def reset_policy(**kwargs: Any) -> Dict[str, Any]:
    """Reset the expiry policy to permanent-by-default."""
    with _POLICY_LOCK:
        _POLICY["default_expires_in_days"] = None
        _POLICY["max_expires_in_days"] = None
        _POLICY["auto_sweep"] = True
    return get_policy()


update_policy = set_policy
configure_policy = set_policy
put_policy = set_policy
patch_policy = set_policy
clear_policy = reset_policy


# ---------------------------------------------------------------------------
# Core operations
# ---------------------------------------------------------------------------


def protect_with_expiry(
    path: Any = None,
    reason: Optional[str] = None,
    storage_root: Optional[Union[str, Path]] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Lock a segment with an optional expiry.

    Accepts ``rec_protect.protect_segment`` args plus any of:
    ``expires_at`` / ``expires_in_days`` / ``ttl_days`` / ``ttl_seconds`` /
    ``ttl_hours`` / ``ttl_minutes`` / ``days`` (and camelCase variants).
    Without expiry keys the configured default policy applies (permanent
    unless ``default_expires_in_days`` is set).
    """
    data = dict(kwargs)
    if path is None:
        for k in ("segment", "segment_path", "file", "file_path", "filepath", "target", "key"):
            if data.get(k) is not None:
                path = data.get(k)
                break
    if reason is None:
        for k in ("note", "notes", "label", "comment", "description", "why", "lock_reason"):
            if data.get(k) is not None:
                reason = str(data.get(k))
                break
    if storage_root is None:
        storage_root = data.get("storage_root")
    now = _now()
    expires_at = _resolve_expiry(data, now=now)
    expires_at = _clamp_to_max(expires_at)
    rec = rec_protect_mod.protect_segment(path, reason=reason, storage_root=storage_root)
    key = rec_protect_mod._norm_key(rec.get("path"))
    policy_label = "never" if expires_at is None else "timed"
    if data.get("expiry_policy") is not None:
        policy_label = str(data.get("expiry_policy"))
    elif expires_at is not None:
        # Label the policy that produced the expiry for auditability.
        if data.get("default_expires_in_days") is not None or _first_present(data, _TTL_DAYS_KEYS) is not None:
            policy_label = "N_days"
        else:
            policy_label = "timed"
    if key is not None:
        updated = _apply_expiry_fields(key, expires_at, storage_root=storage_root, policy_label=policy_label)
        if updated is not None:
            rec = updated
    return _annotate(rec, now)


def set_expiry(
    path: Any = None,
    storage_root: Optional[Union[str, Path]] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Set (or replace) the expiry of an existing protection lock."""
    data = dict(kwargs)
    if path is None:
        for k in ("segment", "segment_path", "file", "file_path", "filepath", "target", "key"):
            if data.get(k) is not None:
                path = data.get(k)
                break
        if path is None:
            path = data.get("path")
    if storage_root is None:
        storage_root = data.get("storage_root")
    if path is None:
        raise ValueError("set_expiry requires a segment path")
    if not rec_protect_mod.is_protected(path, storage_root=storage_root):
        # Auto-create the lock so set_expiry is idempotent.
        rec_protect_mod.protect_segment(path, reason=data.get("reason"), storage_root=storage_root)
    expires_at = _resolve_expiry(data)
    expires_at = _clamp_to_max(expires_at)
    key = rec_protect_mod._norm_key(path)
    # Resolve via suffix matching when the normalized key differs.
    target_key: Optional[str] = None
    if key is not None:
        with rec_protect_mod._LOCK:
            if key in rec_protect_mod._PROTECTED:
                target_key = key
            else:
                for stored in rec_protect_mod._PROTECTED.keys():
                    if rec_protect_mod._matches(stored, path):
                        target_key = stored
                        break
    updated: Optional[Dict[str, Any]] = None
    if target_key is not None:
        label = str(data.get("expiry_policy") or ("never" if expires_at is None else "timed"))
        updated = _apply_expiry_fields(target_key, expires_at, storage_root=storage_root, policy_label=label)
    if updated is None:
        return {"ok": False, "success": False, "protected": False, "path": str(path)}
    out = _annotate(updated)
    out["ok"] = True
    out["success"] = True
    return out


def clear_expiry(
    path: Any = None,
    storage_root: Optional[Union[str, Path]] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Make an existing lock permanent (remove its expiry)."""
    data = dict(kwargs)
    if path is None:
        path = data.get("path") or data.get("segment") or data.get("file")
    if storage_root is None:
        storage_root = data.get("storage_root")
    if path is None:
        raise ValueError("clear_expiry requires a segment path")
    key = rec_protect_mod._norm_key(path)
    target_key: Optional[str] = None
    with rec_protect_mod._LOCK:
        if key is not None and key in rec_protect_mod._PROTECTED:
            target_key = key
        else:
            for stored in rec_protect_mod._PROTECTED.keys():
                if rec_protect_mod._matches(stored, path):
                    target_key = stored
                    break
    if target_key is None:
        return {"ok": False, "success": False, "protected": False, "path": str(path)}
    updated = _apply_expiry_fields(target_key, None, storage_root=storage_root, policy_label="never")
    out = _annotate(updated or {})
    out["ok"] = True
    out["success"] = True
    return out


def extend_protection(
    path: Any = None,
    storage_root: Optional[Union[str, Path]] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Extend a lock by ``extra_days`` (or set a fresh expiry)."""
    data = dict(kwargs)
    if path is None:
        path = data.get("path") or data.get("segment") or data.get("file")
    if storage_root is None:
        storage_root = data.get("storage_root")
    if path is None:
        raise ValueError("extend_protection requires a segment path")
    extra = _first_present(data, ("extra_days", "extraDays", "extend_by_days", "extend_days", "additional_days"))
    if extra is None:
        # No explicit extension: treat as set_expiry with given keys.
        return set_expiry(path, storage_root=storage_root, **data)
    try:
        extra_days = float(extra)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError("extend_protection requires a numeric extra_days")
    current = get_expiry(path, storage_root=storage_root)
    base = _coerce_dt(current.get("expires_at")) or _now()
    new_exp = (base + timedelta(days=float(extra_days))).isoformat()
    new_exp = _clamp_to_max(new_exp) or new_exp
    return set_expiry(path, storage_root=storage_root, expires_at=new_exp, reason=data.get("reason"))


def get_expiry(
    path: Any = None,
    storage_root: Optional[Union[str, Path]] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Return expiry detail for a single lock."""
    data = dict(kwargs)
    if path is None:
        path = data.get("path") or data.get("segment") or data.get("file")
    if storage_root is None:
        storage_root = data.get("storage_root")
    key = rec_protect_mod._norm_key(path) if path is not None else None
    record: Optional[Dict[str, Any]] = None
    with rec_protect_mod._LOCK:
        if key is not None and key in rec_protect_mod._PROTECTED:
            record = dict(rec_protect_mod._PROTECTED[key])
        else:
            for stored, rec in rec_protect_mod._PROTECTED.items():
                if rec_protect_mod._matches(stored, path):
                    record = dict(rec)
                    break
    if record is None:
        return {
            "ok": False,
            "success": False,
            "protected": False,
            "expired": False,
            "expires_at": None,
            "path": str(path) if path is not None else None,
        }
    out = _annotate(record)
    out["protected"] = True
    return out


def is_expired(path: Any, storage_root: Optional[Union[str, Path]] = None, **kwargs: Any) -> bool:
    """Return True when the lock exists and its expiry has passed."""
    data = dict(kwargs)
    if storage_root is None:
        storage_root = data.get("storage_root")
    detail = get_expiry(path, storage_root=storage_root)
    if not detail.get("protected"):
        return False
    return bool(detail.get("expired"))


def is_protected(
    path: Any,
    storage_root: Optional[Union[str, Path]] = None,
    auto_sweep: Optional[bool] = None,
    **kwargs: Any,
) -> bool:
    """Expiry-aware protection check.

    Expired locks report ``False``. Unless ``auto_sweep`` is disabled
    (globally or per-call), expired locks are auto-removed on read.
    """
    data = dict(kwargs)
    if storage_root is None:
        storage_root = data.pop("storage_root", None)
    if auto_sweep is None:
        auto_sweep = data.pop("auto_sweep", data.pop("autoSweep", None))
    if auto_sweep is None:
        with _POLICY_LOCK:
            auto_sweep = bool(_POLICY.get("auto_sweep", True))
    if not rec_protect_mod.is_protected(path, storage_root=storage_root):
        return False
    detail = get_expiry(path, storage_root=storage_root)
    if detail.get("expired"):
        if auto_sweep:
            try:
                rec_protect_mod.unprotect_segment(path, storage_root=storage_root)
            except Exception:  # noqa: BLE001
                pass
        return False
    return True


def status_for(
    path: Any,
    storage_root: Optional[Union[str, Path]] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Alias-rich status payload including expiry fields."""
    data = dict(kwargs)
    if storage_root is None:
        storage_root = data.get("storage_root")
    base = rec_protect_mod.status_for(path, storage_root=storage_root)
    detail = get_expiry(path, storage_root=storage_root)
    expired = bool(detail.get("expired")) if detail.get("protected") else False
    # Expired locks are not protected anymore.
    effective = bool(base.get("protected")) and not expired
    out = dict(base)
    out.update(
        {
            "protected": effective,
            "locked": effective,
            "is_protected": effective,
            "is_locked": effective,
            "expired": expired,
            "is_expired": expired,
            "expires_at": detail.get("expires_at"),
            "expiresAt": detail.get("expires_at"),
            "expiry": detail.get("expires_at"),
            "remaining_seconds": detail.get("remaining_seconds"),
            "expires_in_seconds": detail.get("remaining_seconds"),
            "expiry_policy": detail.get("expiry_policy") or ("timed" if detail.get("expires_at") else "never"),
            "permanent": detail.get("expires_at") is None,
        }
    )
    return out


def list_protected(
    storage_root: Optional[Union[str, Path]] = None,
    include_expired: bool = True,
    **kwargs: Any,
) -> List[Dict[str, Any]]:
    """List locks annotated with expiry fields."""
    if isinstance(storage_root, dict) and not kwargs:
        kwargs = dict(storage_root)
        storage_root = kwargs.pop("storage_root", None)
    data = dict(kwargs)
    if storage_root is None:
        storage_root = data.pop("storage_root", None)
    if "include_expired" in data:
        include_expired = bool(data.pop("include_expired"))
    if "includeExpired" in data:
        include_expired = bool(data.pop("includeExpired"))
    now = _now()
    records = rec_protect_mod.list_protected(storage_root=storage_root)
    out = [_annotate(r, now) for r in records]
    if not include_expired:
        out = [r for r in out if not r.get("expired")]
    return out


def list_expired(
    storage_root: Optional[Union[str, Path]] = None,
    **kwargs: Any,
) -> List[Dict[str, Any]]:
    """Return only locks whose expiry has passed."""
    data = dict(kwargs)
    if storage_root is None:
        storage_root = data.get("storage_root")
    return [r for r in list_protected(storage_root=storage_root) if r.get("expired")]


def list_expiring(
    within_days: Any = 7,
    storage_root: Optional[Union[str, Path]] = None,
    **kwargs: Any,
) -> List[Dict[str, Any]]:
    """Return unexpired locks expiring within ``within_days``."""
    data = dict(kwargs)
    if storage_root is None:
        storage_root = data.get("storage_root")
    if within_days is None:
        within_days = data.get("within_days", data.get("withinDays", 7))
    try:
        window = float(within_days)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        window = 7.0
    now = _now()
    out: List[Dict[str, Any]] = []
    for rec in list_protected(storage_root=storage_root):
        if rec.get("expired"):
            continue
        rem = rec.get("remaining_seconds")
        if rem is None:
            continue
        if 0 <= float(rem) <= window * 86400.0:
            out.append(rec)
    return out


def sweep_expired(
    storage_root: Optional[Union[str, Path]] = None,
    dry_run: Any = False,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Auto-unlock every expired lock. Returns a report dict.

    ``dry_run=True`` reports without removing.
    """
    data = dict(kwargs)
    if storage_root is None:
        storage_root = data.get("storage_root")
    if isinstance(storage_root, dict):
        data = dict(storage_root)
        storage_root = data.get("storage_root")
    if "dry_run" in data:
        dry_run = data.get("dry_run")
    if "dryRun" in data and data.get("dry_run") is None:
        dry_run = data.get("dryRun")
    if isinstance(dry_run, str):
        dry_run = dry_run.strip().lower() in ("1", "true", "yes", "y", "on")
    dry = bool(dry_run)
    now = _now()
    expired = [r for r in list_protected(storage_root=storage_root) if _is_expired_record(r, now)]
    unlocked: List[str] = []
    errors: List[str] = []
    if not dry:
        for rec in expired:
            target = rec.get("path") or rec.get("segment") or rec.get("file")
            try:
                ok = rec_protect_mod.unprotect_segment(target, storage_root=storage_root)
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))
                continue
            if ok:
                unlocked.append(str(target))
            else:
                # Fallback: try raw key removal via suffix match.
                try:
                    ok2 = rec_protect_mod.unprotect_segment(rec.get("path"), storage_root=storage_root)
                    if ok2:
                        unlocked.append(str(rec.get("path")))
                except Exception as exc:  # noqa: BLE001
                    errors.append(str(exc))
    remaining = len(list_protected(storage_root=storage_root)) if dry else len(
        [r for r in list_protected(storage_root=storage_root)]
    )
    paths = [str(r.get("path")) for r in expired]
    return {
        "ok": True,
        "success": True,
        "status": "ok",
        "dry_run": dry,
        "dryRun": dry,
        "swept": unlocked if not dry else [],
        "unlocked": unlocked if not dry else [],
        "released": unlocked if not dry else [],
        "expired": paths,
        "expired_paths": paths,
        "expired_count": len(expired),
        "unlocked_count": len(unlocked) if not dry else 0,
        "swept_count": len(unlocked) if not dry else 0,
        "would_unlock": paths if dry else [],
        "would_unlock_count": len(expired) if dry else 0,
        "count": len(unlocked) if not dry else len(expired),
        "remaining": remaining,
        "remaining_count": remaining,
        "errors": errors,
        "error_count": len(errors),
        "swept_at": now.isoformat(),
    }


def exclude_protected(
    candidates: Optional[Iterable[Any]],
    storage_root: Optional[Union[str, Path]] = None,
    auto_sweep: Optional[bool] = None,
    **kwargs: Any,
) -> List[Any]:
    """Filter deletion candidates, auto-unlocking expired locks first."""
    data = dict(kwargs)
    if storage_root is None:
        storage_root = data.pop("storage_root", None)
    if auto_sweep is None:
        auto_sweep = data.pop("auto_sweep", data.pop("autoSweep", None))
    if auto_sweep is None:
        with _POLICY_LOCK:
            auto_sweep = bool(_POLICY.get("auto_sweep", True))
    if auto_sweep:
        try:
            sweep_expired(storage_root=storage_root)
        except Exception:  # noqa: BLE001
            pass
    return rec_protect_mod.exclude_protected(candidates, storage_root=storage_root)


def clear(storage_root: Optional[Union[str, Path]] = None, **kwargs: Any) -> int:
    """Remove all protection locks (expiry-aware clear)."""
    data = dict(kwargs)
    if storage_root is None:
        storage_root = data.get("storage_root")
    return rec_protect_mod.clear(storage_root=storage_root)


class ProtectExpiryService:
    """Instance wrapper around the protection-expiry helpers."""

    def __init__(self, storage_root: Optional[Union[str, Path]] = None) -> None:
        self.storage_root = Path(storage_root).expanduser() if storage_root is not None else None

    def _root(self, override: Optional[Union[str, Path]] = None) -> Optional[Path]:
        return Path(override).expanduser() if override is not None else self.storage_root

    def protect(self, path: Any = None, reason: Optional[str] = None, **kwargs: Any) -> Dict[str, Any]:
        root = kwargs.pop("storage_root", None)
        return protect_with_expiry(path, reason=reason, storage_root=root if root is not None else self.storage_root, **kwargs)

    def protect_with_expiry(self, path: Any = None, reason: Optional[str] = None, **kwargs: Any) -> Dict[str, Any]:
        return self.protect(path, reason=reason, **kwargs)

    def is_protected(self, path: Any, **kwargs: Any) -> bool:
        root = kwargs.pop("storage_root", None)
        return is_protected(path, storage_root=root if root is not None else self.storage_root, **kwargs)

    def is_expired(self, path: Any, **kwargs: Any) -> bool:
        root = kwargs.pop("storage_root", None)
        return is_expired(path, storage_root=root if root is not None else self.storage_root, **kwargs)

    def status_for(self, path: Any, **kwargs: Any) -> Dict[str, Any]:
        root = kwargs.pop("storage_root", None)
        return status_for(path, storage_root=root if root is not None else self.storage_root, **kwargs)

    def list_protected(self, **kwargs: Any) -> List[Dict[str, Any]]:
        root = kwargs.pop("storage_root", None)
        return list_protected(storage_root=root if root is not None else self.storage_root, **kwargs)

    def list_expired(self, **kwargs: Any) -> List[Dict[str, Any]]:
        root = kwargs.pop("storage_root", None)
        return list_expired(storage_root=root if root is not None else self.storage_root, **kwargs)

    def list_expiring(self, within_days: Any = 7, **kwargs: Any) -> List[Dict[str, Any]]:
        root = kwargs.pop("storage_root", None)
        return list_expiring(within_days, storage_root=root if root is not None else self.storage_root, **kwargs)

    def sweep_expired(self, **kwargs: Any) -> Dict[str, Any]:
        root = kwargs.pop("storage_root", None)
        return sweep_expired(storage_root=root if root is not None else self.storage_root, **kwargs)

    def sweep(self, **kwargs: Any) -> Dict[str, Any]:
        return self.sweep_expired(**kwargs)

    def set_expiry(self, path: Any = None, **kwargs: Any) -> Dict[str, Any]:
        root = kwargs.pop("storage_root", None)
        return set_expiry(path, storage_root=root if root is not None else self.storage_root, **kwargs)

    def clear_expiry(self, path: Any = None, **kwargs: Any) -> Dict[str, Any]:
        root = kwargs.pop("storage_root", None)
        return clear_expiry(path, storage_root=root if root is not None else self.storage_root, **kwargs)

    def extend(self, path: Any = None, **kwargs: Any) -> Dict[str, Any]:
        root = kwargs.pop("storage_root", None)
        return extend_protection(path, storage_root=root if root is not None else self.storage_root, **kwargs)

    def extend_protection(self, path: Any = None, **kwargs: Any) -> Dict[str, Any]:
        return self.extend(path, **kwargs)

    def get_expiry(self, path: Any = None, **kwargs: Any) -> Dict[str, Any]:
        root = kwargs.pop("storage_root", None)
        return get_expiry(path, storage_root=root if root is not None else self.storage_root, **kwargs)

    def get_policy(self, **kwargs: Any) -> Dict[str, Any]:
        return get_policy(**kwargs)

    def set_policy(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        data: Dict[str, Any] = {}
        for a in args:
            if isinstance(a, dict):
                data.update(a)
        data.update(kwargs)
        return set_policy(data)

    def reset_policy(self, **kwargs: Any) -> Dict[str, Any]:
        return reset_policy(**kwargs)

    def filter_candidates(self, candidates: Optional[Iterable[Any]] = None, **kwargs: Any) -> List[Any]:
        root = kwargs.pop("storage_root", None)
        return exclude_protected(candidates, storage_root=root if root is not None else self.storage_root, **kwargs)

    def clear(self, **kwargs: Any) -> int:
        root = kwargs.pop("storage_root", None)
        return clear(storage_root=root if root is not None else self.storage_root, **kwargs)

    # Alias surface (repo convention: many names for the same op).
    lock = protect
    add = protect
    check = is_protected
    get = is_protected
    list = list_protected
    all = list_protected
    filter = filter_candidates
    exclude = filter_candidates
    prune = filter_candidates
    apply = filter_candidates
    sweep_expired_alias = sweep_expired
    unlock_expired = sweep_expired
    auto_unlock = sweep_expired
    expire = sweep_expired


protect_expiry_service = ProtectExpiryService()
service = protect_expiry_service
manager = protect_expiry_service
default_service = protect_expiry_service
rec_protect_expiry_service = protect_expiry_service
protection_expiry_service = protect_expiry_service

# Module-level function aliases.
protect = protect_with_expiry
lock = protect_with_expiry
add_lock = protect_with_expiry
protect_segment = protect_with_expiry
sweep = sweep_expired
unlock_expired = sweep_expired
auto_unlock = sweep_expired
prune_expired = sweep_expired
expire = sweep_expired
release_expired = sweep_expired
extend = extend_protection
renew = extend_protection
filter_candidates = exclude_protected
filter_protected = exclude_protected
apply_protection = exclude_protected
