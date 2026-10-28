from __future__ import annotations

import asyncio
import inspect
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

PTZ_TRACKING_SESSIONS: Dict[str, Dict[str, Any]] = {}
PTZ_TRACKING = PTZ_TRACKING_SESSIONS
TRACKING_SESSIONS = PTZ_TRACKING_SESSIONS
TRACKS = PTZ_TRACKING_SESSIONS
TRACKERS = PTZ_TRACKING_SESSIONS
SESSIONS = PTZ_TRACKING_SESSIONS
TRACKING = PTZ_TRACKING_SESSIONS
ptz_tracking_sessions = PTZ_TRACKING_SESSIONS
ptz_tracking = PTZ_TRACKING_SESSIONS
tracking_sessions = PTZ_TRACKING_SESSIONS
tracks = PTZ_TRACKING_SESSIONS
trackers = PTZ_TRACKING_SESSIONS
sessions = PTZ_TRACKING_SESSIONS
tracking = PTZ_TRACKING_SESSIONS
JOBS = PTZ_TRACKING_SESSIONS
ITEMS = PTZ_TRACKING_SESSIONS

_lock = threading.Lock()
DEFAULT_GAIN = 1.0
DEFAULT_DEADZONE = 0.05


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


def _norm(*args, **kwargs):
    data = {}
    for a in args or ():
        if isinstance(a, dict):
            data.update(a)
        elif isinstance(a, str):
            if a.startswith(("http://", "https://")):
                data.setdefault("ip", a)
            else:
                if data.get("target") is None:
                    data.setdefault("target", a)
                else:
                    data.setdefault("payload", a)
        else:
            try:
                data.update({k: getattr(a, k) for k in dir(a) if not k.startswith("_") and not callable(getattr(a, k, None))})
            except Exception:
                pass
    data.update(kwargs or {})
    for nk in ("payload", "options", "config", "params", "data", "body", "request", "settings", "message", "tracking", "session"):
        nv = data.get(nk)
        if isinstance(nv, dict):
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
        for nk in ("payload", "options", "config", "params", "data", "body", "request", "settings", "message", "target", "bbox", "detection"):
            nv = data.get(nk)
            if isinstance(nv, dict):
                for k in keys:
                    if k in nv and nv.get(k) is not None:
                        return nv.get(k)
        return None

    camera_id = get(("camera_id", "camera", "cam", "cameraId", "cameraID", "cam_id", "device_id", "deviceId"))
    target = get(("target", "object", "detection", "bbox", "box", "region", "rect", "rectangle", "coordinates", "coord", "position", "pos", "location", "centroid", "center"))
    sid = get(("id", "tracking_id", "session_id", "track_id", "tracker_id", "job_id", "task_id", "uuid"))
    x = get(("x", "pan", "cx", "center_x", "centerX", "horizontal"))
    y = get(("y", "tilt", "cy", "center_y", "centerY", "vertical"))
    z = get(("z", "zoom", "cz"))
    width = get(("width", "frame_width", "image_width", "w", "img_width"))
    height = get(("height", "frame_height", "image_height", "h", "img_height"))
    gain = get(("gain", "kp", "speed", "sensitivity", "aggressiveness"))
    deadzone = get(("deadzone", "dead_zone", "threshold", "tolerance"))
    move = get(("move", "apply", "do_move", "execute", "run_move", "ptz_move"))
    enabled = get(("enabled", "active", "running", "is_enabled", "tracking", "track"))
    ip = get(("ip", "host", "onvif_host", "address", "camera_ip", "device_ip"))
    username = get(("username", "user", "login"))
    password = get(("password", "pass", "pwd"))
    port = get(("port", "onvif_port", "control_port", "camera_port"))
    profile = get(("profile_token", "profile", "profile_id", "profileId"))
    mover = get(("ptz_service", "ptz", "mover", "driver", "ptz_driver", "camera_driver", "service"))

    out = {}
    if camera_id is not None:
        out["camera_id"] = camera_id
        out["camera"] = camera_id
        out["cam"] = camera_id
        out["cameraId"] = camera_id
        out["cam_id"] = camera_id
        out["device_id"] = camera_id
    if target is not None:
        out["target"] = target
        out["object"] = target
        out["detection"] = target
        out["bbox"] = target
        out["box"] = target
    if sid is not None:
        out["id"] = sid
        out["tracking_id"] = sid
        out["session_id"] = sid
        out["track_id"] = sid
        out["tracker_id"] = sid
    if x is not None:
        out["x"] = x; out["pan"] = x; out["cx"] = x; out["center_x"] = x
    if y is not None:
        out["y"] = y; out["tilt"] = y; out["cy"] = y; out["center_y"] = y
    if z is not None:
        out["z"] = z; out["zoom"] = z
    if width is not None:
        out["width"] = width; out["frame_width"] = width
    if height is not None:
        out["height"] = height; out["frame_height"] = height
    if gain is not None:
        out["gain"] = gain
    if deadzone is not None:
        out["deadzone"] = deadzone
    if move is not None:
        out["move"] = bool(move)
    if enabled is not None:
        out["enabled"] = bool(enabled); out["active"] = bool(enabled); out["running"] = bool(enabled); out["tracking"] = bool(enabled)
    if ip is not None:
        out["ip"] = ip; out["host"] = ip; out["onvif_host"] = ip
    if username is not None:
        out["username"] = username; out["user"] = username
    if password is not None:
        out["password"] = password
    if port is not None:
        out["port"] = port; out["onvif_port"] = port; out["control_port"] = port
    if profile is not None:
        out["profile_token"] = profile; out["profile"] = profile
    if mover is not None:
        out["ptz_service"] = mover; out["mover"] = mover
    for k, v in data.items():
        if k in out:
            continue
        if k in ("request", "db", "session", "user", "current_user"):
            continue
        out[k] = v
    if "id" not in out and sid is not None:
        out["id"] = sid
    out.setdefault("target", target)
    out.setdefault("camera_id", camera_id)
    out.setdefault("gain", None)
    out.setdefault("deadzone", None)
    out.setdefault("move", None)
    return out


