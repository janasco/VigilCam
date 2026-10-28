"""Per-camera stream profiles (main/sub) with detect/record role separation.

Background
----------
Shinobi monitors (``web/pages/blocks/monitoredit.ejs`` + ``camera.js``) expose
separate sections for each pipeline stage: Connection/Input (full URL or
host+path), Stream (``stream_type``, ``stream_vcodec``/``stream_acodec``,
``stream_fps``, ``stream_scale_x``/``stream_scale_y``, ``svf``), JPEG API
(``snap_*``), Recording (``ext``, ``vcodec``/``acodec``, ``fps``,
``width``/``height``, ``cutoff``) and Detector (``detector_*``). Each stage
can also bind a different FFmpeg input via ``input_map_choices``
(``stream``/``record``/``detector``/``snap``) and extra ``channel`` rows act
as sub-stream channels with their own ``stream_type``/codecs.

Frigate (``frigate/config/camera/ffmpeg.py`` + ``config/config.py``) models
the same idea as *roles*: every camera has ``ffmpeg.inputs: list[CameraInput]``
where each input is ``{path, roles}`` with
``CameraRoleEnum = audio | record | record_sub | detect``. Output args differ
per role (``detect`` uses rawvideo, ``record``/``record_sub`` use segmented
mp4), ``detect`` is required, each role may be used only once, and
``verify_config_roles`` requires a ``record`` role when recording is enabled
(``record_sub`` falls back to ``record`` args).

VigilCam verification
---------------------
VigilCam cameras have only a **single source**. ``app.models.entities.Camera``
exposes exactly one ``source: Mapped[str]`` column (plus ``onvif_host`` /
``ip_address`` helpers); there are no ``main_url``/``sub_url``,
``main_stream``/``sub_stream`` or per-role columns. ``app.schemas.CameraBase``
likewise carries a single ``source`` field, and ``app.services.ingestion``
takes ``(main_url, sub_url)`` only at launch time. See
``verify_single_source_camera_model()`` which introspects the model and
asserts this at runtime.

This module adds the missing profiles layer *without* a DB migration (and
without touching ``app/main.py``): an in-memory, thread-safe per-camera
store holding a ``main`` (high-res, record-oriented) and a ``sub``
(low-res, detect/live-oriented) profile plus Frigate-style role mapping.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

PROFILE_MAIN = "main"
PROFILE_SUB = "sub"
VALID_PROFILES = (PROFILE_MAIN, PROFILE_SUB)

# Frigate parity roles, plus VigilCam live/snapshot conveniences.
# Shinobi mapping: live<->stream section, record<->recording section,
# detect<->detector section, snapshot<->snap section.
VALID_ROLES = ("detect", "record", "record_sub", "live", "snapshot", "audio")

ROLE_ALIASES = {
    "stream": "live",
    "streaming": "live",
    "live_stream": "live",
    "snap": "snapshot",
    "jpeg": "snapshot",
    "detection": "detect",
    "detector": "detect",
    "recording": "record",
    "rec": "record",
    "record-sub": "record_sub",
    "recordsub": "record_sub",
    "sub_record": "record_sub",
}

PROFILE_ALIASES = {
    "primary": PROFILE_MAIN,
    "high": PROFILE_MAIN,
    "highres": PROFILE_MAIN,
    "high_res": PROFILE_MAIN,
    "main_stream": PROFILE_MAIN,
    "mainstream": PROFILE_MAIN,
    "secondary": PROFILE_SUB,
    "low": PROFILE_SUB,
    "lowres": PROFILE_SUB,
    "low_res": PROFILE_SUB,
    "sub_stream": PROFILE_SUB,
    "substream": PROFILE_SUB,
}

_lock = threading.Lock()
_STORE: Dict[str, Dict[str, Dict[str, Any]]] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_camera_key(value: Any) -> Optional[str]:
    if value is None or isinstance(value, bool):
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return str(int(float(s))) if float(s) == int(float(s)) else s
    except (TypeError, ValueError):
        return s


def _normalize_profile_name(value: Any) -> Optional[str]:
    if value is None or isinstance(value, bool):
        return None
    s = str(value).strip().lower()
    if not s:
        return None
    if s in VALID_PROFILES:
        return s
    return PROFILE_ALIASES.get(s)


def _normalize_role(value: Any) -> Optional[str]:
    if value is None or isinstance(value, bool):
        return None
    s = str(value).strip().lower()
    if not s:
        return None
    s = ROLE_ALIASES.get(s, s)
    if s in VALID_ROLES:
        return s
    return None


def _normalize_roles(value: Any) -> Optional[List[str]]:
    if value is None:
        return None
    if isinstance(value, str):
        parts = [p.strip() for p in value.replace(",", " ").split()]
        items: List[str] = []
        for p in parts:
            r = _normalize_role(p)
            if r is not None and r not in items:
                items.append(r)
        return items
    if isinstance(value, (list, tuple, set)):
        items = []
        for v in value:
            r = _normalize_role(v)
            if r is not None and r not in items:
                items.append(r)
        return items
    r = _normalize_role(value)
    return [r] if r is not None else []


def _normalize_url(value: Any) -> Optional[str]:
    if value is None or isinstance(value, bool):
        return None
    s = str(value).strip()
    return s if s else None


def _coerce_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(str(value).strip()))
        except (TypeError, ValueError):
            return None


def _coerce_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _flatten(data: Any) -> Dict[str, Any]:
    if data is None:
        return {}
    if hasattr(data, "model_dump"):
        try:
            return dict(data.model_dump())
        except Exception:
            pass
    if isinstance(data, dict):
        out = dict(data)
        for nested in ("profile", "config", "params", "data", "options", "payload"):
            n = out.get(nested)
            if isinstance(n, dict):
                for k, v in n.items():
                    out.setdefault(k, v)
        return out
    return {}


def verify_single_source_camera_model() -> Dict[str, Any]:
    """Prove VigilCam cameras expose only a single ``source`` column.

    Inspects ``app.models.entities.Camera`` table columns and reports
    whether any main/sub profile columns exist. Expected result is
    ``single_source=True`` with no ``main_*``/``sub_*`` profile fields.
    """
    try:
        from app.models.entities import Camera as _Camera
    except Exception as exc:  # noqa: BLE001
        return {"single_source": None, "error": str(exc)}
    try:
        cols = [c.name for c in _Camera.__table__.columns]
    except Exception:
        cols = []
    lowered = [c.lower() for c in cols]
    main_like = [c for c in lowered if c in ("main_url", "main_stream", "mainstream", "main_source", "sub_url", "sub_stream", "substream", "sub_source", "stream_profiles", "profiles")]
    has_source = "source" in lowered
    return {
        "single_source": bool(has_source and not main_like),
        "source_field": "source" if has_source else None,
        "columns": cols,
        "has_main_sub_profile_columns": bool(main_like),
        "main_sub_like_columns": main_like,
    }


def _default_roles_for(name: str) -> List[str]:
    # Frigate/Shinobi convention: high-res main records, low-res sub detects.
    if name == PROFILE_MAIN:
        return ["record", "live", "snapshot"]
    return ["detect", "live"]


def _to_public(camera_key: str, profile: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(profile)
    # Back-compat aliases for URL field.
    url = out.get("url")
    for alias in ("path", "stream_url", "src", "address", "uri"):
        out.setdefault(alias, url)
    out.setdefault("camera_id", out.get("camera_id"))
    out.setdefault("name", out.get("name"))
    out.setdefault("profile", out.get("name"))
    return out


def _ensure_camera_entry(camera_key: str) -> Dict[str, Dict[str, Any]]:
    return _STORE.setdefault(str(camera_key), {})


def set_profile(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Create or replace a ``main``/``sub`` profile for a camera.

    Accepts flexible inputs, e.g.::

        set_profile(camera_id=1, name="main", url="rtsp://x/high")
        set_profile(1, "sub", "rtsp://x/low", roles=["detect"])
        set_profile({"camera_id": 1, "name": "main", "path": "rtsp://..."})
    """
    flat: Dict[str, Any] = {}
    positional: List[Any] = []
    for a in args:
        if isinstance(a, dict):
            flat.update(a)
        elif hasattr(a, "model_dump"):
            try:
                flat.update(dict(a.model_dump()))
            except Exception:
                pass
        else:
            positional.append(a)
    flat.update(kwargs or {})
    for nested in ("profile", "config", "params", "data", "options", "payload"):
        n = flat.get(nested)
        if isinstance(n, dict):
            for k, v in n.items():
                flat.setdefault(k, v)

    # Positional conveniences: (camera_id, name, url, roles)
    idx = 0
    if "camera_id" not in flat and "camera" not in flat and "cam" not in flat and idx < len(positional):
        flat["camera_id"] = positional[idx]
        idx += 1
    if "name" not in flat and "profile" not in flat and idx < len(positional):
        flat["name"] = positional[idx]
        idx += 1
    if "url" not in flat and "path" not in flat and idx < len(positional):
        flat["url"] = positional[idx]
        idx += 1
    if "roles" not in flat and idx < len(positional):
        flat["roles"] = positional[idx]
        idx += 1

    raw_cam = flat.get("camera_id", flat.get("camera", flat.get("cam", flat.get("cameraId"))))
    camera_key = _normalize_camera_key(raw_cam)
    if camera_key is None:
        raise ValueError("camera_id is required")
    try:
        camera_id: Any = int(camera_key)
    except (TypeError, ValueError):
        camera_id = camera_key

    raw_name = flat.get("name", flat.get("profile", flat.get("profile_name", flat.get("profileName"))))
    name = _normalize_profile_name(raw_name)
    if name is None:
        raise ValueError("profile name must be one of 'main' | 'sub'")

    raw_url = flat.get("url", flat.get("path", flat.get("stream_url", flat.get("src", flat.get("address", flat.get("uri"))))))
    url = _normalize_url(raw_url)
    if url is None:
        raise ValueError("url/path is required")

    raw_roles = flat.get("roles", flat.get("role", flat.get("input_roles")))
    roles = _normalize_roles(raw_roles)
    if roles is None:
        roles = list(_default_roles_for(name))
    # Validate role names strictly when caller supplied something non-empty.
    if raw_roles is not None and (isinstance(raw_roles, str) and str(raw_roles).strip() or isinstance(raw_roles, (list, tuple, set)) and len(list(raw_roles))):
        supplied = [str(v).strip().lower() for v in (raw_roles.replace(",", " ").split() if isinstance(raw_roles, str) else list(raw_roles))]
        supplied = [ROLE_ALIASES.get(s, s) for s in supplied if s]
        unknown = [s for s in supplied if s not in VALID_ROLES]
        if unknown:
            unknown_str = ", ".join(sorted(set(unknown)))
            valid_str = ", ".join(VALID_ROLES)
            raise ValueError(f"unknown role(s): {unknown_str}. valid: {valid_str}")

    enabled_raw = flat.get("enabled", flat.get("active", flat.get("is_enabled", True)))
    enabled = bool(enabled_raw) if not isinstance(enabled_raw, str) else enabled_raw.strip().lower() not in ("0", "false", "no", "off", "")

    # Shinobi-style stream/record/detector knobs (optional, stored verbatim).
    optional_keys = (
        "stream_type", "stream_vcodec", "stream_acodec", "stream_fps",
        "stream_scale_x", "stream_scale_y", "width", "height", "fps",
        "vcodec", "acodec", "codec", "snap_fps", "detector_fps",
        "transport", "rtsp_transport", "username", "password",
        "description", "notes",
    )
    with _lock:
        entry = _ensure_camera_entry(camera_key)
        # Frigate rule: each role may be used only once per camera.
        for other_name, other in entry.items():
            if other_name == name:
                continue
            overlap = set(other.get("roles", [])) & set(roles)
            if overlap:
                raise ValueError(
                    f"role(s) {sorted(overlap)} already assigned to '{other_name}' profile; each role may be used only once"
                )
        now = _now_iso()
        existing = entry.get(name, {})
        profile: Dict[str, Any] = {
            "camera_id": camera_id,
            "cameraId": camera_id,
            "camera": camera_id,
            "name": name,
            "profile": name,
            "url": url,
            "path": url,
            "stream_url": url,
            "src": url,
            "roles": list(roles),
            "enabled": enabled,
            "created_at": existing.get("created_at", now),
            "updated_at": now,
        }
        for k in optional_keys:
            if k in flat and flat[k] is not None:
                profile[k] = flat[k]
        # Preserve unknown extras.
        for k, v in flat.items():
            if k not in profile and k.lower() not in ("camera_id", "camera", "cam", "cameraid", "name", "profile", "url", "path", "roles", "role"):
                profile[k] = v
        entry[name] = profile
        return _to_public(camera_key, profile)


