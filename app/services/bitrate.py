"""Stream bitrate sampler service for VigilCam.

Measures approximate stream throughput (bytes over time -> bps/kbps/mbps)
without touching health logic in ``app.services.stream_probe`` (verified:
``stream_probe`` checks reachability/latency only and has no bitrate
measurement). Results are cached in-memory keyed by normalized target so
the API can serve last-known samples without re-sampling on every read.

Design goals:
- stdlib only (socket + urllib + time) so it works in minimal test envs.
- injectable ``sampler`` callback for deterministic unit tests.
- ``cameras``-service aware URL resolution fallback when no URL is given.
- auto-registered API lives in ``app.api.bitrate``; no ``app/main.py``
  edit is required.

Sample semantics:
- success -> ``status="measured"``, ``ok=True``.
- empty/unknown target -> ``status="unknown"`` (never raises, 0 bps).
- sampler/default-check failure -> ``status="error"`` (never raises, 0 bps).
"""

from __future__ import annotations

import socket
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

DEFAULT_SAMPLE_SECONDS = 2.0
DEFAULT_TIMEOUT_SECONDS = 5.0
DEFAULT_RTSP_PORT = 554
DEFAULT_HTTP_PORT = 80
DEFAULT_HTTPS_PORT = 443
DEFAULT_RTMP_PORT = 1935
MAX_SAMPLE_SECONDS = 60.0

_SCHEME_DEFAULT_PORT = {
    "rtsp": DEFAULT_RTSP_PORT,
    "rtmp": DEFAULT_RTMP_PORT,
    "http": DEFAULT_HTTP_PORT,
    "https": DEFAULT_HTTPS_PORT,
    "onvif": DEFAULT_HTTP_PORT,
    "ws": DEFAULT_HTTP_PORT,
    "wss": DEFAULT_HTTPS_PORT,
}

# In-memory samples keyed by normalized target string.
_RESULTS: Dict[str, "BitrateResult"] = {}


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


def _coerce_duration(value: Any) -> float:
    try:
        t = float(value)
    except (TypeError, ValueError):
        return DEFAULT_SAMPLE_SECONDS
    if t <= 0:
        return DEFAULT_SAMPLE_SECONDS
    return min(t, MAX_SAMPLE_SECONDS)


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


def _compute_rates(bytes_received: float, duration_seconds: float):
    try:
        n = float(bytes_received)
    except (TypeError, ValueError):
        n = 0.0
    if n < 0:
        n = 0.0
    try:
        d = float(duration_seconds)
    except (TypeError, ValueError):
        d = 0.0
    if d <= 0:
        return 0.0, 0.0, 0.0
    bps = (n * 8.0) / d
    return bps, bps / 1000.0, bps / 1_000_000.0