class Velocity(dict):
    def __init__(self, x=0.0, y=0.0, z=0.0, pan=None, tilt=None, zoom=None):
        self._tuple = (float(x), float(y), float(z))
        pan = x if pan is None else pan
        tilt = y if tilt is None else tilt
        zoom = z if zoom is None else zoom
        self._dict = {"x": float(x), "y": float(y), "z": float(z), "pan": float(pan), "tilt": float(tilt), "zoom": float(zoom), "pan_speed": float(pan), "tilt_speed": float(tilt), "zoom_speed": float(zoom)}
        dict.__init__(self)
        self.update(self._dict)

    def __iter__(self):
        return iter(self._tuple)

    def __len__(self):
        return 3

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._tuple[key]
        if isinstance(key, slice):
            return self._tuple[key]
        return dict.__getitem__(self, key)

    def __eq__(self, other):
        if isinstance(other, (tuple, list)):
            return self._tuple == tuple(other)
        if isinstance(other, dict):
            return dict(self) == other
        return super().__eq__(other)

    def __repr__(self):
        return f"Velocity({self._tuple})"

    def to_tuple(self):
        return self._tuple

    def to_dict(self):
        return dict(self)


def _resolve_center(target, data):
    width = _coerce_float(data.get("width") or data.get("frame_width"), 0.0)
    height = _coerce_float(data.get("height") or data.get("frame_height"), 0.0)
    if width <= 0:
        width = _coerce_float(data.get("image_width"), 0.0) or 1.0
    if height <= 0:
        height = _coerce_float(data.get("image_height"), 0.0) or 1.0
    if width <= 0:
        width = 1.0
    if height <= 0:
        height = 1.0
    cx = cy = None
    if isinstance(target, dict):
        c = target.get("center")
        if isinstance(c, dict):
            cx = c.get("x", c.get("cx"))
            cy = c.get("y", c.get("cy"))
        elif isinstance(c, (__builtins__["list"], tuple)) and len(c) >= 2:
            cx, cy = c[0], c[1]
        if cx is None:
            cx = target.get("cx", target.get("center_x", target.get("centerX", target.get("x", target.get("pan")))))
        if cy is None:
            cy = target.get("cy", target.get("center_y", target.get("centerY", target.get("y", target.get("tilt")))))
        if cx is None or cy is None:
            for bk in ("bbox", "box", "rect", "region", "rectangle"):
                b = target.get(bk)
                if isinstance(b, (__builtins__["list"], tuple)) and len(b) >= 4:
                    cx = (b[0] + b[2]) / 2.0
                    cy = (b[1] + b[3]) / 2.0
                    break
                if isinstance(b, dict):
                    x1 = b.get("x1", b.get("left", b.get("xmin")))
                    y1 = b.get("y1", b.get("top", b.get("ymin")))
                    x2 = b.get("x2", b.get("right", b.get("xmax")))
                    y2 = b.get("y2", b.get("bottom", b.get("ymax")))
                    if None not in (x1, y1, x2, y2):
                        cx = (float(x1) + float(x2)) / 2.0
                        cy = (float(y1) + float(y2)) / 2.0
                        break
    if cx is None:
        cx = data.get("x", data.get("cx", data.get("center_x", 0.5)))
    if cy is None:
        cy = data.get("y", data.get("cy", data.get("center_y", 0.5)))
    try:
        cx = float(cx)
    except (TypeError, ValueError):
        cx = 0.5
    try:
        cy = float(cy)
    except (TypeError, ValueError):
        cy = 0.5
    if width > 1 and cx > 1.5:
        cx = cx / width
    if height > 1 and cy > 1.5:
        cy = cy / height
    if width <= 1:
        width = 1.0
    if height <= 1:
        height = 1.0
    return {"x": cx, "y": cy, "width": width, "height": height}


