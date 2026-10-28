"""Firmware version tracker for VigilCam.

Complements ``app.services.firmware`` which performs stateless mock
firmware checks only (verified: ``firmware`` exposes ``check_firmware``,
``parse_version``, ``compare_versions``, ``get_latest_version`` and
``is_update_available`` against the in-memory ``MOCK_LATEST_FIRMWARE``
table with no history tracking, no append log, no deque, no time series).

This module stores every recorded firmware check as an independent
history entry (append-only, per-camera / per-model time series) so
operators can track firmware versions over time, see when each camera
was last checked, which cameras need updates, and export CSV/JSON.

All helpers are sync and dependency-free so they work in tests without
a database. Firmware integration is lazy (imports ``app.services.firmware``
only inside check helpers) to avoid import cycles.
"""

from __future__ import annotations

import csv
import io
import json
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

FW_TRACKER: Dict[str, "FwTrackerEntry"] = {}
fw_tracker = FW_TRACKER
history = FW_TRACKER
HISTORY = FW_TRACKER
STORE = FW_TRACKER
ENTRIES = FW_TRACKER
FW_TRACKER_STORE = FW_TRACKER
FIRMWARE_TRACKER = FW_TRACKER
FIRMWARE_HISTORY = FW_TRACKER
FW_HISTORY = FW_TRACKER
TRACKER = FW_TRACKER
SAMPLES = FW_TRACKER
LOGS = FW_TRACKER
CHECKS = FW_TRACKER

_lock = threading.Lock()
_jobs_lock = _lock

MAX_HISTORY_ENTRIES = 5000
MAX_PER_CAMERA = 500
MAX_PER_MODEL = 500


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


def _normalize_entry_data(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for a in args or ():
        if a is None:
            continue
        if isinstance(a, dict):
            data.update(a)
        elif isinstance(a, (int, str, float)) and not isinstance(a, bool):
            # Positional scalar treated as camera_id (e.g. snapshot_and_record(70304, ...)).
            s = str(a).strip()
            if s and data.get("camera_id") is None:
                # Keep numeric ints as-is; strings pass through for _coerce_camera_id.
                try:
                    data["camera_id"] = int(s) if str(a).strip().isdigit() else a
                except Exception:
                    data["camera_id"] = a
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
                       "entry", "sample", "result", "firmware", "check",
                       "attributes", "camera"):
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


def _firmware_check(data: Dict[str, Any]) -> Dict[str, Any]:
    """Best-effort mock firmware check via app.services.firmware."""
    try:
        from app.services import firmware as _fw

        return dict(
            _fw.check_firmware(
                current_version=_pick(
                    data, "current_version", "current", "current_fw",
                    "fw_version", "firmware_version", "version",
                    "currentVersion",
                ),
                latest_version=_pick(
                    data, "latest_version", "latest", "latest_fw",
                    "latestVersion", "target_version",
                ),
                model=_pick(
                    data, "model", "camera_model", "model_name",
                    "modelName", "kind",
                ),
                camera_id=_pick(
                    data, "camera_id", "camera", "cam", "cameraId",
                    "cam_id", "id", "cameraID",
                ),
            )
        )
    except Exception:
        return {}


class FwTrackerEntry(dict):
    """Dict-like firmware tracker history record with attribute access."""

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


FwTrackerRecord = FwTrackerEntry
FirmwareTrackerEntry = FwTrackerEntry
FirmwareHistoryEntry = FwTrackerEntry
HistoryEntry = FwTrackerEntry
FwHistoryEntry = FwTrackerEntry
CheckEntry = FwTrackerEntry
FwTrackerModel = FwTrackerEntry
FwTrackerItem = FwTrackerEntry
HistoryRecord = FwTrackerEntry
SampleEntry = FwTrackerEntry


