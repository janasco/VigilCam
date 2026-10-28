"""Combined PTZ tour status (UI-ready single call).

Read-only projection combining three sources for one tour id:

- base tour record (:mod:`app.services.ptz_tour`)
- live progress (:mod:`app.services.tour_progress`)
- shuffle mode state (:mod:`app.services.tour_shuffle`)

No state is stored here; every call recomputes from the shared in-memory
stores so no DB migration is required. The combined payload is intended for
UI polling: a single GET returns tour + progress + shuffle state at once.
"""

from __future__ import annotations

import builtins
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

TOUR_STATUS: Dict[str, Dict[str, Any]] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tour_id_of(*args, **kwargs) -> Optional[str]:
    data: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            data.update(a)
        elif isinstance(a, str):
            data.setdefault("id", a)
        elif isinstance(a, (int, float)) and not isinstance(a, bool):
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
    for key in ("camera_id", "camera", "cameraid", "cam", "device_id", "deviceid"):
        if key in data and data.get(key) is not None:
            return str(data.get(key))
        if key in low and low.get(key) is not None:
            return str(low.get(key))
    if "_pos" in data and data.get("_pos") is not None:
        return str(data.get("_pos"))
    return None


def _resolve_tour(sid: str) -> Dict[str, Any]:
    from app.services import ptz_tour as _tour_mod

    tour = _tour_mod.ptz_tour_service._find(sid)  # shared in-memory store
    if tour is None:
        raise KeyError(f"tour not found: {sid}")
    return dict(tour)


def _safe_progress(sid: str) -> Dict[str, Any]:
    from app.services import tour_progress as _progress_mod

    try:
        return dict(_progress_mod.get_progress(sid))
    except Exception:  # noqa: BLE001
        return {}


def _safe_shuffle(sid: str) -> Dict[str, Any]:
    from app.services import tour_shuffle as _shuffle_mod

    try:
        return dict(_shuffle_mod.get_shuffle(sid))
    except Exception:  # noqa: BLE001
        return {"tour_id": sid, "id": sid, "enabled": False,
                "shuffle": False, "shuffle_enabled": False,
                "order": [], "position": 0}


