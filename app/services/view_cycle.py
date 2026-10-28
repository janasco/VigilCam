"""Group view cycler config service (in-memory).

A view cycle rotates the *displayed* group view (which camera group /
layout is shown on the monitor / wall display) on a dwell timer. This is
pure display/view switching config and never moves cameras -- unlike
:mod:`app.services.ptz_tour`, which drives PTZ presets on a camera.

Storage is in-memory so no DB migration is required.
"""

from __future__ import annotations

import builtins
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

VIEW_CYCLES: Dict[str, Dict[str, Any]] = {}
VIEW_CYCLE = VIEW_CYCLES
CYCLES = VIEW_CYCLES
VIEW_SESSIONS = VIEW_CYCLES
CYCLE_SESSIONS = VIEW_CYCLES
GROUP_VIEW_CYCLES = VIEW_CYCLES
GROUP_VIEW_CYCLE = VIEW_CYCLES
view_cycles = VIEW_CYCLES
view_cycle = VIEW_CYCLES
cycles = VIEW_CYCLES
group_view_cycles = VIEW_CYCLES
ITEMS = VIEW_CYCLES
JOBS = VIEW_CYCLES

_lock = threading.Lock()
DEFAULT_DWELL_SECONDS = 10.0
MIN_DWELL_SECONDS = 1.0
MAX_DWELL_SECONDS = 3600.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coerce_int(v, default=None):
    if v is None:
        return default
    if isinstance(v, bool):
        return int(v)
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return default


def _coerce_float(v, default=0.0):
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _coerce_dwell(v, default=DEFAULT_DWELL_SECONDS) -> float:
    d = _coerce_float(v, default)
    try:
        d = float(d)
    except (TypeError, ValueError):
        return float(default)
    if d < MIN_DWELL_SECONDS:
        return float(MIN_DWELL_SECONDS)
    if d > MAX_DWELL_SECONDS:
        return float(MAX_DWELL_SECONDS)
    return float(d)


def _as_view_list(value: Any, dwell: Any = None) -> List[Dict[str, Any]]:
    dwell_s = _coerce_dwell(dwell, DEFAULT_DWELL_SECONDS) if dwell is not None else DEFAULT_DWELL_SECONDS
    if value is None:
        return []
    items = value if isinstance(value, builtins.list) else [value]
    out: List[Dict[str, Any]] = []
    for it in items:
        if isinstance(it, builtins.dict):
            view_ref = (
                it.get("view") or it.get("view_id") or it.get("viewId")
                or it.get("group_id") or it.get("groupId") or it.get("group")
                or it.get("group_name") or it.get("layout") or it.get("layout_id")
                or it.get("layout_name") or it.get("name") or it.get("id")
            )
            d = it.get("dwell", it.get("dwell_seconds", it.get("dwellSeconds", it.get("interval", it.get("interval_seconds", dwell_s)))))
            try:
                d = _coerce_dwell(d, dwell_s)
            except (TypeError, ValueError):
                d = dwell_s
            if view_ref is None:
                continue
            entry: Dict[str, Any] = {
                "view": str(view_ref),
                "view_id": str(view_ref),
                "group": str(view_ref),
                "group_id": str(view_ref),
                "layout": str(view_ref),
                "name": str(it.get("name") or view_ref),
                "dwell": d,
                "dwell_seconds": d,
            }
            for k, v in it.items():
                entry.setdefault(k, v)
            out.append(entry)
        else:
            out.append({
                "view": str(it), "view_id": str(it),
                "group": str(it), "group_id": str(it),
                "layout": str(it), "name": str(it),
                "dwell": dwell_s, "dwell_seconds": dwell_s,
            })
    return out


