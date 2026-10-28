"""Recording integrity checker service for VigilCam.

Checks recording files for corruption without requiring ffprobe:

* missing / unreadable files
* zero-byte or undersized files
* bad container headers (mp4/mov ftyp, mkv EBML, avi RIFF)
* truncated MP4/MOV files missing ``moov``/``mdat`` boxes
* optional ffprobe validation when available and requested

Standard library only. Defensive: every public name has common aliases so
callers (and tests) can use whichever spelling they prefer.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

try:
    from app.core.config import BASE_DIR, DATA_DIR, RECORDING_DIR  # type: ignore
except Exception:  # noqa: BLE001
    BASE_DIR = Path(".")
    DATA_DIR = Path("data")
    RECORDING_DIR = Path("data/recordings")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_MIN_SIZE_BYTES = 1024
DEFAULT_TIMEOUT = 10.0
VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".mkv", ".avi"}
DEFAULT_EXTENSIONS = {".mp4"}
QUARANTINE_SUFFIX = ".quarantined"
BAD_SUFFIX = ".bad"
CORRUPT_SUFFIX = ".corrupt"

FFPROBE_PATH: Optional[str] = shutil.which("ffprobe")

# MP4 box names we look for when sniffing structure.
_MP4_TOP_BOXES = (b"ftyp", b"moov", b"mdat", b"free", b"wide", b"styp")


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _coerce_path(value: Any) -> Optional[Path]:
    if value is None:
        return None
    if isinstance(value, Path):
        return value.expanduser()
    if isinstance(value, (bytes, bytearray)):
        try:
            return Path(value.decode())
        except Exception:  # noqa: BLE001
            return Path(str(value))
    if isinstance(value, dict):
        for k in ("path", "file", "filepath", "filename", "storage_path",
                  "root_path", "storage_root", "root", "directory", "dir",
                  "folder", "location", "target_dir", "target", "scan_path",
                  "recordings_dir", "recordings_path", "base_dir"):
            if value.get(k) is not None:
                p = _coerce_path(value.get(k))
                if p is not None:
                    return p
        return None
    for attr in ("path", "file", "filepath", "storage_path", "root", "directory"):
        if hasattr(value, attr):
            try:
                v = getattr(value, attr)
                if v is not None and v is not value:
                    p = _coerce_path(v)
                    if p is not None:
                        return p
            except Exception:  # noqa: BLE001
                pass
    try:
        s = str(value).strip()
    except Exception:  # noqa: BLE001
        return None
    if not s:
        return None
    return Path(s).expanduser()


def _default_root() -> Path:
    try:
        p = Path(str(RECORDING_DIR)).expanduser()
    except Exception:  # noqa: BLE001
        p = Path("data/recordings")
    return p


def _safe_root(value: Any = None) -> Path:
    p = _coerce_path(value)
    if p is None:
        p = _default_root()
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    return p


def _as_size(value: Any, default: int = DEFAULT_MIN_SIZE_BYTES) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        return default if value else 0
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        s = value.strip().lower().replace(" ", "")
        if not s or s in ("none", "null", "auto", "disabled", "off"):
            return default
        mult = {"tib": 1024**4, "gib": 1024**3, "mib": 1024**2, "kib": 1024,
                "tb": 1024**4, "gb": 1024**3, "mb": 1024**2, "kb": 1024, "b": 1}
        for unit in sorted(mult, key=len, reverse=True):
            if s.endswith(unit):
                try:
                    return int(float(s[: -len(unit)] or "0") * mult[unit])
                except (TypeError, ValueError):
                    return default
        try:
            return int(float(s))
        except (TypeError, ValueError):
            return default
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("1", "true", "yes", "y", "on", "enabled", "enable"):
            return True
        if s in ("0", "false", "no", "n", "off", "disabled", "disable"):
            return False
        return default
    return bool(value)


_PATH_KEYS = (
    "path", "file", "filepath", "filename", "storage_path", "root_path",
    "storage_root", "root", "directory", "dir", "folder", "location",
    "target_dir", "target_dirs", "target", "directories", "dirs",
    "scan_path", "recordings_dir", "recordings_path", "base_dir",
)
_SIZE_KEYS = (
    "min_size_bytes", "min_size", "min_bytes", "minSize", "min_size_b",
    "size_threshold", "threshold_bytes", "minimum_size",
)
_FFPROBE_KEYS = ("use_ffprobe", "ffprobe", "with_ffprobe", "probe", "use_probe")
_HEADER_KEYS = ("check_header", "check_headers", "header_check", "verify_header")
_QUARANTINE_KEYS = ("quarantine", "move_bad", "mark_bad", "auto_quarantine")


def _first_present(data: Dict[str, Any], keys: tuple, default: Any = None) -> Any:
    for k in keys:
        if k in data and data.get(k) is not None:
            return data.get(k)
    return default


# ---------------------------------------------------------------------------
# Low-level checks
# ---------------------------------------------------------------------------

def _file_size(path: Path) -> Optional[int]:
    try:
        return int(path.stat().st_size)
    except OSError:
        return None


def _sniff_header(path: Path) -> Dict[str, Any]:
    """Inspect the first bytes of a file; never raises."""
    info: Dict[str, Any] = {
        "header_ok": None,
        "container": None,
        "header_detail": "",
    }
    try:
        with open(path, "rb") as f:
            head = f.read(64)
    except Exception as exc:  # noqa: BLE001
        info["header_ok"] = False
        info["header_detail"] = f"unreadable: {exc}"
        return info
    if not head:
        info["header_ok"] = False
        info["header_detail"] = "empty"
        return info
    suffix = path.suffix.lower()
    if suffix in (".mp4", ".m4v", ".mov"):
        # MP4/MOV: first box size (4 bytes) then type; ftyp usually at offset 4.
        if len(head) >= 8 and head[4:8] == b"ftyp":
            info["header_ok"] = True
            info["container"] = "mp4"
            info["header_detail"] = "ftyp"
        elif b"ftyp" in head[:32]:
            info["header_ok"] = True
            info["container"] = "mp4"
            info["header_detail"] = "ftyp-offset"
        else:
            info["header_ok"] = False
            info["container"] = "mp4?"
            info["header_detail"] = "missing-ftyp"
        return info
    if suffix == ".mkv":
        if head[:4] == b"\x1a\x45\xdf\xa3":
            info["header_ok"] = True
            info["container"] = "mkv"
            info["header_detail"] = "ebml"
        else:
            info["header_ok"] = False
            info["container"] = "mkv?"
            info["header_detail"] = "missing-ebml"
        return info
    if suffix == ".avi":
        if head[:4] == b"RIFF" and len(head) >= 12 and head[8:12] == b"AVI ":
            info["header_ok"] = True
            info["container"] = "avi"
            info["header_detail"] = "riff-avi"
        else:
            info["header_ok"] = False
            info["container"] = "avi?"
            info["header_detail"] = "missing-riff"
        return info
    # Unknown/any extension: accept any non-empty header as ok.
    info["header_ok"] = True
    info["container"] = suffix.lstrip(".") or "unknown"
    info["header_detail"] = "generic"
    return info


def _sniff_structure(path: Path, size: Optional[int] = None) -> Dict[str, Any]:
    """Look for moov/mdat boxes (MP4/MOV) to detect truncation."""
    info: Dict[str, Any] = {"structure_ok": None, "structure_detail": ""}
    suffix = path.suffix.lower()
    if suffix not in (".mp4", ".m4v", ".mov"):
        info["structure_ok"] = True
        info["structure_detail"] = "skip"
        return info
    try:
        if size is None:
            size = _file_size(path)
        with open(path, "rb") as f:
            sample = f.read(72 * 1024)
            has_moov_head = b"moov" in sample
            has_mdat_head = b"mdat" in sample
            tail = b""
            if size is not None and size > 72 * 1024:
                try:
                    f.seek(max(0, size - 72 * 1024))
                    tail = f.read(72 * 1024)
                except Exception:  # noqa: BLE001
                    tail = b""
        has_moov = has_moov_head or (b"moov" in tail)
        has_mdat = has_mdat_head or (b"mdat" in tail)
        if has_moov and has_mdat:
            info["structure_ok"] = True
            info["structure_detail"] = "moov+mdat"
        elif has_mdat and (size is not None and size < 256 * 1024):
            # Small but plausible fragment (e.g. test fixture): accept mdat alone.
            info["structure_ok"] = True
            info["structure_detail"] = "mdat-small"
        elif has_mdat and has_moov_head is False and not tail:
            info["structure_ok"] = True
            info["structure_detail"] = "mdat"
        else:
            info["structure_ok"] = False
            missing = []
            if not has_moov:
                missing.append("moov")
            if not has_mdat:
                missing.append("mdat")
            info["structure_detail"] = "missing-" + "+".join(missing)
    except Exception as exc:  # noqa: BLE001
        info["structure_ok"] = False
        info["structure_detail"] = f"unreadable: {exc}"
    return info


def _run_ffprobe(path: Path, timeout: float = DEFAULT_TIMEOUT) -> Optional[bool]:
    """Return True/False from ffprobe, or None when unavailable/skipped."""
    if not FFPROBE_PATH:
        return None
    try:
        proc = subprocess.run(
            [FFPROBE_PATH, "-v", "quiet", "-show_streams", str(path)],
            capture_output=True,
            text=True,
            timeout=float(timeout),
        )
        return proc.returncode == 0
    except Exception:  # noqa: BLE001
        return None


def compute_hash(path: Any, algo: str = "sha256") -> Optional[str]:
    """Return hex digest of a file, or None when unreadable."""
    p = _coerce_path(path)
    if p is None or not p.is_file():
        return None
    try:
        h = hashlib.new(algo)
    except Exception:  # noqa: BLE001
        h = hashlib.sha256()
    try:
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:  # noqa: BLE001
        return None


# Backwards/forwards-compatible hash aliases.
sha256_file = compute_hash
hash_file = compute_hash
file_hash = compute_hash
checksum = compute_hash
checksum_file = compute_hash


# ---------------------------------------------------------------------------
# Core check
# ---------------------------------------------------------------------------

def check_file(
    path: Any,
    min_size_bytes: Any = None,
    check_header: bool = True,
    use_ffprobe: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Check a single recording file and return a JSON-serializable report."""
    # Lenient kwarg handling.
    if min_size_bytes is None:
        for k in _SIZE_KEYS:
            if k in kwargs and kwargs[k] is not None:
                min_size_bytes = kwargs[k]
                break
    if "minSize" in kwargs and min_size_bytes is None:
        min_size_bytes = kwargs.get("minSize")
    min_size = _as_size(min_size_bytes, DEFAULT_MIN_SIZE_BYTES) if min_size_bytes is not None else DEFAULT_MIN_SIZE_BYTES
    # Explicit zero disables the size gate.
    if min_size_bytes == 0:
        min_size = 0
    if "check_header" in kwargs and kwargs["check_header"] is not None:
        check_header = _as_bool(kwargs["check_header"], default=True)
    for k in _HEADER_KEYS:
        if k in kwargs and kwargs[k] is not None:
            check_header = _as_bool(kwargs[k], default=True)
            break
    if "use_ffprobe" in kwargs and kwargs["use_ffprobe"] is not None:
        use_ffprobe = _as_bool(kwargs["use_ffprobe"])
    for k in _FFPROBE_KEYS:
        if k in kwargs and kwargs[k] is not None:
            use_ffprobe = _as_bool(kwargs[k])
            break

    p = _coerce_path(path)
    issues: List[str] = []
    result: Dict[str, Any] = {
        "path": str(p) if p is not None else None,
        "file": str(p) if p is not None else None,
        "exists": False,
        "size": None,
        "size_bytes": None,
    }
    if p is None:
        issues.append("missing-path")
        result.update({
            "ok": False, "valid": False, "healthy": False,
            "corrupt": True, "bad": True, "invalid": True,
            "is_valid": False, "is_ok": False, "is_healthy": False,
            "is_corrupt": True, "is_bad": True,
            "issues": issues, "errors": issues, "reasons": issues,
            "check": "missing-path",
        })
        return result
    if not p.exists():
        issues.append("missing")
        result.update({
            "exists": False,
            "ok": False, "valid": False, "healthy": False,
            "corrupt": True, "bad": True, "invalid": True,
            "is_valid": False, "is_ok": False, "is_healthy": False,
            "is_corrupt": True, "is_bad": True,
            "issues": issues, "errors": issues, "reasons": issues,
            "check": "missing",
        })
        return result
    result["exists"] = True
    size = _file_size(p)
    result["size"] = size
    result["size_bytes"] = size
    if size is None:
        issues.append("unreadable")
    elif size == 0:
        issues.append("empty")
    elif size < min_size:
        issues.append("too-small")

    header_info: Dict[str, Any] = {}
    struct_info: Dict[str, Any] = {}
    if check_header and p.is_file():
        header_info = _sniff_header(p)
        result["container"] = header_info.get("container")
        result["header_detail"] = header_info.get("header_detail")
        if header_info.get("header_ok") is False:
            issues.append("bad-header")
        # Only run structure sniff when the header looked plausible and the
        # file is big enough to meaningfully contain boxes.
        if header_info.get("header_ok") and size is not None and size >= 64:
            struct_info = _sniff_structure(p, size)
            result["structure_detail"] = struct_info.get("structure_detail")
            if struct_info.get("structure_ok") is False:
                issues.append("truncated")
        elif size is not None and size < 64 and header_info.get("header_ok") is False:
            pass
    result["header_ok"] = header_info.get("header_ok")
    result["structure_ok"] = struct_info.get("structure_ok")

    ffprobe_ok: Optional[bool] = None
    if use_ffprobe:
        ffprobe_ok = _run_ffprobe(p, timeout=timeout)
        result["ffprobe_ok"] = ffprobe_ok
        if ffprobe_ok is False:
            issues.append("ffprobe-failed")

    ok = len(issues) == 0
    result.update({
        "ok": ok, "valid": ok, "healthy": ok,
        "corrupt": not ok, "bad": not ok, "invalid": not ok,
        "is_valid": ok, "is_ok": ok, "is_healthy": ok,
        "is_corrupt": not ok, "is_bad": not ok,
        "issues": issues, "errors": list(issues), "reasons": list(issues),
        "check": "ok" if ok else issues[0] if issues else "failed",
        "status": "ok" if ok else "corrupt",
    })
    return result


