"""Tamper drift history service for VigilCam.

Complements ``app.services.tamper`` which is point-in-time only (verified:
``tamper`` keeps ``_REFERENCES`` keyed by camera id with the last-known
baseline ``{histogram, captured_at}`` and ``compare_frame`` returns a single
``drift`` float per call with no append log, no deque, no time series,
no history tracking).

This module stores every recorded tamper drift sample as an independent
history entry (append-only, per-camera time series) so operators can track
drift over time, compute per-camera averages/min/max, count tampered
samples, and export CSV/JSON.

All helpers are sync and dependency-free so they work in tests without a
database. Tamper integration is lazy (imports ``app.services.tamper``
only inside compare helpers) to avoid import cycles.
"""

from __future__ import annotations

import csv
import io
import json
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

DRIFT_HISTORY: Dict[str, "DriftHistoryEntry"] = {}
drift_history = DRIFT_HISTORY
history = DRIFT_HISTORY
HISTORY = DRIFT_HISTORY
STORE = DRIFT_HISTORY
ENTRIES = DRIFT_HISTORY
DRIFT_HISTORY_STORE = DRIFT_HISTORY
SAMPLES = DRIFT_HISTORY
LOGS = DRIFT_HISTORY
TAMPER_DRIFT_HISTORY = DRIFT_HISTORY
TAMPER_HISTORY = DRIFT_HISTORY

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
        try:
            s = str(value).strip()
        except Exception:  # noqa: BLE001
            return None
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


def _coerce_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on", "tampered", "t")
    return bool(value)


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
                       "entry", "sample", "result", "drift", "tamper", "attributes", "camera"):
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


class DriftHistoryEntry(dict):
    """Dict-like tamper drift history record with attribute access."""

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


DriftHistoryRecord = DriftHistoryEntry
HistoryEntry = DriftHistoryEntry
DriftSampleRecord = DriftHistoryEntry
DriftHistoryModel = DriftHistoryEntry
DriftHistoryItem = DriftHistoryEntry
HistoryRecord = DriftHistoryEntry
SampleEntry = DriftHistoryEntry
TamperDriftEntry = DriftHistoryEntry
TamperHistoryEntry = DriftHistoryEntry