def _norm(*args, **kwargs) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, builtins.dict):
            data.update(a)
        elif isinstance(a, (builtins.str, builtins.int)):
            if data.get("id") is None and isinstance(a, builtins.str) and len(a) > 8 and "-" in a:
                data.setdefault("id", a)
            elif data.get("name") is None and isinstance(a, builtins.str):
                # Bare string could be a view name; keep as name candidate.
                data.setdefault("name", a)
            else:
                try:
                    data.setdefault("interval_seconds", builtins.int(a))
                except (TypeError, ValueError):
                    data.setdefault("id", a)
        else:
            try:
                data.update({k: getattr(a, k) for k in dir(a)
                             if not k.startswith("_") and not callable(getattr(a, k, None))})
            except Exception:  # noqa: BLE001
                pass
    data.update(kwargs or {})
    for nk in ("payload", "options", "config", "params", "data", "body",
               "request", "settings", "message", "cycle", "view_cycle", "viewCycle"):
        nv = data.get(nk)
        if isinstance(nv, builtins.dict):
            for k, v in nv.items():
                data.setdefault(k, v)

    def get(keys):
        for k in keys:
            if k in data and data.get(k) is not None:
                return data.get(k)
        low = {str(k).lower(): v for k, v in data.items()}
        for k in keys:
            if k.lower() in low and low[k.lower()] is not None:
                return low[k.lower()]
        return None

    sid = get(("id", "cycle_id", "cycleId", "view_cycle_id", "viewCycleId", "uuid"))
    name = get(("name", "title", "label", "cycle_name", "cycleName"))
    views = get(("views", "view_list", "viewList", "stops", "waypoints",
                 "groups", "group_list", "groupList", "layouts", "layout_list",
                 "view_ids", "viewIds", "group_ids", "groupIds"))
    if views is None:
        single = get(("view", "view_id", "viewId", "group", "group_id", "groupId",
                      "group_name", "layout", "layout_id", "layout_name"))
        if single is not None:
            views = [single]
    dwell = get(("dwell", "dwell_seconds", "dwellSeconds", "dwell_time",
                 "interval", "interval_seconds", "intervalSeconds", "delay",
                 "seconds_per_view", "secondsPerView"))
    loop = get(("loop", "repeat", "cycle", "continuous"))
    enabled = get(("enabled", "active", "running", "is_enabled"))
    status = get(("status", "state"))
    index = get(("index", "current_index", "currentIndex", "position", "step"))
    direction = get(("direction", "order"))

    out: Dict[str, Any] = {}
    if sid is not None:
        out["id"] = sid
        out["cycle_id"] = sid
        out["view_cycle_id"] = sid
    if name is not None:
        out["name"] = name
    if views is not None:
        out["views"] = views
    if dwell is not None:
        out["dwell"] = dwell
        out["dwell_seconds"] = dwell
        out["interval_seconds"] = dwell
    if loop is not None:
        out["loop"] = bool(loop)
    if enabled is not None:
        out["enabled"] = bool(enabled)
    if status is not None:
        out["status"] = status
    if index is not None:
        out["index"] = index
        out["current_index"] = index
    if direction is not None:
        out["direction"] = direction
    for k, v in data.items():
        if k in out:
            continue
        if k in ("request", "db", "session", "user", "current_user"):
            continue
        out[k] = v
    return out


