"""Health history service for VigilCam.

Complements ``app.services.camera_health`` which tracks current state only
(verified: ``camera_health`` keeps ``_HEALTH`` keyed by camera id with the
last-known ``CameraHealth`` — online/offline, last frame, reconnect attempts
and backoff — and has no history tracking, no append log, no deque, no time
series) and ``app.services.uptime`` which computes windowed percentages
(``uptime_percent`` over a window from transition flags, no timestamped
health-sample series).

This module stores every recorded health sample as an independent history
entry (append-only, per-camera time series) so operators can track health
over time, compute per-camera online ratios, and export CSV/JSON.

All helpers are sync and dependency-free so they work in tests without a
database. Health/uptime integration is lazy (imports ``camera_health`` and
``uptime`` only inside snapshot helpers) to avoid import cycles.
"""

from __future__ import annotations

import csv
import io
import json
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

HEALTH_HISTORY: Dict[str, "HealthHistoryEntry"] = {}
health_history = HEALTH_HISTORY
history = HEALTH_HISTORY
HISTORY = HEALTH_HISTORY
STORE = HEALTH_HISTORY
ENTRIES = HEALTH_HISTORY
HEALTH_HISTORY_STORE = HEALTH_HISTORY
SAMPLES = HEALTH_HISTORY
LOGS = HEALTH_HISTORY

_lock = threading.Lock()
_jobs_lock = _lock

MAX_HISTORY_ENTRIES = 5000
MAX_PER_CAMERA = 500


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, set):
        return sorted((_json_safe(v) for v in value), key=repr)
    if hasattr(value, "model_dump"):
        try:
            return _json_safe(value.model_dump(mode="json"))
        except Exception:
            try:
                return _json_safe(value.model_dump())
            except Exception:
                pass
    if hasattr(value, "to_dict"):
        try:
            return _json_safe(value.to_dict())
        except Exception:
            pass
    if hasattr(value, "dict"):
        try:
            return _json_safe(value.dict())
        except Exception:
            pass
    return repr(value)


def _to_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            try:
                return value.astimezone(timezone.utc).replace(tzinfo=None)
            except Exception:
                return value.replace(tzinfo=None)
        return value
    if isinstance(value, (int, float)):
        stamp = float(value)
        if abs(stamp) > 1e12:
            stamp = stamp / 1000.0
        try:
            return datetime.utcfromtimestamp(stamp)
        except (OSError, ValueError, OverflowError):
            return None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            parsed = datetime.fromisoformat(s)
            if parsed.tzinfo is not None:
                parsed = parsed.replace(tzinfo=None)
            return parsed
        except ValueError:
            try:
                stamp = float(s)
                if abs(stamp) > 1e12:
                    stamp = stamp / 1000.0
                return datetime.utcfromtimestamp(stamp)
            except (ValueError, OSError, OverflowError):
                return None
    return None


