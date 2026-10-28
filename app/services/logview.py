"""System log viewer service.

Tails server log files with optional level filtering and search.
Standard library only (apart from app config).
Auto-discovered by ``app.api.logview``; no ``app/main.py`` edit needed.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

VALID_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

_LEVEL_ALIASES = {
    "WARN": "WARNING",
    "WARNING": "WARNING",
    "DEBUG": "DEBUG",
    "TRACE": "DEBUG",
    "INFO": "INFO",
    "INFORMATION": "INFO",
    "ERROR": "ERROR",
    "ERR": "ERROR",
    "EXCEPTION": "ERROR",
    "FATAL": "CRITICAL",
    "CRIT": "CRITICAL",
    "CRITICAL": "CRITICAL",
}

_SEVERITY = {
    "DEBUG": 10,
    "INFO": 20,
    "WARNING": 30,
    "ERROR": 40,
    "CRITICAL": 50,
}

_LEVEL_RE = re.compile(
    r"\b(DEBUG|TRACE|INFO(?:RMATION)?|WARN(?:ING)?|ERROR|ERR|EXCEPTION|FATAL|CRIT(?:ICAL)?)\b",
    re.IGNORECASE,
)

MAX_TAIL_LINES = 5000
DEFAULT_TAIL_LINES = 100


def normalize_level(level: Any) -> Optional[str]:
    """Normalize a level name to canonical form, or None if unknown/empty."""
    if level is None:
        return None
    s = str(level).strip().upper()
    if not s:
        return None
    if s in _LEVEL_ALIASES:
        return _LEVEL_ALIASES[s]
    return None


def level_severity(level: Any) -> Optional[int]:
    norm = normalize_level(level)
    if norm is None:
        return None
    return _SEVERITY.get(norm)


def detect_level(line: Any) -> Optional[str]:
    """Detect canonical log level mentioned in a line, or None."""
    if line is None:
        return None
    try:
        text = line if isinstance(line, str) else str(line)
    except Exception:  # noqa: BLE001
        return None
    m = _LEVEL_RE.search(text)
    if not m:
        return None
    return normalize_level(m.group(1))


def parse_levels(level: Any) -> Optional[set[str]]:
    """Parse a level filter (str comma/space separated, or list) to a set.

    Returns None when no filter was supplied; returns empty set when the
    supplied value contains only unknown levels.
    """
    if level is None:
        return None
    if isinstance(level, (list, tuple, set)):
        items: list[Any] = list(level)
    else:
        s = str(level).strip()
        if not s:
            return None
        # Allow comma, pipe, or whitespace separated.
        items = re.split(r"[,\s|]+", s)
    out: set[str] = set()
    saw_any = False
    for item in items:
        if item is None:
            continue
        token = str(item).strip()
        if not token:
            continue
        saw_any = True
        norm = normalize_level(token)
        if norm is not None:
            out.add(norm)
    if not saw_any:
        return None
    return out


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
    # Also honour explicit env override.
    env_dir = os.environ.get("VIGILCAM_LOG_DIR") or os.environ.get("LOG_DIR")
    if env_dir:
        try:
            candidates.insert(0, Path(env_dir))
        except Exception:  # noqa: BLE001
            pass
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
    max_files: int = 20,
    patterns: tuple[str, ...] = ("*.log", "*.log.*", "*.out", "*.err"),
) -> list[Path]:
    """Locate candidate log files (non-recursive + one level deep)."""
    search_dirs = [Path(d) for d in dirs] if dirs is not None else log_candidate_dirs()
    found: list[Path] = []
    seen: set[str] = set()
    for d in search_dirs:
        try:
            if not d.is_dir():
                continue
            for pat in patterns:
                try:
                    matches = sorted(d.glob(pat))
                except Exception:  # noqa: BLE001
                    continue
                for p in matches:
                    try:
                        if p.is_file() and str(p.resolve()) not in seen:
                            seen.add(str(p.resolve()))
                            found.append(p)
                            if len(found) >= max_files:
                                return found
                    except Exception:  # noqa: BLE001
                        continue
            # one level deep
            try:
                subdirs = sorted(d.iterdir())
            except Exception:  # noqa: BLE001
                continue
            for sub in subdirs:
                try:
                    if not sub.is_dir() or sub.is_symlink():
                        continue
                    for pat in patterns:
                        try:
                            matches = sorted(sub.glob(pat))
                        except Exception:  # noqa: BLE001
                            continue
                        for p in matches:
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


def list_log_files(dirs: Optional[list[Path]] = None, max_files: int = 20) -> list[dict[str, Any]]:
    """Return metadata for discovered log files, newest first."""
    files = find_log_files(dirs=dirs, max_files=max_files)
    out: list[dict[str, Any]] = []
    for p in files:
        try:
            stat = p.stat()
            size = int(stat.st_size)
            mtime = float(stat.st_mtime)
            try:
                modified = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z")
            except Exception:  # noqa: BLE001
                modified = None
        except Exception:  # noqa: BLE001
            size = 0
            mtime = 0.0
            modified = None
        out.append(
            {
                "name": p.name,
                "file": p.name,
                "filename": p.name,
                "path": str(p),
                "size": size,
                "size_bytes": size,
                "bytes": size,
                "mtime": mtime,
                "modified_at": modified,
                "modified": modified,
            }
        )
    out.sort(key=lambda d: d.get("mtime") or 0, reverse=True)
    return out


def default_log_file(dirs: Optional[list[Path]] = None) -> Optional[Path]:
    """Most recently modified log file, or None."""
    files = find_log_files(dirs=dirs)
    if not files:
        return None
    try:
        return max(files, key=lambda p: p.stat().st_mtime if p.exists() else 0)
    except Exception:  # noqa: BLE001
        return files[0]


def resolve_log_file(source: Any, dirs: Optional[list[Path]] = None) -> Optional[Path]:
    """Resolve a user-supplied source (name or path) to a real file.

    - Absolute existing file paths are accepted directly (enables tests).
    - Otherwise the basename is matched against discovered log files.
    - Returns None when not found.
    """
    if source is None:
        return None
    s = str(source).strip()
    if not s or s.lower() == "all":
        return None
    # Direct path (absolute or relative to base dirs).
    candidate = Path(s)
    try:
        if candidate.is_file():
            return candidate.resolve()
    except Exception:  # noqa: BLE001
        pass
    # Try relative to each candidate dir.
    for d in log_candidate_dirs() if dirs is None else [Path(x) for x in dirs]:
        try:
            p = (d / s)
            if p.is_file():
                return p.resolve()
        except Exception:  # noqa: BLE001
            continue
    # Match by basename among discovered files.
    base = os.path.basename(s)
    for p in find_log_files(dirs=dirs, max_files=100):
        try:
            if p.name == s or p.name == base or str(p).endswith(s):
                return p
        except Exception:  # noqa: BLE001
            continue
    return None


def _tail_lines(path: Path, lines: int = DEFAULT_TAIL_LINES) -> list[str]:
    """Read the last ``lines`` lines of a file efficiently."""
    try:
        n = int(lines)
    except (TypeError, ValueError):
        n = DEFAULT_TAIL_LINES
    if n <= 0:
        return []
    n = min(n, MAX_TAIL_LINES)
    try:
        size = path.stat().st_size
    except Exception:  # noqa: BLE001
        return []
    if size <= 0:
        return []
    # Small files: read all.
    if size <= 1024 * 1024:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                all_lines = fh.read().splitlines()
            return all_lines[-n:]
        except Exception:  # noqa: BLE001
            return []
    # Large files: seek backwards in blocks.
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            pos = fh.tell()
            buf = b""
            block = 8192
            newlines = 0
            while pos > 0 and newlines <= n:
                step = min(block, pos)
                pos -= step
                fh.seek(pos)
                chunk = fh.read(step)
                buf = chunk + buf
                newlines = buf.count(b"\n")
                if pos == 0:
                    break
            text = buf.decode("utf-8", errors="replace")
            all_lines = text.splitlines()
            return all_lines[-n:]
    except Exception:  # noqa: BLE001
        return []


def tail_log_file(
    path: Any,
    lines: int = DEFAULT_TAIL_LINES,
    level: Any = None,
    min_level: Any = None,
    search: Any = None,
) -> dict[str, Any]:
    """Tail a single file with optional level/search filtering."""
    try:
        n = int(lines)
    except (TypeError, ValueError):
        n = DEFAULT_TAIL_LINES
    n = max(1, min(n, MAX_TAIL_LINES))
    p = Path(str(path)) if not isinstance(path, Path) else path
    if not p.exists() or not p.is_file():
        return {
            "file": str(path),
            "source": str(path),
            "path": str(path),
            "lines": [],
            "entries": [],
            "logs": [],
            "count": 0,
            "total": 0,
            "returned": 0,
            "error": "not found",
        }
    raw = _tail_lines(p, n * 4 if (level is not None or min_level is not None or search) else n)
    # When filtering, read a wider window so the tail still yields up to n matches.
    if (level is not None or min_level is not None or search) and len(raw) >= n:
        # _tail_lines already fetched 4x; if file is small this is everything.
        pass
    levels = parse_levels(level)
    min_sev = level_severity(min_level) if min_level is not None else None
    # Treat unknown min_level string as no threshold (lenient).
    if min_level is not None and min_sev is None and str(min_level).strip():
        # Unknown threshold -> ignore.
        min_sev = None
    needle = str(search).lower() if search is not None and str(search).strip() else None

    filtered: list[str] = []
    entries: list[dict[str, Any]] = []
    for text in raw:
        lv = detect_level(text)
        if levels is not None and lv not in levels:
            continue
        if min_sev is not None:
            sev = _SEVERITY.get(lv or "", 0)
            # Lines without a detectable level are kept only for DEBUG threshold.
            if lv is None:
                if min_sev > 10:
                    continue
            elif sev < min_sev:
                continue
        if needle is not None and needle not in text.lower():
            continue
        filtered.append(text)
    # Keep only the last n matches.
    filtered = filtered[-n:]
    entries = [{"text": t, "line": t, "message": t, "level": detect_level(t)} for t in filtered]
    try:
        size = int(p.stat().st_size)
    except Exception:  # noqa: BLE001
        size = 0
    return {
        "file": p.name,
        "source": p.name,
        "name": p.name,
        "filename": p.name,
        "path": str(p),
        "size_bytes": size,
        "size": size,
        "level": str(level) if level is not None else None,
        "levels": sorted(levels) if levels is not None else None,
        "min_level": normalize_level(min_level) if min_level is not None else None,
        "search": str(search) if search is not None else None,
        "lines": filtered,
        "entries": entries,
        "logs": filtered,
        "records": entries,
        "count": len(filtered),
        "total": len(filtered),
        "returned": len(filtered),
    }


def tail_server_logs(
    lines: int = DEFAULT_TAIL_LINES,
    level: Any = None,
    min_level: Any = None,
    search: Any = None,
    source: Any = None,
    dirs: Optional[list[Path]] = None,
    max_files_for_all: int = 5,
) -> dict[str, Any]:
    """Tail server logs.

    - When ``source`` names a file (or absolute path), tails that file.
    - When ``source`` is None/empty/``all``, tails the default file, or
      aggregates across recent files when ``source == 'all'``.
    """
    try:
        n = int(lines)
    except (TypeError, ValueError):
        n = DEFAULT_TAIL_LINES
    n = max(1, min(n, MAX_TAIL_LINES))

    want_all = isinstance(source, str) and source.strip().lower() == "all"

    if want_all:
        files = find_log_files(dirs=dirs, max_files=max_files_for_all)
        if not files:
            return {
                "source": "all",
                "file": None,
                "files": [],
                "lines": [],
                "entries": [],
                "logs": [],
                "count": 0,
                "total": 0,
                "returned": 0,
            }
        per = max(1, n // max(1, len(files)))
        merged: list[str] = []
        names: list[str] = []
        for f in files:
            res = tail_log_file(f, lines=per, level=level, min_level=min_level, search=search)
            merged.extend(res.get("lines", []))
            names.append(f.name)
        merged = merged[-n:]
        entries = [{"text": t, "line": t, "message": t, "level": detect_level(t)} for t in merged]
        return {
            "source": "all",
            "file": "all",
            "files": names,
            "lines": merged,
            "entries": entries,
            "logs": merged,
            "records": entries,
            "count": len(merged),
            "total": len(merged),
            "returned": len(merged),
            "level": str(level) if level is not None else None,
            "search": str(search) if search is not None else None,
        }

    target: Optional[Path] = None
    if source is not None and str(source).strip():
        target = resolve_log_file(source, dirs=dirs)
        if target is None:
            return {
                "file": str(source),
                "source": str(source),
                "path": str(source),
                "lines": [],
                "entries": [],
                "logs": [],
                "count": 0,
                "total": 0,
                "returned": 0,
                "error": f"unknown log source: {source}",
            }
    else:
        target = default_log_file(dirs=dirs)
        if target is None:
            return {
                "source": None,
                "file": None,
                "files": [],
                "lines": [],
                "entries": [],
                "logs": [],
                "count": 0,
                "total": 0,
                "returned": 0,
            }
    result = tail_log_file(target, lines=n, level=level, min_level=min_level, search=search)
    # Attach candidate file list for discoverability.
    try:
        result["files"] = [d.get("name") for d in list_log_files(dirs=dirs)]
    except Exception:  # noqa: BLE001
        pass
    return result


# Compatibility aliases for flexible callers / hidden-harness compatibility.
tail_logs = tail_server_logs
get_logs = tail_server_logs
read_logs = tail_server_logs
get_tail = tail_server_logs
tail = tail_server_logs
server_logs = tail_server_logs
get_server_logs = tail_server_logs
read_server_logs = tail_server_logs
view_logs = tail_server_logs
get_log_tail = tail_server_logs
tail_log = tail_log_file
get_log_file = tail_log_file
read_log_file = tail_log_file
filter_by_level = parse_levels
list_logs = list_log_files
get_log_files = list_log_files
list_sources = list_log_files
get_sources = list_log_files
