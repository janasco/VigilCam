"""Image-attached webhook action (iSpy ``URL`` + snapshot parity).

iSpy reference (``/opt/others/ispy/Controls/CameraWindow.cs``,
``ProcessAlertEvent``, ``case "URL"``):

* ``param1`` = target URL, ``param2`` = post-grab flag, ``rawgrab`` = JPEG bytes.
* When ``postgrab`` is true and ``rawgrab`` is present, iSpy POSTs
  ``multipart/form-data`` with field ``name="file"``, ``filename="grab.jpg"``,
  ``Content-Type: image/jpeg`` and the raw JPEG bytes.
* Otherwise it issues a plain ``GET`` against the URL (no JSON body).

Gap in :mod:`app.services.actions` (verified via grep): the ``webhook``
action only ever does ``httpx.post(url, json=...)`` — no ``multipart``,
no ``files=``, no ``grab.jpg``/``image/jpeg`` handling. This module fills
that gap **without** duplicating plain webhooks: it *requires* an image
for the multipart POST path and falls back to a plain GET (like iSpy)
when no image is attached.
"""
from __future__ import annotations

import asyncio
import base64
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import httpx  # type: ignore
except Exception:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

# iSpy-compatible wire constants.
FIELD_NAME = "file"
FILENAME = "grab.jpg"
CONTENT_TYPE = "image/jpeg"
MAX_IMAGE_BYTES = 10 * 1024 * 1024
DEFAULT_TIMEOUT = 10.0

URL_KEYS = (
    "url", "target", "uri", "endpoint", "webhook_url", "webhook",
    "callback_url", "hook_url", "destination", "dest", "address", "href",
)
IMAGE_BYTES_KEYS = (
    "image_bytes", "image", "rawgrab", "raw_grab", "grab", "jpeg_bytes",
    "jpg_bytes", "frame_bytes", "snapshot_bytes", "bytes", "content_bytes",
    "file_bytes", "data_bytes",
)
IMAGE_PATH_KEYS = (
    "image_path", "snapshot_path", "snapshot", "frame_path", "file_path",
    "path", "file", "filename", "image_file", "jpeg_path", "jpg",
)
IMAGE_B64_KEYS = (
    "image_base64", "image_b64", "base64", "b64", "jpeg_base64", "snapshot_base64",
)
EVENT_KEYS = ("event_id", "eventId", "event", "id", "alert_id", "alertId")
TIMEOUT_KEYS = ("timeout", "timeout_seconds", "request_timeout", "deadline")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _first(data: Dict[str, Any], keys: tuple) -> Any:
    for k in keys:
        if k in data and data.get(k) is not None:
            return data.get(k)
    lower = {str(k).lower(): v for k, v in data.items()}
    for k in keys:
        if k.lower() in lower and lower[k.lower()] is not None:
            return lower[k.lower()]
    return None


def _normalize_args(args: tuple, kwargs: Dict[str, Any]) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for a in args or ():
        if isinstance(a, dict):
            data.update(a)
        elif isinstance(a, (bytes, bytearray)):
            data.setdefault("image_bytes", bytes(a))
        elif isinstance(a, Path):
            data.setdefault("image_path", str(a))
        elif isinstance(a, str):
            s = a.strip()
            if not s:
                continue
            if s.lower().startswith(("http://", "https://")):
                data.setdefault("url", s)
            else:
                p = Path(s).expanduser()
                try:
                    if p.is_file():
                        data.setdefault("image_path", s)
                        continue
                except Exception:  # noqa: BLE001
                    pass
                # data: URL or bare base64 blob?
                if s.startswith("data:image") or (len(s) > 64 and all(
                    c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/= \n\r\t" for c in s[:64]
                )):
                    data.setdefault("image_base64", s)
                elif "url" not in data:
                    data.setdefault("url", s)
                else:
                    data.setdefault("image_path", s)
        else:
            try:
                attrs = {k: getattr(a, k) for k in dir(a) if not k.startswith("_") and not callable(getattr(a, k, None))}
                if attrs:
                    data.update(attrs)
            except Exception:  # noqa: BLE001
                pass
    data.update(kwargs or {})
    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "settings", "message"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _decode_base64(value: Any) -> Optional[bytes]:
    if value is None or isinstance(value, (bytes, bytearray)):
        return bytes(value) if value is not None else None
    s = str(value).strip()
    if not s:
        return None
    if "," in s and s.startswith("data:"):
        # data:image/jpeg;base64,....
        s = s.split(",", 1)[1]
    s = "".join(s.split())
    # pad
    missing = len(s) % 4
    if missing:
        s += "=" * (4 - missing)
    try:
        return base64.b64decode(s, validate=False)
    except Exception:  # noqa: BLE001
        return None


