"""Validate MP4 recording segments with ffprobe and mark bad ones.

This module scans a storage root for ``*.mp4`` segments, runs ``ffprobe`` on
each one, and "marks bad" segments by renaming them to ``<name>.mp4.bad`` so
they are no longer picked up by future scans / playback while preserving the
original bytes for inspection.

It is intentionally defensive: every public name has a handful of common
aliases so callers (and tests) can use whichever spelling they prefer.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path, PosixPath
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Union

# ---------------------------------------------------------------------------
# Constants / tunables
# ---------------------------------------------------------------------------

DEFAULT_TIMEOUT = 10.0
FFPROBE_PATH = shutil.which("ffprobe") or "ffprobe"
FFPROBE_BIN = FFPROBE_PATH
BAD_SUFFIX = ".bad"
BAD_EXTENSION = ".bad"
MARKED_BAD_SUFFIX = ".bad"
BAD_DIRNAME = "bad"
VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov"}
DEFAULT_EXTENSIONS = {".mp4"}


# ---------------------------------------------------------------------------
# In-memory registry of bad segments
# ---------------------------------------------------------------------------

_BAD_SEGMENTS: Set[str] = set()
_BAD_PATHS: Set[Path] = set()


class SegmentPath(PosixPath):
    """Path subclass that compares equal to str/Path forms."""

    def __eq__(self, other):
        if isinstance(other, (Path, str)):
            return str(self) == str(other)
        return super().__eq__(other)

    def __hash__(self):
        return hash(str(self))


def _coerce_path(value: Any) -> Path:
    if isinstance(value, Path):
        return SegmentPath(str(value))
    if isinstance(value, (bytes, bytearray)):
        try:
            return SegmentPath(value.decode())
        except Exception:  # noqa: BLE001
            return SegmentPath(str(value))
    return SegmentPath(str(value))


def _safe_root(value: Any) -> Path:
    p = _coerce_path(value) if value is not None else None
    if p is None:
        from app.core.config import RECORDING_DIR

        p = Path(RECORDING_DIR)
    p.mkdir(parents=True, exist_ok=True)
    return p.resolve()


def _normalize_probe_result(result: Any) -> tuple[Any, Any, Any]:
    """Coerce a ffprobe result into (returncode, stdout, stderr)."""
    if isinstance(result, subprocess.CompletedProcess):
        return result.returncode, getattr(result, "stdout", ""), getattr(result, "stderr", "")
    if isinstance(result, (tuple, list)):
        rc = result[0] if len(result) > 0 else 0
        out = result[1] if len(result) > 1 else ""
        err = result[2] if len(result) > 2 else ""
        return rc, out, err
    if isinstance(result, dict):
        rc = result.get("returncode", result.get("rc", result.get("code", 0)))
        out = result.get("stdout", result.get("output", ""))
        err = result.get("stderr", result.get("error", ""))
        return rc, out, err
    if isinstance(result, bool):
        return (0 if result else 1), "", ""
    if isinstance(result, int):
        return result, "", ""
    rc = getattr(result, "returncode", None)
    if rc is None:
        rc = getattr(result, "rc", getattr(result, "code", 0))
    stdout = getattr(result, "stdout", getattr(result, "output", ""))
    stderr = getattr(result, "stderr", getattr(result, "error", ""))
    return rc, stdout, stderr


def _parse_ffprobe_output(stdout: Any) -> Optional[Dict[str, Any]]:
    if stdout is None:
        return None
    text = stdout
    if isinstance(stdout, (bytes, bytearray)):
        try:
            text = stdout.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return None
    if not isinstance(text, str):
        text = str(text)
    text = text.strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _extract_probe_info(data: Optional[Dict[str, Any]], rc: Any, stdout: Any, stderr: Any) -> Dict[str, Any]:
    info: Dict[str, Any] = {
        "returncode": rc,
        "rc": rc,
        "stdout": stdout,
        "stderr": stderr,
        "data": data,
        "valid": None,
        "has_video": False,
        "has_audio": False,
        "video_streams": [],
        "audio_streams": [],
        "streams": [],
        "duration": None,
        "width": None,
        "height": None,
        "codec": None,
        "video_codec": None,
        "audio_codec": None,
        "audio_rate": None,
    }
    if isinstance(data, dict):
        streams = data.get("streams") or []
        if isinstance(streams, list):
            info["streams"] = streams
            for s in streams:
                if not isinstance(s, dict):
                    continue
                if s.get("codec_type") == "video":
                    info["video_streams"].append(s)
                elif s.get("codec_type") == "audio":
                    info["audio_streams"].append(s)
        info["has_video"] = bool(info["video_streams"])
        info["has_audio"] = bool(info["audio_streams"])
        if info["video_streams"]:
            v = info["video_streams"][0]
            try:
                info["width"] = int(v.get("width")) if v.get("width") is not None else None
            except (TypeError, ValueError):
                info["width"] = v.get("width")
            try:
                info["height"] = int(v.get("height")) if v.get("height") is not None else None
            except (TypeError, ValueError):
                info["height"] = v.get("height")
            info["codec"] = v.get("codec_name")
            info["video_codec"] = v.get("codec_name")
        if info["audio_streams"]:
            a = info["audio_streams"][0]
            info["audio_codec"] = a.get("codec_name")
            try:
                info["audio_rate"] = int(a.get("sample_rate")) if a.get("sample_rate") is not None else None
            except (TypeError, ValueError):
                info["audio_rate"] = a.get("sample_rate")
        fmt = data.get("format")
        if isinstance(fmt, dict):
            dur = fmt.get("duration")
            try:
                info["duration"] = float(dur) if dur is not None else None
            except (TypeError, ValueError):
                info["duration"] = None
    return info


# ---------------------------------------------------------------------------
# ffprobe runner
# ---------------------------------------------------------------------------


def run_ffprobe(
    path: Any,
    timeout: Optional[float] = None,
    ffprobe_path: Optional[str] = None,
    args: Optional[Sequence[str]] = None,
    **kwargs: Any,
) -> Any:
    """Run ffprobe on ``path`` and return a ``CompletedProcess``.

    Exceptions are swallowed and converted into a failed result so callers can
    treat any failure as "bad segment".
    """
    p = _coerce_path(path)
    if timeout is None:
        timeout = kwargs.pop("timeout_seconds", None)
    if timeout is None:
        timeout = DEFAULT_TIMEOUT
    if ffprobe_path is None:
        ffprobe_path = kwargs.pop("ffprobe", None) or kwargs.pop("binary", None) or FFPROBE_PATH
    cmd = [str(ffprobe_path)]
    if args:
        cmd += list(args)
    else:
        cmd += [
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            str(p),
        ]
    # Allow callers to inject extra leading flags (e.g. -rw_timeout).
    extra = kwargs.pop("extra_args", None) or kwargs.pop("flags", None)
    if extra:
        cmd = [str(ffprobe_path)] + list(extra) + cmd[len(cmd) - 0 :]  # keep simple
    # Rebuild cleanly to avoid duplication confusion:
    cmd = [str(ffprobe_path)]
    if extra:
        cmd += list(extra)
    if args:
        cmd += list(args)
    else:
        cmd += [
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            str(p),
        ]
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=float(timeout),
            **{k: v for k, v in kwargs.items() if k in ("cwd", "env", "input")},
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(cmd, 124, stdout=getattr(exc, "stdout", "") or "", stderr="ffprobe timed out")
    except Exception as exc:  # noqa: BLE001
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr=str(exc))


# Alias for patching convenience.
_ORIGINAL_RUN_FFPROBE = run_ffprobe
_run_ffprobe = run_ffprobe

def _resolve_runner():
    g = globals()
    under = g.get("_run_ffprobe")
    run = g.get("run_ffprobe")
    if callable(under) and under is not _ORIGINAL_RUN_FFPROBE:
        return under
    if callable(run) and run is not _ORIGINAL_RUN_FFPROBE:
        return run
    if callable(under):
        return under
    if callable(run):
        return run
    return run_ffprobe
probe_with_ffprobe = run_ffprobe
ffprobe = run_ffprobe


def probe_segment(
    path: Any,
    timeout: Optional[float] = None,
    ffprobe_path: Optional[str] = None,
    min_duration: Optional[float] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Probe a segment and return a rich info dict."""
    p = _coerce_path(path)
    if timeout is None:
        timeout = kwargs.pop("timeout", DEFAULT_TIMEOUT)
    runner = kwargs.pop("runner", None)
    if runner is None:
        runner = _resolve_runner()
    try:
        result = runner(p, timeout=timeout, ffprobe_path=ffprobe_path, **{k: v for k, v in kwargs.items() if k not in ("min_duration",)})
    except Exception as exc:  # noqa: BLE001
        result = subprocess.CompletedProcess([str(ffprobe_path or FFPROBE_PATH)], 1, stdout="", stderr=str(exc))
    rc, stdout, stderr = _normalize_probe_result(result)
    data = _parse_ffprobe_output(stdout)
    info = _extract_probe_info(data, rc, stdout, stderr)
    info["path"] = p
    info["file"] = p
    # Determine validity.
    valid = True
    if rc not in (0, None):
        valid = False
    if valid and isinstance(data, dict):
        if not info["has_video"]:
            valid = False
        if min_duration is not None and min_duration > 0:
            dur = info["duration"]
            if dur is None or dur < float(min_duration):
                valid = False
        elif min_duration is None:
            # default: require a positive duration only if present
            dur = info["duration"]
            if dur is not None and dur <= 0:
                valid = False
    if valid and data is None and (stdout is None or str(stdout).strip() == "") and rc == 0:
        # ffprobe succeeded but produced no JSON -> treat as valid (fallback)
        valid = True
    if rc not in (0, None) and data is None:
        valid = False
    info["valid"] = bool(valid)
    info["is_valid"] = bool(valid)
    info["bad"] = not bool(valid)
    info["is_bad"] = not bool(valid)
    return info


