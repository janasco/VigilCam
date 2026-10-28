"""Manual storage area-to-area migration service for VigilCam.

Complements the policy-driven auto-tiering in :mod:`app.services.tiering`
(which moves old segments from hot storage to an archive area by
``max_age_days`` / ``max_total_bytes`` policy only). This module performs
*explicit manual* migration of files from one storage area (or directory)
to another, preserving relative paths::

    <source>/<rel> -> <destination>/<rel>

Features:

* dry-run preview (report what *would* move without moving anything)
* move (default) or copy (``copy=True`` / ``mode="copy"``) semantics
* overwrite control (default: rename on collision with ``_1`` suffix)
* optional extension / pattern / camera-bucket filters
* optional ``max_files`` cap and non-recursive mode

Filesystem-only (no DB schema change) so no migrations are required.
Auto-registered API: see ``app.api.storage_migrate`` (no ``app/main.py``
edit needed).
"""

from __future__ import annotations

import fnmatch
import os
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from app.core.config import BASE_DIR, DATA_DIR, RECORDING_DIR  # type: ignore
except Exception:  # noqa: BLE001
    BASE_DIR = Path(".")
    DATA_DIR = Path("data")
    RECORDING_DIR = Path("data/recordings")


_SOURCE_KEYS = (
    "source_root", "source_dir", "source_path", "source", "src",
    "src_root", "src_dir", "src_path",
    "from_root", "from_dir", "from_path", "from", "from_location",
    "source_location", "source_folder", "source_directory",
    "source_storage", "source_area_path", "origin", "origin_path",
    "origin_dir", "origin_root", "source_area", "source_storage_path",
    "src_location", "src_folder",
)
_DEST_KEYS = (
    "dest_root", "dest_dir", "dest_path", "destination", "dst",
    "dst_root", "dst_dir", "dst_path",
    "to_root", "to_dir", "to_path", "to", "to_location",
    "target", "target_root", "target_dir", "target_path",
    "target_location", "destination_dir", "destination_path",
    "destination_root", "dest_location", "dest_folder",
    "dest_directory", "destination_folder", "dest_storage",
    "dest_area_path", "target_folder", "target_directory",
)
_SOURCE_ID_KEYS = (
    "source_area_id", "source_id", "src_id", "src_area_id",
    "from_id", "from_area_id", "source_areaid", "origin_id",
    "origin_area_id", "sourcearea_id",
)
_DEST_ID_KEYS = (
    "dest_area_id", "dest_id", "dst_id", "dst_area_id",
    "to_id", "to_area_id", "target_id", "target_area_id",
    "destination_id", "destination_area_id", "dest_areaid",
)
_GENERIC_ID_KEYS = ("area_id", "storage_area_id", "area", "storage_area")
_DRY_KEYS = ("dry_run", "dryRun", "dry-run", "dry", "simulate", "preview", "preview_only", "what_if")
_COPY_KEYS = ("copy", "copy_files", "copyFiles", "keep_source", "keepSource", "keep", "preserve_source")
_MODE_KEYS = ("mode", "operation", "op", "action", "how")
_OVERWRITE_KEYS = ("overwrite", "over_write", "clobber", "replace", "force_overwrite", "allow_overwrite")
_EXT_KEYS = ("extensions", "suffixes", "video_extensions", "include_extensions", "patterns_ext", "extension", "exts", "ext")
_PATTERN_KEYS = ("patterns", "pattern", "include", "includes", "filter", "filters", "glob", "globs", "match", "matches", "name_pattern")
_CAMERA_NAME_KEYS = ("camera_name", "cameraName", "camera", "cam", "camera_slug", "slug")
_CAMERA_ID_KEYS = ("camera_id", "cameraId", "camera-id", "cam_id", "camId")
_LIMIT_KEYS = ("max_files", "maxFiles", "limit", "count", "top_n", "first_n", "max_count")
_RECURSIVE_KEYS = ("recursive", "recurse", "subdirectories", "walk")


def _safe_camera_name(camera_name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in (camera_name or ""))


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


def _as_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    if isinstance(value, str):
        s = value.strip().lower().replace(" ", "")
        if not s or s in ("none", "null", "auto", "all", "disabled", "off"):
            return None
        try:
            return int(float(s))
        except (TypeError, ValueError):
            return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


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
        for k in _SOURCE_KEYS + _DEST_KEYS + ("path", "storage_path", "root", "directory"):
            if value.get(k) is not None:
                p = _coerce_path(value.get(k))
                if p is not None:
                    return p
        return None
    for attr in ("path", "storage_path", "storage_root", "root", "directory", "archive_path"):
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
        s = str(value).strip()
        if not s:
            return None
        return Path(s).expanduser()
    except Exception:  # noqa: BLE001
        return None


