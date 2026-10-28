"""Camera URL template tester service for VigilCam.

Reads (but never modifies) the iSpy ``XML/Sources.xml`` manufacturer URL
template catalog and the ``ispy-agent-py`` still/MJPEG URL conventions.

Catalog findings baked into this module (see ``/opt/others/ispy`` read-only):

- ``Sources.xml``: ~3099 ``<Manufacturer>`` entries, each with one or more
  ``<url version prefix Source url port>`` rows. ``prefix`` is the scheme
  (``http://`` ~34k rows, ``rtsp://`` ~20k rows, plus ``rtmp://``/``mms://``).
  ``Source`` is the stream kind: ``FFMPEG`` (usually RTSP/video), ``JPEG``
  (single still snapshot), ``MJPEG`` (multipart motion-JPEG stream),
  ``VLC`` (RTSP served via VLC demuxer).
- ``url`` is a *path template* with placeholders: ``[USERNAME]``,
  ``[PASSWORD]``, ``[CHANNEL]``, ``[WIDTH]``, ``[HEIGHT]``, ``[AUTH]``.
  Example (Axis): ``prefix="http://" Source="MJPEG"
  url="mjpg/1/video.mjpg"`` vs ``Source="JPEG" url="jpg/1/image.jpg"``.
- ``ispy-agent-py`` (``agent/device.py``): still snapshot convention
  ``grab.jpg?oid=...`` (single ``image/jpeg``) vs MJPEG stream convention
  ``video.mjpg?oid=...`` (``multipart/x-mixed-replace``), plus
  ``video.mp4``/``video.webm`` variants.

This service tests an arbitrary camera URL (or a catalog-style template +
host/credentials) by fetching it like a snapshot client would and reporting
``status`` / ``latency`` / ``content-type`` plus a kind classification
(``snapshot`` vs ``mjpeg`` vs ``stream``) so the UI can tell the user whether
the template yields a still image, a multipart stream, or something else.

Design goals (mirrors ``app.services.stream_probe``):

- stdlib only (``urllib`` + ``socket``) so it works in minimal test envs.
- injectable ``fetcher`` callback for deterministic unit tests.
- never raises on bad input; empty/unsupported URL -> ``status="unknown"``.
- in-memory last-results cache keyed by normalized URL.
"""

from __future__ import annotations

import base64
import re
import socket
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_TIMEOUT_SECONDS = 60.0
MAX_READ_BYTES = 256 * 1024

DEFAULT_CHANNEL = "1"
DEFAULT_WIDTH = "640"
DEFAULT_HEIGHT = "480"

_HTTP_SCHEMES = ("http", "https")
_TCP_SCHEMES = ("rtsp", "rtmp", "mms", "onvif", "ws", "wss")

_SCHEME_DEFAULT_PORT = {
    "http": 80,
    "https": 443,
    "rtsp": 554,
    "rtmp": 1935,
    "mms": 1755,
    "onvif": 80,
    "ws": 80,
    "wss": 443,
}

# In-memory test results keyed by normalized URL.
_RESULTS: Dict[str, "UrlTestResult"] = {}

_JPEG_MAGIC = b"\xff\xd8\xff"
_PNG_MAGIC = b"\x89PNG"
_GIF_MAGIC_87 = b"GIF87a"
_GIF_MAGIC_89 = b"GIF89a"
_BMP_MAGIC = b"BM"


def _now() -> datetime:
    return datetime.utcnow()


def _coerce_timeout(value: Any) -> float:
    try:
        t = float(value)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SECONDS
    if t <= 0:
        return DEFAULT_TIMEOUT_SECONDS
    return min(t, MAX_TIMEOUT_SECONDS)


def _normalize_url(url: Any) -> str:
    if url is None:
        return ""
    return str(url).strip()


# ---------------------------------------------------------------------------
# Template handling (Sources.xml conventions)
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"\[(USERNAME|PASSWORD|CHANNEL|WIDTH|HEIGHT|AUTH)\]", re.IGNORECASE)