def get_tour_status(*args, **kwargs) -> Dict[str, Any]:
    """Return combined tour + progress + shuffle state for one tour."""
    sid = _tour_id_of(*args, **kwargs)
    if sid is None:
        raise KeyError("tour id is required")
    tour = _resolve_tour(sid)
    # Re-key when the caller addressed the tour by camera id.
    real_sid = str(tour.get("id") or sid)
    progress = _safe_progress(real_sid)
    shuffle = _safe_shuffle(real_sid)

    shuffle_enabled = bool(
        shuffle.get("enabled") or shuffle.get("shuffle_enabled")
        or shuffle.get("shuffle") or shuffle.get("shuffle_mode")
        or tour.get("shuffle_enabled") or tour.get("shuffle")
    )
    order = shuffle.get("order") or tour.get("shuffle_order") or []
    try:
        position = int(shuffle.get("position", 0) or 0)
    except (TypeError, ValueError):
        position = 0

    status = progress.get("status", tour.get("status", "idle"))
    state = progress.get("state", tour.get("state", status))
    running = bool(progress.get("running", tour.get("running", False))) or \
        str(status).lower() == "running"
    paused = bool(progress.get("paused", tour.get("paused", False))) or \
        str(status).lower() == "paused"

    current_index = progress.get("current_preset_index",
                                 progress.get("current_index",
                                              tour.get("current_index", 0)))
    try:
        current_index = int(current_index or 0)
    except (TypeError, ValueError):
        current_index = 0
    current_preset = progress.get("current_preset", tour.get("current_preset"))

    total = progress.get("total_presets", progress.get("preset_count",
                                                       len(tour.get("presets") or [])))
    try:
        total = int(total or 0)
    except (TypeError, ValueError):
        total = 0

    combined: Dict[str, Any] = {
        "id": real_sid,
        "tour_id": real_sid,
        "patrol_id": real_sid,
        "uuid": real_sid,
        "camera_id": progress.get("camera_id", tour.get("camera_id")),
        "camera": progress.get("camera", tour.get("camera")),
        "name": progress.get("name", tour.get("name")),
        "title": progress.get("title", tour.get("title", tour.get("name"))),
        "status": status,
        "state": state,
        "running": running,
        "paused": paused,
        "enabled": tour.get("enabled", progress.get("enabled")),
        "active": tour.get("active", progress.get("active")),
        # Live position (progress wins, tour as fallback).
        "current_index": current_index,
        "current_preset_index": current_index,
        "index": current_index,
        "position": current_index,
        "current_preset": current_preset,
        "current_preset_token": progress.get("current_preset_token", current_preset),
        "preset": current_preset,
        "preset_token": current_preset,
        # Tour shape.
        "total_presets": total,
        "preset_count": total,
        "preset_total": total,
        "count": total,
        "preset_tokens": progress.get("preset_tokens", tour.get("preset_tokens", [])),
        "presets": progress.get("preset_tokens", tour.get("preset_tokens", [])),
        "visits": progress.get("visits", tour.get("visits", 0)),
        "steps": progress.get("steps", tour.get("steps", 0)),
        # Timing (live from progress).
        "started_at": progress.get("started_at", tour.get("started_at")),
        "updated_at": progress.get("updated_at", tour.get("updated_at")),
        "stopped_at": progress.get("stopped_at", tour.get("stopped_at")),
        "elapsed_seconds": progress.get("elapsed_seconds", 0.0),
        "elapsed": progress.get("elapsed", 0.0),
        "dwell_seconds": progress.get("dwell_seconds", tour.get("dwell_seconds", 0.0)),
        "dwell": progress.get("dwell", tour.get("dwell", 0.0)),
        "dwell_elapsed_seconds": progress.get("dwell_elapsed_seconds", 0.0),
        "dwell_remaining_seconds": progress.get("dwell_remaining_seconds", 0.0),
        "progress_percent": progress.get("progress_percent",
                                         progress.get("progress", 0.0)),
        "progress": progress.get("progress", progress.get("progress_percent", 0.0)),
        # Shuffle state (flattened for UI toggles).
        "shuffle_enabled": shuffle_enabled,
        "shuffle": shuffle_enabled,
        "shuffle_mode": shuffle_enabled,
        "shuffle_order": builtins.list(order) if isinstance(order, builtins.list) else order,
        "order": builtins.list(order) if isinstance(order, builtins.list) else order,
        "shuffle_position": position,
        "shuffle_seed": shuffle.get("seed"),
        # Nested sections for detailed UI rendering.
        "tour": dict(tour),
        "progress": dict(progress) if progress else {},
        "shuffle": dict(shuffle) if shuffle else {},
        "checked_at": _now_iso(),
        "ok": True,
        "success": True,
    }
    return combined


def list_tour_statuses(*args, **kwargs) -> List[Dict[str, Any]]:
    """Return combined status for all tours (optionally filtered by camera)."""
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
    records = [dict(t) for t in _tour_mod.PTZ_TOURS.values()]
    if cam is not None:
        records = [t for t in records if str(t.get("camera_id")) == str(cam)]
    out: List[Dict[str, Any]] = []
    for t in records:
        try:
            out.append(get_tour_status(t.get("id")))
        except KeyError:
            continue
    return out


def get_status(*args, **kwargs) -> Dict[str, Any]:
    """Alias of :func:`get_tour_status` (status vocabulary)."""
    return get_tour_status(*args, **kwargs)


def status(*args, **kwargs) -> Dict[str, Any]:
    """Alias of :func:`get_tour_status`."""
    return get_tour_status(*args, **kwargs)


def get(*args, **kwargs) -> Dict[str, Any]:
    """Alias of :func:`get_tour_status`."""
    return get_tour_status(*args, **kwargs)


def get_combined_status(*args, **kwargs) -> Dict[str, Any]:
    """Alias of :func:`get_tour_status`."""
    return get_tour_status(*args, **kwargs)


def list_statuses(*args, **kwargs) -> List[Dict[str, Any]]:
    """Alias of :func:`list_tour_statuses`."""
    return list_tour_statuses(*args, **kwargs)


def list(*args, **kwargs) -> List[Dict[str, Any]]:  # noqa: A001
    """Alias of :func:`list_tour_statuses`."""
    return list_tour_statuses(*args, **kwargs)


def all(*args, **kwargs) -> List[Dict[str, Any]]:  # noqa: A001
    """Alias of :func:`list_tour_statuses`."""
    return list_tour_statuses(*args, **kwargs)


__all__ = [
    "TOUR_STATUS",
    "get_tour_status",
    "get_combined_status",
    "get_status",
    "status",
    "get",
    "list_tour_statuses",
    "list_statuses",
]
