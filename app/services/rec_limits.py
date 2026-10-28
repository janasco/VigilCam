"""Recording stop-condition limits for VigilCam.

Ingestion (``app.services.ingestion.StreamCopyIngestor``) writes fixed-size
segments (default 5 minutes) and only stops on explicit manual calls
(``stop_camera`` / ``stop_all``).  Retention (``app.services.retention``) and
the retention preview (``app.services.retention_preview``) only enforce
max-age / max-total-bytes pruning and dry-run reporting -- they deliberately
do NOT implement recording stop conditions.

This module owns the three recording stop conditions:

* ``max_duration`` -- split/stop a recording once it reaches a maximum
  duration (max-duration split).
* ``min_duration`` -- discard recordings shorter than a minimum duration
  instead of keeping them.
* ``inactivity`` -- stop a recording after N seconds without motion /
  activity / events (inactivity-stop).

Model: global defaults plus optional per-camera overrides, held in memory.
Nothing here patches the ingestor, retention, or preview in place; callers
query :func:`evaluate` / :func:`should_stop` and act (split/stop/discard).
"""

from __future__ import annotations

import re
import threading
from typing import Any, Dict, List, Optional


_LOCK = threading.RLock()

_GLOBAL: Dict[str, Optional[float]] = {
    "max_duration_seconds": None,
    "min_duration_seconds": None,
    "inactivity_timeout_seconds": None,
}
_PER_CAMERA: Dict[str, Dict[str, Optional[float]]] = {}


_MAX_KEYS = (
    "max_duration_seconds",
    "max_duration",
    "maxDurationSeconds",
    "maxDuration",
    "max_seconds",
    "max_secs",
    "maxLengthSeconds",
    "max_length_seconds",
    "maxRecordingDuration",
    "max_recording_duration",
    "maxRecordingSeconds",
    "split_after_seconds",
    "splitAfterSeconds",
    "segment_max_seconds",
    "max_segment_seconds",
    "duration_limit",
    "durationLimit",
    "maxDur",
)
_MIN_KEYS = (
    "min_duration_seconds",
    "min_duration",
    "minDurationSeconds",
    "minDuration",
    "min_seconds",
    "min_secs",
    "minLengthSeconds",
    "min_length_seconds",
    "minRecordingDuration",
    "min_recording_duration",
    "minRecordingSeconds",
    "min_segment_seconds",
    "minimum_duration",
    "minimumDuration",
    "minDur",
)
_INACTIVITY_KEYS = (
    "inactivity_timeout_seconds",
    "inactivity_timeout",
    "inactivityTimeoutSeconds",
    "inactivityTimeout",
    "inactivity_seconds",
    "inactivitySeconds",
    "inactive_timeout_seconds",
    "inactive_timeout",
    "no_motion_timeout_seconds",
    "no_motion_timeout",
    "motion_timeout_seconds",
    "motion_timeout",
    "activity_timeout_seconds",
    "activity_timeout",
    "silence_timeout_seconds",
    "idle_timeout_seconds",
    "idle_timeout",
    "idleTimeout",
    "stop_after_inactive_seconds",
    "stopAfterInactiveSeconds",
)
_CAMERA_KEYS = (
    "camera_name",
    "cameraName",
    "camera",
    "cam",
    "camera_id",
    "cameraId",
    "camera_slug",
    "slug",
    "name",
)
_ELAPSED_KEYS = (
    "elapsed_seconds",
    "elapsed",
    "elapsedSeconds",
    "duration_seconds",
    "duration",
    "durationSeconds",
    "recording_seconds",
    "recording_duration",
    "age_seconds",
    "run_time_seconds",
    "runtime_seconds",
)
_INACTIVE_KEYS = (
    "inactive_seconds",
    "inactiveSeconds",
    "inactive_for",
    "idle_seconds",
    "idleSeconds",
    "since_motion_seconds",
    "since_activity_seconds",
    "no_motion_for_seconds",
    "seconds_since_motion",
    "seconds_since_activity",
    "seconds_inactive",
)
_FINAL_KEYS = (
    "final_duration_seconds",
    "final_duration",
    "total_duration_seconds",
    "actual_duration_seconds",
    "segment_duration_seconds",
    "file_duration_seconds",
)


