"""Bitrate history service for VigilCam.

Complements ``app.services.bitrate`` which samples point-in-time stream
throughput only (verified: ``bitrate`` keeps ``_RESULTS`` keyed by
normalized target with the last-known ``BitrateResult`` and has no history
tracking, no append log, no deque, no time series).

This module stores every recorded bitrate sample as an independent history
entry (append-only, per-target time series) so operators can track bitrate
over time, compute per-target averages/min/max, and export CSV/JSON.

All helpers are sync and dependency-free so they work in tests without a
database. Sampling integration is lazy (imports ``app.services.bitrate``
only inside ``sample_and_record``) to avoid import cycles.
"""

from __future__ import annotations

import csv
import io
import json
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

BITRATE_HISTORY: Dict[str, "BitrateHistoryEntry"] = {}
bitrate_history = BITRATE_HISTORY
history = BITRATE_HISTORY
HISTORY = BITRATE_HISTORY
STORE = BITRATE_HISTORY
ENTRIES = BITRATE_HISTORY
BITRATE_HISTORY_STORE = BITRATE_HISTORY
SAMPLES = BITRATE_HISTORY
LOGS = BITRATE_HISTORY

_lock = threading.Lock()
_jobs_lock = _lock

MAX_HISTORY_ENTRIES = 5000
MAX_PER_TARGET = 500


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


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_target(value: Any) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"camera:{int(value)}"
    s = str(value).strip()
    return s or "unknown"


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
                       "entry", "sample", "result", "bitrate", "attributes"):
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


def _compute_rates(bytes_received: float, duration_seconds: float):
    try:
        n = float(bytes_received)
    except (TypeError, ValueError):
        n = 0.0
    if n < 0:
        n = 0.0
    try:
        d = float(duration_seconds)
    except (TypeError, ValueError):
        d = 0.0
    if d <= 0:
        return 0.0, 0.0, 0.0
    bps = (n * 8.0) / d
    return bps, bps / 1000.0, bps / 1_000_000.0


class BitrateHistoryEntry(dict):
    """Dict-like bitrate history record with attribute access."""

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


BitrateHistoryRecord = BitrateHistoryEntry
HistoryEntry = BitrateHistoryEntry
BitrateSampleRecord = BitrateHistoryEntry
BitrateHistoryModel = BitrateHistoryEntry
BitrateHistoryItem = BitrateHistoryEntry
HistoryRecord = BitrateHistoryEntry
SampleEntry = BitrateHistoryEntry


