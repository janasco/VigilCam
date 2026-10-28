"""Time-based escalation-chain service for VigilCam.

Builds *escalation policies* on top of the existing operator workflow state:

* :mod:`app.services.event_ack` owns per-event ``ack`` / manual ``escalate``
  state (acknowledged / escalated flags).
* :mod:`app.services.alert_throttle` owns send rate-limiting (cooldowns /
  sliding windows).

This module deliberately duplicates **neither**: it adds time-based
escalation *chains* -- "if an event is still unacknowledged after N minutes,
escalate to another channel". Policies declare the delay (``after_minutes``)
and the escalation target channel (``escalate_to``). Caller code registers
newly-raised events via :func:`track_event` and periodically calls
:func:`evaluate` / :func:`process_due` to find (and record) due escalations.
Acknowledgement is read from ``event_ack`` (best-effort, no import cycle) so
acked events stop being due without this module owning ack state.

All state is in-memory and thread-safe so the feature works without DB
migrations and without ``app/main.py`` edits (the API router is
auto-registered by the pkgutil auto-loader).
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

_lock = threading.RLock()

# policy_id -> policy dict
_POLICIES: Dict[str, Dict[str, Any]] = {}
# event_id (int) -> tracked event dict
_TRACKED: Dict[int, Dict[str, Any]] = {}
# fired escalation records (ordered)
_HISTORY: List[Dict[str, Any]] = []
# (policy_id, event_id) pairs already fired (dedupe)
_FIRED: set = set()

MAX_AFTER_MINUTES = 10080.0  # 7 days sanity cap
MIN_AFTER_MINUTES = 0.0  # 0 == escalate immediately on next evaluation

POLICY_ID_KEYS = ("policy_id", "policyId", "id", "policy", "name_or_id", "key")
NAME_KEYS = ("name", "title", "label", "policy_name", "policyName")
AFTER_KEYS = (
    "after_minutes",
    "afterMinutes",
    "after_mins",
    "after",
    "delay_minutes",
    "delayMinutes",
    "delay",
    "delay_mins",
    "after_min",
    "unacked_minutes",
    "unacked_after_minutes",
    "escalate_after_minutes",
    "escalate_after",
    "timeout_minutes",
    "timeout",
    "within_minutes",
    "n_minutes",
    "minutes",
)
CHANNEL_KEYS = (
    "escalate_to",
    "escalateTo",
    "escalate_channel",
    "escalation_channel",
    "to_channel",
    "toChannel",
    "target_channel",
    "targetChannel",
    "channel",
    "via",
    "notify_channel",
)
FROM_CHANNEL_KEYS = (
    "from_channel",
    "fromChannel",
    "trigger_channel",
    "triggerChannel",
    "source_channel",
    "sourceChannel",
    "initial_channel",
)
SEVERITY_KEYS = ("severity", "level", "priority", "min_severity", "min_level")
CAMERA_KEYS = ("camera_id", "camera", "cam", "camera_name", "device_id", "source")
EVENT_ID_KEYS = (
    "event_id",
    "eventId",
    "event",
    "eventid",
    "id_event",
    "for_event",
    "camera_event_id",
    "id",
)
ENABLED_KEYS = ("enabled", "is_enabled", "isEnabled", "active", "is_active", "on", "enable")


def _now_ts() -> float:
    return time.time()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coerce_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        try:
            return float(str(value).strip())
        except (TypeError, ValueError):
            return default


def _coerce_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(str(value).strip()))
        except (TypeError, ValueError):
            return default


def _first(data: Dict[str, Any], keys: tuple) -> Any:
    for k in keys:
        if k in data and data.get(k) is not None:
            return data.get(k)
    lower = {str(k).lower(): v for k, v in data.items()}
    for k in keys:
        lk = str(k).lower()
        if lk in lower and lower[lk] is not None:
            return lower[lk]
    return None


def _flatten(payload: Any) -> Dict[str, Any]:
    if payload is None:
        return {}
    if hasattr(payload, "model_dump"):
        try:
            val = payload.model_dump()
            if isinstance(val, dict):
                payload = val
            else:
                return {}
        except Exception:
            pass
    if isinstance(payload, dict):
        out = dict(payload)
        for nested in ("payload", "options", "config", "params", "data", "body",
                       "request", "fields", "policy", "escalation", "chain", "rule"):
            nested_val = out.get(nested)
            if isinstance(nested_val, dict):
                for k, v in nested_val.items():
                    out.setdefault(k, v)
        return out
    try:
        return dict(payload)
    except Exception:
        return {}


def _normalize_args(args: tuple, kwargs: Dict[str, Any]) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for a in args or ():
        if a is None:
            continue
        if isinstance(a, dict):
            for k, v in a.items():
                data.setdefault(k, v)
        elif isinstance(a, bool):
            continue
        elif isinstance(a, (int, float)):
            # bare event id, e.g. untrack_event(12), due_for_event(12, now=...)
            if "event_id" not in data:
                data["event_id"] = a
            elif data.get("policy_id") is None:
                data["policy_id"] = a
        elif isinstance(a, str):
            s = a.strip()
            if not s:
                continue
            # bare string: prefer policy id slot, else channel slot
            if data.get("policy_id") is None and data.get("id") is None and data.get("name") is None:
                # heuristically treat as id only when it looks like one;
                # otherwise treat as channel below in channel-specific funcs.
                data.setdefault("policy_id", s)
            elif data.get("escalate_to") is None:
                data.setdefault("escalate_to", s)
        else:
            try:
                attrs = {k: getattr(a, k) for k in dir(a)
                         if not k.startswith("_") and not callable(getattr(a, k, None))}
                if attrs:
                    for k, v in attrs.items():
                        data.setdefault(k, v)
            except Exception:
                pass
    for k, v in (kwargs or {}).items():
        data[k] = v
    for nested in ("payload", "options", "config", "params", "data", "body",
                   "request", "fields", "policy", "escalation", "chain", "rule"):
        nested_val = data.get(nested)
        if isinstance(nested_val, dict):
            for k, v in nested_val.items():
                data.setdefault(k, v)
    return data


def _normalize_channel(value: Any) -> Optional[str]:
    if value is None or isinstance(value, bool):
        return None
    s = str(value).strip()
    if not s:
        return None
    return s.lower().replace("-", "_").replace(" ", "_")


def _resolve_now(value: Any) -> float:
    if value is None:
        return _now_ts()
    if isinstance(value, bool):
        return _now_ts()
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return _now_ts()
        try:
            return float(s)
        except (TypeError, ValueError):
            pass
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except (TypeError, ValueError):
            return _now_ts()
    return _now_ts()


def _is_acknowledged(event_id: int) -> bool:
    """Best-effort read of ack state owned by event_ack (never duplicated here)."""
    try:
        from app.services import event_ack as _ack_mod
        check = getattr(_ack_mod, "is_acknowledged", None)
        if callable(check):
            return bool(check(event_id))
    except Exception:
        pass
    return False


def _clamp_after_minutes(value: Any) -> float:
    v = _coerce_float(value, None)
    if v is None:
        raise ValueError("after_minutes is required")
    if v != v:  # NaN
        raise ValueError("after_minutes must be a number")
    if v < MIN_AFTER_MINUTES:
        v = MIN_AFTER_MINUTES
    if v > MAX_AFTER_MINUTES:
        v = MAX_AFTER_MINUTES
    return float(v)


def _to_public_policy(stored: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(stored)
    out.setdefault("ok", True)
    out.setdefault("success", True)
    return out


def _blank_policy(policy_id: str) -> Dict[str, Any]:
    now = _now_iso()
    return {
        "policy_id": policy_id,
        "id": policy_id,
        "name": policy_id,
        "after_minutes": 15.0,
        "escalate_to": "sms",
        "channel": "sms",
        "from_channel": None,
        "severity": None,
        "camera_id": None,
        "enabled": True,
        "created_at": now,
        "updated_at": now,
    }


# ---------------------------------------------------------------------------
# Policy CRUD
# ---------------------------------------------------------------------------

def create_policy(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Create an escalation policy.

    Required: ``after_minutes`` (unacked delay) and ``escalate_to`` channel.
    Optional: ``name``, ``from_channel`` (only escalate events first raised on
    this channel), ``severity`` / ``camera_id`` filters, ``enabled``.
    """
    data = _normalize_args(args, kwargs)
    raw_after = _first(data, AFTER_KEYS)
    if raw_after is None:
        raise ValueError("after_minutes is required")
    after_minutes = _clamp_after_minutes(raw_after)
    raw_channel = _first(data, CHANNEL_KEYS)
    channel = _normalize_channel(raw_channel)
    if channel is None:
        raise ValueError("escalate_to channel is required")
    name = _first(data, NAME_KEYS)
    name = str(name).strip() if name is not None and str(name).strip() else None
    from_channel = _normalize_channel(_first(data, FROM_CHANNEL_KEYS))
    severity = _first(data, SEVERITY_KEYS)
    severity = str(severity).strip().lower() if severity is not None and str(severity).strip() else None
    camera = _first(data, CAMERA_KEYS)
    camera = str(camera).strip() if camera is not None and str(camera).strip() else None
    raw_enabled = _first(data, ENABLED_KEYS)
    if isinstance(raw_enabled, str):
        enabled = raw_enabled.strip().lower() in ("1", "true", "yes", "on", "enabled", "active")
    elif raw_enabled is None:
        enabled = True
    else:
        enabled = bool(raw_enabled)
    policy_id = _first(data, POLICY_ID_KEYS)
    if policy_id is not None and name is None:
        # allow explicit id without conflating it with the display name
        pass
    pid = str(policy_id).strip() if policy_id is not None and str(policy_id).strip() else uuid.uuid4().hex[:12]
    now = _now_iso()
    with _lock:
        if pid in _POLICIES:
            raise ValueError(f"policy {pid!r} already exists")
        stored = _blank_policy(pid)
        stored.update({
            "name": name or pid,
            "after_minutes": after_minutes,
            "escalate_to": channel,
            "channel": channel,
            "from_channel": from_channel,
            "severity": severity,
            "camera_id": camera,
            "enabled": enabled,
            "created_at": now,
            "updated_at": now,
        })
        # keep any extra caller metadata that does not collide
        for k, v in data.items():
            if k not in stored:
                stored[k] = v
        _POLICIES[pid] = stored
        return _to_public_policy(dict(stored))


