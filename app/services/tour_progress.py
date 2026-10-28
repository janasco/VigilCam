"""PTZ tour live progress (computed view).

Read-only projection over the in-memory :mod:`app.services.ptz_tour`
engine. Exposes live execution progress for a running tour:

- ``current_preset_index`` (with ``current_index``/``index``/``position`` aliases)
- ``current_preset`` / ``current_preset_token``
- ``elapsed_seconds`` / ``elapsed`` since ``started_at`` (live wall-clock)
- dwell timing (``dwell_elapsed_seconds``, ``dwell_remaining_seconds``)
- ``progress_percent`` within the preset loop

No state is stored here; every call recomputes from the shared
``PTZ_TOURS`` store so no DB migration is required.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

_lock = threading.Lock()


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now_dt().isoformat()


def _parse_iso(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        # Handle trailing Z (UTC) which fromisoformat does not accept.
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _tour_id_of(*args, **kwargs) -> Optional[str]:
    data: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            data.update(a)
        elif isinstance(a, str):
            data.setdefault("id", a)
        elif isinstance(a, (int, float)) and not isinstance(a, bool):
            # Bare numeric positional arg is ambiguous (camera vs index);
            # only treat as id when no other id-like kwarg is present.
            data.setdefault("_pos", a)
    data.update(kwargs or {})
    for nested_key in ("payload", "options", "config", "params", "data", "body",
                       "request", "settings", "message", "tour", "patrol"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    for key in ("id", "tour_id", "tourId", "patrol_id", "patrolId", "uuid"):
        val = data.get(key)
        if val is not None:
            return str(val)
    low = {str(k).lower(): v for k, v in data.items()}
    for key in ("tour_id", "tourid", "patrol_id", "patrolid", "id", "uuid"):
        if key in low and low[key] is not None:
            return str(low[key])
    # Fall back to camera lookup when only a camera id was given.
    for key in ("camera_id", "camera", "cameraid", "cam", "device_id", "deviceid"):
        if key in data and data.get(key) is not None:
            return str(data.get(key))
        if key in low and low.get(key) is not None:
            return str(low.get(key))
    if "_pos" in data and data.get("_pos") is not None:
        return str(data.get("_pos"))
    return None


def _get_tour_record(sid: str) -> Dict[str, Any]:
    from app.services import ptz_tour as _tour_mod

    tour = _tour_mod.ptz_tour_service._find(sid)  # shared in-memory store
    if tour is None:
        raise KeyError(f"tour not found: {sid}")
    return tour


def _elapsed_seconds(tour: Dict[str, Any], now: datetime) -> float:
    """Live wall-clock seconds since the tour started.

    - running/paused: ``now - started_at``
    - stopped with ``stopped_at``: ``stopped_at - started_at`` (final)
    - never started: ``0.0``
    """
    started = _parse_iso(tour.get("started_at"))
    if started is None:
        return 0.0
    status = str(tour.get("status", "") or "").lower()
    stopped_at = _parse_iso(tour.get("stopped_at"))
    if status in ("stopped", "completed", "finished", "ended") and stopped_at is not None:
        return max(0.0, (stopped_at - started).total_seconds())
    return max(0.0, (now - started).total_seconds())


def _dwell_seconds(tour: Dict[str, Any]) -> float:
    for key in ("dwell_seconds", "dwell", "dwellSeconds", "dwell_time"):
        try:
            val = tour.get(key)
            if val is not None:
                return max(0.0, float(val))
        except (TypeError, ValueError):
            continue
    return 0.0


def _build_progress(tour: Dict[str, Any], now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or _now_dt()
    presets = tour.get("presets") or []
    total = len(presets)
    try:
        idx = int(tour.get("current_index", tour.get("index", 0)) or 0)
    except (TypeError, ValueError):
        idx = 0
    if total > 0:
        idx = max(0, min(idx, total - 1))
    else:
        idx = 0
    preset_tokens = [p.get("preset_token") for p in presets] if presets else []
    current_preset = tour.get("current_preset")
    if current_preset is None and presets:
        try:
            current_preset = presets[idx].get("preset_token")
        except (IndexError, AttributeError):
            current_preset = None
    elapsed = _elapsed_seconds(tour, now)
    dwell = _dwell_seconds(tour)
    updated = _parse_iso(tour.get("updated_at"))
    if updated is not None:
        dwell_elapsed = max(0.0, (now - updated).total_seconds())
    else:
        dwell_elapsed = 0.0
    dwell_remaining = max(0.0, dwell - dwell_elapsed) if dwell > 0 else 0.0
    if total > 0:
        progress_percent = round(((idx + 1) / total) * 100.0, 2)
    else:
        progress_percent = 0.0
    status = tour.get("status", "idle")
    state = tour.get("state", status)
    running = bool(tour.get("running")) or str(status).lower() == "running"
    paused = bool(tour.get("paused")) or str(status).lower() == "paused"
    sid = tour.get("id")
    camera_id = tour.get("camera_id")
    return {
        "id": sid,
        "tour_id": sid,
        "patrol_id": sid,
        "uuid": sid,
        "camera_id": camera_id,
        "camera": camera_id,
        "name": tour.get("name"),
        "title": tour.get("name", tour.get("title")),
        "status": status,
        "state": state,
        "running": running,
        "paused": paused,
        "enabled": tour.get("enabled"),
        "active": tour.get("active"),
        # Live position.
        "current_index": idx,
        "current_preset_index": idx,
        "index": idx,
        "position": idx,
        "current_preset": current_preset,
        "current_preset_token": current_preset,
        "preset_token": current_preset,
        "preset": current_preset,
        # Tour shape.
        "total_presets": total,
        "preset_count": total,
        "preset_total": total,
        "count": total,
        "preset_tokens": preset_tokens,
        "presets": preset_tokens,
        "visits": tour.get("visits", 0),
        "steps": tour.get("steps", 0),
        # Timing (live).
        "started_at": tour.get("started_at"),
        "updated_at": tour.get("updated_at"),
        "stopped_at": tour.get("stopped_at"),
        "elapsed_seconds": elapsed,
        "elapsed": elapsed,
        "elapsed_time": elapsed,
        "dwell_seconds": dwell,
        "dwell": dwell,
        "dwell_elapsed_seconds": dwell_elapsed,
        "dwell_remaining_seconds": dwell_remaining,
        "progress_percent": progress_percent,
        "progress": progress_percent,
        "checked_at": now.isoformat(),
        "ok": True,
        "success": True,
    }


def get_progress(*args, **kwargs) -> Dict[str, Any]:
    """Return live progress for one tour (current preset index + elapsed)."""
    sid = _tour_id_of(*args, **kwargs)
    if sid is None:
        raise KeyError("tour id is required")
    tour = _get_tour_record(sid)
    return _build_progress(dict(tour))


def get_tour_progress(*args, **kwargs) -> Dict[str, Any]:
    """Alias of :func:`get_progress`."""
    return get_progress(*args, **kwargs)


def get_status(*args, **kwargs) -> Dict[str, Any]:
    """Alias of :func:`get_progress` (status vocabulary)."""
    return get_progress(*args, **kwargs)


def status(*args, **kwargs) -> Dict[str, Any]:
    """Alias of :func:`get_progress`."""
    return get_progress(*args, **kwargs)


def list_progress(*args, **kwargs) -> List[Dict[str, Any]]:
    """Return live progress for all tours (optionally filtered by camera)."""
    from app.services import ptz_tour as _tour_mod

    data: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            data.update(a)
    data.update(kwargs or {})
    low = {str(k).lower(): v for k, v in data.items()}
    cam = data.get("camera_id", data.get("camera", data.get("cameraId")))
    if cam is None:
        cam = low.get("camera_id", low.get("camera"))
    now = _now_dt()
    with _lock:
        records = [dict(t) for t in _tour_mod.PTZ_TOURS.values()]
    if cam is not None:
        records = [t for t in records if str(t.get("camera_id")) == str(cam)]
    return [_build_progress(t, now) for t in records]


def list_tour_progress(*args, **kwargs) -> List[Dict[str, Any]]:
    """Alias of :func:`list_progress`."""
    return list_progress(*args, **kwargs)


# Generic aliases (alternate vocabulary).
def get(*a, **k): return get_progress(*a, **k)
def progress(*a, **k): return get_progress(*a, **k)
def list(*a, **k): return list_progress(*a, **k)
def all(*a, **k): return list_progress(*a, **k)


__all__ = [
    "get_progress",
    "get_tour_progress",
    "get_status",
    "status",
    "list_progress",
    "list_tour_progress",
]
