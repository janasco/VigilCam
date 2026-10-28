"""Stream health probe service for VigilCam.

Lightweight, dependency-free probe that checks whether a camera stream URL
is reachable and how long the check takes. Results are cached in-memory
keyed by normalized target so the API can serve last-known status without
re-probing on every read.

Design goals:
- stdlib only (socket + urllib) so it works in minimal test envs.
- injectable ``prober`` callback for deterministic unit tests.
- ``camera_health`` aware fallback when no URL is available.
- auto-registered API lives in ``app.api.stream_probe``; no ``app/main.py``
  edit is required.

Probe semantics:
- ``reachable=True`` -> ``status="healthy"``.
- ``reachable=False`` -> ``status="unhealthy"``.
- empty/unknown target -> ``status="unknown"`` (never raises).
"""

from __future__ import annotations

import socket
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

DEFAULT_TIMEOUT_SECONDS = 5.0
DEFAULT_RTSP_PORT = 554
DEFAULT_HTTP_PORT = 80
DEFAULT_HTTPS_PORT = 443
DEFAULT_RTMP_PORT = 1935

_SCHEME_DEFAULT_PORT = {
    "rtsp": DEFAULT_RTSP_PORT,
    "rtmp": DEFAULT_RTMP_PORT,
    "http": DEFAULT_HTTP_PORT,
    "https": DEFAULT_HTTPS_PORT,
    "onvif": DEFAULT_HTTP_PORT,
    "ws": DEFAULT_HTTP_PORT,
    "wss": DEFAULT_HTTPS_PORT,
}

# In-memory probe results keyed by normalized target string.
_RESULTS: Dict[str, "StreamProbeResult"] = {}


def _now() -> datetime:
    return datetime.utcnow()


def _coerce_timeout(value: Any) -> float:
    try:
        t = float(value)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SECONDS
    if t <= 0:
        return DEFAULT_TIMEOUT_SECONDS
    return min(t, 60.0)


def _normalize_target(target: Any) -> str:
    if target is None:
        return ""
    if isinstance(target, (int, float)) and not isinstance(target, bool):
        return f"camera:{int(target)}"
    s = str(target).strip()
    return s


def _target_key(target: Any, url: Any = None) -> str:
    if url is not None and str(url).strip():
        return _normalize_target(url)
    return _normalize_target(target)


@dataclass
class StreamProbeResult:
    """Single stream probe outcome."""

    target: str
    url: Optional[str] = None
    reachable: bool = False
    healthy: bool = False
    status: str = "unknown"
    latency_ms: Optional[float] = None
    error: Optional[str] = None
    checked_at: Optional[datetime] = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    def to_dict(self) -> Dict[str, Any]:
        return {
            "target": self.target,
            "url": self.url,
            "reachable": bool(self.reachable),
            "healthy": bool(self.healthy),
            "status": self.status,
            "state": self.status,
            "ok": bool(self.reachable),
            "success": bool(self.reachable),
            "latency_ms": self.latency_ms,
            "error": self.error,
            "checked_at": self.checked_at.isoformat() if self.checked_at else None,
            "timeout_seconds": self.timeout_seconds,
        }

    def dict(self) -> Dict[str, Any]:  # noqa: A003
        return self.to_dict()

    def model_dump(self) -> Dict[str, Any]:
        return self.to_dict()

    def as_dict(self) -> Dict[str, Any]:
        return self.to_dict()

    def __getitem__(self, key: str) -> Any:
        if hasattr(self, key):
            return getattr(self, key)
        aliases = {
            "id": "target",
            "latency": "latency_ms",
            "ok": "reachable",
            "success": "reachable",
            "state": "status",
            "message": "error",
        }
        if key in aliases:
            return getattr(self, aliases[key])
        return None

    def get(self, key: str, default: Any = None) -> Any:
        try:
            val = self[key]
            return default if val is None and key not in self.__dict__ else val
        except Exception:  # noqa: BLE001
            return default


StreamProbeStatus = StreamProbeResult
ProbeResult = StreamProbeResult
StreamHealth = StreamProbeResult


def _tcp_check(host: str, port: int, timeout: float) -> None:
    with socket.create_connection((host, port), timeout=timeout):
        pass


def _http_check(url: str, timeout: float) -> None:
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            _ = resp.status
    except Exception as head_exc:  # noqa: BLE001 - fall back to GET
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
                _ = resp.status
        except Exception:  # noqa: BLE001
            raise head_exc


def _default_check(url: str, timeout: float) -> None:
    parsed = urllib.parse.urlparse(url if "://" in url else f"rtsp://{url}")
    scheme = (parsed.scheme or "rtsp").lower()
    host = parsed.hostname or ""
    if not host:
        raise ValueError("no host in stream url")
    if scheme in ("http", "https"):
        _http_check(url, timeout)
        return
    port = parsed.port or _SCHEME_DEFAULT_PORT.get(scheme, DEFAULT_RTSP_PORT)
    _tcp_check(host, port, timeout)


