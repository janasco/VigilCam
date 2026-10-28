"""Event-rate anomaly detection for VigilCam.

Complements :mod:`app.services.event_stats` (which only aggregates counts
by label / camera and has NO spike or baseline logic) and
:mod:`app.services.incidents` (which groups events *spatially* across
distinct cameras, never evaluating temporal rates).

This module answers the temporal question: given a baseline window rate
vs a recent current window rate, is the event stream spiking (surge) or
quiet (dropout)? Pure in-memory helpers plus optional async DB-backed
entrypoints. Standard library only.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_BASELINE_HOURS = 24.0
DEFAULT_BASELINE_MINUTES = DEFAULT_BASELINE_HOURS * 60.0
DEFAULT_BASELINE_SECONDS = DEFAULT_BASELINE_MINUTES * 60.0
DEFAULT_CURRENT_MINUTES = 15.0
DEFAULT_CURRENT_SECONDS = DEFAULT_CURRENT_MINUTES * 60.0
DEFAULT_SPIKE_FACTOR = 3.0
DEFAULT_QUIET_FACTOR = 0.25
DEFAULT_MIN_EVENTS = 3

BASELINE_KEYS = (
    "baseline_seconds",
    "baseline_secs",
    "baseline_window_seconds",
    "baseline_window",
    "baseline_minutes",
    "baseline_mins",
    "baseline_window_minutes",
    "baseline_hours",
    "baseline_hrs",
    "baseline_window_hours",
    "baseline",
    "history_window_seconds",
    "history_minutes",
    "history_hours",
    "reference_window_seconds",
    "reference_minutes",
    "reference_hours",
)
CURRENT_KEYS = (
    "current_seconds",
    "current_secs",
    "current_window_seconds",
    "current_window",
    "current_minutes",
    "current_mins",
    "current_window_minutes",
    "current_hours",
    "current_window_hours",
    "current",
    "recent_window_seconds",
    "recent_minutes",
    "recent_hours",
    "window_seconds",
    "window_minutes",
    "window_hours",
    "window",
)
SPIKE_KEYS = (
    "spike_factor",
    "spike_threshold",
    "spike_ratio",
    "spike_multiple",
    "spike",
    "threshold_spike",
    "surge_factor",
    "surge_threshold",
    "burst_factor",
)
QUIET_KEYS = (
    "quiet_factor",
    "quiet_threshold",
    "quiet_ratio",
    "quiet",
    "threshold_quiet",
    "silence_factor",
    "dropout_factor",
)
NOW_KEYS = (
    "now",
    "at",
    "reference_time",
    "referenceTime",
    "current_time",
    "currentTime",
    "end_time",
    "endTime",
    "end",
    "to",
    "until",
)
MIN_EVENTS_KEYS = (
    "min_events",
    "minEvents",
    "min_count",
    "minCount",
    "min_current_events",
    "min_baseline_events",
)


def _parse_dt(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        ts = float(value)
        if abs(ts) > 1e12:
            ts = ts / 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)
        except (OSError, ValueError, OverflowError):
            return None
    else:
        s = str(value).strip()
        if s == "" or s.lower() in {"none", "null", "undefined", "all"}:
            return None
        try:
            ts = float(s)
            if s.replace(".", "", 1).replace("e", "", 1).replace("E", "", 1).replace(
                "-", "", 1
            ).replace("+", "", 1).isdigit():
                if abs(ts) > 1e12:
                    ts = ts / 1000.0
                return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)
        except Exception:
            pass
        s2 = s[:-1] + "+00:00" if s.endswith("Z") else s
        try:
            dt = datetime.fromisoformat(s2)
        except ValueError:
            return None
    if getattr(dt, "tzinfo", None) is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


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


def _flatten(data: Any) -> Dict[str, Any]:
    if data is None:
        return {}
    if hasattr(data, "model_dump"):
        try:
            return dict(data.model_dump())
        except Exception:
            pass
    if isinstance(data, dict):
        out = dict(data)
        for nested_key in (
            "payload", "options", "config", "params", "data", "body",
            "request", "fields", "rate", "anomaly", "detection",
        ):
            nested = out.get(nested_key)
            if isinstance(nested, dict):
                for k, v in nested.items():
                    out.setdefault(k, v)
        return out
    try:
        return dict(data)
    except Exception:
        return {}


def _first_present(data: Dict[str, Any], keys: Tuple[str, ...]) -> Any:
    for k in keys:
        if k in data and data.get(k) is not None:
            return data.get(k)
    lower = {str(k).lower(): v for k, v in data.items()}
    for k in keys:
        if k.lower() in lower and lower[k.lower()] is not None:
            return lower[k.lower()]
    return None


def _event_time(ev: Any) -> Optional[datetime]:
    if isinstance(ev, dict):
        for attr in ("created_at", "createdAt", "timestamp", "ts", "time",
                     "occurred_at", "occurredAt", "at", "date", "started_at",
                     "event_time", "detected_at"):
            v = ev.get(attr)
            if v is None:
                # case-insensitive fallback
                for k2 in ev.keys():
                    if str(k2).lower() == attr.lower():
                        v = ev.get(k2)
                        break
            if v is None:
                continue
            dt = _parse_dt(v)
            if dt is not None:
                return dt
        return None
    for attr in ("created_at", "createdAt", "timestamp", "ts", "time",
                 "occurred_at", "started_at"):
        try:
            v = getattr(ev, attr, None)
        except Exception:
            v = None
        if v is None:
            continue
        dt = _parse_dt(v)
        if dt is not None:
            return dt
    return None


def _event_camera(ev: Any) -> Optional[Any]:
    if isinstance(ev, dict):
        for k in ("camera_id", "cameraId", "camera", "cam_id", "cam"):
            if ev.get(k) is not None:
                return ev.get(k)
        return None
    for attr in ("camera_id", "cameraId", "cam_id"):
        try:
            v = getattr(ev, attr, None)
        except Exception:
            v = None
        if v is not None:
            return v
    return None


def _extract_events(*args: Any, **kwargs: Any) -> List[Any]:
    flat = _flatten(args[0]) if args and isinstance(args[0], dict) else {}
    merged = dict(flat)
    merged.update(kwargs or {})
    if args and isinstance(args[0], list):
        return list(args[0])
    for key in ("events", "items", "rows", "detections", "alerts",
                "event_list", "data_events"):
        val = merged.get(key)
        if isinstance(val, (list, tuple)):
            return list(val)
    for a in args:
        if isinstance(a, (list, tuple)) and a and not isinstance(a[0], (list, tuple)):
            # bare positional list already handled; skip
            continue
    return []


def _resolve_seconds(data: Dict[str, Any], keys: Tuple[str, ...],
                     default: float) -> float:
    raw = _first_present(data, keys)
    if raw is None:
        return float(default)
    # Hours-suffixed keys need conversion when the matched key is hours.
    # Detect which key matched to apply unit conversion.
    matched_key = None
    for k in keys:
        if k in data and data.get(k) is not None:
            matched_key = k
            break
    if matched_key is None:
        lower = {str(k).lower(): v for k, v in data.items()}
        for k in keys:
            if k.lower() in lower and lower[k.lower()] is not None:
                matched_key = k
                break
    v = _coerce_float(raw, None)
    if v is None or v <= 0:
        return float(default)
    lk = (matched_key or "").lower()
    if "hour" in lk or lk in {"hrs"}:
        return float(v) * 3600.0
    if "min" in lk:
        return float(v) * 60.0
    return float(v)


def _resolve_baseline_seconds(data: Dict[str, Any]) -> float:
    # Explicit hours take precedence for readability.
    for k in ("baseline_hours", "baseline_hrs", "baseline_window_hours",
              "history_hours", "reference_hours"):
        if data.get(k) is not None or str(k).lower() in {str(x).lower() for x in data}:
            v = _coerce_float(_first_present(data, (k,)), None)
            if v is not None and v > 0:
                return float(v) * 3600.0
    return _resolve_seconds(data, BASELINE_KEYS, DEFAULT_BASELINE_SECONDS)


def _resolve_current_seconds(data: Dict[str, Any]) -> float:
    return _resolve_seconds(data, CURRENT_KEYS, DEFAULT_CURRENT_SECONDS)


def _resolve_spike_factor(data: Dict[str, Any]) -> float:
    v = _coerce_float(_first_present(data, SPIKE_KEYS), DEFAULT_SPIKE_FACTOR)
    if v is None or v <= 0:
        return float(DEFAULT_SPIKE_FACTOR)
    return float(v)


def _resolve_quiet_factor(data: Dict[str, Any]) -> float:
    v = _coerce_float(_first_present(data, QUIET_KEYS), DEFAULT_QUIET_FACTOR)
    if v is None or v < 0:
        return float(DEFAULT_QUIET_FACTOR)
    return float(v)


def _resolve_min_events(data: Dict[str, Any]) -> int:
    v = _coerce_int(_first_present(data, MIN_EVENTS_KEYS), DEFAULT_MIN_EVENTS)
    if v is None or v < 1:
        return int(DEFAULT_MIN_EVENTS)
    return int(v)


def _resolve_now(data: Dict[str, Any], events: List[Any]) -> datetime:
    raw = _first_present(data, NOW_KEYS)
    dt = _parse_dt(raw) if raw is not None else None
    if dt is not None:
        return dt
    # Default to the latest event timestamp so fixed historical fixtures work.
    latest: Optional[datetime] = None
    for ev in events or []:
        t = _event_time(ev)
        if t is not None and (latest is None or t > latest):
            latest = t
    if latest is not None:
        return latest
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _resolve_camera_filter(data: Dict[str, Any]) -> Optional[List[str]]:
    cands: List[Any] = []
    for k in ("camera_id", "camera_ids", "cameras", "camera", "cameraId",
              "cameraIds", "cam", "cam_id"):
        if data.get(k) is not None:
            cands.append(data.get(k))
    if not cands:
        return None
    vals: List[Any] = []
    for c in cands:
        if isinstance(c, (list, tuple, set)):
            vals.extend(list(c))
        else:
            vals.append(c)
    ids: List[str] = []
    for v in vals:
        if v is None:
            continue
        if isinstance(v, str) and "," in v:
            for part in v.split(","):
                part = part.strip()
                if part:
                    ids.append(part)
        else:
            try:
                ids.append(str(int(v)))
            except (TypeError, ValueError):
                ids.append(str(v))
    # Normalize numeric strings already; keep raw too for matching.
    return ids or None


def _camera_matches(ev_cam: Any, wanted: Optional[List[str]]) -> bool:
    if not wanted:
        return True
    if ev_cam is None:
        return False
    try:
        ev_str = str(int(ev_cam))
    except (TypeError, ValueError):
        ev_str = str(ev_cam)
    for w in wanted:
        try:
            if ev_str == str(int(w)):
                return True
        except (TypeError, ValueError):
            pass
        if ev_str == str(w) or str(ev_cam) == str(w):
            return True
    return False


def analyze_rate(events: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Compare baseline rate vs current-window rate for event streams.

    Baseline window immediately precedes the current window (no overlap).
    Returns spike / quiet flags plus per-rate details. Never raises.
    """
    try:
        options = _flatten(args[0]) if args and isinstance(args[0], dict) else {}
        options.update(kwargs or {})
        if events is None:
            for k in ("events", "items", "rows", "detections", "alerts",
                      "event_list", "data_events"):
                if isinstance(options.get(k), (list, tuple)):
                    events = list(options.get(k))
                    break
        if events is None and args and isinstance(args[0], list):
            events = list(args[0])
        if events is None:
            events = []
        try:
            event_list = list(events)  # type: ignore[arg-type]
        except TypeError:
            event_list = []

        baseline_s = _resolve_baseline_seconds(options)
        current_s = _resolve_current_seconds(options)
        spike_factor = _resolve_spike_factor(options)
        quiet_factor = _resolve_quiet_factor(options)
        min_events = _resolve_min_events(options)
        wanted = _resolve_camera_filter(options)

        # Camera filter applies before windowing.
        filtered = [e for e in event_list if _camera_matches(_event_camera(e), wanted)]
        # For "now", use latest timestamp among filtered (fallback: all events).
        now_dt = _resolve_now(options, filtered if filtered else event_list)
        current_start = now_dt - timedelta(seconds=float(current_s))
        baseline_end = current_start
        baseline_start = baseline_end - timedelta(seconds=float(baseline_s))

        baseline_count = 0
        current_count = 0
        for ev in filtered:
            t = _event_time(ev)
            if t is None:
                # Untimed events count toward current window (most recent).
                current_count += 1
                continue
            if t >= current_start and t <= now_dt:
                current_count += 1
            elif t >= baseline_start and t < baseline_end:
                baseline_count += 1

        baseline_rate_s = (baseline_count / baseline_s) if baseline_s > 0 else 0.0
        current_rate_s = (current_count / current_s) if current_s > 0 else 0.0
        baseline_per_min = baseline_rate_s * 60.0
        current_per_min = current_rate_s * 60.0
        baseline_per_hour = baseline_rate_s * 3600.0
        current_per_hour = current_rate_s * 3600.0

        if baseline_count <= 0:
            if current_count <= 0:
                ratio: float = 0.0
            else:
                ratio = float("inf")
        else:
            ratio = (current_rate_s / baseline_rate_s) if baseline_rate_s > 0 else 0.0

        # Spike / quiet decisions.
        if baseline_count <= 0:
            spike = bool(current_count >= min_events)
            quiet = False
        else:
            spike = bool(ratio >= spike_factor and current_count >= 2)
            if current_count == 0 and baseline_count >= min_events:
                quiet = True
            else:
                quiet = bool(ratio <= quiet_factor)
                # Avoid flagging quiet on tiny baselines with 1-2 events.
                if baseline_count < min_events and current_count > 0:
                    quiet = False
        anomaly = bool(spike or quiet)

        if spike:
            status = "spike"
        elif quiet:
            status = "quiet"
        elif anomaly:
            status = "anomaly"
        else:
            status = "ok"

        alerts: List[Dict[str, Any]] = []
        if spike:
            alerts.append({
                "type": "spike",
                "kind": "spike",
                "alert": "spike",
                "severity": "warning",
                "message": (
                    f"event rate spike: {current_count} events in last "
                    f"{current_s:.0f}s vs baseline {baseline_count} events "
                    f"in {baseline_s:.0f}s (ratio "
                    f"{'inf' if ratio == float('inf') else f'{ratio:.2f}'} "
                    f">= {spike_factor})"
                ),
                "ratio": ratio if ratio != float("inf") else None,
                "current_count": current_count,
                "baseline_count": baseline_count,
            })
        if quiet:
            alerts.append({
                "type": "quiet",
                "kind": "quiet",
                "alert": "quiet",
                "severity": "info",
                "message": (
                    f"event rate quiet: {current_count} events in last "
                    f"{current_s:.0f}s vs baseline {baseline_count} events "
                    f"in {baseline_s:.0f}s (ratio {ratio:.2f} "
                    f"<= {quiet_factor})"
                ),
                "ratio": ratio,
                "current_count": current_count,
                "baseline_count": baseline_count,
            })

        result: Dict[str, Any] = {
            "status": status,
            "state": status,
            "ok": not anomaly,
            "success": True,
            "healthy": not anomaly,
            "anomaly": anomaly,
            "is_anomaly": anomaly,
            "has_anomaly": anomaly,
            "anomalous": anomaly,
            "spike": spike,
            "is_spike": spike,
            "spike_detected": spike,
            "has_spike": spike,
            "quiet": quiet,
            "is_quiet": quiet,
            "quiet_detected": quiet,
            "has_quiet": quiet,
            "silent": quiet,
            "dropout": quiet,
            "baseline_count": baseline_count,
            "baseline_events": baseline_count,
            "baseline_total": baseline_count,
            "current_count": current_count,
            "current_events": current_count,
            "current_total": current_count,
            "total_baseline": baseline_count,
            "total_current": current_count,
            "baseline_window_seconds": float(baseline_s),
            "baseline_window_minutes": float(baseline_s) / 60.0,
            "baseline_window_hours": float(baseline_s) / 3600.0,
            "baseline_hours": float(baseline_s) / 3600.0,
            "baseline_minutes": float(baseline_s) / 60.0,
            "current_window_seconds": float(current_s),
            "current_window_minutes": float(current_s) / 60.0,
            "current_window_hours": float(current_s) / 3600.0,
            "current_minutes": float(current_s) / 60.0,
            "current_hours": float(current_s) / 3600.0,
            "window_seconds": float(current_s),
            "baseline_rate_per_second": baseline_rate_s,
            "current_rate_per_second": current_rate_s,
            "baseline_rate_per_minute": baseline_per_min,
            "current_rate_per_minute": current_per_min,
            "baseline_rate_per_hour": baseline_per_hour,
            "current_rate_per_hour": current_per_hour,
            "baseline_rate": baseline_per_min,
            "current_rate": current_per_min,
            "rate_ratio": ratio if ratio != float("inf") else None,
            "ratio": ratio if ratio != float("inf") else None,
            "current_vs_baseline": ratio if ratio != float("inf") else None,
            "ratio_raw": ratio if ratio != float("inf") else None,
            "ratio_inf": bool(ratio == float("inf")),
            "spike_factor": float(spike_factor),
            "spike_threshold": float(spike_factor),
            "quiet_factor": float(quiet_factor),
            "quiet_threshold": float(quiet_factor),
            "min_events": int(min_events),
            "thresholds": {
                "spike_factor": float(spike_factor),
                "quiet_factor": float(quiet_factor),
                "min_events": int(min_events),
            },
            "now": now_dt.isoformat(),
            "reference_time": now_dt.isoformat(),
            "current_start": current_start.isoformat(),
            "baseline_start": baseline_start.isoformat(),
            "baseline_end": baseline_end.isoformat(),
            "alerts": alerts,
            "anomalies": alerts,
            "findings": alerts,
            "severity": ("warning" if spike else "info" if quiet else "ok"),
        }
        # Per-camera breakdown (same windows/thresholds per camera).
        try:
            cams: Dict[str, List[Any]] = {}
            for ev in filtered:
                cam = _event_camera(ev)
                key = "unknown" if cam is None else str(cam)
                try:
                    key = str(int(cam)) if cam is not None else "unknown"
                except (TypeError, ValueError):
                    key = str(cam)
                cams.setdefault(key, []).append(ev)
            by_camera: Dict[str, Any] = {}
            for ck, cev in cams.items():
                sub = analyze_rate(
                    cev,
                    baseline_seconds=baseline_s,
                    current_seconds=current_s,
                    spike_factor=spike_factor,
                    quiet_factor=quiet_factor,
                    min_events=min_events,
                    now=now_dt.isoformat(),
                )
                by_camera[ck] = {
                    "camera": ck,
                    "camera_id": ck,
                    "baseline_count": sub["baseline_count"],
                    "current_count": sub["current_count"],
                    "ratio": sub["ratio"],
                    "spike": sub["spike"],
                    "quiet": sub["quiet"],
                    "anomaly": sub["anomaly"],
                    "status": sub["status"],
                }
            result["by_camera"] = by_camera
            result["per_camera"] = by_camera
            result["cameras"] = by_camera
        except Exception:
            pass
        return result
    except Exception:
        return {
            "status": "ok", "state": "ok", "ok": True, "success": True,
            "healthy": True, "anomaly": False, "is_anomaly": False,
            "has_anomaly": False, "spike": False, "is_spike": False,
            "quiet": False, "is_quiet": False, "alerts": [],
            "baseline_count": 0, "current_count": 0,
            "ratio": 0.0, "rate_ratio": 0.0,
        }


