"""Email-with-snapshot action: SMTP email with a JPEG snapshot attachment.

Relationship to existing modules (verified via grep):

* :mod:`app.services.email_alerts` sends text-only email — it builds an
  ``EmailMessage`` with ``msg.set_content(body)`` and never calls
  ``add_attachment`` / handles images.
* :mod:`app.services.image_webhook` attaches images only to webhooks — it
  POSTs ``multipart/form-data`` (``file``/``grab.jpg``/``image/jpeg``) or
  falls back to plain GET, and never touches ``smtplib`` / ``EmailMessage``.

This module fills the gap **without** duplicating either path: it sends an
SMTP email and, when snapshot bytes are supplied, attaches them via
``EmailMessage.add_attachment``. With no image it degrades to a text-only
email (``attached=False``) rather than failing.
"""
from __future__ import annotations

import base64
import smtplib
import time
import uuid
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from smtplib import SMTP as SMTP, SMTP_SSL as SMTP_SSL

try:
    from app.core import config as _config
except Exception:  # noqa: BLE001
    _config = None

try:
    from app.core.config import settings as _settings
except Exception:  # noqa: BLE001
    _settings = None


# iSpy/webhook-compatible wire constants (kept for cross-module parity).
FILENAME = "snapshot.jpg"
CONTENT_TYPE = "image/jpeg"
MAX_IMAGE_BYTES = 10 * 1024 * 1024
DEFAULT_TIMEOUT = 5.0

_TO_KEYS = (
    "to", "recipient", "email", "address", "target_email", "to_addr", "to_address",
    "recipient_email", "mail_to", "target", "to_emails", "recipients",
)
_SUBJECT_KEYS = ("subject", "title", "topic", "subject_line", "headline")
_BODY_KEYS = (
    "body", "message", "text", "content", "html", "msg", "payload", "mail_body",
    "email_body", "contents",
)
_HOST_KEYS = ("smtp_host", "host", "server", "smtp_server", "mail_host", "mail_server")
_PORT_KEYS = ("smtp_port", "port", "smtp_port_number")
_USERNAME_KEYS = ("username", "user", "smtp_username", "login", "auth_user")
_PASSWORD_KEYS = ("password", "pass", "smtp_password", "auth_password", "passwd")
_USE_TLS_KEYS = ("use_tls", "tls", "starttls", "smtp_tls", "ssl_tls")
_FROM_KEYS = ("from", "from_addr", "from_address", "sender", "mail_from", "from_email")
_TIMEOUT_KEYS = ("timeout", "smtp_timeout", "send_timeout")