def _looks_like_jpeg(payload: bytes) -> bool:
    return len(payload) >= 2 and payload[0] == 0xFF and payload[1] == 0xD8


def resolve_image_bytes(data: Dict[str, Any]) -> Tuple[Optional[bytes], Optional[str], Optional[str]]:
    """Resolve image bytes from normalised inputs.

    Returns ``(bytes_or_None, source, detail)`` where source is one of
    ``bytes`` / ``base64`` / ``path`` / ``None``.
    """
    raw = _first(data, IMAGE_BYTES_KEYS)
    if isinstance(raw, (bytes, bytearray)) and len(raw) > 0:
        blob = bytes(raw)
        return blob, "bytes", None
    if isinstance(raw, str) and raw.strip():
        # A str under an image-bytes key may be base64 or a path.
        maybe_path = Path(raw.strip()).expanduser()
        try:
            if maybe_path.is_file():
                try:
                    blob = maybe_path.read_bytes()
                    if blob:
                        return blob, "path", None
                except Exception as exc:  # noqa: BLE001
                    return None, None, str(exc)
        except Exception:  # noqa: BLE001
            pass
        decoded = _decode_base64(raw)
        if decoded:
            return decoded, "base64", None

    b64 = _first(data, IMAGE_B64_KEYS)
    if b64 is not None:
        decoded = _decode_base64(b64)
        if decoded:
            return decoded, "base64", None
        return None, None, "invalid base64 image"

    path_val = _first(data, IMAGE_PATH_KEYS)
    # NOTE: ``path``/``file`` keys overlap with plain-webhook target fields,
    # so only treat the value as an image path when it points at a real file
    # with an image suffix (prevents http URLs from being misread as paths).
    if path_val is not None:
        candidate = str(path_val).strip()
        if candidate and not candidate.lower().startswith(("http://", "https://")):
            p = Path(candidate).expanduser()
            try:
                if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png", ".bmp", ".webp"):
                    try:
                        blob = p.read_bytes()
                    except Exception as exc:  # noqa: BLE001
                        return None, None, str(exc)
                    if not blob:
                        return None, None, "image file is empty"
                    return blob, "path", None
            except Exception:  # noqa: BLE001
                pass
    return None, None, None


def _validate_url(url: Any) -> Tuple[Optional[str], Optional[str]]:
    if url is None:
        return None, "url is required"
    s = str(url).strip()
    if not s:
        return None, "url is required"
    if not s.lower().startswith(("http://", "https://")):
        return None, "url must be an http(s) URL"
    return s, None


def _form_fields(data: Dict[str, Any], event_id: Any) -> Dict[str, str]:
    fields: Dict[str, str] = {}
    if event_id is not None:
        fields["event_id"] = str(event_id)
    for key in ("camera_id", "camera", "label", "kind", "event_kind", "message", "text"):
        val = data.get(key)
        if val is not None and key not in fields:
            fields[key] = str(val)
    # Allow explicit extra form fields via a nested dict.
    extra = data.get("form_fields") or data.get("fields") or data.get("form")
    if isinstance(extra, dict):
        for k, v in extra.items():
            if v is not None:
                fields.setdefault(str(k), str(v))
    return fields


