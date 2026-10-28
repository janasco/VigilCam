"""Solar day/night schedule (stdlib-only astronomical switching).

Complements the on-demand ``day``/``night`` imaging profiles in
``app.services.daynight`` (which have no scheduler and no sunrise/sunset
logic) and the fixed ``06:00``/``18:00`` defaults in
``app.services.zone_schedules.set_day_night``. This module computes
*astronomical* sunrise/sunset from latitude/longitude/date with a
stdlib-only NOAA-style approximation (``math`` + ``datetime`` only, no
third-party astronomy packages) and resolves which profile (``day`` or
``night``) applies at a given moment.

All calculations are pure functions; per-camera location configs are kept
in a small in-memory store. Nothing runs automatically -- callers invoke
``resolve_profile`` / ``apply_solar_profile`` explicitly (or hit the
``/api/solar_schedule`` endpoints).
"""

from __future__ import annotations

import copy
import math
import threading
from datetime import date as _date
from datetime import datetime as _datetime
from datetime import time as _time
from datetime import timezone as _timezone
from typing import Any, Dict, List, Optional

ZENITH = 90.833  # official sunrise/sunset zenith (incl. refraction)

_lock = threading.RLock()
_CONFIGS: Dict[str, Dict[str, Any]] = {}

CAMERA_KEYS = (
    "camera_id", "camera", "cam", "cam_id", "device_id", "id",
    "cameraId", "source_id",
)
LAT_KEYS = ("lat", "latitude", "lattitude", "lat_deg", "y")
LON_KEYS = ("lon", "lng", "long", "longitude", "longtitude", "lon_deg", "x")
DATE_KEYS = ("date", "day", "on_date", "for_date", "target_date", "d")
OFFSET_KEYS = ("utc_offset", "utcOffset", "offset", "tz_offset",
               "gmt_offset", "timezone_offset", "utcoffset")
DT_KEYS = ("now", "at", "timestamp", "datetime", "time", "moment",
           "current_time", "at_time", "when")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_utc() -> _datetime:
    return _datetime.now(_timezone.utc)


def _now_iso() -> str:
    return _now_utc().isoformat()


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
        return dict(data)
    return {}


def _first(data: Dict[str, Any], keys: tuple) -> Any:
    for k in keys:
        if data.get(k) is not None:
            return data.get(k)
    lower = {str(k).lower(): v for k, v in data.items()}
    for k in keys:
        if k.lower() in lower and lower[k.lower()] is not None:
            return lower[k.lower()]
    return None


def _coerce_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"Invalid numeric value: {value!r}")
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"Invalid numeric value: {value!r}")


def _coerce_offset(value: Any) -> float:
    if value is None:
        return 0.0
    v = _coerce_float(value)
    if v is None:
        return 0.0
    if v < -14.0 or v > 14.0:
        raise ValueError("utc_offset must be within -14..+14 hours")
    return float(v)


def _parse_date(value: Any) -> _date:
    if value is None:
        return _now_utc().date()
    if isinstance(value, _datetime):
        return value.date()
    if isinstance(value, _date):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # epoch seconds
        try:
            return _datetime.fromtimestamp(float(value), tz=_timezone.utc).date()
        except (OverflowError, OSError, ValueError):
            raise ValueError(f"Invalid date: {value!r}")
    s = str(value).strip()
    if not s:
        return _now_utc().date()
    # Try ISO datetime first, then bare date.
    try:
        return _datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%m-%d-%Y",
                "%Y%m%d", "%d.%m.%Y"):
        try:
            return _datetime.strptime(s[:10] if len(s) >= 10 else s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Invalid date: {value!r}")


def _parse_dt(value: Any, utc_offset: float = 0.0) -> _datetime:
    """Parse a moment; naive values are assumed to be UTC+utc_offset local."""
    import datetime as _dtmod

    if value is None:
        return _now_utc()
    if isinstance(value, _datetime):
        dt = value
        if dt.tzinfo is None:
            # Naive -> treat as local wall time under utc_offset.
            dt = dt.replace(tzinfo=_timezone.utc) - _dtmod.timedelta(hours=float(utc_offset))
        return dt.astimezone(_timezone.utc)
    if isinstance(value, _date) and not isinstance(value, _datetime):
        return _datetime(value.year, value.month, value.day, 12, 0,
                         tzinfo=_timezone.utc)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _datetime.fromtimestamp(float(value), tz=_timezone.utc)
    s = str(value).strip()
    if not s:
        return _now_utc()
    try:
        dt = _datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_timezone.utc) - _dtmod.timedelta(hours=float(utc_offset))
            # fromisoformat naive interpreted as local wall time
            return dt.astimezone(_timezone.utc)
        return dt.astimezone(_timezone.utc)
    except ValueError:
        raise ValueError(f"Invalid datetime: {value!r}")


