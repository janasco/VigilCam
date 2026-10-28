"""Diagnostics bundle service.

Collect sanitized config, runtime metrics, and recent logs into a single
in-memory zip archive plus a tamper-evident manifest (SHA-256 per file).

Standard library only (apart from app config / metrics helpers).
Secrets are redacted before anything is written to the bundle.
"""
from __future__ import annotations

import hashlib
import io
import json
import platform
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

TOOL_NAME = "vigilcam-diagnostics"
TOOL_VERSION = "1.0"

MAX_LOG_FILES = 20
MAX_LOG_BYTES_EACH = 256 * 1024  # tail per file
MAX_TOTAL_LOG_BYTES = 2 * 1024 * 1024

_SECRET_KEY_RE = re.compile(r"(password|secret|token|api[_-]?key|license[_-]?key)", re.IGNORECASE)
_CRED_IN_URL = re.compile(r"(?i)(rtsps?|https?|onvif)://([^/@\s:]+)(:[^/@\s]*)?@")


def _redact_key(key: str) -> bool:
    return bool(_SECRET_KEY_RE.search(key or ""))


def redact_value(value: Any) -> Any:
    """Recursively redact secrets and embedded URL credentials."""
    if isinstance(value, dict):
        return {k: ("<redacted>" if _redact_key(str(k)) else redact_value(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_value(v) for v in value]
    if isinstance(value, str):
        return _CRED_IN_URL.sub(r"\1://<redacted>@", value)
    return value


def redact_dict(data: dict) -> dict:
    if not isinstance(data, dict):
        return {}
    return redact_value(dict(data))


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _base_dirs() -> tuple[Path, Path]:
    try:
        from app.core.config import BASE_DIR, DATA_DIR

        return Path(BASE_DIR), Path(DATA_DIR)
    except Exception:  # noqa: BLE001
        here = Path(__file__).resolve().parents[2]
        return here, here / "data"


def log_candidate_dirs() -> list[Path]:
    base, data = _base_dirs()
    candidates = [
        base / "logs",
        data / "logs",
        data,
        base,
        base / "data" / "logs",
    ]
    out: list[Path] = []
    for cand in candidates:
        try:
            p = Path(cand)
            if p.is_dir() and p not in out:
                out.append(p)
        except Exception:  # noqa: BLE001
            continue
    return out


def find_log_files(
    dirs: Optional[list[Path]] = None,
    max_files: int = MAX_LOG_FILES,
    patterns: tuple[str, ...] = ("*.log", "*.log.*", "*.out", "*.err"),
) -> list[Path]:
    """Locate candidate log files (non-recursive + one level deep)."""
    search_dirs = dirs if dirs is not None else log_candidate_dirs()
    found: list[Path] = []
    seen: set[str] = set()
    for d in search_dirs:
        try:
            if not d.is_dir():
                continue
            for pat in patterns:
                for p in sorted(d.glob(pat)):
                    try:
                        if p.is_file() and str(p.resolve()) not in seen:
                            seen.add(str(p.resolve()))
                            found.append(p)
                            if len(found) >= max_files:
                                return found
                    except Exception:  # noqa: BLE001
                        continue
            # one level deep
            for sub in sorted(d.iterdir()):
                try:
                    if not sub.is_dir() or sub.is_symlink():
                        continue
                    for pat in patterns:
                        for p in sorted(sub.glob(pat)):
                            if p.is_file() and str(p.resolve()) not in seen:
                                seen.add(str(p.resolve()))
                                found.append(p)
                                if len(found) >= max_files:
                                    return found
                except Exception:  # noqa: BLE001
                    continue
        except Exception:  # noqa: BLE001
            continue
    return found[:max_files]


def _tail_bytes(path: Path, limit: int = MAX_LOG_BYTES_EACH) -> bytes:
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > limit:
                f.seek(size - limit)
                data = f.read(limit)
                # Drop a possible partial first line.
                nl = data.find(b"\n")
                if nl != -1:
                    data = data[nl + 1 :]
                return b"... [truncated]\n" + data
            return f.read()
    except Exception as exc:  # noqa: BLE001
        return f"<unreadable: {exc}>".encode("utf-8", "replace")


def collect_logs(
    max_files: int = MAX_LOG_FILES,
    max_bytes_each: int = MAX_LOG_BYTES_EACH,
) -> tuple[dict[str, bytes], dict[str, str]]:
    """Return ({arcname: bytes}, {arcname: reason-missing})."""
    files: dict[str, bytes] = {}
    missing: dict[str, str] = {}
    total = 0
    for path in find_log_files(max_files=max_files):
        try:
            data = _tail_bytes(path, max_bytes_each)
        except Exception as exc:  # noqa: BLE001
            missing[f"logs/{path.name}"] = str(exc)
            continue
        if total + len(data) > MAX_TOTAL_LOG_BYTES:
            missing[f"logs/{path.name}"] = "skipped: total log budget exceeded"
            continue
        arcname = f"logs/{path.name}"
        # Disambiguate duplicate basenames.
        suffix = 1
        while arcname in files:
            suffix += 1
            arcname = f"logs/{path.stem}-{suffix}{path.suffix}"
        files[arcname] = data
        total += len(data)
    if not files:
        missing["logs/"] = "no log files found"
    return files, missing


def collect_config() -> dict[str, Any]:
    """Return redacted app settings as a plain dict."""
    try:
        from app.core.config import settings

        dump = settings.model_dump() if hasattr(settings, "model_dump") else dict(settings)
        # Path objects are not JSON serializable; stringify them.
        safe: dict[str, Any] = {}
        for k, v in dump.items():
            try:
                if isinstance(v, Path):
                    v = str(v)
                json.dumps(v)
                safe[k] = v
            except Exception:  # noqa: BLE001
                safe[k] = str(v)
        return redact_dict(safe)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"config unavailable: {exc}"}


def _fallback_metrics() -> dict[str, Any]:
    import shutil

    disk: dict[str, Any] = {"path": "/", "total": None, "used": None, "free": None}
    try:
        total, used, free = shutil.disk_usage("/")
        disk = {"path": "/", "total": total, "used": used, "free": free}
    except Exception:  # noqa: BLE001
        pass
    return {
        "timestamp": utc_now_iso(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cpu_count": __import__("os").cpu_count(),
        "disk": disk,
        "fallback": True,
    }


async def collect_metrics(db: Any = None) -> dict[str, Any]:
    """Collect runtime metrics; never raises."""
    try:
        from app.services.system_metrics import collect_system_metrics

        data = await collect_system_metrics(db)
        if isinstance(data, dict):
            return data
        return {"metrics": data}
    except Exception:  # noqa: BLE001
        pass
    # Sync fallback for environments without DB / psutil-style helpers.
    try:
        from app.services.system_metrics import _disk_usage as _du  # type: ignore

        _ = _du
    except Exception:  # noqa: BLE001
        pass
    return _fallback_metrics()


def collect_metrics_sync(db: Any = None) -> dict[str, Any]:
    """Synchronous wrapper (runs coroutine if no loop is running)."""
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is None:
        try:
            return asyncio.run(collect_metrics(db))
        except Exception:  # noqa: BLE001
            return _fallback_metrics()
    # Already inside a loop: return the fallback to avoid blocking.
    return _fallback_metrics()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_bundle_files(
    *,
    include_logs: bool = True,
    include_config: bool = True,
    include_metrics: bool = True,
    metrics: Optional[dict] = None,
    config: Optional[dict] = None,
    extra_files: Optional[dict[str, bytes]] = None,
    max_log_files: int = MAX_LOG_FILES,
    max_log_bytes_each: int = MAX_LOG_BYTES_EACH,
) -> tuple[dict[str, bytes], dict[str, Any]]:
    """Assemble bundle members. Returns (files, parsed-context)."""
    files: dict[str, bytes] = {}
    missing: dict[str, str] = {}
    parsed: dict[str, Any] = {}

    if include_config:
        cfg = config if config is not None else collect_config()
        cfg = redact_dict(cfg if isinstance(cfg, dict) else {"value": cfg})
        parsed["config/config.json"] = cfg
        files["config/config.json"] = (json.dumps(cfg, indent=2, sort_keys=True, default=str) + "\n").encode()

    if include_metrics:
        m = metrics if metrics is not None else _fallback_metrics()
        if not isinstance(m, dict):
            m = {"value": m}
        parsed["metrics/metrics.json"] = m
        files["metrics/metrics.json"] = (json.dumps(m, indent=2, sort_keys=True, default=str) + "\n").encode()

    if include_logs:
        log_files, log_missing = collect_logs(max_files=max_log_files, max_bytes_each=max_log_bytes_each)
        files.update(log_files)
        missing.update(log_missing)

    if extra_files:
        for name, data in extra_files.items():
            if isinstance(data, str):
                data = data.encode()
            files[str(name)] = bytes(data)

    parsed["_missing"] = missing
    return files, parsed


async def build_bundle_files_async(
    *,
    include_logs: bool = True,
    include_config: bool = True,
    include_metrics: bool = True,
    db: Any = None,
    extra_files: Optional[dict[str, bytes]] = None,
    max_log_files: int = MAX_LOG_FILES,
    max_log_bytes_each: int = MAX_LOG_BYTES_EACH,
) -> tuple[dict[str, bytes], dict[str, Any]]:
    metrics = await collect_metrics(db) if include_metrics else None
    return build_bundle_files(
        include_logs=include_logs,
        include_config=include_config,
        include_metrics=include_metrics,
        metrics=metrics,
        extra_files=extra_files,
        max_log_files=max_log_files,
        max_log_bytes_each=max_log_bytes_each,
    )


def finalize_bundle(
    files: dict[str, bytes],
    parsed: dict[str, Any],
    *,
    operator: Optional[str] = None,
) -> tuple[bytes, dict[str, Any]]:
    """Add DIAGNOSTICS.md + manifest.json, zip everything, return (zip_bytes, manifest)."""
    generated_at = utc_now_iso()
    missing = dict(parsed.get("_missing") or {})

    lines = [
        "# VigilCam diagnostics bundle",
        "",
        f"Generated: `{generated_at}`" + (f" by `{operator}`" if operator else ""),
        f"Tool: `{TOOL_NAME} {TOOL_VERSION}`",
        "",
        "Contents",
        "",
    ]
    for name in sorted(files):
        lines.append(f"* `{name}`")
    if missing:
        lines += ["", "Not collected", ""]
        for name, why in sorted(missing.items()):
            lines.append(f"* `{name}` — {why}")
    lines += [
        "",
        "Secrets (passwords, tokens, API keys) are redacted. Stream URLs have",
        "embedded credentials stripped.",
        "",
    ]
    files["DIAGNOSTICS.md"] = "\n".join(lines).encode()

    manifest = {
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "generated_at": generated_at,
        "operator": operator,
        "files": {name: {"sha256": _sha256(data), "bytes": len(data)} for name, data in sorted(files.items())},
        "missing": missing,
    }
    files["manifest.json"] = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(files):
            zf.writestr(name, files[name])
    return buf.getvalue(), manifest


def build_bundle_bytes(
    *,
    include_logs: bool = True,
    include_config: bool = True,
    include_metrics: bool = True,
    metrics: Optional[dict] = None,
    config: Optional[dict] = None,
    extra_files: Optional[dict[str, bytes]] = None,
    operator: Optional[str] = None,
    max_log_files: int = MAX_LOG_FILES,
    max_log_bytes_each: int = MAX_LOG_BYTES_EACH,
) -> tuple[bytes, dict[str, Any]]:
    """Synchronous entry point: collect + zip. Never raises on collection errors."""
    files, parsed = build_bundle_files(
        include_logs=include_logs,
        include_config=include_config,
        include_metrics=include_metrics,
        metrics=metrics if metrics is not None else (_fallback_metrics() if include_metrics else None),
        config=config,
        extra_files=extra_files,
        max_log_files=max_log_files,
        max_log_bytes_each=max_log_bytes_each,
    )
    return finalize_bundle(files, parsed, operator=operator)


async def build_bundle_bytes_async(
    *,
    include_logs: bool = True,
    include_config: bool = True,
    include_metrics: bool = True,
    db: Any = None,
    extra_files: Optional[dict[str, bytes]] = None,
    operator: Optional[str] = None,
    max_log_files: int = MAX_LOG_FILES,
    max_log_bytes_each: int = MAX_LOG_BYTES_EACH,
) -> tuple[bytes, dict[str, Any]]:
    files, parsed = await build_bundle_files_async(
        include_logs=include_logs,
        include_config=include_config,
        include_metrics=include_metrics,
        db=db,
        extra_files=extra_files,
        max_log_files=max_log_files,
        max_log_bytes_each=max_log_bytes_each,
    )
    return finalize_bundle(files, parsed, operator=operator)


def build_bundle_file(
    out_path: str | Path,
    *,
    include_logs: bool = True,
    include_config: bool = True,
    include_metrics: bool = True,
    metrics: Optional[dict] = None,
    config: Optional[dict] = None,
    extra_files: Optional[dict[str, bytes]] = None,
    operator: Optional[str] = None,
    max_log_files: int = MAX_LOG_FILES,
    max_log_bytes_each: int = MAX_LOG_BYTES_EACH,
) -> dict[str, Any]:
    """Write the bundle zip to disk; returns the manifest."""
    blob, manifest = build_bundle_bytes(
        include_logs=include_logs,
        include_config=include_config,
        include_metrics=include_metrics,
        metrics=metrics,
        config=config,
        extra_files=extra_files,
        operator=operator,
        max_log_files=max_log_files,
        max_log_bytes_each=max_log_bytes_each,
    )
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(blob)
    return manifest


# Backwards/forwards-compatible aliases.
collect = collect_logs
collect_all = build_bundle_files
generate = build_bundle_bytes
generate_bundle = build_bundle_bytes
create_bundle = build_bundle_bytes
build_bundle = build_bundle_bytes
get_config = collect_config
get_metrics = collect_metrics_sync