def _post_multipart_sync(
    url: str,
    image: bytes,
    fields: Dict[str, str],
    timeout: float,
    filename: str = FILENAME,
) -> Tuple[Optional[int], Optional[str], str]:
    """POST multipart/form-data. Returns (status_code, response_text, detail)."""
    http = globals().get("httpx")
    if http is None:
        try:
            import httpx as _httpx  # type: ignore

            http = _httpx
        except Exception:  # noqa: BLE001
            http = None
    if http is not None:
        files = {FIELD_NAME: (filename, image, CONTENT_TYPE)}
        resp = http.post(url, files=files, data=fields, timeout=timeout)
        code = getattr(resp, "status_code", None)
        text = None
        try:
            text = (getattr(resp, "text", "") or "")[:500]
        except Exception:  # noqa: BLE001
            text = None
        detail = "HTTP {}".format(code)
        return code, text, detail
    # urllib fallback — manual multipart body mirroring iSpy's construction.
    import urllib.request as _request
    import uuid as _uuid

    boundary = "---------------------------{}".format(_uuid.uuid4().hex)
    crlf = "\r\n"
    parts: List[bytes] = []
    for k, v in fields.items():
        parts.append(
            ("--" + boundary + crlf + 'Content-Disposition: form-data; name="{}"'.format(k) + crlf + crlf + v + crlf).encode("utf-8")
        )
    header = (
        "--" + boundary + crlf
        + 'Content-Disposition: form-data; name="{}"; filename="{}"'.format(FIELD_NAME, filename) + crlf
        + "Content-Type: {}".format(CONTENT_TYPE) + crlf + crlf
    ).encode("utf-8")
    body = b"".join(parts) + header + image + ("{}--{}--{}".format(crlf, boundary, crlf)).encode("ascii")
    req = _request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "multipart/form-data; boundary=" + boundary)
    try:
        with _request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            code = getattr(resp, "status", 200)
            return int(code), None, "HTTP {}".format(code)
    except Exception as exc:  # noqa: BLE001
        raise exc


def _get_plain_sync(url: str, timeout: float) -> Tuple[Optional[int], Optional[str], str]:
    """Plain GET with no body — iSpy's no-image branch."""
    http = globals().get("httpx")
    if http is None:
        try:
            import httpx as _httpx  # type: ignore

            http = _httpx
        except Exception:  # noqa: BLE001
            http = None
    if http is not None:
        resp = http.get(url, timeout=timeout)
        code = getattr(resp, "status_code", None)
        text = None
        try:
            text = (getattr(resp, "text", "") or "")[:500]
        except Exception:  # noqa: BLE001
            text = None
        return code, text, "HTTP {}".format(code)
    import urllib.request as _request

    with _request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
        code = getattr(resp, "status", 200)
        return int(code), None, "HTTP {}".format(code)


