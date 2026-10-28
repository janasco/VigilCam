"""ONVIF snapshot-URI resolver service for VigilCam.

Verified gap (Oct 2026): single-frame capture (``app.services.snapshot_now``)
only honours a manually supplied ``source`` (``source``/``stream_url``/
``rtsp_url``/``url``/``onvif_host`` DB columns) and falls back to a
placeholder JPEG; the ONVIF stub (``app.services.onvif``) only suggests an
RTSP URL and no code path issues ``GetSnapshotUri`` (only ``GetStreamUri``
exists in ``onvif_digest_service``). So there is no ONVIF snapshot-URI
auto-configuration — this module fills that gap.

It resolves a camera's still-image URL via ONVIF media
``GetSnapshotUri`` (Digest with WS-Security retry, via
``onvif_digest_service._onvif_request``) and keeps a per-camera in-memory
store so later snapshot captures can reuse the URI without a manual URL.
No ``app/main.py`` edit is needed; API lives in ``app.api.snapshot_uri``.

Conventions mirror ``app.services.imaging``: stdlib + ``onvif_digest``
transport, prefix-tolerant SOAP parsing, graceful ``HTTPException``
mapping, many alias names.
"""

from __future__ import annotations

import html
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

try:  # Preferred transport; absence is a 503, not a crash.
    from app.services import onvif_digest_service as _ods

    _ODS_AVAILABLE = True
except Exception:  # noqa: BLE001
    _ods = None  # type: ignore[assignment]
    _ODS_AVAILABLE = False


# ---------------------------------------------------------------------------
# Per-camera store (process-local, thread-safe).
# ---------------------------------------------------------------------------

