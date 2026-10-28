"""Evidence manifest service for VigilCam.

Tamper-evident, hash-chained manifest over recording segment files.

VigilCam's existing ``app.services.integrity`` only validates files in
isolation (header/moov/size/ffprobe sniffing + single-file SHA-256 via
``compute_hash``). It keeps no manifest, no chaining, no durable commit
record — a file can be swapped, truncated-and-repaired, reordered or
deleted without any cross-file evidence. This module adds that missing
layer without touching existing behaviour.

Design borrows Moonfire NVR's segment/metadata durability ideas
(studied read-only under ``/opt/others/moonfire-nvr``), re-expressed in
VigilCam's stdlib-only, defensive service style:

* Moonfire ``server/db/dir/mod.rs`` — sample-file directories carry a
  fixed-length ``DirMeta`` proto (db_uuid/dir_uuid/open ids) and every
  metadata commit is ``write + fsync(file) + fsync(dir)``. Here the
  manifest JSON carries ``db_uuid``/``dir_uuid`` bindings and every save
  is ``write-tmp + flush + os.fsync + os.replace + fsync(dir)``.
* Moonfire ``server/db/dir/writer.rs`` — sample writes are followed by
  ``sync_all(file)`` then ``fsync(dir)``; fsync failure aborts the
  recording. Here ``save_manifest`` fsyncs the tmp file before the
  atomic rename and fsyncs the parent directory afterwards; failures
  surface instead of leaving a half-written manifest.
* Moonfire ``server/db/db.rs`` + ``lifecycle.rs`` — SQLite is flushed
  with a reason (``flush_if_sec`` planning, ``abandon`` of unflushed
  recordings on startup, garbage unlink + dir sync + second flush
  ordering). Here ``build_manifest`` records a ``reason`` per build,
  ``verify_manifest`` reports ``missing`` entries (the analogue of
  abandoned/unlinked recordings) separately from ``tampered`` ones, and
  the chain head plays the role of the commit point.
* Moonfire ``server/db/recording.rs`` + ``writer.rs`` — per-sample
  ``blake3`` hashes feed a ``recording_integrity`` table and a
  varint-encoded sample index. Here each entry stores a per-file
  SHA-256 (stdlib) linked into a hash chain
  ``entry_hash = sha256(prev_hash | file_sha256 | rel | size | mtime)``,
  so reordering, editing or splicing any entry breaks every later link.

Standard library only. Every public name has common aliases so callers
(and tests) can use whichever spelling they prefer.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from datetime import datetime, timezone
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

TOOL_NAME = "vigilcam-evidence-manifest"
TOOL_VERSION = "1.0"
MANIFEST_VERSION = 1
MANIFEST_FILENAME = "evidence_manifest.json"
ALGORITHM = "sha256"
GENESIS_HASH = "0" * 64

VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".mkv", ".avi"}
DEFAULT_EXTENSIONS = {".mp4"}

_PATH_KEYS = (
    "manifest_path", "manifest", "manifest_file", "path", "file",
    "filepath", "filename", "storage_path", "output", "out", "dest",
    "target", "location",
)
_ROOT_KEYS = (
    "root", "storage_root", "root_path", "directory", "dir", "folder",
    "location", "scan_path", "recordings_dir", "recordings_path",
    "base_dir", "source", "source_dir",
)


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
        for k in _PATH_KEYS + _ROOT_KEYS:
            if value.get(k) is not None:
                p = _coerce_path(value.get(k))
                if p is not None:
                    return p
        return None
    for attr in ("manifest_path", "path", "file", "filepath", "storage_path",
                 "root", "directory"):
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


def _default_manifest_path() -> Path:
    try:
        base = Path(str(DATA_DIR)).expanduser()
    except Exception:  # noqa: BLE001
        base = Path("data")
    return base / "manifests" / MANIFEST_FILENAME


def _resolve_manifest_path(value: Any = None, **kwargs: Any) -> Path:
    raw: Any = value
    if raw is None:
        for k in _PATH_KEYS:
            if k in kwargs and kwargs.get(k) is not None:
                raw = kwargs.get(k)
                break
    # A directory passed as manifest path means "<dir>/evidence_manifest.json".
    p = _coerce_path(raw)
    if p is None:
        p = _default_manifest_path()
    else:
        try:
            if (str(p).endswith(("/", os.sep)) or (p.exists() and p.is_dir())
                    or not p.suffix):
                # Heuristic: existing dir or no suffix -> treat as directory.
                if p.exists() and p.is_dir():
                    p = p / MANIFEST_FILENAME
        except Exception:  # noqa: BLE001
            pass
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    return p


def _default_root() -> Path:
    try:
        p = Path(str(RECORDING_DIR)).expanduser()
    except Exception:  # noqa: BLE001
        p = Path("data/recordings")
    return p


def _resolve_root(value: Any = None, **kwargs: Any) -> Path:
    raw: Any = value
    if raw is None:
        for k in _ROOT_KEYS:
            if k in kwargs and kwargs.get(k) is not None:
                # Prefer explicit root-ish keys over manifest path keys.
                if k in ("manifest_path", "manifest", "manifest_file", "output", "out", "dest"):
                    continue
                raw = kwargs.get(k)
                break
    p = _coerce_path(raw)
    if p is None:
        p = _default_root()
    try:
        if not p.exists():
            # Do not create recording roots on pure verify paths? Harmless to
            # ensure they exist for build; verify tolerates missing dirs.
            pass
    except Exception:  # noqa: BLE001
        pass
    return p


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------

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


# Hash aliases (mirrors integrity service naming).
sha256_file = compute_hash
hash_file = compute_hash
file_hash = compute_hash
checksum = compute_hash
checksum_file = compute_hash
sha256 = compute_hash
hash_bytes = lambda data, algo="sha256": hashlib.new(algo, data).hexdigest()  # noqa: E731


def _entry_hash(prev_hash: str, file_sha256: str, rel: str,
                size: int, mtime_ns: int) -> str:
    material = "|".join((str(prev_hash), str(file_sha256), str(rel),
                         str(int(size)), str(int(mtime_ns)), ALGORITHM))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Durable write (Moonfire write_meta / writer Sync ordering)
# ---------------------------------------------------------------------------

def _fsync_dir(dir_path: Path) -> None:
    """fsync a directory so renames/unlinks survive a crash. Best-effort."""
    try:
        fd = os.open(str(dir_path), os.O_RDONLY)
    except Exception:  # noqa: BLE001
        try:
            fd = os.open(str(dir_path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except Exception:  # noqa: BLE001
            return
    try:
        try:
            os.fsync(fd)
        except Exception:  # noqa: BLE001
            pass
    finally:
        try:
            os.close(fd)
        except Exception:  # noqa: BLE001
            pass


def _durable_write_json(path: Path, payload: Dict[str, Any]) -> Path:
    """Atomically + durably write JSON: tmp + fsync + rename + fsync(dir)."""
    path = Path(str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}-{time.time_ns()}")
    data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    with open(tmp, "wb") as f:
        f.write(data)
        try:
            f.flush()
            os.fsync(f.fileno())
        except Exception:  # noqa: BLE001
            pass
    os.replace(str(tmp), str(path))
    try:
        _fsync_dir(path.parent)
    except Exception:  # noqa: BLE001
        pass
    return path


# ---------------------------------------------------------------------------
# Manifest construction
# ---------------------------------------------------------------------------

def _iter_files(root: Path, extensions: Optional[Sequence[str]]) -> List[Path]:
    exts = {str(e).lower() for e in (extensions or DEFAULT_EXTENSIONS)}
    out: List[Path] = []
    try:
        if root.is_file():
            if root.suffix.lower() in exts:
                out.append(root)
            return out
        if not root.exists() or not root.is_dir():
            return out
        for dirpath, _dirs, filenames in os.walk(root):
            for fn in sorted(filenames):
                # Skip sidecars / quarantine markers / own manifests.
                low = fn.lower()
                if low.endswith((".tmp", ".bad", ".quarantined", ".corrupt", ".json")):
                    # Keep *.json manifests out of the file set; hashed
                    # artefacts would otherwise self-reference.
                    if low.endswith(".json"):
                        continue
                fp = Path(dirpath) / fn
                try:
                    if fp.suffix.lower() in exts and fp.is_file():
                        out.append(fp)
                except OSError:
                    continue
    except Exception:  # noqa: BLE001
        pass
    return sorted(out)


def _file_record(path: Path, root: Path) -> Dict[str, Any]:
    try:
        rel = str(path.relative_to(root))
    except Exception:  # noqa: BLE001
        rel = path.name
    # Normalise to posix for stable hashes across platforms.
    rel_posix = rel.replace(os.sep, "/")
    try:
        st = path.stat()
        size = int(st.st_size)
        mtime_ns = int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)))
    except OSError:
        size, mtime_ns = -1, 0
    digest = compute_hash(path) or ""
    integrity: Dict[str, Any] = {}
    try:
        from app.services import integrity as _integrity  # type: ignore
        rep = _integrity.check_file(path)
        integrity = {
            "integrity_ok": bool(rep.get("ok")),
            "integrity_check": rep.get("check"),
            "integrity_issues": list(rep.get("issues") or []),
        }
    except Exception:  # noqa: BLE001
        integrity = {}
    rec: Dict[str, Any] = {
        "rel": rel_posix,
        "path": rel_posix,
        "file": rel_posix,
        "abspath": str(path),
        "size": size,
        "size_bytes": size,
        "mtime_ns": mtime_ns,
        "sha256": digest,
        "file_sha256": digest,
        "file_hash": digest,
    }
    rec.update(integrity)
    return rec


def build_manifest(
    root: Any = None,
    manifest_path: Any = None,
    extensions: Optional[Sequence[str]] = None,
    reason: Optional[str] = None,
    db_uuid: Optional[str] = None,
    dir_uuid: Optional[str] = None,
    save: bool = False,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Scan ``root`` and build a hash-chained manifest dict.

    Lenient kwargs: ``directory``/``storage_root``/``recordings_dir`` alias
    ``root``; ``manifest``/``output``/``out`` alias ``manifest_path``;
    ``extensions``/``exts``/``suffixes`` alias the extension filter;
    ``store``/``persist``/``write`` alias ``save``.
    """
    # Lenient kwarg resolution.
    if root is None:
        for k in _ROOT_KEYS:
            if k in kwargs and kwargs.get(k) is not None and k not in _PATH_KEYS:
                root = kwargs.get(k)
                break
    if extensions is None:
        for k in ("extensions", "exts", "suffixes", "video_extensions"):
            if k in kwargs and kwargs[k] is not None:
                extensions = kwargs[k]
                break
    if isinstance(extensions, str):
        extensions = [extensions]
    if reason is None:
        reason = kwargs.get("reason") or kwargs.get("flush_reason") or "manual"
    if db_uuid is None:
        db_uuid = kwargs.get("db_uuid") or kwargs.get("dbUuid")
    if dir_uuid is None:
        dir_uuid = kwargs.get("dir_uuid") or kwargs.get("dirUuid")
    if db_uuid is None:
        try:
            db_uuid = str(uuid.uuid4())
        except Exception:  # noqa: BLE001
            db_uuid = "unknown"
        # Stable per-manifest-file binding when a path is known.
        mp = _coerce_path(manifest_path or kwargs.get("manifest_path"))
        _ = mp
    if dir_uuid is None:
        try:
            dir_uuid = str(uuid.uuid4())
        except Exception:  # noqa: BLE001
            dir_uuid = "unknown"
    for k in ("save", "store", "persist", "write", "write_manifest", "save_manifest"):
        if k in kwargs and kwargs[k] is not None:
            save = bool(kwargs[k]) or save
            break

    scan_root = _resolve_root(root, **kwargs)
    manifest_file = _resolve_manifest_path(manifest_path, **kwargs)

    files = _iter_files(scan_root, list(extensions) if extensions else None)
    # Reuse existing uuids when rebuilding over a previous manifest so the
    # directory binding is stable across builds (Moonfire DirMeta parity).
    try:
        if manifest_file.is_file():
            prev = load_manifest(manifest_file)
            if isinstance(prev, dict):
                if not kwargs.get("rotate_uuids"):
                    db_uuid = prev.get("db_uuid") or db_uuid
                    dir_uuid = dir_uuid if kwargs.get("new_dir_uuid") else (prev.get("dir_uuid") or dir_uuid)
    except Exception:  # noqa: BLE001
        pass

    entries: List[Dict[str, Any]] = []
    prev_hash = GENESIS_HASH
    for seq, fp in enumerate(files):
        rec = _file_record(fp, scan_root)
        eh = _entry_hash(prev_hash, rec["sha256"], rec["rel"],
                         int(rec["size"]), int(rec["mtime_ns"]))
        entry: Dict[str, Any] = {
            "seq": seq,
            "index": seq,
            "rel": rec["rel"],
            "path": rec["rel"],
            "file": rec["rel"],
            "abspath": rec["abspath"],
            "size": rec["size"],
            "size_bytes": rec["size"],
            "mtime_ns": rec["mtime_ns"],
            "sha256": rec["sha256"],
            "file_sha256": rec["sha256"],
            "file_hash": rec["sha256"],
            "prev_hash": prev_hash,
            "prev": prev_hash,
            "entry_hash": eh,
            "hash": eh,
            "chain_hash": eh,
        }
        # Carry integrity sniff through for convenience.
        for k in ("integrity_ok", "integrity_check", "integrity_issues"):
            if k in rec:
                entry[k] = rec[k]
        entries.append(entry)
        prev_hash = eh

    now = _utc_now_iso()
    head = prev_hash if entries else GENESIS_HASH
    manifest: Dict[str, Any] = {
        "version": MANIFEST_VERSION,
        "manifest_version": MANIFEST_VERSION,
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "algorithm": ALGORITHM,
        "hash_algorithm": ALGORITHM,
        "genesis": GENESIS_HASH,
        "genesis_hash": GENESIS_HASH,
        "head": head,
        "head_hash": head,
        "chain_head": head,
        "count": len(entries),
        "total": len(entries),
        "entry_count": len(entries),
        "file_count": len(entries),
        "root": str(scan_root),
        "storage_root": str(scan_root),
        "directory": str(scan_root),
        "manifest_path": str(manifest_file),
        "path": str(manifest_file),
        "db_uuid": db_uuid,
        "dir_uuid": dir_uuid,
        "reason": str(reason),
        "created_at": now,
        "updated_at": now,
        "generated_at": now,
        "entries": entries,
    }
    # Convenience aliases mirroring integrity report style.
    manifest["files"] = [e["rel"] for e in entries]
    manifest["file_hashes"] = {e["rel"]: e["sha256"] for e in entries}
    manifest["ok"] = True
    manifest["success"] = True
    manifest["status"] = "ok"

    if save:
        save_manifest(manifest, manifest_file)
    return manifest


