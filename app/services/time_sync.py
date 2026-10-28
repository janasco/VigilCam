"""NTP / camera time-sync status service.

Read-only status over the NVR clock plus ONVIF camera clocks via
:mod:`app.services.onvif_digest_service` where available. When ONVIF is
unavailable (or the camera is unreachable) the service degrades to a
report instead of raising, so status endpoints stay usable offline.

Conventions mirror :mod:`app.services.nat_status`: never-raise status
helpers, ``ok``/``success`` tolerant keys, and a small class alias
surface for caller convenience.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from datetime import UTC, datetime
from typing import Any, Optional

try:
    from app.services import onvif_digest_service as _ods
    HAS_ONVIF = True
except Exception:  # noqa: BLE001
    _ods = None  # type: ignore[assignment]
    HAS_ONVIF = False


DEFAULT_TOLERANCE_SECONDS = 5.0
DEFAULT_TIMEOUT = 10.0


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iso_now() -> str:
    return _utcnow().isoformat()


def _parse_iso(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    s = str(value).strip()
    if not s:
        return None
    try:
        # fromisoformat does not accept trailing Z.
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    except Exception:  # noqa: BLE001
        return None


def _local_timezone_name() -> str:
    try:
        import time as _time

        return _time.tzname[0] if _time.tzname else "UTC"
    except Exception:  # noqa: BLE001
        return "UTC"


def get_nvr_time() -> dict[str, Any]:
    """Return the NVR/host clock reading. Never raises."""
    now = _utcnow()
    return {
        "ok": True,
        "success": True,
        "nvr_time": now.isoformat(),
        "utc_datetime": now.isoformat(),
        "epoch": now.timestamp(),
        "timezone": _local_timezone_name(),
        "checked_at": now.isoformat(),
    }


def get_ntp_status(timeout: float = 1.0) -> dict[str, Any]:
    """Best-effort host NTP synchronisation note. Never raises.

    Tries ``timedatectl`` when present; otherwise reports ``unknown``
    with the current NVR clock so callers still get a usable payload.
    """
    nvr = get_nvr_time()
    synchronized: Optional[bool] = None
    source = "host-managed"
    detail = "NTP state is host-managed; showing NVR clock."
    if shutil.which("timedatectl"):
        try:
            proc = subprocess.run(
                ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                capture_output=True,
                text=True,
                timeout=max(0.2, min(float(timeout), 5.0)),
            )
            val = (proc.stdout or "").strip().lower()
            if val in ("yes", "true", "1"):
                synchronized = True
                detail = "Host reports NTP synchronised (timedatectl)."
            elif val in ("no", "false", "0"):
                synchronized = False
                detail = "Host reports NTP not synchronised (timedatectl)."
        except Exception:  # noqa: BLE001
            synchronized = None
    result: dict[str, Any] = {
        "ok": True,
        "success": True,
        "synchronized": synchronized,
        "ntp_synchronized": synchronized,
        "source": source,
        "detail": detail,
        "message": detail,
        "nvr_time": nvr["nvr_time"],
        "utc_datetime": nvr["nvr_time"],
        "checked_at": nvr["checked_at"],
    }
    if synchronized is None:
        result["status"] = "unknown"
    else:
        result["status"] = "synchronized" if synchronized else "unsynchronized"
    return result


def compute_drift_seconds(camera_iso: Any, nvr_iso: Any = None) -> Optional[float]:
    """Signed drift in seconds (camera - nvr). None when unparseable."""
    cam = _parse_iso(camera_iso)
    nvr = _parse_iso(nvr_iso) if nvr_iso is not None else _utcnow()
    if cam is None or nvr is None:
        return None
    return (cam - nvr).total_seconds()


def evaluate_sync(
    camera_iso: Any,
    tolerance_seconds: float = DEFAULT_TOLERANCE_SECONDS,
    nvr_iso: Any = None,
) -> dict[str, Any]:
    """Evaluate camera drift against a tolerance window."""
    try:
        tolerance = float(tolerance_seconds)
    except (TypeError, ValueError):
        tolerance = DEFAULT_TOLERANCE_SECONDS
    tolerance = max(0.0, tolerance)
    drift = compute_drift_seconds(camera_iso, nvr_iso)
    if drift is None:
        return {
            "drift_seconds": None,
            "abs_drift_seconds": None,
            "tolerance_seconds": tolerance,
            "in_sync": None,
            "status": "unknown",
        }
    in_sync = abs(drift) <= tolerance
    return {
        "drift_seconds": drift,
        "abs_drift_seconds": abs(drift),
        "tolerance_seconds": tolerance,
        "in_sync": in_sync,
        "status": "in-sync" if in_sync else "drifted",
    }


async def get_camera_time(
    ip: str,
    port: int = 80,
    scheme: str = "http",
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Read a camera clock via ONVIF GetSystemDateAndTime (no auth).

    Falls back to an ``ok: False`` report when ONVIF support is missing
    or the device is unreachable — never raises.
    """
    nvr_iso = _iso_now()
    ip_s = (ip or "").strip()
    if not ip_s:
        return {
            "ok": False,
            "success": False,
            "error": "ip/host is required",
            "method": "none",
            "nvr_time": nvr_iso,
        }
    try:
        port_i = int(port)
    except (TypeError, ValueError):
        port_i = 80
    scheme_s = (scheme or "http").strip().lower() or "http"
    if _ods is None or not HAS_ONVIF:
        return {
            "ok": False,
            "success": False,
            "ip": ip_s,
            "port": port_i,
            "scheme": scheme_s,
            "method": "unavailable",
            "error": "ONVIF time service unavailable",
            "nvr_time": nvr_iso,
        }
    try:
        raw = await _ods.get_system_datetime(ip_s, port_i, scheme_s, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "success": False,
            "ip": ip_s,
            "port": port_i,
            "scheme": scheme_s,
            "method": "onvif",
            "error": str(exc),
            "nvr_time": nvr_iso,
        }
    camera_iso = (raw or {}).get("utc_datetime") if isinstance(raw, dict) else None
    drift = compute_drift_seconds(camera_iso, nvr_iso)
    return {
        "ok": True,
        "success": True,
        "ip": ip_s,
        "port": port_i,
        "scheme": scheme_s,
        "method": "onvif",
        "camera_time": camera_iso,
        "utc_datetime": camera_iso,
        "camera_timezone": (raw or {}).get("camera_timezone") if isinstance(raw, dict) else None,
        "nvr_time": nvr_iso,
        "drift_seconds": drift,
        "abs_drift_seconds": abs(drift) if drift is not None else None,
        "raw": raw if isinstance(raw, dict) else {"value": raw},
    }