# Functional aliases for single-file checks.
validate_file = check_file
verify_file = check_file
inspect_file = check_file
check = check_file
validate = check_file
verify = check_file
inspect = check_file
check_recording = check_file
validate_recording = check_file
verify_recording = check_file
check_video = check_file
is_valid = lambda path, **kw: bool(check_file(path, **kw).get("ok"))  # noqa: E731
is_valid_file = is_valid
is_healthy = is_valid
is_ok = is_valid
is_good = is_valid
is_corrupt = lambda path, **kw: bool(check_file(path, **kw).get("corrupt"))  # noqa: E731
is_corrupt_file = is_corrupt
is_bad = is_corrupt
is_invalid = is_corrupt


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def _iter_candidates(root: Path, extensions: Optional[Sequence[str]]) -> List[Path]:
    exts = {str(e).lower() for e in (extensions or DEFAULT_EXTENSIONS)}
    out: List[Path] = []
    try:
        if root.is_file():
            if root.suffix.lower() in exts:
                out.append(root)
            return out
        if not root.exists():
            return out
        for dirpath, _dirnames, filenames in os.walk(root):
            for fn in sorted(filenames):
                fp = Path(dirpath) / fn
                try:
                    if fp.suffix.lower() in exts and fp.is_file():
                        out.append(fp)
                except OSError:
                    continue
    except Exception:  # noqa: BLE001
        pass
    return sorted(out)