@dataclass
class BitrateResult:
    """Single stream bitrate sample."""

    target: str = "unknown"
    url: Optional[str] = None
    bytes_received: int = 0
    duration_seconds: float = DEFAULT_SAMPLE_SECONDS
    bitrate_bps: float = 0.0
    bitrate_kbps: float = 0.0
    bitrate_mbps: float = 0.0
    status: str = "unknown"
    error: Optional[str] = None
    sampled_at: Optional[datetime] = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    # Compat aliases used by older callers / tests.
    @property
    def checked_at(self) -> Optional[datetime]:
        return self.sampled_at

    @property
    def bytes(self) -> int:
        return self.bytes_received

    @property
    def ok(self) -> bool:
        return self.status == "measured"

    @property
    def success(self) -> bool:
        return self.status == "measured"

    @property
    def bps(self) -> float:
        return self.bitrate_bps

    @property
    def kbps(self) -> float:
        return self.bitrate_kbps

    @property
    def mbps(self) -> float:
        return self.bitrate_mbps

    def to_dict(self) -> Dict[str, Any]:
        return {
            "target": self.target,
            "url": self.url,
            "bytes_received": self.bytes_received,
            "bytes": self.bytes_received,
            "total_bytes": self.bytes_received,
            "size_bytes": self.bytes_received,
            "duration_seconds": self.duration_seconds,
            "duration": self.duration_seconds,
            "sample_seconds": self.duration_seconds,
            "bitrate_bps": self.bitrate_bps,
            "bps": self.bitrate_bps,
            "bitrate_kbps": self.bitrate_kbps,
            "kbps": self.bitrate_kbps,
            "bitrate_mbps": self.bitrate_mbps,
            "mbps": self.bitrate_mbps,
            "status": self.status,
            "state": self.status,
            "ok": self.status == "measured",
            "success": self.status == "measured",
            "error": self.error,
            "sampled_at": self.sampled_at.isoformat() if self.sampled_at else None,
            "checked_at": self.sampled_at.isoformat() if self.sampled_at else None,
            "timeout_seconds": self.timeout_seconds,
        }

    def dict(self) -> Dict[str, Any]:  # noqa: A003
        return self.to_dict()

    def model_dump(self) -> Dict[str, Any]:
        return self.to_dict()

    def as_dict(self) -> Dict[str, Any]:
        return self.to_dict()

    def __getitem__(self, key: str) -> Any:
        data = self.to_dict()
        if key in data:
            return data[key]
        if hasattr(self, key):
            return getattr(self, key)
        aliases = {
            "id": "target",
            "latency": "duration_seconds",
            "message": "error",
        }
        if key in aliases:
            return data.get(aliases[key], getattr(self, aliases[key], None))
        return None

    def get(self, key: str, default: Any = None) -> Any:
        try:
            val = self[key]
            return default if val is None and key not in self.__dict__ else val
        except Exception:  # noqa: BLE001
            return default


BitrateSample = BitrateResult
BitrateStatus = BitrateResult
SampleResult = BitrateResult
StreamBitrate = BitrateResult


def _default_sampler(url: str, duration: float, timeout: float) -> Dict[str, Any]:
    """Stdlib sampler: read bytes from *url* for up to *duration* seconds."""
    parsed = urllib.parse.urlparse(url if "://" in url else f"rtsp://{url}")
    scheme = (parsed.scheme or "rtsp").lower()
    if scheme in ("http", "https"):
        start = time.monotonic()
        total = 0
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                deadline = start + duration
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        chunk = resp.read(min(65536, 8192))
                    except socket.timeout:
                        break
                    if not chunk:
                        break
                    total += len(chunk)
        except Exception:
            raise
        elapsed = max(time.monotonic() - start, 1e-6)
        return {"bytes_received": total, "duration_seconds": elapsed}
    # Non-HTTP: best-effort TCP read for *duration* seconds (often 0 bytes
    # without an RTSP handshake, which is still a valid 0-bps sample when
    # the port is reachable).
    host = parsed.hostname or ""
    if not host:
        raise ValueError("no host in stream url")
    port = parsed.port or _SCHEME_DEFAULT_PORT.get(scheme, DEFAULT_RTSP_PORT)
    start = time.monotonic()
    total = 0
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(min(timeout, 1.0))
        deadline = start + duration
        while time.monotonic() < deadline:
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                break
            except OSError:
                break
            if not chunk:
                break
            total += len(chunk)
    elapsed = max(time.monotonic() - start, 1e-6)
    return {"bytes_received": total, "duration_seconds": elapsed}