def _build_entry(data: Dict[str, Any]) -> BitrateHistoryEntry:
    entry_id = _pick(data, "id", "entry_id", "history_id", "sample_id", "record_id") or str(uuid.uuid4())
    entry_id = str(entry_id)
    target_raw = _pick(data, "target", "stream", "name", "label")
    url_raw = _pick(data, "url", "stream_url", "source", "streamUrl", "rtsp_url")
    camera_raw = _pick(data, "camera_id", "camera", "cam", "cameraId", "cam_id")
    if target_raw is None:
        if camera_raw is not None:
            try:
                target_raw = f"camera:{int(str(camera_raw).strip())}" if str(camera_raw).strip().isdigit() else str(camera_raw)
            except Exception:
                target_raw = str(camera_raw)
        elif url_raw is not None:
            target_raw = str(url_raw).strip()
        else:
            target_raw = "unknown"
    target = _normalize_target(target_raw)
    url = str(url_raw).strip() if url_raw is not None and str(url_raw).strip() else None

    nbytes = _pick(data, "bytes_received", "bytes", "total_bytes", "size_bytes", "size", "nbytes")
    try:
        nbytes_i = max(0, int(float(nbytes))) if nbytes is not None else 0
    except (TypeError, ValueError):
        nbytes_i = 0
    duration = _pick(data, "duration_seconds", "duration", "sample_seconds", "sample_duration", "elapsed", "elapsed_seconds")
    duration_f = _coerce_float(duration, 2.0)
    if duration_f <= 0:
        duration_f = 2.0

    bps = _pick(data, "bitrate_bps", "bps", "bitrate", "bits_per_second")
    kbps = _pick(data, "bitrate_kbps", "kbps")
    mbps = _pick(data, "bitrate_mbps", "mbps")
    try:
        bps_f = float(bps) if bps is not None else None
    except (TypeError, ValueError):
        bps_f = None
    if bps_f is None and kbps is not None:
        try:
            bps_f = float(kbps) * 1000.0
        except (TypeError, ValueError):
            bps_f = None
    if bps_f is None and mbps is not None:
        try:
            bps_f = float(mbps) * 1_000_000.0
        except (TypeError, ValueError):
            bps_f = None
    if bps_f is None:
        bps_f, _k, _m = _compute_rates(nbytes_i, duration_f)
    bps_f = max(0.0, float(bps_f))
    kbps_f = round(bps_f / 1000.0, 3)
    mbps_f = round(bps_f / 1_000_000.0, 6)
    bps_f = round(bps_f, 2)

    status = _pick(data, "status", "state", "stage", "result") or "measured"
    status = str(status)
    error = _pick(data, "error", "message", "failure")
    error_s = str(error) if error is not None else None
    sampled = _pick(data, "sampled_at", "checked_at", "sample_time", "measured_at", "timestamp", "ts", "time", "date")
    sampled_s = _json_safe(sampled) if sampled is not None else None
    if sampled_s is None:
        sampled_s = _now_iso()
    elif not isinstance(sampled_s, str):
        sampled_s = str(sampled_s)
    now_s = _pick(data, "created_at", "createdAt", "recorded_at", "recordedAt") or _now_iso()
    if not isinstance(now_s, str):
        now_s = _json_safe(now_s)
        if not isinstance(now_s, str):
            now_s = _now_iso()

    ok_val = _pick(data, "ok", "success", "succeeded", "healthy", "reachable")
    if ok_val is None:
        ok_val = str(status).lower() == "measured"

    camera_id = camera_raw
    # If target looks like camera:N, expose numeric camera_id too.
    if camera_id is None and target.startswith("camera:"):
        try:
            camera_id = int(target.split(":", 1)[1])
        except (ValueError, IndexError):
            camera_id = target.split(":", 1)[1] if ":" in target else None

    entry = BitrateHistoryEntry()
    entry["id"] = entry_id
    entry["entry_id"] = entry_id
    entry["history_id"] = entry_id
    entry["sample_id"] = entry_id
    entry["record_id"] = entry_id
    entry["target"] = target
    entry["stream"] = target
    entry["name"] = target
    entry["url"] = url
    entry["stream_url"] = url
    entry["source"] = url
    entry["camera_id"] = camera_id
    entry["camera"] = camera_id
    entry["bytes_received"] = nbytes_i
    entry["bytes"] = nbytes_i
    entry["total_bytes"] = nbytes_i
    entry["size_bytes"] = nbytes_i
    entry["duration_seconds"] = duration_f
    entry["duration"] = duration_f
    entry["sample_seconds"] = duration_f
    entry["bitrate_bps"] = bps_f
    entry["bps"] = bps_f
    entry["bitrate"] = bps_f
    entry["bitrate_kbps"] = kbps_f
    entry["kbps"] = kbps_f
    entry["bitrate_mbps"] = mbps_f
    entry["mbps"] = mbps_f
    entry["status"] = status
    entry["state"] = status
    entry["ok"] = bool(ok_val)
    entry["success"] = bool(ok_val)
    entry["error"] = error_s
    entry["message"] = error_s
    entry["sampled_at"] = sampled_s
    entry["checked_at"] = sampled_s
    entry["measured_at"] = sampled_s
    entry["timestamp"] = sampled_s
    entry["created_at"] = now_s
    entry["recorded_at"] = now_s
    entry["updated_at"] = now_s
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


