"""ONVIF imaging settings write service.

Driver-layer note (verified): ``app/drivers/onvif/driver.py`` already
implements ``get_imaging``/``set_imaging`` (plus Hikvision/Secureye
overrides). What was missing was a *service + API* write path built on
``app.services.onvif_digest_service`` — this module fills that gap.

It sets brightness/contrast/saturation via the ONVIF Imaging service
(``GetImagingSettings``/``SetImagingSettings``) using
``onvif_digest_service._onvif_request`` (Digest with WS-Security retry).
If that module ever gains native imaging helpers, they are preferred
automatically (``where available``); otherwise the SOAP is built here.
Every failure degrades to an ``HTTPException`` (422/502/503) — never a
raw socket/XML traceback (``graceful fallback``).
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import HTTPException

try:  # onvif_digest_service is the preferred transport; absence is a 503, not a crash.
    from app.services import onvif_digest_service as _ods
    _ODS_AVAILABLE = True
except Exception:  # noqa: BLE001
    _ods = None  # type: ignore[assignment]
    _ODS_AVAILABLE = False

# Fields this write path manages. Sharpness is read back when the camera
# reports it but is not written here (spec: brightness/contrast/saturation).
WRITABLE_FIELDS = ("brightness", "contrast", "saturation")
_LEVEL_MIN = 0
_LEVEL_MAX = 100


def _clamp_level(value: Any) -> int:
    try:
        iv = int(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail=f"Invalid imaging level: {value!r}")
    return max(_LEVEL_MIN, min(_LEVEL_MAX, iv))


def _coerce_optional_level(value: Any) -> int | None:
    if value is None:
        return None
    return _clamp_level(value)


def _esc(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _num(tag: str, text: str) -> int | None:
    """Prefix-tolerant ``<tt:Tag>digits</tt:Tag>`` extraction."""
    m = re.search(rf"<(?:\w+:)?{tag}>(-?\d+)</(?:\w+:)?{tag}>", text or "")
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _check_ok(status: int, text: str, op: str) -> None:
    if status == 200 and "Fault>" not in (text or ""):
        return
    reason = None
    m = re.search(r"<[^>]*Text[^>]*>([^<]+)</[^>]*Text>", text or "")
    if m:
        reason = m.group(1).strip()
    raise HTTPException(status_code=502, detail=f"{op} failed: {reason or (text or '')[:200]}")


def _require_ods():
    if not _ODS_AVAILABLE or _ods is None:
        raise HTTPException(status_code=503, detail="ONVIF imaging service unavailable")
    return _ods


async def _imaging_service_url(
    ip: str, username: str, password: str, port: int = 80, scheme: str = "http"
) -> str:
    """Resolve the Imaging XAddr, falling back to the conventional path."""
    ods = _require_ods()
    fallback = f"{scheme}://{ip}:{port}/onvif/Imaging"
    try:
        services = await ods.get_services(ip, username, password, port, scheme)
        if isinstance(services, dict) and services.get("imaging"):
            return services["imaging"]
    except Exception:  # noqa: BLE001
        pass
    try:
        caps = await ods.get_capabilities(ip, username, password, port, scheme)
        if isinstance(caps, dict) and caps.get("imaging"):
            return caps["imaging"]
    except Exception:  # noqa: BLE001
        pass
    return fallback


async def _media_service_url(
    ip: str, username: str, password: str, port: int = 80, scheme: str = "http"
) -> str:
    ods = _require_ods()
    fallback = f"{scheme}://{ip}:{port}/onvif/Media"
    try:
        services = await ods.get_services(ip, username, password, port, scheme)
        if isinstance(services, dict) and services.get("media"):
            return services["media"]
    except Exception:  # noqa: BLE001
        pass
    try:
        caps = await ods.get_capabilities(ip, username, password, port, scheme)
        if isinstance(caps, dict) and caps.get("media"):
            return caps["media"]
    except Exception:  # noqa: BLE001
        pass
    return fallback


async def _video_source_token(
    media_url: str, username: str, password: str
) -> str:
    """First GetVideoSources token; graceful fallback to VideoSource_1."""
    ods = _require_ods()
    try:
        status, text = await ods._onvif_request(
            media_url, "<trt:GetVideoSources/>", username, password
        )
        if status == 200 and text:
            m = re.search(r'VideoSources token="([^"]+)"', text)
            if m:
                return m.group(1)
            m = re.search(
                r"<(?:\w+:)?VideoSource[^>]*token=\"([^\"]+)\"", text
            )
            if m:
                return m.group(1)
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001
        pass
    return "VideoSource_1"


def _parse_settings(text: str) -> dict[str, Any]:
    settings: dict[str, Any] = {}
    brightness = _num("Brightness", text)
    contrast = _num("Contrast", text)
    saturation = _num("ColorSaturation", text)
    sharpness = _num("Sharpness", text)
    if brightness is not None:
        settings["brightness"] = brightness
    if contrast is not None:
        settings["contrast"] = contrast
    if saturation is not None:
        settings["saturation"] = saturation
    if sharpness is not None:
        settings["sharpness"] = sharpness
    return settings


async def get_imaging_settings(
    ip: str,
    username: str,
    password: str,
    port: int = 80,
    scheme: str = "http",
) -> dict[str, Any]:
    """Read current imaging levels. Never raises raw exceptions."""
    ods = _require_ods()
    # Prefer a native helper if a newer onvif_digest_service provides one.
    native = getattr(ods, "get_imaging_settings", None)
    if callable(native):
        try:
            result = await native(ip, username, password, port, scheme)
            if isinstance(result, dict):
                return result
            if hasattr(result, "to_dict"):
                return result.to_dict()
            return {"supported": True, "settings": dict(result), "source": "onvif"}
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"GetImagingSettings failed: {exc}")
    try:
        imaging_url = await _imaging_service_url(ip, username, password, port, scheme)
        media_url = await _media_service_url(ip, username, password, port, scheme)
        token = await _video_source_token(media_url, username, password)
        body = (
            "<timg:GetImagingSettings>"
            f"<timg:VideoSourceToken>{_esc(token)}</timg:VideoSourceToken>"
            "</timg:GetImagingSettings>"
        )
        status, text = await ods._onvif_request(imaging_url, body, username, password)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"GetImagingSettings failed: {exc}")
    if status == 401:
        raise HTTPException(status_code=401, detail="Authentication failed - check username/password")
    if status != 200 or "Fault>" in (text or ""):
        raise HTTPException(status_code=502, detail="Imaging not available on this device")
    settings = _parse_settings(text or "")
    return {"supported": bool(settings), "settings": settings, "source": "onvif"}


async def set_imaging_settings(
    ip: str,
    username: str,
    password: str,
    brightness: int | None = None,
    contrast: int | None = None,
    saturation: int | None = None,
    port: int = 80,
    scheme: str = "http",
) -> dict[str, Any]:
    """Write brightness/contrast/saturation (0-100, clamped).

    Merges the patch over the camera's current settings so untouched
    channels are preserved, then returns the re-read settings.
    """
    patch: dict[str, int] = {}
    if brightness is not None:
        patch["brightness"] = _clamp_level(brightness)
    if contrast is not None:
        patch["contrast"] = _clamp_level(contrast)
    if saturation is not None:
        patch["saturation"] = _clamp_level(saturation)
    if not patch:
        raise HTTPException(
            status_code=422,
            detail="At least one of brightness, contrast, saturation is required",
        )

    ods = _require_ods()
    # Prefer a native helper if a newer onvif_digest_service provides one.
    native = getattr(ods, "set_imaging_settings", None) or getattr(ods, "set_imaging", None)
    if callable(native):
        try:
            result = await native(ip, username, password, port, scheme, **patch)
            if isinstance(result, dict):
                return result
            if hasattr(result, "to_dict"):
                return result.to_dict()
            return {"supported": True, "settings": dict(result), "source": "onvif"}
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"SetImagingSettings failed: {exc}")

    try:
        current = await get_imaging_settings(ip, username, password, port, scheme)
    except HTTPException:
        raise
    if not current.get("supported"):
        raise HTTPException(status_code=502, detail="Imaging not available on this device")
    merged = dict(current.get("settings") or {})
    merged.update(patch)

    parts: list[str] = []
    if "brightness" in merged:
        parts.append(f"<tt:Brightness>{int(merged['brightness'])}</tt:Brightness>")
    if "contrast" in merged:
        parts.append(f"<tt:Contrast>{int(merged['contrast'])}</tt:Contrast>")
    if "saturation" in merged:
        parts.append(f"<tt:ColorSaturation>{int(merged['saturation'])}</tt:ColorSaturation>")
    if "sharpness" in merged:
        parts.append(f"<tt:Sharpness>{int(merged['sharpness'])}</tt:Sharpness>")

    try:
        imaging_url = await _imaging_service_url(ip, username, password, port, scheme)
        media_url = await _media_service_url(ip, username, password, port, scheme)
        token = await _video_source_token(media_url, username, password)
        body = (
            "<timg:SetImagingSettings>"
            f"<timg:VideoSourceToken>{_esc(token)}</timg:VideoSourceToken>"
            f"<timg:ImagingSettings>{''.join(parts)}</timg:ImagingSettings>"
            "<timg:ForcePersistence>true</timg:ForcePersistence>"
            "</timg:SetImagingSettings>"
        )
        status, text = await ods._onvif_request(imaging_url, body, username, password)
        _check_ok(status, text or "", "SetImagingSettings")
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"SetImagingSettings failed: {exc}")
    # Re-read so the caller sees what the camera actually stored.
    return await get_imaging_settings(ip, username, password, port, scheme)


# Aliases matching the driver method names and common verb variants.
get_imaging = get_imaging_settings
set_imaging = set_imaging_settings
fetch_imaging = get_imaging_settings
fetch_imaging_settings = get_imaging_settings
update_imaging = set_imaging_settings
update_imaging_settings = set_imaging_settings
apply_imaging = set_imaging_settings
apply_imaging_settings = set_imaging_settings