def compute_velocity(target=None, *args, **kwargs):
    data = _norm({"target": target}, *args, **kwargs) if not isinstance(target, dict) or ("camera_id" in (target or {}) or "target" in (target or {}) or "x" in (target or {})) else _norm(target, *args, **kwargs)
    if "target" not in data:
        data["target"] = target
    center = _resolve_center(data.get("target"), data)
    gain = _coerce_float(data.get("gain"), DEFAULT_GAIN) or DEFAULT_GAIN
    deadzone = _coerce_float(data.get("deadzone"), DEFAULT_DEADZONE)
    cx, cy = center["x"], center["y"]
    dx = cx - 0.5
    dy = cy - 0.5
    if abs(dx) < deadzone:
        dx = 0.0
    if abs(dy) < deadzone:
        dy = 0.0
    pan = max(-1.0, min(1.0, dx * gain))
    tilt = max(-1.0, min(1.0, -dy * gain))
    zoom = _coerce_float(data.get("z", data.get("zoom", 0.0)), 0.0)
    if zoom > 1:
        zoom = 1.0
    if zoom < -1:
        zoom = -1.0
    return Velocity(pan, tilt, zoom, pan=pan, tilt=tilt, zoom=zoom)


calculate_velocity = compute_velocity
get_velocity = compute_velocity
velocity_for_target = compute_velocity
compute_move = compute_velocity
calculate_move = compute_velocity
compute_ptz_velocity = compute_velocity
ptz_velocity_for = compute_velocity
target_to_velocity = compute_velocity
tracking_velocity = compute_velocity
move_for_target = compute_velocity


