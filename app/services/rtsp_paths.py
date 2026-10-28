"""RTSP path brute-forcer for the user's own cameras.

Discovery (``app.services.network_discovery``) only scans subnets/ports:

- TCP ``connect`` checks for open HTTP/RTSP ports,
- an HTTP ``GET /`` probe plus a few ONVIF ``POST`` probes,
- and reports a generic ``rtsp://<ip>:<port>/`` source.

It deliberately never iterates over RTSP URL paths on a host. When the
user already knows the camera host (their own hardware) but not the exact
stream path (``/live`` vs ``/Streaming/Channels/101`` vs ...), this module
tries a **capped** list of common paths with explicit ``DESCRIBE`` probing.

Safety / scope:

- explicit single-host only (no subnet scanning here);
- path count capped by ``MAX_PATHS``;
- per-path timeout capped by ``MAX_TIMEOUT_SECONDS``;
- stdlib only (``socket`` + ``base64``) with an injectable ``describer``
  callback for deterministic unit tests;
- never raises on bad input; empty host -> ``[]`` / ``status="unknown"``.
"""

from __future__ import annotations

import asyncio
import base64
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

# ---------------------------------------------------------------------------
# Tunables (timeout caps).
# ---------------------------------------------------------------------------

#: Default per-path DESCRIBE timeout (seconds).
DEFAULT_TIMEOUT_SECONDS = 2.0
#: Hard cap for any per-path timeout (seconds).
MAX_TIMEOUT_SECONDS = 10.0
#: Hard cap for how many paths a single scan may try.
MAX_PATHS = 64
#: Default RTSP port when none is given.
DEFAULT_RTSP_PORT = 554
#: Default concurrency for the async scanner.
DEFAULT_CONCURRENCY = 8
#: User-Agent sent on DESCRIBE requests.
USER_AGENT = "VigilCam"
#: Max bytes read from a DESCRIBE response.
MAX_RESPONSE_BYTES = 16 * 1024

# ---------------------------------------------------------------------------
# Common RTSP path list (vendor defaults gathered from iSpy/Agent catalogs,
# Hikvision/Dahua/Axis/Uniview defaults and generic firmware paths).
# ---------------------------------------------------------------------------

COMMON_RTSP_PATHS: tuple[str, ...] = (
    "/",
    "/live",
    "/live.sdp",
    "/live1.sdp",
    "/live2.sdp",
    "/live/ch00_0",
    "/live/ch00_1",
    "/live0",
    "/live1",
    "/h264",
    "/h264.sdp",
    "/h264/ch1/main/av_stream",
    "/h264/ch1/sub/av_stream",
    "/Streaming/Channels/101",
    "/Streaming/Channels/102",
    "/Streaming/Channels/201",
    "/cam/realmonitor?channel=1&subtype=0",
    "/cam/realmonitor?channel=1&subtype=1",
    "/axis-media/media.amp",
    "/axis-media/media.amp?videocodec=h264",
    "/onvif1",
    "/onvif2",
    "/media.amp",
    "/11",
    "/12",
    "/ch0",
    "/stream1",
    "/stream2",
    "/videoMain",
    "/mpeg4",
    "/av0_0",
    "/cam1/h264",
)

#: Alias kept for callers that expect a list.
DEFAULT_PATHS: tuple[str, ...] = COMMON_RTSP_PATHS
COMMON_PATHS = COMMON_RTSP_PATHS
RTSP_PATHS = COMMON_RTSP_PATHS
PATH_LIST = COMMON_RTSP_PATHS

# In-memory last-scan results keyed by full RTSP URL.
_RESULTS: Dict[str, "RtspPathResult"] = {}


def _now() -> datetime:
    return datetime.utcnow()


def _coerce_timeout(value: Any) -> float:
    try:
        t = float(value)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SECONDS
    if not (t > 0):
        return DEFAULT_TIMEOUT_SECONDS
    return min(t, MAX_TIMEOUT_SECONDS)


def _coerce_port(value: Any) -> int:
    try:
        p = int(value)
    except (TypeError, ValueError):
        return DEFAULT_RTSP_PORT
    if 1 <= p <= 65535:
        return p
    return DEFAULT_RTSP_PORT


def _normalize_path(path: Any) -> str:
    s = "" if path is None else str(path).strip()
    if not s:
        return "/"
    if not s.startswith("/"):
        s = "/" + s
    return s