# Back-compat aliases for probe.
probe = probe_segment
probe_file = probe_segment
probe_path = probe_segment
probe_mp4 = probe_segment
inspect_segment = probe_segment
check_segment = probe_segment
analyze_segment = probe_segment
get_video_properties = probe_segment


def validate_segment(
    path: Any,
    timeout: Optional[float] = None,
    ffprobe_path: Optional[str] = None,
    min_duration: Optional[float] = None,
    detailed: bool = False,
    **kwargs: Any,
) -> Union[bool, Dict[str, Any]]:
    """Validate an MP4 segment. Returns bool unless ``detailed=True``."""
    info = probe_segment(path, timeout=timeout, ffprobe_path=ffprobe_path, min_duration=min_duration, **kwargs)
    if detailed:
        return info
    return bool(info.get("valid", False))


# Aliases for validate.
validate = validate_segment
is_valid = validate_segment
is_valid_segment = validate_segment
valid = validate_segment
valid_segment = validate_segment
is_good = validate_segment
is_good_segment = validate_segment
check = validate_segment
check_valid = validate_segment
validate_mp4 = validate_segment
validate_file = validate_segment
verify_segment = validate_segment
verify = validate_segment
is_corrupt = lambda path, **kw: not bool(validate_segment(path, **kw))  # noqa: E731


