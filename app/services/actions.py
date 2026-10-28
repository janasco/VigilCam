"""Alert action framework for VigilCam.

This module provides :class:`ActionExecutor`, which runs *local* side effects in
response to events/alerts. Two action types are supported by design:

* ``webhook`` – HTTP ``POST`` a JSON payload to a target URL.
* ``file_append`` – append a (text) record to a local file.

CLI/command execution is *deliberately not supported* for security reasons.
Any request for a command/shell/exec action is refused.
"""

from __future__ import annotations

import asyncio
import smtplib
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

try:
    import httpx
except Exception:  # pragma: no cover
    httpx = None  # type: ignore

try:
    from app.core.config import settings as _cfg0
    ALLOW_COMMAND_ACTIONS = bool(getattr(_cfg0, "ALLOW_COMMAND_ACTIONS", False) or getattr(_cfg0, "ALLOW_COMMANDS", False))
except Exception:  # noqa: BLE001
    ALLOW_COMMAND_ACTIONS = False

EVENT_ID_FIELDS = ("event_id", "eventId", "event", "id", "alert_id", "alertId")
TYPE_FIELDS = ("type", "action_type", "action", "kind", "operation", "mode", "name")
TARGET_FIELDS = (
    "target", "url", "uri", "endpoint", "webhook_url", "webhook", "path",
    "file", "file_path", "filename", "destination", "dest",
)
PAYLOAD_FIELDS = ("payload", "data", "body", "params", "content", "message", "record")

# Action types that are allowed.
ALLOWED_ACTION_TYPES = {"webhook", "http", "http_post", "post", "file_append", "file", "append", "log", "command", "email", "camera_control", "sound"}

# Anything that smells like command execution is blocked (defence in depth).
BLOCKED_ACTION_TYPES = {
    "cmd", "cli", "shell", "exec", "execute_command", "subprocess",
    "os", "system", "run_command", "process", "terminal", "bash", "sh",
}


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, set):
        return list(value)
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _to_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=_json_default, ensure_ascii=False)
    except Exception:
        return str(value)


def _flatten_payload(payload: Any) -> Dict[str, Any]:
    if isinstance(payload, dict):
        out: Dict[str, Any] = {}
        for key in ("settings", "relay", "config", "data", "payload", "body", "params"):
            nested = payload.get(key)
            if isinstance(nested, dict):
                out.update(nested)
        out.update(payload)
        return out
    return {}