def _coerce_paths(paths: Any, max_paths: Any = None) -> List[str]:
    try:
        limit = int(max_paths) if max_paths is not None else MAX_PATHS
    except (TypeError, ValueError):
        limit = MAX_PATHS
    if limit <= 0:
        limit = MAX_PATHS
    limit = min(limit, MAX_PATHS)
    if paths is None:
        items: List[str] = list(COMMON_RTSP_PATHS)
    elif isinstance(paths, (str, bytes)):
        items = [paths.decode() if isinstance(paths, bytes) else paths]
    else:
        try:
            items = list(paths)
        except TypeError:
            items = [paths]
    out: List[str] = []
    seen: set[str] = set()
    for raw in items:
        norm = _normalize_path(raw)
        if norm not in seen:
            seen.add(norm)
            out.append(norm)
        if len(out) >= limit:
            break
    return out


def _normalize_host(host: Any) -> str:
    if host is None:
        return ""
    if isinstance(host, bool):
        return ""
    return str(host).strip().strip("[]")


def build_rtsp_url(
    host: Any,
    port: Any = DEFAULT_RTSP_PORT,
    path: Any = "/",
    username: Any = None,
    password: Any = None,
) -> str:
    """Build ``rtsp://[user:pass@]host:port/path`` (explicit port)."""
    h = _normalize_host(host)
    p = _coerce_port(port)
    norm_path = _normalize_path(path)
    # Split off query before joining to avoid double slashes.
    creds = ""
    if username is not None and str(username) != "":
        user = str(username)
        if password is not None and str(password) != "":
            creds = f"{user}:{password}@"
        else:
            creds = f"{user}@"
    netloc = f"{h}:{p}"
    return f"rtsp://{creds}{netloc}{norm_path}"


def build_describe_request(
    uri: Any,
    cseq: Any = 1,
    username: Any = None,
    password: Any = None,
    user_agent: Any = None,
) -> str:
    """Build a raw RTSP ``DESCRIBE`` request string for *uri*."""
    target = str(uri).strip() if uri is not None else "*"
    if not target:
        target = "*"
    try:
        seq = int(cseq)
    except (TypeError, ValueError):
        seq = 1
    if seq <= 0:
        seq = 1
    ua = str(user_agent).strip() if user_agent else USER_AGENT
    lines = [
        f"DESCRIBE {target} RTSP/1.0",
        f"CSeq: {seq}",
        "Accept: application/sdp",
        f"User-Agent: {ua}",
    ]
    if username is not None and str(username) != "":
        token = f"{username}:{'' if password is None else password}"
        b64 = base64.b64encode(token.encode("utf-8")).decode("ascii")
        lines.append(f"Authorization: Basic {b64}")
    return "\r\n".join(lines) + "\r\n\r\n"