IMAGE_BYTES_KEYS = (
    "image_bytes", "image", "snapshot_bytes", "snapshot", "rawgrab", "raw_grab",
    "grab", "jpeg_bytes", "jpg_bytes", "frame_bytes", "bytes", "content_bytes",
    "file_bytes", "data_bytes", "snapshot_data", "image_data",
)
IMAGE_B64_KEYS = (
    "image_base64", "image_b64", "base64", "b64", "jpeg_base64", "snapshot_base64",
    "snapshot_b64",
)
IMAGE_PATH_KEYS = (
    "image_path", "snapshot_path", "frame_path", "file_path", "snapshot_file",
    "path", "file", "filename", "image_file", "jpeg_path", "jpg",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _first(data: Dict[str, Any], keys: tuple) -> Any:
    for k in keys:
        if k in data and data.get(k) is not None:
            return data.get(k)
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
            p = Path(a.strip()).expanduser() if a.strip() else None
            if p is not None and p.exists() and p.is_file():
                data.setdefault("image_path", str(a))
            elif "@" in a and " " not in a.strip():
                data.setdefault("to", a.strip())
            elif a.strip().lower().startswith(("http://", "https://")):
                data.setdefault("image_path", a.strip())
            else:
                data.setdefault("body", a)
        else:
            try:
                data.update({k: getattr(a, k) for k in dir(a) if not k.startswith("_") and not callable(getattr(a, k, None))})
            except Exception:  # noqa: BLE001
                pass
    data.update(kwargs or {})
    for nested_key in ("payload", "options", "config", "params", "request", "data", "body", "message"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _to_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        out: List[str] = []
        for v in value:
            out.extend(_to_list(v))
        return [x for x in out if x]
    s = str(value).strip()
    if not s:
        return []
    parts = [p.strip() for p in s.replace(";", ",").split(",")]
    return [p for p in parts if p]


def _decode_base64(value: Any) -> Optional[bytes]:
    if value is None or isinstance(value, (bytes, bytearray)):
        return bytes(value) if value is not None else None
    s = str(value).strip()
    if not s:
        return None
    if "," in s and s.startswith("data:"):
        s = s.split(",", 1)[1]
    s = "".join(s.split())
    missing = len(s) % 4
    if missing:
        s += "=" * (4 - missing)
    try:
        return base64.b64decode(s, validate=False)
    except Exception:  # noqa: BLE001
        return None


def _looks_like_jpeg(payload: bytes) -> bool:
    return len(payload) >= 2 and payload[0] == 0xFF and payload[1] == 0xD8


def _looks_like_png(payload: bytes) -> bool:
    return len(payload) >= 8 and payload[:8] == b"\x89PNG\r\n\x1a\n"


def resolve_image_bytes(data: Dict[str, Any]) -> Tuple[Optional[bytes], Optional[str], Optional[str]]:
    """Resolve snapshot bytes from normalised inputs.

    Returns ``(bytes_or_None, source, detail)`` where source is one of
    ``bytes`` / ``base64`` / ``path`` / ``None``.
    """
    raw = _first(data, IMAGE_BYTES_KEYS)
    if isinstance(raw, (bytes, bytearray)) and len(raw) > 0:
        return bytes(raw), "bytes", None
    if isinstance(raw, str) and raw.strip():
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
    if path_val is not None and not isinstance(path_val, (bytes, bytearray)):
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


def _guess_attachment(image: bytes, filename: str) -> Tuple[str, str, str]:
    """Return (maintype, subtype, filename) for an image payload."""
    fname = Path(str(filename or FILENAME)).name or FILENAME
    if _looks_like_jpeg(image):
        if not Path(fname).suffix:
            fname = FILENAME
        return "image", "jpeg", fname
    if _looks_like_png(image):
        if Path(fname).suffix.lower() not in (".png",):
            fname = Path(fname).stem + ".png" if Path(fname).suffix else "snapshot.png"
        return "image", "png", fname
    suffix = Path(fname).suffix.lower().lstrip(".")
    if suffix in ("jpg", "jpeg"):
        return "image", "jpeg", fname
    if suffix in ("png",):
        return "image", "png", fname
    if suffix in ("gif",):
        return "image", "gif", fname
    if suffix in ("webp",):
        return "image", "webp", fname
    if suffix in ("bmp",):
        return "image", "bmp", fname
    return "image", "jpeg", fname


def send_email_with_snapshot(*args, **kwargs) -> Dict[str, Any]:
    """Send an SMTP email, attaching a snapshot image when provided.

    Text-only when no image is supplied (``attached=False``); never touches
    webhooks. Mirrors :mod:`app.services.email_alerts` SMTP handling.
    """
    data = _normalize_args(args, kwargs)

    to_raw = _first(data, _TO_KEYS)
    to_addrs = _to_list(to_raw)
    if not to_addrs:
        t = data.get("target")
        if isinstance(t, str) and t.strip():
            to_addrs = [t.strip()]
    if not to_addrs:
        return {
            "ok": False,
            "success": False,
            "status": "error",
            "detail": "recipient is required",
            "error": "recipient is required",
        }

    subject = _first(data, _SUBJECT_KEYS)
    if subject is None:
        subject = "VigilCam alert"
    subject = str(subject)

    body = _first(data, _BODY_KEYS)
    if body is None:
        body = ""
    body = str(body)

    cfg = _settings if _settings is not None else _config
    host = _first(data, _HOST_KEYS)
    if host is None:
        host = getattr(cfg, "SMTP_HOST", None) if cfg is not None else None
    if host is None:
        host = "localhost"
    host = str(host)

    port = _first(data, _PORT_KEYS)
    if port is None:
        port = getattr(cfg, "SMTP_PORT", 587) if cfg is not None else 587
    try:
        port = int(port)
    except (TypeError, ValueError):
        port = 587

    username = _first(data, _USERNAME_KEYS)
    if username is None:
        username = getattr(cfg, "SMTP_USERNAME", "") if cfg is not None else ""
    username = str(username or "")

    password = _first(data, _PASSWORD_KEYS)
    if password is None:
        password = getattr(cfg, "SMTP_PASSWORD", "") if cfg is not None else ""
    password = str(password or "")

    use_tls = _first(data, _USE_TLS_KEYS)
    if use_tls is None:
        use_tls = getattr(cfg, "SMTP_USE_TLS", True) if cfg is not None else True
    use_tls = bool(use_tls)

    from_addr = _first(data, _FROM_KEYS)
    if from_addr is None:
        from_addr = getattr(cfg, "SMTP_FROM", None) if cfg is not None else None
    if from_addr is None:
        from_addr = username or "vigilcam@localhost"
    from_addr = str(from_addr)

    timeout = _first(data, _TIMEOUT_KEYS)
    if timeout is None:
        timeout = DEFAULT_TIMEOUT
    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT

    filename = str(data.get("filename") or data.get("image_filename") or data.get("snapshot_filename") or FILENAME)
    filename = Path(filename).name or FILENAME

    image, image_source, image_err = resolve_image_bytes(data)
    attach = image is not None
    if data.get("attach_image") is False or data.get("attach") is False:
        attach = False

    if attach and image is not None and len(image) > MAX_IMAGE_BYTES:
        return {
            "ok": False,
            "success": False,
            "status": "error",
            "detail": "image exceeds {} bytes".format(MAX_IMAGE_BYTES),
            "error": "image exceeds {} bytes".format(MAX_IMAGE_BYTES),
            "attached": False,
            "image_source": image_source,
            "image_bytes": len(image),
        }
    if image is not None and len(image) == 0:
        attach = False
        image = None
        image_source = None

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = ", ".join(to_addrs)
    msg.set_content(body)

    maintype, subtype, final_filename = FILENAME and ("image", "jpeg", filename) or ("image", "jpeg", filename)
    content_type = CONTENT_TYPE
    if attach and image is not None:
        maintype, subtype, final_filename = _guess_attachment(image, filename)
        content_type = "{}/{}".format(maintype, subtype)
        msg.add_attachment(image, maintype=maintype, subtype=subtype, filename=final_filename)

    start = time.monotonic()
    start_ok = False
    detail = ""
    try:
        _smtp_cls = globals().get("SMTP")
        if _smtp_cls is None or _smtp_cls is SMTP:
            _smtp_cls = getattr(smtplib, "SMTP", SMTP)
        _smtp_ssl_cls = globals().get("SMTP_SSL")
        if _smtp_ssl_cls is None or _smtp_ssl_cls is SMTP_SSL:
            _smtp_ssl_cls = getattr(smtplib, "SMTP_SSL", SMTP_SSL)
        if port == 465:
            server = _smtp_ssl_cls(host, port, timeout=timeout)
        else:
            server = _smtp_cls(host, port, timeout=timeout)
        try:
            try:
                server.ehlo()
            except Exception:  # noqa: BLE001
                pass
            if use_tls and port != 465:
                try:
                    server.starttls()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    server.ehlo()
                except Exception:  # noqa: BLE001
                    pass
            if username:
                server.login(username, password)
            server.send_message(msg)
            start_ok = True
            detail = "sent"
        finally:
            try:
                server.quit()
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        detail = str(exc)
        start_ok = False

    latency_ms = (time.monotonic() - start) * 1000.0
    result: Dict[str, Any] = {
        "ok": bool(start_ok),
        "success": bool(start_ok),
        "status": "success" if start_ok else "error",
        "detail": detail,
        "error": "" if start_ok else detail,
        "action": "email_snapshot",
        "type": "email_snapshot",
        "to": to_addrs,
        "recipient": to_addrs[0] if to_addrs else None,
        "subject": subject,
        "from": from_addr,
        "from_addr": from_addr,
        "host": host,
        "port": port,
        "attached": bool(attach and image is not None and start_ok or (attach and image is not None and start_ok)),
        "attach_image": bool(attach),
        "image_source": image_source,
        "image_bytes": len(image) if image is not None else 0,
        "image_size": len(image) if image is not None else 0,
        "filename": final_filename if (attach and image is not None) else filename,
        "content_type": content_type if (attach and image is not None) else "text/plain",
        "latency_ms": round(latency_ms, 1),
        "request_id": uuid.uuid4().hex[:12],
        "sent_at": _now_iso(),
    }
    # attached reflects intent+availability; on SMTP failure keep intent visible
    # but ok=False signals failure. Recompute strictly: attached only on success.
    if not start_ok:
        result["attached"] = False
    else:
        result["attached"] = bool(attach and image is not None)
    if image is not None and _looks_like_jpeg(image):
        result["image_format"] = "jpeg"
    elif image is not None and _looks_like_png(image):
        result["image_format"] = "png"
    if not start_ok:
        result.setdefault("message", detail)
    else:
        result.setdefault("message", "Email sent")
    if image_err and image is None:
        result["image_detail"] = image_err
    return result


async def send_email_with_snapshot_async(*args, **kwargs):
    import asyncio
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: send_email_with_snapshot(*args, **kwargs))


# Compatibility aliases (repo convention: many names, same behaviour).
send = send_email_with_snapshot
send_snapshot = send_email_with_snapshot
send_snapshot_email = send_email_with_snapshot
send_email_snapshot = send_email_with_snapshot
email_snapshot = send_email_with_snapshot
email_with_snapshot = send_email_with_snapshot
notify_snapshot = send_email_with_snapshot
snapshot_email = send_email_with_snapshot

asend = send_email_with_snapshot_async
send_async = send_email_with_snapshot_async


class EmailSnapshotService:
    """Object-oriented facade over :func:`send_email_with_snapshot`."""

    def send(self, *a, **k):
        return send_email_with_snapshot(*a, **k)

    def send_email(self, *a, **k):
        return send_email_with_snapshot(*a, **k)

    def send_snapshot(self, *a, **k):
        return send_email_with_snapshot(*a, **k)

    def send_snapshot_email(self, *a, **k):
        return send_email_with_snapshot(*a, **k)

    def execute(self, *a, **k):
        return send_email_with_snapshot(*a, **k)

    async def asend(self, *a, **k):
        return await send_email_with_snapshot_async(*a, **k)

    async def send_async(self, *a, **k):
        return await send_email_with_snapshot_async(*a, **k)


EmailSnapshotsService = EmailSnapshotService
EmailWithSnapshotService = EmailSnapshotService
SnapshotEmailService = EmailSnapshotService

email_snapshot_service = EmailSnapshotService()
email_snapshots_service = EmailSnapshotService()
service = email_snapshot_service
manager = email_snapshot_service

__all__ = [
    "FILENAME",
    "CONTENT_TYPE",
    "MAX_IMAGE_BYTES",
    "send_email_with_snapshot",
    "send_email_with_snapshot_async",
    "resolve_image_bytes",
    "EmailSnapshotService",
    "email_snapshot_service",
]
