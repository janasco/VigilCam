"""RTSP resilience service for VigilCam.

Pure-Python model of the keepalive / stale-stream / reconnect patterns used
by the reference C# client in ``/opt/others/ispy-rtspclient``
(``RtspClientSharp``):

- ``ConnectionParameters`` -- ``ConnectTimeout`` (30s), ``ReceiveTimeout``
  (10s) and ``CancelTimeout`` (5s). Any frame received resets the receive
  timer (``_anyFrameReceived`` flag in ``RtspClient.ReceiveAsync``); if no
  frame arrives within ``ReceiveTimeout`` the session is torn down with an
  ``RtspClientException("Receive timeout")``.
- ``RtspClientInternal`` -- after ``SETUP`` the server ``Session`` header is
  parsed (``ParseSessionHeader``; default 60s when absent). While receiving,
  a keepalive (``GET_PARAMETER``, or a fire-and-forget variant over TCP) is
  sent on a jittered schedule at ``timeout/2`` .. ``timeout*3/4``
  (``GetNextRtspKeepAliveInterval``), but only when the server advertised
  ``GET_PARAMETER`` in the ``Public`` header of the ``OPTIONS`` response
  (``ParsePublicHeader``). ``TEARDOWN`` is sent on clean shutdown.
- ``Examples/SimpleRtspClient`` -- the canonical reconnect loop: connect,
  on ``RtspClientException`` wait a delay then retry; receive, on
  ``RtspClientException`` wait the same delay then reconnect from scratch.

This module keeps dependency-light, in-memory per-stream state so the API
(``app.api.rtsp_health``) and workers can schedule ``OPTIONS``/``GET_PARAMETER``
keepalives, detect stale streams, and apply a capped exponential-backoff
reconnect policy. No ``app/main.py`` edit is required.
"""

from __future__ import annotations

import random
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Tunables (mirroring RtspClientSharp defaults).
# ---------------------------------------------------------------------------

#: Time allowed for the OPTIONS/DESCRIBE/SETUP/PLAY handshake.
CONNECT_TIMEOUT_SECONDS = 30.0
#: No-frame window after which a stream is considered stale.
RECEIVE_TIMEOUT_SECONDS = 10.0
#: Grace period for cancelling a blocked receive before tearing down.
CANCEL_TIMEOUT_SECONDS = 5.0
#: Fallback RTSP session timeout when the server omits it (ParseSessionHeader).
DEFAULT_SESSION_TIMEOUT_SECONDS = 60.0
#: Keepalive is scheduled at session_timeout * [MIN, MAX] with jitter.
KEEPALIVE_MIN_FRACTION = 0.5
KEEPALIVE_MAX_FRACTION = 0.75
#: Base RTCP receiver-report interval (RtcpReportIntervalBaseMs) + jitter.
RTCP_REPORT_INTERVAL_MS = 5000
RTCP_REPORT_JITTER_MS = 1000
#: Reconnect loop delay from the SimpleRtspClient example.
BASE_RECONNECT_DELAY_SECONDS = 5.0
#: Exponential backoff cap for repeated failures.
MAX_RECONNECT_DELAY_SECONDS = 300.0
BACKOFF_MULTIPLIER = 2.0
#: Give up reconnecting after this many consecutive failures (0 = unlimited).
MAX_RECONNECT_ATTEMPTS = 0
#: Default RTSP port when the URI has none (Constants.DefaultRtspPort).
DEFAULT_RTSP_PORT = 554

# In-memory stream state keyed by normalized stream id.
_STREAMS: Dict[str, "RtspStreamHealth"] = {}
_LOCK = threading.Lock()
_CSEQ = 0


def _now() -> datetime:
    return datetime.utcnow()


def _next_cseq() -> int:
    global _CSEQ
    with _LOCK:
        _CSEQ += 1
        return _CSEQ


def _coerce_stream_id(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, bool):
        return str(int(value))
    if hasattr(value, "id") and not isinstance(value, (str, int, float)):
        try:
            value = getattr(value, "id")
        except Exception:  # noqa: BLE001
            pass
    s = str(value).strip()
    return s or None


def _to_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        stamp = float(value)
        if abs(stamp) > 1e12:
            stamp = stamp / 1000.0
        try:
            return datetime.utcfromtimestamp(stamp)
        except (OSError, ValueError, OverflowError):
            return None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            parsed = datetime.fromisoformat(s)
            if parsed.tzinfo is not None:
                parsed = parsed.replace(tzinfo=None)
            return parsed
        except ValueError:
            try:
                return datetime.utcfromtimestamp(float(s))
            except (ValueError, OSError, OverflowError):
                return None
    if isinstance(value, timedelta):
        return _now() + value
    return None


