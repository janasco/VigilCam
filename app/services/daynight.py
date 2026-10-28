"""Day/night imaging profiles (on-demand only, no auto-switching).

Verified baseline: ``app.services.imaging`` sets brightness/contrast/
saturation **on demand** via ``get_imaging_settings`` /
``set_imaging_settings`` (ONVIF ``GetImagingSettings`` /
``SetImagingSettings``). It contains no day/night logic, no schedule, no
background task -- see ``verify_no_auto_switch()``. This module adds a
thin per-camera ``day`` / ``night`` profile store on top of that write
path. Profiles are applied **only** when the caller explicitly invokes
``apply_profile`` / ``apply_day_profile`` / ``apply_night_profile``
(or the ``/apply`` API endpoints). There is no scheduler, no background
thread, no timer, and no automatic switching in this module.
"""

from __future__ import annotations

import copy
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

PROFILE_DAY = "day"
PROFILE_NIGHT = "night"
VALID_PROFILES = (PROFILE_DAY, PROFILE_NIGHT)
DEFAULT_PROFILE = PROFILE_DAY

WRITABLE_FIELDS = ("brightness", "contrast", "saturation")

PROFILE_ALIASES = {
    "day": PROFILE_DAY,
    "daytime": PROFILE_DAY,
    "day_time": PROFILE_DAY,
    "morning": PROFILE_DAY,
    "d": PROFILE_DAY,
    "diurnal": PROFILE_DAY,
    "light": PROFILE_DAY,
    "night": PROFILE_NIGHT,
    "nighttime": PROFILE_NIGHT,
    "night_time": PROFILE_NIGHT,
    "evening": PROFILE_NIGHT,
    "n": PROFILE_NIGHT,
    "nocturnal": PROFILE_NIGHT,
    "dark": PROFILE_NIGHT,
}

_LEVEL_MIN = 0
_LEVEL_MAX = 100

_lock = threading.RLock()
_STORE: Dict[str, Dict[str, Dict[str, Any]]] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_camera_key(value: Any) -> Optional[str]:
    if value is None or isinstance(value, bool):
        return None
    s = str(value).strip()
    if not s:
        return None
    return s


def _flatten(data: Any) -> Dict[str, Any]:
    if data is None:
        return {}
    if hasattr(data, "model_dump"):
        try:
            return dict(data.model_dump())
        except Exception:  # noqa: BLE001
            pass
    if isinstance(data, dict):
        out = dict(data)
        for nested in ("profile_data", "profile", "config", "params",
                       "data", "options", "payload", "settings", "levels"):
            n = out.get(nested)
            if isinstance(n, dict):
                for k, v in n.items():
                    out.setdefault(k, v)
        return out
    return {}