def expand_template(
    template: Any,
    username: Any = None,
    password: Any = None,
    channel: Any = None,
    width: Any = None,
    height: Any = None,
    auth: Any = None,
    **kwargs: Any,
) -> str:
    """Expand iSpy-style ``[USERNAME]`` / ``[PASSWORD]`` / ``[CHANNEL]`` /
    ``[WIDTH]`` / ``[HEIGHT]`` / ``[AUTH]`` placeholders in a template.

    Missing credentials expand to ``""``; missing channel/size fall back to
    ``1`` / ``640`` / ``480``. Accepts common alias kwarg spellings
    (``user``, ``pass``, ``ch``).
    """
    text = "" if template is None else str(template)
    if username is None:
        username = kwargs.get("user", kwargs.get("username", kwargs.get("USERNAME")))
    if password is None:
        password = (
            kwargs.get("pass", kwargs.get("password", kwargs.get("PASSWORD", kwargs.get("pwd"))))
        )
    if channel is None:
        channel = kwargs.get("ch", kwargs.get("channel", kwargs.get("CHANNEL", DEFAULT_CHANNEL)))
    if width is None:
        width = kwargs.get("width", kwargs.get("WIDTH", kwargs.get("w", DEFAULT_WIDTH)))
    if height is None:
        height = kwargs.get("height", kwargs.get("HEIGHT", kwargs.get("h", DEFAULT_HEIGHT)))
    if auth is None:
        auth = kwargs.get("auth", kwargs.get("AUTH", ""))

    values = {
        "username": "" if username is None else str(username),
        "password": "" if password is None else str(password),
        "channel": DEFAULT_CHANNEL if channel is None or str(channel) == "" else str(channel),
        "width": DEFAULT_WIDTH if width is None or str(width) == "" else str(width),
        "height": DEFAULT_HEIGHT if height is None or str(height) == "" else str(height),
        "auth": "" if auth is None else str(auth),
    }

    def _sub(match: "re.Match[str]") -> str:
        return values.get(match.group(1).lower(), match.group(0))

    return _TOKEN_RE.sub(_sub, text)


def has_template_tokens(template: Any) -> bool:
    """Return True when *template* still contains unexpanded ``[TOKEN]``s."""
    if template is None:
        return False
    return bool(_TOKEN_RE.search(str(template)))


def build_url(
    path_or_url: Any,
    host: Any = None,
    port: Any = None,
    prefix: Any = None,
    scheme: Any = None,
    **kwargs: Any,
) -> str:
    """Compose a testable URL from Sources.xml-style parts.

    ``prefix`` is the catalog ``prefix`` attribute (e.g. ``"http://"``);
    ``scheme`` is an equivalent alternative. When ``path_or_url`` is already
    absolute (contains ``://``) it is returned (template-expanded) as-is.
    Otherwise ``scheme://host[:port]/path`` is assembled; a falsy ``host``
    means the path is returned unchanged.
    """
    raw = "" if path_or_url is None else str(path_or_url).strip()
    raw = expand_template(raw, **kwargs)
    if "://" in raw:
        return raw
    if host is None or str(host).strip() == "":
        return raw
    host_s = str(host).strip()
    if scheme is not None and str(scheme).strip():
        scheme_s = str(scheme).strip().rstrip(":/").lower()
    elif prefix is not None and str(prefix).strip():
        scheme_s = str(prefix).strip().rstrip(":/").lower().split("://")[0] or "http"
    else:
        scheme_s = "http"
    if scheme_s not in ("http", "https", "rtsp", "rtmp", "mms", "onvif", "ws", "wss"):
        scheme_s = "http"
    try:
        port_i = int(port) if port is not None and str(port).strip() != "" else None
    except (TypeError, ValueError):
        port_i = None
    default_port = _SCHEME_DEFAULT_PORT.get(scheme_s)
    netloc = host_s
    if port_i is not None and port_i != default_port:
        netloc = f"{host_s}:{port_i}"
    path = raw.lstrip("/")
    return f"{scheme_s}://{netloc}/{path}" if path else f"{scheme_s}://{netloc}/"


# ---------------------------------------------------------------------------
# Classification (catalog Source + URL-shape heuristics)
# ---------------------------------------------------------------------------

_MJPEG_HINTS = ("mjpg", "mjpeg", "video.cgi", "videostream.cgi", "videofeed", "video.mjpg")
_SNAPSHOT_HINTS = (
    "snapshot",
    "image.jpg",
    "jpg/image",
    "image.cgi",
    "grab.jpg",
    "tmpfs/auto.jpg",
    "shot.jpg",
)