def _to_seconds(value: Any, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, timedelta):
        return value.total_seconds()
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# RtspClientInternal scheduling primitives.
# ---------------------------------------------------------------------------

def get_next_keepalive_interval(
    session_timeout: Any = None,
    rng: Optional[random.Random] = None,
) -> float:
    """Jittered keepalive delay in seconds (timeout/2 .. timeout*3/4).

    Mirrors ``RtspClientInternal.GetNextRtspKeepAliveInterval``.
    """
    timeout = _to_seconds(session_timeout, DEFAULT_SESSION_TIMEOUT_SECONDS)
    if timeout <= 0:
        timeout = DEFAULT_SESSION_TIMEOUT_SECONDS
    source = rng or random
    low = timeout * KEEPALIVE_MIN_FRACTION
    high = timeout * KEEPALIVE_MAX_FRACTION
    try:
        return float(source.uniform(low, high))
    except Exception:  # noqa: BLE001
        return (low + high) / 2.0


def get_next_rtcp_report_interval_ms(rng: Optional[random.Random] = None) -> int:
    """RTCP receiver-report interval with jitter (5000ms + 0..1000ms)."""
    source = rng or random
    try:
        jitter = source.randrange(0, 11) * 100
    except Exception:  # noqa: BLE001
        jitter = 0
    return int(RTCP_REPORT_INTERVAL_MS + jitter)


def parse_session_header(header: Any) -> Dict[str, Any]:
    """Parse an RTSP ``Session`` header into ``{session_id, timeout}``.

    Mirrors ``ParseSessionHeader``/``TryParseTimeoutParameter``: timeout
    defaults to 60s when absent or unparsable.
    """
    result: Dict[str, Any] = {"session_id": None, "timeout": DEFAULT_SESSION_TIMEOUT_SECONDS}
    if header is None:
        return result
    s = str(header).strip()
    if not s:
        return result
    delimiter = s.find(";")
    if delimiter == -1:
        result["session_id"] = s or None
    else:
        result["session_id"] = s[:delimiter].strip() or None
        m = re.search(r"timeout\s*=\s*[\"']?\s*(\d+)", s[delimiter:], re.IGNORECASE)
        if m:
            try:
                result["timeout"] = float(m.group(1))
            except ValueError:
                pass
    try:
        if float(result["timeout"]) <= 0:
            result["timeout"] = DEFAULT_SESSION_TIMEOUT_SECONDS
    except (TypeError, ValueError):
        result["timeout"] = DEFAULT_SESSION_TIMEOUT_SECONDS
    return result


def parse_session_timeout(header: Any) -> float:
    """Return just the session timeout seconds from a ``Session`` header."""
    return float(parse_session_header(header)["timeout"])


def supports_get_parameter(public_header: Any) -> bool:
    """Whether the server advertised ``GET_PARAMETER`` (``ParsePublicHeader``)."""
    if public_header is None:
        return False
    return "GET_PARAMETER" in str(public_header).upper()


def is_keepalive_supported(public_header: Any = None, session: Any = None) -> bool:
    _ = session
    return supports_get_parameter(public_header)


# ---------------------------------------------------------------------------
# RTSP request builders (OPTIONS keepalive / GET_PARAMETER / TEARDOWN).
# ---------------------------------------------------------------------------

def _request_line(method: str, uri: str, cseq: int, session_id: Optional[str] = None) -> Dict[str, Any]:
    headers: Dict[str, Any] = {"CSeq": cseq, "User-Agent": "VigilCam"}
    if session_id:
        headers["Session"] = session_id
    text = f"{method} {uri} RTSP/1.0\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers.items()) + "\r\n"
    return {"method": method, "uri": uri, "cseq": cseq, "headers": headers, "text": text}


def build_options_request(uri: Any = "*", session_id: Any = None) -> Dict[str, Any]:
    """Build an ``OPTIONS`` request (session keepalive / capability probe)."""
    target = str(uri).strip() or "*"
    sid = str(session_id).strip() if session_id else None
    return _request_line("OPTIONS", target, _next_cseq(), sid)