def parse_describe_response(raw: Any) -> Dict[str, Any]:
    """Parse a raw RTSP DESCRIBE response into ``{status_code, ok, ...}``."""
    if raw is None:
        return {
            "status_code": None,
            "ok": False,
            "status": "error",
            "headers": {},
            "body": "",
            "sdp": False,
            "error": "empty response",
        }
    text = raw.decode("iso-8859-1", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    # Normalize CRLF, split head/body.
    head, _, body = text.partition("\r\n\r\n")
    if "\r\n\r\n" not in text:
        head, _, body = text.partition("\n\n")
    first_line = (head.splitlines() or [""])[0].strip()
    status_code: Optional[int] = None
    parts = first_line.split()
    if len(parts) >= 2 and parts[0].upper().startswith("RTSP/"):
        try:
            status_code = int(parts[1])
        except (TypeError, ValueError):
            status_code = None
    headers: Dict[str, str] = {}
    for line in head.splitlines()[1:]:
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        headers[k.strip().lower()] = v.strip()
    sdp = ("v=0" in body) or ("m=video" in body) or ("m=audio" in body)
    ok = status_code is not None and 200 <= status_code < 300
    error: Optional[str] = None
    if status_code is None:
        error = f"unparsable response: {first_line[:120]}"
    elif status_code == 401:
        error = "unauthorized (401)"
    elif status_code == 404:
        error = "not found (404)"
    elif not ok:
        error = f"rtsp status {status_code}"
    return {
        "status_code": status_code,
        "status_line": first_line,
        "ok": bool(ok),
        "status": "ok" if ok else "error",
        "headers": headers,
        "body": body[:4096],
        "sdp": bool(sdp),
        "error": error,
    }


def _default_describer(
    host: str,
    port: int,
    path: str,
    timeout: float,
    username: Optional[str] = None,
    password: Optional[str] = None,
) -> Dict[str, Any]:
    """Real network DESCRIBE over a TCP socket (capped timeout/bytes)."""
    uri = build_rtsp_url(host, port, path)
    request = build_describe_request(uri, cseq=1, username=username, password=password)
    raw = b""
    sock: Optional[socket.socket] = None
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        try:
            sock.settimeout(timeout)
        except OSError:
            pass
        sock.sendall(request.encode("utf-8"))
        chunks: List[bytes] = []
        total = 0
        # Read until end-of-headers (and a bit of SDP) or cap.
        while total < MAX_RESPONSE_BYTES:
            try:
                chunk = sock.recv(min(4096, MAX_RESPONSE_BYTES - total))
            except socket.timeout as exc:
                if raw:
                    break
                raise TimeoutError(f"describe timed out after {timeout}s") from exc
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            raw = b"".join(chunks)
            if b"\r\n\r\n" in raw and len(raw) >= 64:
                # If headers complete and we already have some body, stop early.
                if len(raw) > raw.index(b"\r\n\r\n") + 256:
                    break
                # No Content-Length framing guarantee; one extra short read.
                try:
                    sock.settimeout(0.3)
                    extra = sock.recv(4096)
                    if extra:
                        chunks.append(extra)
                        raw = b"".join(chunks)
                except Exception:  # noqa: BLE001
                    pass
                break
        raw = b"".join(chunks)
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:  # noqa: BLE001
                pass
    if not raw:
        raise ConnectionError("empty DESCRIBE response")
    return parse_describe_response(raw)


def _interpret_describer_output(out: Any) -> Dict[str, Any]:
    """Normalize injectable describer outputs to a parsed-response dict."""
    if out is None:
        return parse_describe_response("")
    if isinstance(out, bool):
        return {
            "status_code": 200 if out else 404,
            "ok": bool(out),
            "status": "ok" if out else "error",
            "headers": {},
            "body": "",
            "sdp": False,
            "error": None if out else "not found (404)",
        }
    if isinstance(out, (int, float)) and not isinstance(out, bool):
        code = int(out)
        return {
            "status_code": code,
            "ok": 200 <= code < 300,
            "status": "ok" if 200 <= code < 300 else "error",
            "headers": {},
            "body": "",
            "sdp": False,
            "error": None if 200 <= code < 300 else f"rtsp status {code}",
        }
    if isinstance(out, (bytes, bytearray, str)):
        return parse_describe_response(out)
    if isinstance(out, dict):
        if "status_code" in out or "statusCode" in out or "ok" in out:
            code = out.get("status_code", out.get("statusCode"))
            try:
                code_i = int(code) if code is not None else None
            except (TypeError, ValueError):
                code_i = None
            ok = out.get("ok")
            if ok is None:
                ok = code_i is not None and 200 <= code_i < 300
            else:
                ok = bool(ok)
            headers = out.get("headers", out.get("Headers", {}))
            if not isinstance(headers, dict):
                headers = {}
            body = out.get("body", out.get("sdp_body", out.get("data", "")))
            if isinstance(body, (bytes, bytearray)):
                try:
                    body_s = bytes(body).decode("iso-8859-1", errors="replace")
                except Exception:  # noqa: BLE001
                    body_s = ""
            else:
                body_s = str(body or "")
            err = out.get("error", out.get("message"))
            if err is not None:
                err = str(err)
            elif not ok and code_i is not None:
                err = f"rtsp status {code_i}"
            return {
                "status_code": code_i,
                "ok": bool(ok),
                "status": "ok" if ok else "error",
                "headers": dict(headers),
                "body": body_s[:4096],
                "sdp": bool(out.get("sdp", ("v=0" in body_s))),
                "error": err,
            }
        # Treat as raw header/body mapping failure.
        return parse_describe_response(str(out))
    if isinstance(out, (tuple, list)) and out:
        first = out[0]
        try:
            code_i: Optional[int] = int(first)
        except (TypeError, ValueError):
            return parse_describe_response(str(out))
        headers_d = out[1] if len(out) > 1 and isinstance(out[1], dict) else {}
        body_s = ""
        if len(out) > 2:
            b = out[2]
            body_s = b.decode("iso-8859-1", errors="replace") if isinstance(b, (bytes, bytearray)) else str(b or "")
        ok = 200 <= code_i < 300
        return {
            "status_code": code_i,
            "ok": ok,
            "status": "ok" if ok else "error",
            "headers": dict(headers_d),
            "body": body_s[:4096],
            "sdp": ("v=0" in body_s),
            "error": None if ok else f"rtsp status {code_i}",
        }
    return parse_describe_response(str(out))


# ---------------------------------------------------------------------------
# Result model.
# ---------------------------------------------------------------------------


@dataclass
class RtspPathResult:
    """Outcome of probing one RTSP path with DESCRIBE."""

    host: str = ""
    port: int = DEFAULT_RTSP_PORT
    path: str = "/"
    url: str = ""
    ok: bool = False
    reachable: bool = False
    found: bool = False
    status_code: Optional[int] = None
    status: str = "unknown"
    sdp: bool = False
    headers: Dict[str, Any] = field(default_factory=dict)
    latency_ms: Optional[float] = None
    error: Optional[str] = None
    checked_at: Optional[datetime] = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    def to_dict(self) -> Dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "path": self.path,
            "url": self.url,
            "uri": self.url,
            "ok": bool(self.ok),
            "success": bool(self.ok),
            "reachable": bool(self.reachable),
            "found": bool(self.found),
            "healthy": bool(self.ok),
            "status_code": self.status_code,
            "status": self.status,
            "state": self.status,
            "sdp": bool(self.sdp),
            "has_sdp": bool(self.sdp),
            "headers": dict(self.headers or {}),
            "latency_ms": self.latency_ms,
            "latencyMs": self.latency_ms,
            "error": self.error,
            "checked_at": self.checked_at.isoformat() if self.checked_at else None,
            "checkedAt": self.checked_at.isoformat() if self.checked_at else None,
            "timeout_seconds": self.timeout_seconds,
            "timeoutSeconds": self.timeout_seconds,
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
            "id": "url",
            "target": "url",
            "latency": "latency_ms",
            "message": "error",
            "code": "status_code",
        }
        if key in aliases:
            return data.get(aliases[key])
        return None

    def get(self, key: str, default: Any = None) -> Any:
        try:
            val = self[key]
            return default if val is None and key not in self.__dict__ else val
        except Exception:  # noqa: BLE001
            return default