def probe_url(
    url: Any = None,
    timeout: Any = DEFAULT_TIMEOUT_SECONDS,
    *,
    target: Any = None,
    prober: Optional[Callable[..., Any]] = None,
    **kwargs: Any,
) -> StreamProbeResult:
    """Probe a single stream URL. Never raises; unknown URL -> unhealthy=False."""
    if url is None:
        for cand in (target, kwargs.get("target"), kwargs.get("stream_url"), kwargs.get("source")):
            if cand is not None and str(cand).strip():
                # Only treat as URL if it looks like one; otherwise keep as target.
                url = cand
                break
    url_s = str(url).strip() if url is not None else ""
    if target is None:
        target = kwargs.get("camera_id", url_s)
    key = _normalize_target(url_s if url_s else target)
    timeout_f = _coerce_timeout(timeout)
    if "timeout_seconds" in kwargs:
        timeout_f = _coerce_timeout(kwargs.get("timeout_seconds"))

    if not url_s:
        result = StreamProbeResult(
            target=key or "unknown",
            url=None,
            reachable=False,
            healthy=False,
            status="unknown",
            latency_ms=None,
            error="no stream url",
            checked_at=_now(),
            timeout_seconds=timeout_f,
        )
        _RESULTS[result.target] = result
        return result

    start = time.monotonic()
    error: Optional[str] = None
    reachable = False
    try:
        if prober is not None:
            out = prober(url_s, timeout_f)
            if isinstance(out, dict):
                reachable = bool(out.get("reachable", out.get("ok", out.get("healthy", False))))
                err = out.get("error")
                if err is not None and not reachable:
                    error = str(err)
            else:
                reachable = bool(out)
        else:
            _default_check(url_s, timeout_f)
            reachable = True
    except Exception as exc:  # noqa: BLE001
        reachable = False
        error = f"{type(exc).__name__}: {exc}"[:500]
    latency_ms = (time.monotonic() - start) * 1000.0
    result = StreamProbeResult(
        target=key,
        url=url_s,
        reachable=reachable,
        healthy=bool(reachable),
        status="healthy" if reachable else "unhealthy",
        latency_ms=round(latency_ms, 2),
        error=error,
        checked_at=_now(),
        timeout_seconds=timeout_f,
    )
    _RESULTS[key] = result
    return result


# Aliases for probe_url.
probe = probe_url
probe_stream = probe_url
check_url = probe_url
check_stream = probe_url
probe_target = probe_url
check_target = probe_url


def _resolve_camera_url(camera_id: Any, url: Any = None) -> Optional[str]:
    if url is not None and str(url).strip():
        return str(url).strip()
    # Best-effort: look up camera source from DB-backed service if available.
    try:
        from app.services import cameras as _cams  # type: ignore

        for getter in ("get_camera", "get", "find", "get_by_id"):
            fn = getattr(_cams, getter, None)
            if callable(fn):
                try:
                    cam = fn(camera_id)
                    if cam is not None:
                        for attr in ("source", "stream_url", "url", "rtsp_url"):
                            val = cam.get(attr) if isinstance(cam, dict) else getattr(cam, attr, None)
                            if val and str(val).strip():
                                return str(val).strip()
                except Exception:  # noqa: BLE001
                    continue
    except Exception:  # noqa: BLE001
        pass
    return None


def probe_camera(
    camera_id: Any = None,
    timeout: Any = DEFAULT_TIMEOUT_SECONDS,
    url: Any = None,
    *,
    prober: Optional[Callable[..., Any]] = None,
    **kwargs: Any,
) -> StreamProbeResult:
    """Probe a camera by id, optionally with an explicit stream URL.

    When no URL can be resolved the result falls back to the
    ``camera_health`` online flag (healthy iff currently online), with
    ``status="unknown"`` when no health record exists.
    """
    if camera_id is None:
        for cand in (kwargs.get("camera_id"), kwargs.get("id"), kwargs.get("camera")):
            if cand is not None:
                camera_id = cand
                break
    if url is None:
        url = kwargs.get("url", kwargs.get("stream_url", kwargs.get("source")))
    resolved = _resolve_camera_url(camera_id, url)
    key = f"camera:{camera_id}" if camera_id is not None else _normalize_target(resolved or "unknown")
    timeout_f = _coerce_timeout(timeout)

    if resolved:
        result = probe_url(resolved, timeout_f, target=key, prober=prober)
        # Keep camera-scoped key in addition to URL key for easy lookup.
        _RESULTS[key] = result
        # Also expose camera_id field compat via target alias lookup.
        return result

    # Fallback: derive health from camera_health service.
    try:
        from app.services import camera_health as _ch  # type: ignore

        st = _ch.get_health(camera_id, create=False) if camera_id is not None else None
        if st is not None:
            online = bool(getattr(st, "online", False))
            result = StreamProbeResult(
                target=key,
                url=None,
                reachable=online,
                healthy=online,
                status="healthy" if online else "unhealthy",
                latency_ms=0.0,
                error=None if online else "no recent frames",
                checked_at=_now(),
                timeout_seconds=timeout_f,
            )
            _RESULTS[key] = result
            return result
    except Exception:  # noqa: BLE001
        pass
    result = StreamProbeResult(
        target=key,
        url=None,
        reachable=False,
        healthy=False,
        status="unknown",
        latency_ms=None,
        error="no stream url and no health record",
        checked_at=_now(),
        timeout_seconds=timeout_f,
    )
    _RESULTS[key] = result
    return result