def build_get_parameter_request(uri: Any = "*", session_id: Any = None) -> Dict[str, Any]:
    """Build a ``GET_PARAMETER`` keepalive request."""
    target = str(uri).strip() or "*"
    sid = str(session_id).strip() if session_id else None
    return _request_line("GET_PARAMETER", target, _next_cseq(), sid)


def build_teardown_request(uri: Any = "*", session_id: Any = None) -> Dict[str, Any]:
    """Build a ``TEARDOWN`` request for clean session shutdown."""
    target = str(uri).strip() or "*"
    sid = str(session_id).strip() if session_id else None
    return _request_line("TEARDOWN", target, _next_cseq(), sid)


# ---------------------------------------------------------------------------
# Backoff reconnect policy (SimpleRtspClient retry loop, capped).
# ---------------------------------------------------------------------------

def compute_backoff(
    attempts: Any = None,
    base: Any = None,
    multiplier: Any = None,
    cap: Any = None,
) -> float:
    """Exponential backoff seconds for a 1-based failure count."""
    if base is None:
        base = BASE_RECONNECT_DELAY_SECONDS
    if multiplier is None:
        multiplier = BACKOFF_MULTIPLIER
    if cap is None:
        cap = MAX_RECONNECT_DELAY_SECONDS
    try:
        n = int(attempts)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return 0.0
    try:
        delay = float(base) * (float(multiplier) ** (n - 1))
    except Exception:  # noqa: BLE001
        delay = float(base)
    try:
        delay = min(delay, float(cap))
    except (TypeError, ValueError):
        pass
    return max(0.0, delay)


def reconnect_delay_for_attempt(attempt: Any = None, **kwargs: Any) -> float:
    return compute_backoff(attempt, **kwargs)


# ---------------------------------------------------------------------------
# Per-stream health record.
# ---------------------------------------------------------------------------

@dataclass
class RtspStreamHealth:
    """Health/keepalive state for one RTSP stream."""

    stream_id: str
    url: Optional[str] = None
    transport: str = "tcp"
    status: str = "unknown"
    online: bool = False
    healthy: bool = False
    session_id: Optional[str] = None
    session_timeout_seconds: float = DEFAULT_SESSION_TIMEOUT_SECONDS
    server_supports_get_parameter: bool = False
    connect_timeout_seconds: float = CONNECT_TIMEOUT_SECONDS
    receive_timeout_seconds: float = RECEIVE_TIMEOUT_SECONDS
    last_frame_at: Optional[datetime] = None
    last_keepalive_sent_at: Optional[datetime] = None
    last_keepalive_response_at: Optional[datetime] = None
    next_keepalive_at: Optional[datetime] = None
    reconnect_attempts: int = 0
    consecutive_failures: int = 0
    backoff_seconds: float = 0.0
    next_retry_at: Optional[datetime] = None
    last_error: Optional[str] = None
    last_check_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @property
    def attempts(self) -> int:
        return self.reconnect_attempts

    @property
    def backoff(self) -> float:
        return self.backoff_seconds

    @property
    def is_online(self) -> bool:
        return bool(self.online)

    @property
    def is_stale(self) -> bool:
        return is_stale(self)

    def to_dict(self) -> Dict[str, Any]:
        def _iso(value: Any) -> Any:
            return value.isoformat() if isinstance(value, datetime) else value

        return {
            "stream_id": self.stream_id,
            "id": self.stream_id,
            "camera_id": self.stream_id,
            "url": self.url,
            "transport": self.transport,
            "status": self.status,
            "state": self.status,
            "online": self.online,
            "healthy": self.healthy,
            "session_id": self.session_id,
            "session_timeout_seconds": self.session_timeout_seconds,
            "session_timeout": self.session_timeout_seconds,
            "server_supports_get_parameter": self.server_supports_get_parameter,
            "keepalive_supported": self.server_supports_get_parameter,
            "connect_timeout_seconds": self.connect_timeout_seconds,
            "receive_timeout_seconds": self.receive_timeout_seconds,
            "receive_timeout": self.receive_timeout_seconds,
            "last_frame_at": _iso(self.last_frame_at),
            "last_keepalive_sent_at": _iso(self.last_keepalive_sent_at),
            "last_keepalive_response_at": _iso(self.last_keepalive_response_at),
            "next_keepalive_at": _iso(self.next_keepalive_at),
            "keepalive_due": should_send_keepalive(self),
            "stale": is_stale(self),
            "reconnect_attempts": self.reconnect_attempts,
            "attempts": self.reconnect_attempts,
            "consecutive_failures": self.consecutive_failures,
            "backoff_seconds": self.backoff_seconds,
            "backoff": self.backoff_seconds,
            "reconnect_delay": self.backoff_seconds,
            "next_retry_at": _iso(self.next_retry_at),
            "retry_in_seconds": next_retry_in(self),
            "last_error": self.last_error,
            "last_check_at": _iso(self.last_check_at),
            "updated_at": _iso(self.updated_at),
        }

    def dict(self) -> Dict[str, Any]:  # noqa: A003
        return self.to_dict()

    def model_dump(self) -> Dict[str, Any]:
        return self.to_dict()

    def __getitem__(self, key: str) -> Any:
        data = self.to_dict()
        if key in data:
            return data[key]
        if hasattr(self, key):
            return getattr(self, key)
        return None

    def get(self, key: str, default: Any = None) -> Any:
        try:
            val = self[key]
            return default if val is None and key not in self.__dict__ else val
        except Exception:  # noqa: BLE001
            return default