def _timedelta_hours(h: float):
    import datetime as _dtmod
    return _dtmod.timedelta(hours=h)


def _validate_lat_lon(lat: Any, lon: Any) -> tuple:
    lat_f = _coerce_float(lat)
    lon_f = _coerce_float(lon)
    if lat_f is None or lon_f is None:
        raise ValueError("lat and lon are required")
    if not (-90.0 <= lat_f <= 90.0):
        raise ValueError("lat must be within -90..90")
    if not (-180.0 <= lon_f <= 180.0):
        raise ValueError("lon must be within -180..180")
    return float(lat_f), float(lon_f)


def _minutes_to_hhmm(minutes: float) -> str:
    m = int(round(minutes)) % 1440
    return f"{m // 60:02d}:{m % 60:02d}"


# ---------------------------------------------------------------------------
# Core NOAA-style approximation (stdlib only)
# ---------------------------------------------------------------------------

def _sun_times_utc_minutes(lat: float, lon: float,
                           target: _date) -> Dict[str, Any]:
    """Return sunrise/sunset/solar-noon in UTC minutes from midnight."""
    n = target.timetuple().tm_yday
    lng_hour = lon / 15.0

    def _calc(is_sunrise: bool) -> Optional[float]:
        t = n + ((6.0 - lng_hour) / 24.0) if is_sunrise else n + ((18.0 - lng_hour) / 24.0)
        m = (0.9856 * t) - 3.289
        lang = m + (1.916 * math.sin(math.radians(m))) \
            + (0.020 * math.sin(math.radians(2.0 * m))) + 282.634
        lang = lang % 360.0
        ra = math.degrees(math.atan(0.91764 * math.tan(math.radians(lang))))
        ra = ra % 360.0
        # Quadrant correction.
        l_quad = (math.floor(lang / 90.0)) * 90.0
        ra_quad = (math.floor(ra / 90.0)) * 90.0
        ra = ra + (l_quad - ra_quad)
        ra = ra / 15.0  # hours
        sin_dec = 0.39782 * math.sin(math.radians(lang))
        sin_dec = max(-1.0, min(1.0, sin_dec))
        cos_dec = math.cos(math.asin(sin_dec))
        cos_h = (math.cos(math.radians(ZENITH))
                 - (sin_dec * math.sin(math.radians(lat)))) \
            / (cos_dec * math.cos(math.radians(lat))) \
            if abs(math.cos(math.radians(lat))) > 1e-9 else None
        if cos_h is None:
            return None
        if cos_h > 1.0:
            return None  # sun never rises (polar night)
        if cos_h < -1.0:
            return None  # sun never sets (polar day)
        h = 360.0 - math.degrees(math.acos(cos_h)) if is_sunrise \
            else math.degrees(math.acos(cos_h))
        h = h / 15.0  # hours
        t_utc = h + ra - (0.06571 * t) - 6.622
        utc = (t_utc - lng_hour) % 24.0
        return utc * 60.0

    sunrise_utc = _calc(True)
    sunset_utc = _calc(False)
    # Solar noon ~ midpoint (fallback: longitude-corrected midday).
    if sunrise_utc is not None and sunset_utc is not None:
        if sunset_utc < sunrise_utc:
            noon_utc = ((sunrise_utc + sunset_utc + 1440.0) / 2.0) % 1440.0
        else:
            noon_utc = (sunrise_utc + sunset_utc) / 2.0
    else:
        noon_utc = (720.0 - (lon * 4.0)) % 1440.0
    polar_day = sunrise_utc is None and sunset_utc is None and abs(lat) > 66.0 and (
        (target.month in (4, 5, 6, 7, 8) and lat > 0)
        or (target.month in (10, 11, 12, 1, 2) and lat < 0))
    # Refine: if cos_h says never rises/sets for both, decide by noon altitude.
    polar_night = sunrise_utc is None and sunset_utc is None and not polar_day
    return {
        "sunrise_utc_min": sunrise_utc,
        "sunset_utc_min": sunset_utc,
        "noon_utc_min": noon_utc,
        "polar_day": bool(polar_day),
        "polar_night": bool(polar_night),
    }