def _coerce_camera_id(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    try:
        if hasattr(value, "id") and not isinstance(value, (int, str, float)):
            value = getattr(value, "id")
        return int(value)
    except (TypeError, ValueError):
        s = str(value).strip()
        if not s:
            return None
        import re

        m = re.search(r"(\d+)$", s)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                return None
        return None


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _normalize_entry_data(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for a in args or ():
        if a is None:
            continue
        if isinstance(a, dict):
            data.update(a)
        elif hasattr(a, "to_dict"):
            try:
                d = a.to_dict()
                if isinstance(d, dict):
                    data.update(d)
                continue
            except Exception:
                pass
        elif hasattr(a, "model_dump"):
            try:
                d = a.model_dump()
                if isinstance(d, dict):
                    data.update(d)
                continue
            except Exception:
                try:
                    d = a.model_dump(mode="json")  # type: ignore[call-arg]
                    if isinstance(d, dict):
                        data.update(d)
                    continue
                except Exception:
                    pass
        else:
            try:
                data.update(dict(a))
            except Exception:
                pass
    data.update(kwargs or {})
    for nested_key in ("payload", "options", "config", "params", "data", "body",
                       "entry", "sample", "result", "health", "attributes", "camera"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _pick(data: Dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if data.get(k) is not None:
            return data.get(k)
    lower = {str(k).lower(): v for k, v in data.items()}
    for k in keys:
        if lower.get(k.lower()) is not None:
            return lower.get(k.lower())
    return None


_ONLINE_TRUE = {"online", "healthy", "up", "true", "1", "yes", "on", "ok", "reachable"}


def _coerce_online(data: Dict[str, Any]) -> Optional[bool]:
    for key in ("online", "is_online", "isOnline", "healthy", "is_healthy", "reachable"):
        if data.get(key) is not None:
            val = data.get(key)
            if isinstance(val, bool):
                return val
            if isinstance(val, (int, float)):
                return bool(val)
            if isinstance(val, str):
                return val.strip().lower() in _ONLINE_TRUE
            return bool(val)
    status = _pick(data, "status", "state", "health")
    if status is not None:
        return str(status).strip().lower() in _ONLINE_TRUE | {"degraded"}
    return None


class HealthHistoryEntry(dict):
    """Dict-like health history record with attribute access."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value

    def to_dict(self) -> Dict[str, Any]:
        return dict(self)

    def dict(self) -> Dict[str, Any]:  # pydantic-style compat
        return self.to_dict()

    def model_dump(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.to_dict()

    def as_dict(self) -> Dict[str, Any]:
        return self.to_dict()


HealthHistoryRecord = HealthHistoryEntry
HistoryEntry = HealthHistoryEntry
HealthSampleRecord = HealthHistoryEntry
HealthHistoryModel = HealthHistoryEntry
HealthHistoryItem = HealthHistoryEntry
HistoryRecord = HealthHistoryEntry
SampleEntry = HealthHistoryEntry


def _snapshot_live(camera_id: int) -> Dict[str, Any]:
    """Best-effort snapshot of live health/uptime for *camera_id*."""
    snap: Dict[str, Any] = {}
    try:
        from app.services import camera_health as _ch

        st = _ch.get_health(camera_id, create=False)
        if st is not None:
            try:
                d = st.to_dict() if hasattr(st, "to_dict") else dict(st)
            except Exception:
                d = {}
            for k in ("online", "status", "healthy", "reconnect_attempts",
                      "attempts", "backoff_seconds", "backoff", "last_frame",
                      "last_frame_at", "last_seen", "last_online_at",
                      "last_offline_at", "last_reconnect_at", "updated_at"):
                if d.get(k) is not None and k not in snap:
                    snap[k] = d.get(k)
    except Exception:
        pass
    try:
        from app.services import uptime as _up

        stats = _up.get_uptime(camera_id)
        if isinstance(stats, dict):
            if stats.get("uptime_percent") is not None:
                snap.setdefault("uptime_percent", stats.get("uptime_percent"))
            if stats.get("online") is not None:
                snap.setdefault("uptime_online", stats.get("online"))
    except Exception:
        pass
    return snap


def _build_entry(data: Dict[str, Any]) -> HealthHistoryEntry:
    camera_raw = _pick(data, "camera_id", "camera", "cam", "cameraId", "cam_id", "id")
    # Allow target="camera:N" style too.
    if camera_raw is None:
        target_raw = _pick(data, "target", "stream", "name", "label")
        if target_raw is not None:
            camera_raw = target_raw
    cid = _coerce_camera_id(camera_raw)
    if cid is None:
        raise ValueError("camera_id is required")

    # Live snapshot fills gaps but explicit fields win.
    snap = _snapshot_live(cid)

    online = _coerce_online(data)
    if online is None:
        snap_online = snap.get("online")
        if isinstance(snap_online, bool):
            online = snap_online
        else:
            online = False
    online = bool(online)
    status_s = _pick(data, "status", "state", "health")
    status = str(status_s).strip().lower() if status_s is not None else ("online" if online else "offline")
    if status not in ("online", "offline", "degraded", "unknown", "healthy", "unhealthy"):
        status = "online" if online else "offline"

    healthy_raw = _pick(data, "healthy", "is_healthy", "health_flag")
    if healthy_raw is None:
        healthy = bool(online)
    elif isinstance(healthy_raw, bool):
        healthy = healthy_raw
    elif isinstance(healthy_raw, str):
        healthy = healthy_raw.strip().lower() in _ONLINE_TRUE
    else:
        try:
            healthy = bool(healthy_raw)
        except Exception:
            healthy = bool(online)

    reconnect_attempts = _pick(data, "reconnect_attempts", "attempts", "reconnects", "reconnect_count")
    if reconnect_attempts is None:
        reconnect_attempts = snap.get("reconnect_attempts", snap.get("attempts", 0))
    reconnect_i = _coerce_int(reconnect_attempts, 0)
    if reconnect_i < 0:
        reconnect_i = 0

    backoff = _pick(data, "backoff_seconds", "backoff", "backoffSecs")
    if backoff is None:
        backoff = snap.get("backoff_seconds", snap.get("backoff", 0.0))
    backoff_f = _coerce_float(backoff, 0.0)
    if backoff_f < 0:
        backoff_f = 0.0

    last_frame = _pick(data, "last_frame", "last_frame_at", "last_seen", "lastFrame")
    if last_frame is None:
        last_frame = snap.get("last_frame", snap.get("last_frame_at", snap.get("last_seen")))
    last_frame_s = _json_safe(last_frame) if last_frame is not None else None
    if last_frame_s is not None and not isinstance(last_frame_s, str):
        last_frame_s = str(last_frame_s)

    last_online = _pick(data, "last_online_at", "lastOnlineAt")
    if last_online is None:
        last_online = snap.get("last_online_at")
    last_online_s = _json_safe(last_online) if last_online is not None else None
    if last_online_s is not None and not isinstance(last_online_s, str):
        last_online_s = str(last_online_s)

    last_offline = _pick(data, "last_offline_at", "lastOfflineAt")
    if last_offline is None:
        last_offline = snap.get("last_offline_at")
    last_offline_s = _json_safe(last_offline) if last_offline is not None else None
    if last_offline_s is not None and not isinstance(last_offline_s, str):
        last_offline_s = str(last_offline_s)

    uptime_pct = _pick(data, "uptime_percent", "uptime_pct", "percent_online", "availability_percent")
    if uptime_pct is None:
        uptime_pct = snap.get("uptime_percent")
    try:
        uptime_f = float(uptime_pct) if uptime_pct is not None else None
    except (TypeError, ValueError):
        uptime_f = None

    sampled = _pick(data, "sampled_at", "checked_at", "sample_time", "measured_at",
                    "timestamp", "ts", "time", "date", "when", "at", "recorded_at")
    sampled_s = _json_safe(sampled) if sampled is not None else None
    if sampled_s is None:
        sampled_s = _now_iso()
    elif not isinstance(sampled_s, str):
        sampled_s = str(sampled_s)

    now_s = _pick(data, "created_at", "createdAt")
    if not isinstance(now_s, str):
        now_s = _json_safe(now_s) if now_s is not None else None
        if not isinstance(now_s, str):
            now_s = _now_iso()

    source = _pick(data, "source", "origin", "recorded_by") or "manual"
    source = str(source)
    note = _pick(data, "note", "message", "detail", "reason")
    note_s = str(note) if note is not None else None
    error = _pick(data, "error", "failure")
    error_s = str(error) if error is not None else None

    entry_id = _pick(data, "id", "entry_id", "history_id", "sample_id", "record_id") or str(uuid.uuid4())
    entry_id = str(entry_id)

    entry = HealthHistoryEntry()
    entry["id"] = entry_id
    entry["entry_id"] = entry_id
    entry["history_id"] = entry_id
    entry["sample_id"] = entry_id
    entry["record_id"] = entry_id
    entry["camera_id"] = cid
    entry["camera"] = cid
    entry["cam"] = cid
    entry["target"] = f"camera:{cid}"
    entry["online"] = bool(online)
    entry["is_online"] = bool(online)
    entry["status"] = status
    entry["state"] = status
    entry["healthy"] = bool(healthy)
    entry["health"] = bool(healthy)
    entry["reconnect_attempts"] = reconnect_i
    entry["attempts"] = reconnect_i
    entry["reconnects"] = reconnect_i
    entry["backoff_seconds"] = float(backoff_f)
    entry["backoff"] = float(backoff_f)
    entry["last_frame"] = last_frame_s
    entry["last_frame_at"] = last_frame_s
    entry["last_seen"] = last_frame_s
    entry["last_online_at"] = last_online_s
    entry["last_offline_at"] = last_offline_s
    entry["uptime_percent"] = uptime_f
    entry["uptime_pct"] = uptime_f
    entry["sampled_at"] = sampled_s
    entry["checked_at"] = sampled_s
    entry["measured_at"] = sampled_s
    entry["timestamp"] = sampled_s
    entry["created_at"] = now_s
    entry["recorded_at"] = now_s
    entry["updated_at"] = now_s
    entry["source"] = source
    entry["note"] = note_s
    entry["message"] = note_s
    entry["error"] = error_s
    entry["ok"] = True
    entry["success"] = True
    for k, v in data.items():
        if k not in entry:
            entry[k] = v
    return entry


def _entry_to_dict(entry: Any) -> Dict[str, Any]:
    if isinstance(entry, dict):
        return dict(entry)
    if hasattr(entry, "model_dump"):
        try:
            return dict(entry.model_dump(mode="json"))
        except Exception:
            try:
                return dict(entry.model_dump())
            except Exception:
                pass
    if hasattr(entry, "to_dict"):
        try:
            return dict(entry.to_dict())
        except Exception:
            pass
    try:
        return dict(entry)
    except Exception:
        return {"value": _json_safe(entry)}


def _prune_if_needed(camera_id: Optional[int] = None) -> None:
    try:
        if len(HEALTH_HISTORY) <= MAX_HISTORY_ENTRIES:
            if camera_id is not None:
                per = [e for e in HEALTH_HISTORY.values() if e.get("camera_id") == camera_id]
                if len(per) <= MAX_PER_CAMERA:
                    return
                per_sorted = sorted(per, key=lambda e: str(e.get("created_at", "")))
                drop = len(per_sorted) - MAX_PER_CAMERA
                for e in per_sorted[:drop]:
                    HEALTH_HISTORY.pop(str(e.get("id")), None)
            return
        ordered = sorted(HEALTH_HISTORY.values(), key=lambda e: str(e.get("created_at", "")))
        drop = len(ordered) - MAX_HISTORY_ENTRIES
        for e in ordered[:drop]:
            HEALTH_HISTORY.pop(str(e.get("id")), None)
    except Exception:
        pass


def _apply_filters(
    entries: List[Dict[str, Any]],
    limit: Any = None,
    skip: Any = None,
    offset: Any = None,
    **filters: Any,
) -> List[Dict[str, Any]]:
    lim = filters.pop("limit", limit)
    for alias in ("n", "count", "size", "max", "top", "per_page", "page_size"):
        if lim is None and filters.get(alias) is not None:
            lim = filters.pop(alias)
    sk = filters.pop("skip", skip)
    if sk is None:
        sk = filters.pop("offset", offset if offset is not None else 0)
    for alias in ("start_index", "page"):
        filters.pop(alias, None)
    for k in ("format_filter", "include_jobs", "includeJobs", "format_"):
        filters.pop(k, None)

    since_raw = filters.pop("since", filters.pop("from", filters.pop("start", filters.pop("after", None))))
    until_raw = filters.pop("until", filters.pop("to", filters.pop("end", filters.pop("before", None))))
    since_dt = _to_datetime(since_raw) if since_raw is not None else None
    until_dt = _to_datetime(until_raw) if until_raw is not None else None
    online_f = filters.pop("online", filters.pop("is_online", filters.pop("isOnline", None)))
    if online_f is not None and isinstance(online_f, str):
        online_f = online_f.strip().lower() in ("true", "1", "yes", "online", "on")

    alias_map = {
        "camera": ("camera_id",),
        "cam": ("camera_id",),
        "cameraId": ("camera_id",),
        "state": ("status",),
        "stage": ("status",),
        "result": ("status",),
    }
    out: List[Dict[str, Any]] = []
    for ev in entries:
        ok = True
        for key, expected in (filters or {}).items():
            if expected is None:
                continue
            if key in ("format", "fmt", "output_format") and str(expected).lower() in ("json", "csv"):
                continue
            ev_val = ev.get(key)
            if ev_val is None:
                for alt in alias_map.get(key, ()):
                    if ev.get(alt) is not None:
                        ev_val = ev.get(alt)
                        break
            if ev_val is None:
                if key in alias_map or key in (
                    "camera_id", "camera", "status", "state",
                    "id", "entry_id", "history_id", "sample_id",
                ):
                    ok = False
                    break
                continue
            # camera_id numeric compare tolerant of "camera:N".
            if key in ("camera_id", "camera", "cam", "cameraId"):
                try:
                    if int(str(ev_val).split(":")[-1]) != int(str(expected).split(":")[-1]):
                        ok = False
                        break
                    continue
                except (TypeError, ValueError):
                    pass
            if str(ev_val) != str(expected):
                ok = False
                break
        if not ok:
            continue
        if online_f is not None:
            if bool(ev.get("online")) != bool(online_f):
                continue
        if since_dt is not None or until_dt is not None:
            ts = _to_datetime(ev.get("sampled_at") or ev.get("created_at") or ev.get("timestamp"))
            if ts is not None:
                if since_dt is not None and ts < since_dt:
                    continue
                if until_dt is not None and ts > until_dt:
                    continue
        out.append(ev)
    out.sort(key=lambda e: str(e.get("sampled_at") or e.get("created_at") or ""))
    try:
        sk_i = int(sk or 0)
    except (TypeError, ValueError):
        sk_i = 0
    if sk_i:
        out = out[sk_i:]
    if lim is not None:
        try:
            lim_i = int(lim)
        except (TypeError, ValueError):
            lim_i = None
        if lim_i is not None and lim_i >= 0:
            out = out[:lim_i]
    return out


class HealthHistoryService:
    """Append-only in-memory health sample history."""

    def __init__(self) -> None:
        self.store = HEALTH_HISTORY
        self.history = HEALTH_HISTORY
        self.entries = HEALTH_HISTORY
        self.samples = HEALTH_HISTORY
        self._lock = _lock

    # -- record --
    def record_sample(self, *args: Any, **kwargs: Any) -> HealthHistoryEntry:
        data = _normalize_entry_data(*args, **kwargs)
        entry = _build_entry(data)
        with self._lock:
            HEALTH_HISTORY[entry["id"]] = entry
            _prune_if_needed(entry.get("camera_id"))
        return entry

    def record_history(self, *args: Any, **kwargs: Any) -> HealthHistoryEntry:
        return self.record_sample(*args, **kwargs)

    def log_sample(self, *args: Any, **kwargs: Any) -> HealthHistoryEntry:
        return self.record_sample(*args, **kwargs)

    def log_history(self, *args: Any, **kwargs: Any) -> HealthHistoryEntry:
        return self.record_sample(*args, **kwargs)

    def record_result(self, *args: Any, **kwargs: Any) -> HealthHistoryEntry:
        return self.record_sample(*args, **kwargs)

    def log_result(self, *args: Any, **kwargs: Any) -> HealthHistoryEntry:
        return self.record_sample(*args, **kwargs)

    def add_entry(self, *args: Any, **kwargs: Any) -> HealthHistoryEntry:
        return self.record_sample(*args, **kwargs)

    def create_entry(self, *args: Any, **kwargs: Any) -> HealthHistoryEntry:
        return self.record_sample(*args, **kwargs)

    def add(self, *args: Any, **kwargs: Any) -> HealthHistoryEntry:
        return self.record_sample(*args, **kwargs)

    def create(self, *args: Any, **kwargs: Any) -> HealthHistoryEntry:
        return self.record_sample(*args, **kwargs)

    def log(self, *args: Any, **kwargs: Any) -> HealthHistoryEntry:
        return self.record_sample(*args, **kwargs)

    def append(self, *args: Any, **kwargs: Any) -> HealthHistoryEntry:
        return self.record_sample(*args, **kwargs)

    def record(self, *args: Any, **kwargs: Any) -> HealthHistoryEntry:
        return self.record_sample(*args, **kwargs)

    def push(self, *args: Any, **kwargs: Any) -> HealthHistoryEntry:
        return self.record_sample(*args, **kwargs)

    def snapshot_and_record(self, camera_id: Any = None, **kwargs: Any) -> HealthHistoryEntry:
        """Snapshot live health/uptime for *camera_id* then record history."""
        if camera_id is None:
            camera_id = kwargs.get("camera_id", kwargs.get("camera", kwargs.get("cam", kwargs.get("id"))))
        cid = _coerce_camera_id(camera_id)
        if cid is None:
            raise ValueError("camera_id is required")
        snap = _snapshot_live(cid)
        payload: Dict[str, Any] = dict(snap)
        payload.update({k: v for k, v in kwargs.items() if v is not None})
        payload["camera_id"] = cid
        payload.setdefault("source", "snapshot")
        return self.record_sample(payload)

    def capture_from_health(self, camera_id: Any = None, **kwargs: Any) -> HealthHistoryEntry:
        return self.snapshot_and_record(camera_id, **kwargs)

    def sample_and_record(self, camera_id: Any = None, **kwargs: Any) -> HealthHistoryEntry:
        return self.snapshot_and_record(camera_id, **kwargs)

    # -- get --
    def get_entry(self, entry_id: Any) -> Optional[HealthHistoryEntry]:
        if entry_id is None:
            return None
        key = str(entry_id)
        with self._lock:
            found = HEALTH_HISTORY.get(key)
            if found is not None:
                return found
            for e in HEALTH_HISTORY.values():
                for ak in ("entry_id", "history_id", "sample_id", "record_id"):
                    if str(e.get(ak)) == key:
                        return e
        return None

    def get(self, entry_id: Any) -> Optional[HealthHistoryEntry]:
        return self.get_entry(entry_id)

    def fetch_entry(self, entry_id: Any) -> Optional[HealthHistoryEntry]:
        return self.get_entry(entry_id)

    def fetch(self, entry_id: Any) -> Optional[HealthHistoryEntry]:
        return self.get_entry(entry_id)

    def find(self, entry_id: Any) -> Optional[HealthHistoryEntry]:
        return self.get_entry(entry_id)

    def get_sample(self, entry_id: Any) -> Optional[HealthHistoryEntry]:
        return self.get_entry(entry_id)

    # -- list --
    def list_history(
        self,
        camera_id: Any = None,
        status: Any = None,  # noqa: A002
        limit: Any = None,
        offset: Any = None,
        skip: Any = None,
        **filters: Any,
    ) -> List[HealthHistoryEntry]:
        if isinstance(camera_id, dict):
            filters.update(camera_id)
            camera_id = None
        if camera_id is None:
            for ak in ("camera", "cam", "cameraId", "cam_id", "target"):
                if filters.get(ak) is not None:
                    if ak == "target":
                        raw = filters.pop("target")
                        try:
                            camera_id = _coerce_camera_id(raw)
                            if camera_id is None:
                                camera_id = raw
                        except Exception:
                            camera_id = raw
                    else:
                        camera_id = filters.pop(ak)
                    break
        if status is None:
            status = filters.pop("state", filters.pop("stage", filters.pop("result", None)))
        with self._lock:
            items = [_entry_to_dict(e) for e in list(HEALTH_HISTORY.values())]
        query: Dict[str, Any] = {}
        if camera_id is not None:
            try:
                query["camera_id"] = int(str(camera_id).split(":")[-1])
            except (TypeError, ValueError):
                query["camera_id"] = camera_id
        if status is not None:
            query["status"] = status
        query.update({k: v for k, v in filters.items() if v is not None})
        return _apply_filters(items, limit=limit, skip=skip if skip is not None else offset, **query)  # type: ignore[return-value]

    def list(self, *args: Any, **kwargs: Any) -> List[HealthHistoryEntry]:
        return self.list_history(*args, **kwargs)

    def get_history(self, *args: Any, **kwargs: Any) -> List[HealthHistoryEntry]:
        return self.list_history(*args, **kwargs)

    def fetch_history(self, *args: Any, **kwargs: Any) -> List[HealthHistoryEntry]:
        return self.list_history(*args, **kwargs)

    def get_entries(self, *args: Any, **kwargs: Any) -> List[HealthHistoryEntry]:
        return self.list_history(*args, **kwargs)

    def fetch_entries(self, *args: Any, **kwargs: Any) -> List[HealthHistoryEntry]:
        return self.list_history(*args, **kwargs)

    def all(self, *args: Any, **kwargs: Any) -> List[HealthHistoryEntry]:
        return self.list_history(*args, **kwargs)

    def list_samples(self, *args: Any, **kwargs: Any) -> List[HealthHistoryEntry]:
        return self.list_history(*args, **kwargs)

    def get_samples(self, *args: Any, **kwargs: Any) -> List[HealthHistoryEntry]:
        return self.list_history(*args, **kwargs)

    def history_for(self, camera_id: Any = None, **kwargs: Any) -> List[HealthHistoryEntry]:
        return self.list_history(camera_id=camera_id, **kwargs)

    def get_history_for_camera(self, camera_id: Any = None, **kwargs: Any) -> List[HealthHistoryEntry]:
        return self.list_history(camera_id=camera_id, **kwargs)

    def list_for_camera(self, camera_id: Any = None, **kwargs: Any) -> List[HealthHistoryEntry]:
        return self.list_history(camera_id=camera_id, **kwargs)

    def get_history_for_target(self, target: Any = None, **kwargs: Any) -> List[HealthHistoryEntry]:
        return self.list_history(camera_id=target, **kwargs)

    def get_latest_for_camera(self, camera_id: Any = None, **kwargs: Any) -> Optional[HealthHistoryEntry]:
        items = self.list_history(camera_id=camera_id, **kwargs)
        if not items:
            return None
        return items[-1]

    def get_latest_for_target(self, target: Any = None, **kwargs: Any) -> Optional[HealthHistoryEntry]:
        return self.get_latest_for_camera(target, **kwargs)

    def latest(self, camera_id: Any = None, **kwargs: Any) -> Optional[HealthHistoryEntry]:
        return self.get_latest_for_camera(camera_id, **kwargs)

    def latest_for_camera(self, camera_id: Any = None, **kwargs: Any) -> Optional[HealthHistoryEntry]:
        return self.get_latest_for_camera(camera_id, **kwargs)

    # -- stats --
    def get_stats(self, camera_id: Any = None, **kwargs: Any) -> Dict[str, Any]:
        items = self.list_history(camera_id=camera_id, **kwargs) if camera_id is not None or kwargs else self.list_history(**kwargs)
        total = len(items)
        online = sum(1 for e in items if bool(e.get("online")))
        offline = total - online
        healthy = sum(1 for e in items if bool(e.get("healthy")))
        pct = round(online / total * 100.0, 2) if total else 0.0
        return {
            "camera_id": _coerce_camera_id(camera_id) if camera_id is not None else None,
            "total": total,
            "count": total,
            "entries": total,
            "samples": total,
            "online": online,
            "offline": offline,
            "healthy": healthy,
            "online_percent": pct,
            "online_pct": pct,
            "uptime_percent": pct,
            "ok": True,
            "success": True,
        }

    def get_summary(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.get_stats(*args, **kwargs)

    def summary(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.get_stats(*args, **kwargs)

    def stats(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.get_stats(*args, **kwargs)

    def stats_for_camera(self, camera_id: Any = None, **kwargs: Any) -> Dict[str, Any]:
        return self.get_stats(camera_id=camera_id, **kwargs)

    def status(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        with self._lock:
            total = len(HEALTH_HISTORY)
            by_camera: Dict[str, int] = {}
            by_status: Dict[str, int] = {}
            for e in HEALTH_HISTORY.values():
                c = str(e.get("camera_id", "unknown"))
                by_camera[c] = by_camera.get(c, 0) + 1
                st = str(e.get("status", "unknown"))
                by_status[st] = by_status.get(st, 0) + 1
        out = self.get_stats(*args, **kwargs)
        out.update({
            "status": "ok",
            "service": "health_history",
            "by_camera": by_camera,
            "counts_by_camera": dict(by_camera),
            "by_status": by_status,
            "counts_by_status": dict(by_status),
        })
        return out

    # -- delete / clear --
    def delete_entry(self, entry_id: Any) -> bool:
        if entry_id is None:
            return False
        key = str(entry_id)
        with self._lock:
            if HEALTH_HISTORY.pop(key, None) is not None:
                return True
            for k, e in list(HEALTH_HISTORY.items()):
                for ak in ("entry_id", "history_id", "sample_id", "record_id"):
                    if str(e.get(ak)) == key:
                        HEALTH_HISTORY.pop(k, None)
                        return True
        return False

    def delete(self, entry_id: Any) -> bool:
        return self.delete_entry(entry_id)

    def remove(self, entry_id: Any) -> bool:
        return self.delete_entry(entry_id)

    def remove_entry(self, entry_id: Any) -> bool:
        return self.delete_entry(entry_id)

    def clear_history(self, **filters: Any) -> int:
        with self._lock:
            if not filters:
                n = len(HEALTH_HISTORY)
                HEALTH_HISTORY.clear()
                return n
        targets = self.list_history(**filters)
        n = 0
        with self._lock:
            for t in targets:
                if HEALTH_HISTORY.pop(str(t.get("id")), None) is not None:
                    n += 1
        return n

    def clear(self, **filters: Any) -> int:
        return self.clear_history(**filters)

    def reset(self, **filters: Any) -> int:
        return self.clear_history(**filters)

    def purge(self, **filters: Any) -> int:
        return self.clear_history(**filters)

    def count(self, **filters: Any) -> int:
        return len(self.list_history(**filters))

    def __len__(self) -> int:
        with self._lock:
            return len(HEALTH_HISTORY)


health_history_service = HealthHistoryService()
history_service = health_history_service
service = health_history_service
manager = health_history_service
history_manager = health_history_service
store = health_history_service
default_service = health_history_service
sample_service = health_history_service


def record_sample(*args: Any, **kwargs: Any) -> HealthHistoryEntry:
    return health_history_service.record_sample(*args, **kwargs)


def record_history(*args: Any, **kwargs: Any) -> HealthHistoryEntry:
    return health_history_service.record_sample(*args, **kwargs)


def log_sample(*args: Any, **kwargs: Any) -> HealthHistoryEntry:
    return health_history_service.record_sample(*args, **kwargs)


def log_history(*args: Any, **kwargs: Any) -> HealthHistoryEntry:
    return health_history_service.record_sample(*args, **kwargs)


def record_result(*args: Any, **kwargs: Any) -> HealthHistoryEntry:
    return health_history_service.record_sample(*args, **kwargs)


def add_entry(*args: Any, **kwargs: Any) -> HealthHistoryEntry:
    return health_history_service.record_sample(*args, **kwargs)


def create_entry(*args: Any, **kwargs: Any) -> HealthHistoryEntry:
    return health_history_service.record_sample(*args, **kwargs)


def snapshot_and_record(*args: Any, **kwargs: Any) -> HealthHistoryEntry:
    return health_history_service.snapshot_and_record(*args, **kwargs)


def capture_from_health(*args: Any, **kwargs: Any) -> HealthHistoryEntry:
    return health_history_service.snapshot_and_record(*args, **kwargs)


def sample_and_record(*args: Any, **kwargs: Any) -> HealthHistoryEntry:
    return health_history_service.snapshot_and_record(*args, **kwargs)


def get_entry(entry_id: Any) -> Optional[HealthHistoryEntry]:
    return health_history_service.get_entry(entry_id)


def list_history(*args: Any, **kwargs: Any) -> List[HealthHistoryEntry]:
    return health_history_service.list_history(*args, **kwargs)


def fetch_history(*args: Any, **kwargs: Any) -> List[HealthHistoryEntry]:
    return health_history_service.list_history(*args, **kwargs)


def list_entries(*args: Any, **kwargs: Any) -> List[HealthHistoryEntry]:
    return health_history_service.list_history(*args, **kwargs)


def fetch_entries(*args: Any, **kwargs: Any) -> List[HealthHistoryEntry]:
    return health_history_service.list_history(*args, **kwargs)


def get_history_for_camera(*args: Any, **kwargs: Any) -> List[HealthHistoryEntry]:
    return health_history_service.get_history_for_camera(*args, **kwargs)


def get_history_for_target(*args: Any, **kwargs: Any) -> List[HealthHistoryEntry]:
    return health_history_service.get_history_for_target(*args, **kwargs)


def get_latest_for_camera(*args: Any, **kwargs: Any) -> Optional[HealthHistoryEntry]:
    return health_history_service.get_latest_for_camera(*args, **kwargs)


def get_latest_for_target(*args: Any, **kwargs: Any) -> Optional[HealthHistoryEntry]:
    return health_history_service.get_latest_for_target(*args, **kwargs)


def get_stats(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return health_history_service.get_stats(*args, **kwargs)


def get_summary(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return health_history_service.get_stats(*args, **kwargs)


def delete_entry(entry_id: Any) -> bool:
    return health_history_service.delete_entry(entry_id)


def clear_history(**filters: Any) -> int:
    return health_history_service.clear_history(**filters)


def clear(**filters: Any) -> int:
    return health_history_service.clear_history(**filters)


def status(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return health_history_service.status(*args, **kwargs)


def count(**filters: Any) -> int:
    return health_history_service.count(**filters)


# CSV / JSON helpers for history dumps.
DEFAULT_COLUMNS = [
    "id",
    "camera_id",
    "online",
    "status",
    "healthy",
    "reconnect_attempts",
    "backoff_seconds",
    "last_frame",
    "uptime_percent",
    "source",
    "sampled_at",
    "created_at",
]

CSV_COLUMNS = DEFAULT_COLUMNS
EXPORT_COLUMNS = DEFAULT_COLUMNS
COLUMNS = DEFAULT_COLUMNS
HEADERS = DEFAULT_COLUMNS
FIELDNAMES = DEFAULT_COLUMNS


def _cell(value: Any) -> str:
    safe = _json_safe(value)
    if safe is None:
        return ""
    if isinstance(safe, (dict, list)):
        try:
            return json.dumps(safe, default=str)
        except Exception:
            return str(safe)
    return str(safe)


def to_csv(entries: Any = None, columns: Any = None, **kwargs: Any) -> str:
    if entries is None:
        entries = kwargs.pop("entries", kwargs.pop("items", kwargs.pop("logs", kwargs.pop("samples", []))))
    if isinstance(entries, dict):
        entries = [entries]
    entries = list(entries or [])
    cols = list(columns or kwargs.get("fields") or kwargs.get("fieldnames") or DEFAULT_COLUMNS)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for e in entries:
        norm = _entry_to_dict(e)
        writer.writerow({c: _cell(norm.get(c)) for c in cols})
    return buf.getvalue()


def to_json(entries: Any = None, indent: int = 2, **kwargs: Any) -> str:
    if entries is None:
        entries = kwargs.pop("entries", kwargs.pop("items", kwargs.pop("logs", kwargs.pop("samples", []))))
    if isinstance(entries, dict):
        entries = [entries]
    entries = list(entries or [])
    normed = [_entry_to_dict(e) for e in entries]
    try:
        return json.dumps(_json_safe(normed), indent=kwargs.get("indent", indent), default=str)
    except Exception:
        return json.dumps(_json_safe(normed), default=str)


def export_entries(entries: Any = None, format: str = "json", **kwargs: Any) -> str:  # noqa: A002
    fmt = str(kwargs.pop("fmt", None) or kwargs.pop("output_format", None) or format or "json").lower()
    if fmt in ("csv", "text/csv", "text-csv"):
        return to_csv(entries, **kwargs)
    return to_json(entries, **kwargs)


# Compatibility aliases.
record = record_sample
log = log_sample
add = add_entry
create = create_entry
get = get_entry
fetch = get_entry
find = get_entry
list_all = list_history
all_entries = list_history
get_entries = list_entries
remove = delete_entry
delete = delete_entry
reset = clear
purge = clear
history_entries = list_history
health_history_entries = list_history
samples = list_history
list_samples = list_history
latest = get_latest_for_camera
history_for = get_history_for_camera
summary = get_summary
stats = get_stats
get_status = status
build_csv = to_csv
export_csv = to_csv
build_json = to_json
export_json = to_json

HealthHistoryServiceClass = HealthHistoryService
HealthHistoryManager = HealthHistoryService
HistoryService = HealthHistoryService
HistoryManager = HealthHistoryService

__all__ = [
    "HEALTH_HISTORY",
    "HealthHistoryEntry",
    "HealthHistoryService",
    "health_history_service",
    "record_sample",
    "record_history",
    "log_sample",
    "log_history",
    "snapshot_and_record",
    "capture_from_health",
    "sample_and_record",
    "add_entry",
    "create_entry",
    "get_entry",
    "list_history",
    "fetch_history",
    "get_history_for_camera",
    "get_history_for_target",
    "get_latest_for_camera",
    "get_latest_for_target",
    "get_stats",
    "get_summary",
    "delete_entry",
    "clear_history",
    "clear",
    "status",
    "count",
    "to_csv",
    "to_json",
    "export_entries",
]