def _prune_if_needed(target: Optional[str] = None) -> None:
    try:
        if len(BITRATE_HISTORY) <= MAX_HISTORY_ENTRIES:
            if target:
                per = [e for e in BITRATE_HISTORY.values() if str(e.get("target")) == str(target)]
                if len(per) <= MAX_PER_TARGET:
                    return
                per_sorted = sorted(per, key=lambda e: str(e.get("created_at", "")))
                drop = len(per_sorted) - MAX_PER_TARGET
                for e in per_sorted[:drop]:
                    BITRATE_HISTORY.pop(str(e.get("id")), None)
            return
        # Global overflow: drop oldest entries first.
        ordered = sorted(BITRATE_HISTORY.values(), key=lambda e: str(e.get("created_at", "")))
        drop = len(ordered) - MAX_HISTORY_ENTRIES
        for e in ordered[:drop]:
            BITRATE_HISTORY.pop(str(e.get("id")), None)
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
    min_bps = filters.pop("min_bps", filters.pop("minBps", filters.pop("min_bitrate", None)))
    max_bps = filters.pop("max_bps", filters.pop("maxBps", filters.pop("max_bitrate", None)))
    try:
        min_f = float(min_bps) if min_bps is not None else None
    except (TypeError, ValueError):
        min_f = None
    try:
        max_f = float(max_bps) if max_bps is not None else None
    except (TypeError, ValueError):
        max_f = None

    alias_map = {
        "camera": ("camera_id",),
        "cam": ("camera_id",),
        "cameraId": ("camera_id",),
        "stream": ("target",),
        "name": ("target",),
        "url": ("url",),
        "stream_url": ("url",),
        "source": ("url",),
        "state": ("status",),
        "stage": ("status",),
        "result": ("status",),
        "bytes": ("bytes_received",),
        "total_bytes": ("bytes_received",),
        "bps": ("bitrate_bps",),
        "bitrate": ("bitrate_bps",),
        "kbps": ("bitrate_kbps",),
        "mbps": ("bitrate_mbps",),
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
                    "target", "stream", "url", "camera_id", "camera", "status",
                    "state", "id", "entry_id", "history_id", "sample_id",
                ):
                    ok = False
                    break
                continue
            if str(ev_val) != str(expected):
                ok = False
                break
        if not ok:
            continue
        if since_dt is not None or until_dt is not None:
            ts = _to_datetime(ev.get("sampled_at") or ev.get("created_at") or ev.get("timestamp"))
            if ts is not None:
                if since_dt is not None and ts < since_dt:
                    continue
                if until_dt is not None and ts > until_dt:
                    continue
        if min_f is not None or max_f is not None:
            try:
                b = float(ev.get("bitrate_bps", 0.0) or 0.0)
            except (TypeError, ValueError):
                b = 0.0
            if min_f is not None and b < min_f:
                continue
            if max_f is not None and b > max_f:
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