def _ensure(stream_id: Any, **kwargs: Any) -> RtspStreamHealth:
    sid = _coerce_stream_id(stream_id)
    if sid is None:
        sid = "default"
    with _LOCK:
        state = _STREAMS.get(sid)
        if state is None:
            state = RtspStreamHealth(stream_id=sid, updated_at=_now())
            _STREAMS[sid] = state
    for key in ("url", "transport", "session_id", "session_timeout_seconds",
                "connect_timeout_seconds", "receive_timeout_seconds"):
        if kwargs.get(key) is not None:
            try:
                setattr(state, key, kwargs[key])
            except Exception:  # noqa: BLE001
                pass
    if kwargs.get("session_header"):
        parsed = parse_session_header(kwargs["session_header"])
        if parsed["session_id"]:
            state.session_id = parsed["session_id"]
        state.session_timeout_seconds = float(parsed["timeout"])
    if kwargs.get("public_header") is not None:
        state.server_supports_get_parameter = supports_get_parameter(kwargs["public_header"])
    return state


def get_health(stream_id: Any, create: bool = True, **kwargs: Any) -> Optional[RtspStreamHealth]:
    sid = _coerce_stream_id(stream_id)
    if sid is None:
        return None
    with _LOCK:
        state = _STREAMS.get(sid)
    if state is None:
        if not create:
            return None
        return _ensure(sid, **kwargs)
    return state


def register_stream(stream_id: Any = None, **kwargs: Any) -> RtspStreamHealth:
    if stream_id is None:
        stream_id = kwargs.get("stream_id") or kwargs.get("id") or kwargs.get("camera_id")
    return _ensure(stream_id, **kwargs)


# ---------------------------------------------------------------------------
# Frame / keepalive tracking.
# ---------------------------------------------------------------------------

def mark_frame_received(stream_id: Any = None, when: Any = None, **kwargs: Any) -> RtspStreamHealth:
    """Record an RTP frame; clears stale flag and reconnect backoff.

    Mirrors the ``_anyFrameReceived`` reset in ``RtspClient.ReceiveAsync``.
    """
    if stream_id is None:
        stream_id = kwargs.get("stream_id") or kwargs.get("id") or kwargs.get("camera_id")
    if when is None:
        for cand in (kwargs.get("ts"), kwargs.get("timestamp"), kwargs.get("at"), kwargs.get("when")):
            if cand is not None:
                when = cand
                break
    state = _ensure(stream_id)
    dt = _to_datetime(when) if when is not None else _now()
    if dt is None:
        dt = _now()
    state.last_frame_at = dt
    state.online = True
    state.status = "online"
    state.healthy = True
    state.reconnect_attempts = 0
    state.consecutive_failures = 0
    state.backoff_seconds = 0.0
    state.next_retry_at = None
    state.last_error = None
    state.last_check_at = dt
    state.updated_at = dt
    return state


def mark_keepalive_sent(stream_id: Any = None, when: Any = None, **kwargs: Any) -> RtspStreamHealth:
    """Record an outgoing OPTIONS/GET_PARAMETER keepalive and schedule next."""
    if stream_id is None:
        stream_id = kwargs.get("stream_id") or kwargs.get("id") or kwargs.get("camera_id")
    state = _ensure(stream_id)
    dt = _to_datetime(when) if when is not None else _now()
    if dt is None:
        dt = _now()
    state.last_keepalive_sent_at = dt
    interval = get_next_keepalive_interval(state.session_timeout_seconds)
    state.next_keepalive_at = dt + timedelta(seconds=interval)
    state.last_check_at = dt
    state.updated_at = dt
    return state