RtspPathStatus = RtspPathResult
PathResult = RtspPathResult
ProbeResult = RtspPathResult
DescribeResult = RtspPathResult


def describe_path(
    host: Any = None,
    port: Any = DEFAULT_RTSP_PORT,
    path: Any = "/",
    timeout: Any = DEFAULT_TIMEOUT_SECONDS,
    username: Any = None,
    password: Any = None,
    describer: Optional[Callable[..., Any]] = None,
    **kwargs: Any,
) -> RtspPathResult:
    """DESCRIBE-probe a single ``rtsp://host:port/path``. Never raises."""
    # Tolerance for alias kwarg spellings.
    if host is None:
        for cand in (kwargs.get("hostname"), kwargs.get("ip"), kwargs.get("address")):
            if cand is not None and str(cand).strip():
                host = cand
                break
    if describer is None:
        describer = kwargs.get("prober", kwargs.get("probe", kwargs.get("describe")))
    h = _normalize_host(host)
    p = _coerce_port(port if port is not None else kwargs.get("rtsp_port", DEFAULT_RTSP_PORT))
    norm_path = _normalize_path(path)
    timeout_f = _coerce_timeout(timeout if timeout is not None else kwargs.get("timeout_seconds"))
    user = username if username is not None else kwargs.get("user")
    pwd = password if password is not None else kwargs.get("pass", kwargs.get("pwd"))
    url = build_rtsp_url(h, p, norm_path)

    if not h:
        result = RtspPathResult(
            host="", port=p, path=norm_path, url=url,
            ok=False, reachable=False, found=False,
            status_code=None, status="unknown",
            error="no host provided", checked_at=_now(),
            timeout_seconds=timeout_f,
        )
        return result

    start = time.monotonic()
    try:
        if describer is not None:
            out = describer(h, p, norm_path, timeout_f, user, pwd)
            parsed = _interpret_describer_output(out)
        else:
            parsed = _default_describer(h, p, norm_path, timeout_f, user, pwd)
        latency = round((time.monotonic() - start) * 1000.0, 2)
        ok = bool(parsed.get("ok"))
        code = parsed.get("status_code")
        result = RtspPathResult(
            host=h, port=p, path=norm_path, url=url,
            ok=ok, reachable=ok, found=ok,
            status_code=code,
            status="ok" if ok else "error",
            sdp=bool(parsed.get("sdp")),
            headers=dict(parsed.get("headers") or {}),
            latency_ms=latency,
            error=parsed.get("error"),
            checked_at=_now(),
            timeout_seconds=timeout_f,
        )
    except Exception as exc:  # noqa: BLE001
        latency = round((time.monotonic() - start) * 1000.0, 2)
        result = RtspPathResult(
            host=h, port=p, path=norm_path, url=url,
            ok=False, reachable=False, found=False,
            status_code=None, status="error",
            latency_ms=latency,
            error=f"{type(exc).__name__}: {exc}"[:500],
            checked_at=_now(),
            timeout_seconds=timeout_f,
        )
    _RESULTS[result.url] = result
    return result