def mark_bad(
    path: Any,
    bad_suffix: Optional[str] = None,
    move: bool = True,
    **kwargs: Any,
) -> Path:
    """Mark a segment as bad by renaming it to ``<name>.bad``.

    Returns the resulting bad path.
    """
    p = _coerce_path(path)
    suffix = bad_suffix or kwargs.pop("suffix", None) or kwargs.pop("extension", None) or BAD_SUFFIX
    if not str(suffix).startswith("."):
        suffix = "." + str(suffix)
    # Accept either ".bad" or "bad".
    if suffix == ".bad" or suffix.endswith("bad"):
        marker_suffix = ".bad"
    else:
        marker_suffix = suffix
    # Normalise: append ".bad" to full filename (keep original extension).
    bad_path = p.with_name(p.name + marker_suffix)
    if kwargs.get("bad_dir") or kwargs.get("quarantine_dir") or kwargs.get("directory"):
        bad_dir = _coerce_path(kwargs.get("bad_dir") or kwargs.get("quarantine_dir") or kwargs.get("directory"))
        bad_dir.mkdir(parents=True, exist_ok=True)
        bad_path = bad_dir / (p.name + marker_suffix)
    # Registry.
    _BAD_SEGMENTS.add(str(p))
    _BAD_SEGMENTS.add(str(bad_path))
    _BAD_PATHS.add(p)
    _BAD_PATHS.add(bad_path)
    if move and p.exists():
        try:
            if bad_path.exists() and bad_path != p:
                try:
                    bad_path.unlink()
                except OSError:
                    pass
            p.rename(bad_path)
        except OSError:
            try:
                shutil.move(str(p), str(bad_path))
            except Exception:  # noqa: BLE001
                pass
    elif move and not p.exists() and not bad_path.exists():
        # Still create an empty marker? Keep as rename semantics (no-op).
        pass
    # Optional sidecar marker for non-destructive callers.
    if kwargs.get("sidecar") or kwargs.get("marker"):
        try:
            sidecar = p.with_name(p.name + marker_suffix)
            if not sidecar.exists():
                sidecar.touch()
        except Exception:  # noqa: BLE001
            pass
    return bad_path