def get_policy(policy_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    data = _normalize_args((policy_id,) + tuple(args) if policy_id is not None else tuple(args), kwargs)
    raw = _first(data, POLICY_ID_KEYS + NAME_KEYS)
    if raw is None and isinstance(policy_id, str):
        raw = policy_id
    if raw is None:
        return None
    pid = str(raw).strip()
    with _lock:
        stored = _POLICIES.get(pid)
        if stored is None:
            # allow lookup by display name
            for cand in _POLICIES.values():
                if str(cand.get("name", "")).strip() == pid:
                    return _to_public_policy(dict(cand))
            return None
        return _to_public_policy(dict(stored))


def list_policies(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    data = _normalize_args(args, kwargs)
    enabled_filter = _first(data, ENABLED_KEYS)
    with _lock:
        items = [_to_public_policy(dict(v)) for _, v in sorted(_POLICIES.items())]
    if enabled_filter is not None:
        if isinstance(enabled_filter, str):
            want = enabled_filter.strip().lower() in ("1", "true", "yes", "on", "enabled", "active")
        else:
            want = bool(enabled_filter)
        items = [p for p in items if bool(p.get("enabled")) is want]
    return items


def update_policy(policy_id: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _normalize_args((policy_id,) + tuple(args) if policy_id is not None else tuple(args), kwargs)
    raw = _first(data, POLICY_ID_KEYS)
    if raw is None:
        # fall back to name lookup
        raw = _first(data, NAME_KEYS)
    if raw is None:
        raise ValueError("policy_id is required")
    pid = str(raw).strip()
    with _lock:
        stored = _POLICIES.get(pid)
        if stored is None:
            for key, cand in _POLICIES.items():
                if str(cand.get("name", "")).strip() == pid:
                    pid = key
                    stored = cand
                    break
        if stored is None:
            raise KeyError(f"unknown policy {pid!r}")
        raw_after = _first(data, AFTER_KEYS)
        if raw_after is not None:
            stored["after_minutes"] = _clamp_after_minutes(raw_after)
        raw_channel = _first(data, CHANNEL_KEYS)
        if raw_channel is not None:
            channel = _normalize_channel(raw_channel)
            if channel is None:
                raise ValueError("escalate_to channel is required")
            stored["escalate_to"] = channel
            stored["channel"] = channel
        raw_name = _first(data, NAME_KEYS)
        if raw_name is not None and str(raw_name).strip():
            stored["name"] = str(raw_name).strip()
        if _first(data, FROM_CHANNEL_KEYS) is not None:
            stored["from_channel"] = _normalize_channel(_first(data, FROM_CHANNEL_KEYS))
        if _first(data, SEVERITY_KEYS) is not None:
            sev = _first(data, SEVERITY_KEYS)
            stored["severity"] = str(sev).strip().lower() if sev is not None and str(sev).strip() else None
        if _first(data, CAMERA_KEYS) is not None:
            cam = _first(data, CAMERA_KEYS)
            stored["camera_id"] = str(cam).strip() if cam is not None and str(cam).strip() else None
        raw_enabled = _first(data, ENABLED_KEYS)
        if raw_enabled is not None:
            if isinstance(raw_enabled, str):
                stored["enabled"] = raw_enabled.strip().lower() in ("1", "true", "yes", "on", "enabled", "active")
            else:
                stored["enabled"] = bool(raw_enabled)
        else:
            action = str(data.get("action", "")).lower()
            if action in ("enable", "activate", "on"):
                stored["enabled"] = True
            elif action in ("disable", "deactivate", "off"):
                stored["enabled"] = False
        stored["updated_at"] = _now_iso()
        return _to_public_policy(dict(stored))


def delete_policy(policy_id: Any = None, *args: Any, **kwargs: Any) -> bool:
    data = _normalize_args((policy_id,) + tuple(args) if policy_id is not None else tuple(args), kwargs)
    raw = _first(data, POLICY_ID_KEYS)
    if raw is None:
        raw = _first(data, NAME_KEYS)
    if raw is None:
        return False
    pid = str(raw).strip()
    with _lock:
        if pid in _POLICIES:
            del _POLICIES[pid]
            return True
        for key, cand in list(_POLICIES.items()):
            if str(cand.get("name", "")).strip() == pid:
                del _POLICIES[key]
                return True
        return False


def enable_policy(policy_id: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _normalize_args((), kwargs)
    data["enabled"] = True
    if args:
        return update_policy(policy_id, *args, **data)
    return update_policy(policy_id, **data)


def disable_policy(policy_id: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _normalize_args((), kwargs)
    data["enabled"] = False
    if args:
        return update_policy(policy_id, *args, **data)
    return update_policy(policy_id, **data)


def clear_policies(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    with _lock:
        n = len(_POLICIES)
        _POLICIES.clear()
        return {"ok": True, "success": True, "cleared": True, "count": 0, "removed": n}


# ---------------------------------------------------------------------------
# Tracked (newly-raised) events
# ---------------------------------------------------------------------------

def track_event(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Register a newly-raised event as awaiting acknowledgement.

    Accepts ``event_id`` plus optional ``channel`` (channel first notified on),
    ``severity`` / ``camera_id`` metadata, and ``fired_at`` / ``now`` /
    ``created_at`` timestamp overrides (epoch seconds, ISO string or datetime;
    defaults to now). Re-tracking an event refreshes its metadata but keeps the
    original ``fired_at`` unless an explicit timestamp override is given.
    """
    flat: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            flat.update(a)
        elif hasattr(a, "model_dump"):
            try:
                flat.update(dict(a.model_dump()))
            except Exception:
                pass
        elif isinstance(a, (int, float)) and "event_id" not in flat:
            flat["event_id"] = a
        elif isinstance(a, str) and "channel" not in flat:
            flat["channel"] = a
    flat.update(kwargs or {})
    for nested in ("payload", "params", "data", "body", "ack", "escalation", "event"):
        nested_val = flat.get(nested)
        if isinstance(nested_val, dict):
            for k, v in nested_val.items():
                flat.setdefault(k, v)
    eid = _coerce_int(_first(flat, EVENT_ID_KEYS), None)
    if eid is None:
        raise ValueError("event_id is required")
    eid = int(eid)
    channel = _normalize_channel(_first(flat, CHANNEL_KEYS + ("initial_channel",)))
    severity = _first(flat, SEVERITY_KEYS)
    severity = str(severity).strip().lower() if severity is not None and str(severity).strip() else None
    camera = _first(flat, CAMERA_KEYS)
    camera = str(camera).strip() if camera is not None and str(camera).strip() else None
    ts_raw = flat.get("fired_at", flat.get("created_at", flat.get("raised_at", flat.get("now", None))))
    with _lock:
        existing = _TRACKED.get(eid)
        if existing is not None and ts_raw is None:
            fired_ts = float(existing["fired_ts"])
        else:
            fired_ts = _resolve_now(ts_raw)
        record = {
            "event_id": eid,
            "eventId": eid,
            "id": eid,
            "channel": channel,
            "severity": severity,
            "camera_id": camera,
            "fired_at": datetime.fromtimestamp(fired_ts, tz=timezone.utc).isoformat(),
            "fired_ts": fired_ts,
            "updated_at": _now_iso(),
            "ok": True,
            "success": True,
        }
        for k, v in flat.items():
            if k not in record:
                record[k] = v
        _TRACKED[eid] = record
        return dict(record)


def untrack_event(event_id: Any = None, *args: Any, **kwargs: Any) -> bool:
    flat = _normalize_args((event_id,) + tuple(args) if event_id is not None else tuple(args), kwargs)
    eid = _coerce_int(_first(flat, EVENT_ID_KEYS), None)
    if eid is None:
        return False
    with _lock:
        return _TRACKED.pop(int(eid), None) is not None


def get_tracked(event_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    flat = _normalize_args((event_id,) + tuple(args) if event_id is not None else tuple(args), kwargs)
    eid = _coerce_int(_first(flat, EVENT_ID_KEYS), None)
    if eid is None:
        return None
    with _lock:
        rec = _TRACKED.get(int(eid))
        return dict(rec) if rec is not None else None


def list_tracked(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    with _lock:
        return [dict(_TRACKED[k]) for k in sorted(_TRACKED.keys())]


# ---------------------------------------------------------------------------
# Time-based evaluation: unacked for N minutes -> escalate channel
# ---------------------------------------------------------------------------

def _policy_matches(policy: Dict[str, Any], event: Dict[str, Any]) -> bool:
    if policy.get("from_channel"):
        if (event.get("channel") or None) != policy.get("from_channel"):
            # event without channel metadata only matches policies without a filter
            return False
    if policy.get("severity"):
        if (event.get("severity") or None) != policy.get("severity"):
            return False
    if policy.get("camera_id"):
        if str(event.get("camera_id") or "") != str(policy.get("camera_id")):
            return False
    return True


def _due_for(policies: List[Dict[str, Any]], event: Dict[str, Any], now_ts: float) -> List[Dict[str, Any]]:
    if _is_acknowledged(int(event["event_id"])):
        return []
    fired_at = float(event.get("fired_ts", now_ts))
    age_seconds = max(0.0, now_ts - fired_at)
    age_minutes = age_seconds / 60.0
    due: List[Dict[str, Any]] = []
    for policy in policies:
        if not policy.get("enabled", True):
            continue
        if (str(policy.get("policy_id")), int(event["event_id"])) in _FIRED:
            continue
        if not _policy_matches(policy, event):
            continue
        after = float(policy.get("after_minutes", 0.0))
        if age_minutes >= after:
            due.append({
                "ok": True,
                "success": True,
                "due": True,
                "event_id": int(event["event_id"]),
                "policy_id": str(policy.get("policy_id")),
                "policy_name": policy.get("name"),
                "escalate_to": policy.get("escalate_to"),
                "channel": policy.get("escalate_to"),
                "after_minutes": after,
                "age_seconds": age_seconds,
                "age_minutes": age_minutes,
                "fired_at": event.get("fired_at"),
                "evaluated_at": datetime.fromtimestamp(now_ts, tz=timezone.utc).isoformat(),
            })
    return due


def evaluate(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """Return due escalations without recording them (read-only check).

    Scans tracked events against enabled policies: an escalation is due when
    the event has been unacknowledged for at least the policy's
    ``after_minutes``. Accepts optional ``now`` (epoch/ISO/datetime) and
    optional ``event_id`` to scope to a single event.
    """
    data = _normalize_args(args, kwargs)
    now_ts = _resolve_now(data.get("now", data.get("at", data.get("evaluated_at", None))))
    scoped = _coerce_int(_first(data, EVENT_ID_KEYS), None)
    with _lock:
        policies = [dict(v) for _, v in sorted(_POLICIES.items())]
        if scoped is not None:
            events = [dict(_TRACKED[int(scoped)])] if int(scoped) in _TRACKED else []
        else:
            events = [dict(_TRACKED[k]) for k in sorted(_TRACKED.keys())]
    due: List[Dict[str, Any]] = []
    for event in events:
        due.extend(_due_for(policies, event, now_ts))
    return due


def due_for_event(event_id: Any = None, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    flat = _normalize_args((event_id,) + tuple(args) if event_id is not None else tuple(args), kwargs)
    eid = _coerce_int(_first(flat, EVENT_ID_KEYS), None)
    if eid is None:
        raise ValueError("event_id is required")
    flat["event_id"] = int(eid)
    return evaluate(**flat)


def process_due(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """Evaluate due escalations and record them (dedupe per policy+event).

    Each returned record includes the ``escalate_to`` channel the caller
    should notify on. Already-fired (policy, event) pairs are not returned
    again; acknowledging an event (via ``event_ack``) stops it being due.
    """
    data = _normalize_args(args, kwargs)
    now_ts = _resolve_now(data.get("now", data.get("at", data.get("evaluated_at", None))))
    scoped = _coerce_int(_first(data, EVENT_ID_KEYS), None)
    with _lock:
        policies = [dict(v) for _, v in sorted(_POLICIES.items())]
        if scoped is not None:
            events = [dict(_TRACKED[int(scoped)])] if int(scoped) in _TRACKED else []
        else:
            events = [dict(_TRACKED[k]) for k in sorted(_TRACKED.keys())]
    # _due_for reads _FIRED; compute outside lock then record inside.
    due: List[Dict[str, Any]] = []
    for event in events:
        due.extend(_due_for(policies, event, now_ts))
    if not due:
        return []
    with _lock:
        fired: List[Dict[str, Any]] = []
        for item in due:
            key = (str(item["policy_id"]), int(item["event_id"]))
            if key in _FIRED:
                continue
            _FIRED.add(key)
            record = dict(item)
            record["fired"] = True
            record["recorded_at"] = _now_iso()
            _HISTORY.append(record)
            fired.append(dict(record))
        return fired


def list_escalations(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """Return recorded (fired) escalation history, optionally filtered."""
    data = _normalize_args(args, kwargs)
    policy_filter = _first(data, POLICY_ID_KEYS)
    event_filter = _coerce_int(_first(data, EVENT_ID_KEYS), None)
    with _lock:
        items = [dict(h) for h in _HISTORY]
    if policy_filter is not None:
        items = [h for h in items if str(h.get("policy_id")) == str(policy_filter).strip()
                 or str(h.get("policy_name", "")).strip() == str(policy_filter).strip()]
    if event_filter is not None:
        items = [h for h in items if int(h.get("event_id", -1)) == int(event_filter)]
    return items


def status(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _normalize_args(args, kwargs)
    now_ts = _resolve_now(data.get("now", None))
    with _lock:
        n_policies = len(_POLICIES)
        n_enabled = sum(1 for p in _POLICIES.values() if p.get("enabled", True))
        n_tracked = len(_TRACKED)
        n_fired = len(_HISTORY)
    due = evaluate(now=now_ts)
    return {
        "ok": True,
        "success": True,
        "status": "ok",
        "service": "escalation",
        "policies": n_policies,
        "policies_count": n_policies,
        "enabled_policies": n_enabled,
        "tracked_events": n_tracked,
        "tracked_count": n_tracked,
        "fired_escalations": n_fired,
        "fired_count": n_fired,
        "due_escalations": len(due),
        "due_count": len(due),
    }


def clear(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Clear policies, tracked events and fired history (tests / reset)."""
    data = _normalize_args(args, kwargs)
    scope = str(data.get("scope", data.get("what", "all"))).lower()
    with _lock:
        removed = {"policies": 0, "tracked": 0, "history": 0}
        if scope in ("all", "policies"):
            removed["policies"] = len(_POLICIES)
            _POLICIES.clear()
        if scope in ("all", "tracked", "events"):
            removed["tracked"] = len(_TRACKED)
            _TRACKED.clear()
        if scope in ("all", "history", "fired", "escalations"):
            removed["history"] = len(_HISTORY)
            _HISTORY.clear()
            _FIRED.clear()
        return {"ok": True, "success": True, "cleared": True, "removed": removed}


# --- Async entrypoints (db-optional, ignored) --------------------------------

async def create_policy_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any):
    return create_policy(*args, **kwargs)


async def evaluate_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any):
    return evaluate(*args, **kwargs)


async def process_due_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any):
    return process_due(*args, **kwargs)


# ---------------------------------------------------------------------------
# Aliases (tolerant naming for API/test compatibility)
# ---------------------------------------------------------------------------

create = create_policy
add_policy = create_policy
add = create_policy
register_policy = create_policy
save_policy = create_policy
upsert_policy = create_policy

get = get_policy
fetch_policy = get_policy
get_one = get_policy
find_policy = get_policy

list_all = list_policies
get_all = list_policies
get_policies = list_policies
fetch_policies = list_policies
all_policies = list_policies
all = list_policies

update = update_policy
patch_policy = update_policy
edit_policy = update_policy
modify_policy = update_policy
set_policy = update_policy

delete = delete_policy
remove_policy = delete_policy
remove = delete_policy

enable = enable_policy
activate = enable_policy
disable = disable_policy
deactivate = disable_policy

track = track_event
register_event = track_event
watch_event = track_event
add_event = track_event
note_event = track_event

untrack = untrack_event
unwatch = untrack_event
remove_event = untrack_event
stop_tracking = untrack_event

check = evaluate
check_due = evaluate
due = evaluate
pending = evaluate
scan = evaluate

process = process_due
run = process_due
fire_due = process_due
trigger_due = process_due
dispatch_due = process_due

history = list_escalations
list_history = list_escalations
fired = list_escalations
escalations = list_escalations
get_history = list_escalations

reset = clear
purge = clear
clear_all = clear
clear_all_policies = clear_policies

get_status = status
health = status


class EscalationService:
    """Object-oriented facade over the module-level store."""

    def create_policy(self, *a: Any, **k: Any):
        return create_policy(*a, **k)

    def get_policy(self, *a: Any, **k: Any):
        return get_policy(*a, **k)

    def list_policies(self, *a: Any, **k: Any):
        return list_policies(*a, **k)

    def update_policy(self, *a: Any, **k: Any):
        return update_policy(*a, **k)

    def delete_policy(self, *a: Any, **k: Any):
        return delete_policy(*a, **k)

    def enable_policy(self, *a: Any, **k: Any):
        return enable_policy(*a, **k)

    def disable_policy(self, *a: Any, **k: Any):
        return disable_policy(*a, **k)

    def track_event(self, *a: Any, **k: Any):
        return track_event(*a, **k)

    def untrack_event(self, *a: Any, **k: Any):
        return untrack_event(*a, **k)

    def evaluate(self, *a: Any, **k: Any):
        return evaluate(*a, **k)

    def due_for_event(self, *a: Any, **k: Any):
        return due_for_event(*a, **k)

    def process_due(self, *a: Any, **k: Any):
        return process_due(*a, **k)

    def history(self, *a: Any, **k: Any):
        return list_escalations(*a, **k)

    def list_escalations(self, *a: Any, **k: Any):
        return list_escalations(*a, **k)

    def status(self, *a: Any, **k: Any):
        return status(*a, **k)

    def clear(self, *a: Any, **k: Any):
        return clear(*a, **k)


Escalation = EscalationService
EscalationChainService = EscalationService
EscalationPolicyService = EscalationService

escalation_service = EscalationService()
service = escalation_service
manager = escalation_service
store = escalation_service
policy_service = escalation_service

__all__ = [
    "create_policy",
    "get_policy",
    "list_policies",
    "update_policy",
    "delete_policy",
    "enable_policy",
    "disable_policy",
    "clear_policies",
    "track_event",
    "untrack_event",
    "get_tracked",
    "list_tracked",
    "evaluate",
    "due_for_event",
    "process_due",
    "list_escalations",
    "status",
    "clear",
    "create_policy_async",
    "evaluate_async",
    "process_due_async",
    "EscalationService",
    "escalation_service",
    "MAX_AFTER_MINUTES",
]