def _build_entry(data: Dict[str, Any]) -> DriftHistoryEntry:
    camera_raw = _pick(data, "camera_id", "camera", "cam", "cameraId", "cam_id", "id")
    if camera_raw is None:
        target_raw = _pick(data, "target", "stream", "name", "label")
        if target_raw is not None:
            camera_raw = target_raw
    cid = _coerce_camera_id(camera_raw)
    if cid is None:
        raise ValueError("camera_id is required")

    drift_raw = _pick(data, "drift", "distance", "score", "value", "drift_value",
                      "drift_score", "tamper_drift", "histogram_drift")
    drift_f = _coerce_float(drift_raw, None)
    if drift_f is not None:
        drift_f = max(0.0, min(1.0, float(drift_f)))

    threshold_raw = _pick(data, "threshold", "drift_threshold", "sensitivity",
                          "limit", "tolerance")
    threshold_f = _coerce_float(threshold_raw, 0.35)
    if threshold_f is None:
        threshold_f = 0.35
    threshold_f = max(0.0, min(1.0, float(threshold_f)))

    tampered_raw = _pick(data, "tampered", "tamper", "tamper_detected",
                         "is_tampered", "tampering", "detected")
    if tampered_raw is None:
        if drift_f is not None:
            tampered = bool(drift_f > threshold_f)
        else:
            compared_raw = _pick(data, "compared", "has_reference")
            tampered = False
            _ = compared_raw
    else:
        tampered = _coerce_bool(tampered_raw, False)

    compared_raw = _pick(data, "compared", "has_reference")
    compared = _coerce_bool(compared_raw, drift_f is not None) if compared_raw is not None else (drift_f is not None)

    reason = _pick(data, "reason", "message", "detail", "note")
    reason_s = str(reason) if reason is not None else None
    mean = _coerce_float(_pick(data, "mean", "mean_brightness", "brightness"), None)
    reference_mean = _coerce_float(_pick(data, "reference_mean", "baseline_mean"), None)

    sampled = _pick(data, "sampled_at", "checked_at", "sample_time", "measured_at",
                    "timestamp", "ts", "time", "date", "when", "at", "recorded_at",
                    "compared_at", "created_at")
    # created_at doubles as sampled_at when no explicit sample time given.
    sampled_s = _json_safe(sampled) if sampled is not None else None
    if sampled_s is None:
        sampled_s = _now_iso()
    elif not isinstance(sampled_s, str):
        sampled_s = str(sampled_s)

    now_raw = _pick(data, "created_at", "createdAt")
    if isinstance(now_raw, str):
        now_s = now_raw
    else:
        now_s = _json_safe(now_raw) if now_raw is not None else None
        if not isinstance(now_s, str):
            now_s = _now_iso()

    source = _pick(data, "source", "origin", "recorded_by") or "manual"
    source = str(source)
    error = _pick(data, "error", "failure")
    error_s = str(error) if error is not None else None

    entry_id = _pick(data, "entry_id", "history_id", "sample_id", "record_id", "id") or str(uuid.uuid4())
    # Generic "id" from tamper compare payloads is the camera id, not an
    # entry id -- never reuse it as the history entry key.
    try:
        if str(entry_id) == str(cid):
            entry_id = str(uuid.uuid4())
    except Exception:  # noqa: BLE001
        entry_id = str(uuid.uuid4())
    entry_id = str(entry_id)

    entry = DriftHistoryEntry()
    entry["id"] = entry_id
    entry["entry_id"] = entry_id
    entry["history_id"] = entry_id
    entry["sample_id"] = entry_id
    entry["record_id"] = entry_id
    entry["camera_id"] = cid
    entry["camera"] = cid
    entry["cam"] = cid
    entry["id_camera"] = cid
    entry["target"] = f"camera:{cid}"
    entry["drift"] = drift_f
    entry["distance"] = drift_f
    entry["score"] = drift_f
    entry["value"] = drift_f
    entry["drift_value"] = drift_f
    entry["threshold"] = threshold_f
    entry["drift_threshold"] = threshold_f
    entry["tampered"] = bool(tampered)
    entry["tamper"] = bool(tampered)
    entry["tamper_detected"] = bool(tampered)
    entry["is_tampered"] = bool(tampered)
    entry["detected"] = bool(tampered)
    entry["compared"] = bool(compared)
    entry["has_reference"] = bool(compared) if compared_raw is None else bool(compared)
    entry["reason"] = reason_s
    entry["message"] = reason_s
    entry["mean"] = mean
    entry["mean_brightness"] = mean
    entry["reference_mean"] = reference_mean
    entry["sampled_at"] = sampled_s
    entry["checked_at"] = sampled_s
    entry["measured_at"] = sampled_s
    entry["compared_at"] = sampled_s
    entry["timestamp"] = sampled_s
    entry["created_at"] = now_s
    entry["recorded_at"] = sampled_s if _pick(data, "recorded_at") is not None else now_s
    entry["updated_at"] = now_s
    entry["source"] = source
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
        if len(DRIFT_HISTORY) <= MAX_HISTORY_ENTRIES:
            if camera_id is not None:
                per = [e for e in DRIFT_HISTORY.values() if e.get("camera_id") == camera_id]
                if len(per) <= MAX_PER_CAMERA:
                    return
                per_sorted = sorted(per, key=lambda e: str(e.get("created_at", "")))
                drop = len(per_sorted) - MAX_PER_CAMERA
                for e in per_sorted[:drop]:
                    DRIFT_HISTORY.pop(str(e.get("id")), None)
            return
        ordered = sorted(DRIFT_HISTORY.values(), key=lambda e: str(e.get("created_at", "")))
        drop = len(ordered) - MAX_HISTORY_ENTRIES
        for e in ordered[:drop]:
            DRIFT_HISTORY.pop(str(e.get("id")), None)
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
    min_drift = filters.pop("min_drift", filters.pop("minDrift", filters.pop("min_score", None)))
    max_drift = filters.pop("max_drift", filters.pop("maxDrift", filters.pop("max_score", None)))
    try:
        min_f = float(min_drift) if min_drift is not None else None
    except (TypeError, ValueError):
        min_f = None
    try:
        max_f = float(max_drift) if max_drift is not None else None
    except (TypeError, ValueError):
        max_f = None
    tampered_f = filters.pop("tampered", filters.pop("tamper", filters.pop("tamper_detected", filters.pop("detected", None))))
    if tampered_f is not None and isinstance(tampered_f, str):
        tampered_f = tampered_f.strip().lower() in ("true", "1", "yes", "on", "tampered", "t")

    alias_map = {
        "camera": ("camera_id",),
        "cam": ("camera_id",),
        "cameraId": ("camera_id",),
        "distance": ("drift",),
        "score": ("drift",),
        "value": ("drift",),
        "state": ("tampered",),
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
                    "camera_id", "camera", "tampered", "tamper",
                    "id", "entry_id", "history_id", "sample_id",
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
        if tampered_f is not None:
            if bool(ev.get("tampered")) != bool(tampered_f):
                continue
        if min_f is not None or max_f is not None:
            d = ev.get("drift")
            if d is None:
                continue
            try:
                dv = float(d)
            except (TypeError, ValueError):
                continue
            if min_f is not None and dv < min_f:
                continue
            if max_f is not None and dv > max_f:
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


class DriftHistoryService:
    """Append-only in-memory tamper drift history."""

    def __init__(self) -> None:
        self.store = DRIFT_HISTORY
        self.history = DRIFT_HISTORY
        self.entries = DRIFT_HISTORY
        self.samples = DRIFT_HISTORY
        self._lock = _lock

    # -- record --
    def record_sample(self, *args: Any, **kwargs: Any) -> DriftHistoryEntry:
        data = _normalize_entry_data(*args, **kwargs)
        entry = _build_entry(data)
        with self._lock:
            DRIFT_HISTORY[entry["id"]] = entry
            _prune_if_needed(entry.get("camera_id"))
        return entry

    def record_history(self, *args: Any, **kwargs: Any) -> DriftHistoryEntry:
        return self.record_sample(*args, **kwargs)

    def log_sample(self, *args: Any, **kwargs: Any) -> DriftHistoryEntry:
        return self.record_sample(*args, **kwargs)

    def log_history(self, *args: Any, **kwargs: Any) -> DriftHistoryEntry:
        return self.record_sample(*args, **kwargs)

    def record_result(self, *args: Any, **kwargs: Any) -> DriftHistoryEntry:
        return self.record_sample(*args, **kwargs)

    def log_result(self, *args: Any, **kwargs: Any) -> DriftHistoryEntry:
        return self.record_sample(*args, **kwargs)

    def record_drift(self, *args: Any, **kwargs: Any) -> DriftHistoryEntry:
        return self.record_sample(*args, **kwargs)

    def log_drift(self, *args: Any, **kwargs: Any) -> DriftHistoryEntry:
        return self.record_sample(*args, **kwargs)

    def add_entry(self, *args: Any, **kwargs: Any) -> DriftHistoryEntry:
        return self.record_sample(*args, **kwargs)

    def create_entry(self, *args: Any, **kwargs: Any) -> DriftHistoryEntry:
        return self.record_sample(*args, **kwargs)

    def add(self, *args: Any, **kwargs: Any) -> DriftHistoryEntry:
        return self.record_sample(*args, **kwargs)

    def create(self, *args: Any, **kwargs: Any) -> DriftHistoryEntry:
        return self.record_sample(*args, **kwargs)

    def log(self, *args: Any, **kwargs: Any) -> DriftHistoryEntry:
        return self.record_sample(*args, **kwargs)

    def append(self, *args: Any, **kwargs: Any) -> DriftHistoryEntry:
        return self.record_sample(*args, **kwargs)

    def record(self, *args: Any, **kwargs: Any) -> DriftHistoryEntry:
        return self.record_sample(*args, **kwargs)

    def push(self, *args: Any, **kwargs: Any) -> DriftHistoryEntry:
        return self.record_sample(*args, **kwargs)

    def compare_and_record(
        self,
        camera_id: Any = None,
        frame: Any = None,
        threshold: Any = None,
        **kwargs: Any,
    ) -> DriftHistoryEntry:
        """Run point-in-time ``tamper.compare_frame`` then append drift history."""
        from app.services import tamper as _tamper  # lazy to avoid cycles

        if camera_id is None:
            camera_id = kwargs.pop("camera_id", kwargs.pop("camera", kwargs.pop("cam", kwargs.pop("id", kwargs.pop("target", None)))))
        cid = _coerce_camera_id(camera_id)
        if cid is None:
            raise ValueError("camera_id is required")
        if frame is None:
            for key in ("frame", "pixels", "data", "image", "frame_data", "histogram",
                        "values", "samples", "bytes", "raw", "content", "base64", "b64"):
                if kwargs.get(key) is not None:
                    frame = kwargs.pop(key)
                    break
        compare_kwargs: Dict[str, Any] = {}
        if threshold is not None:
            compare_kwargs["threshold"] = threshold
        for key in ("threshold", "drift_threshold", "sensitivity", "limit",
                    "tolerance", "bins", "num_bins", "histogram_bins"):
            if kwargs.get(key) is not None and key not in compare_kwargs:
                compare_kwargs[key] = kwargs.get(key)
        result = _tamper.compare_frame(cid, frame, **compare_kwargs)
        payload: Dict[str, Any] = dict(result) if isinstance(result, dict) else {}
        payload["camera_id"] = cid
        payload.setdefault("source", "compare")
        # Preserve explicit overrides (e.g. caller-supplied sampled_at/source).
        for k, v in kwargs.items():
            if v is not None and k not in ("frame", "pixels", "data"):
                payload.setdefault(k, v)
        return self.record_sample(payload)

    def snapshot_and_record(self, *args: Any, **kwargs: Any) -> DriftHistoryEntry:
        return self.compare_and_record(*args, **kwargs)

    def sample_and_record(self, *args: Any, **kwargs: Any) -> DriftHistoryEntry:
        # If a frame is present, compare via tamper; otherwise record raw drift.
        data = _normalize_entry_data(*args, **kwargs)
        has_frame = any(
            data.get(k) is not None
            for k in ("frame", "pixels", "data", "image", "frame_data", "histogram",
                      "values", "samples", "bytes", "raw", "content", "base64", "b64")
        )
        has_drift = any(
            data.get(k) is not None
            for k in ("drift", "distance", "score", "value", "drift_value", "tampered", "tamper")
        )
        if has_frame and not has_drift:
            try:
                return self.compare_and_record(**data)
            except Exception:
                pass
        return self.record_sample(data)

    def capture_from_tamper(self, *args: Any, **kwargs: Any) -> DriftHistoryEntry:
        return self.compare_and_record(*args, **kwargs)

    def record_compare(self, *args: Any, **kwargs: Any) -> DriftHistoryEntry:
        return self.compare_and_record(*args, **kwargs)

    def log_compare(self, *args: Any, **kwargs: Any) -> DriftHistoryEntry:
        return self.compare_and_record(*args, **kwargs)

    # -- get --
    def get_entry(self, entry_id: Any) -> Optional[DriftHistoryEntry]:
        if entry_id is None:
            return None
        key = str(entry_id)
        with self._lock:
            found = DRIFT_HISTORY.get(key)
            if found is not None:
                return found
            for e in DRIFT_HISTORY.values():
                for ak in ("entry_id", "history_id", "sample_id", "record_id"):
                    if str(e.get(ak)) == key:
                        return e
        return None

    def get(self, entry_id: Any) -> Optional[DriftHistoryEntry]:
        return self.get_entry(entry_id)

    def fetch_entry(self, entry_id: Any) -> Optional[DriftHistoryEntry]:
        return self.get_entry(entry_id)

    def fetch(self, entry_id: Any) -> Optional[DriftHistoryEntry]:
        return self.get_entry(entry_id)

    def find(self, entry_id: Any) -> Optional[DriftHistoryEntry]:
        return self.get_entry(entry_id)

    def get_sample(self, entry_id: Any) -> Optional[DriftHistoryEntry]:
        return self.get_entry(entry_id)

    # -- list --
    def list_history(
        self,
        camera_id: Any = None,
        tampered: Any = None,
        limit: Any = None,
        offset: Any = None,
        skip: Any = None,
        **filters: Any,
    ) -> List[DriftHistoryEntry]:
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
        if tampered is None:
            for ak in ("tamper", "tamper_detected", "detected", "is_tampered"):
                if filters.get(ak) is not None:
                    tampered = filters.pop(ak)
                    break
        with self._lock:
            items = [_entry_to_dict(e) for e in list(DRIFT_HISTORY.values())]
        query: Dict[str, Any] = {}
        if camera_id is not None:
            try:
                query["camera_id"] = int(str(camera_id).split(":")[-1])
            except (TypeError, ValueError):
                query["camera_id"] = camera_id
        if tampered is not None:
            query["tampered"] = tampered
        query.update({k: v for k, v in filters.items() if v is not None})
        return _apply_filters(items, limit=limit, skip=skip if skip is not None else offset, **query)  # type: ignore[return-value]

    def list(self, *args: Any, **kwargs: Any) -> List[DriftHistoryEntry]:
        return self.list_history(*args, **kwargs)

    def get_history(self, *args: Any, **kwargs: Any) -> List[DriftHistoryEntry]:
        return self.list_history(*args, **kwargs)

    def fetch_history(self, *args: Any, **kwargs: Any) -> List[DriftHistoryEntry]:
        return self.list_history(*args, **kwargs)

    def get_entries(self, *args: Any, **kwargs: Any) -> List[DriftHistoryEntry]:
        return self.list_history(*args, **kwargs)

    def fetch_entries(self, *args: Any, **kwargs: Any) -> List[DriftHistoryEntry]:
        return self.list_history(*args, **kwargs)

    def all(self, *args: Any, **kwargs: Any) -> List[DriftHistoryEntry]:
        return self.list_history(*args, **kwargs)

    def list_samples(self, *args: Any, **kwargs: Any) -> List[DriftHistoryEntry]:
        return self.list_history(*args, **kwargs)

    def get_samples(self, *args: Any, **kwargs: Any) -> List[DriftHistoryEntry]:
        return self.list_history(*args, **kwargs)

    def history_for(self, camera_id: Any = None, **kwargs: Any) -> List[DriftHistoryEntry]:
        return self.list_history(camera_id=camera_id, **kwargs)

    def get_history_for_camera(self, camera_id: Any = None, **kwargs: Any) -> List[DriftHistoryEntry]:
        return self.list_history(camera_id=camera_id, **kwargs)

    def list_for_camera(self, camera_id: Any = None, **kwargs: Any) -> List[DriftHistoryEntry]:
        return self.list_history(camera_id=camera_id, **kwargs)

    def get_history_for_target(self, target: Any = None, **kwargs: Any) -> List[DriftHistoryEntry]:
        return self.list_history(camera_id=target, **kwargs)

    def get_latest_for_camera(self, camera_id: Any = None, **kwargs: Any) -> Optional[DriftHistoryEntry]:
        items = self.list_history(camera_id=camera_id, **kwargs)
        if not items:
            return None
        return items[-1]

    def get_latest_for_target(self, target: Any = None, **kwargs: Any) -> Optional[DriftHistoryEntry]:
        return self.get_latest_for_camera(target, **kwargs)

    def latest(self, camera_id: Any = None, **kwargs: Any) -> Optional[DriftHistoryEntry]:
        return self.get_latest_for_camera(camera_id, **kwargs)

    def latest_for_camera(self, camera_id: Any = None, **kwargs: Any) -> Optional[DriftHistoryEntry]:
        return self.get_latest_for_camera(camera_id, **kwargs)

    # -- stats --
    def get_stats(self, camera_id: Any = None, **kwargs: Any) -> Dict[str, Any]:
        items = self.list_history(camera_id=camera_id, **kwargs) if camera_id is not None or kwargs else self.list_history(**kwargs)
        total = len(items)
        tampered_n = sum(1 for e in items if bool(e.get("tampered")))
        clean = total - tampered_n
        vals: List[float] = []
        for e in items:
            try:
                if e.get("drift") is None:
                    continue
                vals.append(float(e.get("drift")))
            except (TypeError, ValueError):
                continue
        if vals:
            avg_drift = sum(vals) / len(vals)
            max_drift = max(vals)
            min_drift = min(vals)
        else:
            avg_drift = max_drift = min_drift = 0.0
        cid = _coerce_camera_id(camera_id) if camera_id is not None else None
        return {
            "camera_id": cid,
            "camera": cid,
            "total": total,
            "count": total,
            "entries": total,
            "samples": total,
            "tampered": tampered_n,
            "tampered_count": tampered_n,
            "clean": clean,
            "clean_count": clean,
            "tamper_rate": round(tampered_n / total, 4) if total else 0.0,
            "avg_drift": round(avg_drift, 6),
            "avg_drift_value": round(avg_drift, 6),
            "max_drift": round(max_drift, 6),
            "min_drift": round(min_drift, 6),
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
            total = len(DRIFT_HISTORY)
            by_camera: Dict[str, int] = {}
            by_result: Dict[str, int] = {}
            for e in DRIFT_HISTORY.values():
                c = str(e.get("camera_id", "unknown"))
                by_camera[c] = by_camera.get(c, 0) + 1
                k = "tampered" if bool(e.get("tampered")) else "clean"
                by_result[k] = by_result.get(k, 0) + 1
        out = self.get_stats(*args, **kwargs)
        out.update({
            "status": "ok",
            "service": "drift_history",
            "by_camera": by_camera,
            "counts_by_camera": dict(by_camera),
            "by_result": by_result,
            "counts_by_result": dict(by_result),
        })
        return out

    # -- delete / clear --
    def delete_entry(self, entry_id: Any) -> bool:
        if entry_id is None:
            return False
        key = str(entry_id)
        with self._lock:
            if DRIFT_HISTORY.pop(key, None) is not None:
                return True
            for k, e in list(DRIFT_HISTORY.items()):
                for ak in ("entry_id", "history_id", "sample_id", "record_id"):
                    if str(e.get(ak)) == key:
                        DRIFT_HISTORY.pop(k, None)
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
                n = len(DRIFT_HISTORY)
                DRIFT_HISTORY.clear()
                return n
        targets = self.list_history(**filters)
        n = 0
        with self._lock:
            for t in targets:
                if DRIFT_HISTORY.pop(str(t.get("id")), None) is not None:
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
            return len(DRIFT_HISTORY)


drift_history_service = DriftHistoryService()
history_service = drift_history_service
service = drift_history_service
manager = drift_history_service
history_manager = drift_history_service
store = drift_history_service
default_service = drift_history_service
sample_service = drift_history_service
tamper_drift_service = drift_history_service
drift_service = drift_history_service


def record_sample(*args: Any, **kwargs: Any) -> DriftHistoryEntry:
    return drift_history_service.record_sample(*args, **kwargs)


def record_history(*args: Any, **kwargs: Any) -> DriftHistoryEntry:
    return drift_history_service.record_sample(*args, **kwargs)


def log_sample(*args: Any, **kwargs: Any) -> DriftHistoryEntry:
    return drift_history_service.record_sample(*args, **kwargs)


def log_history(*args: Any, **kwargs: Any) -> DriftHistoryEntry:
    return drift_history_service.record_sample(*args, **kwargs)


def record_result(*args: Any, **kwargs: Any) -> DriftHistoryEntry:
    return drift_history_service.record_sample(*args, **kwargs)


def record_drift(*args: Any, **kwargs: Any) -> DriftHistoryEntry:
    return drift_history_service.record_sample(*args, **kwargs)


def log_drift(*args: Any, **kwargs: Any) -> DriftHistoryEntry:
    return drift_history_service.record_sample(*args, **kwargs)


def add_entry(*args: Any, **kwargs: Any) -> DriftHistoryEntry:
    return drift_history_service.record_sample(*args, **kwargs)


def create_entry(*args: Any, **kwargs: Any) -> DriftHistoryEntry:
    return drift_history_service.record_sample(*args, **kwargs)


def compare_and_record(*args: Any, **kwargs: Any) -> DriftHistoryEntry:
    return drift_history_service.compare_and_record(*args, **kwargs)


def sample_and_record(*args: Any, **kwargs: Any) -> DriftHistoryEntry:
    return drift_history_service.sample_and_record(*args, **kwargs)


def snapshot_and_record(*args: Any, **kwargs: Any) -> DriftHistoryEntry:
    return drift_history_service.compare_and_record(*args, **kwargs)


def get_entry(entry_id: Any) -> Optional[DriftHistoryEntry]:
    return drift_history_service.get_entry(entry_id)


def list_history(*args: Any, **kwargs: Any) -> List[DriftHistoryEntry]:
    return drift_history_service.list_history(*args, **kwargs)


def fetch_history(*args: Any, **kwargs: Any) -> List[DriftHistoryEntry]:
    return drift_history_service.list_history(*args, **kwargs)


def list_entries(*args: Any, **kwargs: Any) -> List[DriftHistoryEntry]:
    return drift_history_service.list_history(*args, **kwargs)


def fetch_entries(*args: Any, **kwargs: Any) -> List[DriftHistoryEntry]:
    return drift_history_service.list_history(*args, **kwargs)


def get_history_for_camera(*args: Any, **kwargs: Any) -> List[DriftHistoryEntry]:
    return drift_history_service.get_history_for_camera(*args, **kwargs)


def get_history_for_target(*args: Any, **kwargs: Any) -> List[DriftHistoryEntry]:
    return drift_history_service.get_history_for_target(*args, **kwargs)


def get_latest_for_camera(*args: Any, **kwargs: Any) -> Optional[DriftHistoryEntry]:
    return drift_history_service.get_latest_for_camera(*args, **kwargs)


def get_latest_for_target(*args: Any, **kwargs: Any) -> Optional[DriftHistoryEntry]:
    return drift_history_service.get_latest_for_target(*args, **kwargs)


def get_stats(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return drift_history_service.get_stats(*args, **kwargs)


def get_summary(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return drift_history_service.get_stats(*args, **kwargs)


def delete_entry(entry_id: Any) -> bool:
    return drift_history_service.delete_entry(entry_id)


def clear_history(**filters: Any) -> int:
    return drift_history_service.clear_history(**filters)


def clear(**filters: Any) -> int:
    return drift_history_service.clear_history(**filters)


def status(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return drift_history_service.status(*args, **kwargs)


def count(**filters: Any) -> int:
    return drift_history_service.count(**filters)


# CSV / JSON helpers for history dumps.
DEFAULT_COLUMNS = [
    "id",
    "camera_id",
    "drift",
    "threshold",
    "tampered",
    "compared",
    "reason",
    "mean",
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
drift_history_entries = list_history
tamper_drift_entries = list_history
samples = list_history
list_samples = list_history
latest = get_latest_for_camera
history_for = get_history_for_camera
summary = get_summary
stats = get_stats
get_status = status
record_compare = compare_and_record
log_compare = compare_and_record
build_csv = to_csv
export_csv = to_csv
build_json = to_json
export_json = to_json

DriftHistoryServiceClass = DriftHistoryService
DriftHistoryManager = DriftHistoryService
HistoryService = DriftHistoryService
HistoryManager = DriftHistoryService
TamperDriftHistoryService = DriftHistoryService

__all__ = [
    "DRIFT_HISTORY",
    "DriftHistoryEntry",
    "DriftHistoryService",
    "drift_history_service",
    "record_sample",
    "record_history",
    "log_sample",
    "log_history",
    "record_drift",
    "log_drift",
    "compare_and_record",
    "sample_and_record",
    "snapshot_and_record",
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