_STORE: Dict[str, Dict[str, Any]] = {}
_LOCK = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coerce_camera_id(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _store_key(camera_id: Any = None, ip: Any = None, profile_token: Any = None) -> str:
    cid = _coerce_camera_id(camera_id)
    if cid is not None:
        return "camera:{}".format(cid)
    host = str(ip or "").strip().lower()
    prof = str(profile_token or "").strip()
    if host:
        return "host:{}:{}".format(host, prof) if prof else "host:{}".format(host)
    if prof:
        return "profile:{}".format(prof)
    return "default"


def _esc(s: Any) -> str:
    return html.escape(str(s or ""), quote=True)


def _xml_text(raw: str) -> str:
    return html.unescape(raw or "")


def _require_ods():
    if not _ODS_AVAILABLE or _ods is None:
        raise HTTPException(status_code=503, detail="ONVIF snapshot-uri service unavailable")
    return _ods


# ---------------------------------------------------------------------------
# SOAP helpers
# ---------------------------------------------------------------------------

_URI_RE = re.compile(r"<(?:\w+:)?Uri>([^<]+)</(?:\w+:)?Uri>", re.IGNORECASE)


def parse_snapshot_uri(text: str) -> Optional[str]:
    """Extract the snapshot ``<Uri>`` from a GetSnapshotUri response."""
    if not text:
        return None
    m = _URI_RE.search(text)
    if not m:
        return None
    uri = _xml_text(m.group(1).strip())
    return uri or None


def _check_ok(status: int, text: str, op: str = "GetSnapshotUri") -> None:
    if status == 200 and "Fault>" not in (text or ""):
        return
    reason: Optional[str] = None
    m = re.search(r"<[^>]*Text[^>]*>([^<]+)</[^>]*Text>", text or "")
    if m:
        reason = m.group(1).strip()
    if "Fault>" in (text or ""):
        raise HTTPException(status_code=502, detail="{} failed: {}".format(op, reason or "ONVIF fault"))
    raise HTTPException(status_code=status if status not in (0, None) else 502,
                        detail="{} failed: {}".format(op, reason or (text or "")[:200]))


async def _media_service_url(
    ip: str, username: str, password: str, port: int = 80, scheme: str = "http"
) -> str:
    """Resolve the media XAddr, falling back to the conventional path."""
    ods = _require_ods()
    fallback = "{}://{}:{}/onvif/Media".format(scheme, ip, port)
    try:
        services = await ods.get_services(ip, username, password, port, scheme)
        if isinstance(services, dict) and services.get("media"):
            return str(services["media"])
    except Exception:  # noqa: BLE001
        pass
    try:
        caps = await ods.get_capabilities(ip, username, password, port, scheme)
        if isinstance(caps, dict) and caps.get("media"):
            return str(caps["media"])
    except Exception:  # noqa: BLE001
        pass
    return fallback


# ---------------------------------------------------------------------------
# Core: GetSnapshotUri via ONVIF
# ---------------------------------------------------------------------------


async def get_snapshot_uri_digest(
    ip: str,
    username: str,
    password: str,
    profile_token: str,
    port: int = 80,
    scheme: str = "http",
) -> str:
    """Return the ONVIF snapshot URI for a media profile.

    Issues ``GetSnapshotUri`` against the media service. Never returns raw
    socket/XML tracebacks — transport/auth failures surface as
    ``HTTPException`` (401/502/503).
    """
    if not str(ip or "").strip():
        raise HTTPException(status_code=422, detail="ip/host is required")
    if not str(profile_token or "").strip():
        raise HTTPException(status_code=422, detail="profile_token is required")
    ods = _require_ods()
    native = getattr(ods, "get_snapshot_uri_digest", None)
    if callable(native):
        # Prefer a native helper if a newer digest service provides one.
        try:
            result = await native(str(ip), str(username or ""), str(password or ""),
                                  str(profile_token), int(port or 80), str(scheme or "http"))
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail="GetSnapshotUri failed: {}".format(exc))
        if isinstance(result, dict):
            for key in ("snapshot_uri", "snapshotUri", "uri", "url"):
                if result.get(key):
                    return str(result[key])
            raise HTTPException(status_code=502, detail="No snapshot URI in response")
        if isinstance(result, str) and result.strip():
            return result.strip()
        raise HTTPException(status_code=502, detail="No snapshot URI in response")

    media_url = await _media_service_url(str(ip), str(username or ""), str(password or ""),
                                         int(port or 80), str(scheme or "http"))
    body = (
        "<trt:GetSnapshotUri>"
        "<trt:ProfileToken>{}</trt:ProfileToken>"
        "</trt:GetSnapshotUri>"
    ).format(_esc(profile_token))
    try:
        status, text = await ods._onvif_request(media_url, body, username, password)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail="GetSnapshotUri failed: {}".format(exc))
    if status == 401:
        raise HTTPException(status_code=401, detail="Authentication failed - check username/password")
    if status == 404:
        # Retry on the alternate conventional path (Media vs media_service).
        alt = "{}://{}:{}/onvif/media_service".format(scheme, ip, port)
        if alt != media_url:
            try:
                status, text = await ods._onvif_request(alt, body, username, password)
            except HTTPException:
                raise
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=502, detail="GetSnapshotUri failed: {}".format(exc))
    if status != 200 or "Fault>" in (text or ""):
        _check_ok(status, text or "")
    uri = parse_snapshot_uri(text or "")
    if not uri:
        raise HTTPException(status_code=502, detail="No snapshot URI in response")
    return uri


async def _first_profile_token(ip: str, username: str, password: str, port: int, scheme: str) -> Optional[str]:
    ods = _require_ods()
    try:
        profiles = await ods.fetch_profiles_digest(ip, username, password, port, scheme)
    except Exception:  # noqa: BLE001
        return None
    if isinstance(profiles, list):
        for p in profiles:
            if isinstance(p, dict) and p.get("token"):
                return str(p["token"])
    return None


def _camera_value(camera: Any, key: str, fallback: Any = None) -> Any:
    if camera is None:
        return fallback
    if isinstance(camera, dict):
        return camera.get(key, fallback)
    return getattr(camera, key, fallback)