# Aliases for single-path probing.
probe_path = describe_path
check_path = describe_path
test_path = describe_path
describe = describe_path
probe = describe_path
check = describe_path


def brute_force_paths(
    host: Any = None,
    port: Any = DEFAULT_RTSP_PORT,
    paths: Any = None,
    timeout: Any = DEFAULT_TIMEOUT_SECONDS,
    username: Any = None,
    password: Any = None,
    max_paths: Any = MAX_PATHS,
    describer: Optional[Callable[..., Any]] = None,
    **kwargs: Any,
) -> List[RtspPathResult]:
    """Try capped common RTSP paths on one host via DESCRIBE. Never raises."""
    if host is None:
        for cand in (kwargs.get("hostname"), kwargs.get("ip"), kwargs.get("address")):
            if cand is not None and str(cand).strip():
                host = cand
                break
    if paths is None:
        for cand in (kwargs.get("path_list"), kwargs.get("pathList"), kwargs.get("candidates")):
            if cand is not None:
                paths = cand
                break
    if describer is None:
        describer = kwargs.get("prober", kwargs.get("probe", kwargs.get("describe")))
    if max_paths is None:
        max_paths = kwargs.get("maxPaths", kwargs.get("limit", MAX_PATHS))
    if timeout is None:
        timeout = kwargs.get("timeout_seconds", kwargs.get("timeoutSeconds", DEFAULT_TIMEOUT_SECONDS))
    h = _normalize_host(host)
    if not h:
        return []
    p = _coerce_port(port if port is not None else kwargs.get("rtsp_port", DEFAULT_RTSP_PORT))
    timeout_f = _coerce_timeout(timeout)
    wanted = _coerce_paths(paths, max_paths)
    user = username if username is not None else kwargs.get("user")
    pwd = password if password is not None else kwargs.get("pass", kwargs.get("pwd"))
    results: List[RtspPathResult] = []
    for one in wanted:
        try:
            results.append(describe_path(h, p, one, timeout_f, user, pwd, describer=describer))
        except Exception:  # noqa: BLE001
            continue
    return results


# More aliases (repo convention: many names, same behaviour).
scan_host = brute_force_paths
scan_paths = brute_force_paths
brute_force = brute_force_paths
bruteforce = brute_force_paths
scan = brute_force_paths
probe_host = brute_force_paths
check_host = brute_force_paths
find_paths = brute_force_paths
discover_paths = brute_force_paths


async def adescribe_path(
    host: Any = None,
    port: Any = DEFAULT_RTSP_PORT,
    path: Any = "/",
    timeout: Any = DEFAULT_TIMEOUT_SECONDS,
    username: Any = None,
    password: Any = None,
    describer: Optional[Callable[..., Any]] = None,
    **kwargs: Any,
) -> RtspPathResult:
    """Async single-path DESCRIBE (runs sync probe in a thread)."""
    return await asyncio.to_thread(
        describe_path, host, port, path, timeout, username, password, describer, **kwargs
    )