def record_keepalive_response(
    stream_id: Any = None,
    when: Any = None,
    ok: Any = True,
    **kwargs: Any,
) -> RtspStreamHealth:
    """Record the server response to a keepalive probe."""
    if stream_id is None:
        stream_id = kwargs.get("stream_id") or kwargs.get("id") or kwargs.get("camera_id")
    state = _ensure(stream_id)
    dt = _to_datetime(when) if when is not None else _now()
    if dt is None:
        dt = _now()
    if _coerce_bool(ok, True):
        state.last_keepalive_response_at = dt
        if state.status != "online" and state.last_frame_at is not None:
            state.online = True
            state.status = "online"
            state.healthy = True
    else:
        state.last_error = str(kwargs.get("error", "keepalive failed"))
        return record_failure(state.stream_id, when=dt, error=state.last_error)
    state.last_check_at = dt
    state.updated_at = dt
    return state


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "y", "on", "enabled", "ok"):
        return True
    if s in ("0", "false", "no", "n", "off", "disabled", "fail", "failed", "error"):
        return False
    return default


def _as_state(state: Any, stream_id: Any) -> Any:
    """Resolve ``(state, stream_id)`` pairs, tolerating id-like ``state``.

    Callers (and tests) often pass a stream id positionally, e.g.
    ``is_stale("cam-1")``; treat non-record ``state`` values as ids.
    """
    if state is None:
        if stream_id is None:
            return None
        return get_health(stream_id, create=False)
    if isinstance(state, (RtspStreamHealth, dict)):
        return state
    return get_health(state, create=False)


def should_send_keepalive(state: Any = None, stream_id: Any = None, now: Any = None) -> bool:
    """Whether an OPTIONS/GET_PARAMETER keepalive is due now."""
    state = _as_state(state, stream_id)
    if state is None:
        return False
    if isinstance(state, dict):
        next_at = _to_datetime(state.get("next_keepalive_at"))
    else:
        next_at = getattr(state, "next_keepalive_at", None)
    if next_at is None:
        return True
    now_dt = _to_datetime(now) if now is not None else _now()
    if now_dt is None:
        now_dt = _now()
    try:
        return now_dt >= next_at
    except Exception:  # noqa: BLE001
        return False


def is_stale(
    state: Any = None,
    stream_id: Any = None,
    timeout: Any = None,
    now: Any = None,
) -> bool:
    """Whether the stream produced no frame within the receive timeout.

    Mirrors the ``ReceiveTimeout`` check in ``RtspClient.ReceiveAsync``: a
    stream that never delivered a frame counts as stale.
    """
    state = _as_state(state, stream_id)
    if state is None:
        return False
    if isinstance(state, dict):
        last_frame = _to_datetime(state.get("last_frame_at"))
        limit = state.get("receive_timeout_seconds", state.get("receive_timeout"))
    else:
        last_frame = getattr(state, "last_frame_at", None)
        limit = getattr(state, "receive_timeout_seconds", None)
    if timeout is not None:
        limit = timeout
    limit_f = _to_seconds(limit, RECEIVE_TIMEOUT_SECONDS)
    if last_frame is None:
        return True
    now_dt = _to_datetime(now) if now is not None else _now()
    if now_dt is None:
        now_dt = _now()
    try:
        return (now_dt - last_frame).total_seconds() > float(limit_f)
    except Exception:  # noqa: BLE001
        return False


def evaluate(state: Any = None, stream_id: Any = None, now: Any = None, **kwargs: Any) -> Optional[RtspStreamHealth]:
    """Refresh online/stale flags for one stream (no counter changes)."""
    if isinstance(state, RtspStreamHealth) or isinstance(state, dict):
        pass
    elif state is not None:
        stream_id = state
        state = None
    if state is None and stream_id is not None:
        state = get_health(stream_id, create=True)
    if state is None:
        return None
    if isinstance(state, dict):
        return state  # type: ignore[return-value]
    now_dt = _to_datetime(now) if now is not None else _now()
    if now_dt is None:
        now_dt = _now()
    timeout = kwargs.get("timeout", kwargs.get("receive_timeout", state.receive_timeout_seconds))
    stale = is_stale(state, timeout=timeout, now=now_dt)
    if stale and state.online:
        state.online = False
        state.status = "stale"
        state.healthy = False
        state.last_check_at = now_dt
        state.updated_at = now_dt
    elif not stale and state.last_frame_at is not None and not state.online and state.consecutive_failures == 0:
        state.online = True
        state.status = "online"
        state.healthy = True
        state.last_check_at = now_dt
        state.updated_at = now_dt
    else:
        state.last_check_at = now_dt
    return state