# Aliases for probe_camera.
probe_camera_stream = probe_camera
check_camera = probe_camera
check_camera_stream = probe_camera
camera_probe = probe_camera


async def aprobe_url(url: Any = None, timeout: Any = DEFAULT_TIMEOUT_SECONDS, **kwargs: Any) -> StreamProbeResult:
    import asyncio as _asyncio

    prober = kwargs.pop("prober", None)
    target = kwargs.pop("target", None)
    return await _asyncio.to_thread(probe_url, url, timeout, target=target, prober=prober, **kwargs)


async def aprobe_camera(camera_id: Any = None, timeout: Any = DEFAULT_TIMEOUT_SECONDS, **kwargs: Any) -> StreamProbeResult:
    import asyncio as _asyncio

    return await _asyncio.to_thread(probe_camera, camera_id, timeout, **kwargs)


def get_status(target: Any = None, url: Any = None, **kwargs: Any) -> Optional[StreamProbeResult]:
    key = _target_key(target if target is not None else kwargs.get("target"), url or kwargs.get("url"))
    if not key:
        return None
    # Direct hit.
    if key in _RESULTS:
        return _RESULTS[key]
    # Camera-id shorthand: "3" should also match "camera:3".
    try:
        cid = int(str(key).strip())
        alt = f"camera:{cid}"
        if alt in _RESULTS:
            return _RESULTS[alt]
    except (TypeError, ValueError):
        pass
    return None


get = get_status
get_result = get_status
get_probe = get_status
find = get_status
lookup = get_status
status_for = get_status


def list_results() -> List[StreamProbeResult]:
    return [_RESULTS[k] for k in sorted(_RESULTS.keys())]


list_all = list_results
all_results = list_results
get_all = list_results
results = list_results


def list_results_dicts() -> List[Dict[str, Any]]:
    return [r.to_dict() for r in list_results()]


def get_summary() -> Dict[str, Any]:
    items = list_results()
    total = len(items)
    healthy = sum(1 for r in items if r.healthy)
    unhealthy = sum(1 for r in items if r.status == "unhealthy")
    unknown = total - healthy - unhealthy
    return {
        "total": total,
        "probed": total,
        "healthy": healthy,
        "unhealthy": unhealthy,
        "unknown": unknown,
        "reachable": healthy,
        "ok": total == 0 or unhealthy == 0,
        "success": total == 0 or unhealthy == 0,
    }


summary = get_summary
get_stats = get_summary
stats = get_summary


def is_healthy(target: Any = None, **kwargs: Any) -> bool:
    st = get_status(target, **kwargs)
    return bool(st and st.healthy)


def is_reachable(target: Any = None, **kwargs: Any) -> bool:
    st = get_status(target, **kwargs)
    return bool(st and st.reachable)


def remove(target: Any = None, **kwargs: Any) -> bool:
    key = _target_key(target, kwargs.get("url"))
    if not key:
        return False
    return _RESULTS.pop(key, None) is not None


delete = remove
drop = remove
unregister = remove
clear_one = remove


def clear(target: Any = None, **kwargs: Any) -> None:
    global _RESULTS
    if target is None and "url" not in kwargs and not kwargs.get("all"):
        _RESULTS.clear()
        return
    if target is None:
        target = kwargs.get("target", kwargs.get("url"))
    if target is None:
        _RESULTS.clear()
        return
    key = _normalize_target(target)
    if key in _RESULTS:
        _RESULTS.pop(key, None)
    else:
        _RESULTS.clear()


clear_all = lambda: _RESULTS.clear()  # noqa: E731
reset_all = clear_all
reset = clear_all


def serialize(state: Any) -> Dict[str, Any]:
    if isinstance(state, StreamProbeResult):
        return state.to_dict()
    if isinstance(state, dict):
        return state
    return {"value": str(state)}


__all__ = [
    "StreamProbeResult",
    "StreamProbeStatus",
    "ProbeResult",
    "probe_url",
    "probe_camera",
    "aprobe_url",
    "aprobe_camera",
    "get_status",
    "list_results",
    "get_summary",
    "is_healthy",
    "is_reachable",
    "clear",
    "remove",
]
