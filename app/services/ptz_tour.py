"""PTZ tour / patrol service (in-memory).

A tour (patrol) is an ordered list of presets visited in a loop with a
configurable dwell time. Execution is mocked: stepping advances the index
and records a mock move result without requiring camera hardware.
"""

from __future__ import annotations

import builtins
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

PTZ_TOURS: Dict[str, Dict[str, Any]] = {}
PTZ_TOUR = PTZ_TOURS
TOURS = PTZ_TOURS
TOUR_SESSIONS = PTZ_TOURS
PATROLS = PTZ_TOURS
PTZ_PATROLS = PTZ_TOURS
ptz_tours = PTZ_TOURS
ptz_tour = PTZ_TOURS
tours = PTZ_TOURS
patrols = PTZ_TOURS
tour_sessions = PTZ_TOURS
ITEMS = PTZ_TOURS
JOBS = PTZ_TOURS

_lock = threading.Lock()
DEFAULT_DWELL_SECONDS = 5.0


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


def _as_preset_list(value: Any, dwell: Any = None) -> List[Dict[str, Any]]:
    dwell_s = _coerce_float(dwell, DEFAULT_DWELL_SECONDS)
    if value is None:
        return []
    items = value if isinstance(value, builtins.list) else [value]
    out: List[Dict[str, Any]] = []
    for it in items:
        if isinstance(it, builtins.dict):
            token = (
                it.get("preset_token") or it.get("preset") or it.get("preset_id")
                or it.get("presetId") or it.get("token") or it.get("name")
                or it.get("id")
            )
            d = it.get("dwell", it.get("dwell_seconds", it.get("dwellSeconds", dwell_s)))
            try:
                d = float(d)
            except (TypeError, ValueError):
                d = dwell_s
            entry: Dict[str, Any] = {"preset_token": str(token) if token is not None else None,
                                     "preset": str(token) if token is not None else None,
                                     "dwell": d, "dwell_seconds": d}
            for k, v in it.items():
                entry.setdefault(k, v)
            out.append(entry)
        else:
            out.append({"preset_token": str(it), "preset": str(it),
                        "dwell": dwell_s, "dwell_seconds": dwell_s})
    return [e for e in out if e.get("preset_token")]


def _norm(*args, **kwargs) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, builtins.dict):
            data.update(a)
        elif isinstance(a, (builtins.str, builtins.int)):
            if data.get("id") is None and isinstance(a, builtins.str) and len(a) > 8 and "-" in a:
                data.setdefault("id", a)
            elif data.get("camera_id") is None:
                try:
                    data.setdefault("camera_id", builtins.int(a))
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
               "request", "settings", "message", "tour", "patrol"):
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

    camera_id = get(("camera_id", "camera", "cam", "cameraId", "cameraID", "cam_id",
                     "device_id", "deviceId"))
    sid = get(("id", "tour_id", "tourId", "patrol_id", "patrolId", "uuid"))
    name = get(("name", "title", "label", "tour_name"))
    presets = get(("presets", "preset_list", "presetList", "stops", "waypoints",
                   "preset_tokens", "presetTokens", "tokens"))
    if presets is None:
        single = get(("preset_token", "preset", "preset_id", "presetId", "token"))
        if single is not None:
            presets = [single]
    dwell = get(("dwell", "dwell_seconds", "dwellSeconds", "dwell_time",
                 "interval", "interval_seconds", "delay"))
    loop = get(("loop", "repeat", "cycle", "continuous"))
    enabled = get(("enabled", "active", "running", "is_enabled"))
    status = get(("status", "state"))
    index = get(("index", "current_index", "currentIndex", "position", "step"))
    direction = get(("direction", "order"))

    out: Dict[str, Any] = {}
    if camera_id is not None:
        out["camera_id"] = camera_id
        out["camera"] = camera_id
        out["cam"] = camera_id
        out["cameraId"] = camera_id
    if sid is not None:
        out["id"] = sid
        out["tour_id"] = sid
        out["patrol_id"] = sid
    if name is not None:
        out["name"] = name
    if presets is not None:
        out["presets"] = presets
    if dwell is not None:
        out["dwell"] = dwell
        out["dwell_seconds"] = dwell
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