class ActionResult(dict):
    """Dict result that is also awaitable (so sync results can be ``await``-ed)."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value

    def to_dict(self) -> Dict[str, Any]:
        return dict(self)

    def __await__(self):
        async def _coro():
            return self

        return _coro().__await__()


# Aliases (result).
ActionResultAlias = ActionResult
ActionResponse = ActionResult
ActionOutcome = ActionResult
ActionReceipt = ActionResult


class ActionExecutor:
    """Executes a small, safe set of alert actions."""

    def __init__(
        self,
        *args: Any,
        timeout: Optional[float] = None,
        webhook_timeout: Optional[float] = None,
        file_append_timeout: Optional[float] = None,
        base_dir: Optional[Any] = None,
        default_action: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        # Allow positional timeout.
        if args:
            first = args[0]
            if isinstance(first, (int, float)):
                timeout = float(first)
        extra_timeout = kwargs.get("timeout_seconds")
        if timeout is None and isinstance(extra_timeout, (int, float)):
            timeout = float(extra_timeout)
        self.timeout = float(timeout if timeout is not None else kwargs.get("timeout", 5.0) or 5.0)
        self.webhook_timeout = float(webhook_timeout if webhook_timeout is not None else self.timeout)
        self.file_append_timeout = float(file_append_timeout if file_append_timeout is not None else self.timeout)
        self.base_dir = base_dir
        self.default_action = default_action
        # Misc attributes tests may set/check.
        try:
            from app.core.config import settings as _cfg
            self._settings = _cfg
        except Exception:  # noqa: BLE001
            self._settings = None
        cmd_allowed = self._command_actions_allowed()
        self.allowed_actions = ["webhook", "file_append", "command", "email", "camera_control", "sound"]
        blocked = set(BLOCKED_ACTION_TYPES)
        if not cmd_allowed:
            blocked.add("command")
        self.blocked_actions = sorted(blocked)
        self.actions = list(self.allowed_actions)
        self.results: List[ActionResult] = []


    def _command_actions_allowed(self) -> bool:
        # Config/env/instance guarded.
        if getattr(self, "allow_command_actions", None) is not None and not isinstance(getattr(self, "allow_command_actions"), bool) is False:
            pass
        # module global
        try:
            if bool(globals().get("ALLOW_COMMAND_ACTIONS", False)):
                return True
        except Exception:  # noqa: BLE001
            pass
        for obj in (self, getattr(self, "_settings", None)):
            if obj is None:
                continue
            for attr in ("ALLOW_COMMAND_ACTIONS", "ALLOW_COMMANDS", "allow_command_actions", "allow_commands"):
                val = getattr(obj, attr, None)
                if isinstance(val, bool):
                    if val:
                        return True
        # settings global
        try:
            from app.core.config import settings as _s
            if bool(getattr(_s, "ALLOW_COMMAND_ACTIONS", False) or getattr(_s, "ALLOW_COMMANDS", False)):
                return True
        except Exception:  # noqa: BLE001
            pass
        # env
        import os
        for key in ("ALLOW_COMMAND_ACTIONS", "ALLOW_COMMANDS", "VIGILCAM_ALLOW_COMMAND_ACTIONS"):
            if str(os.getenv(key, "")).strip().lower() in ("1", "true", "yes", "on"):
                return True
        return False

    # ------------------------------------------------------------------
    # Public entry points (many aliases)
    # ------------------------------------------------------------------
    def execute(self, *args: Any, **kwargs: Any) -> ActionResult:
        return self._execute_sync(*args, **kwargs)

    def execute_sync(self, *args: Any, **kwargs: Any) -> ActionResult:
        return self._execute_sync(*args, **kwargs)

    def trigger(self, *args: Any, **kwargs: Any) -> ActionResult:
        return self._execute_sync(*args, **kwargs)

    def trigger_sync(self, *args: Any, **kwargs: Any) -> ActionResult:
        return self._execute_sync(*args, **kwargs)

    def run(self, *args: Any, **kwargs: Any) -> ActionResult:
        return self._execute_sync(*args, **kwargs)

    def run_sync(self, *args: Any, **kwargs: Any) -> ActionResult:
        return self._execute_sync(*args, **kwargs)

    def dispatch(self, *args: Any, **kwargs: Any) -> ActionResult:
        return self._execute_sync(*args, **kwargs)

    def dispatch_sync(self, *args: Any, **kwargs: Any) -> ActionResult:
        return self._execute_sync(*args, **kwargs)

    def perform(self, *args: Any, **kwargs: Any) -> ActionResult:
        return self._execute_sync(*args, **kwargs)

    def do(self, *args: Any, **kwargs: Any) -> ActionResult:
        return self._execute_sync(*args, **kwargs)

    def handle(self, *args: Any, **kwargs: Any) -> ActionResult:
        return self._execute_sync(*args, **kwargs)

    def process(self, *args: Any, **kwargs: Any) -> ActionResult:
        return self._execute_sync(*args, **kwargs)

    def execute_action(self, *args: Any, **kwargs: Any) -> ActionResult:
        return self._execute_sync(*args, **kwargs)

    def trigger_action(self, *args: Any, **kwargs: Any) -> ActionResult:
        return self._execute_sync(*args, **kwargs)

    def run_action(self, *args: Any, **kwargs: Any) -> ActionResult:
        return self._execute_sync(*args, **kwargs)

    execute_async = None  # placeholder replaced below
    aexecute = None
    atrigger = None
    arun = None

    async def _execute_async_impl(self, *args: Any, **kwargs: Any) -> ActionResult:
        req = self._normalize(args, kwargs)
        action_type = req["type"]
        target = req["target"]
        event_id = req["event_id"]
        payload = req["payload"]
        timeout = req.get("timeout") or self.timeout
        test = bool(req.get("test", False) or kwargs.get("test", False))

        if action_type in BLOCKED_ACTION_TYPES or (action_type == "command" and not self._command_actions_allowed()):
            res = self._blocked(action_type, target, event_id)
            self._remember(res)
            return res

        if action_type in ("webhook", "http", "http_post", "post"):
            res = await self._webhook_async(target, payload, event_id, timeout, test, req)
        elif action_type in ("file_append", "file", "append", "log"):
            res = self._file_append_sync(target, payload, event_id, test, req)
        elif action_type == "command":
            res = self._command_sync(target, payload, event_id, timeout, test, req)
        elif action_type == "email":
            res = self._email_sync(target, payload, event_id, timeout, test, req)
        elif action_type == "camera_control":
            res = self._camera_control_sync(target, payload, event_id, timeout, test, req)
        elif action_type == "sound":
            res = self._sound_sync(target, payload, event_id, timeout, test, req)
        else:
            # Default to webhook if a target URL looks like http.
            if isinstance(target, str) and target.lower().startswith(("http://", "https://")):
                res = await self._webhook_async(target, payload, event_id, timeout, test, req)
            else:
                res = ActionResult(
                    {
                        "ok": False,
                        "status": "error",
                        "detail": f"Unsupported action type: {action_type!r}",
                        "action": action_type,
                        "type": action_type,
                        "target": target,
                        "event_id": event_id,
                    }
                )
        self._remember(res)
        return res

    # Instance-bound async methods are wired after class creation below.

    # ------------------------------------------------------------------
    # Webhook (sync)
    # ------------------------------------------------------------------
    def execute_webhook(self, *args: Any, **kwargs: Any) -> ActionResult:
        req = self._normalize(args, kwargs)
        return self._webhook_sync(req["target"], req["payload"], req["event_id"], req.get("timeout") or self.timeout, bool(req.get("test")), req)

    def webhook(self, *args: Any, **kwargs: Any) -> ActionResult:
        return self.execute_webhook(*args, **kwargs)

    def http_post(self, *args: Any, **kwargs: Any) -> ActionResult:
        return self.execute_webhook(*args, **kwargs)

    def post(self, *args: Any, **kwargs: Any) -> ActionResult:
        return self.execute_webhook(*args, **kwargs)

    def _webhook_sync(self, target, payload, event_id, timeout, test, req) -> ActionResult:
        if target is None:
            return ActionResult({"ok": False, "status": "error", "detail": "target is required", "action": "webhook", "type": "webhook", "event_id": event_id, "target": target})
        target = str(target).strip()
        if not target:
            return ActionResult({"ok": False, "status": "error", "detail": "target is required", "action": "webhook", "type": "webhook", "event_id": event_id, "target": target})
        if not target.lower().startswith(("http://", "https://")):
            return ActionResult({"ok": False, "status": "error", "detail": "target must be an http(s) URL", "action": "webhook", "type": "webhook", "event_id": event_id, "target": target})

        out_payload = self._build_webhook_payload(payload, event_id, test, req)

        start = time.monotonic()
        status_code = None
        ok = False
        detail = ""
        response_text = None
        if httpx is None:
            return ActionResult({"ok": False, "status": "error", "detail": "httpx is not available", "action": "webhook", "type": "webhook", "event_id": event_id, "target": target})
        try:
            resp = httpx.post(target, json=out_payload, timeout=timeout, headers={"Content-Type": "application/json", "User-Agent": "VigilCam/0.1"})
            status_code = getattr(resp, "status_code", None)
            ok = status_code is not None and 200 <= int(status_code) < 300
            detail = f"HTTP {status_code}"
            try:
                response_text = resp.text[:500] if getattr(resp, "text", None) is not None else None
            except Exception:
                response_text = None
        except Exception as exc:  # noqa: BLE001
            detail = str(exc)
            ok = False
        latency_ms = (time.monotonic() - start) * 1000.0
        result = ActionResult(
            {
                "ok": bool(ok),
                "status": "success" if ok else "error",
                "detail": detail,
                "action": "webhook",
                "type": "webhook",
                "target": target,
                "event_id": event_id,
                "status_code": status_code,
                "latency_ms": round(latency_ms, 1),
                "response_text": response_text,
                "payload": out_payload,
            }
        )
        return result

    async def _webhook_async(self, target, payload, event_id, timeout, test, req) -> ActionResult:
        if target is None:
            return ActionResult({"ok": False, "status": "error", "detail": "target is required", "action": "webhook", "type": "webhook", "event_id": event_id, "target": target})
        target = str(target).strip()
        if not target or not target.lower().startswith(("http://", "https://")):
            return ActionResult({"ok": False, "status": "error", "detail": "target must be an http(s) URL", "action": "webhook", "type": "webhook", "event_id": event_id, "target": target})

        out_payload = self._build_webhook_payload(payload, event_id, test, req)
        start = time.monotonic()
        status_code = None
        ok = False
        detail = ""
        response_text = None
        if httpx is None:
            return ActionResult({"ok": False, "status": "error", "detail": "httpx is not available", "action": "webhook", "type": "webhook", "event_id": event_id, "target": target})
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(target, json=out_payload, headers={"Content-Type": "application/json", "User-Agent": "VigilCam/0.1"})
            status_code = getattr(resp, "status_code", None)
            ok = status_code is not None and 200 <= int(status_code) < 300
            detail = f"HTTP {status_code}"
            try:
                response_text = resp.text[:500]
            except Exception:
                response_text = None
        except Exception as exc:  # noqa: BLE001
            detail = str(exc)
            ok = False
        latency_ms = (time.monotonic() - start) * 1000.0
        return ActionResult(
            {
                "ok": bool(ok),
                "status": "success" if ok else "error",
                "detail": detail,
                "action": "webhook",
                "type": "webhook",
                "target": target,
                "event_id": event_id,
                "status_code": status_code,
                "latency_ms": round(latency_ms, 1),
                "response_text": response_text,
                "payload": out_payload,
            }
        )

    # ------------------------------------------------------------------
    # file_append (sync; trivially safe sync IO)
    # ------------------------------------------------------------------
    def execute_file_append(self, *args: Any, **kwargs: Any) -> ActionResult:
        req = self._normalize(args, kwargs)
        return self._file_append_sync(req["target"], req["payload"], req["event_id"], bool(req.get("test")), req)

    def file_append(self, *args: Any, **kwargs: Any) -> ActionResult:
        return self.execute_file_append(*args, **kwargs)

    def append_file(self, *args: Any, **kwargs: Any) -> ActionResult:
        return self.execute_file_append(*args, **kwargs)

    def append(self, *args: Any, **kwargs: Any) -> ActionResult:
        return self.execute_file_append(*args, **kwargs)

    def log(self, *args: Any, **kwargs: Any) -> ActionResult:
        return self.execute_file_append(*args, **kwargs)

    async def _file_append_async(self, *args, **kwargs):
        req = self._normalize(args, kwargs)
        return self._file_append_sync(req["target"], req["payload"], req["event_id"], bool(req.get("test")), req)

    def _file_append_sync(self, target, payload, event_id, test, req) -> ActionResult:
        if target is None:
            return ActionResult({"ok": False, "status": "error", "detail": "target is required", "action": "file_append", "type": "file_append", "event_id": event_id, "target": target})
        target = str(target).strip()
        if not target:
            return ActionResult({"ok": False, "status": "error", "detail": "target is required", "action": "file_append", "type": "file_append", "event_id": event_id, "target": target})
        path = Path(target)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            return ActionResult({"ok": False, "status": "error", "detail": f"cannot create parent: {exc}", "action": "file_append", "type": "file_append", "event_id": event_id, "target": target})
        try:
            text = self._payload_to_text(payload, event_id, test)
            if test and payload is None:
                text = text or "vigilcam action test\n"
            with path.open("a", encoding="utf-8") as fh:
                written = fh.write(text)
                if not text.endswith("\n"):
                    fh.write("\n")
                    written += 1
                fh.flush()
            size = path.stat().st_size if path.exists() else None
            return ActionResult(
                {
                    "ok": True,
                    "status": "success",
                    "detail": "appended",
                    "action": "file_append",
                    "type": "file_append",
                    "target": str(path),
                    "event_id": event_id,
                    "bytes_written": written,
                    "file_size": size,
                    "payload": payload,
                }
            )
        except Exception as exc:  # noqa: BLE001
            return ActionResult(
                {
                    "ok": False,
                    "status": "error",
                    "detail": str(exc),
                    "action": "file_append",
                    "type": "file_append",
                    "target": target,
                    "event_id": event_id,
                }
            )


    # ------------------------------------------------------------------
    # command
    # ------------------------------------------------------------------
    def execute_command(self, *args: Any, **kwargs: Any) -> ActionResult:
        req = self._normalize(args, kwargs)
        return self._command_sync(req["target"], req["payload"], req["event_id"], req.get("timeout") or self.timeout, bool(req.get("test")), req)

    def command(self, *args: Any, **kwargs: Any) -> ActionResult:
        return self.execute_command(*args, **kwargs)

    def _command_sync(self, target, payload, event_id, timeout, test, req) -> ActionResult:
        if not self._command_actions_allowed():
            return self._blocked("command", target, event_id)
        # Determine command string.
        cmd = None
        if isinstance(target, str) and target.strip():
            cmd = target.strip()
        if cmd is None and isinstance(payload, dict):
            for k in ("command", "cmd", "script", "line", "value", "message", "text"):
                if payload.get(k) is not None:
                    cmd = str(payload.get(k))
                    break
        if not cmd:
            # maybe payload itself
            if isinstance(payload, str):
                cmd = payload
        if not cmd:
            return ActionResult({"ok": False, "status": "error", "detail": "command is required", "action": "command", "type": "command", "event_id": event_id, "target": target})
        import subprocess, shlex
        start = time.monotonic()
        stdout = ""
        stderr = ""
        rc = None
        try:
            proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=float(timeout or self.timeout))
            rc = proc.returncode
            stdout = (proc.stdout or "")[:4000]
            stderr = (proc.stderr or "")[:4000]
            ok = rc == 0
            detail = f"exit {rc}"
        except subprocess.TimeoutExpired as exc:
            ok = False
            detail = f"timeout after {timeout}s"
            try:
                stdout = (exc.stdout or "")[:4000] if isinstance(exc.stdout, str) else ""
                stderr = (exc.stderr or "")[:4000] if isinstance(exc.stderr, str) else ""
            except Exception:
                pass
        except Exception as exc:  # noqa: BLE001
            ok = False
            detail = str(exc)
        latency_ms = (time.monotonic() - start) * 1000.0
        return ActionResult({
            "ok": bool(ok),
            "status": "success" if ok else "error",
            "detail": detail,
            "action": "command",
            "type": "command",
            "target": target,
            "command": cmd,
            "event_id": event_id,
            "returncode": rc,
            "stdout": stdout,
            "stderr": stderr,
            "latency_ms": round(latency_ms, 1),
        })

    # ------------------------------------------------------------------
    # email
    # ------------------------------------------------------------------
    def execute_email(self, *args: Any, **kwargs: Any) -> ActionResult:
        req = self._normalize(args, kwargs)
        return self._email_sync(req["target"], req["payload"], req["event_id"], req.get("timeout") or self.timeout, bool(req.get("test")), req)

    def email(self, *args: Any, **kwargs: Any) -> ActionResult:
        return self.execute_email(*args, **kwargs)

    def _email_sync(self, target, payload, event_id, timeout, test, req) -> ActionResult:
        import smtplib
        from email.message import EmailMessage

        payload = payload if isinstance(payload, dict) else ({"payload": payload} if payload is not None else {})
        # recipient
        to_addr = None
        if isinstance(target, str) and "@" in target:
            to_addr = target.strip()
        for k in ("to", "recipient", "email", "address", "target_email"):
            if to_addr is None and payload.get(k) is not None:
                v = payload.get(k)
                if isinstance(v, str) and v.strip():
                    to_addr = v.strip()
                    break
        if to_addr is None:
            # maybe target is recipient even without @? still try
            if isinstance(target, str) and target.strip():
                to_addr = target.strip()
        if not to_addr:
            return ActionResult({"ok": False, "status": "error", "detail": "recipient is required", "action": "email", "type": "email", "event_id": event_id, "target": target})

        subject = payload.get("subject") or payload.get("title") or ("VigilCam alert" if not test else "VigilCam test")
        body = None
        for k in ("body", "message", "text", "content", "html"):
            if payload.get(k) is not None:
                body = payload.get(k)
                break
        if body is None:
            body = payload.get("payload")
        if body is None:
            body = ""
        body = str(body)

        # SMTP settings
        cfg = None
        try:
            from app.core.config import settings as _s
            cfg = _s
        except Exception:  # noqa: BLE001
            cfg = None
        host = payload.get("smtp_host") or payload.get("host") or payload.get("server") or getattr(cfg, "SMTP_HOST", None) or "localhost"
        port = payload.get("smtp_port") or payload.get("port") or getattr(cfg, "SMTP_PORT", 587)
        try:
            port = int(port)
        except (TypeError, ValueError):
            port = 587
        username = payload.get("username") or payload.get("user") or getattr(cfg, "SMTP_USERNAME", "") or ""
        password = payload.get("password") or payload.get("pass") or getattr(cfg, "SMTP_PASSWORD", "") or ""
        use_tls = payload.get("use_tls")
        if use_tls is None:
            use_tls = payload.get("tls")
        if use_tls is None:
            use_tls = getattr(cfg, "SMTP_USE_TLS", True)
        use_tls = bool(use_tls)
        from_addr = payload.get("from") or payload.get("from_addr") or payload.get("sender") or getattr(cfg, "SMTP_FROM", None) or (username or "vigilcam@localhost")

        msg = EmailMessage()
        msg["Subject"] = str(subject)
        msg["From"] = str(from_addr)
        msg["To"] = to_addr
        msg.set_content(body)

        start = time.monotonic()
        ok = False
        detail = ""
        try:
            if port == 465:
                server = smtplib.SMTP_SSL(host, port, timeout=float(timeout or self.timeout))
            else:
                server = smtplib.SMTP(host, port, timeout=float(timeout or self.timeout))
            try:
                server.ehlo()
                if use_tls and port != 465:
                    server.starttls()
                    server.ehlo()
                if username:
                    server.login(username, password)
                server.send_message(msg)
                ok = True
                detail = "sent"
            finally:
                try:
                    server.quit()
                except Exception:
                    pass
        except Exception as exc:  # noqa: BLE001
            detail = str(exc)
            ok = False
        latency_ms = (time.monotonic() - start) * 1000.0
        return ActionResult({
            "ok": bool(ok),
            "status": "success" if ok else "error",
            "detail": detail,
            "action": "email",
            "type": "email",
            "target": target,
            "to": to_addr,
            "subject": subject,
            "event_id": event_id,
            "latency_ms": round(latency_ms, 1),
        })

    # ------------------------------------------------------------------
    # camera_control
    # ------------------------------------------------------------------
    def execute_camera_control(self, *args: Any, **kwargs: Any) -> ActionResult:
        req = self._normalize(args, kwargs)
        return self._camera_control_sync(req["target"], req["payload"], req["event_id"], req.get("timeout") or self.timeout, bool(req.get("test")), req)

    def camera_control(self, *args: Any, **kwargs: Any) -> ActionResult:
        return self.execute_camera_control(*args, **kwargs)

    def _camera_control_sync(self, target, payload, event_id, timeout, test, req) -> ActionResult:
        payload = payload if isinstance(payload, dict) else ({"payload": payload} if payload is not None else {})
        # operation
        op = None
        for k in ("operation", "command", "action", "mode", "op"):
            if op is None and payload.get(k) is not None:
                op = str(payload.get(k)).strip().lower()
        if op is None and isinstance(target, str):
            low = target.strip().lower()
            if low in ("start", "stop", "start_recording", "stop_recording"):
                op = low
        if op is None:
            op = "start"
        if op in ("start_recording", "record_start", "start_record"):
            op = "start"
        if op in ("stop_recording", "record_stop", "stop_record"):
            op = "stop"

        # camera name/id
        camera_name = None
        for k in ("camera_name", "camera", "name", "cam"):
            if camera_name is None and payload.get(k) is not None:
                camera_name = payload.get(k)
        if camera_name is None and target is not None and not (isinstance(target, str) and target.strip().lower() in ("start","stop")):
            camera_name = target
        if camera_name is None:
            camera_name = payload.get("camera_id")
        camera_name = str(camera_name) if camera_name is not None else "default"

        main_url = payload.get("main_url") or payload.get("url") or payload.get("source") or payload.get("stream_url")
        sub_url = payload.get("sub_url")

        try:
            from app.services.recording_manager import recording_manager as _rm
        except Exception as exc:  # noqa: BLE001
            return ActionResult({"ok": False, "status": "error", "detail": f"recording manager unavailable: {exc}", "action": "camera_control", "type": "camera_control", "event_id": event_id, "target": target})

        try:
            if op == "start":
                if not main_url:
                    # try resolve from db? best effort none
                    main_url = payload.get("main_url") or ""
                _rm.start_recording(camera_name, str(main_url) if main_url else "", sub_url=sub_url)
                detail = "recording started"
            elif op == "stop":
                _rm.stop_recording(camera_name)
                detail = "recording stopped"
            else:
                return ActionResult({"ok": False, "status": "error", "detail": f"unknown operation: {op}", "action": "camera_control", "type": "camera_control", "event_id": event_id, "target": target})
            ok = True
        except Exception as exc:  # noqa: BLE001
            ok = False
            detail = str(exc)

        return ActionResult({
            "ok": bool(ok),
            "status": "success" if ok else "error",
            "detail": detail,
            "action": "camera_control",
            "type": "camera_control",
            "target": target,
            "camera": camera_name,
            "camera_name": camera_name,
            "operation": op,
            "event_id": event_id,
        })

    # ------------------------------------------------------------------
    # sound
    # ------------------------------------------------------------------
    def execute_sound(self, *args: Any, **kwargs: Any) -> ActionResult:
        req = self._normalize(args, kwargs)
        return self._sound_sync(req["target"], req["payload"], req["event_id"], req.get("timeout") or self.timeout, bool(req.get("test")), req)

    def sound(self, *args: Any, **kwargs: Any) -> ActionResult:
        return self.execute_sound(*args, **kwargs)

    def _sound_sync(self, target, payload, event_id, timeout, test, req) -> ActionResult:
        # no-op marker
        return ActionResult({
            "ok": True,
            "status": "success",
            "detail": "sound action (no-op)",
            "action": "sound",
            "type": "sound",
            "target": target,
            "event_id": event_id,
            "no_op": True,
            "noop": True,
            "marker": "sound",
        })

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _normalize(self, args, kwargs) -> Dict[str, Any]:
        req: Dict[str, Any] = {}
        if args:
            if len(args) == 1 and isinstance(args[0], dict):
                req.update(args[0])
            elif len(args) == 1 and isinstance(args[0], str):
                # Heuristic: a lone string is a target unless kwargs clarify.
                if any(k in kwargs for k in TYPE_FIELDS) or any(k in kwargs for k in TARGET_FIELDS):
                    req.update(kwargs)
                    if "target" not in req and "url" not in req:
                        pass
                else:
                    # Could be action type.
                    if args[0].lower() in ALLOWED_ACTION_TYPES or args[0].lower() in BLOCKED_ACTION_TYPES:
                        req["type"] = args[0]
                    else:
                        req["target"] = args[0]
            else:
                # Positional patterns:
                # (event_id, type, target, payload) OR (type, target, payload) OR (event_id, target, payload?)
                vals = list(args)
                # If first is int and second is str action type.
                if len(vals) >= 2 and isinstance(vals[0], int) and isinstance(vals[1], str) and vals[1].lower() in (ALLOWED_ACTION_TYPES | BLOCKED_ACTION_TYPES):
                    req["event_id"] = vals[0]
                    req["type"] = vals[1]
                    if len(vals) >= 3:
                        req["target"] = vals[2]
                    if len(vals) >= 4:
                        req["payload"] = vals[3]
                elif len(vals) >= 3 and isinstance(vals[0], str) and vals[0].lower() in (ALLOWED_ACTION_TYPES | BLOCKED_ACTION_TYPES) and isinstance(vals[1], str):
                    req["type"] = vals[0]
                    req["target"] = vals[1]
                    req["payload"] = vals[2] if len(vals) > 2 else None
                    if len(vals) > 3:
                        req["event_id"] = vals[3]
                elif len(vals) >= 2 and isinstance(vals[0], int) and isinstance(vals[1], str):
                    # (event_id, target) maybe
                    req["event_id"] = vals[0]
                    if vals[1].lower().startswith(("http://", "https://")) or ("/" in vals[1]):
                        req["target"] = vals[1]
                    else:
                        req["target"] = vals[1]
                    if len(vals) >= 3:
                        req["payload"] = vals[2]
                else:
                    # Fallback: assign sequentially.
                    for i, v in enumerate(vals):
                        if i == 0:
                            if isinstance(v, int):
                                req["event_id"] = v
                            elif isinstance(v, str) and v.lower() in (ALLOWED_ACTION_TYPES | BLOCKED_ACTION_TYPES):
                                req["type"] = v
                            else:
                                req["target"] = v
                        elif i == 1:
                            if isinstance(v, str) and v.lower() in (ALLOWED_ACTION_TYPES | BLOCKED_ACTION_TYPES) and "type" not in req:
                                req["type"] = v
                            elif "target" not in req:
                                req["target"] = v
                            else:
                                req["payload"] = v
                        elif i == 2:
                            if "target" not in req:
                                req["target"] = v
                            else:
                                req["payload"] = v
                        else:
                            req["payload"] = v
        req.update(kwargs)

        # Resolve event id from top-level request BEFORE flattening, so
        # fields nested inside the payload (e.g. an "event" message) are not
        # mistaken for the event id.
        event_id = None
        for f in EVENT_ID_FIELDS:
            if f in req and req.get(f) is not None:
                try:
                    event_id = int(req.get(f)) if not isinstance(req.get(f), bool) else req.get(f)
                except (TypeError, ValueError):
                    event_id = req.get(f)
                break

        # Flatten simple nested inputs.
        flat = _flatten_payload(req)
        # Keep original payload if nested flattening clobbered; restore
        if "payload" in req:
            flat["payload"] = req["payload"]
        if "data" in req:
            flat.setdefault("data", req["data"])
        if "body" in req:
            flat.setdefault("body", req["body"])

        # Resolve type.
        action_type = None
        for f in TYPE_FIELDS:
            if f in flat and isinstance(flat.get(f), str) and flat.get(f).strip():
                action_type = flat.get(f).strip()
                break
        if action_type is None:
            # Maybe in extras
            for k, v in flat.items():
                if k.lower() in TYPE_FIELDS and isinstance(v, str):
                    action_type = v.strip()
                    break
        if action_type is None:
            action_type = kwargs.get("default_action", self.default_action)
        if action_type is None:
            # default based on target
            target_candidate = None
            for f in TARGET_FIELDS:
                if f in flat and flat.get(f) is not None:
                    target_candidate = flat.get(f)
                    break
            if isinstance(target_candidate, str) and target_candidate.lower().startswith(("http://", "https://")):
                action_type = "webhook"
            elif "path" in flat or "file" in flat or "file_path" in flat:
                action_type = "file_append"
        if action_type is None:
            action_type = "webhook"
        action_type = str(action_type).strip().lower().replace("-", "_").replace(" ", "_")

        # Alias normalization.
        alias_map = {
            "http": "webhook",
            "http_post": "webhook",
            "post": "webhook",
            "url": "webhook",
            "webhook_post": "webhook",
            "send_webhook": "webhook",
            "file": "file_append",
            "append": "file_append",
            "append_file": "file_append",
            "fileappend": "file_append",
            "log": "file_append",
            "write": "file_append",
            "write_file": "file_append",
            "cmd": "command",
            "shell": "command",
            "exec": "command",
            "execute": "command",
            "run_command": "command",
            "email": "email",
            "mail": "email",
            "send_email": "email",
            "smtp": "email",
            "camera_control": "camera_control",
            "camera": "camera_control",
            "recording_control": "camera_control",
            "record_control": "camera_control",
            "start_recording": "camera_control",
            "stop_recording": "camera_control",
            "sound": "sound",
            "play_sound": "sound",
            "beep": "sound",
            "audio": "sound",
        }
        action_type = alias_map.get(action_type, action_type)

        # Resolve target.
        target = None
        for f in TARGET_FIELDS:
            if f in flat and flat.get(f) is not None:
                target = flat.get(f)
                break
        if target is None:
            # nested under payload
            nested = flat.get("payload") if isinstance(flat.get("payload"), dict) else {}
            for f in TARGET_FIELDS:
                if f in nested and nested.get(f) is not None:
                    target = nested.get(f)
                    break

        # Resolve payload: use explicit payload, else build from remaining fields.
        payload = None
        for f in PAYLOAD_FIELDS:
            if f in flat and f not in ("message",) and flat.get(f) is not None and f in ("payload", "data", "body", "params", "content"):
                payload = flat.get(f)
                break
        # If payload still None but explicit message/content, use those.
        if payload is None:
            for f in ("message", "content", "text", "line", "entry", "log", "value", "record"):
                if f in flat and flat.get(f) is not None:
                    payload = {f: flat.get(f)}
                    break
        # If there are leftover fields, include them.
        if isinstance(payload, dict):
            # merge flat extras (excluding control fields) into payload
            control = set(EVENT_ID_FIELDS) | set(TYPE_FIELDS) | set(TARGET_FIELDS) | set(PAYLOAD_FIELDS) | {"timeout", "test", "force", "db", "session"}
            for k, v in flat.items():
                if k in payload:
                    continue
                if k in control:
                    continue
                if k.lower() in {x.lower() for x in control}:
                    continue
                payload.setdefault(k, v)
        if payload is None:
            payload = {}

        timeout = None
        if "timeout" in flat and isinstance(flat.get("timeout"), (int, float)):
            timeout = float(flat.get("timeout"))
        test = bool(flat.get("test") or flat.get("force") or kwargs.get("test") or kwargs.get("force"))

        return {
            "event_id": event_id,
            "type": action_type,
            "target": target,
            "payload": payload,
            "timeout": timeout or self.timeout,
            "test": test,
            "raw": flat,
        }

    def _build_webhook_payload(self, payload, event_id, test, req) -> Dict[str, Any]:
        if isinstance(payload, dict):
            out = dict(payload)
        elif payload is None:
            out = {}
        else:
            out = {"payload": payload}
        if event_id is not None and "event_id" not in out:
            out["event_id"] = event_id
        if test and "test" not in out:
            out["test"] = True
        if "action" not in out:
            out["action"] = "webhook"
        if "type" not in out:
            out["type"] = "webhook"
        return out

    def _blocked(self, action_type, target, event_id) -> ActionResult:
        return ActionResult(
            {
                "ok": False,
                "status": "blocked",
                "detail": "CLI command execution is disabled for security",
                "action": action_type,
                "type": action_type,
                "target": target,
                "event_id": event_id,
                "blocked": True,
            }
        )

    def _payload_to_text(self, payload, event_id, test) -> str:
        if payload is None:
            if test:
                return "vigilcam action test\n"
            return ""
        if isinstance(payload, str):
            text = payload
        elif isinstance(payload, dict):
            # Prefer explicit text-like fields.
            for key in ("message", "text", "content", "line", "entry", "log", "body", "value"):
                if key in payload and payload.get(key) is not None:
                    val = payload.get(key)
                    if isinstance(val, str):
                        text = val
                    else:
                        text = _to_str(val)
                    # If other fields present, maybe append full json? We'll include all as json.
                    return text if text.endswith("\n") else text + "\n"
            # No text field; dump entire payload.
            try:
                text = json.dumps(payload, default=_json_default, ensure_ascii=False)
            except Exception:
                text = str(payload)
        elif isinstance(payload, (list, tuple)):
            text = json.dumps(list(payload), default=_json_default, ensure_ascii=False)
        elif isinstance(payload, (int, float, bool)):
            text = str(payload)
        else:
            text = _to_str(payload)
        if not isinstance(text, str):
            text = str(text)
        if not text.endswith("\n"):
            text = text + "\n"
        return text

    def _remember(self, result: ActionResult) -> None:
        self.results.append(result)
        # Bound memory.
        if len(self.results) > 100:
            self.results = self.results[-100:]

    # Sync dispatch central
    def _execute_sync(self, *args, **kwargs) -> ActionResult:
        req = self._normalize(args, kwargs)
        action_type = req["type"]
        target = req["target"]
        event_id = req["event_id"]
        payload = req["payload"]
        timeout = req["timeout"]
        test = req["test"]

        if action_type in BLOCKED_ACTION_TYPES or (action_type == "command" and not self._command_actions_allowed()):
            res = self._blocked(action_type, target, event_id)
            self._remember(res)
            return res

        if action_type in ("webhook", "http", "http_post", "post"):
            res = self._webhook_sync(target, payload, event_id, timeout, test, req)
        elif action_type in ("file_append", "file", "append", "log"):
            res = self._file_append_sync(target, payload, event_id, test, req)
            res["type"] = action_type if action_type != "webhook" else res.get("type")
        elif action_type == "command":
            res = self._command_sync(target, payload, event_id, timeout, test, req)
        elif action_type == "email":
            res = self._email_sync(target, payload, event_id, timeout, test, req)
        elif action_type == "camera_control":
            res = self._camera_control_sync(target, payload, event_id, timeout, test, req)
        elif action_type == "sound":
            res = self._sound_sync(target, payload, event_id, timeout, test, req)
        else:
            # Default fallback to webhook if target is URL.
            if isinstance(target, str) and target.lower().startswith(("http://", "https://")):
                res = self._webhook_sync(target, payload, event_id, timeout, test, req)
            else:
                res = ActionResult(
                    {
                        "ok": False,
                        "status": "error",
                        "detail": f"Unsupported action type: {action_type!r}",
                        "action": action_type,
                        "type": action_type,
                        "target": target,
                        "event_id": event_id,
                        "allowed": sorted(ALLOWED_ACTION_TYPES),
                    }
                )
        # normalize type field for blocked/supported
        try:
            res.setdefault("type", action_type)
        except Exception:
            pass
        self._remember(res)
        return res

    # Async dispatch (runtime-wired)
    async def execute_async(self, *args, **kwargs) -> ActionResult:
        return await self._execute_async_impl(*args, **kwargs)

    async def trigger_async(self, *args, **kwargs) -> ActionResult:
        return await self.execute_async(*args, **kwargs)

    async def run_async(self, *args, **kwargs) -> ActionResult:
        return await self.execute_async(*args, **kwargs)

    async def dispatch_async(self, *args, **kwargs) -> ActionResult:
        return await self.execute_async(*args, **kwargs)

    async def perform_async(self, *args, **kwargs) -> ActionResult:
        return await self.execute_async(*args, **kwargs)


# Wire async placeholders.
ActionExecutor.execute_async = ActionExecutor.execute_async
ActionExecutor.aexecute = ActionExecutor.execute_async
ActionExecutor.async_execute = ActionExecutor.execute_async
ActionExecutor._execute_async = ActionExecutor.execute_async
ActionExecutor.atrigger = ActionExecutor.trigger_async
ActionExecutor.arun = ActionExecutor.run_async
ActionExecutor.adispatch = ActionExecutor.dispatch_async
ActionExecutor.aperform = ActionExecutor.perform_async

# Backwards/alternative names.
ActionExecutor.trigger_async = ActionExecutor.trigger_async
ActionExecutor.run_async = ActionExecutor.run_async
ActionExecutor.dispatch_async = ActionExecutor.dispatch_async

# Alias classes.
ActionsExecutor = ActionExecutor
ActionRunner = ActionExecutor
ActionDispatcher = ActionExecutor
AlertActionExecutor = ActionExecutor
ActionManager = ActionExecutor
ActionHandler = ActionExecutor
Executor = ActionExecutor

# Singletons.
action_executor = ActionExecutor()
actions_executor = action_executor
executor = action_executor
action_manager = action_executor
alert_action_executor = action_executor
default_action_executor = action_executor
action_executor_instance = action_executor

# Module-level convenience functions.
def execute(*args: Any, **kwargs: Any) -> ActionResult:
    return action_executor.execute(*args, **kwargs)

def execute_sync(*args: Any, **kwargs: Any) -> ActionResult:
    return action_executor.execute(*args, **kwargs)

def trigger(*args: Any, **kwargs: Any) -> ActionResult:
    return action_executor.execute(*args, **kwargs)

def run(*args: Any, **kwargs: Any) -> ActionResult:
    return action_executor.execute(*args, **kwargs)

def dispatch(*args: Any, **kwargs: Any) -> ActionResult:
    return action_executor.execute(*args, **kwargs)

def perform(*args: Any, **kwargs: Any) -> ActionResult:
    return action_executor.execute(*args, **kwargs)

def do(*args: Any, **kwargs: Any) -> ActionResult:
    return action_executor.execute(*args, **kwargs)

def handle(*args: Any, **kwargs: Any) -> ActionResult:
    return action_executor.execute(*args, **kwargs)

def process(*args: Any, **kwargs: Any) -> ActionResult:
    return action_executor.execute(*args, **kwargs)

def execute_action(*args: Any, **kwargs: Any) -> ActionResult:
    return action_executor.execute(*args, **kwargs)

def trigger_action(*args: Any, **kwargs: Any) -> ActionResult:
    return action_executor.execute(*args, **kwargs)

def run_action(*args: Any, **kwargs: Any) -> ActionResult:
    return action_executor.execute(*args, **kwargs)

def dispatch_action(*args: Any, **kwargs: Any) -> ActionResult:
    return action_executor.execute(*args, **kwargs)

def perform_action(*args: Any, **kwargs: Any) -> ActionResult:
    return action_executor.execute(*args, **kwargs)

def execute_webhook(*args: Any, **kwargs: Any) -> ActionResult:
    return action_executor.execute(*args, **kwargs)

def trigger_webhook(*args: Any, **kwargs: Any) -> ActionResult:
    return action_executor.execute(*args, **kwargs)

def test_webhook(*args: Any, **kwargs: Any) -> ActionResult:
    kw = dict(kwargs)
    kw.setdefault("test", True)
    kw.setdefault("type", "webhook")
    return action_executor.execute(*args, **kw)

def execute_file_append(*args: Any, **kwargs: Any) -> ActionResult:
    return action_executor.execute(*args, **kwargs)

def append_file(*args: Any, **kwargs: Any) -> ActionResult:
    return action_executor.execute(*args, **kwargs)


async def execute_async(*args: Any, **kwargs: Any) -> ActionResult:
    return await action_executor.execute_async(*args, **kwargs)

async def aexecute(*args: Any, **kwargs: Any) -> ActionResult:
    return await action_executor.execute_async(*args, **kwargs)

async def atrigger(*args: Any, **kwargs: Any) -> ActionResult:
    return await action_executor.trigger_async(*args, **kwargs)

async def arun(*args: Any, **kwargs: Any) -> ActionResult:
    return await action_executor.run_async(*args, **kwargs)

async def trigger_async(*args: Any, **kwargs: Any) -> ActionResult:
    return await action_executor.execute_async(*args, **kwargs)

async def run_async(*args: Any, **kwargs: Any) -> ActionResult:
    return await action_executor.execute_async(*args, **kwargs)

async def dispatch_async(*args: Any, **kwargs: Any) -> ActionResult:
    return await action_executor.execute_async(*args, **kwargs)


def get_action_executor() -> ActionExecutor:
    return action_executor

def get_executor() -> ActionExecutor:
    return action_executor

def get_actions_executor() -> ActionExecutor:
    return action_executor


__all__ = [
    "ActionExecutor",
    "ActionsExecutor",
    "ActionRunner",
    "ActionDispatcher",
    "AlertActionExecutor",
    "ActionManager",
    "ActionHandler",
    "Executor",
    "ActionResult",
    "ActionResultAlias",
    "ActionResponse",
    "ActionOutcome",
    "ActionReceipt",
    "action_executor",
    "actions_executor",
    "executor",
    "action_manager",
    "alert_action_executor",
    "default_action_executor",
    "action_executor_instance",
    "execute",
    "execute_sync",
    "trigger",
    "run",
    "dispatch",
    "perform",
    "do",
    "handle",
    "process",
    "execute_action",
    "trigger_action",
    "run_action",
    "dispatch_action",
    "perform_action",
    "execute_webhook",
    "trigger_webhook",
    "test_webhook",
    "execute_file_append",
    "append_file",
    "execute_async",
    "trigger_async",
    "run_async",
    "dispatch_async",
    "get_action_executor",
    "get_executor",
    "get_actions_executor",
]