def classify_url(url: Any, source: Any = None) -> Dict[str, Any]:
    """Classify a camera URL into ``snapshot`` / ``mjpeg`` / ``stream`` / etc.

    ``source`` mirrors the Sources.xml ``Source`` attribute (``JPEG``,
    ``MJPEG``, ``FFMPEG``, ``VLC``) when the caller knows which catalog row
    the URL came from.
    """
    url_s = _normalize_url(url)
    source_s = "" if source is None else str(source).strip().upper()
    parsed = urllib.parse.urlparse(url_s if "://" in url_s else f"http://{url_s}")
    scheme = (parsed.scheme or "").lower()
    lowered = url_s.lower()

    if scheme in ("rtsp", "rtmp", "mms"):
        kind = "stream"
        expected = "video stream; snapshot requires an ffmpeg frame grab"
    elif scheme in ("http", "https", ""):
        if source_s == "MJPEG" or any(h in lowered for h in _MJPEG_HINTS):
            kind = "mjpeg"
            expected = "multipart/x-mixed-replace motion-JPEG stream"
        elif source_s == "JPEG" or any(h in lowered for h in _SNAPSHOT_HINTS):
            kind = "snapshot"
            expected = "single still image (image/jpeg)"
        elif source_s in ("FFMPEG", "VLC") or lowered.endswith((".asf", ".mp4", ".m3u8")):
            kind = "stream"
            expected = "video stream; snapshot requires an ffmpeg frame grab"
        elif lowered.endswith((".jpg", ".jpeg", ".png", ".gif", ".bmp")):
            kind = "snapshot"
            expected = "single still image"
        else:
            kind = "http"
            expected = "generic HTTP endpoint; content-type determines handling"
    else:
        kind = "unknown"
        expected = "unsupported scheme for snapshot fetch"

    return {
        "kind": kind,
        "scheme": scheme,
        "source": source_s or None,
        "expected": expected,
        "is_snapshot_candidate": kind in ("snapshot", "mjpeg", "http"),
        "needs_ffmpeg": kind == "stream",
    }


def _sniff_kind(content_type: str, body: bytes) -> Dict[str, Any]:
    ct = (content_type or "").lower()
    head = bytes(body[:16]) if body else b""
    is_jpeg = head.startswith(_JPEG_MAGIC)
    is_png = head.startswith(_PNG_MAGIC)
    is_gif = head.startswith((_GIF_MAGIC_87, _GIF_MAGIC_89))
    is_bmp = head.startswith(_BMP_MAGIC)
    is_image_ct = ct.startswith("image/")
    is_mjpeg_ct = "multipart/x-mixed-replace" in ct or "multipart/x-mixed" in ct
    text = ""
    try:
        text = bytes(body[:512]).decode("latin-1", errors="ignore")
    except Exception:  # noqa: BLE001
        text = ""
    looks_mjpeg_body = text.startswith("--") and "content-type:" in text.lower()
    if is_mjpeg_ct or looks_mjpeg_body:
        detected = "mjpeg"
    elif is_image_ct or is_jpeg or is_png or is_gif or is_bmp:
        detected = "snapshot"
    elif ct.startswith("video/") or ct in ("application/octet-stream",):
        detected = "stream"
    elif not ct and (is_jpeg or is_png):
        detected = "snapshot"
    else:
        detected = "unknown"
    return {
        "detected": detected,
        "is_image": bool(is_image_ct or is_jpeg or is_png or is_gif or is_bmp),
        "is_mjpeg": bool(is_mjpeg_ct or looks_mjpeg_body or detected == "mjpeg"),
    }


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


@dataclass
class UrlTestResult:
    """Outcome of testing a single camera URL."""

    url: str
    expanded_url: Optional[str] = None
    ok: bool = False
    reachable: bool = False
    status: str = "unknown"
    http_status: Optional[int] = None
    content_type: Optional[str] = None
    content_length: Optional[int] = None
    bytes_read: int = 0
    truncated: bool = False
    kind: Optional[str] = None
    detected: Optional[str] = None
    is_image: bool = False
    latency_ms: Optional[float] = None
    error: Optional[str] = None
    checked_at: Optional[datetime] = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "url": self.url,
            "expanded_url": self.expanded_url,
            "ok": bool(self.ok),
            "success": bool(self.ok),
            "reachable": bool(self.reachable),
            "healthy": bool(self.ok),
            "status": self.status,
            "state": self.status,
            "http_status": self.http_status,
            "status_code": self.http_status,
            "content_type": self.content_type,
            "contentType": self.content_type,
            "content_length": self.content_length,
            "contentLength": self.content_length,
            "bytes_read": self.bytes_read,
            "bytesRead": self.bytes_read,
            "truncated": bool(self.truncated),
            "kind": self.kind,
            "detected": self.detected,
            "is_image": bool(self.is_image),
            "isImage": bool(self.is_image),
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
        aliases = {
            "id": "url",
            "target": "url",
            "latency": "latency_ms",
            "state": "status",
            "message": "error",
            "contentType": "content_type",
            "status_code": "http_status",
        }
        attr = aliases.get(key, key)
        if hasattr(self, attr):
            return getattr(self, attr)
        return None

    def get(self, key: str, default: Any = None) -> Any:
        try:
            val = self[key]
            return default if val is None and key not in self.__dict__ else val
        except Exception:  # noqa: BLE001
            return default