def _first_present(data: Dict[str, Any], keys: Tuple[str, ...], default: Any = None) -> Any:
    for k in keys:
        if k in data and data.get(k) is not None:
            return data.get(k)
    return default


def _normalize_extensions(value: Any) -> Optional[set]:
    if value is None:
        return None
    items: List[Any]
    if isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = [value]
    out: set = set()
    for item in items:
        if item is None:
            continue
        s = str(item).strip().lower()
        if not s or s in ("none", "null", "auto", "all"):
            continue
        for part in s.replace(",", " ").split():
            part = part.strip()
            if not part:
                continue
            if part == "*":
                continue
            if not part.startswith("."):
                # Allow bare "mp4" or "*.mp4".
                part = part.lstrip("*")
                if not part.startswith("."):
                    part = "." + part
            out.add(part)
    return out or None


def _normalize_patterns(value: Any) -> Optional[List[str]]:
    if value is None:
        return None
    if isinstance(value, str):
        parts = [p.strip() for p in value.replace(",", " ").split() if p.strip()]
        return parts or None
    if isinstance(value, (list, tuple, set)):
        out = [str(v).strip() for v in value if str(v).strip()]
        return out or None
    s = str(value).strip()
    return [s] if s else None


def _size(path: Path) -> int:
    try:
        return int(path.stat().st_size)
    except OSError:
        return 0


def _mtime(path: Path) -> float:
    try:
        return float(path.stat().st_mtime)
    except OSError:
        return time.time()


def _matches_patterns(rel_posix: str, name: str, patterns: Optional[List[str]]) -> bool:
    if not patterns:
        return True
    for pat in patterns:
        p = pat.strip()
        if not p:
            continue
        if any(ch in p for ch in ("*", "?", "[")):
            if fnmatch.fnmatch(name, p) or fnmatch.fnmatch(rel_posix, p):
                return True
        else:
            # Substring match against name or relative path.
            if p.lower() in name.lower() or p.lower() in rel_posix.lower():
                return True
    return False