class BitrateHistoryService:
    """Append-only in-memory bitrate sample history."""

    def __init__(self) -> None:
        self.store = BITRATE_HISTORY
        self.history = BITRATE_HISTORY
        self.entries = BITRATE_HISTORY
        self.samples = BITRATE_HISTORY
        self._lock = _lock

    # -- record --
    def record_sample(self, *args: Any, **kwargs: Any) -> BitrateHistoryEntry:
        data = _normalize_entry_data(*args, **kwargs)
        entry = _build_entry(data)
        with self._lock:
            BITRATE_HISTORY[entry["id"]] = entry
            _prune_if_needed(entry.get("target"))
        return entry

    def record_history(self, *args: Any, **kwargs: Any) -> BitrateHistoryEntry:
        return self.record_sample(*args, **kwargs)

    def log_sample(self, *args: Any, **kwargs: Any) -> BitrateHistoryEntry:
        return self.record_sample(*args, **kwargs)

    def log_history(self, *args: Any, **kwargs: Any) -> BitrateHistoryEntry:
        return self.record_sample(*args, **kwargs)

    def record_result(self, *args: Any, **kwargs: Any) -> BitrateHistoryEntry:
        return self.record_sample(*args, **kwargs)

    def log_result(self, *args: Any, **kwargs: Any) -> BitrateHistoryEntry:
        return self.record_sample(*args, **kwargs)

    def add_entry(self, *args: Any, **kwargs: Any) -> BitrateHistoryEntry:
        return self.record_sample(*args, **kwargs)

    def create_entry(self, *args: Any, **kwargs: Any) -> BitrateHistoryEntry:
        return self.record_sample(*args, **kwargs)

    def add(self, *args: Any, **kwargs: Any) -> BitrateHistoryEntry:
        return self.record_sample(*args, **kwargs)

    def create(self, *args: Any, **kwargs: Any) -> BitrateHistoryEntry:
        return self.record_sample(*args, **kwargs)

    def log(self, *args: Any, **kwargs: Any) -> BitrateHistoryEntry:
        return self.record_sample(*args, **kwargs)

    def append(self, *args: Any, **kwargs: Any) -> BitrateHistoryEntry:
        return self.record_sample(*args, **kwargs)

    def record(self, *args: Any, **kwargs: Any) -> BitrateHistoryEntry:
        return self.record_sample(*args, **kwargs)

    def push(self, *args: Any, **kwargs: Any) -> BitrateHistoryEntry:
        return self.record_sample(*args, **kwargs)

    def sample_and_record(
        self,
        url: Any = None,
        duration: Any = 2.0,
        timeout: Any = 5.0,
        *,
        target: Any = None,
        sampler: Any = None,
        **kwargs: Any,
    ) -> BitrateHistoryEntry:
        """Sample live bitrate via ``app.services.bitrate`` then record history."""
        from app.services import bitrate as _br  # lazy to avoid cycles

        sampler_fn = sampler if sampler is not None else kwargs.pop("sampler", None)
        tgt = target if target is not None else kwargs.get("target", kwargs.get("camera_id", kwargs.get("id")))
        u = url if url is not None else kwargs.get("url", kwargs.get("stream_url", kwargs.get("source")))
        dur = kwargs.pop("duration_seconds", kwargs.pop("sample_seconds", duration))
        tmo = kwargs.pop("timeout_seconds", timeout)
        try:
            if u:
                result = _br.sample_url(u, dur, tmo, target=tgt or u, sampler=sampler_fn)
            elif tgt is not None:
                s = str(tgt).strip()
                if s.startswith("camera:") or s.isdigit():
                    try:
                        cid: Any = int(s.split(":", 1)[1]) if ":" in s else int(s)
                    except (ValueError, IndexError):
                        cid = tgt
                    result = _br.sample_camera(cid, dur, tmo, url=u, sampler=sampler_fn)
                elif "://" in s or "." in s or "/" in s:
                    result = _br.sample_url(s, dur, tmo, target=s, sampler=sampler_fn)
                else:
                    result = _br.sample_camera(tgt, dur, tmo, url=u, sampler=sampler_fn)
            else:
                raise ValueError("Provide 'url' or 'target' to sample")
        except Exception as exc:  # noqa: BLE001 - never raise; record error entry
            data = {"target": _normalize_target(tgt or u or "unknown"), "url": str(u) if u else None,
                    "status": "error", "error": f"{type(exc).__name__}: {exc}"[:500]}
            data.update(kwargs)
            return self.record_sample(data)
        payload: Dict[str, Any] = {}
        try:
            if hasattr(result, "to_dict"):
                payload = dict(result.to_dict())
            elif isinstance(result, dict):
                payload = dict(result)
        except Exception:
            payload = {}
        payload.update({k: v for k, v in kwargs.items() if v is not None})
        return self.record_sample(payload)

    # -- get --
    def get_entry(self, entry_id: Any) -> Optional[BitrateHistoryEntry]:
        if entry_id is None:
            return None
        key = str(entry_id)
        with self._lock:
            found = BITRATE_HISTORY.get(key)
            if found is not None:
                return found
            for e in BITRATE_HISTORY.values():
                for ak in ("entry_id", "history_id", "sample_id", "record_id"):
                    if str(e.get(ak)) == key:
                        return e
        return None

    def get(self, entry_id: Any) -> Optional[BitrateHistoryEntry]:
        return self.get_entry(entry_id)

    def fetch_entry(self, entry_id: Any) -> Optional[BitrateHistoryEntry]:
        return self.get_entry(entry_id)

    def fetch(self, entry_id: Any) -> Optional[BitrateHistoryEntry]:
        return self.get_entry(entry_id)

    def find(self, entry_id: Any) -> Optional[BitrateHistoryEntry]:
        return self.get_entry(entry_id)

    def get_sample(self, entry_id: Any) -> Optional[BitrateHistoryEntry]:
        return self.get_entry(entry_id)

    # -- list --
    def list_history(
        self,
        target: Any = None,
        status: Any = None,  # noqa: A002
        limit: Any = None,
        offset: Any = None,
        skip: Any = None,
        **filters: Any,
    ) -> List[BitrateHistoryEntry]:
        if isinstance(target, dict):
            filters.update(target)
            target = None
        if target is None:
            target = filters.pop("stream", filters.pop("name", None))
        if target is None:
            for ak in ("camera_id", "camera", "cam", "cameraId", "url", "stream_url", "source"):
                if filters.get(ak) is not None and ak in ("camera_id", "camera", "cam", "cameraId"):
                    target = None  # keep camera filter separate below
                    break
        url_f = filters.pop("url", filters.pop("stream_url", filters.pop("source", None)))
        camera_f = filters.pop("camera_id", filters.pop("camera", filters.pop("cam", filters.pop("cameraId", None))))
        if status is None:
            status = filters.pop("state", filters.pop("stage", filters.pop("result", None)))
        with self._lock:
            items = [_entry_to_dict(e) for e in list(BITRATE_HISTORY.values())]
        query: Dict[str, Any] = {}
        if target is not None:
            query["target"] = target
        if url_f is not None:
            query["url"] = url_f
        if camera_f is not None:
            query["camera_id"] = camera_f
        if status is not None:
            query["status"] = status
        query.update({k: v for k, v in filters.items() if v is not None})
        return _apply_filters(items, limit=limit, skip=skip if skip is not None else offset, **query)  # type: ignore[return-value]

    def list(self, *args: Any, **kwargs: Any) -> List[BitrateHistoryEntry]:
        return self.list_history(*args, **kwargs)

    def get_history(self, *args: Any, **kwargs: Any) -> List[BitrateHistoryEntry]:
        return self.list_history(*args, **kwargs)

    def fetch_history(self, *args: Any, **kwargs: Any) -> List[BitrateHistoryEntry]:
        return self.list_history(*args, **kwargs)

    def get_entries(self, *args: Any, **kwargs: Any) -> List[BitrateHistoryEntry]:
        return self.list_history(*args, **kwargs)

    def fetch_entries(self, *args: Any, **kwargs: Any) -> List[BitrateHistoryEntry]:
        return self.list_history(*args, **kwargs)

    def all(self, *args: Any, **kwargs: Any) -> List[BitrateHistoryEntry]:
        return self.list_history(*args, **kwargs)

    def list_samples(self, *args: Any, **kwargs: Any) -> List[BitrateHistoryEntry]:
        return self.list_history(*args, **kwargs)

    def get_samples(self, *args: Any, **kwargs: Any) -> List[BitrateHistoryEntry]:
        return self.list_history(*args, **kwargs)

    def history_for(self, target: Any = None, **kwargs: Any) -> List[BitrateHistoryEntry]:
        return self.list_history(target=target, **kwargs)

    def get_history_for_target(self, target: Any = None, **kwargs: Any) -> List[BitrateHistoryEntry]:
        return self.list_history(target=target, **kwargs)

    def list_for_target(self, target: Any = None, **kwargs: Any) -> List[BitrateHistoryEntry]:
        return self.list_history(target=target, **kwargs)

    def get_latest_for_target(self, target: Any = None, **kwargs: Any) -> Optional[BitrateHistoryEntry]:
        items = self.list_history(target=target, **kwargs)
        if not items:
            # Tolerate camera:N vs bare target mismatch.
            if target is not None:
                s = str(target).strip()
                alt = f"camera:{s}" if s.isdigit() and not s.startswith("camera:") else None
                if alt:
                    items = self.list_history(target=alt, **kwargs)
        if not items:
            return None
        return items[-1]

    def latest(self, target: Any = None, **kwargs: Any) -> Optional[BitrateHistoryEntry]:
        return self.get_latest_for_target(target, **kwargs)

    def latest_for_target(self, target: Any = None, **kwargs: Any) -> Optional[BitrateHistoryEntry]:
        return self.get_latest_for_target(target, **kwargs)

    # -- stats --
    def get_stats(self, target: Any = None, **kwargs: Any) -> Dict[str, Any]:
        items = self.list_history(target=target, **kwargs) if target is not None or kwargs else self.list_history(**kwargs)
        total = len(items)
        measured = sum(1 for e in items if str(e.get("status")) == "measured")
        errors = sum(1 for e in items if str(e.get("status")) == "error")
        unknown = total - measured - errors
        if items:
            vals = []
            for e in items:
                try:
                    vals.append(float(e.get("bitrate_bps", 0.0) or 0.0))
                except (TypeError, ValueError):
                    vals.append(0.0)
            avg_bps = sum(vals) / len(vals)
            max_bps = max(vals)
            min_bps = min(vals)
        else:
            avg_bps = max_bps = min_bps = 0.0
        return {
            "target": str(target) if target is not None else None,
            "total": total,
            "count": total,
            "entries": total,
            "samples": total,
            "measured": measured,
            "errors": errors,
            "unknown": unknown,
            "ok": True,
            "success": True,
            "avg_bitrate_bps": round(avg_bps, 2),
            "avg_bitrate_kbps": round(avg_bps / 1000.0, 3),
            "avg_bps": round(avg_bps, 2),
            "max_bitrate_bps": round(max_bps, 2),
            "max_bitrate_kbps": round(max_bps / 1000.0, 3),
            "max_bps": round(max_bps, 2),
            "min_bitrate_bps": round(min_bps, 2),
            "min_bps": round(min_bps, 2),
        }

    def get_summary(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.get_stats(*args, **kwargs)

    def summary(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.get_stats(*args, **kwargs)

    def stats(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.get_stats(*args, **kwargs)

    def stats_for_target(self, target: Any = None, **kwargs: Any) -> Dict[str, Any]:
        return self.get_stats(target=target, **kwargs)

    def status(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        with self._lock:
            total = len(BITRATE_HISTORY)
            by_target: Dict[str, int] = {}
            by_status: Dict[str, int] = {}
            for e in BITRATE_HISTORY.values():
                t = str(e.get("target", "unknown"))
                by_target[t] = by_target.get(t, 0) + 1
                st = str(e.get("status", "measured"))
                by_status[st] = by_status.get(st, 0) + 1
        out = self.get_stats(*args, **kwargs)
        out.update({
            "status": "ok",
            "service": "bitrate_history",
            "by_target": by_target,
            "counts_by_target": dict(by_target),
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
            if BITRATE_HISTORY.pop(key, None) is not None:
                return True
            for k, e in list(BITRATE_HISTORY.items()):
                for ak in ("entry_id", "history_id", "sample_id", "record_id"):
                    if str(e.get(ak)) == key:
                        BITRATE_HISTORY.pop(k, None)
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
                n = len(BITRATE_HISTORY)
                BITRATE_HISTORY.clear()
                return n
        targets = self.list_history(**filters)
        n = 0
        with self._lock:
            for t in targets:
                if BITRATE_HISTORY.pop(str(t.get("id")), None) is not None:
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
            return len(BITRATE_HISTORY)


bitrate_history_service = BitrateHistoryService()
history_service = bitrate_history_service
service = bitrate_history_service
manager = bitrate_history_service
history_manager = bitrate_history_service
store = bitrate_history_service
default_service = bitrate_history_service
sample_service = bitrate_history_service


def record_sample(*args: Any, **kwargs: Any) -> BitrateHistoryEntry:
    return bitrate_history_service.record_sample(*args, **kwargs)


def record_history(*args: Any, **kwargs: Any) -> BitrateHistoryEntry:
    return bitrate_history_service.record_sample(*args, **kwargs)


def log_sample(*args: Any, **kwargs: Any) -> BitrateHistoryEntry:
    return bitrate_history_service.record_sample(*args, **kwargs)


def log_history(*args: Any, **kwargs: Any) -> BitrateHistoryEntry:
    return bitrate_history_service.record_sample(*args, **kwargs)


def record_result(*args: Any, **kwargs: Any) -> BitrateHistoryEntry:
    return bitrate_history_service.record_sample(*args, **kwargs)


def add_entry(*args: Any, **kwargs: Any) -> BitrateHistoryEntry:
    return bitrate_history_service.record_sample(*args, **kwargs)


def create_entry(*args: Any, **kwargs: Any) -> BitrateHistoryEntry:
    return bitrate_history_service.record_sample(*args, **kwargs)


def sample_and_record(*args: Any, **kwargs: Any) -> BitrateHistoryEntry:
    return bitrate_history_service.sample_and_record(*args, **kwargs)


def get_entry(entry_id: Any) -> Optional[BitrateHistoryEntry]:
    return bitrate_history_service.get_entry(entry_id)


def list_history(*args: Any, **kwargs: Any) -> List[BitrateHistoryEntry]:
    return bitrate_history_service.list_history(*args, **kwargs)


def fetch_history(*args: Any, **kwargs: Any) -> List[BitrateHistoryEntry]:
    return bitrate_history_service.list_history(*args, **kwargs)


def list_entries(*args: Any, **kwargs: Any) -> List[BitrateHistoryEntry]:
    return bitrate_history_service.list_history(*args, **kwargs)


def fetch_entries(*args: Any, **kwargs: Any) -> List[BitrateHistoryEntry]:
    return bitrate_history_service.list_history(*args, **kwargs)


def get_history_for_target(*args: Any, **kwargs: Any) -> List[BitrateHistoryEntry]:
    return bitrate_history_service.get_history_for_target(*args, **kwargs)


def get_latest_for_target(*args: Any, **kwargs: Any) -> Optional[BitrateHistoryEntry]:
    return bitrate_history_service.get_latest_for_target(*args, **kwargs)


def get_stats(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return bitrate_history_service.get_stats(*args, **kwargs)


def get_summary(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return bitrate_history_service.get_stats(*args, **kwargs)


def delete_entry(entry_id: Any) -> bool:
    return bitrate_history_service.delete_entry(entry_id)


def clear_history(**filters: Any) -> int:
    return bitrate_history_service.clear_history(**filters)


def clear(**filters: Any) -> int:
    return bitrate_history_service.clear_history(**filters)


def status(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return bitrate_history_service.status(*args, **kwargs)


def count(**filters: Any) -> int:
    return bitrate_history_service.count(**filters)


# CSV / JSON helpers for history dumps.
DEFAULT_COLUMNS = [
    "id",
    "target",
    "url",
    "camera_id",
    "bytes_received",
    "duration_seconds",
    "bitrate_bps",
    "bitrate_kbps",
    "bitrate_mbps",
    "status",
    "error",
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
bitrate_history_entries = list_history
samples = list_history
list_samples = list_history
latest = get_latest_for_target
history_for = get_history_for_target
summary = get_summary
stats = get_stats
get_status = status
build_csv = to_csv
export_csv = to_csv
build_json = to_json
export_json = to_json

BitrateHistoryServiceClass = BitrateHistoryService
BitrateHistoryManager = BitrateHistoryService
HistoryService = BitrateHistoryService
HistoryManager = BitrateHistoryService

__all__ = [
    "BITRATE_HISTORY",
    "BitrateHistoryEntry",
    "BitrateHistoryService",
    "bitrate_history_service",
    "record_sample",
    "record_history",
    "log_sample",
    "log_history",
    "sample_and_record",
    "add_entry",
    "create_entry",
    "get_entry",
    "list_history",
    "fetch_history",
    "get_history_for_target",
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