def _mock_move(preset_token: Any, camera_id: Any = None) -> Dict[str, Any]:
    return {"success": True, "mock": True, "moved": True,
            "preset_token": preset_token, "preset": preset_token,
            "camera_id": camera_id, "status": "moved", "ok": True,
            "executed_at": _now()}


class PTZTourService:
    """In-memory CRUD + execution engine for PTZ tours/patrols."""

    def __init__(self, *args, **kwargs):
        self.default_dwell = _coerce_float(
            kwargs.get("dwell", kwargs.get("dwell_seconds", DEFAULT_DWELL_SECONDS)),
            DEFAULT_DWELL_SECONDS)
        self.mover = kwargs.get("mover", kwargs.get("ptz_service"))

    def _store(self, tour: Dict[str, Any]) -> Dict[str, Any]:
        with _lock:
            PTZ_TOURS[str(tour["id"])] = tour
        return tour

    def _find(self, sid: Any) -> Optional[Dict[str, Any]]:
        if sid is None:
            return None
        key = str(sid)
        with _lock:
            if key in PTZ_TOURS:
                return PTZ_TOURS[key]
            for t in PTZ_TOURS.values():
                if str(t.get("camera_id")) == key or str(t.get("camera")) == key:
                    return t
        return None

    def _remove(self, sid: Any) -> bool:
        key = str(sid)
        with _lock:
            if key in PTZ_TOURS:
                PTZ_TOURS.pop(key, None)
                return True
        return False

    def _make(self, data: Dict[str, Any]) -> Dict[str, Any]:
        sid = str(data.get("id") or data.get("tour_id") or data.get("patrol_id") or uuid.uuid4())
        camera_id = data.get("camera_id")
        try:
            camera_id = int(camera_id) if camera_id is not None else None
        except (TypeError, ValueError):
            pass
        dwell = _coerce_float(data.get("dwell", data.get("dwell_seconds")), self.default_dwell)
        presets = _as_preset_list(data.get("presets", []), dwell)
        now = _now()
        tour: Dict[str, Any] = {
            "id": sid, "tour_id": sid, "patrol_id": sid, "uuid": sid,
            "camera_id": camera_id, "camera": camera_id, "cam": camera_id,
            "cameraId": camera_id, "device_id": camera_id,
            "name": data.get("name", f"tour-{sid[:8]}"),
            "title": data.get("name", data.get("title")),
            "presets": presets, "stops": presets, "waypoints": presets,
            "preset_tokens": [p.get("preset_token") for p in presets],
            "dwell": dwell, "dwell_seconds": dwell,
            "loop": bool(data.get("loop", True)),
            "repeat": bool(data.get("loop", True)),
            "enabled": True, "active": True,
            "status": "idle", "state": "idle",
            "running": False, "paused": False,
            "current_index": 0, "index": 0, "position": 0,
            "current_preset": presets[0].get("preset_token") if presets else None,
            "visits": 0, "steps": 0,
            "created_at": now, "updated_at": now, "started_at": None,
            "stopped_at": None,
            "ok": True, "success": True,
        }
        for k, v in data.items():
            if k in tour or k in ("request", "db", "session", "user", "current_user"):
                continue
            tour[k] = v
        return tour

    # -- CRUD ---------------------------------------------------------
    def create_tour(self, *args, **kwargs) -> Dict[str, Any]:
        data = _norm(*args, **kwargs)
        tour = self._make(data)
        return self._store(tour)

    def get_tour(self, *args, **kwargs) -> Dict[str, Any]:
        data = _norm(*args, **kwargs)
        sid = data.get("id") or data.get("tour_id") or data.get("patrol_id")
        if sid is None and args:
            sid = args[0] if not isinstance(args[0], dict) else None
        t = self._find(sid)
        if t is None:
            raise KeyError(f"tour not found: {sid}")
        return dict(t)

    def update_tour(self, *args, **kwargs) -> Dict[str, Any]:
        data = _norm(*args, **kwargs)
        sid = data.get("id") or data.get("tour_id") or data.get("patrol_id")
        if sid is None and args and not isinstance(args[0], dict):
            sid = args[0]
        t = self._find(sid)
        if t is None:
            # upsert: create with given id
            return self._store(self._make(data))
        if data.get("camera_id") is not None:
            try:
                t["camera_id"] = int(data["camera_id"])
            except (TypeError, ValueError):
                t["camera_id"] = data["camera_id"]
            t["camera"] = t["camera_id"]
            t["cam"] = t["camera_id"]
        if data.get("name") is not None:
            t["name"] = data["name"]
            t["title"] = data["name"]
        if data.get("presets") is not None:
            dwell = _coerce_float(data.get("dwell", data.get("dwell_seconds", t.get("dwell"))),
                                  self.default_dwell)
            t["presets"] = _as_preset_list(data["presets"], dwell)
            t["stops"] = t["presets"]
            t["waypoints"] = t["presets"]
            t["preset_tokens"] = [p.get("preset_token") for p in t["presets"]]
        if data.get("dwell") is not None or data.get("dwell_seconds") is not None:
            d = _coerce_float(data.get("dwell", data.get("dwell_seconds")), t.get("dwell"))
            t["dwell"] = d
            t["dwell_seconds"] = d
        if data.get("loop") is not None:
            t["loop"] = bool(data["loop"])
        t["updated_at"] = _now()
        return self._store(t)

    def delete_tour(self, *args, **kwargs) -> Dict[str, Any]:
        data = _norm(*args, **kwargs)
        sid = data.get("id") or data.get("tour_id") or data.get("patrol_id")
        if sid is None and args and not isinstance(args[0], dict):
            sid = args[0]
        ok = self._remove(sid)
        return {"removed": ok, "deleted": ok, "id": sid, "ok": True, "success": True}

    def list_tours(self, *args, **kwargs) -> List[Dict[str, Any]]:
        data = _norm(*args, **kwargs)
        with _lock:
            items = [dict(t) for t in PTZ_TOURS.values()]
        cam = data.get("camera_id")
        if cam is not None:
            items = [t for t in items if str(t.get("camera_id")) == str(cam)]
        return items

    def clear(self, *args, **kwargs) -> int:
        with _lock:
            n = len(PTZ_TOURS)
            PTZ_TOURS.clear()
        return n

    # -- execution ----------------------------------------------------
    def start_tour(self, *args, **kwargs) -> Dict[str, Any]:
        data = _norm(*args, **kwargs)
        sid = data.get("id") or data.get("tour_id") or data.get("patrol_id")
        if sid is None and data.get("camera_id") is not None:
            with _lock:
                for t in PTZ_TOURS.values():
                    if str(t.get("camera_id")) == str(data.get("camera_id")):
                        sid = t.get("id")
                        break
        t = self._find(sid)
        if t is None:
            if data.get("presets") is not None or data.get("camera_id") is not None:
                t = self._store(self._make(data))
            else:
                raise KeyError(f"tour not found: {sid}")
        if not t.get("presets"):
            # allow starting empty tours; status reflects empty
            t.update({"status": "idle", "state": "idle", "running": False})
            return self._store(t)
        idx = _coerce_int(data.get("index", data.get("current_index")), 0) or 0
        idx = max(0, min(idx, len(t["presets"]) - 1))
        now = _now()
        t.update({"status": "running", "state": "running", "running": True,
                  "paused": False, "enabled": True, "active": True,
                  "current_index": idx, "index": idx, "position": idx,
                  "current_preset": t["presets"][idx].get("preset_token"),
                  "started_at": now, "updated_at": now,
                  "ok": True, "success": True})
        t["move_result"] = _mock_move(t["current_preset"], t.get("camera_id"))
        return self._store(t)

    def stop_tour(self, *args, **kwargs) -> Dict[str, Any]:
        data = _norm(*args, **kwargs)
        sid = data.get("id") or data.get("tour_id") or data.get("patrol_id") or data.get("camera_id")
        if sid is None and args and not isinstance(args[0], dict):
            sid = args[0]
        t = self._find(sid)
        now = _now()
        if t is None:
            return {"id": sid, "tour_id": sid, "status": "stopped", "state": "stopped",
                    "running": False, "ok": True, "success": True, "stopped_at": now}
        t.update({"status": "stopped", "state": "stopped", "running": False,
                  "paused": False, "enabled": False, "active": False,
                  "stopped_at": now, "updated_at": now, "ok": True, "success": True})
        return self._store(t)

    def pause_tour(self, *args, **kwargs) -> Dict[str, Any]:
        data = _norm(*args, **kwargs)
        sid = data.get("id") or data.get("tour_id") or data.get("patrol_id") or data.get("camera_id")
        if sid is None and args and not isinstance(args[0], dict):
            sid = args[0]
        t = self._find(sid)
        if t is None:
            raise KeyError(f"tour not found: {sid}")
        t.update({"status": "paused", "state": "paused", "paused": True,
                  "running": False, "updated_at": _now(), "ok": True, "success": True})
        return self._store(t)

    def resume_tour(self, *args, **kwargs) -> Dict[str, Any]:
        data = _norm(*args, **kwargs)
        sid = data.get("id") or data.get("tour_id") or data.get("patrol_id") or data.get("camera_id")
        if sid is None and args and not isinstance(args[0], dict):
            sid = args[0]
        t = self._find(sid)
        if t is None:
            raise KeyError(f"tour not found: {sid}")
        t.update({"status": "running", "state": "running", "paused": False,
                  "running": True, "enabled": True, "active": True,
                  "updated_at": _now(), "ok": True, "success": True})
        return self._store(t)

    def step_tour(self, *args, **kwargs) -> Dict[str, Any]:
        data = _norm(*args, **kwargs)
        sid = data.get("id") or data.get("tour_id") or data.get("patrol_id") or data.get("camera_id")
        if sid is None and args and not isinstance(args[0], dict):
            sid = args[0]
        t = self._find(sid)
        if t is None:
            raise KeyError(f"tour not found: {sid}")
        presets = t.get("presets") or []
        if not presets:
            t.update({"status": "idle", "updated_at": _now()})
            return self._store(t)
        if str(t.get("status", "")).lower() == "paused" and not kwargs.get("force"):
            return dict(t)
        idx = int(t.get("current_index", 0) or 0)
        steps = _coerce_int(data.get("steps", data.get("count")), 1) or 1
        idx = (idx + steps) % len(presets) if t.get("loop", True) else min(idx + steps, len(presets) - 1)
        t.update({"current_index": idx, "index": idx, "position": idx,
                  "current_preset": presets[idx].get("preset_token"),
                  "visits": int(t.get("visits", 0) or 0) + 1,
                  "steps": int(t.get("steps", 0) or 0) + 1,
                  "status": "running", "state": "running",
                  "running": True, "paused": False,
                  "updated_at": _now(), "ok": True, "success": True})
        t["move_result"] = _mock_move(t["current_preset"], t.get("camera_id"))
        t["last_step"] = dict(t["move_result"])
        return self._store(t)

    def get_status(self, *args, **kwargs) -> Dict[str, Any]:
        data = _norm(*args, **kwargs)
        sid = data.get("id") or data.get("tour_id") or data.get("patrol_id")
        if sid is None and data.get("camera_id") is not None:
            sid = data.get("camera_id")
        if sid is None and args and not isinstance(args[0], dict):
            sid = args[0]
        t = self._find(sid)
        if t is None:
            return {"id": sid, "tour_id": sid, "status": "idle", "state": "idle",
                    "running": False, "ok": True, "success": True}
        out = dict(t)
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
            ids = list(PTZ_TOURS.keys())
        for sid in ids:
            if limit is not None and len(processed) >= (_coerce_int(limit, 0) or 0):
                break
            t = PTZ_TOURS.get(sid)
            if not t or str(t.get("status", "")).lower() != "running":
                continue
            try:
                self.step_tour(sid)
                processed.append(dict(PTZ_TOURS.get(sid, t)))
            except Exception:  # noqa: BLE001
                continue
        return {"processed": len(processed), "count": len(processed),
                "items": processed, "tours": processed, "ok": True, "success": True}

    # aliases
    def start(self, *a, **k): return self.start_tour(*a, **k)
    def stop(self, *a, **k): return self.stop_tour(*a, **k)
    def pause(self, *a, **k): return self.pause_tour(*a, **k)
    def resume(self, *a, **k): return self.resume_tour(*a, **k)
    def step(self, *a, **k): return self.step_tour(*a, **k)
    def next(self, *a, **k): return self.step_tour(*a, **k)
    def status(self, *a, **k): return self.get_status(*a, **k)
    def get(self, *a, **k): return self.get_status(*a, **k)
    def list(self, *a, **k): return self.list_tours(*a, **k)
    def all(self, *a, **k): return self.list_tours(*a, **k)
    def create(self, *a, **k): return self.create_tour(*a, **k)
    def add(self, *a, **k): return self.create_tour(*a, **k)
    def update(self, *a, **k): return self.update_tour(*a, **k)
    def remove(self, *a, **k): return self.delete_tour(*a, **k)
    def delete(self, *a, **k): return self.delete_tour(*a, **k)
    def reset(self, *a, **k): return self.clear(*a, **k)
    def process(self, *a, **k): return self.tick(*a, **k)
    def run(self, *a, **k): return self.tick(*a, **k)