UrlTesterResult = UrlTestResult
TestResult = UrlTestResult
UrlTestStatus = UrlTestResult


# ---------------------------------------------------------------------------
# Fetch core
# ---------------------------------------------------------------------------


def _basic_auth_header(username: Any, password: Any) -> Optional[str]:
    if username is None or str(username) == "":
        return None
    token = f"{username}:{'' if password is None else password}"
    return "Basic " + base64.b64encode(token.encode("utf-8")).decode("ascii")


def _do_http(
    url: str, timeout: float, headers: Optional[Dict[str, str]] = None
) -> tuple[Optional[int], Dict[str, str], bytes]:
    """Perform a bounded HTTP GET. Returns (status, headers, body[:MAX+1])."""
    req = urllib.request.Request(
        url,
        method="GET",
        headers={
            "User-Agent": "VigilCam-url-tester/1.0",
            "Accept": "image/*, multipart/x-mixed-replace, */*;q=0.8",
            **(headers or {}),
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        status = getattr(resp, "status", None) or resp.getcode()
        raw_headers: Dict[str, str] = {}
        try:
            for k, v in resp.headers.items():
                raw_headers[str(k).lower()] = str(v)
        except Exception:  # noqa: BLE001
            pass
        try:
            body = resp.read(MAX_READ_BYTES + 1)
        except Exception:  # noqa: BLE001
            body = b""
        return status, raw_headers, bytes(body or b"")


def _tcp_check(host: str, port: int, timeout: float) -> None:
    with socket.create_connection((host, port), timeout=timeout):
        pass


def test_url(
    url: Any = None,
    timeout: Any = DEFAULT_TIMEOUT_SECONDS,
    *,
    source: Any = None,
    username: Any = None,
    password: Any = None,
    channel: Any = None,
    width: Any = None,
    height: Any = None,
    host: Any = None,
    port: Any = None,
    prefix: Any = None,
    scheme: Any = None,
    fetcher: Optional[Callable[..., Any]] = None,
    **kwargs: Any,
) -> UrlTestResult:
    """Fetch a snapshot from an arbitrary camera URL and describe the result.

    Never raises; transport/parse failures are captured in ``error`` with
    ``status`` ``"error"`` (or ``"unknown"`` when no URL was given).
    ``fetcher`` may be injected by tests with signature
    ``fetcher(url, timeout, headers) -> (status, headers, body)`` or a dict
    with ``status``/``headers``/``body`` keys.
    """
    for alias in ("target", "stream_url", "streamUrl", "source_url", "uri", "href"):
        if (url is None or str(url).strip() == "") and kwargs.get(alias) is not None:
            url = kwargs.get(alias)
    if username is None:
        username = kwargs.get("username", kwargs.get("user"))
    if password is None:
        password = kwargs.get("password", kwargs.get("pass", kwargs.get("pwd")))
    if source is None:
        source = kwargs.get("source_kind", kwargs.get("kind_source", kwargs.get("Source")))
    timeout_f = _coerce_timeout(timeout if timeout is not None else kwargs.get("timeout_seconds"))

    original = _normalize_url(url)
    if host is not None or prefix is not None or scheme is not None or port is not None:
        expanded = build_url(
            original, host=host, port=port, prefix=prefix, scheme=scheme,
            username=username, password=password, channel=channel,
            width=width, height=height, **kwargs,
        )
    else:
        expanded = expand_template(
            original, username=username, password=password,
            channel=channel, width=width, height=height, **kwargs,
        )
    key = expanded or original or "unknown"

    if not expanded:
        result = UrlTestResult(
            url=original, expanded_url=None, ok=False, reachable=False,
            status="unknown", error="no url provided", checked_at=_now(),
            timeout_seconds=timeout_f,
        )
        _RESULTS[key] = result
        return result

    if has_template_tokens(expanded) and (username is None and password is None and host is None):
        # Still testable, but flag that credentials were not supplied.
        pass

    info = classify_url(expanded, source=source)
    parsed = urllib.parse.urlparse(expanded if "://" in expanded else f"http://{expanded}")
    url_scheme = (parsed.scheme or "http").lower()

    if url_scheme not in (*_HTTP_SCHEMES, *_TCP_SCHEMES):
        result = UrlTestResult(
            url=original, expanded_url=expanded, ok=False, reachable=False,
            status="error", kind=info["kind"], error=f"unsupported scheme '{url_scheme}'",
            checked_at=_now(), timeout_seconds=timeout_f,
        )
        _RESULTS[key] = result
        return result

    if url_scheme not in _HTTP_SCHEMES:
        # Non-HTTP (RTSP/RMTP/...) — TCP reachability check like stream_probe.
        host_part = parsed.hostname or ""
        if not host_part:
            result = UrlTestResult(
                url=original, expanded_url=expanded, ok=False, reachable=False,
                status="error", kind=info["kind"], error="no host in stream url",
                checked_at=_now(), timeout_seconds=timeout_f,
            )
            _RESULTS[key] = result
            return result
        port_i = parsed.port or _SCHEME_DEFAULT_PORT.get(url_scheme, 554)
        start = time.monotonic()
        try:
            if fetcher is not None:
                out = fetcher(expanded, timeout_f, {})
                reachable = bool(out.get("reachable", out.get("ok", True))) if isinstance(out, dict) else bool(out)
                error = None if reachable else str(out.get("error", "unreachable") if isinstance(out, dict) else "unreachable")
            else:
                _tcp_check(host_part, port_i, timeout_f)
                reachable, error = True, None
        except Exception as exc:  # noqa: BLE001
            reachable, error = False, f"{type(exc).__name__}: {exc}"[:500]
        latency_ms = round((time.monotonic() - start) * 1000.0, 2)
        result = UrlTestResult(
            url=original, expanded_url=expanded, ok=reachable, reachable=reachable,
            status="ok" if reachable else "error", kind=info["kind"],
            detected="stream", is_image=False, latency_ms=latency_ms, error=error,
            checked_at=_now(), timeout_seconds=timeout_f,
            extra={"note": info["expected"]},
        )
        _RESULTS[key] = result
        return result

    headers: Dict[str, str] = {}
    basic = _basic_auth_header(username, password)
    if basic:
        headers["Authorization"] = basic

    start = time.monotonic()
    try:
        if fetcher is not None:
            out = fetcher(expanded, timeout_f, headers)
            if isinstance(out, dict):
                status_code = out.get("status", out.get("http_status"))
                resp_headers = {str(k).lower(): str(v) for k, v in dict(out.get("headers", {})).items()}
                body = out.get("body", b"") or b""
                if isinstance(body, str):
                    body = body.encode("latin-1")
                body = bytes(body)
                if out.get("error") and not out.get("ok", status_code):
                    raise RuntimeError(str(out["error"]))
            else:
                status_code, resp_headers, body = out
                body = bytes(body or b"")
        else:
            status_code, resp_headers, body = _do_http(expanded, timeout_f, headers)
        latency_ms = round((time.monotonic() - start) * 1000.0, 2)
        try:
            status_i = int(status_code) if status_code is not None else None
        except (TypeError, ValueError):
            status_i = None
        content_type = resp_headers.get("content-type")
        content_length: Optional[int] = None
        if resp_headers.get("content-length") is not None:
            try:
                content_length = int(str(resp_headers["content-length"]).strip())
            except (TypeError, ValueError):
                content_length = None
        truncated = len(body) > MAX_READ_BYTES
        body = body[:MAX_READ_BYTES]
        sniffed = _sniff_kind(content_type or "", body)
        ok = status_i is not None and 200 <= status_i < 300
        result = UrlTestResult(
            url=original, expanded_url=expanded, ok=ok, reachable=ok,
            status="ok" if ok else "error", http_status=status_i,
            content_type=content_type, content_length=content_length,
            bytes_read=len(body), truncated=truncated, kind=info["kind"],
            detected=sniffed["detected"], is_image=sniffed["is_image"],
            latency_ms=latency_ms,
            error=None if ok else f"http status {status_i}",
            checked_at=_now(), timeout_seconds=timeout_f,
            extra={"is_mjpeg": sniffed["is_mjpeg"], "expected": info["expected"]},
        )
    except Exception as exc:  # noqa: BLE001
        latency_ms = round((time.monotonic() - start) * 1000.0, 2)
        result = UrlTestResult(
            url=original, expanded_url=expanded, ok=False, reachable=False,
            status="error", kind=info["kind"], latency_ms=latency_ms,
            error=f"{type(exc).__name__}: {exc}"[:500],
            checked_at=_now(), timeout_seconds=timeout_f,
        )
    _RESULTS[key] = result
    return result


# Aliases (repo convention: many names, same behaviour).
fetch_snapshot = test_url
fetch_url = test_url
check_url = test_url
probe_url = test_url
test_camera_url = test_url
test_snapshot_url = test_url
snapshot_probe = test_url
url_test = test_url
test = test_url


def test_template(
    preset: Any = None,
    host: Any = None,
    timeout: Any = DEFAULT_TIMEOUT_SECONDS,
    **kwargs: Any,
) -> UrlTestResult:
    """Test a Sources.xml-style preset dict against a camera host.

    ``preset`` is a ``{"prefix": ..., "url": ..., "port": ..., "source": ...}``
    mapping as produced by ``app.services.source_presets``. ``host`` is the
    camera IP/hostname; credentials use the ``username``/``password`` kwargs.
    """
    data: Dict[str, Any] = {}
    if isinstance(preset, dict):
        data = dict(preset)
    elif isinstance(preset, str):
        data = {"url": preset}
    path = data.get("url", data.get("Url", data.get("path", "")))
    prefix = data.get("prefix", data.get("Prefix", kwargs.get("prefix")))
    source = data.get("source", data.get("Source", kwargs.get("source")))
    port = data.get("port", data.get("Port", kwargs.get("port")))
    if host is None:
        host = kwargs.get("host", kwargs.get("hostname", kwargs.get("ip")))
    built = build_url(path, host=host, port=port, prefix=prefix, **kwargs)
    return test_url(built, timeout, source=source, **kwargs)


def get_status(target: Any = None, **kwargs: Any) -> Optional[UrlTestResult]:
    """Return the last test result for a URL, if any."""
    key = _normalize_url(target if target is not None else kwargs.get("url", kwargs.get("expanded_url")))
    if not key:
        return None
    if key in _RESULTS:
        return _RESULTS[key]
    expanded = expand_template(key, **kwargs) if kwargs else key
    return _RESULTS.get(expanded)


get = get_status
get_result = get_status
find = get_status
lookup = get_status
status_for = get_status


def list_results() -> List[UrlTestResult]:
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
    err_count = sum(1 for r in items if r.status == "error")
    unknown = total - ok_count - err_count
    return {
        "total": total,
        "tested": total,
        "ok_count": ok_count,
        "errors": err_count,
        "unknown": unknown,
        "reachable": ok_count,
        "healthy": ok_count,
        "ok": err_count == 0,
        "success": err_count == 0,
    }


summary = get_summary
get_stats = get_summary
stats = get_summary


def clear(target: Any = None, **kwargs: Any) -> None:
    global _RESULTS
    if target is None and "url" not in kwargs:
        _RESULTS.clear()
        return
    key = _normalize_url(target if target is not None else kwargs.get("url"))
    if key in _RESULTS:
        _RESULTS.pop(key, None)
    else:
        _RESULTS.clear()


clear_all = lambda: _RESULTS.clear()  # noqa: E731
reset_all = clear_all
reset = clear_all


def remove(target: Any = None, **kwargs: Any) -> bool:
    key = _normalize_url(target if target is not None else kwargs.get("url"))
    if not key:
        return False
    return _RESULTS.pop(key, None) is not None


delete = remove
drop = remove


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_READ_BYTES",
    "UrlTestResult",
    "UrlTesterResult",
    "TestResult",
    "expand_template",
    "has_template_tokens",
    "build_url",
    "classify_url",
    "test_url",
    "test_template",
    "fetch_snapshot",
    "fetch_url",
    "get_status",
    "list_results",
    "list_results_dicts",
    "get_summary",
    "clear",
    "remove",
]