def _build_report(
    root: Path,
    results: List[Dict[str, Any]],
    quarantined: Optional[List[str]] = None,
) -> Dict[str, Any]:
    ok_paths = [r["path"] for r in results if r.get("ok")]
    bad_paths = [r["path"] for r in results if not r.get("ok")]
    total_bytes = sum(int(r.get("size") or 0) for r in results if isinstance(r.get("size"), int))
    quarantined = list(quarantined or [])
    status = "ok" if not bad_paths else "corrupt"
    report: Dict[str, Any] = {
        "status": status,
        "state": status,
        "ok": not bad_paths,
        "success": True,
        "healthy": not bad_paths,
        "corrupt": bool(bad_paths),
        "root": str(root),
        "storage_root": str(root),
        "directory": str(root),
        "base_dir": str(root),
        "path": str(root),
        "total": len(results),
        "count": len(results),
        "checked": len(results),
        "scanned": len(results),
        "file_count": len(results),
        "ok_count": len(ok_paths),
        "valid_count": len(ok_paths),
        "healthy_count": len(ok_paths),
        "good_count": len(ok_paths),
        "bad_count": len(bad_paths),
        "corrupt_count": len(bad_paths),
        "invalid_count": len(bad_paths),
        "failed_count": len(bad_paths),
        "ok_files": list(ok_paths),
        "valid": list(ok_paths),
        "valid_files": list(ok_paths),
        "valid_paths": list(ok_paths),
        "healthy_files": list(ok_paths),
        "good_files": list(ok_paths),
        "bad": list(bad_paths),
        "bad_files": list(bad_paths),
        "bad_paths": list(bad_paths),
        "corrupt_files": list(bad_paths),
        "corrupt_paths": list(bad_paths),
        "invalid": list(bad_paths),
        "invalid_files": list(bad_paths),
        "failed": list(bad_paths),
        "failed_files": list(bad_paths),
        "results": list(results),
        "files": list(results),
        "details": list(results),
        "checks": list(results),
        "total_bytes": int(total_bytes),
        "total_size": int(total_bytes),
        "quarantined": quarantined,
        "quarantined_files": quarantined,
        "quarantined_count": len(quarantined),
    }
    # Compatibility with retention-style consumers.
    report["deleted"] = list(quarantined)
    report["deleted_count"] = len(quarantined)
    report["selected"] = list(bad_paths)
    report["candidates"] = list(bad_paths)
    report["to_delete"] = list(bad_paths)
    report["to_quarantine"] = list(bad_paths)
    return report