def _safe_camera(value: Any) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in s)


def _coerce_seconds(value: Any) -> Optional[float]:
    """Coerce a duration to seconds. None/empty/disabled -> None."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if v <= 0:
            return None
        return v
    s = str(value).strip().lower().replace(" ", "")
    if not s or s in ("none", "null", "auto", "all", "disabled", "off", "no", "0"):
        return None
    # plain number
    try:
        v = float(s)
        return v if v > 0 else None
    except (TypeError, ValueError):
        pass
    m = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*([a-z]+)$", s)
    if m:
        num = float(m.group(1))
        unit = m.group(2)
        mult = {
            "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
            "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
            "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
            "d": 86400, "day": 86400, "days": 86400,
            "ms": 0.001, "millis": 0.001,
        }.get(unit)
        if mult is not None and num > 0:
            return num * mult
        return None
    # "HH:MM:SS" form
    if ":" in s:
        try:
            parts = [float(p) for p in s.split(":")]
            total = 0.0
            for p in parts:
                total = total * 60.0 + p
            return total if total > 0 else None
        except (TypeError, ValueError):
            return None
    return None


def _first(data: Dict[str, Any], keys: tuple) -> Any:
    for k in keys:
        if k in data and data.get(k) is not None:
            return data.get(k)
    return None


def _normalize_kwargs(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            data.update(a)
        elif isinstance(a, (int, float)) and not isinstance(a, bool):
            data.setdefault("elapsed_seconds", a)
    data.update(kwargs or {})
    for nested in ("payload", "options", "config", "params", "data", "body",
                   "request", "policy", "settings", "limits", "filter", "filters"):
        inner = data.get(nested)
        if isinstance(inner, dict):
            for k, v in inner.items():
                data.setdefault(k, v)
    return data


def _extract_limits(data: Dict[str, Any]) -> Dict[str, Optional[float]]:
    out: Dict[str, Optional[float]] = {}
    raw_max = _first(data, _MAX_KEYS)
    raw_min = _first(data, _MIN_KEYS)
    raw_inact = _first(data, _INACTIVITY_KEYS)
    if raw_max is not None:
        out["max_duration_seconds"] = _coerce_seconds(raw_max)
    if raw_min is not None:
        out["min_duration_seconds"] = _coerce_seconds(raw_min)
    if raw_inact is not None:
        out["inactivity_timeout_seconds"] = _coerce_seconds(raw_inact)
    return out


def _extract_camera(data: Dict[str, Any]) -> Optional[str]:
    return _safe_camera(_first(data, _CAMERA_KEYS))


def _extract_elapsed(data: Dict[str, Any]) -> Optional[float]:
    raw = _first(data, _ELAPSED_KEYS)
    if raw is None:
        # final-duration aliases double as elapsed when evaluating a segment
        raw = _first(data, _FINAL_KEYS)
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        coerced = _coerce_seconds(raw)
        return coerced


def _extract_inactive(data: Dict[str, Any]) -> Optional[float]:
    raw = _first(data, _INACTIVE_KEYS)
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return _coerce_seconds(raw)


def _extract_final(data: Dict[str, Any]) -> Optional[float]:
    raw = _first(data, _FINAL_KEYS)
    if raw is None:
        raw = _first(data, _ELAPSED_KEYS)
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return _coerce_seconds(raw)


def _effective_for(camera: Optional[str] = None) -> Dict[str, Optional[float]]:
    with _LOCK:
        eff = dict(_GLOBAL)
        if camera:
            per = _PER_CAMERA.get(camera)
            if per is None:
                # case-insensitive / safe-name fallback
                target = _safe_camera(camera)
                for k, v in _PER_CAMERA.items():
                    if _safe_camera(k) == target or str(k).lower() == str(camera).lower():
                        per = v
                        break
            if per:
                for key in ("max_duration_seconds", "min_duration_seconds",
                            "inactivity_timeout_seconds"):
                    if per.get(key) is not None:
                        eff[key] = per.get(key)
    return eff


# ---------------------------------------------------------------------------
# Mutators / accessors
# ---------------------------------------------------------------------------

def set_limits(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Set global defaults and/or a per-camera override."""
    data = _normalize_kwargs(*args, **kwargs)
    camera = _extract_camera(data)
    vals = _extract_limits(data)
    with _LOCK:
        if camera is not None and (
            vals or data.get("per_camera") is True or data.get("camera_specific") is True
            or any(k in data for k in _CAMERA_KEYS)
        ):
            entry = _PER_CAMERA.setdefault(camera, {
                "max_duration_seconds": None,
                "min_duration_seconds": None,
                "inactivity_timeout_seconds": None,
            })
            for k, v in vals.items():
                entry[k] = v
            eff = _effective_for(camera)
            return _limits_payload(camera=camera, effective=eff)
        for k, v in vals.items():
            _GLOBAL[k] = v
        eff = _effective_for(None)
    return _limits_payload(camera=camera, effective=eff)