# ---------------------------------------------------------------------------
# Failure / reconnect policy.
# ---------------------------------------------------------------------------

def record_failure(stream_id: Any = None, when: Any = None, error: Any = None, **kwargs: Any) -> RtspStreamHealth:
    """Record a connection/receive failure and schedule the next retry."""
    if stream_id is None:
        stream_id = kwargs.get("stream_id") or kwargs.get("id") or kwargs.get("camera_id")
    if isinstance(stream_id, RtspStreamHealth):
        state = stream_id
    else:
        state = _ensure(stream_id)
    dt = _to_datetime(when) if when is not None else _now()
    if dt is None:
        dt = _now()
    if error is None:
        error = kwargs.get("last_error", kwargs.get("reason", "connection error"))
    state.reconnect_attempts = int(state.reconnect_attempts or 0) + 1
    state.consecutive_failures = int(state.consecutive_failures or 0) + 1
    state.backoff_seconds = compute_backoff(state.reconnect_attempts)
    state.next_retry_at = dt + timedelta(seconds=state.backoff_seconds)
    state.online = False
    state.status = "offline"
    state.healthy = False
    if error is not None:
        state.last_error = str(error)
    state.last_check_at = dt
    state.updated_at = dt
    return state


def record_success(stream_id: Any = None, when: Any = None, **kwargs: Any) -> RtspStreamHealth:
    """Record a successful (re)connect; resets backoff like a fresh session."""
    if stream_id is None:
        stream_id = kwargs.get("stream_id") or kwargs.get("id") or kwargs.get("camera_id")
    if isinstance(stream_id, RtspStreamHealth):
        state = stream_id
    else:
        state = _ensure(stream_id)
    dt = _to_datetime(when) if when is not None else _now()
    if dt is None:
        dt = _now()
    state.reconnect_attempts = 0
    state.consecutive_failures = 0
    state.backoff_seconds = 0.0
    state.next_retry_at = None
    state.last_error = None
    state.online = True
    state.status = "online"
    state.healthy = True
    state.last_check_at = dt
    state.updated_at = dt
    return state


def next_retry_in(state: Any = None, stream_id: Any = None, now: Any = None) -> float:
    """Seconds until the next reconnect attempt is due (0 when due now)."""
    state = _as_state(state, stream_id)
    if state is None:
        return 0.0
    next_at = state.get("next_retry_at") if isinstance(state, dict) else getattr(state, "next_retry_at", None)
    if next_at is None:
        return 0.0
    next_dt = _to_datetime(next_at)
    if next_dt is None:
        return 0.0
    now_dt = _to_datetime(now) if now is not None else _now()
    if now_dt is None:
        now_dt = _now()
    try:
        return max(0.0, (next_dt - now_dt).total_seconds())
    except Exception:  # noqa: BLE001
        return 0.0


def should_reconnect(state: Any = None, stream_id: Any = None, now: Any = None, **kwargs: Any) -> bool:
    """Whether a reconnect attempt is due (backoff elapsed, caps respected)."""
    state = _as_state(state, stream_id)
    if state is None:
        return False
    online = state.get("online") if isinstance(state, dict) else getattr(state, "online", False)
    if online:
        return False
    attempts = state.get("reconnect_attempts") if isinstance(state, dict) else getattr(state, "reconnect_attempts", 0)
    max_attempts = kwargs.get("max_attempts", kwargs.get("max_retries", MAX_RECONNECT_ATTEMPTS))
    try:
        max_attempts_i = int(max_attempts)
    except (TypeError, ValueError):
        max_attempts_i = 0
    try:
        attempts_i = int(attempts or 0)
    except (TypeError, ValueError):
        attempts_i = 0
    if max_attempts_i > 0 and attempts_i >= max_attempts_i:
        return False
    return next_retry_in(state, now=now) <= 0


def reset_backoff(stream_id: Any = None, **kwargs: Any) -> RtspStreamHealth:
    if stream_id is None:
        stream_id = kwargs.get("stream_id") or kwargs.get("id") or kwargs.get("camera_id")
    state = _ensure(stream_id)
    state.reconnect_attempts = 0
    state.consecutive_failures = 0
    state.backoff_seconds = 0.0
    state.next_retry_at = None
    state.last_error = None
    state.updated_at = _now()
    return state