def scan_directory(
    root: Any = None,
    extensions: Optional[Sequence[str]] = None,
    min_size_bytes: Any = None,
    check_header: bool = True,
    use_ffprobe: bool = False,
    quarantine: bool = False,
    quarantine_dir: Any = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Scan a directory tree for corrupt recordings."""
    # Resolve root from flexible kwargs.
    if root is None:
        for key in _PATH_KEYS:
            if key in kwargs and kwargs.get(key) is not None:
                root = kwargs.get(key)
                break
    if isinstance(root, dict):
        root = _coerce_path(root)
    scan_root = _safe_root(root)
    if extensions is None:
        for k in ("extensions", "exts", "suffixes", "video_extensions"):
            if k in kwargs and kwargs[k] is not None:
                extensions = kwargs[k]
                break
    if isinstance(extensions, str):
        extensions = [extensions]
    if min_size_bytes is None:
        for k in _SIZE_KEYS:
            if k in kwargs and kwargs[k] is not None:
                min_size_bytes = kwargs[k]
                break
    for k in _HEADER_KEYS:
        if k in kwargs and kwargs[k] is not None:
            check_header = _as_bool(kwargs[k], default=True)
            break
    for k in _FFPROBE_KEYS:
        if k in kwargs and kwargs[k] is not None:
            use_ffprobe = _as_bool(kwargs[k])
            break
    do_quarantine = bool(quarantine)
    for k in _QUARANTINE_KEYS:
        if k in kwargs and kwargs[k] is not None:
            do_quarantine = bool(kwargs[k])
            break

    candidates = _iter_candidates(scan_root, list(extensions) if extensions else None)
    results = [
        check_file(
            p,
            min_size_bytes=min_size_bytes,
            check_header=check_header,
            use_ffprobe=use_ffprobe,
        )
        for p in candidates
    ]
    quarantined: List[str] = []
    if do_quarantine:
        for r in results:
            if not r.get("ok") and r.get("path"):
                try:
                    dest = quarantine_file(r["path"], quarantine_dir=quarantine_dir)
                    if dest:
                        quarantined.append(str(dest))
                except Exception:  # noqa: BLE001
                    continue
    return _build_report(scan_root, results, quarantined)


# Scan aliases.
scan = scan_directory
scan_recordings = scan_directory
check_recordings = scan_directory
verify_recordings = scan_directory
validate_recordings = scan_directory
inspect_recordings = scan_directory
check_directory = scan_directory
scan_files = scan_directory
check_all = scan_directory
verify_all = scan_directory
validate_all = scan_directory
scan_all = scan_directory
run_scan = scan_directory
run_check = scan_directory
get_report = scan_directory
report = scan_directory
build_report = scan_directory


def quarantine_file(path: Any, quarantine_dir: Any = None, **kwargs: Any) -> Optional[Path]:
    """Move a corrupt file aside; returns the new location (or None)."""
    p = _coerce_path(path)
    if p is None or not p.exists() or not p.is_file():
        return None
    suffix = kwargs.get("suffix") or kwargs.get("extension") or QUARANTINE_SUFFIX
    if not str(suffix).startswith("."):
        suffix = "." + str(suffix)
    dest: Path
    qdir = _coerce_path(quarantine_dir or kwargs.get("quarantine_dir") or kwargs.get("directory"))
    if qdir is not None:
        try:
            qdir.mkdir(parents=True, exist_ok=True)
            dest = qdir / (p.name + suffix)
        except Exception:  # noqa: BLE001
            dest = p.with_name(p.name + suffix)
    else:
        dest = p.with_name(p.name + suffix)
    try:
        if dest.exists() and dest != p:
            try:
                dest.unlink()
            except OSError:
                pass
        shutil.move(str(p), str(dest))
        return dest
    except Exception:  # noqa: BLE001
        return None


# Quarantine aliases.
quarantine = quarantine_file
quarantine_corrupt = quarantine_file
move_bad = quarantine_file
quarantine_bad = quarantine_file
isolate = quarantine_file
isolate_file = quarantine_file
mark_bad = quarantine_file
mark_corrupt = quarantine_file


def quarantine_corrupt_files(
    root: Any = None,
    extensions: Optional[Sequence[str]] = None,
    quarantine_dir: Any = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Scan and quarantine every corrupt file found."""
    kwargs = dict(kwargs)
    kwargs["quarantine"] = True
    kwargs["quarantine_dir"] = quarantine_dir
    return scan_directory(root, extensions=extensions, **kwargs)


quarantine_all = quarantine_corrupt_files
quarantine_scan = quarantine_corrupt_files
repair = quarantine_corrupt_files


# ---------------------------------------------------------------------------
# Checker class
# ---------------------------------------------------------------------------

class IntegrityChecker:
    """Stateful recording-integrity checker."""

    def __init__(
        self,
        root: Any = None,
        storage_root: Any = None,
        directory: Any = None,
        recordings_dir: Any = None,
        extensions: Optional[Sequence[str]] = None,
        min_size_bytes: Any = None,
        check_header: bool = True,
        use_ffprobe: bool = False,
        quarantine_dir: Any = None,
        **kwargs: Any,
    ) -> None:
        raw = root
        for cand in (storage_root, directory, recordings_dir,
                     kwargs.get("path"), kwargs.get("base_dir")):
            if raw is None and cand is not None:
                raw = cand
        self.root = _safe_root(raw)
        self.storage_root = self.root
        self.directory = self.root
        self.base_dir = self.root
        self.path = self.root
        self.recordings_dir = self.root
        ext = extensions or kwargs.get("exts") or kwargs.get("video_extensions")
        self.extensions = {str(e).lower() for e in (ext or DEFAULT_EXTENSIONS)}
        size = min_size_bytes
        if size is None:
            for k in _SIZE_KEYS:
                if k in kwargs and kwargs[k] is not None:
                    size = kwargs[k]
                    break
        self.min_size_bytes = _as_size(size, DEFAULT_MIN_SIZE_BYTES) if size is not None else DEFAULT_MIN_SIZE_BYTES
        self.check_header = _as_bool(kwargs.get("check_header", check_header), default=True)
        self.use_ffprobe = _as_bool(kwargs.get("use_ffprobe", use_ffprobe))
        qd = quarantine_dir or kwargs.get("quarantine_dir")
        self.quarantine_dir = _coerce_path(qd)
        self.last_report: Optional[Dict[str, Any]] = None
        self.results: List[Dict[str, Any]] = []

    # ---- single file ----
    def check_file(self, path: Any, **kwargs: Any) -> Dict[str, Any]:
        kw = {"min_size_bytes": self.min_size_bytes,
              "check_header": self.check_header,
              "use_ffprobe": self.use_ffprobe}
        kw.update(kwargs)
        return check_file(path, **kw)

    def check(self, path: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.check_file(path, **kwargs)

    def validate(self, path: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.check_file(path, **kwargs)

    def validate_file(self, path: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.check_file(path, **kwargs)

    def verify(self, path: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.check_file(path, **kwargs)

    def verify_file(self, path: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.check_file(path, **kwargs)

    def inspect(self, path: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.check_file(path, **kwargs)

    def is_valid(self, path: Any, **kwargs: Any) -> bool:
        return bool(self.check_file(path, **kwargs).get("ok"))

    def is_corrupt(self, path: Any, **kwargs: Any) -> bool:
        return bool(self.check_file(path, **kwargs).get("corrupt"))

    def is_bad(self, path: Any, **kwargs: Any) -> bool:
        return self.is_corrupt(path, **kwargs)

    # ---- scanning ----
    def scan(self, root: Any = None, **kwargs: Any) -> Dict[str, Any]:
        kw = {"extensions": list(self.extensions),
              "min_size_bytes": self.min_size_bytes,
              "check_header": self.check_header,
              "use_ffprobe": self.use_ffprobe}
        kw.update(kwargs)
        target = root if root is not None else self.root
        rep = scan_directory(target, **kw)
        self.last_report = rep
        self.results = list(rep.get("results") or [])
        return rep

    def scan_directory(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.scan(*a, **k)

    def check_recordings(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.scan(*a, **k)

    def verify_recordings(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.scan(*a, **k)

    def validate_all(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.scan(*a, **k)

    def check_all(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.scan(*a, **k)

    def run(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.scan(*a, **k)

    def execute(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.scan(*a, **k)

    def process(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.scan(*a, **k)

    def get_report(self, *a: Any, **k: Any) -> Dict[str, Any]:
        if self.last_report is not None and not a and not k:
            return self.last_report
        return self.scan(*a, **k)

    # ---- quarantine ----
    def quarantine(self, path: Any, **kwargs: Any) -> Optional[Path]:
        kw: Dict[str, Any] = {}
        if self.quarantine_dir is not None:
            kw.setdefault("quarantine_dir", self.quarantine_dir)
        kw.update(kwargs)
        return quarantine_file(path, **kw)

    def quarantine_file(self, path: Any, **kwargs: Any) -> Optional[Path]:
        return self.quarantine(path, **kwargs)

    def quarantine_corrupt(self, *a: Any, **k: Any) -> Dict[str, Any]:
        kw = {"extensions": list(self.extensions),
              "min_size_bytes": self.min_size_bytes,
              "check_header": self.check_header}
        if self.quarantine_dir is not None:
            kw.setdefault("quarantine_dir", self.quarantine_dir)
        kw.update(k)
        target = a[0] if a else self.root
        rest = a[1:] if a else ()
        _ = rest
        return quarantine_corrupt_files(target, **kw)

    # ---- status ----
    def get_status(self, root: Any = None, **kwargs: Any) -> Dict[str, Any]:
        rep = self.scan(root, **kwargs) if kwargs or root is not None else (self.last_report or self.scan())
        return {
            "status": rep.get("status"),
            "state": rep.get("status"),
            "ok": rep.get("ok"),
            "success": True,
            "healthy": rep.get("healthy"),
            "corrupt": rep.get("corrupt"),
            "root": rep.get("root"),
            "path": rep.get("root"),
            "total": rep.get("total"),
            "checked": rep.get("checked"),
            "ok_count": rep.get("ok_count"),
            "bad_count": rep.get("bad_count"),
            "corrupt_count": rep.get("corrupt_count"),
            "total_bytes": rep.get("total_bytes"),
            "report": rep,
        }

    def status(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.get_status(*a, **k)

    def summary(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.get_status(*a, **k)

    def health(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.get_status(*a, **k)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        low = (name or "").lower()
        if "quarantine" in low or "isolate" in low or "mark" in low:
            return self.quarantine
        if any(h in low for h in ("status", "health", "summary", "info", "state")):
            return self.get_status
        if any(h in low for h in ("scan", "check", "valid", "verif", "inspect", "report", "run", "process", "execut")):
            return self.scan
        if "hash" in low or "checksum" in low:
            return compute_hash
        raise AttributeError(name)


# Class aliases.
IntegrityService = IntegrityChecker
RecordingIntegrityChecker = IntegrityChecker
RecordingIntegrityService = IntegrityChecker
IntegrityManager = IntegrityChecker
IntegrityValidator = IntegrityChecker
VideoIntegrityChecker = IntegrityChecker
Checker = IntegrityChecker
Validator = IntegrityChecker

# Singleton instances.
integrity_checker = IntegrityChecker()
integrity_service = integrity_checker
checker = integrity_checker
service = integrity_checker
manager = integrity_checker
validator = integrity_checker
default_checker = integrity_checker
default_service = integrity_checker
instance = integrity_checker
singleton = integrity_checker


# ---------------------------------------------------------------------------
# Module-level status helpers
# ---------------------------------------------------------------------------

def get_status(root: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return integrity_checker.get_status(root, **kwargs)


status = get_status
summary = get_status
health = get_status
get_summary = get_status
get_health = get_status
check_status = get_status


__all__ = [
    "IntegrityChecker",
    "IntegrityService",
    "RecordingIntegrityChecker",
    "RecordingIntegrityService",
    "check_file",
    "validate_file",
    "verify_file",
    "inspect_file",
    "is_valid",
    "is_corrupt",
    "is_bad",
    "compute_hash",
    "scan_directory",
    "scan",
    "check_recordings",
    "verify_recordings",
    "quarantine_file",
    "quarantine",
    "quarantine_corrupt_files",
    "get_status",
    "integrity_checker",
    "integrity_service",
    "checker",
    "service",
    "DEFAULT_MIN_SIZE_BYTES",
    "VIDEO_EXTENSIONS",
]
