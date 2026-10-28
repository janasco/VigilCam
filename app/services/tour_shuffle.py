"""PTZ tour shuffle mode (in-memory).

Wraps the sequential :mod:`app.services.ptz_tour` engine with an opt-in
shuffle mode. The base engine always steps sequentially
(``current_index + 1 mod len(presets)``) and has no shuffle/random support;
this module layers shuffled-order stepping on top without modifying it.

State is in-memory so no DB migration is required. Each tour id maps to a
shuffle record::

    {"tour_id": ..., "enabled": bool, "seed": ..., "order": [indices],
     "position": int, ...}

``order`` is a permutation of ``range(len(presets))`` generated with
``random.Random(seed)`` so a caller-supplied seed reproduces the order.
Stepping in shuffle mode walks ``order`` cyclically; when a full cycle
completes the order is reshuffled (with the same seed logic) so tours keep
varying instead of repeating one fixed permutation.
"""

from __future__ import annotations

import builtins
import random
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

TOUR_SHUFFLE: Dict[str, Dict[str, Any]] = {}
SHUFFLE_STATES = TOUR_SHUFFLE
SHUFFLES = TOUR_SHUFFLE
tour_shuffle = TOUR_SHUFFLE
shuffle_states = TOUR_SHUFFLE

_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tour_id_of(*args, **kwargs) -> Optional[str]:
    data: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            data.update(a)
        elif isinstance(a, str):
            data.setdefault("tour_id", a)
    data.update(kwargs or {})
    for nested_key in ("payload", "options", "config", "params", "data", "body",
                       "request", "settings", "message", "tour", "patrol"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    for key in ("tour_id", "tourId", "patrol_id", "patrolId", "id", "uuid"):
        val = data.get(key)
        if val is not None:
            return str(val)
    low = {str(k).lower(): v for k, v in data.items()}
    for key in ("tour_id", "tourid", "patrol_id", "patrolid", "id", "uuid"):
        if key in low and low[key] is not None:
            return str(low[key])
    return None


def _seed_of(*args, **kwargs) -> Any:
    data: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            data.update(a)
    data.update(kwargs or {})
    for key in ("seed", "random_seed", "randomSeed", "shuffle_seed"):
        if data.get(key) is not None:
            return data.get(key)
    return None


def _enabled_of(*args, **kwargs) -> Optional[bool]:
    data: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            data.update(a)
    data.update(kwargs or {})
    for key in ("enabled", "active", "shuffle", "shuffle_enabled",
                "shuffleEnabled", "shuffle_mode", "shuffleMode"):
        if key in data and data.get(key) is not None:
            return bool(data.get(key))
    low = {str(k).lower(): v for k, v in data.items()}
    for key in ("enabled", "shuffle", "shuffle_enabled", "shuffleenabled",
                "shuffle_mode", "shufflemode"):
        if key in low and low[key] is not None:
            return bool(low[key])
    return None


def _get_tour(sid: str) -> Dict[str, Any]:
    from app.services import ptz_tour as _tour_mod

    tour = _tour_mod.ptz_tour_service._find(sid)  # shared in-memory store
    if tour is None:
        raise KeyError(f"tour not found: {sid}")
    return tour


def _preset_count(tour: Dict[str, Any]) -> int:
    return len(tour.get("presets") or [])


def _make_order(count: int, seed: Any = None) -> List[int]:
    order = builtins.list(range(count))
    rnd = random.Random(seed)
    rnd.shuffle(order)
    # Avoid the identity permutation for count > 1 so shuffle mode is
    # observably different from sequential order (unless a seed explicitly
    # reproduces it on retry -- then keep it to stay deterministic).
    if count > 1 and order == builtins.list(range(count)):
        order = order[1:] + order[:1]
    return order


def enable_shuffle(*args, **kwargs) -> Dict[str, Any]:
    """Enable shuffle mode for a tour and (re)generate its shuffled order."""
    sid = _tour_id_of(*args, **kwargs)
    if sid is None:
        raise KeyError("tour id is required")
    seed = _seed_of(*args, **kwargs)
    if seed is None and args:
        for a in args:
            if isinstance(a, (int, float)) and not isinstance(a, bool):
                seed = a
                break
    tour = _get_tour(sid)
    count = _preset_count(tour)
    order = _make_order(count, seed)
    now = _now()
    with _lock:
        state = TOUR_SHUFFLE.get(sid, {})
        state.update({
            "tour_id": sid, "id": sid, "patrol_id": sid,
            "enabled": True, "active": True,
            "shuffle": True, "shuffle_enabled": True, "shuffle_mode": True,
            "seed": seed, "order": order, "position": 0,
            "count": count, "updated_at": now, "ok": True, "success": True,
        })
        state.setdefault("created_at", now)
        TOUR_SHUFFLE[sid] = state
        return dict(state)


def disable_shuffle(*args, **kwargs) -> Dict[str, Any]:
    """Disable shuffle mode for a tour (sequential stepping resumes)."""
    sid = _tour_id_of(*args, **kwargs)
    if sid is None:
        raise KeyError("tour id is required")
    now = _now()
    with _lock:
        state = TOUR_SHUFFLE.get(sid, {"tour_id": sid, "id": sid})
        state.update({
            "tour_id": sid, "id": sid, "patrol_id": sid,
            "enabled": False, "active": False,
            "shuffle": False, "shuffle_enabled": False, "shuffle_mode": False,
            "updated_at": now, "ok": True, "success": True,
        })
        state.setdefault("created_at", now)
        TOUR_SHUFFLE[sid] = state
        return dict(state)


def set_shuffle(*args, **kwargs) -> Dict[str, Any]:
    """Set shuffle mode explicitly via an ``enabled`` flag."""
    enabled = _enabled_of(*args, **kwargs)
    if enabled is None:
        # Default action without an explicit flag is to enable.
        enabled = True
    if enabled:
        return enable_shuffle(*args, **kwargs)
    return disable_shuffle(*args, **kwargs)


def get_shuffle(*args, **kwargs) -> Dict[str, Any]:
    """Return the shuffle state for a tour (disabled default)."""
    sid = _tour_id_of(*args, **kwargs)
    if sid is None:
        raise KeyError("tour id is required")
    with _lock:
        state = TOUR_SHUFFLE.get(sid)
        if state is not None:
            return dict(state)
    return {"tour_id": sid, "id": sid, "patrol_id": sid,
            "enabled": False, "active": False,
            "shuffle": False, "shuffle_enabled": False, "shuffle_mode": False,
            "order": [], "position": 0, "ok": True, "success": True}


def is_shuffle_enabled(*args, **kwargs) -> bool:
    try:
        return bool(get_shuffle(*args, **kwargs).get("enabled"))
    except Exception:  # noqa: BLE001
        return False


def get_order(*args, **kwargs) -> List[int]:
    return builtins.list(get_shuffle(*args, **kwargs).get("order") or [])


def reshuffle(*args, **kwargs) -> Dict[str, Any]:
    """Regenerate the shuffled order for a tour (stays enabled)."""
    sid = _tour_id_of(*args, **kwargs)
    if sid is None:
        raise KeyError("tour id is required")
    seed = _seed_of(*args, **kwargs)
    with _lock:
        existing = TOUR_SHUFFLE.get(sid, {})
        if seed is None:
            seed = existing.get("seed")
    tour = _get_tour(sid)
    order = _make_order(_preset_count(tour), seed)
    now = _now()
    with _lock:
        state = TOUR_SHUFFLE.get(sid, {})
        state.update({
            "tour_id": sid, "id": sid, "patrol_id": sid,
            "enabled": True, "active": True,
            "shuffle": True, "shuffle_enabled": True, "shuffle_mode": True,
            "seed": seed, "order": order, "position": 0,
            "reshuffles": int(state.get("reshuffles", 0) or 0) + 1,
            "updated_at": now, "ok": True, "success": True,
        })
        state.setdefault("created_at", now)
        TOUR_SHUFFLE[sid] = state
        return dict(state)


def step_shuffled(*args, **kwargs) -> Dict[str, Any]:
    """Advance a tour to its next shuffled preset.

    Falls back to sequential :func:`ptz_tour.step_tour` when shuffle mode
    is not enabled for the tour.
    """
    from app.services import ptz_tour as _tour_mod

    sid = _tour_id_of(*args, **kwargs)
    if sid is None:
        raise KeyError("tour id is required")
    if not is_shuffle_enabled(sid):
        return _tour_mod.step_tour(sid)
    tour = _get_tour(sid)
    presets = tour.get("presets") or []
    if not presets:
        return dict(tour)
    with _lock:
        state = TOUR_SHUFFLE.get(sid)
        if not state or not state.get("order"):
            order = _make_order(len(presets), state.get("seed") if state else None)
            state = state or {"tour_id": sid, "id": sid}
            state.update({"order": order, "position": 0, "enabled": True})
            TOUR_SHUFFLE[sid] = state
        order = builtins.list(state.get("order") or [])
        pos = int(state.get("position", 0) or 0)
        # If the tour preset list changed size, regenerate the order.
        if len(order) != len(presets):
            order = _make_order(len(presets), state.get("seed"))
            pos = 0
        idx = order[pos % len(order)]
        pos += 1
        if pos >= len(order):
            # Full cycle consumed: reshuffle for the next cycle.
            order = _make_order(len(presets), state.get("seed"))
            pos = 0
        state.update({"order": order, "position": pos,
                      "visits": int(state.get("visits", 0) or 0) + 1,
                      "steps": int(state.get("steps", 0) or 0) + 1,
                      "updated_at": _now()})
        TOUR_SHUFFLE[sid] = state
    token = presets[idx].get("preset_token")
    tour.update({"current_index": idx, "index": idx, "position": idx,
                 "current_preset": token,
                 "visits": int(tour.get("visits", 0) or 0) + 1,
                 "steps": int(tour.get("steps", 0) or 0) + 1,
                 "status": "running", "state": "running",
                 "running": True, "paused": False,
                 "shuffle": True, "shuffle_enabled": True, "shuffle_mode": True,
                 "shuffle_order": builtins.list(TOUR_SHUFFLE[sid]["order"]),
                 "updated_at": _now(), "ok": True, "success": True})
    tour["move_result"] = _tour_mod._mock_move(token, tour.get("camera_id"))  # noqa: SLF001
    tour["last_step"] = dict(tour["move_result"])
    with _lock:
        _tour_mod.PTZ_TOURS[str(sid)] = tour
    return dict(tour)


def list_shuffles(*args, **kwargs) -> List[Dict[str, Any]]:
    with _lock:
        return [dict(v) for v in TOUR_SHUFFLE.values()]


def clear(*args, **kwargs) -> int:
    with _lock:
        n = len(TOUR_SHUFFLE)
        TOUR_SHUFFLE.clear()
    return n


# Aliases (alternate vocabulary).
def enable(*a, **k): return enable_shuffle(*a, **k)
def disable(*a, **k): return disable_shuffle(*a, **k)
def set_shuffle_mode(*a, **k): return set_shuffle(*a, **k)
def get_state(*a, **k): return get_shuffle(*a, **k)
def get_status(*a, **k): return get_shuffle(*a, **k)
def status(*a, **k): return get_shuffle(*a, **k)
def step(*a, **k): return step_shuffled(*a, **k)
def next(*a, **k): return step_shuffled(*a, **k)
def step_tour(*a, **k): return step_shuffled(*a, **k)
def step_tour_shuffled(*a, **k): return step_shuffled(*a, **k)
def shuffle_step(*a, **k): return step_shuffled(*a, **k)
def list(*a, **k): return list_shuffles(*a, **k)
def all(*a, **k): return list_shuffles(*a, **k)
def reset(*a, **k): return clear(*a, **k)


__all__ = [
    "TOUR_SHUFFLE",
    "enable_shuffle",
    "disable_shuffle",
    "set_shuffle",
    "get_shuffle",
    "is_shuffle_enabled",
    "get_order",
    "reshuffle",
    "step_shuffled",
    "list_shuffles",
    "clear",
]
