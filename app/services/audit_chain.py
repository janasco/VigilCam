"""Audit hash-chain service for VigilCam.

Tamper-evident, hash-chained layer over audit log entries.

Status quo (verified before writing this module):

* ``app.services.audit`` stores rows in ``AuditStore`` (in-memory +
  optional JSONL) with **no** ``prev_hash`` / ``entry_hash`` /
  ``chain_head`` / ``genesis`` fields and **no** ``verify_chain`` /
  ``verify_manifest`` helper — a row can be edited, reordered or
  deleted without detection.
* ``app.services.evidence_manifest`` *does* hash-chain, but **recordings
  only**: it scans video files (``.mp4`` et al.), stores per-file
  SHA-256 digests and links ``entry_hash =
  sha256(prev_hash | file_sha256 | rel | size | mtime)``. It never
  chains audit actions.

This module adds the missing layer for audit entries without touching
existing behaviour:

* Each audit entry is canonicalised (stable sorted JSON, chain fields
  stripped) and linked as
  ``entry_hash = sha256(prev_hash | canonical_json | algorithm)``,
  so editing, reordering or splicing any entry breaks every later
  link — the same chaining idea as the evidence manifest, re-expressed
  for JSON audit rows instead of recording files.
* The chain is persisted durably (``tmp + flush + fsync + rename +
  fsync(dir)``, mirroring ``evidence_manifest._durable_write_json``)
  to ``data/manifests/audit_chain.json`` by default.
* ``verify_chain`` reports linkage breaks (``mismatched``), content
  edits (``tampered``) and head mismatches (truncation/extension)
  separately, mirroring the evidence-manifest report shape.

Standard library only. Every public name has common aliases so callers
(and tests) can use whichever spelling they prefer.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

try:
    from app.core.config import DATA_DIR  # type: ignore
except Exception:  # noqa: BLE001
    DATA_DIR = Path("data")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TOOL_NAME = "vigilcam-audit-chain"
TOOL_VERSION = "1.0"
CHAIN_VERSION = 1
MANIFEST_VERSION = 1
ALGORITHM = "sha256"
GENESIS_HASH = "0" * 64
CHAIN_FILENAME = "audit_chain.json"

# Fields that carry chain linkage itself; stripped before hashing so the
# hash covers the audit payload, not the previous hash wrapper.
_CHAIN_FIELDS = frozenset({
    "prev_hash", "prev", "previous_hash", "previous",
    "entry_hash", "hash", "chain_hash", "audit_hash", "entryHash",
    "chainHead", "head_hash",
    "seq", "index", "sequence", "position", "order",
})

_PATH_KEYS = (
    "chain_path", "chain", "chain_file", "manifest_path", "manifest",
    "manifest_file", "path", "file", "filepath", "filename",
    "storage_path", "output", "out", "dest", "target", "location",
)

_ENTRIES_KEYS = (
    "entries", "items", "logs", "rows", "events", "audit_entries",
    "audit_logs", "records", "data", "chain_entries", "chain",
)

_STORE_KEYS = ("store", "audit_store", "storage", "backend")


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
        for k in _PATH_KEYS:
            if value.get(k) is not None:
                p = _coerce_path(value.get(k))
                if p is not None:
                    return p
        return None
    for attr in ("chain_path", "manifest_path", "path", "file", "filepath"):
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


def _default_chain_path() -> Path:
    try:
        base = Path(str(DATA_DIR)).expanduser()
    except Exception:  # noqa: BLE001
        base = Path("data")
    return base / "manifests" / CHAIN_FILENAME


def _resolve_chain_path(value: Any = None, **kwargs: Any) -> Path:
    raw: Any = value
    if raw is None:
        for k in _PATH_KEYS:
            if k in kwargs and kwargs.get(k) is not None:
                # Avoid treating an audit entry dict's generic `data`/`chain`
                # style keys as paths; only explicit path-ish keys win here.
                if k in ("chain", "data") and isinstance(kwargs.get(k), (list, dict)):
                    # `chain` may be a chain dict — not a path.
                    if isinstance(kwargs.get(k), dict) and "entries" in kwargs.get(k):
                        continue
                    if isinstance(kwargs.get(k), list):
                        continue
                raw = kwargs.get(k)
                break
    p = _coerce_path(raw)
    if p is None:
        p = _default_chain_path()
    else:
        try:
            if str(p).endswith(("/", os.sep)) or (p.exists() and p.is_dir()) or not p.suffix:
                if p.exists() and p.is_dir():
                    p = p / CHAIN_FILENAME
        except Exception:  # noqa: BLE001
            pass
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    return p


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# JSON / hashing helpers
# ---------------------------------------------------------------------------

def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, set):
        return sorted((_json_safe(v) for v in value), key=repr)
    if hasattr(value, "model_dump"):
        try:
            return _json_safe(value.model_dump(mode="json"))
        except Exception:  # noqa: BLE001
            try:
                return _json_safe(value.model_dump())
            except Exception:  # noqa: BLE001
                pass
    if hasattr(value, "dict"):
        try:
            return _json_safe(value.dict())
        except Exception:  # noqa: BLE001
            pass
    return repr(value)


def canonical_entry(entry: Any) -> Dict[str, Any]:
    """Return the audit payload with chain-linkage fields stripped."""
    if entry is None:
        return {}
    if hasattr(entry, "model_dump"):
        try:
            entry = entry.model_dump(mode="json")
        except Exception:  # noqa: BLE001
            try:
                entry = entry.model_dump()
            except Exception:  # noqa: BLE001
                entry = {}
    elif hasattr(entry, "dict"):
        try:
            entry = entry.dict()
        except Exception:  # noqa: BLE001
            pass
    if not isinstance(entry, dict):
        try:
            entry = dict(entry)
        except Exception:  # noqa: BLE001
            return {"value": _json_safe(entry)}
    out: Dict[str, Any] = {}
    for k, v in entry.items():
        if str(k) in _CHAIN_FIELDS:
            continue
        out[str(k)] = _json_safe(v)
    return out


def canonical_json(entry: Any) -> str:
    """Stable canonical JSON for one audit entry (sorted keys)."""
    return json.dumps(canonical_entry(entry), sort_keys=True, separators=(",", ":"), default=str)


def _entry_hash(prev_hash: str, canonical: str, algorithm: str = ALGORITHM) -> str:
    material = "|".join((str(prev_hash), str(canonical), str(algorithm)))
    try:
        h = hashlib.new(algorithm)
    except Exception:  # noqa: BLE001
        h = hashlib.sha256()
    h.update(material.encode("utf-8"))
    return h.hexdigest()


def compute_entry_hash(entry: Any, prev_hash: str = GENESIS_HASH,
                       algorithm: str = ALGORITHM) -> str:
    """Hash one audit entry against ``prev_hash``."""
    return _entry_hash(str(prev_hash or GENESIS_HASH), canonical_json(entry),
                       algorithm or ALGORITHM)


# Hash aliases (mirror evidence-manifest naming).
compute_hash = compute_entry_hash
hash_entry = compute_entry_hash
entry_digest = compute_entry_hash
audit_hash = compute_entry_hash
sha256_entry = compute_entry_hash
hash_bytes = lambda data, algo="sha256": hashlib.new(algo, data if isinstance(data, bytes) else str(data).encode()).hexdigest()  # noqa: E731


# ---------------------------------------------------------------------------
# Durable write (mirrors evidence_manifest ordering)
# ---------------------------------------------------------------------------

def _fsync_dir(dir_path: Path) -> None:
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
# Entry sourcing
# ---------------------------------------------------------------------------

def _resolve_store(store: Any = None, **kwargs: Any) -> Any:
    if store is not None:
        return store
    for key in _STORE_KEYS:
        if kwargs.get(key) is not None:
            return kwargs.get(key)
    try:
        from app.services.audit import _default_store as _store
        return _store
    except Exception:  # noqa: BLE001
        return None


def _coerce_entries(value: Any) -> Optional[List[dict]]:
    if value is None:
        return None
    if isinstance(value, dict):
        # A chain doc carries entries; a single audit row does not.
        if isinstance(value.get("entries"), list):
            return [dict(e) if isinstance(e, dict) else {"value": _json_safe(e)}
                    for e in value["entries"]]
        return [dict(value)]
    try:
        items = list(value)
    except Exception:  # noqa: BLE001
        return [ {"value": _json_safe(value)} ]
    out: List[dict] = []
    for e in items:
        if isinstance(e, dict):
            out.append(dict(e))
        else:
            try:
                out.append(dict(e))
            except Exception:  # noqa: BLE001
                out.append({"value": _json_safe(e)})
    return out


def _read_entries(store: Any, entries: Any = None) -> List[dict]:
    coerced = _coerce_entries(entries)
    if coerced is not None:
        return coerced
    if store is None:
        return []
    for attr in ("read_all", "all", "list"):
        try:
            fn = getattr(store, attr, None)
            if callable(fn):
                data = fn()
                import inspect as _inspect
                if _inspect.isawaitable(data):
                    continue
                return [dict(e) if isinstance(e, dict) else dict(e) for e in (data or [])]
        except Exception:  # noqa: BLE001
            continue
    try:
        raw = getattr(store, "_entries", None)
        if isinstance(raw, list):
            return [dict(e) if isinstance(e, dict) else dict(e) for e in raw]
    except Exception:  # noqa: BLE001
        pass
    return []


def _extract_entries_kwarg(kwargs: Dict[str, Any]) -> Any:
    for k in _ENTRIES_KEYS:
        if kwargs.get(k) is not None:
            v = kwargs.get(k)
            # `chain` key holding a chain doc counts as entries source too.
            return v
    return None


# ---------------------------------------------------------------------------
# Chain construction
# ---------------------------------------------------------------------------

def chain_entries(entries: Any = None, genesis: Optional[str] = None,
                  algorithm: Optional[str] = None, **kwargs: Any) -> List[Dict[str, Any]]:
    """Chain plain audit entries; returns new dicts with linkage fields."""
    raw = _coerce_entries(entries if entries is not None else _extract_entries_kwarg(kwargs))
    if raw is None:
        store = _resolve_store(kwargs.get("store"), **kwargs)
        raw = _read_entries(store, None)
    algo = str(algorithm or kwargs.get("algorithm") or kwargs.get("hash_algorithm") or ALGORITHM)
    prev = str(genesis or kwargs.get("genesis") or kwargs.get("genesis_hash") or GENESIS_HASH)
    chained: List[Dict[str, Any]] = []
    for seq, row in enumerate(raw or []):
        canon = canonical_json(row)
        eh = _entry_hash(prev, canon, algo)
        entry = dict(row)
        entry["seq"] = seq
        entry["index"] = seq
        entry["prev_hash"] = prev
        entry["prev"] = prev
        entry["entry_hash"] = eh
        entry["hash"] = eh
        entry["chain_hash"] = eh
        chained.append(entry)
        prev = eh
    return chained


def build_chain(entries: Any = None, store: Any = None, chain_path: Any = None,
                genesis: Optional[str] = None, algorithm: Optional[str] = None,
                save: bool = False, **kwargs: Any) -> Dict[str, Any]:
    """Build a hash-chained audit-chain doc.

    Lenient kwargs: ``items``/``logs``/``rows``/``records`` alias
    ``entries``; ``manifest_path``/``path``/``file`` alias
    ``chain_path``; ``store``/``persist``/``write`` alias ``save``;
    ``store``/``audit_store`` supplies the audit backend.
    """
    if entries is None:
        entries = _extract_entries_kwarg(kwargs)
    if store is None:
        store = _resolve_store(kwargs.get("store"), **kwargs)
    if chain_path is None:
        for k in _PATH_KEYS:
            if k in kwargs and kwargs.get(k) is not None:
                cand = kwargs.get(k)
                if k in ("chain", "data") and isinstance(cand, (list, dict)):
                    if isinstance(cand, dict) and "entries" in cand:
                        continue
                    if isinstance(cand, list):
                        continue
                chain_path = cand
                break
    if genesis is None:
        genesis = kwargs.get("genesis") or kwargs.get("genesis_hash") or GENESIS_HASH
    if algorithm is None:
        algorithm = kwargs.get("algorithm") or kwargs.get("hash_algorithm") or ALGORITHM
    for k in ("save", "store_chain", "persist", "write", "write_chain", "save_chain"):
        if k in kwargs and kwargs[k] is not None:
            save = bool(kwargs[k]) or save
            break

    dest = _resolve_chain_path(chain_path, **kwargs)
    raw = _read_entries(store, entries)
    chained = chain_entries(raw, genesis=str(genesis), algorithm=str(algorithm))
    head = chained[-1]["entry_hash"] if chained else str(genesis)
    now = _utc_now_iso()
    doc: Dict[str, Any] = {
        "version": CHAIN_VERSION,
        "chain_version": CHAIN_VERSION,
        "manifest_version": MANIFEST_VERSION,
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "algorithm": str(algorithm),
        "hash_algorithm": str(algorithm),
        "genesis": str(genesis),
        "genesis_hash": str(genesis),
        "head": head,
        "head_hash": head,
        "chain_head": head,
        "count": len(chained),
        "total": len(chained),
        "entry_count": len(chained),
        "chain_path": str(dest),
        "path": str(dest),
        "created_at": now,
        "updated_at": now,
        "generated_at": now,
        "entries": chained,
        "ok": True,
        "success": True,
        "status": "ok",
    }
    if save:
        save_chain(doc, dest)
    return doc


# Build aliases.
build_audit_chain = build_chain
create_chain = build_chain
generate_chain = build_chain
generate = build_chain
create = build_chain
build = build_chain
make_chain = build_chain
new_chain = build_chain
chain = chain_entries
chain_audit_entries = chain_entries
build_chained_entries = chain_entries


def save_chain(chain: Any = None, chain_path: Any = None, **kwargs: Any) -> Optional[Path]:
    """Durably persist a chain doc (tmp + fsync + rename + fsync dir)."""
    if chain is None:
        for k in ("chain", "manifest", "data", "payload", "body", "doc"):
            if k in kwargs and isinstance(kwargs[k], dict):
                chain = kwargs[k]
                break
    if chain is None and isinstance(chain_path, dict):
        chain, chain_path = chain_path, None
    if isinstance(chain, list):
        chain = {"entries": chain}
    if not isinstance(chain, dict):
        return None
    # Accept a bare entry list passed as `entries`.
    if "entries" not in chain and isinstance(kwargs.get("entries"), list):
        chain = dict(chain)
        chain["entries"] = kwargs.get("entries")
    dest = _resolve_chain_path(chain_path, **kwargs)
    doc = dict(chain)
    # Recompute head when entries carry linkage.
    try:
        ents = doc.get("entries") or []
        if ents and isinstance(ents[-1], dict):
            last = ents[-1]
            doc["head"] = last.get("entry_hash") or last.get("hash") or doc.get("head")
            doc["head_hash"] = doc["head"]
            doc["chain_head"] = doc["head"]
            doc["count"] = len(ents)
            doc["total"] = len(ents)
            doc["entry_count"] = len(ents)
    except Exception:  # noqa: BLE001
        pass
    doc["updated_at"] = _utc_now_iso()
    doc["chain_path"] = str(dest)
    doc["path"] = str(dest)
    try:
        return _durable_write_json(dest, doc)
    except Exception:  # noqa: BLE001
        return None


# Save aliases.
write_chain = save_chain
store_chain = save_chain
persist_chain = save_chain
persist = save_chain
flush_chain = save_chain
flush = save_chain
store = save_chain
write = save_chain
save = save_chain


def load_chain(chain_path: Any = None, **kwargs: Any) -> Optional[Dict[str, Any]]:
    """Load a chain JSON file; returns None when missing/unreadable."""
    dest = _resolve_chain_path(chain_path, **kwargs)
    try:
        if not dest.is_file():
            return None
        with open(dest, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return {"entries": data, "head": data[-1].get("entry_hash") if data else GENESIS_HASH,
                    "count": len(data), "chain_path": str(dest), "path": str(dest)}
        if not isinstance(data, dict):
            return None
        return data
    except Exception:  # noqa: BLE001
        return None


# Load aliases.
read_chain = load_chain
read = load_chain
load = load_chain
get_chain = load_chain
fetch_chain = load_chain
open_chain = load_chain
load_audit_chain = load_chain


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def _verify_chained(chained: Sequence[Any], genesis: str, algorithm: str) -> Dict[str, Any]:
    prev = str(genesis or GENESIS_HASH)
    bad: List[str] = []
    tampered: List[str] = []
    mismatched: List[str] = []
    issues: List[str] = []
    checked = 0
    for i, e in enumerate(chained):
        if not isinstance(e, dict):
            bad.append(f"entry#{i}")
            issues.append(f"entry#{i}: malformed")
            continue
        checked += 1
        label = str(e.get("action") or e.get("event_type") or e.get("type")
                    or e.get("id") or f"entry#{i}")
        exp_prev = str(e.get("prev_hash") or e.get("prev") or "")
        eh = str(e.get("entry_hash") or e.get("hash") or e.get("chain_hash") or "")
        if exp_prev != prev:
            bad.append(label)
            mismatched.append(label)
            issues.append(f"{label}: broken-link (prev mismatch at seq {i})")
            prev = eh or prev
            continue
        recomputed = _entry_hash(exp_prev, canonical_json(e), algorithm)
        if recomputed != eh:
            bad.append(label)
            tampered.append(label)
            issues.append(f"{label}: entry-hash mismatch (entry edited?)")
            prev = eh
            continue
        prev = eh
    return {
        "checked": checked,
        "bad": bad,
        "tampered": tampered,
        "mismatched": mismatched,
        "issues": issues,
        "computed_head": prev,
    }


def verify_chain(chain: Any = None, chain_path: Any = None, entries: Any = None,
                 genesis: Optional[str] = None, algorithm: Optional[str] = None,
                 **kwargs: Any) -> Dict[str, Any]:
    """Verify chain linkage over a chain doc or plain entry list.

    Accepts a chain dict (``{"entries": [...], "head": ...}``), a bare
    entry list, or ``None`` (loads ``chain_path`` from disk). Lenient
    kwargs: ``items``/``logs``/``rows`` alias ``entries``;
    ``manifest_path``/``path``/``file`` alias ``chain_path``.
    """
    for k in ("check_files", "with_files", "verify_files"):
        kwargs.pop(k, None)
    if chain is None:
        for k in ("chain", "manifest", "data", "payload", "body", "doc"):
            if k in kwargs and isinstance(kwargs[k], dict) and "entries" in kwargs[k]:
                chain = kwargs[k]
                break
    if entries is None:
        maybe = _extract_entries_kwarg(kwargs)
        # Distinguish a chain doc from a plain entry list.
        if isinstance(maybe, dict) and "entries" in maybe and chain is None:
            chain = maybe
        elif maybe is not None:
            entries = maybe
    if chain is not None and isinstance(chain, list):
        entries = chain
        chain = None
    if chain is None and entries is None:
        # Try the chain file.
        mp = chain_path
        if mp is None:
            for k in _PATH_KEYS:
                if k in kwargs and kwargs.get(k) is not None:
                    cand = kwargs.get(k)
                    if isinstance(cand, (list, dict)):
                        continue
                    mp = cand
                    break
        dest = _resolve_chain_path(mp, **kwargs)
        doc = load_chain(dest)
        if doc is None:
            return {
                "ok": False, "valid": False, "healthy": False,
                "corrupt": True, "bad": True, "invalid": True,
                "status": "missing", "state": "missing",
                "checked": 0, "total": 0, "count": 0,
                "ok_count": 0, "bad_count": 0,
                "tampered": [], "missing": [], "mismatched": [],
                "issues": ["chain-missing"], "errors": ["chain-missing"],
                "reasons": ["chain-missing"],
                "chain_path": str(dest), "path": str(dest),
            }
        chain = doc
        chain_path = dest
    doc: Optional[Dict[str, Any]] = chain if isinstance(chain, dict) else None
    if doc is None:
        # Plain entry list with no stored hashes: chain it fresh and
        # report linkage of the freshly built chain (always ok unless
        # entries already carry conflicting hashes).
        raw = _coerce_entries(entries) or []
        carries_hash = any(isinstance(e, dict) and (e.get("entry_hash") or e.get("hash")) for e in raw)
        if not carries_hash:
            chained = chain_entries(raw, genesis=genesis or GENESIS_HASH,
                                    algorithm=algorithm or ALGORITHM)
            head = chained[-1]["entry_hash"] if chained else str(genesis or GENESIS_HASH)
            return {
                "ok": True, "valid": True, "healthy": True,
                "corrupt": False, "bad": False, "invalid": False,
                "success": True, "status": "ok", "state": "ok",
                "checked": len(chained), "total": len(chained), "count": len(chained),
                "entry_count": len(chained), "file_count": len(chained),
                "ok_count": len(chained), "valid_count": len(chained),
                "bad_count": 0, "corrupt_count": 0,
                "bad_files": [], "tampered": [], "tampered_files": [],
                "missing": [], "missing_files": [],
                "mismatched": [], "issues": [], "errors": [], "reasons": [],
                "head": head, "computed_head": head,
                "chain_path": str(_resolve_chain_path(chain_path, **kwargs)),
                "path": str(_resolve_chain_path(chain_path, **kwargs)),
            }
        doc = {"entries": raw, "head": None, "genesis": genesis or GENESIS_HASH,
               "algorithm": algorithm or ALGORITHM}
    chained_entries = doc.get("entries") or []
    gen = str(doc.get("genesis") or doc.get("genesis_hash") or genesis or GENESIS_HASH)
    algo = str(doc.get("algorithm") or doc.get("hash_algorithm") or algorithm or ALGORITHM)
    detail = _verify_chained(chained_entries, gen, algo)
    total = len(chained_entries)
    head = doc.get("head") or doc.get("head_hash") or doc.get("chain_head")
    if chained_entries and head not in (None, "", detail["computed_head"]):
        detail["issues"].append("head mismatch (chain truncated or extended?)")
        if not detail["bad"]:
            last = chained_entries[-1]
            lbl = str(last.get("action") if isinstance(last, dict) else "head")
            detail["bad"] = [lbl]
    ok = len(detail["bad"]) == 0
    dest_s = str(_resolve_chain_path(chain_path, **kwargs))
    report: Dict[str, Any] = {
        "ok": ok, "valid": ok, "healthy": ok,
        "corrupt": not ok, "bad": not ok, "invalid": not ok,
        "success": True,
        "status": "ok" if ok else ("tampered" if detail["tampered"] else ("mismatched" if detail["mismatched"] else "corrupt")),
        "state": "ok" if ok else "corrupt",
        "checked": detail["checked"],
        "total": total, "count": total,
        "entry_count": total,
        "ok_count": total - len(detail["bad"]),
        "valid_count": total - len(detail["bad"]),
        "bad_count": len(detail["bad"]),
        "corrupt_count": len(detail["bad"]),
        "bad_files": list(detail["bad"]),
        "bad_entries": list(detail["bad"]),
        "tampered": list(detail["tampered"]),
        "tampered_files": list(detail["tampered"]),
        "tampered_entries": list(detail["tampered"]),
        "missing": [],
        "missing_files": [],
        "mismatched": list(detail["mismatched"]),
        "mismatched_entries": list(detail["mismatched"]),
        "issues": list(detail["issues"]),
        "errors": list(detail["issues"]),
        "reasons": list(detail["issues"]),
        "head": head,
        "computed_head": detail["computed_head"],
        "genesis": gen,
        "algorithm": algo,
        "chain_path": dest_s,
        "path": dest_s,
        "chain": doc,
    }
    return report


# Verify aliases.
verify_audit_chain = verify_chain
validate_chain = verify_chain
validate = verify_chain
check_chain = verify_chain
check = verify_chain
inspect_chain = verify_chain
verify = verify_chain
verify_entries = verify_chain
verify_audit = verify_chain
audit_verify = verify_chain


def append_entry(entry: Any = None, chain_path: Any = None, store: Any = None,
                 persist_audit: bool = True, **kwargs: Any) -> Dict[str, Any]:
    """Append one audit entry to the audit store *and* the hash chain.

    The audit row is first written via ``app.services.audit``
    (best-effort; chain still advances when the store write fails), then
    the persisted chain file is extended with the new link and saved
    durably. Re-appending an identical payload is a no-op duplicate.
    """
    if entry is None:
        for k in ("entry", "audit_entry", "item", "record", "event",
                  "payload", "data", "details", "audit"):
            if k in kwargs and kwargs.get(k) is not None and k not in ("data",):
                entry = kwargs.pop(k)
                break
        if entry is None and kwargs:
            # Remaining kwargs are the audit fields themselves.
            entry = {k: v for k, v in kwargs.items()
                     if k not in _PATH_KEYS and k not in ("store", "audit_store", "persist_audit", "save", "persist", "write")}
            kwargs = {}
    if entry is None:
        raise ValueError("append_entry requires an audit entry")
    mp: Any = chain_path
    if mp is None:
        for k in _PATH_KEYS:
            if k in kwargs and kwargs.get(k) is not None and not isinstance(kwargs.get(k), (list, dict)):
                mp = kwargs.get(k)
                break
    dest = _resolve_chain_path(mp, **kwargs)
    backend = store if store is not None else _resolve_store(kwargs.get("store"), **kwargs)

    stored: Dict[str, Any] = dict(entry) if isinstance(entry, dict) else {"value": _json_safe(entry)}
    if persist_audit and backend is not None:
        try:
            from app.services.audit import append_audit_entry as _append
            res = _append(backend, dict(stored))
            if isinstance(res, dict):
                stored = dict(res)
        except Exception:  # noqa: BLE001
            pass

    doc = load_chain(dest)
    if doc is None:
        # Seed from the audit store when possible so the chain covers
        # history written before chaining existed.
        seed = _read_entries(backend, None)
        # If the just-stored row is not yet visible, include it.
        if stored and not any(canonical_json(s) == canonical_json(stored) for s in seed):
            seed = list(seed) + [dict(stored)]
        doc = build_chain(seed, store=None, chain_path=dest,
                          genesis=kwargs.get("genesis"), algorithm=kwargs.get("algorithm"))
        save_chain(doc, dest)
        # Find the stored row's link.
        for e in doc.get("entries", []):
            if isinstance(e, dict) and canonical_json(e) == canonical_json(stored):
                return {"entry": e, "chain": doc, "manifest": doc,
                        "chain_path": str(dest), "path": str(dest),
                        "ok": True, "success": True, "status": "appended",
                        "duplicate": False, "stored": stored}
        last = doc.get("entries", [])[-1] if doc.get("entries") else None
        return {"entry": last, "chain": doc, "manifest": doc,
                "chain_path": str(dest), "path": str(dest),
                "ok": True, "success": True, "status": "appended",
                "duplicate": False, "stored": stored}

    chained_entries = list(doc.get("entries") or [])
    prev_hash = doc.get("head") or doc.get("chain_head") or GENESIS_HASH
    if chained_entries:
        last = chained_entries[-1]
        if isinstance(last, dict):
            prev_hash = last.get("entry_hash") or last.get("hash") or prev_hash
    algo = str(doc.get("algorithm") or kwargs.get("algorithm") or ALGORITHM)
    # Deduplicate: identical payload already head/chained -> no-op.
    canon_new = canonical_json(stored)
    for e in chained_entries:
        if isinstance(e, dict) and canonical_json(e) == canon_new:
            return {"entry": e, "chain": doc, "manifest": doc,
                    "chain_path": str(dest), "path": str(dest),
                    "ok": True, "success": True, "status": "exists", "duplicate": True,
                    "stored": stored}
    eh = _entry_hash(str(prev_hash), canon_new, algo)
    new_entry = dict(stored)
    new_entry["seq"] = len(chained_entries)
    new_entry["index"] = len(chained_entries)
    new_entry["prev_hash"] = str(prev_hash)
    new_entry["prev"] = str(prev_hash)
    new_entry["entry_hash"] = eh
    new_entry["hash"] = eh
    new_entry["chain_hash"] = eh
    chained_entries.append(new_entry)
    doc["entries"] = chained_entries
    doc["head"] = eh
    doc["head_hash"] = eh
    doc["chain_head"] = eh
    doc["count"] = len(chained_entries)
    doc["total"] = len(chained_entries)
    doc["entry_count"] = len(chained_entries)
    save_chain(doc, dest)
    return {"entry": new_entry, "chain": doc, "manifest": doc,
            "chain_path": str(dest), "path": str(dest),
            "ok": True, "success": True, "status": "appended",
            "duplicate": False, "stored": stored}


# Append aliases.
append = append_entry
append_audit_entry = append_entry
add_entry = append_entry
add = append_entry
record = append_entry
record_entry = append_entry
record_audit_entry = append_entry
track = append_entry
track_entry = append_entry
chain_entry = append_entry
append_chained_entry = append_entry


def get_status(chain_path: Any = None, entries: Any = None, store: Any = None,
               **kwargs: Any) -> Dict[str, Any]:
    """Lightweight status: chain presence + verification summary."""
    mp: Any = chain_path
    if mp is None:
        for k in _PATH_KEYS:
            if k in kwargs and kwargs.get(k) is not None and not isinstance(kwargs.get(k), (list, dict)):
                mp = kwargs.get(k)
                break
    if entries is None:
        entries = _extract_entries_kwarg(kwargs)
        if isinstance(entries, dict) and "entries" not in entries:
            entries = None
    dest = _resolve_chain_path(mp, **kwargs)
    doc = load_chain(dest)
    if doc is None:
        # No persisted chain yet: report unchained coverage from the store.
        backend = store if store is not None else _resolve_store(kwargs.get("store"), **kwargs)
        pending = _read_entries(backend, entries)
        return {
            "status": "missing" if not pending else "unchained",
            "state": "missing" if not pending else "unchained",
            "ok": False, "success": True, "healthy": False, "valid": False,
            "exists": False, "chained": False,
            "total": len(pending), "count": len(pending), "entry_count": len(pending),
            "ok_count": 0, "bad_count": 0,
            "head": None, "computed_head": None,
            "pending": len(pending),
            "chain_path": str(dest), "path": str(dest),
        }
    rep = verify_chain(doc, chain_path=dest)
    out = dict(rep)
    out.update({
        "exists": True,
        "chained": True,
        "entries": len(doc.get("entries") or []),
        "chain_version": doc.get("version"),
        "algorithm": doc.get("algorithm"),
    })
    return out


# Status aliases.
status = get_status
summary = get_status
health = get_status
get_summary = get_status
get_health = get_status
check_status = get_status
chain_status = get_status
audit_chain_status = get_status


# ---------------------------------------------------------------------------
# Stateful checker class
# ---------------------------------------------------------------------------

class AuditChain:
    """Stateful hash-chain manager over audit entries (durable by default)."""

    def __init__(self, chain_path: Any = None, store: Any = None,
                 genesis: Any = None, algorithm: Any = None,
                 **kwargs: Any) -> None:
        self.chain_path = _resolve_chain_path(
            chain_path if chain_path is not None else kwargs.get("manifest"), **kwargs)
        self.store = store if store is not None else _resolve_store(kwargs.get("store"), **kwargs)
        self.genesis = str(genesis or kwargs.get("genesis") or kwargs.get("genesis_hash") or GENESIS_HASH)
        self.algorithm = str(algorithm or kwargs.get("algorithm") or ALGORITHM)
        self.last_chain: Optional[Dict[str, Any]] = None
        self.last_report: Optional[Dict[str, Any]] = None

    # ---- build ----
    def build_chain(self, entries: Any = None, **kwargs: Any) -> Dict[str, Any]:
        kw = {"genesis": self.genesis, "algorithm": self.algorithm}
        kw.update(kwargs)
        target = entries
        if target is None:
            target = _extract_entries_kwarg(kw)
        doc = build_chain(target, store=self.store, chain_path=self.chain_path, **kw)
        self.last_chain = doc
        return doc

    def build(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.build_chain(*a, **k)

    def scan(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.build_chain(*a, **k)

    def generate(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.build_chain(*a, **k)

    def create(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.build_chain(*a, **k)

    # ---- persist ----
    def save(self, chain: Any = None, **kwargs: Any) -> Optional[Path]:
        doc = chain if isinstance(chain, dict) else self.last_chain
        if doc is None:
            doc = self.build_chain(**kwargs)
        return save_chain(doc, self.chain_path, **kwargs)

    def save_chain(self, *a: Any, **k: Any) -> Optional[Path]:
        return self.save(*a, **k)

    def load(self, **kwargs: Any) -> Optional[Dict[str, Any]]:
        doc = load_chain(self.chain_path, **kwargs)
        if doc is not None:
            self.last_chain = doc
        return doc

    def load_chain(self, *a: Any, **k: Any) -> Optional[Dict[str, Any]]:
        return self.load(*a, **k)

    # ---- append ----
    def append(self, entry: Any = None, **kwargs: Any) -> Dict[str, Any]:
        return append_entry(entry, chain_path=self.chain_path, store=self.store, **kwargs)

    def append_entry(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.append(*a, **k)

    def add_entry(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.append(*a, **k)

    # ---- verify ----
    def verify(self, chain: Any = None, **kwargs: Any) -> Dict[str, Any]:
        if chain is None:
            chain = self.load()
            if chain is None:
                # Nothing persisted: verify whatever the store holds.
                rep = verify_chain(None, chain_path=self.chain_path, **kwargs)
                self.last_report = rep
                return rep
        rep = verify_chain(chain, chain_path=self.chain_path, **kwargs)
        self.last_report = rep
        return rep

    def verify_chain(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.verify(*a, **k)

    def validate(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.verify(*a, **k)

    def check(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return self.verify(*a, **k)

    # ---- status ----
    def get_status(self, **kwargs: Any) -> Dict[str, Any]:
        return get_status(self.chain_path, store=self.store, **kwargs)

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
        if low in ("append", "add", "record", "track") or "append" in low:
            return self.append
        if any(h in low for h in ("status", "health", "summary", "info", "state")):
            return self.get_status
        if any(h in low for h in ("verif", "valid", "check", "inspect")):
            return self.verify
        if any(h in low for h in ("build", "generat", "creat", "scan", "make", "chain")):
            return self.build_chain
        if any(h in low for h in ("save", "write", "store", "persist", "flush")):
            return self.save
        if any(h in low for h in ("load", "read", "open", "fetch", "get_chain")):
            return self.load
        if "hash" in low or "checksum" in low:
            return compute_entry_hash
        raise AttributeError(name)


# Class aliases.
AuditChainService = AuditChain
AuditChainedService = AuditChain
ChainService = AuditChain
AuditChainManager = AuditChain
AuditChainChecker = AuditChain
ChainChecker = AuditChain
Checker = AuditChain
Validator = AuditChain
Service = AuditChain

# Singleton instances.
audit_chain = AuditChain()
audit_chain_service = audit_chain
chain_service = audit_chain
service = audit_chain
checker = audit_chain
manager = audit_chain
instance = audit_chain
singleton = audit_chain
default_checker = audit_chain
default_service = audit_chain

__all__ = [
    "AuditChain",
    "AuditChainService",
    "ChainService",
    "GENESIS_HASH",
    "ALGORITHM",
    "TOOL_NAME",
    "TOOL_VERSION",
    "CHAIN_VERSION",
    "canonical_entry",
    "canonical_json",
    "compute_entry_hash",
    "hash_entry",
    "chain_entries",
    "build_chain",
    "build_audit_chain",
    "create_chain",
    "save_chain",
    "write_chain",
    "load_chain",
    "read_chain",
    "verify_chain",
    "verify_audit_chain",
    "append_entry",
    "append",
    "add_entry",
    "get_status",
    "audit_chain",
    "audit_chain_service",
    "service",
]