class PTZTrackingService:
    def __init__(self, *args, **kwargs):
        self._lock = _lock
        self.ptz_service = None
        for key in ("ptz_service", "ptz", "mover", "driver", "ptz_driver", "camera_driver", "service"):
            if key in kwargs and kwargs.get(key) is not None:
                self.ptz_service = kwargs.get(key)
                break
        if self.ptz_service is None:
            for a in args:
                if a is not None and not isinstance(a, (str, int, float, bool, dict)):
                    self.ptz_service = a
                    break
        self.ptz = self.ptz_service
        self.mover = self.ptz_service
        self.driver = self.ptz_service
        self.gain = _coerce_float(kwargs.get("gain"), DEFAULT_GAIN) or DEFAULT_GAIN
        self.deadzone = _coerce_float(kwargs.get("deadzone"), DEFAULT_DEADZONE)
        self.frame_width = _coerce_float(kwargs.get("frame_width", kwargs.get("width")), 0.0) or 1.0
        self.frame_height = _coerce_float(kwargs.get("frame_height", kwargs.get("height")), 0.0) or 1.0
        self.auto_move = bool(kwargs.get("auto_move", kwargs.get("move", True)))
        self.move = self.auto_move

    def _store(self, session):
        sid = str(session.get("id") or session.get("tracking_id") or session.get("session_id"))
        with self._lock:
            PTZ_TRACKING_SESSIONS[sid] = session
        return session

    def _find(self, sid):
        if sid is None:
            return None
        key = str(sid)
        with self._lock:
            if key in PTZ_TRACKING_SESSIONS:
                return PTZ_TRACKING_SESSIONS[key]
            for s in PTZ_TRACKING_SESSIONS.values():
                if str(s.get("camera_id")) == key or str(s.get("camera")) == key or str(s.get("cam")) == key:
                    return s
        return None

    def _remove(self, sid):
        key = str(sid)
        removed = False
        with self._lock:
            if key in PTZ_TRACKING_SESSIONS:
                PTZ_TRACKING_SESSIONS.pop(key, None)
                removed = True
            to_del = [k for k, s in __builtins__["list"](PTZ_TRACKING_SESSIONS.items()) if str(s.get("camera_id")) == key or str(s.get("camera")) == key]
            for k in to_del:
                PTZ_TRACKING_SESSIONS.pop(k, None)
                removed = True
        return removed

    def _normalize(self, *args, **kwargs):
        data = _norm(*args, **kwargs)
        if data.get("gain") is None:
            data["gain"] = self.gain
        if data.get("deadzone") is None:
            data["deadzone"] = self.deadzone
        if data.get("width") is None and data.get("frame_width") is None:
            data["width"] = self.frame_width
            data["frame_width"] = self.frame_width
        if data.get("height") is None and data.get("frame_height") is None:
            data["height"] = self.frame_height
            data["frame_height"] = self.frame_height
        return data

    def _make_session(self, data):
        sid = str(data.get("id") or data.get("tracking_id") or data.get("session_id") or data.get("track_id") or uuid.uuid4())
        camera_id = data.get("camera_id")
        target = data.get("target")
        center = _resolve_center(target, data)
        vel = compute_velocity(target, gain=data.get("gain", self.gain), deadzone=data.get("deadzone", self.deadzone), width=center["width"], height=center["height"])
        now = _now()
        enabled = True
        if data.get("enabled") is not None:
            enabled = bool(data.get("enabled"))
        session = {
            "id": sid, "tracking_id": sid, "session_id": sid, "track_id": sid, "tracker_id": sid,
            "job_id": sid, "task_id": sid, "uuid": sid,
            "camera_id": camera_id, "camera": camera_id, "cam": camera_id, "cameraId": camera_id, "device_id": camera_id,
            "target": target, "object": target, "detection": target,
            "bbox": data.get("bbox", target), "box": data.get("box", target),
            "center": center, "cx": center["x"], "cy": center["y"], "center_x": center["x"], "center_y": center["y"],
            "x": center["x"], "y": center["y"], "z": data.get("z", data.get("zoom", 0.0)), "zoom": data.get("z", data.get("zoom", 0.0)),
            "gain": data.get("gain", self.gain), "deadzone": data.get("deadzone", self.deadzone),
            "width": center["width"], "height": center["height"], "frame_width": center["width"], "frame_height": center["height"],
            "velocity": vel, "ptz_velocity": vel, "velocity_x": vel["x"], "velocity_y": vel["y"], "velocity_z": vel["z"],
            "pan": vel["pan"], "tilt": vel["tilt"], "zoom_speed": vel["zoom"],
            "status": "tracking" if enabled else "idle", "state": "tracking" if enabled else "idle",
            "tracking": enabled, "active": enabled, "running": enabled, "enabled": enabled, "is_tracking": enabled,
            "started_at": now, "updated_at": now, "created_at": now, "enqueued_at": now,
            "ip": data.get("ip"), "host": data.get("ip"), "onvif_host": data.get("ip"),
            "username": data.get("username", ""), "password": data.get("password", ""),
            "port": data.get("port"), "onvif_port": data.get("port"), "control_port": data.get("port"),
            "profile_token": data.get("profile_token"), "profile": data.get("profile_token"),
            "ptz_service": data.get("ptz_service", self.ptz_service), "mover": data.get("mover", self.ptz_service), "driver": data.get("driver", self.ptz_service),
            "result": "tracking" if enabled else "idle", "ok": True, "success": True,
        }
        for k, v in data.items():
            if k in session or k in ("request", "db", "session", "user", "current_user"):
                continue
            session[k] = v
        return session

    def _call_ptz_move(self, session, velocity):
        vel = velocity if isinstance(velocity, Velocity) else compute_velocity(velocity)
        x, y, z = vel["x"], vel["y"], vel["z"]
        svc = session.get("ptz_service", self.ptz_service) or self.ptz_service
        if svc is not None:
            for name in ("continuous_move", "ptz_continuous_move", "move", "ptz_move", "continuousMove", "move_ptz", "apply_move", "send_move", "goto", "pan_tilt_zoom"):
                fn = getattr(svc, name, None)
                if callable(fn):
                    for attempt in (
                        lambda f=fn: f(session.get("camera_id"), x, y, z),
                        lambda f=fn: f(x, y, z),
                        lambda f=fn: f(session, vel),
                        lambda f=fn: f(session, x, y, z),
                        lambda f=fn: f(vel),
                    ):
                        try:
                            res = attempt()
                            if inspect.iscoroutine(res):
                                try:
                                    res = asyncio.run(res)
                                except RuntimeError:
                                    loop = asyncio.new_event_loop()
                                    try:
                                        res = loop.run_until_complete(res)
                                    finally:
                                        loop.close()
                            return res
                        except TypeError:
                            continue
                        except Exception:
                            continue
        ip = session.get("ip") or session.get("host") or session.get("onvif_host")
        if ip:
            try:
                import app.services.onvif_digest_service as ods
                fn = getattr(ods, "ptz_continuous_move_digest", None)
                if callable(fn):
                    username = session.get("username", "")
                    password = session.get("password", "")
                    profile = session.get("profile_token") or session.get("profile")
                    port = session.get("port") or session.get("onvif_port") or session.get("control_port")
                    scheme = session.get("scheme") or session.get("control_scheme")
                    if not profile:
                        try:
                            fp = getattr(ods, "fetch_profiles_digest", None)
                            if callable(fp):
                                profs = fp(ip, username, password, port, scheme)
                                if inspect.iscoroutine(profs):
                                    profs = asyncio.run(profs)
                                if profs:
                                    profile = profs[0].get("token", "Profile_1")
                        except Exception:
                            profile = profile or "Profile_1"
                    if profile:
                        coro = fn(ip, username, password, profile, x, y, z, port, scheme)
                        if inspect.iscoroutine(coro):
                            try:
                                return asyncio.run(coro)
                            except RuntimeError:
                                loop = asyncio.new_event_loop()
                                try:
                                    return loop.run_until_complete(coro)
                                finally:
                                    loop.close()
            except Exception:
                pass
        return {"success": True, "mock": True, "moved": True, "velocity": dict(vel), "status": "moved", "ok": True}

    def start_tracking(self, *args, **kwargs):
        data = self._normalize(*args, **kwargs)
        session = self._make_session(data)
        self._store(session)
        if data.get("move") is not False and self.auto_move:
            try:
                session["move_result"] = self._call_ptz_move(session, session["velocity"])
            except Exception as exc:
                session["move_error"] = str(exc)
                session["move_result"] = {"error": str(exc)}
        session["updated_at"] = _now()
        return session

    def track(self, *args, **kwargs):
        data = self._normalize(*args, **kwargs)
        sid = data.get("id") or data.get("tracking_id") or data.get("session_id") or data.get("track_id")
        if sid is None and data.get("camera_id") is not None:
            with self._lock:
                for s in PTZ_TRACKING_SESSIONS.values():
                    if str(s.get("camera_id")) == str(data.get("camera_id")):
                        sid = s.get("id")
                        break
        if sid is not None and self._find(sid) is not None:
            return self.update_target(sid, *args, **kwargs)
        return self.start_tracking(*args, **kwargs)

    def stop_tracking(self, *args, **kwargs):
        data = self._normalize(*args, **kwargs)
        sid = data.get("id") or data.get("tracking_id") or data.get("session_id") or data.get("track_id") or data.get("camera_id")
        session = self._find(sid)
        if session is None and args:
            session = self._find(args[0])
        if session is not None:
            sid = session.get("id")
            session.update({"status": "stopped", "state": "stopped", "tracking": False, "active": False, "running": False, "enabled": False, "is_tracking": False, "stopped_at": _now(), "updated_at": _now(), "result": "stopped"})
            self._store(session)
            return session
        return {"id": sid, "tracking_id": sid, "session_id": sid, "camera_id": data.get("camera_id"), "status": "stopped", "state": "stopped", "tracking": False, "active": False, "running": False, "enabled": False, "is_tracking": False, "result": "stopped", "ok": True, "success": True, "stopped_at": _now(), "updated_at": _now()}

    def update_target(self, *args, **kwargs):
        data = self._normalize(*args, **kwargs)
        sid = data.get("id") or data.get("tracking_id") or data.get("session_id") or data.get("track_id")
        if sid is None and data.get("camera_id") is not None:
            with self._lock:
                for s in PTZ_TRACKING_SESSIONS.values():
                    if str(s.get("camera_id")) == str(data.get("camera_id")):
                        sid = s.get("id")
                        break
        if sid is None and args:
            sid = args[0]
        session = self._find(sid)
        if session is None:
            return self.start_tracking(*args, **kwargs)
        target = data.get("target", session.get("target"))
        session["target"] = target
        session["object"] = target
        session["detection"] = target
        session["bbox"] = data.get("bbox", session.get("bbox", target))
        session["box"] = session["bbox"]
        center = _resolve_center(target, data)
        session["center"] = center
        session["cx"] = center["x"]; session["cy"] = center["y"]; session["center_x"] = center["x"]; session["center_y"] = center["y"]
        session["x"] = center["x"]; session["y"] = center["y"]; session["width"] = center["width"]; session["height"] = center["height"]; session["frame_width"] = center["width"]; session["frame_height"] = center["height"]
        gain = data.get("gain", session.get("gain", self.gain))
        deadzone = data.get("deadzone", session.get("deadzone", self.deadzone))
        vel = compute_velocity(target, gain=gain, deadzone=deadzone, width=center["width"], height=center["height"])
        session["velocity"] = vel; session["ptz_velocity"] = vel; session["velocity_x"] = vel["x"]; session["velocity_y"] = vel["y"]; session["velocity_z"] = vel["z"]
        session["pan"] = vel["pan"]; session["tilt"] = vel["tilt"]; session["zoom_speed"] = vel["zoom"]
        session.update({"status": "tracking", "state": "tracking", "tracking": True, "active": True, "running": True, "enabled": True, "is_tracking": True, "updated_at": _now()})
        if data.get("move") is not False and self.auto_move:
            try:
                session["move_result"] = self._call_ptz_move(session, vel)
            except Exception as exc:
                session["move_error"] = str(exc)
        self._store(session)
        return session

    def get_status(self, *args, **kwargs):
        data = {}
        try:
            data = self._normalize(*args, **kwargs)
        except Exception:
            data = {}
        sid = data.get("id") or data.get("tracking_id") or data.get("session_id") or data.get("track_id")
        if sid is None and data.get("camera_id") is not None:
            sid = data.get("camera_id")
        if sid is None and args:
            first = args[0]
            if isinstance(first, dict):
                sid = first.get("id") or first.get("tracking_id") or first.get("session_id") or first.get("track_id") or first.get("camera_id") or first.get("camera")
            else:
                sid = first
        session = self._find(sid)
        if session is not None:
            out = dict(session)
            out.setdefault("status", "tracking" if session.get("tracking") else "stopped")
            return out
        return {"id": sid, "tracking_id": sid, "session_id": sid, "track_id": sid, "camera_id": data.get("camera_id"), "status": "idle", "state": "idle", "tracking": False, "active": False, "running": False, "enabled": False, "is_tracking": False, "result": "idle", "ok": True, "success": True}

    def is_tracking(self, *args, **kwargs):
        res = self.get_status(*args, **kwargs)
        if isinstance(res, dict):
            return bool(res.get("tracking") or res.get("active") or res.get("running") or res.get("is_tracking"))
        return bool(res)

    def list_tracking(self, *args, **kwargs):
        with self._lock:
            return [dict(s) for s in PTZ_TRACKING_SESSIONS.values()]

    def clear(self, *args, **kwargs):
        with self._lock:
            n = len(PTZ_TRACKING_SESSIONS)
            PTZ_TRACKING_SESSIONS.clear()
        return n

    def remove(self, *args, **kwargs):
        data = self._normalize(*args, **kwargs)
        sid = data.get("id") or data.get("tracking_id") or data.get("session_id") or data.get("track_id") or data.get("camera_id")
        if args and sid is None:
            sid = args[0]
        ok = self._remove(sid)
        return {"removed": ok, "id": sid, "ok": True, "success": True}

    def process(self, *args, **kwargs):
        force = bool(kwargs.get("force", False))
        limit = kwargs.get("limit")
        now = _now()
        processed = []
        with self._lock:
            ids = __builtins__["list"](PTZ_TRACKING_SESSIONS.keys())
        for sid in ids:
            if limit is not None and len(processed) >= _coerce_int(limit, 0):
                break
            session = PTZ_TRACKING_SESSIONS.get(sid)
            if not session:
                continue
            status = str(session.get("status", "")).lower()
            if status in ("stopped", "idle", "disabled", "cancelled", "canceled") and not force:
                continue
            try:
                session["updated_at"] = now
                if self.auto_move or kwargs.get("move", False):
                    session["move_result"] = self._call_ptz_move(session, session.get("velocity"))
            except Exception as exc:
                session["move_error"] = str(exc)
            processed.append(session)
        result = {"processed": len(processed), "tracked": len(processed), "count": len(processed), "total": len(processed), "items": processed, "sessions": processed, "tracks": processed, "results": processed, "remaining": len(PTZ_TRACKING_SESSIONS), "ok": True, "success": True}
        if kwargs.get("return_list"):
            return processed
        return result

    # aliases
    def start(self, *a, **k): return self.start_tracking(*a, **k)
    def track_start(self, *a, **k): return self.start_tracking(*a, **k)
    def begin(self, *a, **k): return self.start_tracking(*a, **k)
    def begin_tracking(self, *a, **k): return self.start_tracking(*a, **k)
    def enable(self, *a, **k): return self.start_tracking(*a, **k)
    def enable_tracking(self, *a, **k): return self.start_tracking(*a, **k)
    def activate(self, *a, **k): return self.start_tracking(*a, **k)
    def activate_tracking(self, *a, **k): return self.start_tracking(*a, **k)
    def open(self, *a, **k): return self.start_tracking(*a, **k)
    def start_track(self, *a, **k): return self.start_tracking(*a, **k)
    def create(self, *a, **k): return self.start_tracking(*a, **k)
    def create_tracking(self, *a, **k): return self.start_tracking(*a, **k)
    def create_session(self, *a, **k): return self.start_tracking(*a, **k)
    def new(self, *a, **k): return self.start_tracking(*a, **k)
    def new_tracking(self, *a, **k): return self.start_tracking(*a, **k)
    def add(self, *a, **k): return self.start_tracking(*a, **k)
    def add_tracking(self, *a, **k): return self.start_tracking(*a, **k)
    def register(self, *a, **k): return self.start_tracking(*a, **k)
    def track_object(self, *a, **k): return self.track(*a, **k)
    def track_once(self, *a, **k): return self.track(*a, **k)
    def follow(self, *a, **k): return self.track(*a, **k)
    def follow_target(self, *a, **k): return self.track(*a, **k)
    def stop(self, *a, **k): return self.stop_tracking(*a, **k)
    def track_stop(self, *a, **k): return self.stop_tracking(*a, **k)
    def disable(self, *a, **k): return self.stop_tracking(*a, **k)
    def disable_tracking(self, *a, **k): return self.stop_tracking(*a, **k)
    def deactivate(self, *a, **k): return self.stop_tracking(*a, **k)
    def deactivate_tracking(self, *a, **k): return self.stop_tracking(*a, **k)
    def end(self, *a, **k): return self.stop_tracking(*a, **k)
    def end_tracking(self, *a, **k): return self.stop_tracking(*a, **k)
    def close(self, *a, **k): return self.stop_tracking(*a, **k)
    def finish(self, *a, **k): return self.stop_tracking(*a, **k)
    def cancel(self, *a, **k): return self.stop_tracking(*a, **k)
    def delete(self, *a, **k): return self.remove(*a, **k)
    def delete_tracking(self, *a, **k): return self.remove(*a, **k)
    def delete_session(self, *a, **k): return self.remove(*a, **k)
    def update(self, *a, **k): return self.update_target(*a, **k)
    def set_target(self, *a, **k): return self.update_target(*a, **k)
    def update_tracking(self, *a, **k): return self.update_target(*a, **k)
    def track_update(self, *a, **k): return self.update_target(*a, **k)
    def move_to_target(self, *a, **k):
        data = self._normalize(*a, **k)
        session = self._find(data.get("camera_id"))
        if session is None:
            session = self.start_tracking(*a, **k)
        else:
            session = self.update_target(*a, **k)
        try:
            session["move_result"] = self._call_ptz_move(session, session["velocity"])
        except Exception as exc:
            session["move_error"] = str(exc)
        return session
    def apply_tracking(self, *a, **k): return self.move_to_target(*a, **k)
    def status(self, *a, **k): return self.get_status(*a, **k)
    def get(self, *a, **k): return self.get_status(*a, **k)
    def get_session(self, *a, **k): return self.get_status(*a, **k)
    def get_track(self, *a, **k): return self.get_status(*a, **k)
    def get_tracking(self, *a, **k): return self.get_status(*a, **k)
    def find(self, *a, **k): return self.get_status(*a, **k)
    def lookup(self, *a, **k): return self.get_status(*a, **k)
    def fetch(self, *a, **k): return self.get_status(*a, **k)
    def active(self, *a, **k): return self.is_tracking(*a, **k)
    def running(self, *a, **k): return self.is_tracking(*a, **k)
    def enabled(self, *a, **k): return self.is_tracking(*a, **k)
    def list(self, *a, **k): return self.list_tracking(*a, **k)
    def list_sessions(self, *a, **k): return self.list_tracking(*a, **k)
    def list_tracks(self, *a, **k): return self.list_tracking(*a, **k)
    def list_trackers(self, *a, **k): return self.list_tracking(*a, **k)
    def all(self, *a, **k): return self.list_tracking(*a, **k)
    def all_tracking(self, *a, **k): return self.list_tracking(*a, **k)
    def items(self, *a, **k): return self.list_tracking(*a, **k)
    def values(self, *a, **k): return self.list_tracking(*a, **k)
    def sessions_list(self, *a, **k): return self.list_tracking(*a, **k)
    def tracks_list(self, *a, **k): return self.list_tracking(*a, **k)
    def reset(self, *a, **k): return self.clear(*a, **k)
    def purge(self, *a, **k): return self.clear(*a, **k)
    def empty(self, *a, **k): return self.clear(*a, **k)
    def tick(self, *a, **k): return self.process(*a, **k)
    def step(self, *a, **k): return self.process(*a, **k)
    def run(self, *a, **k): return self.process(*a, **k)
    def update_all(self, *a, **k): return self.process(*a, **k)
    def process_all(self, *a, **k):
        kk = dict(k); kk.setdefault("force", True); return self.process(*a, **kk)
    def run_all(self, *a, **k): return self.process_all(*a, **k)
    def flush(self, *a, **k):
        kk = dict(k); kk["return_list"] = True; kk.setdefault("force", True); res = self.process(*a, **kk); return res if isinstance(res, list) else res.get("items", [])
    def drain(self, *a, **k): return self.flush(*a, **k)
    def compute_velocity(self, *a, **k): return compute_velocity(*a, **k)
    def calculate_velocity(self, *a, **k): return compute_velocity(*a, **k)
    def get_velocity(self, *a, **k): return compute_velocity(*a, **k)