def _build_entry(data: Dict[str, Any]) -> FwTrackerEntry:
    # Enrich with a live mock firmware check; explicit fields win.
    live: Dict[str, Any] = {}
    try:
        live = _firmware_check(data)
    except Exception:
        live = {}

    camera_raw = _pick(data, "camera_id", "camera", "cam", "cameraId", "cam_id", "cameraID", "id")
    if camera_raw is None and live.get("camera_id") is not None:
        camera_raw = live.get("camera_id")
    # Allow "camera:N" style target too.
    if camera_raw is None:
        target_raw = _pick(data, "target", "stream", "name", "label")
        if target_raw is not None:
            camera_raw = target_raw
    cid = _coerce_camera_id(camera_raw)

    model_raw = _pick(data, "model", "camera_model", "model_name", "modelName", "kind")
    if model_raw is None:
        model_raw = live.get("model")
    model_s = str(model_raw).strip() if model_raw is not None and str(model_raw).strip() else "generic"

    current_raw = _pick(
        data, "current_version", "current", "current_fw", "fw_version",
        "firmware_version", "version", "currentVersion", "fwVersion",
    )
    if current_raw is None:
        current_raw = live.get("current_version")
    current_s = str(current_raw).strip() if current_raw is not None and str(current_raw).strip() else "0.0.0"

    latest_raw = _pick(data, "latest_version", "latest", "latest_fw", "latestVersion", "target_version")
    if latest_raw is None:
        latest_raw = live.get("latest_version")
    if latest_raw is None:
        try:
            from app.services.firmware import get_latest_version as _latest

            latest_raw = _latest(model_s)
        except Exception:
            latest_raw = "0.0.0"
    latest_s = str(latest_raw).strip() if str(latest_raw).strip() else "0.0.0"

    update_raw = _pick(data, "update_available", "needs_update", "needsUpdate", "updateAvailable")
    if update_raw is None:
        update_raw = live.get("update_available", live.get("needs_update"))
    if update_raw is None:
        try:
            from app.services.firmware import is_update_available as _needs

            update_raw = bool(_needs(current_s, latest_s, model_s))
        except Exception:
            update_raw = False
    update_b = bool(update_raw) if isinstance(update_raw, bool) else str(update_raw).strip().lower() in (
        "true", "1", "yes", "y", "on") if isinstance(update_raw, str) else bool(update_raw)

    up_to_date_raw = _pick(data, "up_to_date", "upToDate", "up_toDate")
    if up_to_date_raw is None:
        up_to_date_raw = live.get("up_to_date")
    if up_to_date_raw is None:
        up_to_date_raw = not update_b
    if isinstance(up_to_date_raw, str):
        up_to_b = up_to_date_raw.strip().lower() in ("true", "1", "yes", "y", "on")
    else:
        up_to_b = bool(up_to_date_raw)

    status_raw = _pick(data, "status", "state", "result")
    if status_raw is None:
        status_raw = live.get("status")
    if status_raw is None:
        status_raw = "update_available" if update_b else "up_to_date"
    status_s = str(status_raw).strip().lower() or ("update_available" if update_b else "up_to_date")

    comparison_raw = _pick(data, "comparison", "cmp", "compare")
    if comparison_raw is None:
        comparison_raw = live.get("comparison")
    if comparison_raw is None:
        try:
            from app.services.firmware import compare_versions as _cmp

            comparison_raw = int(_cmp(current_s, latest_s))
        except Exception:
            comparison_raw = 0
    try:
        comparison_i = int(comparison_raw)
    except (TypeError, ValueError):
        comparison_i = 0

    checked = _pick(data, "checked_at", "sampled_at", "sample_time", "measured_at",
                    "timestamp", "ts", "time", "date", "when", "at", "recorded_at")
    checked_s = _json_safe(checked) if checked is not None else None
    if checked_s is None:
        checked_s = _now_iso()
    elif not isinstance(checked_s, str):
        checked_s = str(checked_s)

    now_s = _pick(data, "created_at", "createdAt")
    if not isinstance(now_s, str):
        now_s = _json_safe(now_s) if now_s is not None else None
        if not isinstance(now_s, str):
            now_s = _now_iso()

    source = _pick(data, "source", "origin", "recorded_by") or "manual"
    source = str(source)
    note = _pick(data, "note", "message", "detail", "reason")
    note_s = str(note) if note is not None else None

    entry_id = _pick(data, "id", "entry_id", "history_id", "check_id", "record_id", "sample_id") or str(uuid.uuid4())
    entry_id = str(entry_id)

    entry = FwTrackerEntry()
    entry["id"] = entry_id
    entry["entry_id"] = entry_id
    entry["history_id"] = entry_id
    entry["check_id"] = entry_id
    entry["record_id"] = entry_id
    entry["sample_id"] = entry_id
    entry["camera_id"] = cid
    entry["camera"] = cid
    entry["cam"] = cid
    entry["target"] = f"camera:{cid}" if cid is not None else f"model:{model_s}"
    entry["model"] = model_s
    entry["camera_model"] = model_s
    entry["model_name"] = model_s
    entry["current_version"] = current_s
    entry["current"] = current_s
    entry["fw_version"] = current_s
    entry["firmware_version"] = current_s
    entry["version"] = current_s
    entry["latest_version"] = latest_s
    entry["latest"] = latest_s
    entry["target_version"] = latest_s
    entry["update_available"] = bool(update_b)
    entry["needs_update"] = bool(update_b)
    entry["update_needed"] = bool(update_b)
    entry["up_to_date"] = bool(up_to_b)
    entry["status"] = status_s
    entry["state"] = status_s
    entry["comparison"] = comparison_i
    entry["checked_at"] = checked_s
    entry["sampled_at"] = checked_s
    entry["measured_at"] = checked_s
    entry["timestamp"] = checked_s
    entry["created_at"] = now_s
    entry["recorded_at"] = now_s
    entry["updated_at"] = now_s
    entry["source"] = source
    entry["note"] = note_s
    entry["message"] = note_s
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