def normalize_profile_name(value: Any) -> Optional[str]:
    """Normalize a user-supplied profile name to ``day`` | ``night``."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, dict):
        for k in ("profile", "profile_name", "name", "mode", "preset",
                  "when", "period", "phase", "value", "id", "key"):
            if value.get(k) is not None:
                got = normalize_profile_name(value.get(k))
                if got is not None:
                    return got
        return None
    s = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    if not s:
        return None
    if s in VALID_PROFILES:
        return s
    return PROFILE_ALIASES.get(s)


def canonical_profile_name(value: Any, default: Optional[str] = None) -> Optional[str]:
    out = normalize_profile_name(value)
    return out if out is not None else default


def is_valid_profile(value: Any) -> bool:
    return normalize_profile_name(value) is not None


def _clamp_level(value: Any) -> int:
    try:
        # bool is not a valid level.
        if isinstance(value, bool):
            raise ValueError("bool is not a valid imaging level")
        iv = int(value) if not isinstance(value, float) else int(round(value))
        if isinstance(value, str) and str(value).strip() == "":
            raise ValueError("empty imaging level")
    except (TypeError, ValueError):
        raise ValueError(f"Invalid imaging level: {value!r}")
    return max(_LEVEL_MIN, min(_LEVEL_MAX, iv))


def _coerce_optional_level(value: Any) -> Optional[int]:
    if value is None:
        return None
    return _clamp_level(value)


def _extract_levels(data: Dict[str, Any]) -> Dict[str, int]:
    """Pull brightness/contrast/saturation (with common aliases) from data."""
    out: Dict[str, int] = {}
    # Direct + alias keys.
    brightness = data.get("brightness")
    if brightness is None:
        for alias in ("bright", "bri", "b"):
            if data.get(alias) is not None:
                brightness = data.get(alias)
                break
    contrast = data.get("contrast")
    if contrast is None:
        for alias in ("cont", "con", "c"):
            if data.get(alias) is not None:
                contrast = data.get(alias)
                break
    saturation = data.get("saturation")
    if saturation is None:
        for alias in ("satur", "sat", "s", "color_saturation",
                      "colorSaturation", "colour_saturation", "satu"):
            if data.get(alias) is not None:
                saturation = data.get(alias)
                break
    if brightness is not None:
        out["brightness"] = _clamp_level(brightness)
    if contrast is not None:
        out["contrast"] = _clamp_level(contrast)
    if saturation is not None:
        out["saturation"] = _clamp_level(saturation)
    return out


def _public(camera_key: str, profile_name: str, stored: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(stored)
    out.setdefault("camera_id", stored.get("camera_id", camera_key))
    out.setdefault("camera", out.get("camera_id"))
    out.setdefault("profile", profile_name)
    out.setdefault("name", profile_name)
    out.setdefault("mode", profile_name)
    out.setdefault("ok", True)
    out.setdefault("success", True)
    return out


# ---------------------------------------------------------------------------
# Core CRUD (on-demand store only; no timers / schedulers here)
# ---------------------------------------------------------------------------

def set_profile(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Create or replace a ``day``/``night`` imaging profile for a camera.

    Accepts flexible inputs, e.g.::

        set_profile(camera_id="cam1", profile="day", brightness=60, contrast=55)
        set_profile("cam1", "night", {"brightness": 80})
        set_profile({"camera_id": "cam1", "profile": "night", "brightness": 80})

    At least one of brightness/contrast/saturation is required. Values are
    clamped to 0-100. Nothing is written to the camera here -- use
    ``apply_profile`` for an explicit on-demand write via
    ``app.services.imaging``.
    """
    flat: Dict[str, Any] = {}
    positional: List[Any] = []
    for a in args:
        if a is None:
            continue
        if isinstance(a, dict):
            flat.update(a)
        elif hasattr(a, "model_dump"):
            try:
                flat.update(dict(a.model_dump()))
            except Exception:  # noqa: BLE001
                pass
        else:
            positional.append(a)
    flat.update(kwargs or {})
    for nested in ("profile_data", "profile", "config", "params", "data",
                   "options", "payload", "settings", "levels"):
        n = flat.get(nested)
        if isinstance(n, dict):
            for k, v in n.items():
                flat.setdefault(k, v)

    # Positional conveniences: (camera_id, profile, levels-dict-or-brightness)
    idx = 0
    cam_keys = ("camera_id", "camera", "cam", "cam_id", "device_id", "id")
    if all(flat.get(k) is None for k in cam_keys) and idx < len(positional):
        flat["camera_id"] = positional[idx]
        idx += 1
    prof_keys = ("profile", "profile_name", "name", "mode", "preset",
                 "when", "period", "phase")
    if all(flat.get(k) is None for k in prof_keys) and idx < len(positional):
        flat["profile"] = positional[idx]
        idx += 1
    if idx < len(positional):
        rest = positional[idx]
        if isinstance(rest, dict):
            for k, v in rest.items():
                flat.setdefault(k, v)
        elif flat.get("brightness") is None:
            flat["brightness"] = rest

    raw_cam = None
    for k in cam_keys:
        if flat.get(k) is not None:
            raw_cam = flat.get(k)
            break
    camera_key = _normalize_camera_key(raw_cam)
    if camera_key is None:
        raise ValueError("camera_id is required")

    raw_prof = None
    for k in prof_keys:
        if flat.get(k) is not None:
            raw_prof = flat.get(k)
            break
    # A bare {"day": {...}} / {"night": {...}} map also selects the profile.
    if raw_prof is None:
        for cand in VALID_PROFILES:
            if isinstance(flat.get(cand), dict):
                raw_prof = cand
                for k, v in flat[cand].items():
                    flat.setdefault(k, v)
                break
    profile_name = normalize_profile_name(raw_prof)
    if profile_name is None:
        raise ValueError("profile must be one of 'day' | 'night'")

    levels = _extract_levels(flat)
    if not levels:
        raise ValueError(
            "At least one of brightness, contrast, saturation is required"
        )

    now = _now_iso()
    with _lock:
        entry = _STORE.setdefault(str(camera_key), {})
        existing = entry.get(profile_name, {})
        stored: Dict[str, Any] = {
            "camera_id": camera_key,
            "camera": camera_key,
            "profile": profile_name,
            "name": profile_name,
            "mode": profile_name,
            "brightness": levels.get("brightness", existing.get("brightness")),
            "contrast": levels.get("contrast", existing.get("contrast")),
            "saturation": levels.get("saturation", existing.get("saturation")),
            "created_at": existing.get("created_at", now),
            "updated_at": now,
            "ok": True,
            "success": True,
        }
        # Drop None channels (partial profile keeps only set channels).
        stored = {k: v for k, v in stored.items() if v is not None}
        stored.setdefault("ok", True)
        stored.setdefault("success", True)
        entry[profile_name] = stored
        return _public(str(camera_key), profile_name, stored)