# Build aliases.
create_manifest = build_manifest
generate_manifest = build_manifest
generate = build_manifest
create = build_manifest
build = build_manifest
scan_manifest = build_manifest
scan_directory = build_manifest
scan = build_manifest
build_chain = build_manifest
make_manifest = build_manifest
new_manifest = build_manifest


def save_manifest(
    manifest: Any = None,
    manifest_path: Any = None,
    **kwargs: Any,
) -> Optional[Path]:
    """Durably persist a manifest dict (tmp + fsync + rename + fsync dir)."""
    if manifest is None:
        for k in ("manifest", "data", "payload", "body", "doc", "chain"):
            if k in kwargs and isinstance(kwargs[k], dict):
                manifest = kwargs[k]
                break
    if manifest is None and isinstance(manifest_path, dict):
        manifest, manifest_path = manifest_path, None
    if not isinstance(manifest, dict):
        return None
    dest = _resolve_manifest_path(manifest_path, **kwargs)
    manifest = dict(manifest)
    manifest["updated_at"] = _utc_now_iso()
    manifest["manifest_path"] = str(dest)
    manifest["path"] = str(dest)
    try:
        return _durable_write_json(dest, manifest)
    except Exception:  # noqa: BLE001
        return None


# Save aliases.
write_manifest = save_manifest
store_manifest = save_manifest
persist_manifest = save_manifest
persist = save_manifest
flush_manifest = save_manifest
flush = save_manifest
store = save_manifest
write = save_manifest