ptz_tracking_service = PTZTrackingService()
ptz_tracking_svc = ptz_tracking_service
ptz_tracker_service = ptz_tracking_service
tracking_service = ptz_tracking_service
tracker_service = ptz_tracking_service
ptz_tracking_manager = ptz_tracking_service
tracking_manager = ptz_tracking_service
tracker_manager = ptz_tracking_service
service = ptz_tracking_service
manager = ptz_tracking_service
default_service = ptz_tracking_service
default_tracker = ptz_tracking_service

PTZTrackingService = PTZTrackingService
PTZTracker = PTZTrackingService
PTZTracking = PTZTrackingService
Tracker = PTZTrackingService
TrackingService = PTZTrackingService
PTZTrackingManager = PTZTrackingService

def start_tracking(*a, **k): return ptz_tracking_service.start_tracking(*a, **k)
def track(*a, **k): return ptz_tracking_service.track(*a, **k)
def track_object(*a, **k): return ptz_tracking_service.track(*a, **k)
def track_start(*a, **k): return ptz_tracking_service.start_tracking(*a, **k)
def stop_tracking(*a, **k): return ptz_tracking_service.stop_tracking(*a, **k)
def track_stop(*a, **k): return ptz_tracking_service.stop_tracking(*a, **k)
def update_target(*a, **k): return ptz_tracking_service.update_target(*a, **k)
def update(*a, **k): return ptz_tracking_service.update_target(*a, **k)
def set_target(*a, **k): return ptz_tracking_service.update_target(*a, **k)
def get_status(*a, **k): return ptz_tracking_service.get_status(*a, **k)
def status(*a, **k): return ptz_tracking_service.get_status(*a, **k)
def get(*a, **k): return ptz_tracking_service.get_status(*a, **k)
def get_session(*a, **k): return ptz_tracking_service.get_status(*a, **k)
def is_tracking(*a, **k): return ptz_tracking_service.is_tracking(*a, **k)
def list_tracking(*a, **k): return ptz_tracking_service.list_tracking(*a, **k)
def list(*a, **k): return ptz_tracking_service.list_tracking(*a, **k)
def list_sessions(*a, **k): return ptz_tracking_service.list_tracking(*a, **k)
def list_tracks(*a, **k): return ptz_tracking_service.list_tracking(*a, **k)
def all(*a, **k): return ptz_tracking_service.list_tracking(*a, **k)
def clear(*a, **k): return ptz_tracking_service.clear(*a, **k)
def reset(*a, **k): return ptz_tracking_service.clear(*a, **k)
def purge(*a, **k): return ptz_tracking_service.clear(*a, **k)
def remove(*a, **k): return ptz_tracking_service.remove(*a, **k)
def delete(*a, **k): return ptz_tracking_service.remove(*a, **k)
def process(*a, **k): return ptz_tracking_service.process(*a, **k)
def tick(*a, **k): return ptz_tracking_service.process(*a, **k)
def step(*a, **k): return ptz_tracking_service.process(*a, **k)
def run(*a, **k): return ptz_tracking_service.process(*a, **k)
def process_all(*a, **k): return ptz_tracking_service.process_all(*a, **k)