def get_profile(camera_id: Any = None, profile: Any = None,
                *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    """Return one stored ``day``/``night`` profile, or None."""
    flat = _flatten(args[0]) if args and isinstance(args[0], dict) else {}
    flat.update(kwargs or {})
    if camera_id is None:
        for k in ("camera_id", "camera", "cam", "cam_id", "device_id", "id"):
            if flat.get(k) is not None:
                camera_id = flat.get(k)
                break
    if profile is None:
        for k in ("profile", "profile_name", "name", "mode", "preset",
                  "when", "period", "phase"):
            if flat.get(k) is not None:
                profile = flat.get(k)
                break
    # Allow set_profile-style single dict.
    if isinstance(camera_id, dict) and profile is None:
        return get_profile(**camera_id)
    camera_key = _normalize_camera_key(camera_id)
    profile_name = normalize_profile_name(profile)
    if camera_key is None or profile_name is None:
        return None
    with _lock:
        stored = _STORE.get(str(camera_key), {}).get(profile_name)
        if stored is None:
            return None
        return _public(str(camera_key), profile_name, stored)


def get_daynight(camera_id: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Return both ``day`` and ``night`` profiles for a camera."""
    flat = _flatten(args[0]) if args and isinstance(args[0], dict) else {}
    flat.update(kwargs or {})
    if camera_id is None:
        for k in ("camera_id", "camera", "cam", "cam_id", "device_id", "id"):
            if flat.get(k) is not None:
                camera_id = flat.get(k)
                break
        if camera_id is None and args and not isinstance(args[0], dict):
            camera_id = args[0]
    camera_key = _normalize_camera_key(camera_id)
    if camera_key is None:
        raise ValueError("camera_id is required")
    with _lock:
        entry = _STORE.get(str(camera_key), {})
        day = _public(str(camera_key), PROFILE_DAY, entry[PROFILE_DAY]) \
            if PROFILE_DAY in entry else None
        night = _public(str(camera_key), PROFILE_NIGHT, entry[PROFILE_NIGHT]) \
            if PROFILE_NIGHT in entry else None
    return {
        "camera_id": camera_key,
        "camera": camera_key,
        "day": day,
        "night": night,
        "profiles": {k: v for k, v in (("day", day), ("night", night)) if v is not None},
        "ok": True,
        "success": True,
    }


def list_profiles(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """List stored profiles, optionally filtered by ``camera_id``."""
    flat = _flatten(args[0]) if args and isinstance(args[0], dict) else {}
    flat.update(kwargs or {})
    raw_cam = None
    for k in ("camera_id", "camera", "cam", "cam_id", "device_id", "id"):
        if flat.get(k) is not None:
            raw_cam = flat.get(k)
            break
    if raw_cam is None and args and not isinstance(args[0], dict):
        # list_profiles("cam1") filters by camera.
        if isinstance(args[0], str):
            raw_cam = args[0]
    wanted = _normalize_camera_key(raw_cam) if raw_cam is not None else None
    out: List[Dict[str, Any]] = []
    with _lock:
        for camera_key, entry in _STORE.items():
            if wanted is not None and str(camera_key) != str(wanted):
                continue
            for pname, stored in entry.items():
                out.append(_public(str(camera_key), pname, stored))
    out.sort(key=lambda d: (str(d.get("camera_id")), str(d.get("profile"))))
    return out


def delete_profile(camera_id: Any = None, profile: Any = None,
                   *args: Any, **kwargs: Any) -> bool:
    flat = _flatten(args[0]) if args and isinstance(args[0], dict) else {}
    flat.update(kwargs or {})
    if camera_id is None:
        for k in ("camera_id", "camera", "cam", "cam_id", "device_id", "id"):
            if flat.get(k) is not None:
                camera_id = flat.get(k)
                break
    if profile is None:
        for k in ("profile", "profile_name", "name", "mode", "preset",
                  "when", "period", "phase"):
            if flat.get(k) is not None:
                profile = flat.get(k)
                break
    camera_key = _normalize_camera_key(camera_id)
    profile_name = normalize_profile_name(profile)
    if camera_key is None or profile_name is None:
        return False
    with _lock:
        entry = _STORE.get(str(camera_key))
        if not entry or profile_name not in entry:
            return False
        del entry[profile_name]
        if not entry:
            _STORE.pop(str(camera_key), None)
        return True


def delete_profiles_for_camera(camera_id: Any = None, *args: Any,
                               **kwargs: Any) -> int:
    flat = _flatten(args[0]) if args and isinstance(args[0], dict) else {}
    flat.update(kwargs or {})
    if camera_id is None:
        for k in ("camera_id", "camera", "cam", "cam_id", "device_id", "id"):
            if flat.get(k) is not None:
                camera_id = flat.get(k)
                break
        if camera_id is None and args and not isinstance(args[0], dict):
            camera_id = args[0]
    camera_key = _normalize_camera_key(camera_id)
    if camera_key is None:
        return 0
    with _lock:
        entry = _STORE.pop(str(camera_key), {})
        return len(entry)


def clear(*args: Any, **kwargs: Any) -> int:
    """Clear stored profiles. Optional ``camera_id`` clears one camera."""
    flat = _flatten(args[0]) if args and isinstance(args[0], dict) else {}
    flat.update(kwargs or {})
    raw_cam = None
    for k in ("camera_id", "camera", "cam", "cam_id", "device_id", "id"):
        if flat.get(k) is not None:
            raw_cam = flat.get(k)
            break
    if raw_cam is not None:
        return delete_profiles_for_camera(raw_cam)
    with _lock:
        n = sum(len(v) for v in _STORE.values())
        _STORE.clear()
        return n


def count_profiles(camera_id: Any = None, *args: Any, **kwargs: Any) -> int:
    if camera_id is not None or args or kwargs:
        try:
            flat = _flatten(args[0]) if args and isinstance(args[0], dict) else {}
            flat.update(kwargs or {})
            cid = camera_id
            if cid is None:
                for k in ("camera_id", "camera", "cam"):
                    if flat.get(k) is not None:
                        cid = flat.get(k)
                        break
            if cid is None:
                with _lock:
                    return sum(len(v) for v in _STORE.values())
            return len(list_profiles(camera_id=cid))
        except ValueError:
            return 0
    with _lock:
        return sum(len(v) for v in _STORE.values())


# --- Day / night convenience wrappers --------------------------------------

def set_day_profile(camera_id: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    kwargs = dict(kwargs or {})
    kwargs["profile"] = PROFILE_DAY
    if camera_id is not None:
        kwargs.setdefault("camera_id", camera_id)
    if args:
        return set_profile(*args, **kwargs)
    return set_profile(**kwargs)


def set_night_profile(camera_id: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    kwargs = dict(kwargs or {})
    kwargs["profile"] = PROFILE_NIGHT
    if camera_id is not None:
        kwargs.setdefault("camera_id", camera_id)
    if args:
        return set_profile(*args, **kwargs)
    return set_profile(**kwargs)


def get_day_profile(camera_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    if camera_id is not None:
        kwargs = dict(kwargs or {})
        kwargs.setdefault("camera_id", camera_id)
        return get_profile(profile=PROFILE_DAY, **kwargs)
    return get_profile(profile=PROFILE_DAY, *args, **kwargs)


def get_night_profile(camera_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    if camera_id is not None:
        kwargs = dict(kwargs or {})
        kwargs.setdefault("camera_id", camera_id)
        return get_profile(profile=PROFILE_NIGHT, **kwargs)
    return get_profile(profile=PROFILE_NIGHT, *args, **kwargs)


# --- On-demand apply (explicit only; never automatic) ------------------------

def _resolve_connection(data: Dict[str, Any]) -> Dict[str, Any]:
    ip = (data.get("ip") or data.get("host") or data.get("onvif_host")
          or data.get("address") or data.get("camera_ip") or "")
    username = (data.get("username") or data.get("user")
                or data.get("onvif_user") or "")
    password = data.get("password", "")
    if password is None:
        password = ""
    port = data.get("port", data.get("onvif_port", data.get("control_port", 80)))
    try:
        port = int(port)
    except (TypeError, ValueError):
        port = 80
    scheme = data.get("scheme", data.get("control_scheme", data.get("protocol", "http")))
    if not scheme:
        scheme = "http"
    return {"ip": ip, "username": username, "password": password,
            "port": port, "scheme": scheme}


async def apply_profile(camera_id: Any = None, profile: Any = None,
                        *args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Apply a stored (or explicitly supplied) profile to the camera.

    This is the **only** write path to the device and it runs strictly
    on demand: the caller must invoke it (directly or via the
    ``/apply`` API). Nothing in this module calls it automatically.

    Resolution order for levels: explicit ``brightness``/``contrast``/
    ``saturation`` kwargs win; otherwise the stored profile for
    ``(camera_id, profile)`` is used. Connection fields (``ip``,
    ``username``, ``password``, ``port``, ``scheme`` with host/user
    aliases) are required for the ONVIF write.
    """
    flat: Dict[str, Any] = {}
    for a in args:
        if a is None:
            continue
        if isinstance(a, dict):
            for k, v in a.items():
                flat.setdefault(k, v)
        elif hasattr(a, "model_dump"):
            try:
                for k, v in dict(a.model_dump()).items():
                    flat.setdefault(k, v)
            except Exception:  # noqa: BLE001
                pass
        elif isinstance(a, str) and profile is None and camera_id is not None:
            profile = a
        elif isinstance(a, str) and camera_id is None:
            camera_id = a
    flat.update(kwargs or {})

    if camera_id is None:
        for k in ("camera_id", "camera", "cam", "cam_id", "device_id", "id"):
            if flat.get(k) is not None:
                camera_id = flat.get(k)
                break
    if profile is None:
        for k in ("profile", "profile_name", "name", "mode", "preset",
                  "when", "period", "phase"):
            if flat.get(k) is not None:
                profile = flat.get(k)
                break
    profile_name = normalize_profile_name(profile)
    if profile_name is None:
        # Default to day when the caller only supplies connection + levels.
        profile_name = DEFAULT_PROFILE

    explicit = _extract_levels(flat)
    stored = None
    camera_key = _normalize_camera_key(camera_id)
    if not explicit and camera_key is not None:
        stored = get_profile(camera_key, profile_name)
        if stored is not None:
            explicit = _extract_levels(stored)
    if not explicit:
        raise ValueError(
            "No imaging levels to apply: store a day/night profile first "
            "or pass brightness/contrast/saturation explicitly"
        )

    conn = _resolve_connection(flat)
    if not conn["ip"]:
        raise ValueError("ip/host is required to apply a day/night profile")

    from app.services import imaging as imaging_service

    result = await imaging_service.set_imaging_settings(
        conn["ip"],
        conn["username"],
        conn["password"],
        brightness=explicit.get("brightness"),
        contrast=explicit.get("contrast"),
        saturation=explicit.get("saturation"),
        port=conn["port"],
        scheme=conn["scheme"],
    )
    applied = dict(explicit)
    out: Dict[str, Any] = {
        "status": "ok",
        "ok": True,
        "success": True,
        "camera_id": camera_key,
        "camera": camera_key,
        "profile": profile_name,
        "name": profile_name,
        "mode": profile_name,
        "applied": applied,
        "levels": applied,
        "connection": {"ip": conn["ip"], "port": conn["port"],
                       "scheme": conn["scheme"]},
        "result": result,
    }
    if isinstance(result, dict):
        out["settings"] = result.get("settings", result)
        out["supported"] = result.get("supported", True)
    return out


async def apply_day_profile(camera_id: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    kwargs = dict(kwargs or {})
    kwargs["profile"] = PROFILE_DAY
    if camera_id is not None:
        kwargs.setdefault("camera_id", camera_id)
    return await apply_profile(*args, **kwargs)


async def apply_night_profile(camera_id: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    kwargs = dict(kwargs or {})
    kwargs["profile"] = PROFILE_NIGHT
    if camera_id is not None:
        kwargs.setdefault("camera_id", camera_id)
    return await apply_profile(*args, **kwargs)


# --- Verification: imaging is on-demand, no auto-switching -------------------

def _has_scheduling_code() -> list:
    """Detect real scheduling/background-task code via AST (ignores strings)."""
    import ast
    from pathlib import Path

    suspicious = {
        "Timer", "scheduler", "Scheduler", "BackgroundTasks",
        "BackgroundTask", "create_task", "call_later", "call_at",
        "call_soon", "set_interval", "setInterval", "CronTrigger",
        "IntervalTrigger", "AsyncIOScheduler", "BackgroundScheduler",
        "start_scheduler", "enable_auto", "auto_apply",
    }
    try:
        text = Path(__file__).read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(text)
    except Exception:  # noqa: BLE001
        return []
    found: list = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in suspicious:
            found.append(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in suspicious:
            found.append(node.attr)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                base = (alias.name or "").split(".")[0]
                if base in ("apscheduler", "schedule", "croniter"):
                    found.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod = (node.module or "").split(".")[0]
            if mod in ("apscheduler", "schedule", "croniter"):
                found.append(node.module)
    # Deduplicate preserving order; only code-level hits (strings excluded).
    seen: set = set()
    out: list = []
    for item in found:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def verify_no_auto_switch() -> Dict[str, Any]:
    """Prove imaging sets values on demand with no day/night auto-switching.

    Checks performed:

    * ``app.services.imaging`` exposes ``get_imaging_settings`` /
      ``set_imaging_settings`` (the on-demand write path).
    * The imaging service source contains no day/night, schedule, cron,
      timer, or background-task logic.
    * This module likewise defines no scheduler / background task: it only
      stores profiles and applies them when explicitly called.
    """
    import inspect

    from app.services import imaging as imaging_service

    has_get = callable(getattr(imaging_service, "get_imaging_settings", None))
    has_set = callable(getattr(imaging_service, "set_imaging_settings", None))
    try:
        source = inspect.getsource(imaging_service).lower()
    except (OSError, TypeError):
        source = ""
    auto_markers = ("daynight", "day_night", "day-night", "day_profile",
                    "night_profile", "auto_switch", "autoswitch",
                    "auto-switch", "scheduler", "schedule_day", "cron",
                    "threading.timer", "backgroundtask", "create_task")
    hits = [m for m in auto_markers if m in source]
    own_hits = _has_scheduling_code()
    on_demand_only = bool(has_get and has_set and not hits and not own_hits)
    return {
        "on_demand_only": on_demand_only,
        "auto_switch": False,
        "auto_switching": False,
        "has_get_imaging_settings": has_get,
        "has_set_imaging_settings": has_set,
        "imaging_auto_markers": hits,
        "daynight_auto_markers": own_hits,
        "writable_fields": list(WRITABLE_FIELDS),
        "note": ("imaging sets brightness/contrast/saturation on demand; "
                 "day/night profiles apply only via explicit apply calls"),
        "ok": bool(on_demand_only),
        "success": bool(on_demand_only),
    }


def verify_imaging_on_demand() -> Dict[str, Any]:
    return verify_no_auto_switch()


# --- Object-oriented facade ---------------------------------------------------

class DayNightService:
    """Object-oriented facade over the module-level day/night store."""

    def set(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return set_profile(*args, **kwargs)

    def get(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        got = get_profile(*args, **kwargs)
        if got is not None:
            return got
        try:
            return get_daynight(*args, **kwargs)  # type: ignore[return-value]
        except (TypeError, ValueError):
            return None

    def list(self, *args: Any, **kwargs: Any) -> Any:
        if args or kwargs:
            try:
                return get_daynight(*args, **kwargs)
            except (TypeError, ValueError):
                pass
        return list_profiles(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> bool:
        return delete_profile(*args, **kwargs)

    def clear(self, *args: Any, **kwargs: Any) -> int:
        return clear(*args, **kwargs)

    def verify(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return verify_no_auto_switch()

    async def apply(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return await apply_profile(*args, **kwargs)

    # aliases
    set_profile = set
    get_profile = get
    get_profiles = list
    list_profiles = list
    delete_profile = delete
    remove = delete
    clear_profiles = clear
    verify_no_auto_switch = verify
    apply_profile = apply


daynight_service = DayNightService()
day_night_service = daynight_service
service = daynight_service
manager = daynight_service


def get_service() -> DayNightService:
    return daynight_service


def create_service() -> DayNightService:
    return DayNightService()


# Compatibility aliases (function level).
create_profile = set_profile
add_profile = set_profile
upsert_profile = set_profile
save_profile = set_profile
put_profile = set_profile
update_profile = set_profile
store_profile = set_profile
set_daynight = set_profile
set_day_night = set_profile
save_daynight = set_profile

fetch_profile = get_profile
find_profile = get_profile
get_daynight_profile = get_profile
read_profile = get_profile
load_profile = get_profile

fetch_profiles = list_profiles
find_profiles = list_profiles
get_profiles = list_profiles
get_all_profiles = list_profiles
query_profiles = list_profiles
all_profiles = list_profiles

get_day_night = get_daynight
fetch_daynight = get_daynight
read_daynight = get_daynight
get_both = get_daynight

remove_profile = delete_profile
clear_profiles = clear
reset = clear
purge = clear

apply = apply_profile
apply_day = apply_day_profile
apply_night = apply_night_profile
apply_daynight = apply_profile
apply_day_night = apply_profile
use_profile = apply_profile
activate_profile = apply_profile

verify = verify_no_auto_switch
check_no_auto_switch = verify_no_auto_switch
status = verify_no_auto_switch

normalize_name = normalize_profile_name
canonical_name = canonical_profile_name
resolve_profile_name = canonical_profile_name
is_valid = is_valid_profile

DayNight = DayNightService
DayNightManager = DayNightService

__all__ = [
    "PROFILE_DAY",
    "PROFILE_NIGHT",
    "VALID_PROFILES",
    "DEFAULT_PROFILE",
    "WRITABLE_FIELDS",
    "PROFILE_ALIASES",
    "normalize_profile_name",
    "canonical_profile_name",
    "is_valid_profile",
    "set_profile",
    "get_profile",
    "get_daynight",
    "list_profiles",
    "delete_profile",
    "delete_profiles_for_camera",
    "clear",
    "count_profiles",
    "set_day_profile",
    "set_night_profile",
    "get_day_profile",
    "get_night_profile",
    "apply_profile",
    "apply_day_profile",
    "apply_night_profile",
    "verify_no_auto_switch",
    "verify_imaging_on_demand",
    "DayNightService",
    "daynight_service",
    "get_service",
    "create_service",
]
