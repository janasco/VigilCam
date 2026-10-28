"""PTZ idle home-return service (in-memory).

Returns a camera to its configured home preset after N idle seconds.

This is deliberately distinct from existing PTZ services:
- :mod:`app.services.ptz_tour` runs patrols (ordered preset loops).
- :mod:`app.services.patrol_schedule` triggers tours on a time schedule.
- :mod:`app.services.ptz_tracking` follows objects and computes velocities.

This module does none of those: it only tracks per-camera last-activity
timestamps and, when a camera has been idle longer than its configured
``idle_seconds``, records a mock move back to its ``home_preset``.
No hardware is required; moves are mocked.
"""

from __future__ import annotations

import builtins
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

PTZ_IDLE_CONFIGS: Dict[str, Dict[str, Any]] = {}
PTZ_IDLE = PTZ_IDLE_CONFIGS
IDLE_CONFIGS = PTZ_IDLE_CONFIGS
IDLE_STATE = PTZ_IDLE_CONFIGS
PTZ_IDLE_STATE = PTZ_IDLE_CONFIGS
ptz_idle_configs = PTZ_IDLE_CONFIGS
idle_configs = PTZ_IDLE_CONFIGS
CONFIGS = PTZ_IDLE_CONFIGS
ITEMS = PTZ_IDLE_CONFIGS
JOBS = PTZ_IDLE_CONFIGS

_lock = threading.RLock()
DEFAULT_IDLE_SECONDS = 300.0


def _now_ts() -> float:
    return time.time()


def _now_iso(ts: Optional[float] = None) -> str:
    if ts is None:
        return datetime.now(timezone.utc).isoformat()
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _coerce_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default


def _key(camera_id: Any) -> str:
    return str(camera_id)