def _prune_if_needed(camera_id: Optional[int] = None, model: Optional[str] = None) -> None:
    try:
        if len(FW_TRACKER) <= MAX_HISTORY_ENTRIES:
            if camera_id is not None:
                per = [e for e in FW_TRACKER.values() if e.get("camera_id") == camera_id]
                if len(per) <= MAX_PER_CAMERA:
                    return
                per_sorted = sorted(per, key=lambda e: str(e.get("created_at", "")))
                drop = len(per_sorted) - MAX_PER_CAMERA
                for e in per_sorted[:drop]:
                    FW_TRACKER.pop(str(e.get("id")), None)
            return
        ordered = sorted(FW_TRACKER.values(), key=lambda e: str(e.get("created_at", "")))
        drop = len(ordered) - MAX_HISTORY_ENTRIES
        for e in ordered[:drop]:
            FW_TRACKER.pop(str(e.get("id")), None)
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

    update_f = filters.pop("update_available", filters.pop("needs_update", filters.pop("needsUpdate", None)))
    if update_f is None:
        update_f = filters.pop("update_needed", None)
    if isinstance(update_f, str):
        update_f = update_f.strip().lower() in ("true", "1", "yes", "online", "on")

    up_to_date_f = filters.pop("up_to_date", filters.pop("upToDate", None))
    if isinstance(up_to_date_f, str):
        up_to_date_f = up_to_date_f.strip().lower() in ("true", "1", "yes", "on")

    alias_map = {
        "camera": ("camera_id",),
        "cam": ("camera_id",),
        "cameraId": ("camera_id",),
        "model_name": ("model",),
        "camera_model": ("model",),
        "current": ("current_version",),
        "version": ("current_version",),
        "fw_version": ("current_version",),
        "firmware_version": ("current_version",),
        "latest": ("latest_version",),
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
                    "camera_id", "camera", "model", "status", "state",
                    "id", "entry_id", "history_id", "check_id",
                    "current_version", "latest_version",
                ):
                    ok = False
                    break
                continue
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
        if update_f is not None:
            if bool(ev.get("update_available", ev.get("needs_update"))) != bool(update_f):
                continue
        if up_to_date_f is not None:
            if bool(ev.get("up_to_date")) != bool(up_to_date_f):
                continue
        if since_dt is not None or until_dt is not None:
            ts = _to_datetime(ev.get("checked_at") or ev.get("sampled_at") or ev.get("created_at") or ev.get("timestamp"))
            if ts is not None:
                if since_dt is not None and ts < since_dt:
                    continue
                if until_dt is not None and ts > until_dt:
                    continue
        out.append(ev)
    out.sort(key=lambda e: str(e.get("checked_at") or e.get("created_at") or ""))
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