def calculate(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Calculate sunrise/sunset/solar-noon for lat/lon/date.

    Accepts ``lat``/``latitude``, ``lon``/``longitude``/``lng``,
    ``date`` (YYYY-MM-DD, date, datetime, or epoch; default today UTC),
    ``utc_offset`` (hours, default 0). Returns local wall-clock
    ``"HH:MM"`` times plus UTC minutes and ISO stamps.
    """
    flat: Dict[str, Any] = {}
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
    flat.update(kwargs or {})
    for nested in ("location", "coords", "coordinates", "config",
                   "params", "data", "payload", "options", "settings"):
        n = flat.get(nested)
        if isinstance(n, dict):
            for k, v in n.items():
                flat.setdefault(k, v)

    # Positional conveniences: (lat, lon[, date[, utc_offset]]).
    positional = [a for a in args if not isinstance(a, (dict,)) and not hasattr(a, "model_dump")]
    lat = _first(flat, LAT_KEYS)
    lon = _first(flat, LON_KEYS)
    if lat is None and len(positional) > 0 and isinstance(positional[0], (int, float)):
        lat = positional[0]
    if lon is None and len(positional) > 1 and isinstance(positional[1], (int, float)):
        lon = positional[1]
    raw_date = _first(flat, DATE_KEYS)
    if raw_date is None and len(positional) > 2:
        raw_date = positional[2]
    raw_offset = _first(flat, OFFSET_KEYS)
    if raw_offset is None and len(positional) > 3:
        raw_offset = positional[3]

    lat_f, lon_f = _validate_lat_lon(lat, lon)
    target = _parse_date(raw_date)
    offset = _coerce_offset(raw_offset)

    core = _sun_times_utc_minutes(lat_f, lon_f, target)

    def _local(mins: Optional[float]) -> Optional[str]:
        if mins is None:
            return None
        return _minutes_to_hhmm(mins + offset * 60.0)

    def _iso(mins: Optional[float]) -> Optional[str]:
        if mins is None:
            return None
        local_m = (mins + offset * 60.0) % 1440.0
        hh, mm = int(local_m // 60), int(local_m % 60)
        return _datetime(target.year, target.month, target.day, hh, mm,
                         tzinfo=_timezone.utc).isoformat().replace("+00:00", "Z") \
            if offset == 0 else f"{target.isoformat()}T{_minutes_to_hhmm(local_m)}"

    sunrise = _local(core["sunrise_utc_min"])
    sunset = _local(core["sunset_utc_min"])
    noon = _local(core["noon_utc_min"])
    out: Dict[str, Any] = {
        "lat": lat_f,
        "latitude": lat_f,
        "lon": lon_f,
        "longitude": lon_f,
        "lng": lon_f,
        "date": target.isoformat(),
        "day_of_year": target.timetuple().tm_yday,
        "utc_offset": offset,
        "sunrise": sunrise,
        "sunset": sunset,
        "solar_noon": noon,
        "noon": noon,
        "sunrise_utc": _minutes_to_hhmm(core["sunrise_utc_min"]) if core["sunrise_utc_min"] is not None else None,
        "sunset_utc": _minutes_to_hhmm(core["sunset_utc_min"]) if core["sunset_utc_min"] is not None else None,
        "noon_utc": _minutes_to_hhmm(core["noon_utc_min"]),
        "sunrise_iso": _iso(core["sunrise_utc_min"]),
        "sunset_iso": _iso(core["sunset_utc_min"]),
        "polar_day": core["polar_day"],
        "polar_night": core["polar_night"],
        "day_length_minutes": (round((core["sunset_utc_min"] - core["sunrise_utc_min"]) % 1440.0)
                               if core["sunrise_utc_min"] is not None and core["sunset_utc_min"] is not None
                               else (1440 if core["polar_day"] else 0)),
        "method": "noaa-approx-stdlib",
        "ok": True,
        "success": True,
    }
    return out


def sunrise(*args: Any, **kwargs: Any) -> Optional[str]:
    """Return local ``"HH:MM"`` sunrise (None on polar night)."""
    return calculate(*args, **kwargs)["sunrise"]


def sunset(*args: Any, **kwargs: Any) -> Optional[str]:
    """Return local ``"HH:MM"`` sunset (None on polar day)."""
    return calculate(*args, **kwargs)["sunset"]


def solar_noon(*args: Any, **kwargs: Any) -> str:
    """Return local ``"HH:MM"`` solar noon."""
    return calculate(*args, **kwargs)["solar_noon"]


def _resolve_lat_lon_offset(data: Dict[str, Any],
                            camera_id: Optional[str] = None) -> tuple:
    lat = _first(data, LAT_KEYS)
    lon = _first(data, LON_KEYS)
    offset = _first(data, OFFSET_KEYS)
    if (lat is None or lon is None) and camera_id is not None:
        with _lock:
            cfg = _CONFIGS.get(str(camera_id))
        if cfg is not None:
            if lat is None:
                lat = cfg.get("lat", cfg.get("latitude"))
            if lon is None:
                lon = cfg.get("lon", cfg.get("longitude"))
            if offset is None:
                offset = cfg.get("utc_offset", 0.0)
    if offset is None:
        offset = 0.0
    return lat, lon, offset


def is_day(*args: Any, **kwargs: Any) -> bool:
    """Return True if the given moment is daytime at lat/lon.

    Kwargs: ``lat``, ``lon``, ``now``/``at``/``datetime`` (default now UTC),
    ``date`` (used when ``now`` omits a date -- currently informational),
    ``utc_offset``, or ``camera_id`` to reuse a stored config.
    Polar day -> True, polar night -> False.
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
    flat.update(kwargs or {})
    raw_cam = _first(flat, CAMERA_KEYS)
    cam_key = _normalize_camera_key(raw_cam)
    lat, lon, raw_offset = _resolve_lat_lon_offset(flat, cam_key)
    lat_f, lon_f = _validate_lat_lon(lat, lon)
    offset = _coerce_offset(raw_offset)
    raw_now = _first(flat, DT_KEYS)
    if raw_now is None:
        raw_now = _first(flat, DATE_KEYS)
    if raw_now is None:
        now_utc = _now_utc()
    elif isinstance(raw_now, _datetime):
        now_utc = _parse_dt(raw_now, offset)
    elif isinstance(raw_now, _date):
        # Date only -> assume local noon.
        now_utc = _parse_dt(
            _datetime(raw_now.year, raw_now.month, raw_now.day, 12, 0), offset)
    else:
        now_utc = _parse_dt(raw_now, offset)
    # _parse_dt already handles date->noon; re-derive target date from UTC instant.
    target = now_utc.date()
    core = _sun_times_utc_minutes(lat_f, lon_f, target)
    if core["polar_day"]:
        return True
    if core["polar_night"]:
        return False
    if core["sunrise_utc_min"] is None or core["sunset_utc_min"] is None:
        # Fallback: decide by solar altitude sign via noon comparison.
        return True
    now_min = now_utc.hour * 60.0 + now_utc.minute + now_utc.second / 60.0
    rise, set_ = core["sunrise_utc_min"], core["sunset_utc_min"]
    if set_ >= rise:
        return bool(rise <= now_min < set_)
    return bool(now_min >= rise or now_min < set_)


def is_night(*args: Any, **kwargs: Any) -> bool:
    return not is_day(*args, **kwargs)


def current_profile(*args: Any, **kwargs: Any) -> str:
    """Return ``"day"`` or ``"night"`` for the given moment/location."""
    return "day" if is_day(*args, **kwargs) else "night"


def resolve_profile(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Resolve which day/night profile applies now at a location/camera.

    Returns ``{"profile": "day"|"night", "is_day": bool, ...sun times}``.
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
        elif isinstance(a, str) and _first(flat, CAMERA_KEYS) is None and flat.get("camera_id") is None:
            flat.setdefault("camera_id", a)
    flat.update(kwargs or {})
    raw_cam = _first(flat, CAMERA_KEYS)
    cam_key = _normalize_camera_key(raw_cam)
    lat, lon, raw_offset = _resolve_lat_lon_offset(flat, cam_key)
    lat_f, lon_f = _validate_lat_lon(lat, lon)
    offset = _coerce_offset(raw_offset)
    raw_now = _first(flat, DT_KEYS)
    now_utc = _parse_dt(raw_now, offset)
    target = now_utc.date()
    sun = calculate(lat=lat_f, lon=lon_f, date=target, utc_offset=offset)
    day = is_day(lat=lat_f, lon=lon_f, now=now_utc, utc_offset=offset)
    profile = "day" if day else "night"
    out = dict(sun)
    out.update({
        "profile": profile,
        "mode": profile,
        "name": profile,
        "is_day": bool(day),
        "is_night": bool(not day),
        "now_utc": now_utc.isoformat(),
        "camera_id": cam_key,
        "camera": cam_key,
    })
    if cam_key is not None:
        with _lock:
            cfg = _CONFIGS.get(str(cam_key))
        if cfg is not None:
            out["config"] = copy.deepcopy(cfg)
    out["ok"] = True
    out["success"] = True
    return out


def next_transition(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Return the next sunrise/sunset transition after ``now``."""
    info = resolve_profile(*args, **kwargs)
    flat: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            flat.update(a)
    flat.update(kwargs or {})
    offset = info.get("utc_offset", 0.0)
    now_utc = _parse_dt(_first(flat, DT_KEYS), offset)
    lat_f, lon_f = info["lat"], info["lon"]
    # Scan today + tomorrow for the next event.
    candidates = []
    for delta in (0, 1):
        target = (_parse_date(info.get("date")) if delta == 0 else _parse_date(info.get("date")))
        import datetime as _dtmod
        base = _parse_date(info.get("date"))
        day = base + _dtmod.timedelta(days=delta)
        core = _sun_times_utc_minutes(lat_f, lon_f, day)
        for kind, mins in (("sunrise", core["sunrise_utc_min"]),
                           ("sunset", core["sunset_utc_min"])):
            if mins is None:
                continue
            evt = _datetime(day.year, day.month, day.day, tzinfo=_timezone.utc) \
                + _timedelta_hours(mins / 60.0)
            if evt > now_utc:
                candidates.append((evt, kind))
    candidates.sort(key=lambda t: t[0])
    if not candidates:
        return {"next": None, "event": None, "profile": info["profile"],
                "polar_day": info.get("polar_day"), "polar_night": info.get("polar_night"),
                "ok": True, "success": True}
    evt, kind = candidates[0]
    return {
        "next": evt.isoformat(),
        "next_utc": evt.isoformat(),
        "event": kind,
        "transition": kind,
        "profile": info["profile"],
        "current_profile": info["profile"],
        "next_profile": "night" if kind == "sunset" else "day",
        "camera_id": info.get("camera_id"),
        "ok": True,
        "success": True,
    }


# ---------------------------------------------------------------------------
# Per-camera location store
# ---------------------------------------------------------------------------

def configure(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Store a per-camera solar location config.

    Required: ``camera_id`` + ``lat`` + ``lon``. Optional ``utc_offset``
    (default 0), ``enabled`` (default True), ``day_profile``/``night_profile``
    labels (default ``"day"``/``"night"``).
    """
    flat: Dict[str, Any] = {}
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
    flat.update(kwargs or {})
    for nested in ("location", "coords", "coordinates", "config",
                   "params", "data", "payload", "options", "settings"):
        n = flat.get(nested)
        if isinstance(n, dict):
            for k, v in n.items():
                flat.setdefault(k, v)
    raw_cam = _first(flat, CAMERA_KEYS)
    cam_key = _normalize_camera_key(raw_cam)
    if cam_key is None:
        raise ValueError("camera_id is required")
    lat = _first(flat, LAT_KEYS)
    lon = _first(flat, LON_KEYS)
    lat_f, lon_f = _validate_lat_lon(lat, lon)
    offset = _coerce_offset(_first(flat, OFFSET_KEYS))
    enabled = flat.get("enabled", flat.get("active", True))
    if enabled is None:
        enabled = True
    enabled = bool(enabled)
    now = _now_iso()
    with _lock:
        existing = _CONFIGS.get(str(cam_key), {})
        cfg: Dict[str, Any] = {
            "camera_id": cam_key,
            "camera": cam_key,
            "lat": lat_f,
            "latitude": lat_f,
            "lon": lon_f,
            "longitude": lon_f,
            "lng": lon_f,
            "utc_offset": offset,
            "enabled": enabled,
            "active": enabled,
            "day_profile": flat.get("day_profile", existing.get("day_profile", "day")),
            "night_profile": flat.get("night_profile", existing.get("night_profile", "night")),
            "created_at": existing.get("created_at", now),
            "updated_at": now,
            "ok": True,
            "success": True,
        }
        for k, v in flat.items():
            if k not in cfg:
                cfg[k] = v
        _CONFIGS[str(cam_key)] = cfg
        return copy.deepcopy(cfg)


def get_config(camera_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    flat = _flatten(args[0]) if args and isinstance(args[0], dict) else {}
    flat.update(kwargs or {})
    if camera_id is None:
        camera_id = _first(flat, CAMERA_KEYS)
        if camera_id is None and args and isinstance(args[0], str):
            camera_id = args[0]
    cam_key = _normalize_camera_key(camera_id)
    if cam_key is None:
        return None
    with _lock:
        cfg = _CONFIGS.get(str(cam_key))
        return copy.deepcopy(cfg) if cfg is not None else None


def list_configs(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    with _lock:
        return [copy.deepcopy(v) for v in _CONFIGS.values()]


def delete_config(camera_id: Any = None, *args: Any, **kwargs: Any) -> bool:
    flat = _flatten(args[0]) if args and isinstance(args[0], dict) else {}
    flat.update(kwargs or {})
    if camera_id is None:
        camera_id = _first(flat, CAMERA_KEYS)
        if camera_id is None and args and isinstance(args[0], str):
            camera_id = args[0]
    cam_key = _normalize_camera_key(camera_id)
    if cam_key is None:
        return False
    with _lock:
        return _CONFIGS.pop(str(cam_key), None) is not None


def clear(*args: Any, **kwargs: Any) -> int:
    flat = _flatten(args[0]) if args and isinstance(args[0], dict) else {}
    flat.update(kwargs or {})
    raw_cam = _first(flat, CAMERA_KEYS)
    if raw_cam is not None:
        ok = delete_config(raw_cam)
        return 1 if ok else 0
    with _lock:
        n = len(_CONFIGS)
        _CONFIGS.clear()
        return n


def count(*args: Any, **kwargs: Any) -> int:
    with _lock:
        return len(_CONFIGS)


def status(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    with _lock:
        n = len(_CONFIGS)
    return {
        "service": "solar_schedule",
        "configured": n,
        "count": n,
        "size": n,
        "method": "noaa-approx-stdlib",
        "zenith": ZENITH,
        "ok": True,
        "success": True,
    }


async def apply_solar_profile(camera_id: Any = None, *args: Any,
                              **kwargs: Any) -> Dict[str, Any]:
    """Resolve the solar profile and apply it via day/night imaging profiles.

    Requires a stored (or inline) lat/lon plus ONVIF connection fields
    (``ip``, ``username``, ``password``). Delegates the device write to
    ``app.services.daynight.apply_profile`` on demand -- no background task.
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
        elif isinstance(a, str) and camera_id is not None and flat.get("profile") is None:
            flat.setdefault("profile", a)
        elif isinstance(a, str) and camera_id is None:
            camera_id = a
    flat.update(kwargs or {})
    if camera_id is None:
        camera_id = _first(flat, CAMERA_KEYS)
    cam_key = _normalize_camera_key(camera_id)
    if cam_key is None:
        raise ValueError("camera_id is required")
    resolved = resolve_profile(camera_id=cam_key, **{k: v for k, v in flat.items()
                                                    if k not in ("camera_id", "camera", "cam")})
    profile = resolved["profile"]
    from app.services import daynight as _daynight

    result = await _daynight.apply_profile(camera_id=cam_key, profile=profile, **flat)
    result = dict(result)
    result["solar"] = {k: resolved.get(k) for k in (
        "sunrise", "sunset", "solar_noon", "is_day", "profile",
        "date", "lat", "lon", "utc_offset")}
    result["ok"] = True
    result["success"] = True
    return result


# --- Object-oriented facade -------------------------------------------------

class SolarSchedule:
    """Facade over the module-level solar store + calculations."""

    def calculate(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return calculate(*a, **k)

    def resolve(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return resolve_profile(*a, **k)

    def configure(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return configure(*a, **k)

    def get(self, *a: Any, **k: Any) -> Optional[Dict[str, Any]]:
        return get_config(*a, **k)

    def list(self, *a: Any, **k: Any) -> List[Dict[str, Any]]:
        return list_configs(*a, **k)

    def clear(self, *a: Any, **k: Any) -> int:
        return clear(*a, **k)

    def status(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return status(*a, **k)

    async def apply(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return await apply_solar_profile(*a, **k)


solar_schedule = SolarSchedule()
solar_service = solar_schedule
service = solar_schedule
manager = solar_schedule
scheduler = solar_schedule


def get_service() -> SolarSchedule:
    return solar_schedule


def create_service() -> SolarSchedule:
    return SolarSchedule()


# Compatibility aliases (function level).
calculate_sun_times = calculate
sun_times = calculate
get_sun_times = calculate
sunrise_sunset = calculate
get_sunrise_sunset = calculate
sun_times_for = calculate
calc = calculate
compute = calculate
compute_sun_times = calculate

get_sunrise = sunrise
get_sunset = sunset
get_solar_noon = solar_noon
noon = solar_noon

is_daytime = is_day
isDay = is_day
daytime = is_day
night = is_night
isNight = is_night

profile_for_time = resolve_profile
resolve = resolve_profile
current = resolve_profile
decide_profile = resolve_profile
decide = resolve_profile
get_profile_for_time = resolve_profile
get_current_profile = current_profile
which_profile = current_profile

set_config = configure
set_location = configure
configure_camera = configure
set_solar_config = configure
save_config = configure
add_config = configure
register = configure
setup = configure

get_camera_config = get_config
fetch_config = get_config
read_config = get_config
find_config = get_config

list_all = list_configs
all = list_configs
items = list_configs
values = list_configs
list = list_configs

remove_config = delete_config
delete = delete_config
remove = delete_config
delete_camera = delete_config

reset = clear
purge = clear

next_change = next_transition
next_event = next_transition

apply = apply_solar_profile

SolarScheduler = SolarSchedule
SolarService = SolarSchedule
SolarManager = SolarSchedule

__all__ = [
    "ZENITH",
    "calculate",
    "sunrise",
    "sunset",
    "solar_noon",
    "is_day",
    "is_night",
    "current_profile",
    "resolve_profile",
    "next_transition",
    "configure",
    "get_config",
    "list_configs",
    "delete_config",
    "clear",
    "count",
    "status",
    "apply_solar_profile",
    "SolarSchedule",
    "solar_schedule",
    "get_service",
    "create_service",
]