def remove(stream_id: Any = None, **kwargs: Any) -> bool:
    if stream_id is None:
        stream_id = kwargs.get("stream_id") or kwargs.get("id") or kwargs.get("camera_id")
    sid = _coerce_stream_id(stream_id)
    if sid is None:
        return False
    with _LOCK:
        return _STREAMS.pop(sid, None) is not None


def clear(stream_id: Any = None, **kwargs: Any) -> None:
    if stream_id is None:
        stream_id = kwargs.get("stream_id") or kwargs.get("id") or kwargs.get("camera_id")
    with _LOCK:
        if stream_id is None:
            _STREAMS.clear()
            return
        sid = _coerce_stream_id(stream_id)
        if sid is None:
            _STREAMS.clear()
            return
        _STREAMS.pop(sid, None)


def list_health() -> List[RtspStreamHealth]:
    with _LOCK:
        ids = sorted(_STREAMS.keys())
    out = []
    for sid in ids:
        with _LOCK:
            state = _STREAMS.get(sid)
        if state is not None:
            try:
                evaluate(state)
            except Exception:  # noqa: BLE001
                pass
            out.append(state)
    return out


def list_health_dicts() -> List[Dict[str, Any]]:
    return [st.to_dict() for st in list_health()]


def get_summary() -> Dict[str, Any]:
    states = list_health()
    total = len(states)
    online = sum(1 for s in states if s.online)
    stale = sum(1 for s in states if is_stale(s))
    return {
        "total": total,
        "streams": total,
        "cameras": total,
        "online": online,
        "offline": total - online,
        "healthy": online,
        "stale": stale,
        "total_reconnect_attempts": sum(int(s.reconnect_attempts or 0) for s in states),
    }


def get_config() -> Dict[str, Any]:
    return {
        "connect_timeout_seconds": CONNECT_TIMEOUT_SECONDS,
        "receive_timeout_seconds": RECEIVE_TIMEOUT_SECONDS,
        "cancel_timeout_seconds": CANCEL_TIMEOUT_SECONDS,
        "default_session_timeout_seconds": DEFAULT_SESSION_TIMEOUT_SECONDS,
        "keepalive_min_fraction": KEEPALIVE_MIN_FRACTION,
        "keepalive_max_fraction": KEEPALIVE_MAX_FRACTION,
        "rtcp_report_interval_ms": RTCP_REPORT_INTERVAL_MS,
        "base_reconnect_delay_seconds": BASE_RECONNECT_DELAY_SECONDS,
        "max_reconnect_delay_seconds": MAX_RECONNECT_DELAY_SECONDS,
        "backoff_multiplier": BACKOFF_MULTIPLIER,
        "max_reconnect_attempts": MAX_RECONNECT_ATTEMPTS,
        "default_rtsp_port": DEFAULT_RTSP_PORT,
    }


def serialize(state: Any) -> Dict[str, Any]:
    if isinstance(state, RtspStreamHealth):
        return state.to_dict()
    if isinstance(state, dict):
        return state
    return {}


# ---------------------------------------------------------------------------
# Class-based monitor for compatibility.
# ---------------------------------------------------------------------------