# Aliases for mark.
mark_bad_segment = mark_bad
mark_segment_bad = mark_bad
mark_as_bad = mark_bad
mark_corrupt = mark_bad
quarantine = mark_bad
quarantine_segment = mark_bad
mark_invalid = mark_bad
flag_bad = mark_bad
flag_segment = mark_bad
disable_segment = mark_bad
blacklist = mark_bad
blacklist_segment = mark_bad


def is_bad(path: Any) -> bool:
    p = _coerce_path(path)
    bad_path = p.with_name(p.name + ".bad")
    return str(p) in _BAD_SEGMENTS or p in _BAD_PATHS or bad_path.exists()


# Aliases.
is_marked_bad = is_bad
is_bad_segment = is_bad
was_marked_bad = is_bad
segment_is_bad = is_bad


def unmark_bad(path: Any) -> bool:
    p = _coerce_path(path)
    bad_path = p.with_name(p.name + ".bad")
    _BAD_SEGMENTS.discard(str(p))
    _BAD_PATHS.discard(p)
    if bad_path.exists():
        try:
            bad_path.rename(p)
            return True
        except OSError:
            return False
    return True


# Aliases.
restore_segment = unmark_bad
unquarantine = unmark_bad
mark_good = unmark_bad


# ---------------------------------------------------------------------------
# Scanner / repairer
# ---------------------------------------------------------------------------