def get_limits(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _normalize_kwargs(*args, **kwargs)
    camera = _extract_camera(data)
    eff = _effective_for(camera)
    return _limits_payload(camera=camera, effective=eff)


def clear_limits(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _normalize_kwargs(*args, **kwargs)
    camera = _extract_camera(data)
    with _LOCK:
        if camera is not None:
            removed = 0
            for key in list(_PER_CAMERA.keys()):
                if key == camera or _safe_camera(key) == _safe_camera(camera):
                    _PER_CAMERA.pop(key, None)
                    removed += 1
            eff = _effective_for(camera)
            payload = _limits_payload(camera=camera, effective=eff)
            payload["cleared"] = removed
            payload["removed"] = removed
            return payload
        _GLOBAL["max_duration_seconds"] = None
        _GLOBAL["min_duration_seconds"] = None
        _GLOBAL["inactivity_timeout_seconds"] = None
        _PER_CAMERA.clear()
        eff = dict(_GLOBAL)
    payload = _limits_payload(camera=None, effective=eff)
    payload["cleared"] = True
    return payload


def update_limits(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return set_limits(*args, **kwargs)


def _limits_payload(camera: Optional[str], effective: Dict[str, Any]) -> Dict[str, Any]:
    mx = effective.get("max_duration_seconds")
    mn = effective.get("min_duration_seconds")
    ina = effective.get("inactivity_timeout_seconds")
    with _LOCK:
        per = {k: dict(v) for k, v in _PER_CAMERA.items()}
        glob = dict(_GLOBAL)
    base: Dict[str, Any] = {
        "ok": True,
        "success": True,
        "status": "ok",
        "camera_name": camera,
        "camera": camera,
        "max_duration_seconds": mx,
        "max_duration": mx,
        "maxDurationSeconds": mx,
        "maxDuration": mx,
        "min_duration_seconds": mn,
        "min_duration": mn,
        "minDurationSeconds": mn,
        "minDuration": mn,
        "inactivity_timeout_seconds": ina,
        "inactivity_timeout": ina,
        "inactivityTimeoutSeconds": ina,
        "inactivityTimeout": ina,
        "inactivity_seconds": ina,
        "idle_timeout_seconds": ina,
        "global": dict(glob),
        "defaults": dict(glob),
        "global_limits": dict(glob),
        "per_camera": dict(per),
        "per_camera_limits": dict(per),
        "limits": {"max_duration_seconds": mx, "min_duration_seconds": mn,
                   "inactivity_timeout_seconds": ina},
        "effective": {"max_duration_seconds": mx, "min_duration_seconds": mn,
                      "inactivity_timeout_seconds": ina},
    }
    return base


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def evaluate(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Evaluate stop conditions for an in-progress or finished recording.

    Inputs (many aliases accepted): elapsed/duration seconds, inactive/idle
    seconds, optional camera name for per-camera overrides.
    """
    data = _normalize_kwargs(*args, **kwargs)
    camera = _extract_camera(data)
    elapsed = _extract_elapsed(data)
    inactive = _extract_inactive(data)
    final = _extract_final(data)
    # duration used for max-duration check: prefer elapsed, fall back to final
    duration = elapsed if elapsed is not None else final
    eff = _effective_for(camera)
    mx = eff.get("max_duration_seconds")
    mn = eff.get("min_duration_seconds")
    ina = eff.get("inactivity_timeout_seconds")

    hit_max = bool(mx is not None and duration is not None and duration >= float(mx))
    hit_inactivity = bool(
        ina is not None and inactive is not None and float(inactive) >= float(ina)
    )
    # min-duration is a keep/discard gate applied to the finished length.
    check_len = final if final is not None else duration
    too_short = bool(mn is not None and check_len is not None and float(check_len) < float(mn))

    should_split = bool(hit_max)
    should_stop = bool(hit_max or hit_inactivity)
    should_discard = bool(too_short)

    reasons: List[str] = []
    if hit_max:
        reasons.append("max_duration")
    if hit_inactivity:
        reasons.append("inactivity")
    if too_short:
        reasons.append("min_duration")

    if should_split:
        action = "split"
    elif hit_inactivity:
        action = "stop"
    elif should_discard:
        action = "discard"
    else:
        action = "continue"

    out: Dict[str, Any] = {
        "ok": True,
        "success": True,
        "status": "stop" if should_stop else ("discard" if should_discard else "continue"),
        "camera_name": camera,
        "camera": camera,
        "elapsed_seconds": elapsed if elapsed is not None else duration,
        "elapsed": elapsed if elapsed is not None else duration,
        "duration_seconds": duration,
        "duration": duration,
        "final_duration_seconds": final,
        "inactive_seconds": inactive,
        "inactive": inactive,
        "max_duration_seconds": mx,
        "max_duration": mx,
        "min_duration_seconds": mn,
        "min_duration": mn,
        "inactivity_timeout_seconds": ina,
        "inactivity_timeout": ina,
        "hit_max_duration": hit_max,
        "max_exceeded": hit_max,
        "max_duration_exceeded": hit_max,
        "hit_inactivity": hit_inactivity,
        "inactivity_exceeded": hit_inactivity,
        "inactivity_timed_out": hit_inactivity,
        "too_short": too_short,
        "below_min_duration": too_short,
        "min_duration_violated": too_short,
        "should_stop": should_stop,
        "shouldStop": should_stop,
        "stop": should_stop,
        "stop_recording": should_stop,
        "should_split": should_split,
        "shouldSplit": should_split,
        "split": should_split,
        "should_discard": should_discard,
        "shouldDiscard": should_discard,
        "discard": should_discard,
        "drop": should_discard,
        "keep": not should_discard,
        "should_keep": not should_discard,
        "reasons": reasons,
        "reason": reasons[0] if reasons else None,
        "stop_reason": reasons[0] if reasons else None,
        "action": action,
        "decision": action,
        "effective": dict(eff),
        "limits": dict(eff),
    }
    return out


def should_stop(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return evaluate(*args, **kwargs)


def should_split(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    result = evaluate(*args, **kwargs)
    # focused split view that still carries the full payload
    result["split_requested"] = bool(result.get("should_split"))
    return result


def should_discard(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    result = evaluate(*args, **kwargs)
    result["discard_requested"] = bool(result.get("should_discard"))
    return result


def check_duration(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return evaluate(*args, **kwargs)


def check_limits(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return evaluate(*args, **kwargs)


def check_inactivity(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return evaluate(*args, **kwargs)


def is_too_long(*args: Any, **kwargs: Any) -> bool:
    return bool(evaluate(*args, **kwargs).get("hit_max_duration"))


def is_too_short(*args: Any, **kwargs: Any) -> bool:
    return bool(evaluate(*args, **kwargs).get("too_short"))


def is_inactive(*args: Any, **kwargs: Any) -> bool:
    return bool(evaluate(*args, **kwargs).get("hit_inactivity"))


def status(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _normalize_kwargs(*args, **kwargs)
    camera = _extract_camera(data)
    if any(k in data for k in _ELAPSED_KEYS + _INACTIVE_KEYS + _FINAL_KEYS):
        return evaluate(*args, **kwargs)
    return get_limits(*args, **kwargs)


def get_status(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return status(*args, **kwargs)


# ---------------------------------------------------------------------------
# Service class
# ---------------------------------------------------------------------------

class RecLimitsService:
    """Instance wrapper around the module-level limit registry."""

    def set_limits(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return set_limits(*args, **kwargs)

    def get_limits(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return get_limits(*args, **kwargs)

    def clear(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return clear_limits(*args, **kwargs)

    def clear_limits(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return clear_limits(*args, **kwargs)

    def update(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return set_limits(*args, **kwargs)

    def update_limits(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return set_limits(*args, **kwargs)

    def evaluate(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return evaluate(*args, **kwargs)

    def should_stop(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return evaluate(*args, **kwargs)

    def should_split(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return should_split(*args, **kwargs)

    def should_discard(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return should_discard(*args, **kwargs)

    def check(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return evaluate(*args, **kwargs)

    def check_limits(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return evaluate(*args, **kwargs)

    def check_duration(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return evaluate(*args, **kwargs)

    def check_inactivity(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return evaluate(*args, **kwargs)

    def is_too_long(self, *args: Any, **kwargs: Any) -> bool:
        return is_too_long(*args, **kwargs)

    def is_too_short(self, *args: Any, **kwargs: Any) -> bool:
        return is_too_short(*args, **kwargs)

    def is_inactive(self, *args: Any, **kwargs: Any) -> bool:
        return is_inactive(*args, **kwargs)

    def status(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return status(*args, **kwargs)

    # alias surface (repo convention: many names for the same op)
    set = set_limits
    get = get_limits
    list = get_limits
    all = get_limits
    reset = clear_limits
    purge = clear_limits
    evaluate_limits = evaluate
    check_limit = check_limits
    stop_check = should_stop
    split_check = should_split
    discard_check = should_discard


rec_limits_service = RecLimitsService()
rec_service = rec_limits_service
limits_service = rec_limits_service
service = rec_limits_service
manager = rec_limits_service
default_service = rec_limits_service
recording_limits_service = rec_limits_service


# Module-level aliases
get = get_limits
set = set_limits
update = update_limits
update_limit = update_limits
clear = clear_limits
reset = clear_limits
purge = clear_limits
remove_limits = clear_limits
delete_limits = clear_limits
list_limits = get_limits
all_limits = get_limits
fetch_limits = get_limits
retrieve_limits = get_limits
check = check_limits
check_limit = check_limits
evaluate_limits = evaluate
evaluate_recording = evaluate
evaluate_stop = evaluate
stop_check = should_stop
split_check = should_split
discard_check = should_discard


__all__ = [
    "RecLimitsService",
    "rec_limits_service",
    "set_limits",
    "get_limits",
    "clear_limits",
    "update_limits",
    "evaluate",
    "should_stop",
    "should_split",
    "should_discard",
    "check_limits",
    "check_duration",
    "check_inactivity",
    "is_too_long",
    "is_too_short",
    "is_inactive",
    "status",
    "get_status",
]