class StorageMigrateManager:
    """Manual area-to-area storage migration (move or copy)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        merged: Dict[str, Any] = {}
        pos_paths: List[Any] = []
        for a in args:
            if isinstance(a, dict):
                merged.update(a)
            elif isinstance(a, (str, Path)):
                pos_paths.append(a)
            elif isinstance(a, bool):
                merged.setdefault("dry_run", a)
        merged.update(kwargs)
        for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "filter", "filters"):
            nested = merged.get(nested_key)
            if isinstance(nested, dict):
                for k, v in nested.items():
                    merged.setdefault(k, v)
        self._kwargs = merged
        self._pos_paths = pos_paths
        # Resolve defaults from positionals: first -> source, second -> dest.
        raw_source = _first_present(merged, _SOURCE_KEYS, None)
        if raw_source is None and len(pos_paths) > 0:
            raw_source = pos_paths[0]
        raw_dest = _first_present(merged, _DEST_KEYS, None)
        if raw_dest is None and len(pos_paths) > 1:
            raw_dest = pos_paths[1]
        self.source_root: Optional[Path] = _coerce_path(raw_source)
        self.dest_root: Optional[Path] = _coerce_path(raw_dest)

    # ---------- options ----------

    def _merged_options(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        merged: Dict[str, Any] = dict(self._kwargs)
        pos: List[Any] = []
        for a in args:
            if isinstance(a, dict):
                merged.update(a)
            elif isinstance(a, (str, Path)):
                pos.append(a)
            elif isinstance(a, bool):
                merged.setdefault("dry_run", a)
        merged.update(kwargs)
        for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "filter", "filters"):
            nested = merged.get(nested_key)
            if isinstance(nested, dict):
                for k, v in nested.items():
                    merged.setdefault(k, v)
        if pos:
            if _first_present(merged, _SOURCE_KEYS, None) is None:
                merged["_pos_source"] = pos[0]
            if len(pos) > 1 and _first_present(merged, _DEST_KEYS, None) is None:
                merged["_pos_dest"] = pos[1]
        return merged

    def _effective(self, options: Dict[str, Any]) -> Dict[str, Any]:
        raw_source = _first_present(options, _SOURCE_KEYS, None)
        if raw_source is None:
            raw_source = options.get("_pos_source")
        if raw_source is None:
            raw_source = options.get("_pos_path")
        if raw_source is None:
            # Generic single-path fallback ("path" means source when dest given separately).
            if options.get("path") is not None and _first_present(options, _DEST_KEYS, None) is not None:
                raw_source = options.get("path")
        source = _coerce_path(raw_source) or self.source_root

        raw_dest = _first_present(options, _DEST_KEYS, None)
        if raw_dest is None:
            raw_dest = options.get("_pos_dest")
        dest = _coerce_path(raw_dest) or self.dest_root

        dry = False
        if _first_present(options, _DRY_KEYS, None) is not None:
            dry = bool(_first_present(options, _DRY_KEYS))

        # Copy vs move.
        copy = False
        if _first_present(options, _COPY_KEYS, None) is not None:
            copy = bool(_first_present(options, _COPY_KEYS))
        mode_raw = _first_present(options, _MODE_KEYS, None)
        mode = str(mode_raw).strip().lower() if mode_raw is not None else ("copy" if copy else "move")
        if mode_raw is not None:
            if mode in ("copy", "cp", "duplicate"):
                copy = True
                mode = "copy"
            else:
                copy = False
                mode = "move"
        else:
            mode = "copy" if copy else "move"

        overwrite = False
        if _first_present(options, _OVERWRITE_KEYS, None) is not None:
            overwrite = _as_bool(_first_present(options, _OVERWRITE_KEYS), default=False)

        raw_ext = _first_present(options, _EXT_KEYS, None)
        exts = _normalize_extensions(raw_ext)

        raw_pat = _first_present(options, _PATTERN_KEYS, None)
        patterns = _normalize_patterns(raw_pat)

        cam_name: Any = None
        for k in _CAMERA_NAME_KEYS:
            v = options.get(k)
            if v is not None and not isinstance(v, dict):
                if k == "name" and _coerce_path(v) is not None and Path(str(v)).exists():
                    continue
                cam_name = str(v)
                break
        cam_id: Any = None
        for k in _CAMERA_ID_KEYS:
            if options.get(k) is not None:
                cam_id = options.get(k)
                break
        if cam_id is not None and cam_name is None:
            cam_name = str(cam_id)

        raw_limit = _first_present(options, _LIMIT_KEYS, None)
        max_files = _as_int(raw_limit)
        if max_files is not None and max_files < 0:
            max_files = 0

        recursive = True
        if _first_present(options, _RECURSIVE_KEYS, None) is not None:
            recursive = _as_bool(_first_present(options, _RECURSIVE_KEYS), default=True)

        return {
            "source_root": source,
            "dest_root": dest,
            "dry_run": dry,
            "copy": copy,
            "mode": mode,
            "overwrite": overwrite,
            "extensions": exts,
            "patterns": patterns,
            "camera_name": cam_name,
            "camera_id": cam_id,
            "max_files": max_files,
            "recursive": recursive,
            "options": options,
        }

    # ---------- internals ----------

    def _is_under(self, path: Path, root: Optional[Path]) -> bool:
        if root is None:
            return False
        try:
            path.resolve().relative_to(root.resolve())
            return True
        except Exception:  # noqa: BLE001
            try:
                return str(path).startswith(str(root))
            except Exception:  # noqa: BLE001
                return False

    def _collect_files(self, _eff: Dict[str, Any]) -> List[Path]:
        source: Optional[Path] = _eff["source_root"]
        dest: Optional[Path] = _eff["dest_root"]
        exts: Optional[set] = _eff.get("extensions")
        patterns: Optional[List[str]] = _eff.get("patterns")
        cam_name = _eff.get("camera_name")
        recursive = bool(_eff.get("recursive", True))
        if source is None:
            return []
        try:
            if not source.exists():
                return []
            if source.is_file():
                candidates = [source]
            elif recursive:
                candidates = []
                for root, _dirs, files in os.walk(source):
                    for fn in files:
                        fp = Path(root) / fn
                        try:
                            if fp.is_file() or fp.is_symlink():
                                candidates.append(fp)
                        except OSError:
                            continue
            else:
                candidates = []
                try:
                    for child in source.iterdir():
                        try:
                            if child.is_file() or child.is_symlink():
                                candidates.append(child)
                        except OSError:
                            continue
                except OSError:
                    return []
        except Exception:  # noqa: BLE001
            return []
        out: List[Path] = []
        wanted = _safe_camera_name(str(cam_name)).lower() if cam_name is not None else None
        for fp in candidates:
            # Exclude anything already under the destination area (avoid loops).
            if dest is not None and self._is_under(fp, dest):
                # Only skip when source != dest file itself; a single-file source
                # equal to dest child is still under dest but should be skipped
                # to avoid self-migration.
                try:
                    if fp.resolve() != dest.resolve():
                        # If dest is inside source, files under dest are not part of source scan.
                        if dest.resolve().relative_to(source.resolve()) == dest.resolve().relative_to(source.resolve()):
                            continue
                except Exception:  # noqa: BLE001
                    continue
            try:
                if exts is not None and fp.suffix.lower() not in exts:
                    continue
            except Exception:  # noqa: BLE001
                pass
            try:
                if source.is_file():
                    rel_posix = fp.name
                else:
                    rel_posix = fp.relative_to(source).as_posix()
            except Exception:  # noqa: BLE001
                rel_posix = fp.name
            if not _matches_patterns(rel_posix, fp.name, patterns):
                continue
            if wanted:
                try:
                    if source.is_file():
                        bucket = ""
                    else:
                        rel = fp.relative_to(source)
                        bucket = rel.parts[0] if len(rel.parts) > 1 else ""
                except ValueError:
                    bucket = ""
                if _safe_camera_name(bucket).lower() != wanted:
                    if wanted not in _safe_camera_name(str(fp)).lower():
                        continue
            out.append(fp)
        out.sort(key=lambda p: str(p))
        max_files = _eff.get("max_files")
        if max_files is not None and max_files >= 0:
            out = out[: int(max_files)]
        return out

    def _dest_for(self, src: Path, source: Path, dest: Path, overwrite: bool) -> Path:
        try:
            if src == source or source.is_file():
                rel = Path(src.name)
            else:
                rel = src.relative_to(source)
        except ValueError:
            rel = Path(src.name)
        target = dest / rel
        if overwrite:
            return target
        if not target.exists():
            return target
        stem = target.stem
        suffix = target.suffix
        parent = target.parent
        i = 1
        while True:
            cand = parent / f"{stem}_{i}{suffix}"
            if not cand.exists():
                return cand
            i += 1

    def _cleanup_empty_dirs(self, source: Path) -> None:
        try:
            if not source.exists() or not source.is_dir():
                return
            for root, dirs, _files in os.walk(source, topdown=False):
                for d in dirs:
                    dp = Path(root) / d
                    try:
                        dp.rmdir()
                    except OSError:
                        pass
        except Exception:  # noqa: BLE001
            pass

    # ---------- selection ----------

    def select_for_migration(self, *args: Any, **kwargs: Any) -> List[Path]:
        options = self._merged_options(*args, **kwargs)
        eff = self._effective(options)
        return self._collect_files(eff)

    def select_candidates(self, *args: Any, **kwargs: Any) -> List[Path]:
        return self.select_for_migration(*args, **kwargs)

    def candidates(self, *args: Any, **kwargs: Any) -> List[Path]:
        return self.select_for_migration(*args, **kwargs)

    def collect_candidates(self, *args: Any, **kwargs: Any) -> List[Path]:
        return self.select_for_migration(*args, **kwargs)

    def get_files_to_migrate(self, *args: Any, **kwargs: Any) -> List[Path]:
        return self.select_for_migration(*args, **kwargs)

    def scan(self, *args: Any, **kwargs: Any) -> List[Path]:
        return self.select_for_migration(*args, **kwargs)

    def select(self, *args: Any, **kwargs: Any) -> List[Path]:
        return self.select_for_migration(*args, **kwargs)

    def list_files(self, *args: Any, **kwargs: Any) -> List[Path]:
        return self.select_for_migration(*args, **kwargs)

    # ---------- execution ----------

    def run(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        options = self._merged_options(*args, **kwargs)
        eff = self._effective(options)
        dry_run = bool(eff["dry_run"])
        copy = bool(eff["copy"])
        mode = str(eff["mode"])
        overwrite = bool(eff["overwrite"])
        source: Optional[Path] = eff["source_root"]
        dest: Optional[Path] = eff["dest_root"]

        errors: List[str] = []
        if source is None or dest is None:
            missing = []
            if source is None:
                missing.append("source")
            if dest is None:
                missing.append("destination")
            msg = f"missing required: {', '.join(missing)}"
            return self._error_result(eff, [msg], dry_run=dry_run)
        try:
            same = source.resolve() == dest.resolve()
        except Exception:  # noqa: BLE001
            same = str(source) == str(dest)
        if same:
            return self._error_result(eff, ["source and destination are the same"], dry_run=dry_run)
        if not source.exists():
            return self._error_result(eff, [f"source does not exist: {source}"], dry_run=dry_run)

        selected = self._collect_files(eff)
        total_before = sum(_size(p) for p in selected)

        moved: List[str] = []
        mapping: List[Dict[str, str]] = []
        moved_bytes = 0
        copied_bytes = 0
        if not dry_run:
            try:
                if dest.is_file() and len(selected) > 1:
                    errors.append(f"destination is a file: {dest}")
                    selected = []
                else:
                    dest.mkdir(parents=True, exist_ok=True)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{dest}: {exc}")
            for src in selected:
                try:
                    sz = _size(src)
                    target = self._dest_for(src, source, dest, overwrite)
                    try:
                        target.parent.mkdir(parents=True, exist_ok=True)
                    except Exception:  # noqa: BLE001
                        pass
                    if copy:
                        shutil.copy2(str(src), str(target))
                        copied_bytes += sz
                    else:
                        try:
                            shutil.move(str(src), str(target))
                        except Exception:
                            shutil.copy2(str(src), str(target))
                            src.unlink(missing_ok=True)  # type: ignore[call-arg]
                        moved_bytes += sz
                    moved.append(str(src))
                    mapping.append({
                        "source": str(src),
                        "destination": str(target),
                        "src": str(src),
                        "dst": str(target),
                        "from": str(src),
                        "to": str(target),
                    })
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{src}: {exc}")
            if not copy:
                self._cleanup_empty_dirs(source)
            acted_bytes = copied_bytes if copy else moved_bytes
        else:
            moved = [str(p) for p in selected]
            acted_bytes = int(total_before)
            for src in selected:
                try:
                    target = self._dest_for(src, source, dest, overwrite)
                except Exception:  # noqa: BLE001
                    target = dest / src.name
                mapping.append({
                    "source": str(src),
                    "destination": str(target),
                    "src": str(src),
                    "dst": str(target),
                    "from": str(src),
                    "to": str(target),
                })

        acted_bytes = int(copied_bytes if (copy and not dry_run) else (moved_bytes if not dry_run else total_before))
        return self._ok_result(
            eff, selected=[str(p) for p in selected], moved=moved,
            mapping=mapping, errors=errors, moved_bytes=int(acted_bytes),
            total_before=int(total_before), dry_run=dry_run,
        )

    def _error_result(self, eff: Dict[str, Any], errors: List[str], dry_run: bool = False) -> Dict[str, Any]:
        source = eff.get("source_root")
        dest = eff.get("dest_root")
        base: Dict[str, Any] = {
            "status": "error",
            "state": "error",
            "ok": False,
            "success": False,
            "dry_run": bool(dry_run),
            "preview": bool(dry_run),
            "copy": bool(eff.get("copy", False)),
            "mode": str(eff.get("mode", "move")),
            "overwrite": bool(eff.get("overwrite", False)),
            "moved": [],
            "moved_files": [],
            "moved_paths": [],
            "moved_count": 0,
            "migrated": [],
            "migrated_files": [],
            "migrated_count": 0,
            "relocated": [],
            "relocated_files": [],
            "relocated_count": 0,
            "transferred": [],
            "transferred_files": [],
            "transferred_count": 0,
            "copied": [],
            "copied_files": [],
            "copied_count": 0,
            "selected": [],
            "selected_files": [],
            "selected_count": 0,
            "candidates": [],
            "candidate_count": 0,
            "files": [],
            "paths": [],
            "files_to_migrate": [],
            "files_to_move": [],
            "to_migrate": [],
            "to_move": [],
            "mapping": [],
            "moves": [],
            "moved_mapping": [],
            "destinations": [],
            "destination_files": [],
            "moved_bytes": 0,
            "migrated_bytes": 0,
            "transferred_bytes": 0,
            "copied_bytes": 0,
            "bytes_moved": 0,
            "total_bytes": 0,
            "file_count": 0,
            "errors": list(errors),
            "error_count": len(errors),
            "error": "; ".join(errors),
            "message": "; ".join(errors),
            "detail": "; ".join(errors),
            "source_root": str(source) if source is not None else None,
            "source_dir": str(source) if source is not None else None,
            "source_path": str(source) if source is not None else None,
            "source": str(source) if source is not None else None,
            "src": str(source) if source is not None else None,
            "from_path": str(source) if source is not None else None,
            "dest_root": str(dest) if dest is not None else None,
            "dest_dir": str(dest) if dest is not None else None,
            "dest_path": str(dest) if dest is not None else None,
            "destination": str(dest) if dest is not None else None,
            "dst": str(dest) if dest is not None else None,
            "to_path": str(dest) if dest is not None else None,
            "target": str(dest) if dest is not None else None,
        }
        return base

    def _ok_result(
        self,
        eff: Dict[str, Any],
        selected: List[str],
        moved: List[str],
        mapping: List[Dict[str, str]],
        errors: List[str],
        moved_bytes: int,
        total_before: int,
        dry_run: bool,
    ) -> Dict[str, Any]:
        source = eff.get("source_root")
        dest = eff.get("dest_root")
        copy = bool(eff.get("copy", False))
        mode = str(eff.get("mode", "move"))
        # Per-camera breakdown of selected.
        grouped: Dict[str, List[str]] = {}
        for s in moved:
            try:
                if source is not None and not Path(str(source)).is_file():
                    rel = Path(s).relative_to(Path(str(source)))
                    bucket = rel.parts[0] if len(rel.parts) > 1 else "unassigned"
                else:
                    bucket = "unassigned"
            except Exception:  # noqa: BLE001
                bucket = "unassigned"
            grouped.setdefault(bucket, []).append(s)
        per_camera = [
            {"camera_name": k, "camera": k, "name": k, "selected": sorted(v),
             "selected_count": len(v), "moved": sorted(v), "moved_count": len(v),
             "migrated": sorted(v), "relocated": sorted(v)}
            for k, v in sorted(grouped.items())
        ]
        ok = len(errors) == 0
        result: Dict[str, Any] = {
            "status": "completed" if ok else "completed_with_errors",
            "state": "completed" if ok else "completed_with_errors",
            "ok": ok,
            "success": ok,
            "dry_run": bool(dry_run),
            "preview": bool(dry_run),
            "copy": bool(copy),
            "mode": mode,
            "overwrite": bool(eff.get("overwrite", False)),
            "moved": list(moved),
            "moved_files": list(moved),
            "moved_paths": list(moved),
            "moved_count": len(moved),
            "migrated": list(moved),
            "migrated_files": list(moved),
            "migrated_paths": list(moved),
            "migrated_count": len(moved),
            "relocated": list(moved),
            "relocated_files": list(moved),
            "relocated_paths": list(moved),
            "relocated_count": len(moved),
            "transferred": list(moved),
            "transferred_files": list(moved),
            "transferred_paths": list(moved),
            "transferred_count": len(moved),
            "copied": list(moved) if copy else [],
            "copied_files": list(moved) if copy else [],
            "copied_count": len(moved) if copy else 0,
            "selected": list(selected),
            "selected_files": list(selected),
            "selected_paths": list(selected),
            "selected_count": len(selected),
            "candidates": list(selected),
            "candidate_files": list(selected),
            "candidate_count": len(selected),
            "files": list(moved),
            "paths": list(moved),
            "files_to_migrate": list(selected),
            "files_to_move": list(selected),
            "to_migrate": list(selected),
            "to_move": list(selected),
            "to_transfer": list(selected),
            "mapping": mapping,
            "moves": mapping,
            "moved_mapping": mapping,
            "migrations": mapping,
            "transfers": mapping,
            "destinations": [m.get("destination") for m in mapping],
            "destination_files": [m.get("destination") for m in mapping],
            "moved_bytes": int(moved_bytes),
            "migrated_bytes": int(moved_bytes),
            "transferred_bytes": int(moved_bytes),
            "copied_bytes": int(moved_bytes) if copy else 0,
            "bytes_moved": int(moved_bytes),
            "bytes_transferred": int(moved_bytes),
            "bytes_copied": int(moved_bytes) if copy else 0,
            "freed_bytes": int(moved_bytes) if not copy and not dry_run else 0,
            "total_bytes": int(total_before),
            "scanned_total_bytes": int(total_before),
            "original_bytes": int(total_before),
            "file_count": len(selected),
            "scanned_files": len(selected),
            "files_scanned": len(selected),
            "errors": errors,
            "error_count": len(errors),
            "source_root": str(source) if source is not None else None,
            "source_dir": str(source) if source is not None else None,
            "source_path": str(source) if source is not None else None,
            "source": str(source) if source is not None else None,
            "src": str(source) if source is not None else None,
            "src_root": str(source) if source is not None else None,
            "from_path": str(source) if source is not None else None,
            "from": str(source) if source is not None else None,
            "dest_root": str(dest) if dest is not None else None,
            "dest_dir": str(dest) if dest is not None else None,
            "dest_path": str(dest) if dest is not None else None,
            "destination": str(dest) if dest is not None else None,
            "dst": str(dest) if dest is not None else None,
            "dst_root": str(dest) if dest is not None else None,
            "to_path": str(dest) if dest is not None else None,
            "to": str(dest) if dest is not None else None,
            "target": str(dest) if dest is not None else None,
            "target_path": str(dest) if dest is not None else None,
            "per_camera": per_camera,
            "breakdown": per_camera,
            "by_camera": {e["camera_name"]: e["selected"] for e in per_camera},
        }
        return result

    def preview(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        kw = dict(kwargs)
        kw["dry_run"] = True
        for a in args:
            if isinstance(a, dict):
                kw.update(a)
        return self.run(**kw)

    def dry_run(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.preview(*args, **kwargs)

    def migrate(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.run(*args, **kwargs)

    def move(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        kw = dict(kwargs)
        kw["copy"] = False
        kw["mode"] = "move"
        return self.run(*args, **kw)

    def copy_files(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        kw = dict(kwargs)
        kw["copy"] = True
        kw["mode"] = "copy"
        return self.run(*args, **kw)

    def copy(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.copy_files(*args, **kwargs)

    def relocate(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.run(*args, **kwargs)

    def transfer(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.run(*args, **kwargs)

    def execute(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.run(*args, **kwargs)

    def enforce(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.run(*args, **kwargs)

    def apply(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.run(*args, **kwargs)

    def process(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.run(*args, **kwargs)

    def get_status(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        options = self._merged_options(*args, **kwargs)
        eff = self._effective(options)
        source: Optional[Path] = eff["source_root"]
        dest: Optional[Path] = eff["dest_root"]
        src_files: List[Path] = self._collect_files(eff) if source is not None else []
        src_bytes = sum(_size(p) for p in src_files)
        dst_files: List[Path] = []
        dst_bytes = 0
        if dest is not None:
            try:
                if dest.exists():
                    if dest.is_file():
                        dst_files = [dest]
                    else:
                        for root, _dirs, fnames in os.walk(dest):
                            for fn in fnames:
                                fp = Path(root) / fn
                                try:
                                    if fp.is_file():
                                        dst_files.append(fp)
                                except OSError:
                                    continue
                    dst_bytes = sum(_size(p) for p in dst_files)
            except Exception:  # noqa: BLE001
                pass
        return {
            "status": "ok",
            "state": "ok",
            "ok": True,
            "success": True,
            "dry_run": bool(eff.get("dry_run", False)),
            "copy": bool(eff.get("copy", False)),
            "mode": str(eff.get("mode", "move")),
            "source_root": str(source) if source is not None else None,
            "source_dir": str(source) if source is not None else None,
            "source_path": str(source) if source is not None else None,
            "source": str(source) if source is not None else None,
            "src": str(source) if source is not None else None,
            "dest_root": str(dest) if dest is not None else None,
            "dest_dir": str(dest) if dest is not None else None,
            "dest_path": str(dest) if dest is not None else None,
            "destination": str(dest) if dest is not None else None,
            "dst": str(dest) if dest is not None else None,
            "source_exists": bool(source.exists()) if source is not None else False,
            "dest_exists": bool(dest.exists()) if dest is not None else False,
            "file_count": len(src_files),
            "scanned_files": len(src_files),
            "files_scanned": len(src_files),
            "candidate_count": len(src_files),
            "selected_count": len(src_files),
            "total_bytes": int(src_bytes),
            "source_bytes": int(src_bytes),
            "used_bytes": int(src_bytes),
            "dest_file_count": len(dst_files),
            "dest_files": len(dst_files),
            "dest_bytes": int(dst_bytes),
            "destination_bytes": int(dst_bytes),
            "candidates": [str(p) for p in src_files],
            "selected": [str(p) for p in src_files],
            "to_migrate": [str(p) for p in src_files],
            "to_move": [str(p) for p in src_files],
            "files": [str(p) for p in src_files],
        }

    def status(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.get_status(*args, **kwargs)

    def get_summary(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.get_status(*args, **kwargs)

    def summary(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.get_status(*args, **kwargs)

    def usage(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.get_status(*args, **kwargs)

    def info(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.get_status(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        low = (name or "").lower()
        select_hints = ("select", "candidate", "collect", "files", "paths", "scan", "list")
        if any(h in low for h in select_hints):
            return self.select_for_migration
        status_hints = ("status", "usage", "summary", "info", "health", "state")
        if any(h in low for h in status_hints):
            return self.get_status
        preview_hints = ("preview", "dry")
        if any(h in low for h in preview_hints):
            return self.preview
        copy_hints = ("copy", "duplicate", "clone")
        if any(h in low for h in copy_hints) and "move" not in low:
            return self.copy_files
        return self.run


# ---------- class / instance aliases ----------
StorageMigrateService = StorageMigrateManager
StorageMigrationManager = StorageMigrateManager
StorageMigrationService = StorageMigrateManager
MigrateManager = StorageMigrateManager
MigrateService = StorageMigrateManager
MigrationManager = StorageMigrateManager
MigrationService = StorageMigrateManager
AreaMigrateManager = StorageMigrateManager
AreaMigrationService = StorageMigrateManager

storage_migrate_service = StorageMigrateManager()
storage_migrate_manager = storage_migrate_service
storage_migration_service = storage_migrate_service
service = storage_migrate_service
manager = storage_migrate_service
default_service = storage_migrate_service
default_storage_migrate_service = storage_migrate_service
migrate_service = storage_migrate_service


# ---------- module-level convenience functions ----------
def _merge_call_args(args: Any, kwargs: Any) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            merged.update(a)
        elif isinstance(a, (str, Path)):
            merged.setdefault("path", a)
        elif isinstance(a, bool):
            merged.setdefault("dry_run", a)
    merged.update(kwargs)
    return merged


def migrate(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return storage_migrate_service.run(*args, **kwargs)


def run(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return storage_migrate_service.run(*args, **kwargs)


def move(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return storage_migrate_service.move(*args, **kwargs)


def copy(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return storage_migrate_service.copy_files(*args, **kwargs)


def copy_files(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return storage_migrate_service.copy_files(*args, **kwargs)


def relocate(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return storage_migrate_service.run(*args, **kwargs)


def transfer(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return storage_migrate_service.run(*args, **kwargs)


def execute(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return storage_migrate_service.run(*args, **kwargs)


def preview(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return storage_migrate_service.preview(*args, **kwargs)


def dry_run(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return storage_migrate_service.preview(*args, **kwargs)


def preview_migration(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return storage_migrate_service.preview(*args, **kwargs)


def scan(*args: Any, **kwargs: Any) -> List[Path]:
    return storage_migrate_service.select_for_migration(*args, **kwargs)


def select_for_migration(*args: Any, **kwargs: Any) -> List[Path]:
    return storage_migrate_service.select_for_migration(*args, **kwargs)


def select_candidates(*args: Any, **kwargs: Any) -> List[Path]:
    return storage_migrate_service.select_for_migration(*args, **kwargs)


def collect_candidates(*args: Any, **kwargs: Any) -> List[Path]:
    return storage_migrate_service.select_for_migration(*args, **kwargs)


def get_files_to_migrate(*args: Any, **kwargs: Any) -> List[Path]:
    return storage_migrate_service.select_for_migration(*args, **kwargs)


def get_status(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return storage_migrate_service.get_status(*args, **kwargs)


def status(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return storage_migrate_service.get_status(*args, **kwargs)


def get_summary(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return storage_migrate_service.get_status(*args, **kwargs)


def summary(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return storage_migrate_service.get_status(*args, **kwargs)


__all__ = [
    "StorageMigrateManager",
    "StorageMigrateService",
    "StorageMigrationManager",
    "StorageMigrationService",
    "MigrateManager",
    "MigrateService",
    "MigrationManager",
    "MigrationService",
    "AreaMigrateManager",
    "AreaMigrationService",
    "storage_migrate_service",
    "storage_migrate_manager",
    "storage_migration_service",
    "service",
    "manager",
    "migrate",
    "run",
    "move",
    "copy",
    "copy_files",
    "relocate",
    "transfer",
    "execute",
    "preview",
    "dry_run",
    "preview_migration",
    "scan",
    "select_for_migration",
    "select_candidates",
    "collect_candidates",
    "get_files_to_migrate",
    "get_status",
    "status",
    "get_summary",
    "summary",
]