def _parse_sampler_output(
    out: Any, fallback_duration: float
) -> tuple[int, float, Optional[float], Optional[str]]:
    """Return (bytes_received, duration_seconds, bitrate_bps_or_None, error)."""
    if out is None:
        return 0, fallback_duration, None, "sampler returned no data"
    if isinstance(out, bool):
        return (8192 if out else 0), fallback_duration, None, None if out else "sampler reported failure"
    if isinstance(out, (int, float)):
        try:
            return max(0, int(out)), fallback_duration, None, None
        except (TypeError, ValueError):
            return 0, fallback_duration, None, "invalid sampler result"
    if isinstance(out, (list, tuple)) and len(out) == 2:
        try:
            n = max(0, int(float(out[0])))
        except (TypeError, ValueError):
            n = 0
        try:
            d = float(out[1])
            if d <= 0:
                d = fallback_duration
        except (TypeError, ValueError):
            d = fallback_duration
        return n, d, None, None
    if isinstance(out, dict):
        err = out.get("error")
        n = None
        for k in ("bytes_received", "bytes", "total_bytes", "size_bytes", "size", "length", "content_length", "nbytes"):
            if out.get(k) is not None:
                try:
                    n = max(0, int(float(out[k])))
                    break
                except (TypeError, ValueError):
                    continue
        if n is None:
            n = 0
        d = fallback_duration
        for k in ("duration_seconds", "duration", "elapsed", "elapsed_seconds", "sample_seconds", "sample_duration"):
            if out.get(k) is not None:
                try:
                    cand = float(out[k])
                    if cand > 0:
                        d = cand
                    break
                except (TypeError, ValueError):
                    continue
        bps = None
        for k in ("bitrate_bps", "bps", "bitrate", "bits_per_second"):
            if out.get(k) is not None:
                try:
                    bps = max(0.0, float(out[k]))
                    break
                except (TypeError, ValueError):
                    continue
        if bps is None:
            for k in ("bitrate_kbps", "kbps"):
                if out.get(k) is not None:
                    try:
                        bps = max(0.0, float(out[k])) * 1000.0
                        break
                    except (TypeError, ValueError):
                        continue
        if bps is None:
            for k in ("bitrate_mbps", "mbps"):
                if out.get(k) is not None:
                    try:
                        bps = max(0.0, float(out[k])) * 1_000_000.0
                        break
                    except (TypeError, ValueError):
                        continue
        if err is not None and n == 0 and bps in (None, 0):
            return n, d, bps, str(err)
        return n, d, bps, str(err) if err else None
    # Unknown shape: stringify defensively.
    try:
        n = max(0, int(float(str(out).strip())))
        return n, fallback_duration, None, None
    except (TypeError, ValueError):
        return 0, fallback_duration, None, None