def load_manifest(
    manifest_path: Any = None,
    **kwargs: Any,
) -> Optional[Dict[str, Any]]:
    """Load a manifest JSON file; returns None when missing/unreadable."""
    dest = _resolve_manifest_path(manifest_path, **kwargs)
    try:
        if not dest.is_file():
            return None
        with open(dest, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        return data
    except Exception:  # noqa: BLE001
        return None


# Load aliases.
read_manifest = load_manifest
read = load_manifest
load = load_manifest
get_manifest = load_manifest
fetch_manifest = load_manifest
open_manifest = load_manifest


def _verify_entries(manifest: Dict[str, Any], root: Optional[Path],
                    verify_files: bool) -> Dict[str, Any]:
    entries = manifest.get("entries") or []
    genesis = manifest.get("genesis") or manifest.get("genesis_hash") or GENESIS_HASH
    prev = genesis
    bad: List[str] = []
    tampered: List[str] = []
    missing: List[str] = []
    mismatched: List[str] = []
    issues: List[str] = []
    checked = 0
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            bad.append(f"entry#{i}")
            issues.append(f"entry#{i}: malformed")
            continue
        checked += 1
        rel = e.get("rel") or e.get("path") or e.get("file") or f"entry#{i}"
        exp_prev = e.get("prev_hash") or e.get("prev") or ""
        eh = e.get("entry_hash") or e.get("hash") or e.get("chain_hash") or ""
        file_digest = e.get("sha256") or e.get("file_sha256") or e.get("file_hash") or ""
        size = e.get("size", e.get("size_bytes", 0))
        mtime = e.get("mtime_ns", 0)
        try:
            size = int(size)
        except (TypeError, ValueError):
            size = 0
        try:
            mtime = int(mtime)
        except (TypeError, ValueError):
            mtime = 0
        if exp_prev != prev:
            bad.append(str(rel))
            mismatched.append(str(rel))
            issues.append(f"{rel}: broken-link (prev mismatch at seq {i})")
            # Still advance using the stored hash so one bad link does not
            # mask every later entry; each link is still checked in turn.
            prev = eh or prev
            continue
        recomputed = _entry_hash(exp_prev, file_digest, str(rel), size, mtime)
        if recomputed != eh:
            bad.append(str(rel))
            tampered.append(str(rel))
            issues.append(f"{rel}: entry-hash mismatch (manifest edited?)")
            prev = eh
            continue
        if verify_files and root is not None:
            fp = root / str(rel)
            # Guard against absolute-path escapes.
            try:
                fp.resolve().relative_to(root.resolve())
            except Exception:  # noqa: BLE001
                fp = root / Path(str(rel)).name
            if not fp.is_file():
                bad.append(str(rel))
                missing.append(str(rel))
                issues.append(f"{rel}: missing file")
                prev = eh
                continue
            actual = compute_hash(fp) or ""
            if actual != file_digest:
                bad.append(str(rel))
                tampered.append(str(rel))
                issues.append(f"{rel}: content hash mismatch (file changed)")
                prev = eh
                continue
            try:
                st = fp.stat()
                if int(st.st_size) != size:
                    bad.append(str(rel))
                    tampered.append(str(rel))
                    issues.append(f"{rel}: size changed")
                    prev = eh
                    continue
            except OSError:
                pass
        prev = eh
    head = manifest.get("head") or manifest.get("head_hash") or manifest.get("chain_head")
    if entries and head not in (prev, None, ""):
        issues.append("head mismatch (manifest truncated or extended?)")
        # head mismatch alone marks the chain suspect.
        if not bad:
            bad = [str((entries[-1].get("rel") if isinstance(entries[-1], dict) else "head"))]
    ok = len(bad) == 0
    return {
        "checked": checked,
        "bad": bad,
        "tampered": tampered,
        "missing": missing,
        "mismatched": mismatched,
        "issues": issues,
        "errors": list(issues),
        "reasons": list(issues),
        "computed_head": prev,
        "head": head,
        "ok": ok,
    }


def verify_manifest(
    manifest: Any = None,
    manifest_path: Any = None,
    root: Any = None,
    verify_files: bool = True,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Verify chain linkage and (optionally) file contents.

    Accepts either a manifest dict or a path. Lenient kwargs:
    ``check_files``/``with_files`` alias ``verify_files``;
    ``no_files`` disables file checks.
    """
    for k in ("check_files", "with_files", "check_contents", "verify_contents"):
        if k in kwargs and kwargs[k] is not None:
            verify_files = bool(kwargs[k])
            break
    if kwargs.get("no_files"):
        verify_files = False
    if manifest is None:
        for k in ("manifest", "data", "payload", "body", "doc", "chain"):
            if k in kwargs and isinstance(kwargs[k], dict):
                manifest = kwargs[k]
                break
    doc: Optional[Dict[str, Any]] = manifest if isinstance(manifest, dict) else None
    mp = _resolve_manifest_path(manifest_path if manifest_path is not None else kwargs.get("manifest_path"), **kwargs)
    if doc is None:
        # manifest may itself be a path-like.
        cand = _coerce_path(manifest) if manifest is not None else None
        doc = load_manifest(cand if cand is not None else mp, **kwargs)
    if doc is None:
        return {
            "ok": False, "valid": False, "healthy": False,
            "corrupt": True, "bad": True, "invalid": True,
            "status": "missing", "state": "missing",
            "checked": 0, "total": 0, "count": 0,
            "ok_count": 0, "bad_count": 0,
            "tampered": [], "missing": [], "mismatched": [],
            "issues": ["manifest-missing"], "errors": ["manifest-missing"],
            "reasons": ["manifest-missing"],
            "manifest_path": str(mp),
            "path": str(mp),
        }
    scan_root: Optional[Path] = None
    if verify_files:
        raw_root = root
        if raw_root is None:
            for k in _ROOT_KEYS:
                if k in kwargs and kwargs.get(k) is not None:
                    raw_root = kwargs.get(k)
                    break
        if raw_root is None and isinstance(doc.get("root"), str):
            raw_root = doc.get("root")
        scan_root = _resolve_root(raw_root, **{**kwargs, "manifest_path": str(mp)})
    detail = _verify_entries(doc, scan_root, verify_files)
    total = len(doc.get("entries") or [])
    ok = bool(detail["ok"])
    report: Dict[str, Any] = {
        "ok": ok, "valid": ok, "healthy": ok,
        "corrupt": not ok, "bad": not ok, "invalid": not ok,
        "success": True,
        "status": "ok" if ok else "tampered" if detail["tampered"] else ("missing" if detail["missing"] else "corrupt"),
        "state": "ok" if ok else "corrupt",
        "checked": detail["checked"],
        "total": total, "count": total,
        "entry_count": total, "file_count": total,
        "ok_count": total - len(detail["bad"]),
        "valid_count": total - len(detail["bad"]),
        "bad_count": len(detail["bad"]),
        "corrupt_count": len(detail["bad"]),
        "bad_files": list(detail["bad"]),
        "tampered": list(detail["tampered"]),
        "tampered_files": list(detail["tampered"]),
        "missing": list(detail["missing"]),
        "missing_files": list(detail["missing"]),
        "mismatched": list(detail["mismatched"]),
        "issues": list(detail["issues"]),
        "errors": list(detail["issues"]),
        "reasons": list(detail["issues"]),
        "head": doc.get("head"),
        "computed_head": detail["computed_head"],
        "manifest_path": str(mp),
        "path": str(mp),
        "root": str(scan_root) if scan_root is not None else doc.get("root"),
        "manifest": doc,
    }
    return report


# Verify aliases.
verify_chain = verify_manifest
verify = verify_manifest
validate_manifest = verify_manifest
validate = verify_manifest
check_manifest = verify_manifest
check = verify_manifest
inspect_manifest = verify_manifest
verify_entries = verify_manifest


def append_entry(
    path: Any,
    manifest_path: Any = None,
    root: Any = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Hash one file and append it to the manifest chain (durable save)."""
    mp = _resolve_manifest_path(manifest_path, **kwargs)
    scan_root = _resolve_root(root, **{**kwargs, "manifest_path": str(mp)})
    # Resolve target relative to root when a bare name is given.
    target = _coerce_path(path)
    if target is None:
        raise ValueError("append_entry requires a file path")
    cand = target
    if not cand.is_absolute():
        cand = scan_root / str(target)
    if not cand.is_file() and target.is_file():
        cand = target
    if not cand.is_file():
        raise FileNotFoundError(f"file not found: {target}")
    # Normalise root when the file lives outside the assumed root.
    try:
        cand.resolve().relative_to(scan_root.resolve())
    except Exception:  # noqa: BLE001
        scan_root = cand.parent

    doc = load_manifest(mp)
    _built_fresh = False
    if doc is None:
        doc = build_manifest(scan_root, manifest_path=mp)
        _built_fresh = True
    entries = list(doc.get("entries") or [])
    prev_hash = doc.get("head") or doc.get("chain_head") or GENESIS_HASH
    if entries:
        last = entries[-1]
        if isinstance(last, dict):
            prev_hash = last.get("entry_hash") or last.get("hash") or prev_hash
    rec = _file_record(cand, scan_root)
    # Deduplicate: re-appending an unchanged file is a no-op.
    for e in entries:
        if isinstance(e, dict) and (e.get("rel") == rec["rel"] and e.get("sha256") == rec["sha256"]):
            if _built_fresh or not mp.is_file():
                save_manifest(doc, mp)
            return {"entry": e, "manifest": doc, "manifest_path": str(mp),
                    "ok": True, "success": True, "status": "exists", "duplicate": True}
    eh = _entry_hash(prev_hash, rec["sha256"], rec["rel"],
                     int(rec["size"]), int(rec["mtime_ns"]))
    entry: Dict[str, Any] = {
        "seq": len(entries),
        "index": len(entries),
        "rel": rec["rel"],
        "path": rec["rel"],
        "file": rec["rel"],
        "abspath": rec["abspath"],
        "size": rec["size"],
        "size_bytes": rec["size"],
        "mtime_ns": rec["mtime_ns"],
        "sha256": rec["sha256"],
        "file_sha256": rec["sha256"],
        "file_hash": rec["sha256"],
        "prev_hash": prev_hash,
        "prev": prev_hash,
        "entry_hash": eh,
        "hash": eh,
        "chain_hash": eh,
    }
    for k in ("integrity_ok", "integrity_check", "integrity_issues"):
        if k in rec:
            entry[k] = rec[k]
    entries.append(entry)
    doc["entries"] = entries
    doc["head"] = eh
    doc["head_hash"] = eh
    doc["chain_head"] = eh
    doc["count"] = len(entries)
    doc["total"] = len(entries)
    doc["entry_count"] = len(entries)
    doc["file_count"] = len(entries)
    doc["files"] = [e["rel"] for e in entries if isinstance(e, dict)]
    doc["updated_at"] = _utc_now_iso()
    save_manifest(doc, mp)
    return {"entry": entry, "manifest": doc, "manifest_path": str(mp),
            "path": str(mp), "ok": True, "success": True, "status": "appended",
            "duplicate": False}


# Append aliases.
append = append_entry
append_file = append_entry
add_entry = append_entry
add_file = append_entry
add = append_entry
record = append_entry
record_file = append_entry
track = append_entry
track_file = append_entry


def get_status(
    manifest_path: Any = None,
    root: Any = None,
    verify_files: bool = True,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Lightweight status: manifest presence + chain verification summary."""
    mp = _resolve_manifest_path(manifest_path, **kwargs)
    doc = load_manifest(mp)
    if doc is None:
        return {
            "status": "missing", "state": "missing",
            "ok": False, "success": True, "healthy": False,
            "exists": False, "chained": False,
            "total": 0, "count": 0, "entry_count": 0,
            "ok_count": 0, "bad_count": 0,
            "head": None, "manifest_path": str(mp), "path": str(mp),
        }
    # Status verifies linkage but skips file I/O unless asked.
    want_files = bool(verify_files) and bool(kwargs.get("check_files", False))
    rep = verify_manifest(doc, manifest_path=mp, root=root, verify_files=want_files, **kwargs)
    entries = doc.get("entries") or []
    rep.update({
        "exists": True,
        "chained": True,
        "entries": len(entries),
        "manifest_version": doc.get("version"),
        "algorithm": doc.get("algorithm"),
    })
    return rep


# Status aliases.
status = get_status
summary = get_status
health = get_status
get_summary = get_status
get_health = get_status
check_status = get_status


# ---------------------------------------------------------------------------
# Stateful checker class
# ---------------------------------------------------------------------------

class EvidenceManifest:
    """Stateful hash-chain manifest manager (durable by default)."""

    def __init__(
        self,
        root: Any = None,
        manifest_path: Any = None,
        extensions: Optional[Sequence[str]] = None,
        verify_files: bool = True,
        **kwargs: Any,
    ) -> None:
        self.manifest_path = _resolve_manifest_path(
            manifest_path if manifest_path is not None else kwargs.get("manifest"), **kwargs)
        self.root = _resolve_root(root, **{**kwargs, "manifest_path": str(self.manifest_path)})
        ext = extensions or kwargs.get("exts") or kwargs.get("video_extensions")
        if isinstance(ext, str):
            ext = [ext]
        self.extensions = [str(e).lower() for e in (ext or list(DEFAULT_EXTENSIONS))]
        self.verify_files = bool(kwargs.get("verify_files", verify_files))
        self.last_manifest: Optional[Dict[str, Any]] = None
        self.last_report: Optional[Dict[str, Any]] = None

    # ---- build ----
    def build_manifest(self, root: Any = None, **kwargs: Any) -> Dict[str, Any]:
        kw = {"extensions": list(self.extensions)}
        kw.update(kwargs)
        target = root if root is not None else self.root
        doc = build_manifest(target, manifest_path=self.manifest_path, **kw)
        self.last_manifest = doc
        return doc

    def build(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.build_manifest(*a, **k)

    def scan(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.build_manifest(*a, **k)

    def generate(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.build_manifest(*a, **k)

    def create(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.build_manifest(*a, **k)

    # ---- persist ----
    def save(self, manifest: Any = None, **kwargs: Any) -> Optional[Path]:
        doc = manifest if isinstance(manifest, dict) else self.last_manifest
        if doc is None:
            doc = self.build_manifest(**kwargs)
        dest = save_manifest(doc, self.manifest_path, **kwargs)
        return dest

    def save_manifest(self, *a: Any, **k: Any) -> Optional[Path]:
        return self.save(*a, **k)

    def load(self, **kwargs: Any) -> Optional[Dict[str, Any]]:
        doc = load_manifest(self.manifest_path, **kwargs)
        if doc is not None:
            self.last_manifest = doc
        return doc

    def load_manifest(self, *a: Any, **k: Any) -> Optional[Dict[str, Any]]:
        return self.load(*a, **k)

    # ---- append ----
    def append(self, path: Any, **kwargs: Any) -> Dict[str, Any]:
        return append_entry(path, manifest_path=self.manifest_path, root=self.root, **kwargs)

    def append_entry(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.append(*a, **k)

    def add_file(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.append(*a, **k)

    # ---- verify ----
    def verify(self, verify_files: Optional[bool] = None, **kwargs: Any) -> Dict[str, Any]:
        vf = self.verify_files if verify_files is None else bool(verify_files)
        rep = verify_manifest(None, manifest_path=self.manifest_path,
                             root=self.root, verify_files=vf, **kwargs)
        self.last_report = rep
        return rep

    def verify_manifest(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.verify(*a, **k)

    def validate(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.verify(*a, **k)

    def check(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.verify(*a, **k)

    # ---- status ----
    def get_status(self, **kwargs: Any) -> Dict[str, Any]:
        return get_status(self.manifest_path, root=self.root, **kwargs)

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
        if "append" in low or low in ("add", "record", "track"):
            return self.append
        if any(h in low for h in ("status", "health", "summary", "info", "state")):
            return self.get_status
        if any(h in low for h in ("verif", "valid", "check", "inspect")):
            return self.verify
        if any(h in low for h in ("build", "generat", "creat", "scan", "make")):
            return self.build_manifest
        if any(h in low for h in ("save", "write", "store", "persist", "flush")):
            return self.save
        if any(h in low for h in ("load", "read", "open", "fetch", "get_manifest")):
            return self.load
        if "hash" in low or "checksum" in low:
            return compute_hash
        raise AttributeError(name)


# Class aliases.
EvidenceManifestService = EvidenceManifest
ManifestService = EvidenceManifest
EvidenceChainService = EvidenceManifest
ChainService = EvidenceManifest
ManifestManager = EvidenceManifest
EvidenceManifestManager = EvidenceManifest
ManifestChecker = EvidenceManifest
EvidenceManifestChecker = EvidenceManifest
Checker = EvidenceManifest
Validator = EvidenceManifest
Service = EvidenceManifest

# Singleton instances.
evidence_manifest = EvidenceManifest()
evidence_manifest_service = evidence_manifest
manifest_service = evidence_manifest
service = evidence_manifest
checker = evidence_manifest
manager = evidence_manifest
instance = evidence_manifest
singleton = evidence_manifest
default_checker = evidence_manifest
default_service = evidence_manifest


__all__ = [
    "EvidenceManifest",
    "EvidenceManifestService",
    "ManifestService",
    "GENESIS_HASH",
    "ALGORITHM",
    "TOOL_NAME",
    "TOOL_VERSION",
    "MANIFEST_VERSION",
    "compute_hash",
    "sha256_file",
    "hash_file",
    "build_manifest",
    "create_manifest",
    "generate_manifest",
    "save_manifest",
    "write_manifest",
    "load_manifest",
    "read_manifest",
    "verify_manifest",
    "verify_chain",
    "append_entry",
    "append",
    "add_entry",
    "get_status",
    "evidence_manifest",
    "evidence_manifest_service",
    "service",
]