async def get_time_sync_status(
    ip: Optional[str] = None,
    port: int = 80,
    scheme: str = "http",
    tolerance_seconds: float = DEFAULT_TOLERANCE_SECONDS,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """NVR clock plus optional per-camera drift evaluation. Never raises."""
    try:
        tolerance = float(tolerance_seconds)
    except (TypeError, ValueError):
        tolerance = DEFAULT_TOLERANCE_SECONDS
    nvr = get_nvr_time()
    ntp = get_ntp_status()
    base: dict[str, Any] = {
        "ok": True,
        "success": True,
        "nvr_time": nvr["nvr_time"],
        "utc_datetime": nvr["nvr_time"],
        "epoch": nvr["epoch"],
        "timezone": nvr["timezone"],
        "tolerance_seconds": max(0.0, tolerance),
        "ntp": ntp,
        "checked_at": nvr["checked_at"],
    }
    ip_s = (ip or "").strip() if ip is not None else ""
    if not ip_s:
        base.update(
            {
                "ip": None,
                "method": "none",
                "camera_time": None,
                "drift_seconds": None,
                "abs_drift_seconds": None,
                "in_sync": None,
                "status": "nvr-only",
                "detail": "No camera ip provided; reporting NVR clock only.",
            }
        )
        return base
    cam = await get_camera_time(ip_s, port, scheme, timeout=timeout)
    if not cam.get("ok"):
        base.update(
            {
                "ip": ip_s,
                "method": cam.get("method", "onvif"),
                "camera_time": None,
                "camera_timezone": None,
                "drift_seconds": None,
                "abs_drift_seconds": None,
                "in_sync": None,
                "status": "unknown",
                "error": cam.get("error"),
                "detail": cam.get("error") or "Camera clock unavailable.",
                "camera": cam,
            }
        )
        return base
    verdict = evaluate_sync(cam.get("camera_time"), tolerance, nvr["nvr_time"])
    base.update(
        {
            "ip": ip_s,
            "port": cam.get("port"),
            "scheme": cam.get("scheme"),
            "method": "onvif",
            "camera_time": cam.get("camera_time"),
            "camera_timezone": cam.get("camera_timezone"),
            "drift_seconds": verdict["drift_seconds"],
            "abs_drift_seconds": verdict["abs_drift_seconds"],
            "in_sync": verdict["in_sync"],
            "status": verdict["status"],
            "detail": (
                f"Camera drift {verdict['drift_seconds']:.1f}s "
                f"(tolerance {tolerance:.1f}s)."
                if verdict["drift_seconds"] is not None
                else "Camera clock read; drift unparseable."
            ),
            "camera": cam,
        }
    )
    return base


async def sync_camera_time(
    ip: str,
    username: str = "",
    password: str = "",
    port: int = 80,
    scheme: str = "http",
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Push the NVR's UTC clock to a camera via ONVIF SetSystemDateAndTime.

    Uses :mod:`app.services.onvif_digest_service` where available;
    otherwise returns an ``ok: False`` report instead of raising.
    """
    nvr_iso = _iso_now()
    ip_s = (ip or "").strip()
    if not ip_s:
        return {
            "ok": False,
            "success": False,
            "error": "ip/host is required",
            "method": "none",
            "nvr_time": nvr_iso,
        }
    try:
        port_i = int(port)
    except (TypeError, ValueError):
        port_i = 80
    scheme_s = (scheme or "http").strip().lower() or "http"
    if _ods is None or not HAS_ONVIF:
        return {
            "ok": False,
            "success": False,
            "ip": ip_s,
            "method": "unavailable",
            "error": "ONVIF time service unavailable",
            "nvr_time": nvr_iso,
        }
    if not (username and password):
        return {
            "ok": False,
            "success": False,
            "ip": ip_s,
            "port": port_i,
            "scheme": scheme_s,
            "method": "onvif",
            "error": "username/password is required for ONVIF SetSystemDateAndTime",
            "nvr_time": nvr_iso,
        }
    started = time.monotonic()
    try:
        result = await _ods.set_system_datetime(ip_s, username, password, port_i, scheme_s)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "success": False,
            "ip": ip_s,
            "port": port_i,
            "scheme": scheme_s,
            "method": "onvif",
            "error": str(exc),
            "nvr_time": nvr_iso,
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
        }
    synced_iso = (result or {}).get("synced_utc") if isinstance(result, dict) else None
    # Best-effort readback; sync success does not depend on it.
    readback: dict[str, Any] | None = None
    try:
        readback = await _ods.get_system_datetime(ip_s, port_i, scheme_s, timeout=timeout)
    except Exception:  # noqa: BLE001
        readback = None
    camera_iso = (readback or {}).get("utc_datetime") if isinstance(readback, dict) else None
    drift = compute_drift_seconds(camera_iso or synced_iso, _iso_now())
    return {
        "ok": True,
        "success": True,
        "ip": ip_s,
        "port": port_i,
        "scheme": scheme_s,
        "method": "onvif",
        "synced_utc": synced_iso,
        "camera_time": camera_iso or synced_iso,
        "camera_timezone": (readback or {}).get("camera_timezone") if isinstance(readback, dict) else None,
        "nvr_time": nvr_iso,
        "drift_seconds": drift,
        "abs_drift_seconds": abs(drift) if drift is not None else None,
        "in_sync": (abs(drift) <= DEFAULT_TOLERANCE_SECONDS) if drift is not None else None,
        "status": "synced",
        "latency_ms": round((time.monotonic() - started) * 1000, 1),
        "detail": f"Pushed NVR UTC clock to {ip_s} via ONVIF SetSystemDateAndTime.",
        "raw": result,
    }


# Aliases matching repo naming tolerance.
get_status = get_time_sync_status
check_sync = get_time_sync_status
time_sync_status = get_time_sync_status
status = get_time_sync_status
check_drift = get_time_sync_status
get_drift = get_time_sync_status
camera_time = get_camera_time
nvr_time = get_nvr_time
ntp_status = get_ntp_status
sync_camera = sync_camera_time
set_camera_time = sync_camera_time
push_camera_time = sync_camera_time


class TimeSyncService:
    def nvr_time(self) -> dict[str, Any]:
        return get_nvr_time()

    def ntp_status(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return get_ntp_status(*args, **kwargs)

    async def camera_time(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return await get_camera_time(*args, **kwargs)

    async def get_status(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return await get_time_sync_status(*args, **kwargs)

    async def get_time_sync_status(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return await get_time_sync_status(*args, **kwargs)

    async def sync(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return await sync_camera_time(*args, **kwargs)

    async def sync_camera_time(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return await sync_camera_time(*args, **kwargs)


time_sync_service = TimeSyncService()
service = time_sync_service
time_service = time_sync_service

TimeService = TimeSyncService
TimeSync = TimeSyncService
NtpService = TimeSyncService


def __getattr__(name: str):  # pragma: no cover - compatibility shim
    if name.startswith("_"):
        raise AttributeError(name)
    low = name.lower()
    if any(tok in low for tok in ("service", "manager", "handler", "checker")):
        return TimeSyncService
    if "ntp" in low:
        return get_ntp_status
    if "nvr" in low:
        return get_nvr_time
    if "drift" in low or "evaluat" in low:
        return evaluate_sync
    if "sync" in low and ("set" in low or "push" in low or "camera" in low):
        return sync_camera_time
    if any(tok in low for tok in ("status", "sync", "check", "time", "clock")):
        return get_time_sync_status
    raise AttributeError(name)


__all__ = [
    "HAS_ONVIF",
    "DEFAULT_TOLERANCE_SECONDS",
    "DEFAULT_TIMEOUT",
    "get_nvr_time",
    "get_ntp_status",
    "compute_drift_seconds",
    "evaluate_sync",
    "get_camera_time",
    "get_time_sync_status",
    "sync_camera_time",
    "TimeSyncService",
    "time_sync_service",
]