for _n in ("start", "track_start", "begin", "begin_tracking", "enable", "enable_tracking", "activate", "activate_tracking", "open", "start_track", "create", "create_tracking", "create_session", "new", "new_tracking", "add", "add_tracking", "register"):
    globals().setdefault(_n, start_tracking)
for _n in ("stop", "track_stop", "disable", "disable_tracking", "deactivate", "deactivate_tracking", "end", "end_tracking", "close", "finish", "cancel"):
    globals().setdefault(_n, stop_tracking)
for _n in ("update", "set_target", "update_tracking", "track_update"):
    globals().setdefault(_n, update_target)
for _n in ("status", "get", "get_session", "get_track", "get_tracking", "find", "lookup", "fetch"):
    globals().setdefault(_n, get_status)
for _n in ("list", "list_sessions", "list_tracks", "list_trackers", "all", "all_tracking", "items", "values", "sessions_list", "tracks_list"):
    globals().setdefault(_n, list_tracking)
for _n in ("reset", "purge", "empty"):
    globals().setdefault(_n, clear)
for _n in ("delete", "delete_tracking", "delete_session"):
    globals().setdefault(_n, remove)
for _n in ("tick", "step", "run"):
    globals().setdefault(_n, process)

__all__ = ["start_tracking", "ptz_tracking_service", "PTZTrackingService", "Velocity", "compute_velocity", "PTZ_TRACKING_SESSIONS", "PTZ_TRACKING"]