ptz_tour_service = PTZTourService()
ptz_patrol_service = ptz_tour_service
tour_service = ptz_tour_service
patrol_service = ptz_tour_service
service = ptz_tour_service
manager = ptz_tour_service
default_service = ptz_tour_service

PTZTourServiceAlias = PTZTourService
PTZTour = PTZTourService
PTZPatrolService = PTZTourService
TourService = PTZTourService
PatrolService = PTZTourService


def create_tour(*a, **k): return ptz_tour_service.create_tour(*a, **k)
def get_tour(*a, **k): return ptz_tour_service.get_tour(*a, **k)
def update_tour(*a, **k): return ptz_tour_service.update_tour(*a, **k)
def delete_tour(*a, **k): return ptz_tour_service.delete_tour(*a, **k)
def list_tours(*a, **k): return ptz_tour_service.list_tours(*a, **k)
def start_tour(*a, **k): return ptz_tour_service.start_tour(*a, **k)
def stop_tour(*a, **k): return ptz_tour_service.stop_tour(*a, **k)
def pause_tour(*a, **k): return ptz_tour_service.pause_tour(*a, **k)
def resume_tour(*a, **k): return ptz_tour_service.resume_tour(*a, **k)
def step_tour(*a, **k): return ptz_tour_service.step_tour(*a, **k)
def get_status(*a, **k): return ptz_tour_service.get_status(*a, **k)
def is_running(*a, **k): return ptz_tour_service.is_running(*a, **k)
def tick(*a, **k): return ptz_tour_service.tick(*a, **k)
def clear(*a, **k): return ptz_tour_service.clear(*a, **k)


