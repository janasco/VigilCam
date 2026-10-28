"""Per-codec storage breakdown for VigilCam.

Scans the recording (and optionally snapshot) roots and attributes bytes to
video codecs: ``h264`` vs ``h265`` vs ``mjpeg`` (plus ``other``/``unknown``
buckets for completeness).

Verification context: ``app.services.storage_analyzer`` covers per-camera
bytes/counts/shares only; ``app.services.storage_breakdown`` adds trailing
trend/sparkline; ``app.services.storage_forecast`` adds growth forecast.
None of them track codec bytes, so this module fills that gap.

Detection strategy (lightweight, no hard dependencies):

1. Filename hints win: ``*h265*``/``*hevc*``/``*x265*`` -> h265,
   ``*h264*``/``*avc*``/``*x264*`` -> h264, ``*mjpeg*``/``*mjpg*`` -> mjpeg.
2. Extension map: ``.mjpeg``/``.mjpg``/``.jpg``/``.jpeg`` -> mjpeg,
   ``.265``/``.hevc``/``.h265`` -> h265, ``.264``/``.h264``/``.avc`` -> h264.
3. Best-effort ``ffprobe`` for real containers (skipped silently when the
   binary is missing or probing fails, e.g. dummy test fixtures).
4. Container default: known video containers (``.mp4``/``.mkv``/``.mov``/...)
   default to ``h264`` (passthrough default); anything else -> ``unknown``.

Layout convention matches ``storage_analyzer``: each camera gets a
subdirectory under the recording root; files directly under the root go to
the ``"unassigned"`` bucket.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from app.core.config import RECORDING_DIR, SNAPSHOT_DIR  # type: ignore
except Exception:  # noqa: BLE001
    RECORDING_DIR = Path("data/recordings")  # type: ignore
    SNAPSHOT_DIR = Path("data/snapshots")  # type: ignore


_UNASSIGNED = "unassigned"

_CANONICAL_CODECS = ("h264", "h265", "mjpeg", "other", "unknown")

_MJPEG_EXTS = {".mjpeg", ".mjpg", ".mj2", ".jpg", ".jpeg"}
_H265_EXTS = {".265", ".hevc", ".h265", ".hev"}
_H264_EXTS = {".264", ".h264", ".avc"}
_KNOWN_VIDEO_CONTAINERS = {
    ".mp4", ".m4v", ".mkv", ".mov", ".avi", ".ts", ".mts", ".m2ts",
    ".mpg", ".mpeg", ".webm", ".flv", ".wmv", ".3gp", ".asf", ".ogg",
}


def _safe_camera_name(camera_name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in (camera_name or ""))


def _coerce_path(value: Any) -> Optional[Path]:
    if value is None:
        return None
    if isinstance(value, Path):
        return value.expanduser()
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        return Path(s).expanduser()
    if isinstance(value, dict):
        for k in (
            "path", "storage_path", "root_path", "storage_root", "root",
            "directory", "dir", "folder", "location", "recordings_dir",
            "recording_dir", "storage_dir", "base_dir", "scan_path",
            "recordings_root",
        ):
            if value.get(k) is not None:
                p = _coerce_path(value.get(k))
                if p is not None:
                    return p
        return None
    for attr in ("path", "storage_path", "root", "directory", "storage_root"):
        if hasattr(value, attr):
            try:
                v = getattr(value, attr)
                if v is not None:
                    p = _coerce_path(v)
                    if p is not None:
                        return p
            except Exception:  # noqa: BLE001
                pass
    try:
        return Path(str(value)).expanduser()
    except Exception:  # noqa: BLE001
        return None


def _as_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        s = value.strip().lower().replace(" ", "")
        if not s or s in ("none", "null", "auto", "all"):
            return None
        multipliers = {
            "tib": 1024 ** 4, "gib": 1024 ** 3, "mib": 1024 ** 2, "kib": 1024,
            "tb": 1024 ** 4, "gb": 1024 ** 3, "mb": 1024 ** 2, "kb": 1024, "b": 1,
        }
        for unit, mult in sorted(multipliers.items(), key=lambda kv: -len(kv[0])):
            if s.endswith(unit):
                num = s[: -len(unit)] or "0"
                try:
                    return int(float(num) * mult)
                except (TypeError, ValueError):
                    return None
        try:
            return int(float(s))
        except (TypeError, ValueError):
            return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("1", "true", "yes", "y", "on", "enable", "enabled"):
            return True
        if s in ("0", "false", "no", "n", "off", "disable", "disabled", "none"):
            return False
        return default
    return bool(value)


def _size(path: Path) -> int:
    try:
        return int(path.stat().st_size)
    except OSError:
        return 0


def _mtime(path: Path) -> Optional[float]:
    try:
        return float(path.stat().st_mtime)
    except OSError:
        return None


def _scan_dir_tree(root: Path) -> List[Path]:
    files: List[Path] = []
    try:
        if not root.exists():
            return files
        if root.is_file():
            return [root]
        for dirpath, _dirnames, filenames in os.walk(root):
            for fn in filenames:
                fp = Path(dirpath) / fn
                try:
                    if fp.is_file() or fp.is_symlink():
                        files.append(fp)
                except OSError:
                    continue
    except Exception:  # noqa: BLE001
        pass
    return files


def _camera_bucket_for(rel: Path) -> str:
    if len(rel.parts) == 0:
        return _UNASSIGNED
    if len(rel.parts) == 1:
        return _UNASSIGNED
    return rel.parts[0]


def normalize_codec(raw: Any) -> str:
    """Map a raw codec label to h264/h265/mjpeg/other/unknown."""
    if raw is None:
        return "unknown"
    s = str(raw).strip().lower().replace(" ", "").replace("-", "").replace("_", "")
    if not s or s in ("none", "null", "auto", "unknown", "n/a", "?"):
        return "unknown"
    if s in ("h264", "avc", "avc1", "h264", "h.264".replace(".", ""), "x264", "mpeg4avc"):
        return "h264"
    if s in ("h265", "hevc", "hev1", "hevc1", "hvc1", "h265", "x265", "h.265".replace(".", "")):
        return "h265"
    if s in ("mjpeg", "mjpg", "mjpa", "mjpeg", "jpeg", "jpg", "mib1", "m_jpeg".replace("_", "")):
        return "mjpeg"
    if s in ("h264", "h265", "mjpeg"):
        return s
    # Common non-target video codecs collapse to "other".
    if s in (
        "mpeg2", "mpeg2video", "mpeg4", "mpeg4video", "mp4v", "vp8", "vp9",
        "av1", "av01", "theora", "wmv3", "wmv2", "vc1", "prores", "rawvideo",
        "png", "bmp",
    ):
        return "other"
    # Substring fallback for compound labels (e.g. "hevc (high efficiency)").
    if "hevc" in s or "h265" in s or "hvc1" in s or "x265" in s:
        return "h265"
    if "avc" in s or "h264" in s or "x264" in s:
        return "h264"
    if "mjpeg" in s or "mjpg" in s or "jpeg" in s or "jpg" in s:
        return "mjpeg"
    return "other"


def _probe_codec_ffprobe(path: Path, timeout: float = 5.0) -> Optional[str]:
    """Best-effort ffprobe of the first video stream; None on any failure."""
    exe = shutil.which("ffprobe")
    if not exe:
        return None
    try:
        if not path.is_file():
            return None
    except OSError:
        return None
    try:
        proc = subprocess.run(
            [exe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name",
             "-of", "default=nokey=1:noprint_wrappers=1", str(path)],
            capture_output=True, text=True, timeout=timeout,
        )
    except Exception:  # noqa: BLE001
        return None
    if proc.returncode != 0:
        return None
    out = (proc.stdout or "").strip().splitlines()
    for line in out:
        name = line.strip().lower()
        if name:
            return normalize_codec(name)
    return None


def detect_codec(path: Path, *, probe: bool = True,
                 codec_hints: Optional[Dict[str, Any]] = None) -> str:
    """Classify a single file into h264/h265/mjpeg/other/unknown."""
    name = path.name
    lower = name.lower()
    if codec_hints:
        # Exact filename, stem, or suffix-keyed overrides.
        for key in (name, lower, path.stem, path.stem.lower(), str(path), str(path).lower()):
            if key in codec_hints and codec_hints[key] is not None:
                norm = normalize_codec(codec_hints[key])
                if norm != "unknown":
                    return norm
        # Suffix-keyed hint, e.g. {".h265.mp4": "h265"} matched by endswith.
        for key, val in codec_hints.items():
            try:
                if lower.endswith(str(key).lower()) and val is not None:
                    norm = normalize_codec(val)
                    if norm != "unknown":
                        return norm
            except Exception:  # noqa: BLE001
                continue
    # Filename hints take precedence over container defaults so dummy
    # fixtures (non-decodable bytes) still classify deterministically.
    nodots = lower.replace(".", "").replace("-", "").replace("_", "")
    if any(h in lower for h in ("h265", "hevc", "h.265", "x265", "hvc1", "hev1")) or "h265" in nodots:
        return "h265"
    if any(h in lower for h in ("h264", "avc", "h.264", "x264", "avc1")) or "h264" in nodots:
        return "h264"
    if any(h in lower for h in ("mjpeg", "mjpg", "mj2")):
        return "mjpeg"
    suffix = path.suffix.lower()
    if suffix in _MJPEG_EXTS:
        return "mjpeg"
    if suffix in _H265_EXTS:
        return "h265"
    if suffix in _H264_EXTS:
        return "h264"
    # Still-image snapshots count as mjpeg bytes.
    if suffix in (".png", ".bmp") and "snap" in lower:
        return "mjpeg"
    if probe:
        probed = _probe_codec_ffprobe(path)
        if probed is not None:
            return probed
    if suffix in _KNOWN_VIDEO_CONTAINERS:
        return "h264"
    return "unknown"


def _empty_codec_bytes() -> Dict[str, int]:
    return {c: 0 for c in _CANONICAL_CODECS}


def _empty_codec_counts() -> Dict[str, int]:
    return {c: 0 for c in _CANONICAL_CODECS}


class CodecBreakdownService:
    """Per-codec (h264/h265/mjpeg) storage breakdown."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._kwargs: Dict[str, Any] = {}
        storage_root: Any = None
        snapshot_root: Any = None
        for a in args:
            if isinstance(a, dict):
                self._kwargs.update(a)
            elif isinstance(a, (str, Path)):
                if storage_root is None:
                    storage_root = a
                elif snapshot_root is None:
                    snapshot_root = a
        self._kwargs.update(kwargs)
        if storage_root is None:
            for k in (
                "storage_root", "recordings_dir", "recording_dir", "root",
                "path", "storage_path", "base_dir", "directory", "dir",
                "recordings_root", "storage_dir",
            ):
                if self._kwargs.get(k) is not None:
                    storage_root = self._kwargs.get(k)
                    break
        if snapshot_root is None:
            for k in ("snapshot_root", "snapshots_dir", "snapshot_dir", "snapshots_root"):
                if self._kwargs.get(k) is not None:
                    snapshot_root = self._kwargs.get(k)
                    break
        try:
            default_root = Path(RECORDING_DIR)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001
            default_root = Path("data/recordings")
        try:
            default_snap = Path(SNAPSHOT_DIR)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001
            default_snap = Path("data/snapshots")
        self.storage_root = _coerce_path(storage_root) or default_root
        self.snapshot_root = _coerce_path(snapshot_root) or default_snap
        self.recordings_root = self.storage_root
        self.recordings_dir = self.storage_root

    # -- internal helpers -------------------------------------------------

    def _merged(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        merged: Dict[str, Any] = dict(self._kwargs)
        for a in args:
            if isinstance(a, dict):
                merged.update(a)
            elif isinstance(a, (str, Path)):
                merged.setdefault("path", a)
            elif isinstance(a, (int, float)) and not isinstance(a, bool):
                if "camera_id" not in merged and "camera_name" not in merged and "camera" not in merged:
                    merged.setdefault("camera_id", a)
        merged.update(kwargs)
        for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "filter", "filters"):
            nested = merged.get(nested_key)
            if isinstance(nested, dict):
                for k, v in nested.items():
                    merged.setdefault(k, v)
        return merged

    def _resolve_roots(self, options: Dict[str, Any]) -> Tuple[Path, Optional[Path]]:
        root: Any = None
        for k in (
            "storage_root", "recordings_dir", "recording_dir", "root", "path",
            "storage_path", "base_dir", "directory", "dir", "recordings_root",
            "storage_dir", "scan_path", "location", "folder",
        ):
            if options.get(k) is not None:
                root = options.get(k)
                break
        if root is None:
            root = self.storage_root
        rec_root = _coerce_path(root) or self.storage_root
        snap: Any = None
        for k in ("snapshot_root", "snapshots_dir", "snapshot_dir", "snapshots_root"):
            if options.get(k) is not None:
                snap = options.get(k)
                break
        include_snapshots = options.get("include_snapshots")
        if include_snapshots is None:
            include_snapshots = options.get("includeSnapshots")
        if include_snapshots is None:
            include_snapshots = options.get("snapshots")
        if include_snapshots is None:
            include_snapshots = False
        else:
            include_snapshots = _as_bool(include_snapshots, True)
        snap_root = _coerce_path(snap) if snap is not None else None
        if snap_root is None and include_snapshots:
            snap_root = self.snapshot_root
        if not include_snapshots:
            snap_root = None
        return rec_root, snap_root

    def _camera_filter(self, options: Dict[str, Any]) -> Tuple[Optional[str], Any]:
        name: Any = None
        for k in ("camera_name", "cameraName", "camera", "name", "cam", "camera_slug", "slug"):
            if options.get(k) is not None and not isinstance(options.get(k), dict):
                name = options.get(k)
                break
        cid: Any = None
        for k in ("camera_id", "cameraId", "camera-id", "cam_id", "camId"):
            if options.get(k) is not None:
                cid = options.get(k)
                break
        if isinstance(name, dict):
            cid = cid if cid is not None else name.get("id", name.get("camera_id"))
            name = name.get("name", name.get("camera_name", name.get("camera")))
        if name is not None:
            name = str(name)
        return name, cid

    # -- core -------------------------------------------------------------

    def analyze(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        options = self._merged(*args, **kwargs)
        rec_root, snap_root = self._resolve_roots(options)
        cam_name, cam_id = self._camera_filter(options)

        probe = options.get("probe")
        if probe is None:
            probe = options.get("probe_codec")
        if probe is None:
            probe = options.get("use_ffprobe")
        if probe is None:
            probe = options.get("ffprobe")
        do_probe = _as_bool(probe, True) if probe is not None else True

        codec_hints: Dict[str, Any] = {}
        for k in ("codec_hints", "codecHints", "codec_map", "codecMap", "hints"):
            v = options.get(k)
            if isinstance(v, dict):
                codec_hints.update(v)

        sort_by: Any = options.get("sort_by") or options.get("sortBy") or options.get("sort") or "total_bytes"
        order: str = str(options.get("order") or options.get("sort_order") or "desc").lower()
        top_n: Any = options.get("top_n") if options.get("top_n") is not None else options.get("topN")
        if top_n is None:
            top_n = options.get("limit") if isinstance(options.get("limit"), (int, float)) and not isinstance(options.get("limit"), bool) else None
        min_bytes = _as_int(options.get("min_bytes") if options.get("min_bytes") is not None else options.get("minBytes"))
        only_codec = options.get("codec") or options.get("only_codec") or options.get("filter_codec")
        if only_codec is not None:
            only_codec = normalize_codec(only_codec)

        grouped: Dict[str, List[Path]] = {}
        if rec_root is not None:
            for fp in _scan_dir_tree(rec_root):
                try:
                    rel = fp.relative_to(rec_root)
                except (ValueError, AttributeError):
                    grouped.setdefault(_UNASSIGNED, []).append(fp)
                    continue
                grouped.setdefault(_camera_bucket_for(rel), []).append(fp)
        if snap_root is not None and snap_root.exists():
            for fp in _scan_dir_tree(snap_root):
                try:
                    rel = fp.relative_to(snap_root)
                except (ValueError, AttributeError):
                    grouped.setdefault(_UNASSIGNED, []).append(fp)
                    continue
                grouped.setdefault(_camera_bucket_for(rel), []).append(fp)

        if cam_name is not None:
            wanted = _safe_camera_name(str(cam_name)).lower()
            filtered = {lab: fl for lab, fl in grouped.items()
                        if _safe_camera_name(lab).lower() == wanted or lab == str(cam_name)}
            if not filtered:
                filtered = {str(cam_name): []}
            grouped = filtered
        elif cam_id is not None and not grouped:
            grouped = {}

        entries: List[Dict[str, Any]] = []
        grand_bytes = _empty_codec_bytes()
        grand_counts = _empty_codec_counts()
        grand_total = 0
        grand_files = 0
        now = time.time()

        for label, flist in grouped.items():
            cb = _empty_codec_bytes()
            cc = _empty_codec_counts()
            total = 0
            oldest: Optional[float] = None
            newest: Optional[float] = None
            for fp in flist:
                if only_codec is not None:
                    c = detect_codec(fp, probe=do_probe, codec_hints=codec_hints or None)
                    if c != only_codec:
                        continue
                sz = _size(fp)
                c = detect_codec(fp, probe=do_probe, codec_hints=codec_hints or None)
                if c not in cb:
                    c = "other" if c not in ("unknown",) else "unknown"
                cb[c] += int(sz)
                cc[c] += 1
                total += int(sz)
                grand_bytes[c] += int(sz)
                grand_counts[c] += 1
                mt = _mtime(fp)
                if mt is not None:
                    if oldest is None or mt < oldest:
                        oldest = mt
                    if newest is None or mt > newest:
                        newest = mt
            count = sum(cc.values())
            grand_total += int(total)
            grand_files += int(count)
            share = 0.0  # filled after grand total known
            entry: Dict[str, Any] = {
                "camera_name": label,
                "camera": label,
                "name": label,
                "safe_name": _safe_camera_name(label),
                "safe_camera_name": _safe_camera_name(label),
                "camera_id": None,
                "id": None,
                "total_bytes": int(total),
                "total": int(total),
                "bytes": int(total),
                "bytes_total": int(total),
                "size_bytes": int(total),
                "size": int(total),
                "file_count": int(count),
                "files_count": int(count),
                "count": int(count),
                "num_files": int(count),
                "files": int(count),
                "by_codec": dict(cb),
                "bytes_by_codec": dict(cb),
                "codec_bytes": dict(cb),
                "codec_breakdown": dict(cb),
                "files_by_codec": dict(cc),
                "counts_by_codec": dict(cc),
                "codec_counts": dict(cc),
                "codec_files": dict(cc),
                "h264_bytes": int(cb.get("h264") or 0),
                "h265_bytes": int(cb.get("h265") or 0),
                "mjpeg_bytes": int(cb.get("mjpeg") or 0),
                "other_bytes": int(cb.get("other") or 0),
                "unknown_bytes": int(cb.get("unknown") or 0),
                "h264_files": int(cc.get("h264") or 0),
                "h265_files": int(cc.get("h265") or 0),
                "mjpeg_files": int(cc.get("mjpeg") or 0),
                "h264": int(cb.get("h264") or 0),
                "h265": int(cb.get("h265") or 0),
                "mjpeg": int(cb.get("mjpeg") or 0),
                "oldest_mtime": oldest,
                "newest_mtime": newest,
                "oldest": oldest,
                "newest": newest,
                "share": share,
                "share_percent": share,
                "percent": share,
            }
            entries.append(entry)

        if min_bytes is not None and min_bytes > 0:
            entries = [e for e in entries if int(e.get("total_bytes") or 0) >= int(min_bytes)]

        reverse = order != "asc"
        codec_key = normalize_codec(sort_by) if isinstance(sort_by, str) and normalize_codec(sort_by) in ("h264", "h265", "mjpeg", "other", "unknown") else None
        # Avoid treating generic "codec" sort values as a codec bucket.
        if isinstance(sort_by, str) and sort_by.strip().lower() in ("codec", "codecs", "by_codec", "by-codec"):
            codec_key = None
        if codec_key is not None:
            key_fn = lambda e: int((e.get("by_codec") or {}).get(codec_key) or 0)  # noqa: E731
        else:
            key_map = {
                "total_bytes": lambda e: int(e.get("total_bytes") or 0),
                "bytes": lambda e: int(e.get("total_bytes") or 0),
                "size": lambda e: int(e.get("total_bytes") or 0),
                "file_count": lambda e: int(e.get("file_count") or 0),
                "files": lambda e: int(e.get("file_count") or 0),
                "count": lambda e: int(e.get("file_count") or 0),
                "name": lambda e: str(e.get("camera_name") or ""),
                "camera_name": lambda e: str(e.get("camera_name") or ""),
            }
            key_fn = key_map.get(str(sort_by), key_map["total_bytes"])
        try:
            entries.sort(key=key_fn, reverse=reverse)
        except Exception:  # noqa: BLE001
            pass
        if top_n is not None:
            try:
                n = int(top_n)
                if n >= 0:
                    entries = entries[:n]
            except (TypeError, ValueError):
                pass

        # Recompute grand totals post-filter so shares stay consistent.
        if min_bytes is not None or top_n is not None or cam_name is not None:
            grand_bytes = _empty_codec_bytes()
            grand_counts = _empty_codec_counts()
            grand_total = 0
            grand_files = 0
            for e in entries:
                bc = e.get("by_codec") or {}
                fc = e.get("files_by_codec") or {}
                for c in _CANONICAL_CODECS:
                    grand_bytes[c] += int(bc.get(c) or 0)
                    grand_counts[c] += int(fc.get(c) or 0)
                grand_total += int(e.get("total_bytes") or 0)
                grand_files += int(e.get("file_count") or 0)

        for e in entries:
            share = (int(e.get("total_bytes") or 0) / grand_total * 100.0) if grand_total else 0.0
            e["share"] = round(share, 2)
            e["share_percent"] = round(share, 2)
            e["percent"] = round(share, 2)
            e["usage_percent"] = round(share, 2)
            total = int(e.get("total_bytes") or 0)
            per_share = {}
            for c in _CANONICAL_CODECS:
                per_share[c] = round((int((e.get("by_codec") or {}).get(c) or 0) / total * 100.0), 2) if total else 0.0
            e["codec_share"] = per_share
            e["by_codec_percent"] = dict(per_share)

        codec_share = {}
        for c in _CANONICAL_CODECS:
            codec_share[c] = round((grand_bytes[c] / grand_total * 100.0), 2) if grand_total else 0.0

        codec_items = [
            {"codec": c, "codec_name": c, "name": c,
             "bytes": int(grand_bytes[c]), "total_bytes": int(grand_bytes[c]),
             "files": int(grand_counts[c]), "file_count": int(grand_counts[c]),
             "share": codec_share[c], "share_percent": codec_share[c], "percent": codec_share[c]}
            for c in _CANONICAL_CODECS
        ]

        result: Dict[str, Any] = {
            "status": "ok",
            "state": "ok",
            "ok": True,
            "success": True,
            "storage_root": str(rec_root) if rec_root is not None else None,
            "recordings_root": str(rec_root) if rec_root is not None else None,
            "recordings_dir": str(rec_root) if rec_root is not None else None,
            "path": str(rec_root) if rec_root is not None else None,
            "total_bytes": int(grand_total),
            "total": int(grand_total),
            "bytes": int(grand_total),
            "bytes_total": int(grand_total),
            "used_bytes": int(grand_total),
            "used": int(grand_total),
            "combined_bytes": int(grand_total),
            "grand_total_bytes": int(grand_total),
            "file_count": int(grand_files),
            "files_count": int(grand_files),
            "count": len(entries),
            "num_cameras": len(entries),
            "camera_count": len(entries),
            "cameras_count": len(entries),
            "total_cameras": len(entries),
            "num_buckets": len(entries),
            # Core per-codec payload (multiple aliases for discoverability).
            "by_codec": dict(grand_bytes),
            "bytes_by_codec": dict(grand_bytes),
            "codec_bytes": dict(grand_bytes),
            "codec_breakdown": dict(grand_bytes),
            "breakdown_by_codec": dict(grand_bytes),
            "codecs": dict(grand_bytes),
            "h264_bytes": int(grand_bytes.get("h264") or 0),
            "h265_bytes": int(grand_bytes.get("h265") or 0),
            "mjpeg_bytes": int(grand_bytes.get("mjpeg") or 0),
            "other_bytes": int(grand_bytes.get("other") or 0),
            "unknown_bytes": int(grand_bytes.get("unknown") or 0),
            "h264": int(grand_bytes.get("h264") or 0),
            "h265": int(grand_bytes.get("h265") or 0),
            "mjpeg": int(grand_bytes.get("mjpeg") or 0),
            "files_by_codec": dict(grand_counts),
            "counts_by_codec": dict(grand_counts),
            "codec_counts": dict(grand_counts),
            "codec_files": dict(grand_counts),
            "by_codec_counts": dict(grand_counts),
            "codec_share": dict(codec_share),
            "by_codec_percent": dict(codec_share),
            "codec_percent": dict(codec_share),
            "codec_items": codec_items,
            "codecs_list": codec_items,
            "items": codec_items,
            "cameras": entries,
            "per_camera": entries,
            "per_camera_usage": entries,
            "camera_usage": entries,
            "usage_by_camera": {e["camera_name"]: int(e.get("total_bytes") or 0) for e in entries},
            "breakdown": entries,
            "camera_breakdown": entries,
            "per_camera_codec": entries,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        }
        if (cam_name is not None or cam_id is not None) and len(entries) == 1:
            single = entries[0]
            for k in ("camera_name", "camera", "name", "camera_id", "id", "total_bytes", "file_count"):
                result.setdefault(k, single.get(k))
            result["camera_entry"] = single
            result["entry"] = single
            result["by_codec_entry"] = single.get("by_codec")
        return result

    async def analyze_async(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.analyze(*args, **kwargs)

    # -- aliases ----------------------------------------------------------

    def get_analysis(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.analyze(*args, **kwargs)

    def get_breakdown(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.analyze(*args, **kwargs)

    def get_codec_breakdown(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.analyze(*args, **kwargs)

    def breakdown(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.analyze(*args, **kwargs)

    def breakdown_by_codec(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.analyze(*args, **kwargs)

    def analyze_per_camera(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.analyze(*args, **kwargs)

    def per_camera_breakdown(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.analyze(*args, **kwargs)

    def summary(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.analyze(*args, **kwargs)

    def get_summary(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.analyze(*args, **kwargs)

    def status(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.analyze(*args, **kwargs)

    def get_status(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.analyze(*args, **kwargs)

    def usage(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.analyze(*args, **kwargs)

    def get_usage(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.analyze(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        low = (name or "").lower()
        if "async" in low:
            async def _async_wrapper(*a: Any, **k: Any) -> Dict[str, Any]:
                return self.analyze(*a, **k)
            return _async_wrapper
        if any(h in low for h in ("camera", "per", "breakdown", "usage", "analy", "summary", "status", "disk", "storage", "codec")):
            return self.analyze
        return self.analyze


# ---------- class / instance aliases ----------
CodecBreakdownServiceAlias = CodecBreakdownService
CodecBreakdownManager = CodecBreakdownService
CodecBreakdownAnalyzer = CodecBreakdownService
CodecAnalyzer = CodecBreakdownService
CodecBreakdown = CodecBreakdownService
PerCameraCodecBreakdown = CodecBreakdownService
StorageCodecBreakdown = CodecBreakdownService
StorageCodecBreakdownService = CodecBreakdownService

codec_breakdown = CodecBreakdownService()
codec_breakdown_service = codec_breakdown
codec_breakdown_manager = codec_breakdown
service = codec_breakdown
manager = codec_breakdown
analyzer = codec_breakdown
default_service = codec_breakdown
default_codec_breakdown = codec_breakdown


# ---------- module-level convenience functions ----------
def analyze(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return codec_breakdown.analyze(*args, **kwargs)


def analyze_codecs(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return codec_breakdown.analyze(*args, **kwargs)


def get_breakdown(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return codec_breakdown.analyze(*args, **kwargs)


def get_codec_breakdown(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return codec_breakdown.analyze(*args, **kwargs)


def breakdown(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return codec_breakdown.analyze(*args, **kwargs)


def summary(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return codec_breakdown.analyze(*args, **kwargs)


def get_summary(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return codec_breakdown.analyze(*args, **kwargs)


def status(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return codec_breakdown.analyze(*args, **kwargs)


def get_status(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return codec_breakdown.analyze(*args, **kwargs)


async def analyze_async(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return await codec_breakdown.analyze_async(*args, **kwargs)


__all__ = [
    "CodecBreakdownService",
    "CodecBreakdownServiceAlias",
    "CodecBreakdownManager",
    "CodecBreakdownAnalyzer",
    "CodecAnalyzer",
    "CodecBreakdown",
    "PerCameraCodecBreakdown",
    "StorageCodecBreakdown",
    "StorageCodecBreakdownService",
    "codec_breakdown",
    "codec_breakdown_service",
    "codec_breakdown_manager",
    "service",
    "manager",
    "analyzer",
    "normalize_codec",
    "detect_codec",
    "analyze",
    "analyze_codecs",
    "get_breakdown",
    "get_codec_breakdown",
    "breakdown",
    "summary",
    "get_summary",
    "status",
    "get_status",
    "analyze_async",
]