def sample_url(
    url: Any = None,
    duration: Any = DEFAULT_SAMPLE_SECONDS,
    timeout: Any = DEFAULT_TIMEOUT_SECONDS,
    *,
    target: Any = None,
    sampler: Optional[Callable[..., Any]] = None,
    **kwargs: Any,
) -> BitrateResult:
    """Sample bitrate for a single stream URL. Never raises."""
    if url is None:
        for cand in (target, kwargs.get("target"), kwargs.get("stream_url"), kwargs.get("source")):
            if cand is not None and str(cand).strip():
                url = cand
                break
    url_s = str(url).strip() if url is not None else ""
    if target is None:
        target = kwargs.get("camera_id", kwargs.get("id", kwargs.get("camera", url_s)))
    key = _normalize_target(url_s if url_s else target)
    duration_f = _coerce_duration(
        kwargs.get("duration_seconds", kwargs.get("sample_seconds", kwargs.get("sample_duration", duration)))
    )
    timeout_f = _coerce_timeout(kwargs.get("timeout_seconds", timeout))
    if "timeout" in kwargs and kwargs.get("timeout") is not None and timeout == DEFAULT_TIMEOUT_SECONDS:
        timeout_f = _coerce_timeout(kwargs.get("timeout"))

    if not url_s:
        result = BitrateResult(
            target=key or "unknown",
            url=None,
            bytes_received=0,
            duration_seconds=duration_f,
            bitrate_bps=0.0,
            bitrate_kbps=0.0,
            bitrate_mbps=0.0,
            status="unknown",
            error="no stream url",
            sampled_at=_now(),
            timeout_seconds=timeout_f,
        )
        _RESULTS[result.target] = result
        return result

    start = time.monotonic()
    error: Optional[str] = None
    nbytes = 0
    elapsed = duration_f
    bps_override: Optional[float] = None
    ok = False
    try:
        if sampler is not None:
            try:
                out = sampler(url_s, duration_f)
            except TypeError:
                # Tolerate sampler(url), sampler(url, timeout=...), sampler(url, duration).
                try:
                    out = sampler(url_s)
                except TypeError:
                    out = sampler(url_s, duration_f, timeout_f)
            nbytes, elapsed, bps_override, sampler_err = _parse_sampler_output(out, duration_f)
            if sampler_err and nbytes == 0 and (bps_override in (None, 0)):
                # Dict explicitly reporting failure counts as error unless it
                # carried a usable bitrate.
                if isinstance(out, dict) and not out.get("reachable", out.get("ok", out.get("healthy", True))):
                    error = sampler_err
                elif nbytes == 0 and bps_override in (None, 0):
                    # Plain zero-byte sample without error is still measured (0 bps).
                    error = None if sampler_err in (None, "sampler reported failure") and nbytes == 0 and False else None
                    if isinstance(out, dict) and sampler_err:
                        error = sampler_err if out.get("error") else None
                else:
                    error = sampler_err
            elif sampler_err:
                error = sampler_err
            ok = error is None
            if isinstance(out, dict):
                # Explicit reachable=False forces error status.
                for fk in ("reachable", "ok", "healthy", "success"):
                    if fk in out and not out[fk] and nbytes == 0 and (bps_override in (None, 0)):
                        ok = False
                        if error is None:
                            error = str(out.get("error", "sampler reported failure"))
                        break
        else:
            out = _default_sampler(url_s, duration_f, timeout_f)
            nbytes, elapsed, bps_override, sampler_err = _parse_sampler_output(out, duration_f)
            error = sampler_err
            ok = error is None
    except Exception as exc:  # noqa: BLE001
        ok = False
        error = f"{type(exc).__name__}: {exc}"[:500]
        elapsed = max(time.monotonic() - start, 1e-6)
    if bps_override is not None:
        bps = max(0.0, float(bps_override))
        kbps = bps / 1000.0
        mbps = bps / 1_000_000.0
    else:
        bps, kbps, mbps = _compute_rates(nbytes, elapsed)
    # Zero-byte success is still a valid measurement (0 bps link).
    status = "measured" if ok else "error"
    result = BitrateResult(
        target=key,
        url=url_s,
        bytes_received=int(nbytes),
        duration_seconds=round(float(elapsed), 3),
        bitrate_bps=round(float(bps), 2),
        bitrate_kbps=round(float(kbps), 3),
        bitrate_mbps=round(float(mbps), 6),
        status=status,
        error=error,
        sampled_at=_now(),
        timeout_seconds=timeout_f,
    )
    _RESULTS[key] = result
    return result


# Aliases for sample_url.
sample = sample_url
sample_stream = sample_url
measure_url = sample_url
measure = sample_url
measure_stream = sample_url
probe_url = sample_url
probe = sample_url
probe_stream = sample_url
probe_bitrate = sample_url
sample_bitrate = sample_url
measure_bitrate = sample_url
get_bitrate = sample_url
check_url = sample_url
check_stream = sample_url
check_bitrate = sample_url


def _resolve_camera_url(camera_id: Any, url: Any = None) -> Optional[str]:
    if url is not None and str(url).strip():
        return str(url).strip()
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


def sample_camera(
    camera_id: Any = None,
    duration: Any = DEFAULT_SAMPLE_SECONDS,
    timeout: Any = DEFAULT_TIMEOUT_SECONDS,
    url: Any = None,
    *,
    sampler: Optional[Callable[..., Any]] = None,
    **kwargs: Any,
) -> BitrateResult:
    """Sample bitrate for a camera id, optionally with an explicit stream URL."""
    if camera_id is None:
        for cand in (kwargs.get("camera_id"), kwargs.get("id"), kwargs.get("camera")):
            if cand is not None:
                camera_id = cand
                break
    if url is None:
        url = kwargs.get("url", kwargs.get("stream_url", kwargs.get("source")))
    if duration == DEFAULT_SAMPLE_SECONDS:
        for k in ("duration_seconds", "sample_seconds", "sample_duration"):
            if kwargs.get(k) is not None:
                duration = kwargs[k]
                break
    resolved = _resolve_camera_url(camera_id, url)
    key = f"camera:{camera_id}" if camera_id is not None else _normalize_target(resolved or "unknown")
    timeout_f = _coerce_timeout(kwargs.get("timeout_seconds", timeout))
    duration_f = _coerce_duration(duration)

    if resolved:
        result = sample_url(resolved, duration_f, timeout_f, target=key, sampler=sampler)
        _RESULTS[key] = result
        return result

    result = BitrateResult(
        target=key,
        url=None,
        bytes_received=0,
        duration_seconds=duration_f,
        bitrate_bps=0.0,
        bitrate_kbps=0.0,
        bitrate_mbps=0.0,
        status="unknown",
        error="no stream url and no health record",
        sampled_at=_now(),
        timeout_seconds=timeout_f,
    )
    _RESULTS[key] = result
    return result


