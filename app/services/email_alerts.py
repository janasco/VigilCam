from __future__ import annotations

import smtplib
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, List, Optional

from smtplib import SMTP as SMTP, SMTP_SSL as SMTP_SSL

try:
    from app.core import config as _config
except Exception:  # noqa: BLE001
    _config = None

try:
    from app.core.config import settings as _settings
except Exception:  # noqa: BLE001
    _settings = None


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
        elif isinstance(a, (str, Path)):
            p = Path(str(a)).expanduser()
            if p.exists():
                data.setdefault("file_path", str(a))
            else:
                if "@" in str(a):
                    data.setdefault("to", str(a))
                elif "target" not in data:
                    data.setdefault("target", str(a))
                else:
                    data.setdefault("to", str(a))
        elif isinstance(a, (bytes, bytearray)):
            try:
                data.setdefault("body", bytes(a).decode("utf-8", errors="replace"))
            except Exception:  # noqa: BLE001
                pass
        else:
            try:
                data.update({k: getattr(a, k) for k in dir(a) if not k.startswith("_") and not callable(getattr(a, k, None))})
            except Exception:  # noqa: BLE001
                pass
    data.update(kwargs or {})
    for nested_key in ("payload", "options", "config", "params", "request", "data", "body", "message"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            merged = dict(nested)
            for k, v in merged.items():
                data.setdefault(k, v)
    return data


def _to_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        out = []
        for v in value:
            out.extend(_to_list(v))
        return [x for x in out if x]
    s = str(value).strip()
    if not s:
        return []
    parts = [p.strip() for p in s.replace(";", ",").split(",")]
    return [p for p in parts if p]


def send_email(*args, **kwargs) -> Dict[str, Any]:
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
        timeout = 5.0
    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        timeout = 5.0

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = ", ".join(to_addrs)
    msg.set_content(body)

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

    result = {
        "ok": bool(start_ok),
        "success": bool(start_ok),
        "status": "success" if start_ok else "error",
        "detail": detail,
        "error": "" if start_ok else detail,
        "to": to_addrs,
        "recipient": to_addrs[0] if to_addrs else None,
        "subject": subject,
        "from": from_addr,
        "from_addr": from_addr,
        "host": host,
        "port": port,
    }
    if not start_ok:
        result.setdefault("message", detail)
    else:
        result.setdefault("message", "Email sent")
    return result


send = send_email
send_alert = send_email
send_alert_email = send_email
send_email_alert = send_email
send_smtp = send_email
smtp_send = send_email
send_message = send_email
email_alert = send_email
email_alerts = send_email
notify = send_email
notify_email = send_email
send_mail = send_email
mail = send_email

async def send_email_async(*args, **kwargs):
    import asyncio
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: send_email(*args, **kwargs))

asend = send_email_async
send_async = send_email_async


class EmailAlertService:
    def send_email(self, *a, **k):
        return send_email(*a, **k)

    def send(self, *a, **k):
        return send_email(*a, **k)

    def send_alert(self, *a, **k):
        return send_email(*a, **k)

    def send_alert_email(self, *a, **k):
        return send_email(*a, **k)

    def send_smtp(self, *a, **k):
        return send_email(*a, **k)

    async def send_async(self, *a, **k):
        return await send_email_async(*a, **k)


class EmailAlertsService(EmailAlertService):
    pass


class EmailService(EmailAlertService):
    pass


email_alert_service = EmailAlertService()
email_alerts_service = EmailAlertsService()
email_service = EmailService()
service = email_alerts_service