class ViewCycleService:
    """In-memory CRUD + execution engine for group view cycles.

    Cycles switch the displayed group view only; no camera motion is issued.
    """

    def __init__(self, *args, **kwargs):
        self.default_dwell = _coerce_dwell(
            kwargs.get("dwell", kwargs.get("dwell_seconds", kwargs.get("interval_seconds", DEFAULT_DWELL_SECONDS))),
            DEFAULT_DWELL_SECONDS)

    def _store(self, cycle: Dict[str, Any]) -> Dict[str, Any]:
        with _lock:
            VIEW_CYCLES[str(cycle["id"])] = cycle
        return cycle

    def _find(self, sid: Any) -> Optional[Dict[str, Any]]:
        if sid is None:
            return None
        key = str(sid)
        with _lock:
            if key in VIEW_CYCLES:
                return VIEW_CYCLES[key]
            for c in VIEW_CYCLES.values():
                if str(c.get("name", "")) == key:
                    return c
        return None

    def _remove(self, sid: Any) -> bool:
        key = str(sid)
        with _lock:
            if key in VIEW_CYCLES:
                VIEW_CYCLES.pop(key, None)
                return True
        return False

    def _make(self, data: Dict[str, Any]) -> Dict[str, Any]:
        sid = str(data.get("id") or data.get("cycle_id") or data.get("view_cycle_id") or uuid.uuid4())
        dwell_raw = data.get("dwell", data.get("dwell_seconds", data.get("interval_seconds", self.default_dwell)))
        dwell = _coerce_dwell(dwell_raw, self.default_dwell)
        views = _as_view_list(data.get("views", []), dwell)
        now = _now()
        cycle: Dict[str, Any] = {
            "id": sid, "cycle_id": sid, "view_cycle_id": sid, "uuid": sid,
            "name": data.get("name", f"view-cycle-{sid[:8]}"),
            "title": data.get("name", data.get("title")),
            "views": views, "stops": views, "waypoints": views, "groups": views,
            "view_names": [v.get("view") for v in views],
            "dwell": dwell, "dwell_seconds": dwell, "interval_seconds": dwell,
            "interval": dwell,
            "loop": bool(data.get("loop", True)),
            "repeat": bool(data.get("loop", True)),
            "enabled": True, "active": True,
            "status": "idle", "state": "idle",
            "running": False, "paused": False,
            "current_index": 0, "index": 0, "position": 0,
            "current_view": views[0].get("view") if views else None,
            "visits": 0, "steps": 0, "advances": 0,
            "created_at": now, "updated_at": now, "started_at": None,
            "stopped_at": None,
            "moves_camera": False, "ptz": False, "display_only": True,
            "ok": True, "success": True,
        }
        for k, v in data.items():
            if k in cycle or k in ("request", "db", "session", "user", "current_user"):
                continue
            cycle[k] = v
        return cycle

    # -- CRUD ---------------------------------------------------------
    def create_cycle(self, *args, **kwargs) -> Dict[str, Any]:
        data = _norm(*args, **kwargs)
        cycle = self._make(data)
        return self._store(cycle)

    def get_cycle(self, *args, **kwargs) -> Dict[str, Any]:
        data = _norm(*args, **kwargs)
        sid = data.get("id") or data.get("cycle_id") or data.get("view_cycle_id")
        if sid is None and args:
            sid = args[0] if not isinstance(args[0], dict) else None
        c = self._find(sid)
        if c is None:
            raise KeyError(f"view cycle not found: {sid}")
        return dict(c)

    def update_cycle(self, *args, **kwargs) -> Dict[str, Any]:
        data = _norm(*args, **kwargs)
        sid = data.get("id") or data.get("cycle_id") or data.get("view_cycle_id")
        if sid is None and args and not isinstance(args[0], dict):
            sid = args[0]
        c = self._find(sid)
        if c is None:
            # upsert: create with given id
            return self._store(self._make(data))
        if data.get("name") is not None:
            c["name"] = data["name"]
            c["title"] = data["name"]
        if data.get("views") is not None:
            dwell_raw = data.get("dwell", data.get("dwell_seconds", data.get("interval_seconds", c.get("dwell"))))
            dwell = _coerce_dwell(dwell_raw, self.default_dwell)
            c["views"] = _as_view_list(data["views"], dwell)
            c["stops"] = c["views"]
            c["waypoints"] = c["views"]
            c["groups"] = c["views"]
            c["view_names"] = [v.get("view") for v in c["views"]]
            if c.get("current_view") is None and c["views"]:
                c["current_view"] = c["views"][0].get("view")
        if data.get("dwell") is not None or data.get("dwell_seconds") is not None or data.get("interval_seconds") is not None:
            d = _coerce_dwell(data.get("dwell", data.get("dwell_seconds", data.get("interval_seconds"))), c.get("dwell"))
            c["dwell"] = d
            c["dwell_seconds"] = d
            c["interval_seconds"] = d
            c["interval"] = d
        if data.get("loop") is not None:
            c["loop"] = bool(data["loop"])
            c["repeat"] = bool(data["loop"])
        c["updated_at"] = _now()
        return self._store(c)

    def delete_cycle(self, *args, **kwargs) -> Dict[str, Any]:
        data = _norm(*args, **kwargs)
        sid = data.get("id") or data.get("cycle_id") or data.get("view_cycle_id")
        if sid is None and args and not isinstance(args[0], dict):
            sid = args[0]
        ok = self._remove(sid)
        return {"removed": ok, "deleted": ok, "id": sid, "ok": True, "success": True}

    def list_cycles(self, *args, **kwargs) -> List[Dict[str, Any]]:
        with _lock:
            items = [dict(c) for c in VIEW_CYCLES.values()]
        return items

    def clear(self, *args, **kwargs) -> int:
        with _lock:
            n = len(VIEW_CYCLES)
            VIEW_CYCLES.clear()
        return n

    # -- execution (display-only view switching) ----------------------
    def start_cycle(self, *args, **kwargs) -> Dict[str, Any]:
        data = _norm(*args, **kwargs)
        sid = data.get("id") or data.get("cycle_id") or data.get("view_cycle_id")
        if sid is None and data.get("name") is not None:
            with _lock:
                for c in VIEW_CYCLES.values():
                    if str(c.get("name")) == str(data.get("name")):
                        sid = c.get("id")
                        break
        c = self._find(sid)
        if c is None:
            if data.get("views") is not None or data.get("name") is not None:
                c = self._store(self._make(data))
            else:
                raise KeyError(f"view cycle not found: {sid}")
        if not c.get("views"):
            c.update({"status": "idle", "state": "idle", "running": False})
            return self._store(c)
        idx = _coerce_int(data.get("index", data.get("current_index")), 0) or 0
        idx = max(0, min(idx, len(c["views"]) - 1))
        now = _now()
        c.update({"status": "running", "state": "running", "running": True,
                  "paused": False, "enabled": True, "active": True,
                  "current_index": idx, "index": idx, "position": idx,
                  "current_view": c["views"][idx].get("view"),
                  "started_at": now, "updated_at": now,
                  "ok": True, "success": True})
        return self._store(c)

    def stop_cycle(self, *args, **kwargs) -> Dict[str, Any]:
        data = _norm(*args, **kwargs)
        sid = data.get("id") or data.get("cycle_id") or data.get("view_cycle_id") or data.get("name")
        if sid is None and args and not isinstance(args[0], dict):
            sid = args[0]
        c = self._find(sid)
        now = _now()
        if c is None:
            return {"id": sid, "cycle_id": sid, "status": "stopped", "state": "stopped",
                    "running": False, "ok": True, "success": True, "stopped_at": now}
        c.update({"status": "stopped", "state": "stopped", "running": False,
                  "paused": False, "enabled": False, "active": False,
                  "stopped_at": now, "updated_at": now, "ok": True, "success": True})
        return self._store(c)

    def pause_cycle(self, *args, **kwargs) -> Dict[str, Any]:
        data = _norm(*args, **kwargs)
        sid = data.get("id") or data.get("cycle_id") or data.get("view_cycle_id") or data.get("name")
        if sid is None and args and not isinstance(args[0], dict):
            sid = args[0]
        c = self._find(sid)
        if c is None:
            raise KeyError(f"view cycle not found: {sid}")
        c.update({"status": "paused", "state": "paused", "paused": True,
                  "running": False, "updated_at": _now(), "ok": True, "success": True})
        return self._store(c)

    def resume_cycle(self, *args, **kwargs) -> Dict[str, Any]:
        data = _norm(*args, **kwargs)
        sid = data.get("id") or data.get("cycle_id") or data.get("view_cycle_id") or data.get("name")
        if sid is None and args and not isinstance(args[0], dict):
            sid = args[0]
        c = self._find(sid)
        if c is None:
            raise KeyError(f"view cycle not found: {sid}")
        c.update({"status": "running", "state": "running", "paused": False,
                  "running": True, "enabled": True, "active": True,
                  "updated_at": _now(), "ok": True, "success": True})
        return self._store(c)

    def step_cycle(self, *args, **kwargs) -> Dict[str, Any]:
        data = _norm(*args, **kwargs)
        sid = data.get("id") or data.get("cycle_id") or data.get("view_cycle_id") or data.get("name")
        if sid is None and args and not isinstance(args[0], dict):
            sid = args[0]
        c = self._find(sid)
        if c is None:
            raise KeyError(f"view cycle not found: {sid}")
        views = c.get("views") or []
        if not views:
            c.update({"status": "idle", "updated_at": _now()})
            return self._store(c)
        if str(c.get("status", "")).lower() == "paused" and not kwargs.get("force"):
            return dict(c)
        idx = int(c.get("current_index", 0) or 0)
        steps = _coerce_int(data.get("steps", data.get("count")), 1) or 1
        idx = (idx + steps) % len(views) if c.get("loop", True) else min(idx + steps, len(views) - 1)
        c.update({"current_index": idx, "index": idx, "position": idx,
                  "current_view": views[idx].get("view"),
                  "visits": int(c.get("visits", 0) or 0) + 1,
                  "steps": int(c.get("steps", 0) or 0) + 1,
                  "advances": int(c.get("advances", 0) or 0) + 1,
                  "status": "running", "state": "running",
                  "running": True, "paused": False,
                  "updated_at": _now(), "ok": True, "success": True})
        return self._store(c)

    advance_cycle = step_cycle
    next_view = step_cycle

    def get_status(self, *args, **kwargs) -> Dict[str, Any]:
        data = _norm(*args, **kwargs)
        sid = data.get("id") or data.get("cycle_id") or data.get("view_cycle_id") or data.get("name")
        if sid is None and args and not isinstance(args[0], dict):
            sid = args[0]
        c = self._find(sid)
        if c is None:
            return {"id": sid, "cycle_id": sid, "status": "idle", "state": "idle",
                    "running": False, "ok": True, "success": True}
        out = dict(c)
        out.setdefault("status", "idle")
        return out

    def is_running(self, *args, **kwargs) -> bool:
        try:
            res = self.get_status(*args, **kwargs)
        except Exception:  # noqa: BLE001
            return False
        return bool(res.get("running")) or str(res.get("status", "")).lower() == "running"

    def tick(self, *args, **kwargs) -> Dict[str, Any]:
        limit = kwargs.get("limit")
        processed = []
        with _lock:
            ids = list(VIEW_CYCLES.keys())
        for sid in ids:
            if limit is not None and len(processed) >= (_coerce_int(limit, 0) or 0):
                break
            c = VIEW_CYCLES.get(sid)
            if not c or str(c.get("status", "")).lower() != "running":
                continue
            try:
                self.step_cycle(sid)
                processed.append(dict(VIEW_CYCLES.get(sid, c)))
            except Exception:  # noqa: BLE001
                continue
        return {"processed": len(processed), "count": len(processed),
                "items": processed, "cycles": processed, "ok": True, "success": True}

    # aliases
    def start(self, *a, **k): return self.start_cycle(*a, **k)
    def stop(self, *a, **k): return self.stop_cycle(*a, **k)
    def pause(self, *a, **k): return self.pause_cycle(*a, **k)
    def resume(self, *a, **k): return self.resume_cycle(*a, **k)
    def step(self, *a, **k): return self.step_cycle(*a, **k)
    def next(self, *a, **k): return self.step_cycle(*a, **k)
    def advance(self, *a, **k): return self.step_cycle(*a, **k)
    def status(self, *a, **k): return self.get_status(*a, **k)
    def get(self, *a, **k): return self.get_status(*a, **k)
    def list(self, *a, **k): return self.list_cycles(*a, **k)
    def all(self, *a, **k): return self.list_cycles(*a, **k)
    def create(self, *a, **k): return self.create_cycle(*a, **k)
    def add(self, *a, **k): return self.create_cycle(*a, **k)
    def update(self, *a, **k): return self.update_cycle(*a, **k)
    def remove(self, *a, **k): return self.delete_cycle(*a, **k)
    def delete(self, *a, **k): return self.delete_cycle(*a, **k)
    def reset(self, *a, **k): return self.clear(*a, **k)
    def process(self, *a, **k): return self.tick(*a, **k)
    def run(self, *a, **k): return self.tick(*a, **k)