# Aliases for sample_camera.
sample_camera_stream = sample_camera
check_camera = sample_camera
check_camera_stream = sample_camera
camera_sample = sample_camera
probe_camera = sample_camera
measure_camera = sample_camera
get_camera_bitrate = sample_camera


async def asample_url(url: Any = None, duration: Any = DEFAULT_SAMPLE_SECONDS, **kwargs: Any) -> BitrateResult:
    import asyncio as _asyncio

    sampler = kwargs.pop("sampler", None)
    target = kwargs.pop("target", None)
    timeout = kwargs.pop("timeout", kwargs.pop("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
    return await _asyncio.to_thread(sample_url, url, duration, timeout, target=target, sampler=sampler, **kwargs)


async def asample_camera(camera_id: Any = None, duration: Any = DEFAULT_SAMPLE_SECONDS, **kwargs: Any) -> BitrateResult:
    import asyncio as _asyncio

    return await _asyncio.to_thread(sample_camera, camera_id, duration, **kwargs)


def get_status(target: Any = None, url: Any = None, **kwargs: Any) -> Optional[BitrateResult]:
    key = _target_key(target if target is not None else kwargs.get("target"), url or kwargs.get("url"))
    if not key:
        return None
    if key in _RESULTS:
        return _RESULTS[key]
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
get_sample = get_status
find = get_status
lookup = get_status
status_for = get_status
sample_for = get_status


def list_results() -> List[BitrateResult]:
    return [_RESULTS[k] for k in sorted(_RESULTS.keys())]


list_all = list_results
all_results = list_results
get_all = list_results
results = list_results
list_samples = list_results
samples = list_results


def list_results_dicts() -> List[Dict[str, Any]]:
    return [r.to_dict() for r in list_results()]


def get_summary() -> Dict[str, Any]:
    items = list_results()
    total = len(items)
    measured = sum(1 for r in items if r.status == "measured")
    errors = sum(1 for r in items if r.status == "error")
    unknown = total - measured - errors
    if items:
        avg_bps = sum(r.bitrate_bps for r in items) / len(items)
        max_bps = max(r.bitrate_bps for r in items)
    else:
        avg_bps = 0.0
        max_bps = 0.0
    return {
        "total": total,
        "sampled": total,
        "measured": measured,
        "errors": errors,
        "unknown": unknown,
        "ok": total == 0 or errors == 0,
        "success": total == 0 or errors == 0,
        "avg_bitrate_bps": round(avg_bps, 2),
        "avg_bitrate_kbps": round(avg_bps / 1000.0, 3),
        "max_bitrate_bps": round(max_bps, 2),
        "max_bitrate_kbps": round(max_bps / 1000.0, 3),
    }


summary = get_summary
get_stats = get_summary
stats = get_summary


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
    if isinstance(state, BitrateResult):
        return state.to_dict()
    if isinstance(state, dict):
        return state
    return {"value": str(state)}


__all__ = [
    "BitrateResult",
    "BitrateSample",
    "BitrateStatus",
    "SampleResult",
    "DEFAULT_SAMPLE_SECONDS",
    "DEFAULT_TIMEOUT_SECONDS",
    "sample_url",
    "sample_camera",
    "asample_url",
    "asample_camera",
    "get_status",
    "list_results",
    "get_summary",
    "clear",
    "remove",
]