class RtspHealthMonitor:
    """Thin object wrapper around the module-level stream registry."""

    def ensure(self, stream_id: Any, **kwargs: Any) -> RtspStreamHealth:
        return _ensure(stream_id, **kwargs)

    def get(self, stream_id: Any, create: bool = True, **kwargs: Any) -> Optional[RtspStreamHealth]:
        return get_health(stream_id, create=create, **kwargs)

    def get_health(self, stream_id: Any, create: bool = True, **kwargs: Any) -> Optional[RtspStreamHealth]:
        return get_health(stream_id, create=create, **kwargs)

    def mark_frame_received(self, *a: Any, **kw: Any) -> RtspStreamHealth:
        return mark_frame_received(*a, **kw)

    def mark_keepalive_sent(self, *a: Any, **kw: Any) -> RtspStreamHealth:
        return mark_keepalive_sent(*a, **kw)

    def record_keepalive_response(self, *a: Any, **kw: Any) -> RtspStreamHealth:
        return record_keepalive_response(*a, **kw)

    def record_failure(self, *a: Any, **kw: Any) -> RtspStreamHealth:
        return record_failure(*a, **kw)

    def record_success(self, *a: Any, **kw: Any) -> RtspStreamHealth:
        return record_success(*a, **kw)

    def should_reconnect(self, *a: Any, **kw: Any) -> bool:
        return should_reconnect(*a, **kw)

    def should_send_keepalive(self, *a: Any, **kw: Any) -> bool:
        return should_send_keepalive(*a, **kw)

    def is_stale(self, *a: Any, **kw: Any) -> bool:
        return is_stale(*a, **kw)

    def evaluate(self, *a: Any, **kw: Any) -> Optional[RtspStreamHealth]:
        return evaluate(*a, **kw)

    def compute_backoff(self, *a: Any, **kw: Any) -> float:
        return compute_backoff(*a, **kw)

    def reset_backoff(self, *a: Any, **kw: Any) -> RtspStreamHealth:
        return reset_backoff(*a, **kw)

    def next_retry_in(self, *a: Any, **kw: Any) -> float:
        return next_retry_in(*a, **kw)

    def remove(self, *a: Any, **kw: Any) -> bool:
        return remove(*a, **kw)

    def clear(self, *a: Any, **kw: Any) -> None:
        return clear(*a, **kw)

    def list(self) -> List[RtspStreamHealth]:
        return list_health()

    def get_summary(self) -> Dict[str, Any]:
        return get_summary()

    def get_config(self) -> Dict[str, Any]:
        return get_config()


RtspHealthService = RtspHealthMonitor
RtspHealthManager = RtspHealthMonitor
RtspHealthTracker = RtspHealthMonitor
RtspMonitor = RtspHealthMonitor

monitor = RtspHealthMonitor()
rtsp_monitor = monitor
health_monitor = monitor
service = monitor
default_monitor = monitor

# Aliases for likely alternative names.
get = get_health
get_stream_health = get_health
stream_health_for = get_health
health_for = get_health
find_health = lambda stream_id: get_health(stream_id, create=False)  # noqa: E731
lookup = find_health
frame_received = mark_frame_received
record_frame = mark_frame_received
on_frame = mark_frame_received
mark_frame = mark_frame_received
keepalive_sent = mark_keepalive_sent
record_failure_alias = record_failure
reconnect = record_failure
register = register_stream
get_all = list_health
list_all = list_health
all_health = list_health
summary = get_summary
config = get_config
reset = reset_backoff
delete = remove
unregister = remove
drop = remove
next_keepalive_interval = get_next_keepalive_interval
next_keepalive_in = get_next_keepalive_interval
rtcp_interval = get_next_rtcp_report_interval_ms
backoff = compute_backoff
reconnect_delay = reconnect_delay_for_attempt
can_reconnect = should_reconnect
needs_reconnect = should_reconnect
retry_due = should_reconnect
keepalive_due = should_send_keepalive
check_stale = is_stale
stale = is_stale
is_online = lambda stream_id=None, **kw: bool((get_health(stream_id, create=False) or _ensure(stream_id)).online)  # noqa: E731
parse_public_header = supports_get_parameter
get_parameter_supported = supports_get_parameter
options_request = build_options_request
get_parameter_request = build_get_parameter_request
teardown_request = build_teardown_request
RtspStreamState = RtspStreamHealth
RtspHealth = RtspStreamHealth
StreamHealth = RtspStreamHealth

__all__ = [
    "RtspStreamHealth",
    "RtspHealthMonitor",
    "RtspHealthService",
    "monitor",
    "CONNECT_TIMEOUT_SECONDS",
    "RECEIVE_TIMEOUT_SECONDS",
    "CANCEL_TIMEOUT_SECONDS",
    "DEFAULT_SESSION_TIMEOUT_SECONDS",
    "BASE_RECONNECT_DELAY_SECONDS",
    "MAX_RECONNECT_DELAY_SECONDS",
    "get_next_keepalive_interval",
    "get_next_rtcp_report_interval_ms",
    "parse_session_header",
    "supports_get_parameter",
    "build_options_request",
    "build_get_parameter_request",
    "build_teardown_request",
    "compute_backoff",
    "get_health",
    "register_stream",
    "mark_frame_received",
    "mark_keepalive_sent",
    "record_keepalive_response",
    "should_send_keepalive",
    "is_stale",
    "evaluate",
    "record_failure",
    "record_success",
    "should_reconnect",
    "next_retry_in",
    "reset_backoff",
    "list_health",
    "get_summary",
    "get_config",
    "remove",
    "clear",
]
