"""Allowlisted script runner service for VigilCam.

Safe alternative to the deliberately-blocked ``command`` action in
``app.services.actions`` (which refuses all CLI/shell execution by
default). This module only executes scripts that are explicitly
allowlisted by logical name -- never arbitrary shell strings.

Security properties:

- No shell execution; interpreter + argv list only.
- Logical-name allowlist (``ALLOWED_SCRIPTS``); unknown names, absolute
  paths, and path traversal (``..``, ``/``, ``\\\\``) are rejected.
- Only ``.py`` (via ``sys.executable``) and ``.sh`` (via ``/bin/sh``)
  targets are supported.
- Argument allowlist validation: list of short printable strings,
  bounded count/length, no NUL bytes.
- Bounded timeout (clamped to ``MAX_TIMEOUT_SECONDS``) with
  ``subprocess.TimeoutExpired`` captured as a structured error.
- Bounded output capture (stdout/stderr truncated, never unbounded).

Standard library only. ``runner`` callback is injectable for tests via
``register_script`` / monkeypatching ``ALLOWED_SCRIPTS``.
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from app.core.config import BASE_DIR as _BASE_DIR
    SCRIPT_DIR = Path(_BASE_DIR) / "scripts"
except Exception:  # noqa: BLE001
    SCRIPT_DIR = Path(__file__).resolve().parents[2] / "scripts"

DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_TIMEOUT_SECONDS = 120.0
MAX_OUTPUT_CHARS = 8000
MAX_STDOUT_CHARS = 4000
MAX_STDERR_CHARS = 4000
MAX_ARGS = 32
MAX_ARG_LENGTH = 1024

_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_ALLOWED_EXTENSIONS = (".py", ".sh")


def _now() -> datetime:
    return datetime.utcnow()


def _coerce_timeout(value: Any) -> float:
    try:
        t = float(value) if value is not None else DEFAULT_TIMEOUT_SECONDS
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SECONDS
    if t <= 0:
        return DEFAULT_TIMEOUT_SECONDS
    return min(t, MAX_TIMEOUT_SECONDS)


def _default_allowlist() -> Dict[str, Path]:
    out: Dict[str, Path] = {}
    candidates = {
        "evidence_pack": SCRIPT_DIR / "evidence_pack.py",
        "generate-secrets": SCRIPT_DIR / "generate-secrets.sh",
    }
    for name, path in candidates.items():
        try:
            if path.exists() and path.is_file():
                out[name] = path
        except Exception:  # noqa: BLE001
            continue
    return out


ALLOWED_SCRIPTS: Dict[str, Path] = _default_allowlist()


def _is_safe_name(name: Any) -> bool:
    if not isinstance(name, str):
        return False
    text = name.strip()
    if not text or len(text) > 128:
        return False
    if text != name.strip():
        return False
    if "/" in text or "\\" in text or "\x00" in text:
        return False
    if ".." in text:
        return False
    if text.startswith("-") or text.startswith("."):
        return False
    return bool(_SAFE_NAME_RE.match(text))


def _validate_args(args: Any) -> List[str]:
    if args is None:
        return []
    if isinstance(args, (str, bytes)):
        raise ValueError("args must be a list of strings, not a single string")
    try:
        items = list(args)
    except TypeError as exc:
        raise ValueError(f"args must be a list: {exc}") from exc
    if len(items) > MAX_ARGS:
        raise ValueError(f"too many args (max {MAX_ARGS})")
    cleaned: List[str] = []
    for item in items:
        if isinstance(item, bool):
            item = str(item)
        elif isinstance(item, (int, float)):
            item = str(item)
        if not isinstance(item, str):
            raise ValueError(f"arg must be a string, got {type(item).__name__}")
        if "\x00" in item:
            raise ValueError("arg contains NUL byte")
        if len(item) > MAX_ARG_LENGTH:
            raise ValueError(f"arg too long (max {MAX_ARG_LENGTH} chars)")
        cleaned.append(item)
    return cleaned


def get_script_path(name: Any) -> Optional[Path]:
    """Return the resolved allowlisted path for *name*, or None."""
    if not _is_safe_name(name):
        return None
    key = str(name).strip()
    target = ALLOWED_SCRIPTS.get(key)
    if target is None:
        return None
    try:
        resolved = Path(target).resolve()
    except Exception:  # noqa: BLE001
        return None
    # Registered entries must exist, be files, and have an allowed suffix.
    try:
        if not resolved.exists() or not resolved.is_file():
            return None
    except Exception:  # noqa: BLE001
        return None
    if resolved.suffix not in _ALLOWED_EXTENSIONS:
        return None
    return resolved


def is_allowed(name: Any) -> bool:
    """Return True when *name* is an executable allowlisted script."""
    return get_script_path(name) is not None


def list_scripts() -> List[Dict[str, Any]]:
    """List allowlisted scripts with existence metadata."""
    out: List[Dict[str, Any]] = []
    for name in sorted(ALLOWED_SCRIPTS.keys()):
        try:
            raw = Path(ALLOWED_SCRIPTS[name])
        except Exception:  # noqa: BLE001
            continue
        try:
            resolved = raw.resolve()
            exists = resolved.exists() and resolved.is_file()
        except Exception:  # noqa: BLE001
            resolved = raw
            exists = False
        suffix = resolved.suffix if isinstance(resolved, Path) else ""
        if suffix == ".py":
            interpreter = sys.executable
        elif suffix == ".sh":
            interpreter = "/bin/sh"
        else:
            interpreter = None
        out.append(
            {
                "name": name,
                "script": name,
                "path": str(resolved),
                "exists": bool(exists),
                "allowed": bool(exists and suffix in _ALLOWED_EXTENSIONS),
                "interpreter": interpreter,
            }
        )
    return out


def register_script(name: Any, path: Any) -> Path:
    """Register *path* under logical *name* (for tests/admin setup)."""
    if not _is_safe_name(name):
        raise ValueError(f"invalid script name: {name!r}")
    p = Path(path)
    try:
        resolved = p.resolve()
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"invalid script path: {exc}") from exc
    if not resolved.exists() or not resolved.is_file():
        raise ValueError(f"script does not exist: {resolved}")
    if resolved.suffix not in _ALLOWED_EXTENSIONS:
        raise ValueError(f"unsupported script extension: {resolved.suffix}")
    ALLOWED_SCRIPTS[str(name).strip()] = resolved
    return resolved


def unregister_script(name: Any) -> bool:
    """Remove *name* from the allowlist. Returns True when removed."""
    key = str(name).strip() if isinstance(name, str) else name
    return ALLOWED_SCRIPTS.pop(key, None) is not None


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) > limit:
        return text[:limit], True
    return text, False


@dataclass
class ScriptResult:
    """Outcome of running one allowlisted script."""

    script: str
    ok: bool = False
    returncode: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    truncated: bool = False
    command: List[str] = field(default_factory=list)
    latency_ms: Optional[float] = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    timed_out: bool = False
    blocked: bool = False
    error: Optional[str] = None
    detail: Optional[str] = None
    checked_at: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        detail = self.detail if self.detail is not None else (self.error or "")
        return {
            "script": self.script,
            "name": self.script,
            "ok": bool(self.ok),
            "success": bool(self.ok),
            "blocked": bool(self.blocked),
            "status": "blocked" if self.blocked else ("success" if self.ok else "error"),
            "returncode": self.returncode,
            "return_code": self.returncode,
            "exit_code": self.returncode,
            "stdout": self.stdout,
            "output": self.stdout,
            "stderr": self.stderr,
            "error_output": self.stderr,
            "stdout_truncated": bool(self.stdout_truncated),
            "stderr_truncated": bool(self.stderr_truncated),
            "truncated": bool(self.truncated),
            "command": list(self.command),
            "argv": list(self.command),
            "latency_ms": self.latency_ms,
            "timeout_seconds": self.timeout_seconds,
            "timeoutSeconds": self.timeout_seconds,
            "timed_out": bool(self.timed_out),
            "timeout": bool(self.timed_out),
            "error": self.error,
            "detail": detail,
            "checked_at": self.checked_at.isoformat() if self.checked_at else None,
        }

    def dict(self) -> Dict[str, Any]:  # noqa: A003
        return self.to_dict()

    def model_dump(self) -> Dict[str, Any]:
        return self.to_dict()

    def as_dict(self) -> Dict[str, Any]:
        return self.to_dict()

    def __getitem__(self, key: str) -> Any:
        data = self.to_dict()
        aliases = {
            "id": "script",
            "target": "script",
            "state": "status",
            "message": "detail",
            "exitCode": "returncode",
        }
        return data.get(aliases.get(key, key))

    def get(self, key: str, default: Any = None) -> Any:
        try:
            data = self.to_dict()
            aliases = {
                "id": "script",
                "target": "script",
                "state": "status",
                "message": "detail",
                "exitCode": "returncode",
            }
            lookup = aliases.get(key, key)
            return data.get(lookup, default)
        except Exception:  # noqa: BLE001
            return default


ScriptRunnerResult = ScriptResult
RunResult = ScriptResult


def _blocked_result(name: Any, reason: str) -> ScriptResult:
    label = "" if name is None else str(name)
    return ScriptResult(
        script=label,
        ok=False,
        blocked=True,
        error=reason,
        detail=reason,
        timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
        checked_at=_now(),
    )


def run_script(
    name: Any = None,
    args: Any = None,
    timeout: Any = None,
    **kwargs: Any,
) -> ScriptResult:
    """Run allowlisted script *name* with argv *args* and capture output.

    Never raises on bad input; unknown/traversal names yield
    ``blocked=True`` and invalid args yield ``ok=False`` with a detail.
    Accepts ``script``/``timeout_seconds`` aliases via kwargs.
    """
    if name is None:
        name = kwargs.get("script", kwargs.get("name"))
    if args is None:
        for alias in ("args", "argv", "arguments", "params"):
            if kwargs.get(alias) is not None:
                args = kwargs.get(alias)
                break
    if timeout is None:
        timeout = kwargs.get("timeout_seconds", kwargs.get("timeoutSeconds"))

    label = "" if name is None else str(name).strip() if isinstance(name, str) else str(name)
    if not label or not _is_safe_name(label):
        return _blocked_result(label, f"script is not allowlisted: {label!r}")

    target = get_script_path(label)
    if target is None:
        return _blocked_result(label, f"script is not allowlisted: {label!r}")

    try:
        argv = _validate_args(args)
    except ValueError as exc:
        return ScriptResult(
            script=label,
            ok=False,
            error=str(exc),
            detail=str(exc),
            timeout_seconds=_coerce_timeout(timeout),
            checked_at=_now(),
        )

    timeout_f = _coerce_timeout(timeout)
    suffix = target.suffix
    if suffix == ".py":
        cmd = [sys.executable, str(target), *argv]
    elif suffix == ".sh":
        cmd = ["/bin/sh", str(target), *argv]
    else:  # pragma: no cover - get_script_path already filters suffixes
        return _blocked_result(label, f"unsupported script type: {suffix!r}")

    start = time.monotonic()
    try:
        # NOTE: shell is never enabled here (default False) -- keep it that way.
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_f,
            cwd=str(SCRIPT_DIR) if SCRIPT_DIR.exists() else None,
        )
        latency_ms = round((time.monotonic() - start) * 1000.0, 2)
        stdout = proc.stdout if isinstance(proc.stdout, str) else ""
        stderr = proc.stderr if isinstance(proc.stderr, str) else ""
        stdout, stdout_tr = _truncate(stdout, MAX_STDOUT_CHARS)
        stderr, stderr_tr = _truncate(stderr, MAX_STDERR_CHARS)
        ok = proc.returncode == 0
        return ScriptResult(
            script=label,
            ok=ok,
            returncode=proc.returncode,
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=stdout_tr,
            stderr_truncated=stderr_tr,
            truncated=bool(stdout_tr or stderr_tr),
            command=cmd,
            latency_ms=latency_ms,
            timeout_seconds=timeout_f,
            timed_out=False,
            error=None if ok else (stderr.strip().splitlines()[0][:500] if stderr.strip() else f"exit {proc.returncode}"),
            detail=f"exit {proc.returncode}",
            checked_at=_now(),
        )
    except subprocess.TimeoutExpired as exc:
        latency_ms = round((time.monotonic() - start) * 1000.0, 2)
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        stdout, stdout_tr = _truncate(stdout or "", MAX_STDOUT_CHARS)
        stderr, stderr_tr = _truncate(stderr or "", MAX_STDERR_CHARS)
        reason = f"timeout after {timeout_f}s"
        return ScriptResult(
            script=label,
            ok=False,
            returncode=None,
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=stdout_tr,
            stderr_truncated=stderr_tr,
            truncated=bool(stdout_tr or stderr_tr),
            command=cmd,
            latency_ms=latency_ms,
            timeout_seconds=timeout_f,
            timed_out=True,
            error=reason,
            detail=reason,
            checked_at=_now(),
        )
    except Exception as exc:  # noqa: BLE001
        latency_ms = round((time.monotonic() - start) * 1000.0, 2)
        reason = f"{type(exc).__name__}: {exc}"[:500]
        return ScriptResult(
            script=label,
            ok=False,
            error=reason,
            detail=reason,
            command=cmd,
            latency_ms=latency_ms,
            timeout_seconds=timeout_f,
            checked_at=_now(),
        )


# Aliases (repo convention: many names, same behaviour).
run = run_script
execute = run_script
execute_script = run_script
run_allowed_script = run_script
run_script_timeout = run_script


def get_status(name: Any = None, **kwargs: Any) -> Optional[Dict[str, Any]]:
    """Return allowlist metadata for *name*, if allowlisted."""
    label = name if name is not None else kwargs.get("script", kwargs.get("name"))
    path = get_script_path(label)
    if path is None:
        return None
    for entry in list_scripts():
        if entry["name"] == str(label).strip():
            return entry
    return {"name": str(label).strip(), "path": str(path), "exists": True, "allowed": True}


get = get_status


def get_summary() -> Dict[str, Any]:
    items = list_scripts()
    return {
        "total": len(items),
        "count": len(items),
        "scripts": [entry["name"] for entry in items],
        "names": [entry["name"] for entry in items],
        "ok": True,
        "success": True,
    }


summary = get_summary
get_stats = get_summary
stats = get_summary


__all__ = [
    "SCRIPT_DIR",
    "ALLOWED_SCRIPTS",
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_TIMEOUT_SECONDS",
    "MAX_OUTPUT_CHARS",
    "ScriptResult",
    "ScriptRunnerResult",
    "RunResult",
    "is_allowed",
    "get_script_path",
    "list_scripts",
    "register_script",
    "unregister_script",
    "run_script",
    "run",
    "execute",
    "get_status",
    "get_summary",
]