async def _maybe_load_camera(db: Any, camera: Any, camera_id: Any) -> Any:
    if camera is not None and not isinstance(camera, (int, str)):
        return camera
    cid = camera_id
    if cid is None and isinstance(camera, (int, str)):
        try:
            int(str(camera).strip())
            cid = camera
            camera = None
        except (TypeError, ValueError):
            return None
    if cid is None or db is None:
        return camera
    try:
        coerced = int(str(cid).strip())
    except (TypeError, ValueError):
        return camera
    try:
        from app.models.entities import Camera

        if hasattr(db, "get"):
            loaded = await db.get(Camera, coerced)
            if loaded is not None:
                return loaded
    except Exception:  # noqa: BLE001
        pass
    return camera


async def resolve_snapshot_uri(
    camera_id: Any = None,
    ip: Any = None,
    username: Any = None,
    password: Any = None,
    port: Any = None,
    scheme: Any = None,
    profile_token: Any = None,
    db: Any = None,
    camera: Any = None,
    store: bool = True,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Resolve a camera's ONVIF snapshot URI and (optionally) store it.

    Accepts common aliases (``host``/``onvif_host``, ``user``/``pass``,
    ``onvif_port``/``control_scheme``, ``profileToken``/``profile``). When
    ``camera``/``camera_id`` + ``db`` is given, missing connection fields are
    filled from the ``Camera`` row. When ``profile_token`` is omitted, the
    first ONVIF media profile is used.
    """
    # Alias normalisation.
    if camera_id is None:
        for key in ("camera", "cam", "id", "cameraId", "cameraID", "device_id", "source_id"):
            if kwargs.get(key) is not None:
                camera_id = kwargs.get(key)
                break
    if camera is None:
        camera = kwargs.get("camera_obj", kwargs.get("camera_row"))
    if ip is None:
        for key in ("host", "onvif_host", "hostname", "ip_address", "ipAddress"):
            val = kwargs.get(key)
            if val is not None and str(val).strip():
                ip = val
                break
    if username is None:
        for key in ("user", "username", "Username", "login"):
            if kwargs.get(key) is not None:
                username = kwargs.get(key)
                break
    if password is None:
        for key in ("pass", "password", "Password", "pwd"):
            if kwargs.get(key) is not None:
                password = kwargs.get(key)
                break
    if port is None:
        for key in ("onvif_port", "control_port", "onvifPort"):
            if kwargs.get(key) is not None:
                port = kwargs.get(key)
                break
    if scheme is None:
        for key in ("control_scheme", "onvif_scheme", "protocol"):
            if kwargs.get(key) is not None:
                scheme = kwargs.get(key)
                break
    if profile_token is None:
        for key in ("profileToken", "profile", "ProfileToken", "token", "media_profile"):
            if kwargs.get(key) is not None:
                profile_token = kwargs.get(key)
                break

    camera = await _maybe_load_camera(db, camera if camera is not None else camera_id, camera_id)
    if camera is not None and not isinstance(camera, (int, str)):
        if ip is None:
            ip = _camera_value(camera, "ip_address") or _camera_value(camera, "onvif_host")
            if not ip:
                # Derive from stream source (rtsp://user@host/...) like onboarding does.
                try:
                    from app.services.camera_onboarding import derive_ip_address as _derive

                    ip = _derive(_camera_value(camera, "source"), _camera_value(camera, "onvif_host"))
                except Exception:  # noqa: BLE001
                    pass
        if username is None:
            username = _camera_value(camera, "username")
        if password is None:
            password = _camera_value(camera, "password")
        if port is None:
            port = _camera_value(camera, "onvif_port")
        if scheme is None:
            scheme = _camera_value(camera, "control_scheme")

    host = str(ip or "").strip()
    if not host:
        raise HTTPException(status_code=422, detail="ip/host is required (or camera_id with a known camera)")
    user_s = str(username or "")
    pass_s = str(password or "")
    try:
        port_i = int(port) if port is not None and str(port).strip() != "" else 80
    except (TypeError, ValueError):
        port_i = 80
    scheme_s = str(scheme or "http").strip().lower() or "http"
    if scheme_s not in ("http", "https"):
        scheme_s = "http"

    ods = _require_ods()
    # Resolve the real control endpoint (port/scheme scan) when available.
    try:
        resolve = getattr(ods, "resolve_control_endpoint", None)
        if callable(resolve):
            scheme_s, port_i = await resolve(host, port_i, scheme_s)
    except Exception:  # noqa: BLE001
        pass

    profile_s = str(profile_token or "").strip() or None
    if not profile_s:
        profile_s = await _first_profile_token(host, user_s, pass_s, port_i, scheme_s)
    if not profile_s:
        raise HTTPException(status_code=502, detail="No ONVIF media profile available for GetSnapshotUri")

    uri = await get_snapshot_uri_digest(host, user_s, pass_s, profile_s, port_i, scheme_s)

    record: Dict[str, Any] = {
        "camera_id": _coerce_camera_id(camera_id),
        "ip": host,
        "host": host,
        "port": port_i,
        "scheme": scheme_s,
        "profile_token": profile_s,
        "profileToken": profile_s,
        "snapshot_uri": uri,
        "snapshotUri": uri,
        "uri": uri,
        "url": uri,
        "source": "onvif",
        "resolved_at": _now_iso(),
        "ok": True,
        "success": True,
        "status": "resolved",
    }
    if store:
        try:
            store_snapshot_uri(camera_id=camera_id if _coerce_camera_id(camera_id) is not None else host,
                               snapshot_uri=uri, profile_token=profile_s,
                               ip=host, port=port_i, scheme=scheme_s)
        except Exception:  # noqa: BLE001
            pass
    return record


# ---------------------------------------------------------------------------
# Store helpers
# ---------------------------------------------------------------------------


def store_snapshot_uri(
    camera_id: Any = None,
    snapshot_uri: Any = None,
    profile_token: Any = None,
    ip: Any = None,
    port: Any = None,
    scheme: Any = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Persist a resolved snapshot URI in the per-camera store."""
    if snapshot_uri is None:
        for key in ("uri", "url", "snapshotUri", "snapshotUrl", "snapshotURL", "value", "href"):
            if kwargs.get(key) is not None:
                snapshot_uri = kwargs.get(key)
                break
    if camera_id is None:
        for key in ("camera", "cam", "id", "key", "target"):
            if kwargs.get(key) is not None:
                camera_id = kwargs.get(key)
                break
    if profile_token is None:
        for key in ("profileToken", "profile", "token"):
            if kwargs.get(key) is not None:
                profile_token = kwargs.get(key)
                break
    if ip is None:
        for key in ("host", "hostname", "onvif_host", "ip_address"):
            if kwargs.get(key) is not None:
                ip = kwargs.get(key)
                break
    uri_s = str(snapshot_uri or "").strip()
    if not uri_s:
        raise ValueError("snapshot_uri is required")
    key = _store_key(camera_id, ip, profile_token)
    record: Dict[str, Any] = {
        "key": key,
        "camera_id": _coerce_camera_id(camera_id),
        "snapshot_uri": uri_s,
        "snapshotUri": uri_s,
        "uri": uri_s,
        "url": uri_s,
        "profile_token": str(profile_token or "").strip() or None,
        "ip": str(ip or "").strip() or None,
        "port": port,
        "scheme": scheme,
        "stored_at": _now_iso(),
        "ok": True,
        "success": True,
    }
    with _LOCK:
        _STORE[key] = record
    return dict(record)


def get_stored_snapshot_uri(camera_id: Any = None, **kwargs: Any) -> Optional[Dict[str, Any]]:
    """Return the stored snapshot-URI record for a camera, if any."""
    target = camera_id
    if target is None:
        for key in ("camera", "cam", "id", "key", "target", "ip", "host"):
            if kwargs.get(key) is not None:
                target = kwargs.get(key)
                break
    if target is None:
        return None
    candidates: List[str] = []
    cid = _coerce_camera_id(target)
    if cid is not None:
        candidates.append(_store_key(cid))
        candidates.append(str(cid))
        candidates.append("camera:{}".format(cid))
    s = str(target).strip()
    if s:
        candidates.append(s)
        candidates.append(s.lower())
        candidates.append(_store_key(None, s, kwargs.get("profile_token", kwargs.get("profile"))))
    with _LOCK:
        for cand in candidates:
            if cand in _STORE:
                return dict(_STORE[cand])
            # case-insensitive host lookup
            for k, v in _STORE.items():
                if k.lower() == str(cand).lower():
                    return dict(v)
    return None


def list_stored_snapshot_uris() -> List[Dict[str, Any]]:
    with _LOCK:
        return [dict(_STORE[k]) for k in sorted(_STORE.keys())]


def clear_stored_snapshot_uri(camera_id: Any = None, **kwargs: Any) -> bool:
    rec = get_stored_snapshot_uri(camera_id, **kwargs)
    if rec is None:
        return False
    with _LOCK:
        return _STORE.pop(rec.get("key", ""), None) is not None


def clear_all_snapshot_uris() -> None:
    with _LOCK:
        _STORE.clear()


def snapshot_source_for_camera(camera_id: Any = None, **kwargs: Any) -> Optional[str]:
    """Return the stored snapshot URI string for snapshot capture use."""
    rec = get_stored_snapshot_uri(camera_id, **kwargs)
    if rec is None:
        return None
    return rec.get("snapshot_uri")


# Aliases (repo convention: many names, same behaviour).
get_snapshot_uri = get_snapshot_uri_digest
fetch_snapshot_uri = get_snapshot_uri_digest
fetch_snapshot_uri_digest = get_snapshot_uri_digest
getSnapshotUri = get_snapshot_uri_digest
resolve = resolve_snapshot_uri
resolve_uri = resolve_snapshot_uri
fetch_and_store = resolve_snapshot_uri
resolve_and_store = resolve_snapshot_uri
get_or_resolve = resolve_snapshot_uri
store = store_snapshot_uri
save = store_snapshot_uri
save_snapshot_uri = store_snapshot_uri
put_snapshot_uri = store_snapshot_uri
set_snapshot_uri = store_snapshot_uri
remember_snapshot_uri = store_snapshot_uri
get_stored = get_stored_snapshot_uri
get_for_camera = get_stored_snapshot_uri
get_snapshot_uri_for_camera = get_stored_snapshot_uri
fetch_stored = get_stored_snapshot_uri
lookup = get_stored_snapshot_uri
find = get_stored_snapshot_uri
stored = list_stored_snapshot_uris
list_all = list_stored_snapshot_uris
list_stored = list_stored_snapshot_uris
all_stored = list_stored_snapshot_uris
clear = clear_all_snapshot_uris
clear_all = clear_all_snapshot_uris
reset = clear_all_snapshot_uris
reset_all = clear_all_snapshot_uris
remove = clear_stored_snapshot_uri
delete = clear_stored_snapshot_uri
drop = clear_stored_snapshot_uri
parse_uri = parse_snapshot_uri
extract_uri = parse_snapshot_uri

__all__ = [
    "get_snapshot_uri_digest",
    "resolve_snapshot_uri",
    "parse_snapshot_uri",
    "store_snapshot_uri",
    "get_stored_snapshot_uri",
    "list_stored_snapshot_uris",
    "clear_stored_snapshot_uri",
    "clear_all_snapshot_uris",
    "snapshot_source_for_camera",
]