view_cycle_service = ViewCycleService()
group_view_cycle_service = view_cycle_service
cycle_service = view_cycle_service
service = view_cycle_service
manager = view_cycle_service
default_service = view_cycle_service

ViewCycleServiceAlias = ViewCycleService
ViewCycle = ViewCycleService
GroupViewCycleService = ViewCycleService
CycleService = ViewCycleService


def create_cycle(*a, **k): return view_cycle_service.create_cycle(*a, **k)
def get_cycle(*a, **k): return view_cycle_service.get_cycle(*a, **k)
def update_cycle(*a, **k): return view_cycle_service.update_cycle(*a, **k)
def delete_cycle(*a, **k): return view_cycle_service.delete_cycle(*a, **k)
def list_cycles(*a, **k): return view_cycle_service.list_cycles(*a, **k)
def start_cycle(*a, **k): return view_cycle_service.start_cycle(*a, **k)
def stop_cycle(*a, **k): return view_cycle_service.stop_cycle(*a, **k)
def pause_cycle(*a, **k): return view_cycle_service.pause_cycle(*a, **k)
def resume_cycle(*a, **k): return view_cycle_service.resume_cycle(*a, **k)
def step_cycle(*a, **k): return view_cycle_service.step_cycle(*a, **k)
def advance_cycle(*a, **k): return view_cycle_service.step_cycle(*a, **k)
def next_view(*a, **k): return view_cycle_service.step_cycle(*a, **k)
def get_status(*a, **k): return view_cycle_service.get_status(*a, **k)
def is_running(*a, **k): return view_cycle_service.is_running(*a, **k)
def tick(*a, **k): return view_cycle_service.tick(*a, **k)
def clear(*a, **k): return view_cycle_service.clear(*a, **k)