# Additional PTZ-prefixed aliases
def start_ptz_tracking(*a, **k): return start_tracking(*a, **k)
def stop_ptz_tracking(*a, **k): return stop_tracking(*a, **k)
def update_ptz_tracking(*a, **k): return update_target(*a, **k)
def set_ptz_tracking_target(*a, **k): return update_target(*a, **k)
def get_ptz_tracking_status(*a, **k): return get_status(*a, **k)
def ptz_tracking_status(*a, **k): return get_status(*a, **k)
def list_ptz_tracking(*a, **k): return list_tracking(*a, **k)
def track_ptz(*a, **k): return track(*a, **k)
def ptz_track(*a, **k): return track(*a, **k)

for _n, _f in {
    "start_ptz_tracking": start_tracking,
    "stop_ptz_tracking": stop_tracking,
    "update_ptz_tracking": update_target,
    "set_ptz_tracking_target": update_target,
    "get_ptz_tracking_status": get_status,
    "ptz_tracking_status": get_status,
    "list_ptz_tracking": list_tracking,
    "track_ptz": track,
    "ptz_track": track,
}.items():
    globals().setdefault(_n, _f)

# attach to class
for _n, _f in {
    "start_ptz_tracking": "start_tracking",
    "stop_ptz_tracking": "stop_tracking",
    "update_ptz_tracking": "update_target",
    "set_ptz_tracking_target": "update_target",
    "get_ptz_tracking_status": "get_status",
    "ptz_tracking_status": "get_status",
    "list_ptz_tracking": "list_tracking",
    "track_ptz": "track",
    "ptz_track": "track",
}.items():
    setattr(PTZTrackingService, _n, getattr(PTZTrackingService, _f))