class FwTrackerService:
    """Append-only in-memory firmware version history."""

    def __init__(self) -> None:
        self.store = FW_TRACKER
        self.history = FW_TRACKER
        self.entries = FW_TRACKER
        self.checks = FW_TRACKER
        self.tracker = FW_TRACKER
        self._lock = _lock

    # -- record --
    def record_sample(self, *args: Any, **kwargs: Any) -> FwTrackerEntry:
        data = _normalize_entry_data(*args, **kwargs)
        entry = _build_entry(data)
        with self._lock:
            FW_TRACKER[entry["id"]] = entry
            _prune_if_needed(entry.get("camera_id"))
        return entry

    def record_history(self, *args: Any, **kwargs: Any) -> FwTrackerEntry:
        return self.record_sample(*args, **kwargs)

    def log_sample(self, *args: Any, **kwargs: Any) -> FwTrackerEntry:
        return self.record_sample(*args, **kwargs)

    def log_history(self, *args: Any, **kwargs: Any) -> FwTrackerEntry:
        return self.record_sample(*args, **kwargs)

    def record_result(self, *args: Any, **kwargs: Any) -> FwTrackerEntry:
        return self.record_sample(*args, **kwargs)

    def record_check(self, *args: Any, **kwargs: Any) -> FwTrackerEntry:
        return self.record_sample(*args, **kwargs)

    def log_check(self, *args: Any, **kwargs: Any) -> FwTrackerEntry:
        return self.record_sample(*args, **kwargs)

    def track(self, *args: Any, **kwargs: Any) -> FwTrackerEntry:
        return self.record_sample(*args, **kwargs)

    def track_check(self, *args: Any, **kwargs: Any) -> FwTrackerEntry:
        return self.record_sample(*args, **kwargs)

    def track_version(self, *args: Any, **kwargs: Any) -> FwTrackerEntry:
        return self.record_sample(*args, **kwargs)

    def record_version(self, *args: Any, **kwargs: Any) -> FwTrackerEntry:
        return self.record_sample(*args, **kwargs)

    def log_version(self, *args: Any, **kwargs: Any) -> FwTrackerEntry:
        return self.record_sample(*args, **kwargs)

    def check_and_record(self, *args: Any, **kwargs: Any) -> FwTrackerEntry:
        return self.record_sample(*args, **kwargs)

    def snapshot_and_record(self, *args: Any, **kwargs: Any) -> FwTrackerEntry:
        data = _normalize_entry_data(*args, **kwargs)
        data.setdefault("source", "snapshot")
        return self.record_sample(data)

    def capture_from_firmware(self, *args: Any, **kwargs: Any) -> FwTrackerEntry:
        return self.snapshot_and_record(*args, **kwargs)

    def sample_and_record(self, *args: Any, **kwargs: Any) -> FwTrackerEntry:
        return self.snapshot_and_record(*args, **kwargs)

    def add_entry(self, *args: Any, **kwargs: Any) -> FwTrackerEntry:
        return self.record_sample(*args, **kwargs)

    def create_entry(self, *args: Any, **kwargs: Any) -> FwTrackerEntry:
        return self.record_sample(*args, **kwargs)

    def add(self, *args: Any, **kwargs: Any) -> FwTrackerEntry:
        return self.record_sample(*args, **kwargs)

    def create(self, *args: Any, **kwargs: Any) -> FwTrackerEntry:
        return self.record_sample(*args, **kwargs)

    def log(self, *args: Any, **kwargs: Any) -> FwTrackerEntry:
        return self.record_sample(*args, **kwargs)

    def append(self, *args: Any, **kwargs: Any) -> FwTrackerEntry:
        return self.record_sample(*args, **kwargs)

    def record(self, *args: Any, **kwargs: Any) -> FwTrackerEntry:
        return self.record_sample(*args, **kwargs)

    def push(self, *args: Any, **kwargs: Any) -> FwTrackerEntry:
        return self.record_sample(*args, **kwargs)

    # -- get --
    def get_entry(self, entry_id: Any) -> Optional[FwTrackerEntry]:
        if entry_id is None:
            return None
        key = str(entry_id)
        with self._lock:
            found = FW_TRACKER.get(key)
            if found is not None:
                return found
            for e in FW_TRACKER.values():
                for ak in ("entry_id", "history_id", "check_id", "record_id", "sample_id"):
                    if str(e.get(ak)) == key:
                        return e
        return None

    def get(self, entry_id: Any) -> Optional[FwTrackerEntry]:
        return self.get_entry(entry_id)

    def fetch_entry(self, entry_id: Any) -> Optional[FwTrackerEntry]:
        return self.get_entry(entry_id)

    def fetch(self, entry_id: Any) -> Optional[FwTrackerEntry]:
        return self.get_entry(entry_id)

    def find(self, entry_id: Any) -> Optional[FwTrackerEntry]:
        return self.get_entry(entry_id)

    def get_check(self, entry_id: Any) -> Optional[FwTrackerEntry]:
        return self.get_entry(entry_id)

    # -- list --
    def list_history(
        self,
        camera_id: Any = None,
        model: Any = None,
        status: Any = None,  # noqa: A002
        limit: Any = None,
        offset: Any = None,
        skip: Any = None,
        **filters: Any,
    ) -> List[FwTrackerEntry]:
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
        if model is None:
            for ak in ("camera_model", "model_name", "modelName", "kind"):
                if filters.get(ak) is not None:
                    model = filters.pop(ak)
                    break
        if status is None:
            status = filters.pop("state", filters.pop("stage", filters.pop("result", None)))
        with self._lock:
            items = [_entry_to_dict(e) for e in list(FW_TRACKER.values())]
        query: Dict[str, Any] = {}
        if camera_id is not None:
            try:
                query["camera_id"] = int(str(camera_id).split(":")[-1])
            except (TypeError, ValueError):
                query["camera_id"] = camera_id
        if model is not None:
            query["model"] = model
        if status is not None:
            query["status"] = status
        query.update({k: v for k, v in filters.items() if v is not None})
        return _apply_filters(items, limit=limit, skip=skip if skip is not None else offset, **query)  # type: ignore[return-value]

    def list(self, *args: Any, **kwargs: Any) -> List[FwTrackerEntry]:
        return self.list_history(*args, **kwargs)

    def get_history(self, *args: Any, **kwargs: Any) -> List[FwTrackerEntry]:
        return self.list_history(*args, **kwargs)

    def fetch_history(self, *args: Any, **kwargs: Any) -> List[FwTrackerEntry]:
        return self.list_history(*args, **kwargs)

    def get_entries(self, *args: Any, **kwargs: Any) -> List[FwTrackerEntry]:
        return self.list_history(*args, **kwargs)

    def fetch_entries(self, *args: Any, **kwargs: Any) -> List[FwTrackerEntry]:
        return self.list_history(*args, **kwargs)

    def all(self, *args: Any, **kwargs: Any) -> List[FwTrackerEntry]:
        return self.list_history(*args, **kwargs)

    def list_checks(self, *args: Any, **kwargs: Any) -> List[FwTrackerEntry]:
        return self.list_history(*args, **kwargs)

    def get_checks(self, *args: Any, **kwargs: Any) -> List[FwTrackerEntry]:
        return self.list_history(*args, **kwargs)

    def list_samples(self, *args: Any, **kwargs: Any) -> List[FwTrackerEntry]:
        return self.list_history(*args, **kwargs)

    def get_samples(self, *args: Any, **kwargs: Any) -> List[FwTrackerEntry]:
        return self.list_history(*args, **kwargs)

    def history_for(self, camera_id: Any = None, **kwargs: Any) -> List[FwTrackerEntry]:
        return self.list_history(camera_id=camera_id, **kwargs)

    def get_history_for_camera(self, camera_id: Any = None, **kwargs: Any) -> List[FwTrackerEntry]:
        return self.list_history(camera_id=camera_id, **kwargs)

    def list_for_camera(self, camera_id: Any = None, **kwargs: Any) -> List[FwTrackerEntry]:
        return self.list_history(camera_id=camera_id, **kwargs)

    def get_history_for_model(self, model: Any = None, **kwargs: Any) -> List[FwTrackerEntry]:
        return self.list_history(model=model, **kwargs)

    def list_for_model(self, model: Any = None, **kwargs: Any) -> List[FwTrackerEntry]:
        return self.list_history(model=model, **kwargs)

    def get_history_for_target(self, target: Any = None, **kwargs: Any) -> List[FwTrackerEntry]:
        return self.list_history(camera_id=target, **kwargs)

    def get_latest_for_camera(self, camera_id: Any = None, **kwargs: Any) -> Optional[FwTrackerEntry]:
        items = self.list_history(camera_id=camera_id, **kwargs)
        if not items:
            return None
        return items[-1]

    def get_latest_for_model(self, model: Any = None, **kwargs: Any) -> Optional[FwTrackerEntry]:
        items = self.list_history(model=model, **kwargs)
        if not items:
            return None
        return items[-1]

    def get_latest_for_target(self, target: Any = None, **kwargs: Any) -> Optional[FwTrackerEntry]:
        return self.get_latest_for_camera(target, **kwargs)

    def latest(self, camera_id: Any = None, **kwargs: Any) -> Optional[FwTrackerEntry]:
        if camera_id is None and kwargs.get("model") is not None:
            return self.get_latest_for_model(kwargs.pop("model"), **kwargs)
        return self.get_latest_for_camera(camera_id, **kwargs)

    def latest_for_camera(self, camera_id: Any = None, **kwargs: Any) -> Optional[FwTrackerEntry]:
        return self.get_latest_for_camera(camera_id, **kwargs)

    def latest_for_model(self, model: Any = None, **kwargs: Any) -> Optional[FwTrackerEntry]:
        return self.get_latest_for_model(model, **kwargs)

    # -- stats --
    def get_stats(self, camera_id: Any = None, **kwargs: Any) -> Dict[str, Any]:
        items = self.list_history(camera_id=camera_id, **kwargs) if camera_id is not None or kwargs else self.list_history(**kwargs)
        total = len(items)
        needs = sum(1 for e in items if bool(e.get("update_available", e.get("needs_update"))))
        up_to_date = sum(1 for e in items if bool(e.get("up_to_date")))
        return {
            "camera_id": _coerce_camera_id(camera_id) if camera_id is not None else None,
            "total": total,
            "count": total,
            "entries": total,
            "checks": total,
            "samples": total,
            "update_available": needs,
            "needs_update": needs,
            "up_to_date": up_to_date,
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
            total = len(FW_TRACKER)
            by_camera: Dict[str, int] = {}
            by_status: Dict[str, int] = {}
            by_model: Dict[str, int] = {}
            for e in FW_TRACKER.values():
                c = str(e.get("camera_id", "unknown"))
                by_camera[c] = by_camera.get(c, 0) + 1
                st = str(e.get("status", "unknown"))
                by_status[st] = by_status.get(st, 0) + 1
                m = str(e.get("model", "unknown"))
                by_model[m] = by_model.get(m, 0) + 1
        out = self.get_stats(*args, **kwargs)
        out.update({
            "status": "ok",
            "service": "fw_tracker",
            "by_camera": by_camera,
            "counts_by_camera": dict(by_camera),
            "by_status": by_status,
            "counts_by_status": dict(by_status),
            "by_model": by_model,
            "counts_by_model": dict(by_model),
        })
        return out

    # -- delete / clear --
    def delete_entry(self, entry_id: Any) -> bool:
        if entry_id is None:
            return False
        key = str(entry_id)
        with self._lock:
            if FW_TRACKER.pop(key, None) is not None:
                return True
            for k, e in list(FW_TRACKER.items()):
                for ak in ("entry_id", "history_id", "check_id", "record_id", "sample_id"):
                    if str(e.get(ak)) == key:
                        FW_TRACKER.pop(k, None)
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
                n = len(FW_TRACKER)
                FW_TRACKER.clear()
                return n
        targets = self.list_history(**filters)
        n = 0
        with self._lock:
            for t in targets:
                if FW_TRACKER.pop(str(t.get("id")), None) is not None:
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
            return len(FW_TRACKER)


fw_tracker_service = FwTrackerService()
firmware_tracker_service = fw_tracker_service
history_service = fw_tracker_service
service = fw_tracker_service
manager = fw_tracker_service
tracker_service = fw_tracker_service
tracker_manager = fw_tracker_service
history_manager = fw_tracker_service
store = fw_tracker_service
default_service = fw_tracker_service
check_service = fw_tracker_service


def record_sample(*args: Any, **kwargs: Any) -> FwTrackerEntry:
    return fw_tracker_service.record_sample(*args, **kwargs)


def record_history(*args: Any, **kwargs: Any) -> FwTrackerEntry:
    return fw_tracker_service.record_sample(*args, **kwargs)


def log_sample(*args: Any, **kwargs: Any) -> FwTrackerEntry:
    return fw_tracker_service.record_sample(*args, **kwargs)


def log_history(*args: Any, **kwargs: Any) -> FwTrackerEntry:
    return fw_tracker_service.record_sample(*args, **kwargs)


def record_result(*args: Any, **kwargs: Any) -> FwTrackerEntry:
    return fw_tracker_service.record_sample(*args, **kwargs)


def record_check(*args: Any, **kwargs: Any) -> FwTrackerEntry:
    return fw_tracker_service.record_sample(*args, **kwargs)


def log_check(*args: Any, **kwargs: Any) -> FwTrackerEntry:
    return fw_tracker_service.record_sample(*args, **kwargs)


def track(*args: Any, **kwargs: Any) -> FwTrackerEntry:
    return fw_tracker_service.record_sample(*args, **kwargs)


def track_check(*args: Any, **kwargs: Any) -> FwTrackerEntry:
    return fw_tracker_service.record_sample(*args, **kwargs)


def track_version(*args: Any, **kwargs: Any) -> FwTrackerEntry:
    return fw_tracker_service.record_sample(*args, **kwargs)


def record_version(*args: Any, **kwargs: Any) -> FwTrackerEntry:
    return fw_tracker_service.record_sample(*args, **kwargs)


def check_and_record(*args: Any, **kwargs: Any) -> FwTrackerEntry:
    return fw_tracker_service.record_sample(*args, **kwargs)


def snapshot_and_record(*args: Any, **kwargs: Any) -> FwTrackerEntry:
    return fw_tracker_service.snapshot_and_record(*args, **kwargs)


def capture_from_firmware(*args: Any, **kwargs: Any) -> FwTrackerEntry:
    return fw_tracker_service.snapshot_and_record(*args, **kwargs)


def sample_and_record(*args: Any, **kwargs: Any) -> FwTrackerEntry:
    return fw_tracker_service.snapshot_and_record(*args, **kwargs)


def add_entry(*args: Any, **kwargs: Any) -> FwTrackerEntry:
    return fw_tracker_service.record_sample(*args, **kwargs)


def create_entry(*args: Any, **kwargs: Any) -> FwTrackerEntry:
    return fw_tracker_service.record_sample(*args, **kwargs)


def get_entry(entry_id: Any) -> Optional[FwTrackerEntry]:
    return fw_tracker_service.get_entry(entry_id)


def list_history(*args: Any, **kwargs: Any) -> List[FwTrackerEntry]:
    return fw_tracker_service.list_history(*args, **kwargs)


def fetch_history(*args: Any, **kwargs: Any) -> List[FwTrackerEntry]:
    return fw_tracker_service.list_history(*args, **kwargs)


def list_entries(*args: Any, **kwargs: Any) -> List[FwTrackerEntry]:
    return fw_tracker_service.list_history(*args, **kwargs)


def fetch_entries(*args: Any, **kwargs: Any) -> List[FwTrackerEntry]:
    return fw_tracker_service.list_history(*args, **kwargs)


def list_checks(*args: Any, **kwargs: Any) -> List[FwTrackerEntry]:
    return fw_tracker_service.list_history(*args, **kwargs)


def get_history_for_camera(*args: Any, **kwargs: Any) -> List[FwTrackerEntry]:
    return fw_tracker_service.get_history_for_camera(*args, **kwargs)


def get_history_for_model(*args: Any, **kwargs: Any) -> List[FwTrackerEntry]:
    return fw_tracker_service.get_history_for_model(*args, **kwargs)


def get_history_for_target(*args: Any, **kwargs: Any) -> List[FwTrackerEntry]:
    return fw_tracker_service.get_history_for_target(*args, **kwargs)


def get_latest_for_camera(*args: Any, **kwargs: Any) -> Optional[FwTrackerEntry]:
    return fw_tracker_service.get_latest_for_camera(*args, **kwargs)


def get_latest_for_model(*args: Any, **kwargs: Any) -> Optional[FwTrackerEntry]:
    return fw_tracker_service.get_latest_for_model(*args, **kwargs)


def get_latest_for_target(*args: Any, **kwargs: Any) -> Optional[FwTrackerEntry]:
    return fw_tracker_service.get_latest_for_target(*args, **kwargs)


def get_stats(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return fw_tracker_service.get_stats(*args, **kwargs)


def get_summary(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return fw_tracker_service.get_stats(*args, **kwargs)


def delete_entry(entry_id: Any) -> bool:
    return fw_tracker_service.delete_entry(entry_id)


def clear_history(**filters: Any) -> int:
    return fw_tracker_service.clear_history(**filters)


def clear(**filters: Any) -> int:
    return fw_tracker_service.clear_history(**filters)


def status(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return fw_tracker_service.status(*args, **kwargs)


def count(**filters: Any) -> int:
    return fw_tracker_service.count(**filters)


# CSV / JSON helpers for history dumps.
DEFAULT_COLUMNS = [
    "id",
    "camera_id",
    "model",
    "current_version",
    "latest_version",
    "update_available",
    "up_to_date",
    "status",
    "source",
    "checked_at",
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
        entries = kwargs.pop("entries", kwargs.pop("items", kwargs.pop("logs", kwargs.pop("samples", kwargs.pop("checks", [])))))
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
        entries = kwargs.pop("entries", kwargs.pop("items", kwargs.pop("logs", kwargs.pop("samples", kwargs.pop("checks", [])))))
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
firmware_history_entries = list_history
fw_history_entries = list_history
samples = list_history
list_samples = list_history
checks = list_history
latest = get_latest_for_camera
history_for = get_history_for_camera
summary = get_summary
stats = get_stats
get_status = status
build_csv = to_csv
export_csv = to_csv
build_json = to_json
export_json = to_json

FwTrackerServiceClass = FwTrackerService
FwTrackerManager = FwTrackerService
FirmwareTrackerService = FwTrackerService
FirmwareHistoryService = FwTrackerService
HistoryService = FwTrackerService
HistoryManager = FwTrackerService
TrackerService = FwTrackerService

__all__ = [
    "FW_TRACKER",
    "FwTrackerEntry",
    "FwTrackerService",
    "fw_tracker_service",
    "record_sample",
    "record_history",
    "log_sample",
    "log_history",
    "record_check",
    "log_check",
    "track",
    "track_check",
    "track_version",
    "record_version",
    "check_and_record",
    "snapshot_and_record",
    "capture_from_firmware",
    "sample_and_record",
    "add_entry",
    "create_entry",
    "get_entry",
    "list_history",
    "fetch_history",
    "get_history_for_camera",
    "get_history_for_model",
    "get_history_for_target",
    "get_latest_for_camera",
    "get_latest_for_model",
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