# Group-view vocabulary aliases (same engine, alternate naming).
def create_view_cycle(*a, **k): return create_cycle(*a, **k)
def get_view_cycle(*a, **k): return get_cycle(*a, **k)
def update_view_cycle(*a, **k): return update_cycle(*a, **k)
def delete_view_cycle(*a, **k): return delete_cycle(*a, **k)
def list_view_cycles(*a, **k): return list_cycles(*a, **k)
def start_view_cycle(*a, **k): return start_cycle(*a, **k)
def stop_view_cycle(*a, **k): return stop_cycle(*a, **k)
def pause_view_cycle(*a, **k): return pause_cycle(*a, **k)
def resume_view_cycle(*a, **k): return resume_cycle(*a, **k)
def step_view_cycle(*a, **k): return step_cycle(*a, **k)


for _n, _f in {
    "create": create_cycle, "add": create_cycle, "get": get_status, "status": get_status,
    "start": start_cycle, "stop": stop_cycle,
    "pause": pause_cycle, "resume": resume_cycle, "step": step_cycle,
    "advance": advance_cycle, "next": next_view,
    "remove": delete_cycle, "delete": delete_cycle, "reset": clear, "purge": clear,
    "process": tick, "run": tick, "tick": tick,
    "create_cycle_alias": create_cycle, "list_cycles_alias": list_cycles,
}.items():
    globals().setdefault(_n, _f)

__all__ = [
    "ViewCycleService", "view_cycle_service", "VIEW_CYCLES",
    "DEFAULT_DWELL_SECONDS",
    "create_cycle", "get_cycle", "update_cycle", "delete_cycle", "list_cycles",
    "start_cycle", "stop_cycle", "pause_cycle", "resume_cycle", "step_cycle",
    "advance_cycle", "next_view", "get_status", "is_running", "tick", "clear",
]