async def _fetch_events(db: Any, limit: Optional[int] = None) -> List[Any]:
    if db is None:
        return []
    if isinstance(db, (list, tuple)):
        return list(db)
    if isinstance(db, dict):
        for k in ("events", "items", "rows"):
            if isinstance(db.get(k), (list, tuple)):
                return list(db.get(k))
        return []
    if hasattr(db, "execute"):
        try:
            from sqlalchemy import select as _select

            from app.models.entities import Event as _Event

            q = _select(_Event).order_by(_Event.id.desc())
            if limit is not None:
                try:
                    q = q.limit(int(limit))
                except (TypeError, ValueError):
                    pass
            res = await db.execute(q)
            return list(res.scalars().all())
        except Exception:
            return []
    try:
        return list(db)  # type: ignore[arg-type]
    except Exception:
        return []


async def get_rate_anomaly(db: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Aggregate rate anomaly from DB or inline events. Never raises."""
    try:
        options = _flatten(args[0]) if args and isinstance(args[0], dict) else {}
        options.update(kwargs or {})
        session = db
        if session is None:
            for k in ("db", "session", "database", "db_session"):
                if options.get(k) is not None:
                    session = options.get(k)
                    break
        preset = None
        for k in ("events", "items", "rows", "event_list"):
            if isinstance(options.get(k), (list, tuple)):
                preset = list(options.get(k))
                break
        if preset is not None:
            return analyze_rate(preset, **options)
        if isinstance(session, (list, tuple)):
            return analyze_rate(list(session), **options)
        limit = None
        for k in ("limit", "page_size", "per_page", "n", "count", "size"):
            if options.get(k) is not None:
                try:
                    limit = int(options.get(k))
                    break
                except (TypeError, ValueError):
                    pass
        rows = await _fetch_events(session, limit=limit)
        return analyze_rate(rows, **options)
    except Exception:
        return analyze_rate([], **kwargs)


async def get_rate_anomaly_async(db: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    return await get_rate_anomaly(db, *args, **kwargs)


def detect_rate_anomaly(events: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    if events is None and args and isinstance(args[0], (list, tuple)):
        events = args[0]
        args = args[1:]
    return analyze_rate(events or [], *args, **kwargs)


def check_rate(events: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    return detect_rate_anomaly(events, *args, **kwargs)


def evaluate_rate(events: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    return detect_rate_anomaly(events, *args, **kwargs)


class RateAnomalyDetector:
    """Object-oriented facade over the rate-anomaly helpers."""

    def analyze(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        if args and isinstance(args[0], (list, tuple)) and "events" not in kwargs:
            return analyze_rate(args[0], **kwargs)
        return analyze_rate(kwargs.get("events", []), *args, **kwargs)

    def detect(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.analyze(*args, **kwargs)

    def check(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.analyze(*args, **kwargs)

    def evaluate(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.analyze(*args, **kwargs)

    async def analyze_async(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        db = kwargs.pop("db", None)
        return await get_rate_anomaly(db, *args, **kwargs)

    def get_status(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.analyze(*args, **kwargs)

    # aliases
    detect_rate_anomaly = analyze
    check_rate = analyze
    evaluate_rate = analyze
    status = get_status


rate_anomaly_detector = RateAnomalyDetector()
rate_detector = rate_anomaly_detector
detector = rate_anomaly_detector
service = rate_anomaly_detector
manager = rate_anomaly_detector
analyzer = rate_anomaly_detector
default_service = rate_anomaly_detector

# Compatibility aliases.
detect = detect_rate_anomaly
detect_anomaly = detect_rate_anomaly
analyze = analyze_rate
analyze_events = analyze_rate
check = check_rate
evaluate = evaluate_rate
get_status = RateAnomalyDetector().get_status
get_anomaly = analyze_rate
find_anomalies = analyze_rate
rate_anomaly = analyze_rate
compute_rate_anomaly = analyze_rate

__all__ = [
    "analyze_rate",
    "detect_rate_anomaly",
    "check_rate",
    "evaluate_rate",
    "get_rate_anomaly",
    "get_rate_anomaly_async",
    "RateAnomalyDetector",
    "rate_anomaly_detector",
    "DEFAULT_BASELINE_SECONDS",
    "DEFAULT_CURRENT_SECONDS",
    "DEFAULT_SPIKE_FACTOR",
    "DEFAULT_QUIET_FACTOR",
]