# Patrol + generic aliases (same engine, alternate vocabulary).
def create_patrol(*a, **k): return create_tour(*a, **k)
def get_patrol(*a, **k): return get_tour(*a, **k)
def update_patrol(*a, **k): return update_tour(*a, **k)
def delete_patrol(*a, **k): return delete_tour(*a, **k)
def list_patrols(*a, **k): return list_tours(*a, **k)
def start_patrol(*a, **k): return start_tour(*a, **k)
def stop_patrol(*a, **k): return stop_tour(*a, **k)
def pause_patrol(*a, **k): return pause_tour(*a, **k)
def resume_patrol(*a, **k): return resume_tour(*a, **k)
def step_patrol(*a, **k): return step_tour(*a, **k)


for _n, _f in {
    "create": create_tour, "add": create_tour, "get": get_status, "status": get_status,
    "start": start_tour, "stop": stop_tour,
    "pause": pause_tour, "resume": resume_tour, "step": step_tour,
    "remove": delete_tour, "delete": delete_tour, "reset": clear, "purge": clear,
    "process": tick, "run": tick, "tick": tick,
    "create_tour_alias": create_tour, "list_tours_alias": list_tours,
}.items():
    globals().setdefault(_n, _f)

__all__ = [
    "PTZTourService", "ptz_tour_service", "PTZ_TOURS",
    "create_tour", "get_tour", "update_tour", "delete_tour", "list_tours",
    "start_tour", "stop_tour", "pause_tour", "resume_tour", "step_tour",
    "get_status", "is_running", "tick", "clear",
]