def get_profile(camera_id: Any = None, name: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    flat = _flatten(args[0]) if args and isinstance(args[0], dict) else {}
    flat.update(kwargs or {})
    if camera_id is None:
        camera_id = flat.get("camera_id", flat.get("camera", flat.get("cam")))
    if name is None:
        name = flat.get("name", flat.get("profile"))
    if camera_id is None or name is None:
        # Allow set_profile-style single dict.
        if isinstance(camera_id, dict):
            return get_profile(**camera_id)
        return None
    camera_key = _normalize_camera_key(camera_id)
    pname = _normalize_profile_name(name)
    if camera_key is None or pname is None:
        return None
    with _lock:
        entry = _STORE.get(str(camera_key), {})
        prof = entry.get(pname)
        return _to_public(camera_key, prof) if prof is not None else None


def delete_profile(camera_id: Any = None, name: Any = None, *args: Any, **kwargs: Any) -> bool:
    flat = _flatten(args[0]) if args and isinstance(args[0], dict) else {}
    flat.update(kwargs or {})
    if camera_id is None:
        camera_id = flat.get("camera_id", flat.get("camera", flat.get("cam")))
    if name is None:
        name = flat.get("name", flat.get("profile"))
    camera_key = _normalize_camera_key(camera_id)
    pname = _normalize_profile_name(name)
    if camera_key is None or pname is None:
        return False
    with _lock:
        entry = _STORE.get(str(camera_key))
        if not entry or pname not in entry:
            return False
        del entry[pname]
        if not entry:
            _STORE.pop(str(camera_key), None)
        return True


def get_stream_profiles(camera_id: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Return all profiles plus the effective role mapping for a camera."""
    flat = _flatten(args[0]) if args and isinstance(args[0], dict) else {}
    flat.update(kwargs or {})
    if camera_id is None:
        camera_id = flat.get("camera_id", flat.get("camera", flat.get("cam")))
        if camera_id is None and args and not isinstance(args[0], dict):
            camera_id = args[0]
    camera_key = _normalize_camera_key(camera_id)
    if camera_key is None:
        raise ValueError("camera_id is required")
    try:
        cid: Any = int(camera_key)
    except (TypeError, ValueError):
        cid = camera_key
    with _lock:
        entry = dict(_STORE.get(str(camera_key), {}))
        profiles = {k: _to_public(camera_key, v) for k, v in entry.items()}
    roles = resolve_roles(camera_id=camera_key)
    return {
        "camera_id": cid,
        "profiles": profiles,
        "main": profiles.get("main"),
        "sub": profiles.get("sub"),
        "roles": roles,
        "single_source_fallback": len(profiles) == 0,
    }


def list_stream_profiles(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    flat = _flatten(args[0]) if args and isinstance(args[0], dict) else {}
    flat.update(kwargs or {})
    with _lock:
        out: List[Dict[str, Any]] = []
        for camera_key, entry in _STORE.items():
            for pname, prof in entry.items():
                item = _to_public(camera_key, prof)
                if flat:
                    # Optional filter by camera_id.
                    raw = flat.get("camera_id", flat.get("camera", flat.get("cam")))
                    if raw is not None and _normalize_camera_key(raw) != str(camera_key):
                        continue
                out.append(item)
        return sorted(out, key=lambda d: (str(d.get("camera_id")), str(d.get("name"))))


def resolve_roles(camera_id: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Optional[str]]:
    """Map each known role to the profile name serving it (Frigate-style)."""
    flat = _flatten(args[0]) if args and isinstance(args[0], dict) else {}
    flat.update(kwargs or {})
    if camera_id is None:
        camera_id = flat.get("camera_id", flat.get("camera", flat.get("cam")))
        if camera_id is None and args and not isinstance(args[0], dict):
            camera_id = args[0]
    camera_key = _normalize_camera_key(camera_id)
    mapping: Dict[str, Optional[str]] = {r: None for r in VALID_ROLES}
    if camera_key is None:
        return mapping
    with _lock:
        entry = _STORE.get(str(camera_key), {})
        for pname, prof in entry.items():
            if not prof.get("enabled", True):
                continue
            for r in prof.get("roles", []) or []:
                if r in mapping and mapping[r] is None:
                    mapping[r] = pname
    return mapping


def profile_for_role(camera_id: Any, role: Any, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    r = _normalize_role(role)
    if r is None:
        return None
    camera_key = _normalize_camera_key(camera_id)
    if camera_key is None:
        return None
    with _lock:
        entry = _STORE.get(str(camera_key), {})
        for prof in entry.values():
            if prof.get("enabled", True) and r in (prof.get("roles") or []):
                return _to_public(camera_key, prof)
    return None


def verify_roles(camera_id: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Frigate ``verify_config_roles`` equivalent for one camera.

    Returns ``{"ok": bool, "issues": [...], "roles": {...}}``. Rules:
    each role used at most once (enforced on write, re-checked here) and a
    ``detect`` role should exist once profiles are configured so motion/AI
    has a low-res feed. ``record``/``record_sub`` presence is reported but
    only a warning because VigilCam recording can fall back to the single
    camera ``source``.
    """
    flat = _flatten(args[0]) if args and isinstance(args[0], dict) else {}
    flat.update(kwargs or {})
    if camera_id is None:
        camera_id = flat.get("camera_id", flat.get("camera", flat.get("cam")))
        if camera_id is None and args and not isinstance(args[0], dict):
            camera_id = args[0]
    camera_key = _normalize_camera_key(camera_id)
    roles = resolve_roles(camera_key)
    issues: List[str] = []
    if camera_key is None:
        return {"ok": False, "issues": ["camera_id is required"], "roles": roles}
    with _lock:
        entry = _STORE.get(str(camera_key), {})
        if not entry:
            return {"ok": True, "issues": [], "roles": roles, "note": "no profiles; single camera source serves all roles"}
        seen: Dict[str, str] = {}
        for pname, prof in entry.items():
            for r in prof.get("roles") or []:
                if r in seen:
                    issues.append(f"role '{r}' assigned to both '{seen[r]}' and '{pname}'")
                else:
                    seen[r] = pname
        if "detect" not in seen:
            issues.append("detect role is required when stream profiles are configured (assign it to the sub profile)")
    warnings: List[str] = []
    if "record" not in roles and "record_sub" not in roles:
        warnings.append("no record/record_sub role; recording falls back to the single camera source")
    return {"ok": len(issues) == 0, "issues": issues, "warnings": warnings, "roles": roles}


def build_ffmpeg_inputs(camera_id: Any = None, fallback_source: Any = None, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """Return Frigate-style ``[{path, roles}]`` inputs for a camera."""
    flat = _flatten(args[0]) if args and isinstance(args[0], dict) else {}
    flat.update(kwargs or {})
    if camera_id is None:
        camera_id = flat.get("camera_id", flat.get("camera", flat.get("cam")))
    if fallback_source is None:
        fallback_source = flat.get("fallback_source", flat.get("source", flat.get("main_url")))
    camera_key = _normalize_camera_key(camera_id)
    inputs: List[Dict[str, Any]] = []
    if camera_key is not None:
        with _lock:
            entry = dict(_STORE.get(str(camera_key), {}))
        for pname in (PROFILE_MAIN, PROFILE_SUB):
            prof = entry.get(pname)
            if prof is None or not prof.get("enabled", True):
                continue
            roles = [r for r in (prof.get("roles") or []) if r in ("detect", "record", "record_sub", "audio")]
            if not roles:
                continue
            inputs.append({"path": prof.get("url"), "roles": roles, "profile": pname})
        # Anything else (live/snapshot-only profiles) is not an ffmpeg record/detect input.
    if not inputs and fallback_source:
        s = _normalize_url(fallback_source)
        if s:
            inputs.append({"path": s, "roles": ["detect", "record"], "profile": "main"})
    return inputs


def ingestion_urls(camera_id: Any = None, fallback_source: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Return ``(main_url, sub_url)`` for ``StreamCopyIngestor.start_camera``."""
    flat = _flatten(args[0]) if args and isinstance(args[0], dict) else {}
    flat.update(kwargs or {})
    if camera_id is None:
        camera_id = flat.get("camera_id", flat.get("camera", flat.get("cam")))
    if fallback_source is None:
        fallback_source = flat.get("fallback_source", flat.get("source", flat.get("main_url", flat.get("url"))))
    camera_key = _normalize_camera_key(camera_id)
    main_url = _normalize_url(fallback_source)
    sub_url: Optional[str] = None
    if camera_key is not None:
        with _lock:
            entry = _STORE.get(str(camera_key), {})
            main_prof = entry.get(PROFILE_MAIN)
            sub_prof = entry.get(PROFILE_SUB)
        if main_prof is not None and main_prof.get("enabled", True):
            main_url = _normalize_url(main_prof.get("url")) or main_url
        if sub_prof is not None and sub_prof.get("enabled", True):
            sub_url = _normalize_url(sub_prof.get("url"))
    return {"main_url": main_url, "sub_url": sub_url}


def set_stream_profiles(camera_id: Any = None, profiles: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Bulk upsert ``{"main": {...}, "sub": {...}}`` for a camera."""
    flat: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            # Heuristic: a dict with main/sub keys is the profiles map.
            if "main" in a or "sub" in a:
                profiles = a if profiles is None else profiles
            else:
                flat.update(a)
    flat.update(kwargs or {})
    if camera_id is None:
        camera_id = flat.pop("camera_id", flat.pop("camera", flat.pop("cam", None) if "cam" in flat else None))
    if profiles is None:
        profiles = flat.pop("profiles", None)
        if profiles is None:
            # Remaining flat may itself be {main: {...}, sub: {...}}.
            if "main" in flat or "sub" in flat:
                profiles = {"main": flat.get("main"), "sub": flat.get("sub")}
            else:
                profiles = flat
    if not isinstance(profiles, dict):
        raise ValueError("profiles must be a dict like {'main': {...}, 'sub': {...}}")
    # Two-phase: validate all first so a duplicate-role bulk write is atomic.
    normalized: Dict[str, Dict[str, Any]] = {}
    for key, val in profiles.items():
        pname = _normalize_profile_name(key)
        if pname is None:
            raise ValueError(f"unknown profile '{key}'; expected 'main' | 'sub'")
        if val is None:
            continue
        if isinstance(val, str):
            val = {"url": val}
        if not isinstance(val, dict):
            raise ValueError(f"profile '{pname}' must be a dict or URL string")
        merged = dict(val)
        merged["camera_id"] = camera_id
        merged["name"] = pname
        # Pre-validate roles/urls without writing.
        url = _normalize_url(merged.get("url", merged.get("path", merged.get("stream_url", merged.get("src")))))
        if url is None:
            raise ValueError(f"profile '{pname}' requires url/path")
        roles = _normalize_roles(merged.get("roles", merged.get("role")))
        if roles is None:
            roles = list(_default_roles_for(pname))
        merged["_norm_roles"] = roles
        merged["_norm_url"] = url
        normalized[pname] = merged
    # Cross-profile duplicate-role check.
    seen: Dict[str, str] = {}
    for pname, merged in normalized.items():
        for r in merged["_norm_roles"]:
            if r in seen:
                raise ValueError(
                    f"role(s) ['{r}'] already assigned to '{seen[r]}' profile; each role may be used only once"
                )
            seen[r] = pname
    # Also check against existing profiles not being overwritten.
    camera_key = _normalize_camera_key(camera_id)
    if camera_key is None:
        raise ValueError("camera_id is required")
    with _lock:
        existing = _STORE.get(str(camera_key), {})
        for pname, merged in normalized.items():
            for other_name, other in existing.items():
                if other_name in normalized:
                    continue
                overlap = set(other.get("roles", [])) & set(merged["_norm_roles"])
                if overlap:
                    raise ValueError(
                        f"role(s) {sorted(overlap)} already assigned to '{other_name}' profile; each role may be used only once"
                    )
    out_profiles: Dict[str, Any] = {}
    for pname, merged in normalized.items():
        roles = merged.pop("_norm_roles")
        merged.pop("_norm_url", None)
        merged["roles"] = roles
        out_profiles[pname] = set_profile(merged)
    return get_stream_profiles(camera_key)


def delete_profiles_for_camera(camera_id: Any = None, *args: Any, **kwargs: Any) -> int:
    flat = _flatten(args[0]) if args and isinstance(args[0], dict) else {}
    flat.update(kwargs or {})
    if camera_id is None:
        camera_id = flat.get("camera_id", flat.get("camera", flat.get("cam")))
        if camera_id is None and args and not isinstance(args[0], dict):
            camera_id = args[0]
    camera_key = _normalize_camera_key(camera_id)
    if camera_key is None:
        return 0
    with _lock:
        entry = _STORE.pop(str(camera_key), {})
        return len(entry)


def clear(*args: Any, **kwargs: Any) -> int:
    with _lock:
        n = sum(len(v) for v in _STORE.values())
        _STORE.clear()
        return n


def count_profiles(camera_id: Any = None, *args: Any, **kwargs: Any) -> int:
    if camera_id is not None or args or kwargs:
        try:
            data = get_stream_profiles(camera_id, *args, **kwargs)
            return len(data.get("profiles", {}))
        except ValueError:
            return 0
    with _lock:
        return sum(len(v) for v in _STORE.values())


# --- Async entrypoints (db-optional, ignored) --------------------------------

async def set_profile_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> Dict[str, Any]:
    kwargs.pop("db", None)
    kwargs.pop("session", None)
    return set_profile(*args, **kwargs)


async def get_profile_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return get_profile(*args, **kwargs)


async def get_stream_profiles_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return get_stream_profiles(*args, **kwargs)


async def delete_profile_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> bool:
    return delete_profile(*args, **kwargs)


class StreamProfilesService:
    """Object-oriented facade over the module-level store."""

    def set(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return set_profile(*args, **kwargs)

    def get(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        # get(camera_id, name) or get_stream_profiles(camera_id) dispatch.
        if len(args) >= 2 or "name" in kwargs or "profile" in kwargs:
            got = get_profile(*args, **kwargs)
            if got is not None:
                return got
        try:
            data = get_stream_profiles(*args, **kwargs)
            if isinstance(data, dict) and "profiles" in data:
                return data  # type: ignore[return-value]
        except (TypeError, ValueError):
            pass
        return get_profile(*args, **kwargs)

    def list(self, *args: Any, **kwargs: Any) -> Any:
        if args or kwargs:
            try:
                return get_stream_profiles(*args, **kwargs)
            except (TypeError, ValueError):
                pass
        return list_stream_profiles(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> bool:
        return delete_profile(*args, **kwargs)

    def clear(self, *args: Any, **kwargs: Any) -> int:
        return clear(*args, **kwargs)

    def roles(self, *args: Any, **kwargs: Any) -> Dict[str, Optional[str]]:
        return resolve_roles(*args, **kwargs)

    def verify(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return verify_roles(*args, **kwargs)

    def ffmpeg_inputs(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return build_ffmpeg_inputs(*args, **kwargs)

    # aliases
    set_profile = set
    upsert = set
    save = set
    put = set
    get_profile = get
    get_profiles = list
    list_profiles = list
    delete_profile = delete
    remove = delete
    clear_profiles = clear
    resolve = roles
    check = verify


stream_profiles_service = StreamProfilesService()
stream_profile_service = stream_profiles_service
profiles_service = stream_profiles_service
service = stream_profiles_service
manager = stream_profiles_service
store = stream_profiles_service


# Compatibility aliases (function level).
create_profile = set_profile
add_profile = set_profile
upsert_profile = set_profile
save_profile = set_profile
put_profile = set_profile
update_profile = set_profile

fetch_profile = get_profile
find_profile = get_profile
get_stream_profile = get_profile

fetch_profiles = get_stream_profiles
find_profiles = get_stream_profiles
list_profiles = list_stream_profiles
get_profiles = list_stream_profiles
get_all_profiles = list_stream_profiles
query_profiles = list_stream_profiles

remove_profile = delete_profile
clear_profiles = clear
reset = clear
purge = clear

resolve_role_mapping = resolve_roles
get_roles = resolve_roles
effective_roles = resolve_roles
role_mapping = resolve_roles

check_roles = verify_roles
validate_roles = verify_roles

ffmpeg_inputs = build_ffmpeg_inputs
get_ffmpeg_inputs = build_ffmpeg_inputs

get_ingestion_urls = ingestion_urls
resolve_ingestion_urls = ingestion_urls

bulk_set = set_stream_profiles
set_profiles = set_stream_profiles
put_profiles = set_stream_profiles


__all__ = [
    "PROFILE_MAIN",
    "PROFILE_SUB",
    "VALID_PROFILES",
    "VALID_ROLES",
    "ROLE_ALIASES",
    "verify_single_source_camera_model",
    "set_profile",
    "get_profile",
    "delete_profile",
    "get_stream_profiles",
    "list_stream_profiles",
    "resolve_roles",
    "profile_for_role",
    "verify_roles",
    "build_ffmpeg_inputs",
    "ingestion_urls",
    "set_stream_profiles",
    "delete_profiles_for_camera",
    "clear",
    "count_profiles",
    "StreamProfilesService",
    "stream_profiles_service",
]