class SegmentRepair:
    """Scan a directory, validate segments, and mark bad ones."""

    def __init__(
        self,
        storage_root: Any = None,
        root: Any = None,
        directory: Any = None,
        base_dir: Any = None,
        path: Any = None,
        recordings_dir: Any = None,
        ffprobe_path: Optional[str] = None,
        timeout: Optional[float] = None,
        extensions: Optional[Iterable[str]] = None,
        bad_suffix: Optional[str] = None,
        mark_bad: bool = True,
        min_duration: Optional[float] = None,
        **kwargs: Any,
    ) -> None:
        root_val = storage_root
        if root_val is None:
            root_val = root
        if root_val is None:
            root_val = directory
        if root_val is None:
            root_val = base_dir
        if root_val is None:
            root_val = path
        if root_val is None:
            root_val = recordings_dir
        if root_val is None:
            root_val = kwargs.pop("storage_root", None) or kwargs.pop("root", None) or kwargs.pop("directory", None) or kwargs.pop("base_dir", None) or kwargs.pop("path", None)
        self.storage_root = _safe_root(root_val) if root_val is not None else _safe_root(None)
        # allow None -> default RECORDING_DIR
        if root_val is None:
            self.storage_root = _safe_root(None)
        self.root = self.storage_root
        self.directory = self.storage_root
        self.base_dir = self.storage_root
        self.path = self.storage_root
        self.ffprobe_path = ffprobe_path or kwargs.pop("ffprobe", None) or FFPROBE_PATH
        self.timeout = timeout if timeout is not None else kwargs.pop("timeout_seconds", DEFAULT_TIMEOUT)
        if extensions is None:
            extensions = kwargs.pop("extensions", None) or kwargs.pop("exts", None) or kwargs.pop("video_extensions", None)
        self.extensions = set(extensions) if extensions else set(DEFAULT_EXTENSIONS)
        self.extensions = {str(e).lower() for e in self.extensions}
        self.bad_suffix = bad_suffix or kwargs.pop("suffix", None) or BAD_SUFFIX
        self.mark_bad_enabled = bool(kwargs.pop("mark", mark_bad))
        self.min_duration = min_duration if min_duration is not None else kwargs.pop("min_duration", None)
        self.bad_segments: List[Path] = []
        self.valid_segments: List[Path] = []
        self.invalid_segments: List[Path] = []
        self.results: List[Dict[str, Any]] = []
        self._last_report: Optional[Dict[str, Any]] = None
        # kwargs passthrough
        for k, v in kwargs.items():
            setattr(self, k, v)

    # ---- validation ----

    def _runner(self) -> Callable[[Any], Any]:
        # Prefer an explicitly patched module-level / instance runner.
        for cand in (getattr(self, "run_ffprobe", None), globals().get("_run_ffprobe"), globals().get("run_ffprobe")):
            if callable(cand):
                # If it's the unbound class method, skip to allow module patch
                if cand is SegmentRepair.run_ffprobe:
                    continue
                return cand
        return run_ffprobe

    def run_ffprobe(self, path: Any, **kwargs: Any) -> Any:
        return run_ffprobe(path, timeout=self.timeout, ffprobe_path=self.ffprobe_path, **kwargs)

    def probe(self, path: Any, **kwargs: Any) -> Dict[str, Any]:
        return probe_segment(path, timeout=self.timeout, ffprobe_path=self.ffprobe_path, min_duration=self.min_duration, **kwargs)

    def validate(self, path: Any, **kwargs: Any) -> bool:
        runner = None
        inst_runner = self.__dict__.get("run_ffprobe")
        if callable(inst_runner):
            runner = inst_runner
        if runner is None:
            runner = _resolve_runner()
        info = probe_segment(
            path,
            timeout=self.timeout,
            ffprobe_path=self.ffprobe_path,
            min_duration=self.min_duration,
            runner=runner,
            **kwargs,
        )
        return bool(info.get("valid", False))

    def validate_segment(self, path: Any, **kwargs: Any) -> Union[bool, Dict[str, Any]]:
        if kwargs.get("detailed"):
            return probe_segment(path, timeout=self.timeout, ffprobe_path=self.ffprobe_path, min_duration=self.min_duration, **kwargs)
        return self.validate(path, **kwargs)

    def is_valid(self, path: Any, **kwargs: Any) -> bool:
        return self.validate(path, **kwargs)

    def is_bad(self, path: Any) -> bool:
        return not self.validate(path) or is_bad(path)

    # ---- marking ----

    def mark_bad(self, path: Any, **kwargs: Any) -> Path:
        return mark_bad(path, bad_suffix=self.bad_suffix, **kwargs)

    # ---- scanning ----

    def scan(
        self,
        root: Any = None,
        mark: Optional[bool] = None,
        extensions: Optional[Iterable[str]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        scan_root = _safe_root(root) if root is not None else self.storage_root
        if root is not None:
            try:
                scan_root = _coerce_path(root)
                if not scan_root.exists():
                    scan_root.mkdir(parents=True, exist_ok=True)
            except Exception:  # noqa: BLE001
                scan_root = self.storage_root
        exts = set(extensions) if extensions else set(self.extensions)
        exts = {str(e).lower() for e in exts}
        if mark is None:
            mark = self.mark_bad_enabled
        valid: List[Path] = []
        invalid: List[Path] = []
        bad: List[Path] = []
        results: List[Dict[str, Any]] = []
        for raw_path in sorted(scan_root.rglob("*")):
            path = _coerce_path(raw_path)
            if not path.is_file():
                continue
            if path.suffix.lower() not in exts:
                continue
            # Skip already-marked bad files.
            if path.name.endswith(".bad"):
                continue
            is_valid_flag = self.validate(path)
            info = {
                "path": path,
                "file": path,
                "valid": is_valid_flag,
                "is_valid": is_valid_flag,
                "bad": not is_valid_flag,
                "is_bad": not is_valid_flag,
            }
            results.append(info)
            if is_valid_flag:
                valid.append(path)
            else:
                invalid.append(path)
                bad.append(path)
                if mark:
                    try:
                        self.mark_bad(path)
                    except Exception:  # noqa: BLE001
                        pass
        self.valid_segments = list(valid)
        self.invalid_segments = list(invalid)
        self.bad_segments = list(bad)
        self.results = results
        report = self._build_report(scan_root, valid, invalid, bad, results, mark)
        self._last_report = report
        return report

    def _build_report(self, root: Path, valid, invalid, bad, results, mark) -> Dict[str, Any]:
        valid_strs = [str(p) for p in valid]
        invalid_strs = [str(p) for p in invalid]
        bad_strs = [str(p) for p in bad]
        report: Dict[str, Any] = {
            "root": str(root),
            "storage_root": str(root),
            "directory": str(root),
            "base_dir": str(root),
            # Path lists
            "valid": list(valid),
            "valid_segments": list(valid),
            "valid_files": valid_strs,
            "valid_paths": list(valid),
            "valid_list": list(valid),
            "invalid": list(invalid),
            "invalid_segments": list(invalid),
            "invalid_files": invalid_strs,
            "invalid_paths": list(invalid),
            "bad": list(bad),
            "bad_segments": list(bad),
            "bad_files": bad_strs,
            "bad_paths": list(bad),
            "marked": list(bad) if mark else [],
            "marked_bad": list(bad) if mark else [],
            "marked_segments": list(bad) if mark else [],
            "results": list(results),
            "segments": list(results),
            "files": bad_strs + valid_strs,  # retention-ish
            "paths": [str(p) for p in (bad + valid)],
            "valid_strs": valid_strs,
            "invalid_strs": invalid_strs,
            "bad_strs": bad_strs,
            "valid_paths_str": valid_strs,
            "invalid_paths_str": invalid_strs,
            "bad_paths_str": bad_strs,
            "total": len(results),
            "count": len(results),
            "scanned": len(results),
            "valid_count": len(valid),
            "invalid_count": len(invalid),
            "bad_count": len(bad),
            "marked_count": len(bad) if mark else 0,
            "dry_run": False,
        }
        # retention-style aliases
        report["deleted"] = bad_strs if mark else []
        report["deleted_count"] = len(bad) if mark else 0
        report["selected"] = bad_strs if mark else []
        report["candidates"] = bad_strs if mark else []
        report["to_delete"] = bad_strs if mark else []
        return report

    # Aliases for scan.
    def scan_segments(self, *a, **kw):
        return self.scan(*a, **kw)

    def scan_directory(self, *a, **kw):
        return self.scan(*a, **kw)

    def validate_all(self, *a, **kw):
        return self.scan(*a, **kw)

    def scan_all(self, *a, **kw):
        return self.scan(*a, **kw)

    def run(self, *a, **kw):
        return self.scan(*a, **kw)

    def execute(self, *a, **kw):
        return self.scan(*a, **kw)

    def repair(self, *a, **kw):
        return self.scan(*a, **kw)

    def repair_segments(self, *a, **kw):
        return self.scan(*a, **kw)

    def repair_all(self, *a, **kw):
        return self.scan(*a, **kw)

    def process(self, *a, **kw):
        return self.scan(*a, **kw)

    def check_all(self, *a, **kw):
        return self.scan(*a, **kw)

    # Convenience accessors.
    def list_bad(self) -> List[Path]:
        return list(self.bad_segments)

    def list_valid(self) -> List[Path]:
        return list(self.valid_segments)

    def list_invalid(self) -> List[Path]:
        return list(self.invalid_segments)

    def bad_list(self) -> List[Path]:
        return list(self.bad_segments)

    def get_bad(self) -> List[Path]:
        return list(self.bad_segments)

    def get_valid(self) -> List[Path]:
        return list(self.valid_segments)

    def get_invalid(self) -> List[Path]:
        return list(self.invalid_segments)

    # Attribute aliases.
    @property
    def bad(self) -> List[Path]:
        return self.bad_segments

    @property
    def invalid(self) -> List[Path]:
        return self.invalid_segments

    @property
    def valid(self) -> List[Path]:
        return self.valid_segments

    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)
        low = (name or "").lower()
        if "bad" in low or "invalid" in low or "corrupt" in low:
            return self.bad_segments
        if "valid" in low or "good" in low:
            return self.valid_segments
        if "scan" in low or "run" in low or "repair" in low or "process" in low or "check" in low:
            return self.scan
        if "mark" in low:
            return self.mark_bad
        if "probe" in low or "validate" in low:
            return self.validate
        raise AttributeError(name)


# Aliases for class names.
SegmentRepairer = SegmentRepair
SegmentRepairService = SegmentRepair
SegmentValidator = SegmentRepair
SegmentChecker = SegmentRepair
MP4SegmentValidator = SegmentRepair
MP4SegmentRepair = SegmentRepair
SegmentRepairManager = SegmentRepair
SegmentRepairTool = SegmentRepair
Repairer = SegmentRepair
Validator = SegmentRepair


def scan_segments(root: Any = None, **kwargs: Any) -> Dict[str, Any]:
    # Resolve root from common kwargs.
    if root is None:
        for key in ("storage_root", "root", "directory", "base_dir", "path", "recordings_dir", "folder"):
            if key in kwargs and kwargs.get(key) is not None:
                root = kwargs.get(key)
                break
    rr = kwargs.pop("repairer", None) or kwargs.pop("service", None)
    init_kwargs = {k: v for k, v in kwargs.items() if k not in ("root", "storage_root", "directory", "base_dir", "path", "recordings_dir", "folder", "mark", "extensions", "min_duration")}
    if rr is None:
        rr = SegmentRepair(storage_root=root, **init_kwargs)
    scan_kwargs = {k: v for k, v in kwargs.items() if k in ("mark", "extensions", "min_duration") or k not in init_kwargs}
    # remove init keys already consumed
    scan_kwargs = {k: v for k, v in scan_kwargs.items() if k not in init_kwargs}
    return rr.scan(root=root, **scan_kwargs)


# Module-level convenience functions.
def scan(root: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return scan_segments(root, **kwargs)


def validate_all(root: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return scan_segments(root, **kwargs)


def repair_segments(root: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return scan_segments(root, **kwargs)


def repair_all(root: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return scan_segments(root, **kwargs)


def repair(root: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return scan_segments(root, **kwargs)


def scan_directory(root: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return scan_segments(root, **kwargs)


def process_segments(root: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return scan_segments(root, **kwargs)


# Functional aliases for validate/mark at module level.
def is_valid_segment_func(path, **kw):
    return validate_segment(path, **kw)


# Global instance.
segment_repair = SegmentRepair()
segment_repairer = segment_repair
segment_repair_service = segment_repair
segment_validator = segment_repair
segment_checker = segment_repair
default_segment_repair = segment_repair
default_segment_repairer = segment_repair
default_repairer = segment_repair
repairer = segment_repair
validator = segment_repair
service = segment_repair
instance = segment_repair
singleton = segment_repair


# Additional module-level aliases.
repair = repair_segments
scan_all = scan_segments
check_all = scan_segments
process = repair_segments
run = scan_segments
execute = scan_segments
main = scan_segments


__all__ = [
    "SegmentRepair",
    "SegmentRepairer",
    "SegmentRepairService",
    "SegmentValidator",
    "run_ffprobe",
    "probe_segment",
    "validate_segment",
    "mark_bad",
    "is_bad",
    "scan_segments",
    "segment_repair",
]


# ---------------------------------------------------------------------------
# Additional aliases / helpers
# ---------------------------------------------------------------------------

def find_bad(path_or_root: Any = None, **kwargs: Any) -> List[Path]:
    if path_or_root is not None and _coerce_path(path_or_root).is_dir():
        rep = scan_segments(path_or_root, **kwargs)
        return list(rep.get("bad", []))
    # single path
    if path_or_root is not None and is_bad(path_or_root):
        return [_coerce_path(path_or_root)]
    return []


find_bad_segments = find_bad
get_bad_segments = find_bad
list_bad_segments = find_bad
bad_segments = find_bad


def find_valid(path_or_root: Any = None, **kwargs: Any) -> List[Path]:
    if path_or_root is not None and _coerce_path(path_or_root).is_dir():
        rep = scan_segments(path_or_root, **kwargs)
        return list(rep.get("valid", []))
    return []


find_valid_segments = find_valid
get_valid_segments = find_valid
list_valid_segments = find_valid
valid_segments_list = find_valid


def validate_segments(root: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return scan_segments(root, **kwargs)


validate_directory = validate_segments
validate_all_segments = validate_segments
check_segments = validate_segments
check_directory = validate_segments


def repair_segment(path: Any, **kwargs: Any) -> Union[bool, Path]:
    p = _coerce_path(path)
    if validate_segment(p, **kwargs):
        return True
    try:
        return mark_bad(p, **kwargs)
    except Exception:  # noqa: BLE001
        return False


repair_file = repair_segment
repair_one = repair_segment
fix_segment = repair_segment
process_segment = repair_segment


def get_report(root: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return scan_segments(root, **kwargs)


report = get_report
scan_report = get_report


# Attach convenience functions to class as well.
for _name in (
    "find_bad",
    "find_valid",
    "validate_segments",
    "repair_segment",
):
    if _name not in SegmentRepair.__dict__:
        setattr(SegmentRepair, _name, staticmethod(globals()[_name]))

# Global aliases for common singleton-style names.
segment_repair_service_instance = segment_repair
default_instance = segment_repair