def _norm(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            data.update(a)
        elif isinstance(a, (str, int)):
            # Bare camera id or preset token.
            if data.get("camera_id") is None:
                try:
                    # Prefer numeric camera ids when possible.
                    data.setdefault("camera_id", int(a))  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    s = str(a)
                    if "-" in s and len(s) > 8:
                        data.setdefault("id", s)
                    else:
                        data.setdefault("camera_id", s)
            elif data.get("home_preset") is None and isinstance(a, str):
                data.setdefault("home_preset", a)
        else:
            try:
                data.update(
                    {
                        k: getattr(a, k)
                        for k in dir(a)
                        if not k.startswith("_") and not callable(getattr(a, k, None))
                    }
                )
            except Exception:  # noqa: BLE001
                pass
    data.update(kwargs or {})
    for nested_key in (
        "payload",
        "options",
        "config",
        "params",
        "data",
        "body",
        "request",
        "settings",
        "message",
        "idle",
        "ptz_idle",
    ):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _pick(data: Dict[str, Any], keys: tuple, default: Any = None) -> Any:
    for k in keys:
        if k in data and data.get(k) is not None:
            return data.get(k)
    low = {str(k).lower(): v for k, v in data.items()}
    for k in keys:
        if k.lower() in low and low[k.lower()] is not None:
            return low[k.lower()]
    return default


_CAMERA_KEYS = ("camera_id", "camera", "cam", "cameraId", "cameraID", "cam_id", "device_id", "deviceId")
_HOME_KEYS = ("home_preset", "home", "homePreset", "preset_token", "preset", "preset_id", "presetId", "token")
_IDLE_KEYS = ("idle_seconds", "idleSeconds", "idle", "timeout", "timeout_seconds", "delay", "delay_seconds", "seconds", "interval")
_ENABLED_KEYS = ("enabled", "active", "is_enabled", "on")


def _mock_home_move(camera_id: Any, home_preset: Any) -> Dict[str, Any]:
    return {
        "success": True,
        "mock": True,
        "moved": True,
        "camera_id": camera_id,
        "camera": camera_id,
        "preset_token": home_preset,
        "preset": home_preset,
        "home_preset": home_preset,
        "status": "moved",
        "ok": True,
        "executed_at": _now_iso(),
    }


class PTZIdleService:
    """In-memory per-camera idle home-return configuration and checks."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.default_idle_seconds = _coerce_float(
            kwargs.get("idle_seconds", kwargs.get("default_idle_seconds", DEFAULT_IDLE_SECONDS)),
            DEFAULT_IDLE_SECONDS,
        )
        self.mover = kwargs.get("mover", kwargs.get("ptz_service"))

    # -- internals ------------------------------------------------------
    def _store(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        with _lock:
            PTZ_IDLE_CONFIGS[_key(cfg["camera_id"])] = cfg
        return cfg

    def _find(self, camera_id: Any) -> Optional[Dict[str, Any]]:
        if camera_id is None:
            return None
        with _lock:
            return PTZ_IDLE_CONFIGS.get(_key(camera_id))

    def _make(self, data: Dict[str, Any]) -> Dict[str, Any]:
        camera_id = _pick(data, _CAMERA_KEYS)
        if camera_id is None:
            raise ValueError("camera_id is required")
        try:
            camera_id = int(camera_id)
        except (TypeError, ValueError):
            pass
        home = _pick(data, _HOME_KEYS)
        idle_s = _pick(data, _IDLE_KEYS, self.default_idle_seconds)
        idle_s = _coerce_float(idle_s, self.default_idle_seconds)
        if idle_s <= 0:
            idle_s = self.default_idle_seconds
        enabled = _pick(data, _ENABLED_KEYS, True)
        enabled = bool(enabled) if enabled is not None else True
        now_ts = _now_ts()
        # Allow tests to seed last-activity explicitly.
        last_ts = _pick(data, ("last_activity_ts", "lastActivityTs", "last_seen_ts"), None)
        if last_ts is None:
            last_seen = _pick(data, ("last_activity", "lastActivity", "last_seen", "lastSeen"), None)
            if last_seen is not None:
                try:
                    last_ts = datetime.fromisoformat(str(last_seen)).timestamp()
                except Exception:  # noqa: BLE001
                    last_ts = _coerce_float(last_seen, now_ts)
            else:
                last_ts = now_ts
        else:
            last_ts = _coerce_float(last_ts, now_ts)
        cfg: Dict[str, Any] = {
            "id": _key(camera_id),
            "camera_id": camera_id,
            "camera": camera_id,
            "cam": camera_id,
            "cameraId": camera_id,
            "home_preset": home,
            "home": home,
            "preset_token": home,
            "preset": home,
            "idle_seconds": idle_s,
            "idleSeconds": idle_s,
            "timeout": idle_s,
            "enabled": enabled,
            "active": enabled,
            "last_activity_ts": float(last_ts),
            "last_activity": _now_iso(float(last_ts)),
            "last_return_at": None,
            "last_return_ts": None,
            "return_count": 0,
            "returns": 0,
            "status": "configured" if enabled else "disabled",
            "state": "configured" if enabled else "disabled",
            "created_at": _now_iso(now_ts),
            "updated_at": _now_iso(now_ts),
            "ok": True,
            "success": True,
        }
        for k, v in data.items():
            if k in cfg or k in ("request", "db", "session", "user", "current_user"):
                continue
            cfg[k] = v
        return cfg

    # -- CRUD -----------------------------------------------------------
    def configure(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        data = _norm(*args, **kwargs)
        camera_id = _pick(data, _CAMERA_KEYS)
        if camera_id is None and args and not isinstance(args[0], dict):
            camera_id = args[0]
            data["camera_id"] = camera_id
        existing = self._find(camera_id) if camera_id is not None else None
        if existing is None:
            return self._store(self._make(data))
        return self.update(*args, **kwargs)

    def update(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        data = _norm(*args, **kwargs)
        camera_id = _pick(data, _CAMERA_KEYS)
        if camera_id is None and args and not isinstance(args[0], dict):
            camera_id = args[0]
        cfg = self._find(camera_id)
        if cfg is None:
            return self._store(self._make(data))
        home = _pick(data, _HOME_KEYS, None)
        if home is not None:
            cfg["home_preset"] = home
            cfg["home"] = home
            cfg["preset_token"] = home
            cfg["preset"] = home
        idle_s = _pick(data, _IDLE_KEYS, None)
        if idle_s is not None:
            idle_s = _coerce_float(idle_s, cfg.get("idle_seconds", self.default_idle_seconds))
            if idle_s and idle_s > 0:
                cfg["idle_seconds"] = idle_s
                cfg["idleSeconds"] = idle_s
                cfg["timeout"] = idle_s
        enabled = _pick(data, _ENABLED_KEYS, None)
        if enabled is not None:
            cfg["enabled"] = bool(enabled)
            cfg["active"] = bool(enabled)
            cfg["status"] = "configured" if bool(enabled) else "disabled"
            cfg["state"] = cfg["status"]
        cfg["updated_at"] = _now_iso()
        cfg["ok"] = True
        cfg["success"] = True
        return self._store(cfg)

    def remove(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        data = _norm(*args, **kwargs)
        camera_id = _pick(data, _CAMERA_KEYS + ("id",))
        if camera_id is None and args and not isinstance(args[0], dict):
            camera_id = args[0]
        with _lock:
            existed = _key(camera_id) in PTZ_IDLE_CONFIGS if camera_id is not None else False
            if existed:
                PTZ_IDLE_CONFIGS.pop(_key(camera_id), None)
        return {"removed": bool(existed), "deleted": bool(existed), "camera_id": camera_id, "ok": True, "success": True}

    def get(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        data = _norm(*args, **kwargs)
        camera_id = _pick(data, _CAMERA_KEYS + ("id",))
        if camera_id is None and args and not isinstance(args[0], dict):
            camera_id = args[0]
        cfg = self._find(camera_id)
        if cfg is None:
            raise KeyError(f"ptz idle config not found: {camera_id}")
        return dict(cfg)

    def list(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        with _lock:
            return [dict(c) for c in PTZ_IDLE_CONFIGS.values()]

    def clear(self, *args: Any, **kwargs: Any) -> int:
        with _lock:
            n = len(PTZ_IDLE_CONFIGS)
            PTZ_IDLE_CONFIGS.clear()
        return n

    # -- activity -------------------------------------------------------
    def notify_activity(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        """Mark a camera as active now (resets its idle timer)."""
        data = _norm(*args, **kwargs)
        camera_id = _pick(data, _CAMERA_KEYS + ("id",))
        if camera_id is None and args and not isinstance(args[0], dict):
            camera_id = args[0]
        cfg = self._find(camera_id)
        ts_raw = _pick(data, ("now", "ts", "timestamp", "at"), None)
        now_ts = _coerce_float(ts_raw, _now_ts()) if ts_raw is not None else _now_ts()
        if cfg is None:
            # Auto-create a disabled-by-default entry? Keep enabled so the
            # timer starts; home preset may be set later.
            cfg = self._make({"camera_id": camera_id, "last_activity_ts": now_ts})
            return self._store(cfg)
        cfg["last_activity_ts"] = float(now_ts)
        cfg["last_activity"] = _now_iso(float(now_ts))
        cfg["updated_at"] = _now_iso()
        return self._store(cfg)

    def get_status(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        data = _norm(*args, **kwargs)
        camera_id = _pick(data, _CAMERA_KEYS + ("id",))
        if camera_id is None and args and not isinstance(args[0], dict):
            camera_id = args[0]
        now_ts = _now_ts()
        now_override = _pick(data, ("now", "ts", "timestamp", "at"), None)
        if now_override is not None:
            now_ts = _coerce_float(now_override, now_ts)
        cfg = self._find(camera_id)
        if cfg is None:
            return {
                "camera_id": camera_id,
                "configured": False,
                "enabled": False,
                "idle_seconds": None,
                "elapsed": 0.0,
                "due": False,
                "status": "unconfigured",
                "ok": True,
                "success": True,
            }
        elapsed = max(0.0, now_ts - float(cfg.get("last_activity_ts", now_ts)))
        idle_s = float(cfg.get("idle_seconds", self.default_idle_seconds))
        due = bool(cfg.get("enabled")) and cfg.get("home_preset") is not None and elapsed >= idle_s
        out = dict(cfg)
        out.update(
            {
                "configured": True,
                "elapsed": elapsed,
                "elapsed_seconds": elapsed,
                "idle_elapsed": elapsed,
                "due": due,
                "is_due": due,
                "now_ts": now_ts,
                "now": _now_iso(now_ts),
                "ok": True,
                "success": True,
            }
        )
        return out

    def is_due(self, *args: Any, **kwargs: Any) -> bool:
        try:
            return bool(self.get_status(*args, **kwargs).get("due"))
        except Exception:  # noqa: BLE001
            return False

    # -- idle check / home return --------------------------------------
    def check(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        """Return idle cameras to their home preset (mock move)."""
        data = _norm(*args, **kwargs)
        now_ts = _now_ts()
        now_override = _pick(data, ("now", "ts", "timestamp", "at"), None)
        if now_override is not None:
            now_ts = _coerce_float(now_override, now_ts)
        force = bool(data.get("force", False))
        camera_filter = _pick(data, _CAMERA_KEYS + ("id",), None)
        limit = _pick(data, ("limit", "count"), None)
        limit_n = _coerce_int(limit, None) if limit is not None else None

        with _lock:
            ids = builtins.list(PTZ_IDLE_CONFIGS.keys())
        if camera_filter is not None:
            ids = [_key(camera_filter)] if _key(camera_filter) in PTZ_IDLE_CONFIGS else []

        returned: List[Dict[str, Any]] = []
        skipped = 0
        for cid in ids:
            if limit_n is not None and len(returned) >= limit_n:
                break
            cfg = PTZ_IDLE_CONFIGS.get(cid)
            if not cfg:
                continue
            if not force and not bool(cfg.get("enabled", True)):
                skipped += 1
                continue
            home = cfg.get("home_preset")
            if home is None:
                skipped += 1
                continue
            elapsed = max(0.0, now_ts - float(cfg.get("last_activity_ts", now_ts)))
            idle_s = float(cfg.get("idle_seconds", self.default_idle_seconds))
            if not force and elapsed < idle_s:
                skipped += 1
                continue
            move_result = _mock_home_move(cfg.get("camera_id"), home)
            # Attempt real mover when one is configured; never fail the check.
            mover = cfg.get("mover", self.mover) or self.mover
            if mover is not None:
                try:
                    fn = getattr(mover, "goto_preset", None) or getattr(mover, "goto", None)
                    if callable(fn):
                        try:
                            fn(cfg.get("camera_id"), home)
                        except TypeError:
                            try:
                                fn(home)
                            except Exception:  # noqa: BLE001
                                pass
                except Exception:  # noqa: BLE001
                    pass
            cfg["last_return_at"] = _now_iso(now_ts)
            cfg["last_return_ts"] = float(now_ts)
            cfg["return_count"] = int(cfg.get("return_count", 0) or 0) + 1
            cfg["returns"] = cfg["return_count"]
            cfg["last_move"] = move_result
            cfg["move_result"] = move_result
            cfg["updated_at"] = _now_iso(now_ts)
            cfg["status"] = "returned_home"
            cfg["state"] = "returned_home"
            # Reset idle timer on return so we don't re-fire every tick.
            cfg["last_activity_ts"] = float(now_ts)
            cfg["last_activity"] = _now_iso(now_ts)
            returned.append(
                {
                    "camera_id": cfg.get("camera_id"),
                    "camera": cfg.get("camera_id"),
                    "home_preset": home,
                    "preset_token": home,
                    "elapsed": elapsed,
                    "idle_seconds": idle_s,
                    "move_result": move_result,
                    "returned": True,
                    "ok": True,
                    "success": True,
                }
            )
        return {
            "processed": len(returned),
            "count": len(returned),
            "returned": returned,
            "items": returned,
            "results": returned,
            "skipped": skipped,
            "skipped_count": skipped,
            "checked_at": _now_iso(now_ts),
            "ok": True,
            "success": True,
            "status": "completed",
        }

    def return_home(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        data = dict(_norm(*args, **kwargs))
        data["force"] = True
        res = self.check(**data)
        items = res.get("returned") or []
        camera_filter = _pick(_norm(*args, **kwargs), _CAMERA_KEYS + ("id",), None)
        if camera_filter is not None:
            for item in items:
                if str(item.get("camera_id")) == str(camera_filter):
                    out = dict(item)
                    out.update({"status": "returned_home", "state": "returned_home"})
                    return out
            cfg = self._find(camera_filter)
            if cfg is not None and cfg.get("home_preset") is not None:
                move_result = _mock_home_move(cfg.get("camera_id"), cfg.get("home_preset"))
                cfg["last_return_at"] = _now_iso()
                cfg["last_return_ts"] = _now_ts()
                cfg["return_count"] = int(cfg.get("return_count", 0) or 0) + 1
                cfg["returns"] = cfg["return_count"]
                cfg["last_move"] = move_result
                cfg["move_result"] = move_result
                return {
                    "camera_id": cfg.get("camera_id"),
                    "home_preset": cfg.get("home_preset"),
                    "move_result": move_result,
                    "returned": True,
                    "status": "returned_home",
                    "ok": True,
                    "success": True,
                }
            raise KeyError(f"ptz idle config not found: {camera_filter}")
        if items:
            out = dict(items[0])
            out.update({"status": "returned_home", "state": "returned_home"})
            return out
        return {"returned": False, "processed": 0, "status": "no_return", "ok": True, "success": True}

    # -- aliases ---------------------------------------------------------
    def enable(self, *a: Any, **k: Any) -> Dict[str, Any]:
        d = _norm(*a, **k)
        d["enabled"] = True
        return self.update(**d)

    def disable(self, *a: Any, **k: Any) -> Dict[str, Any]:
        d = _norm(*a, **k)
        d["enabled"] = False
        return self.update(**d)

    def set_home(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.configure(*a, **k)

    def tick(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.check(*a, **k)

    def process(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.check(*a, **k)

    def run(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.check(*a, **k)

    def status(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.get_status(*a, **k)

    def list_configs(self, *a: Any, **k: Any) -> List[Dict[str, Any]]:
        return self.list(*a, **k)


ptz_idle_service = PTZIdleService()
idle_service = ptz_idle_service
service = ptz_idle_service
manager = ptz_idle_service
default_service = ptz_idle_service

PTZIdleServiceAlias = PTZIdleService
PTZIdle = PTZIdleService
IdleService = PTZIdleService
IdleHomeService = PTZIdleService


def configure(*a: Any, **k: Any) -> Dict[str, Any]:
    return ptz_idle_service.configure(*a, **k)


def update(*a: Any, **k: Any) -> Dict[str, Any]:
    return ptz_idle_service.update(*a, **k)


def remove(*a: Any, **k: Any) -> Dict[str, Any]:
    return ptz_idle_service.remove(*a, **k)


def delete(*a: Any, **k: Any) -> Dict[str, Any]:
    return ptz_idle_service.remove(*a, **k)


def get(*a: Any, **k: Any) -> Dict[str, Any]:
    return ptz_idle_service.get(*a, **k)


def get_status(*a: Any, **k: Any) -> Dict[str, Any]:
    return ptz_idle_service.get_status(*a, **k)


def status(*a: Any, **k: Any) -> Dict[str, Any]:
    return ptz_idle_service.get_status(*a, **k)


def list_configs(*a: Any, **k: Any) -> List[Dict[str, Any]]:
    return ptz_idle_service.list(*a, **k)


def list(*a: Any, **k: Any) -> List[Dict[str, Any]]:  # noqa: A001
    return ptz_idle_service.list(*a, **k)


def clear(*a: Any, **k: Any) -> int:
    return ptz_idle_service.clear(*a, **k)


def notify_activity(*a: Any, **k: Any) -> Dict[str, Any]:
    return ptz_idle_service.notify_activity(*a, **k)


def record_activity(*a: Any, **k: Any) -> Dict[str, Any]:
    return ptz_idle_service.notify_activity(*a, **k)


def mark_active(*a: Any, **k: Any) -> Dict[str, Any]:
    return ptz_idle_service.notify_activity(*a, **k)


def check(*a: Any, **k: Any) -> Dict[str, Any]:
    return ptz_idle_service.check(*a, **k)


def tick(*a: Any, **k: Any) -> Dict[str, Any]:
    return ptz_idle_service.check(*a, **k)


def process(*a: Any, **k: Any) -> Dict[str, Any]:
    return ptz_idle_service.check(*a, **k)


def run(*a: Any, **k: Any) -> Dict[str, Any]:
    return ptz_idle_service.check(*a, **k)


def return_home(*a: Any, **k: Any) -> Dict[str, Any]:
    return ptz_idle_service.return_home(*a, **k)


def go_home(*a: Any, **k: Any) -> Dict[str, Any]:
    return ptz_idle_service.return_home(*a, **k)


def enable(*a: Any, **k: Any) -> Dict[str, Any]:
    return ptz_idle_service.enable(*a, **k)


def disable(*a: Any, **k: Any) -> Dict[str, Any]:
    return ptz_idle_service.disable(*a, **k)


def set_home(*a: Any, **k: Any) -> Dict[str, Any]:
    return ptz_idle_service.configure(*a, **k)


def is_due(*a: Any, **k: Any) -> bool:
    return ptz_idle_service.is_due(*a, **k)


for _n, _f in {
    "create": configure,
    "add": configure,
    "set": configure,
    "register": configure,
    "reset": clear,
    "purge": clear,
    "all": list_configs,
    "items": list_configs,
    "values": list_configs,
}.items():
    globals().setdefault(_n, _f)

__all__ = [
    "PTZIdleService",
    "ptz_idle_service",
    "PTZ_IDLE_CONFIGS",
    "configure",
    "update",
    "remove",
    "get",
    "get_status",
    "list_configs",
    "clear",
    "notify_activity",
    "check",
    "tick",
    "return_home",
    "is_due",
]