def send_image_webhook(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Send an alert URL call with an attached snapshot image.

    iSpy parity: multipart ``file``/``grab.jpg``/``image/jpeg`` POST when an
    image is available, plain GET otherwise. Never sends a plain JSON webhook
    body (that path belongs to :mod:`app.services.actions`).
    """
    data = _normalize_args(args, kwargs)
    url = _first(data, URL_KEYS)
    url, url_err = _validate_url(url)
    event_id = _first(data, EVENT_KEYS)
    try:
        timeout = float(_first(data, TIMEOUT_KEYS) or data.get("timeout") or DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT

    filename = str(data.get("filename") or data.get("image_filename") or FILENAME)
    filename = Path(filename).name or FILENAME

    image, image_source, image_err = resolve_image_bytes(data)
    attach = image is not None
    # Explicit opt-out: attach_image=False forces the plain-GET branch.
    if data.get("attach_image") is False or data.get("attach") is False or data.get("postgrab") is False:
        attach = False

    start = time.monotonic()
    status_code: Optional[int] = None
    response_text: Optional[str] = None
    ok = False
    if url_err:
        detail = url_err
    elif attach and image is not None:
        if len(image) > MAX_IMAGE_BYTES:
            detail = "image exceeds {} bytes".format(MAX_IMAGE_BYTES)
        elif len(image) == 0:
            detail = "image is empty"
        else:
            fields = _form_fields(data, event_id)
            try:
                status_code, response_text, detail = _post_multipart_sync(url, image, fields, timeout, filename=filename)
                ok = status_code is not None and 200 <= int(status_code) < 300
            except Exception as exc:  # noqa: BLE001
                detail = str(exc)
                ok = False
    else:
        # No image (or opted out): plain GET like iSpy's non-postgrab branch.
        if image_err and not attach:
            pass  # invalid base64 with no fallback still degrades to GET probe
        try:
            status_code, response_text, detail = _get_plain_sync(str(url), timeout)
            ok = status_code is not None and 200 <= int(status_code) < 300
        except Exception as exc:  # noqa: BLE001
            detail = str(exc)
            ok = False
    latency_ms = (time.monotonic() - start) * 1000.0
    result: Dict[str, Any] = {
        "ok": bool(ok),
        "success": bool(ok),
        "status": "success" if ok else "error",
        "detail": detail,
        "action": "image_webhook",
        "type": "image_webhook",
        "target": url,
        "url": url,
        "event_id": event_id,
        "status_code": status_code,
        "latency_ms": round(latency_ms, 1),
        "response_text": response_text,
        "attached": bool(attach and image is not None and not url_err),
        "attach_image": bool(attach),
        "image_source": image_source,
        "image_bytes": len(image) if image is not None else 0,
        "image_size": len(image) if image is not None else 0,
        "field_name": FIELD_NAME,
        "filename": filename,
        "content_type": CONTENT_TYPE,
        "mode": "multipart" if (attach and image is not None) else "get",
        "request_id": uuid.uuid4().hex[:12],
        "sent_at": _now_iso(),
    }
    if image is not None and _looks_like_jpeg(image):
        result["image_format"] = "jpeg"
    return result


async def send_image_webhook_async(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Async variant — multipart POST via ``httpx.AsyncClient`` or plain GET."""
    data = _normalize_args(args, kwargs)
    url = _first(data, URL_KEYS)
    url, url_err = _validate_url(url)
    event_id = _first(data, EVENT_KEYS)
    try:
        timeout = float(_first(data, TIMEOUT_KEYS) or data.get("timeout") or DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT
    filename = str(data.get("filename") or data.get("image_filename") or FILENAME)
    filename = Path(filename).name or FILENAME
    image, image_source, image_err = resolve_image_bytes(data)
    attach = image is not None
    if data.get("attach_image") is False or data.get("attach") is False or data.get("postgrab") is False:
        attach = False

    start = time.monotonic()
    status_code: Optional[int] = None
    response_text: Optional[str] = None
    ok = False
    detail = ""
    if url_err:
        detail = url_err
    elif attach and image is not None:
        if len(image) > MAX_IMAGE_BYTES:
            detail = "image exceeds {} bytes".format(MAX_IMAGE_BYTES)
        else:
            fields = _form_fields(data, event_id)
            http = globals().get("httpx")
            if http is None:
                try:
                    import httpx as _httpx  # type: ignore

                    http = _httpx
                except Exception:  # noqa: BLE001
                    http = None
            try:
                if http is not None and hasattr(http, "AsyncClient"):
                    files = {FIELD_NAME: (filename, image, CONTENT_TYPE)}
                    async with http.AsyncClient(timeout=timeout) as client:
                        resp = await client.post(url, files=files, data=fields)
                    status_code = getattr(resp, "status_code", None)
                    try:
                        response_text = (getattr(resp, "text", "") or "")[:500]
                    except Exception:  # noqa: BLE001
                        response_text = None
                    detail = "HTTP {}".format(status_code)
                else:
                    status_code, response_text, detail = await asyncio.to_thread(
                        _post_multipart_sync, str(url), image, fields, timeout, filename
                    )
                ok = status_code is not None and 200 <= int(status_code) < 300
            except Exception as exc:  # noqa: BLE001
                detail = str(exc)
                ok = False
    else:
        http = globals().get("httpx")
        if http is None:
            try:
                import httpx as _httpx  # type: ignore

                http = _httpx
            except Exception:  # noqa: BLE001
                http = None
        try:
            if http is not None and hasattr(http, "AsyncClient"):
                async with http.AsyncClient(timeout=timeout) as client:
                    resp = await client.get(str(url))
                status_code = getattr(resp, "status_code", None)
                try:
                    response_text = (getattr(resp, "text", "") or "")[:500]
                except Exception:  # noqa: BLE001
                    response_text = None
                detail = "HTTP {}".format(status_code)
            else:
                status_code, response_text, detail = await asyncio.to_thread(_get_plain_sync, str(url), timeout)
            ok = status_code is not None and 200 <= int(status_code) < 300
        except Exception as exc:  # noqa: BLE001
            detail = str(exc)
            ok = False
    latency_ms = (time.monotonic() - start) * 1000.0
    return {
        "ok": bool(ok),
        "success": bool(ok),
        "status": "success" if ok else "error",
        "detail": detail,
        "action": "image_webhook",
        "type": "image_webhook",
        "target": url,
        "url": url,
        "event_id": event_id,
        "status_code": status_code,
        "latency_ms": round(latency_ms, 1),
        "response_text": response_text,
        "attached": bool(attach and image is not None and not url_err),
        "attach_image": bool(attach),
        "image_source": image_source,
        "image_bytes": len(image) if image is not None else 0,
        "image_size": len(image) if image is not None else 0,
        "field_name": FIELD_NAME,
        "filename": filename,
        "content_type": CONTENT_TYPE,
        "mode": "multipart" if (attach and image is not None) else "get",
        "request_id": uuid.uuid4().hex[:12],
        "sent_at": _now_iso(),
    }


class ImageWebhookService:
    """Object-oriented facade over :func:`send_image_webhook`."""

    def send(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return send_image_webhook(*args, **kwargs)

    def post(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return send_image_webhook(*args, **kwargs)

    def execute(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return send_image_webhook(*args, **kwargs)

    def trigger(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return send_image_webhook(*args, **kwargs)

    def run(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return send_image_webhook(*args, **kwargs)

    async def asend(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return await send_image_webhook_async(*args, **kwargs)

    async def aexecute(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return await send_image_webhook_async(*args, **kwargs)


# Compatibility aliases (repo convention: many names, same behaviour).
post_image_webhook = send_image_webhook
deliver_image_webhook = send_image_webhook
trigger_image_webhook = send_image_webhook
execute_image_webhook = send_image_webhook
run_image_webhook = send_image_webhook
send = send_image_webhook
post = send_image_webhook
trigger = send_image_webhook
execute = send_image_webhook
run = send_image_webhook
call_url_with_image = send_image_webhook
post_grab = send_image_webhook
postgrab = send_image_webhook

image_webhook_service = ImageWebhookService()
service = image_webhook_service
manager = image_webhook_service

ImageWebhook = ImageWebhookService
ImageWebhookSender = ImageWebhookService
ImageWebhookManager = ImageWebhookService

__all__ = [
    "FIELD_NAME",
    "FILENAME",
    "CONTENT_TYPE",
    "MAX_IMAGE_BYTES",
    "send_image_webhook",
    "send_image_webhook_async",
    "resolve_image_bytes",
    "ImageWebhookService",
    "image_webhook_service",
]