async def abrute_force_paths(
    host: Any = None,
    port: Any = DEFAULT_RTSP_PORT,
    paths: Any = None,
    timeout: Any = DEFAULT_TIMEOUT_SECONDS,
    username: Any = None,
    password: Any = None,
    max_paths: Any = MAX_PATHS,
    describer: Optional[Callable[..., Any]] = None,
    concurrency: Any = DEFAULT_CONCURRENCY,
    **kwargs: Any,
) -> List[RtspPathResult]:
    """Async brute-force with capped concurrency (default 8)."""
    if host is None:
        for cand in (kwargs.get("hostname"), kwargs.get("ip"), kwargs.get("address")):
            if cand is not None and str(cand).strip():
                host = cand
                break
    if paths is None:
        for cand in (kwargs.get("path_list"), kwargs.get("pathList"), kwargs.get("candidates")):
            if cand is not None:
                paths = cand
                break
    if describer is None:
        describer = kwargs.get("prober", kwargs.get("probe"))
    if max_paths is None:
        max_paths = kwargs.get("maxPaths", kwargs.get("limit", MAX_PATHS))
    h = _normalize_host(host)
    if not h:
        return []
    p = _coerce_port(port)
    timeout_f = _coerce_timeout(timeout if timeout is not None else kwargs.get("timeout_seconds"))
    wanted = _coerce_paths(paths, max_paths)
    try:
        conc = int(concurrency if concurrency is not None else kwargs.get("concurrency", DEFAULT_CONCURRENCY))
    except (TypeError, ValueError):
        conc = DEFAULT_CONCURRENCY
    conc = max(1, min(conc, 16))
    sem = asyncio.Semaphore(conc)

    async def _one(one: str) -> RtspPathResult:
        async with sem:
            return await asyncio.to_thread(
                describe_path, h, p, one, timeout_f, username, password, describer
            )

    out = await asyncio.gather(*[_one(one) for one in wanted])
    return list(out)


ascan_host = abrute_force_paths
ascan = abrute_force_paths
abrute_force = abrute_force_paths


def get_status(url: Any = None, **kwargs: Any) -> Optional[RtspPathResult]:
    key = str(url if url is not None else kwargs.get("url", kwargs.get("target", ""))).strip()
    if not key:
        return None
    if key in _RESULTS:
        return _RESULTS[key]
    return None


get = get_status
get_result = get_status
find = get_status
lookup = get_status
status_for = get_status


def list_results() -> List[RtspPathResult]:
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
    ok_count = sum(1 for r in items if r.ok)
    return {
        "total": total,
        "tested": total,
        "ok_count": ok_count,
        "found": ok_count,
        "errors": total - ok_count,
        "ok": True,
        "success": True,
        "common_paths": len(COMMON_RTSP_PATHS),
        "max_paths": MAX_PATHS,
        "max_timeout_seconds": MAX_TIMEOUT_SECONDS,
    }


summary = get_summary
get_stats = get_summary
stats = get_summary


def clear(url: Any = None, **kwargs: Any) -> None:
    if url is None and "url" not in kwargs and "target" not in kwargs:
        _RESULTS.clear()
        return
    key = str(url if url is not None else kwargs.get("url", kwargs.get("target", ""))).strip()
    if key and key in _RESULTS:
        _RESULTS.pop(key, None)
    else:
        _RESULTS.clear()


clear_all = lambda: _RESULTS.clear()  # noqa: E731
reset_all = clear_all
reset = clear_all


def remove(url: Any = None, **kwargs: Any) -> bool:
    key = str(url if url is not None else kwargs.get("url", kwargs.get("target", ""))).strip()
    if not key:
        return False
    return _RESULTS.pop(key, None) is not None


delete = remove
drop = remove


def serialize(state: Any) -> Dict[str, Any]:
    if isinstance(state, RtspPathResult):
        return state.to_dict()
    if isinstance(state, dict):
        return state
    return {"value": str(state)}


__all__ = [
    "COMMON_RTSP_PATHS",
    "DEFAULT_PATHS",
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_TIMEOUT_SECONDS",
    "MAX_PATHS",
    "DEFAULT_RTSP_PORT",
    "DEFAULT_CONCURRENCY",
    "USER_AGENT",
    "RtspPathResult",
    "RtspPathStatus",
    "build_rtsp_url",
    "build_describe_request",
    "parse_describe_response",
    "describe_path",
    "brute_force_paths",
    "abrute_force_paths",
    "adescribe_path",
    "get_status",
    "list_results",
    "list_results_dicts",
    "get_summary",
    "clear",
    "remove",
]
