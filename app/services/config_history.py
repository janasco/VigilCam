"""Camera config history service.

Versioned snapshots of per-camera configuration with field-level diffs.

This complements :mod:`app.services.audit` (which logs *actions* as flat
events) and :mod:`app.services.config_io` (which exports the whole system
config as a JSON document): ``config_history`` keeps an append-only,
per-camera version chain so operators can answer "what changed on camera X
and when?" and diff any two versions.

All helpers are sync and dependency-free (in-memory store) so they work in
tests without a database. No ``app/main.py`` changes are required: the API
module auto-registers.

Entry shape::

    {
      "id": "<uuid>",
      "camera_id": "1",
      "version": 3,            # per-camera monotonically increasing
      "config": {...},         # normalized JSON-safe snapshot
      "config_hash": "<sha1>",
      "changes": [...],        # diff vs previous version (empty for v1)
      "changed_fields": ["fps", "name"],
      "actor": ...,
      "reason": ...,
      "created_at": "2026-...",
    }

Diff item shape::

    {"path": "fps", "op": "changed", "old": 15, "new": 30}
    {"path": "watermark", "op": "added", "old": None, "new": "x"}
    {"path": "password", "op": "removed", "old": "s", "new": None}
"""

from __future__ import annotations

import copy
import hashlib
import json
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

CONFIG_HISTORY: Dict[str, "ConfigHistoryEntry"] = {}
config_history = CONFIG_HISTORY
history = CONFIG_HISTORY
HISTORY = CONFIG_HISTORY
STORE = CONFIG_HISTORY
ENTRIES = CONFIG_HISTORY
CONFIG_HISTORY_STORE = CONFIG_HISTORY

# Per-camera version counters + ordering index.
_VERSION_COUNTERS: Dict[str, int] = {}
_CAMERA_INDEX: Dict[str, List[str]] = {}

_lock = threading.Lock()
_jobs_lock = _lock


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        except Exception:
            try:
                return _json_safe(value.model_dump())
            except Exception:
                pass
    if hasattr(value, "dict"):
        try:
            return _json_safe(value.dict())
        except Exception:
            pass
    table = getattr(value, "__table__", None)
    if table is not None and hasattr(table, "columns"):
        try:
            out: Dict[str, Any] = {}
            for col in table.columns:  # type: ignore[union-attr]
                try:
                    out[col.name] = _json_safe(getattr(value, col.name, None))
                except Exception:
                    pass
            return out
        except Exception:
            pass
    raw = getattr(value, "__dict__", None)
    if isinstance(raw, dict):
        return {k: _json_safe(v) for k, v in raw.items() if not k.startswith("_sa_") and not k.startswith("_")}
    return repr(value)


def _normalize_config(config: Any) -> Dict[str, Any]:
    """Coerce flexible config inputs into a JSON-safe dict."""
    if config is None:
        return {}
    if hasattr(config, "model_dump"):
        try:
            data = config.model_dump(mode="json")
        except Exception:
            try:
                data = config.model_dump()
            except Exception:
                data = {}
        if isinstance(data, dict):
            return _json_safe(data)  # type: ignore[return-value]
        return {}
    if hasattr(config, "dict") and not isinstance(config, dict):
        try:
            data = config.dict()
            if isinstance(data, dict):
                return _json_safe(data)  # type: ignore[return-value]
        except Exception:
            pass
    table = getattr(config, "__table__", None)
    if table is not None:
        return _json_safe(config)  # type: ignore[return-value]
    if isinstance(config, dict):
        data = dict(config)
        # Unwrap common single-key wrappers.
        for _ in range(2):
            if len(data) == 1:
                only = next(iter(data))
                if only in ("config", "camera", "settings", "data", "body", "payload") and isinstance(data[only], dict):
                    data = dict(data[only])
                    continue
            break
        out: Dict[str, Any] = {}
        for k, v in data.items():
            if k.startswith("_sa_") or k == "_sa_instance_state":
                continue
            out[str(k)] = _json_safe(v)
        return out
    try:
        return _json_safe(dict(config))  # type: ignore[arg-type]
    except Exception:
        return {"value": _json_safe(config)}


def _config_hash(config: Dict[str, Any]) -> str:
    try:
        text = json.dumps(config, sort_keys=True, default=str)
    except Exception:
        text = repr(sorted(config.items()))
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def diff_configs(old: Any, new: Any, prefix: str = "") -> List[Dict[str, Any]]:
    """Recursively diff two JSON-safe mappings.

    Returns a list of ``{"path", "op", "old", "new"}`` where ``op`` is one
    of ``added`` / ``removed`` / ``changed``.
    """
    old_n = _normalize_config(old) if not isinstance(old, dict) else _json_safe(old)
    new_n = _normalize_config(new) if not isinstance(new, dict) else _json_safe(new)
    if not isinstance(old_n, dict):
        old_n = {"value": old_n}
    if not isinstance(new_n, dict):
        new_n = {"value": new_n}
    changes: List[Dict[str, Any]] = []
    for key in sorted(set(old_n.keys()) | set(new_n.keys())):
        path = f"{prefix}.{key}" if prefix else str(key)
        in_old = key in old_n
        in_new = key in new_n
        if not in_old:
            changes.append({"path": path, "op": "added", "old": None, "new": new_n[key]})
        elif not in_new:
            changes.append({"path": path, "op": "removed", "old": old_n[key], "new": None})
        else:
            ov, nv = old_n[key], new_n[key]
            if isinstance(ov, dict) and isinstance(nv, dict):
                changes.extend(diff_configs(ov, nv, prefix=path))
            elif isinstance(ov, list) and isinstance(nv, list):
                if ov != nv:
                    changes.append({"path": path, "op": "changed", "old": ov, "new": nv})
            elif ov != nv:
                changes.append({"path": path, "op": "changed", "old": ov, "new": nv})
    return changes


# Backwards/forwards-compatible alias names.
compute_diff = diff_configs
diff = diff_configs
compare_configs = diff_configs
config_diff = diff_configs


class ConfigHistoryEntry(dict):
    """A single versioned camera-config snapshot (dict subclass)."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value

    def to_dict(self) -> Dict[str, Any]:
        return dict(self)

    def dict(self) -> Dict[str, Any]:
        return self.to_dict()

    def model_dump(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.to_dict()

    def as_dict(self) -> Dict[str, Any]:
        return self.to_dict()


ConfigHistoryRecord = ConfigHistoryEntry
HistoryEntry = ConfigHistoryEntry
ConfigVersion = ConfigHistoryEntry
SnapshotEntry = ConfigHistoryEntry
ConfigSnapshot = ConfigHistoryEntry


def _build_entry(
    camera_id: Any,
    config: Dict[str, Any],
    *,
    version: int,
    previous: Optional[Dict[str, Any]] = None,
    actor: Any = None,
    reason: Any = None,
    extra: Optional[Dict[str, Any]] = None,
) -> ConfigHistoryEntry:
    entry_id = str(uuid.uuid4())
    now = _now_iso()
    changes: List[Dict[str, Any]] = []
    if previous is not None:
        try:
            changes = diff_configs(previous.get("config", {}) if isinstance(previous, dict) else {}, config)
        except Exception:
            changes = []
    changed_fields = sorted({c["path"].split(".")[0] for c in changes if c.get("path")})
    entry = ConfigHistoryEntry()
    entry["id"] = entry_id
    entry["entry_id"] = entry_id
    entry["version_id"] = entry_id
    entry["snapshot_id"] = entry_id
    entry["camera_id"] = str(camera_id)
    entry["camera"] = str(camera_id)
    entry["version"] = int(version)
    entry["revision"] = int(version)
    entry["config"] = copy.deepcopy(config)
    entry["snapshot"] = copy.deepcopy(config)
    entry["data"] = copy.deepcopy(config)
    entry["config_hash"] = _config_hash(config)
    entry["hash"] = entry["config_hash"]
    entry["changes"] = changes
    entry["diff"] = changes
    entry["changed_fields"] = changed_fields
    entry["fields"] = changed_fields
    entry["change_count"] = len(changes)
    entry["actor"] = actor
    entry["user"] = actor
    entry["reason"] = reason
    entry["note"] = reason
    entry["created_at"] = now
    entry["timestamp"] = now
    entry["ts"] = now
    if extra:
        for k, v in extra.items():
            if k not in entry:
                entry[k] = v
    return entry


class ConfigHistoryService:
    """In-memory per-camera config version store."""

    def __init__(self) -> None:
        self.store = CONFIG_HISTORY
        self.history = CONFIG_HISTORY
        self.entries = CONFIG_HISTORY
        self._lock = _lock

    # -- snapshot --
    def snapshot_camera(
        self,
        camera_id: Any = None,
        config: Any = None,
        *args: Any,
        **kwargs: Any,
    ) -> ConfigHistoryEntry:
        # Flexible calling: snapshot_camera(config), snapshot_camera(id, config),
        # snapshot_camera(camera_id=..., config=...) plus nested wrappers.
        data: Dict[str, Any] = {}
        for a in args:
            if isinstance(a, dict):
                for k, v in a.items():
                    data.setdefault(k, v)
        data.update(kwargs)
        # Unwrap nested config wrappers.
        for nested_key in ("config", "camera_config", "settings", "snapshot", "data", "body", "payload"):
            nested = data.get(nested_key)
            if isinstance(nested, dict) and config is None and nested_key != "config":
                pass  # handled below via _pick
        if camera_id is None:
            for key in ("camera_id", "camera", "cam", "cameraId", "cam_id", "id_camera"):
                if data.get(key) is not None:
                    camera_id = data.pop(key)
                    break
        if config is None:
            for key in ("config", "camera_config", "settings", "snapshot", "data", "payload", "body"):
                if isinstance(data.get(key), dict):
                    config = data.pop(key)
                    break
            else:
                # Remaining keys (minus metadata) are the config itself.
                meta_keys = {
                    "camera_id", "camera", "cam", "actor", "user", "username",
                    "reason", "note", "comment", "force", "dedupe", "skip_unchanged",
                }
                leftover = {k: v for k, v in data.items() if k not in meta_keys}
                if leftover and config is None:
                    config = leftover
        actor = data.get("actor", data.get("user", data.get("username")))
        reason = data.get("reason", data.get("note", data.get("comment")))
        force = bool(data.get("force", data.get("record_unchanged", False)))
        skip_unchanged = data.get("skip_unchanged", data.get("dedupe", True))
        if isinstance(skip_unchanged, str):
            skip_unchanged = skip_unchanged.strip().lower() not in ("0", "false", "no", "off")
        else:
            skip_unchanged = bool(skip_unchanged) if skip_unchanged is not None else True

        if camera_id is None or (isinstance(camera_id, str) and not str(camera_id).strip()):
            raise ValueError("camera_id is required")
        normalized = _normalize_config(config if config is not None else {})
        cam_key = str(camera_id)
        with self._lock:
            ids = list(_CAMERA_INDEX.get(cam_key, []))
            previous = CONFIG_HISTORY[ids[-1]] if ids else None
            if previous is not None and not force and skip_unchanged:
                try:
                    if previous.get("config_hash") == _config_hash(normalized):
                        return previous
                except Exception:
                    pass
                try:
                    if not diff_configs(previous.get("config", {}), normalized):
                        return previous
                except Exception:
                    pass
            version = int(_VERSION_COUNTERS.get(cam_key, 0)) + 1
            _VERSION_COUNTERS[cam_key] = version
            entry = _build_entry(cam_key, normalized, version=version, previous=previous, actor=actor, reason=reason)
            CONFIG_HISTORY[entry["id"]] = entry
            _CAMERA_INDEX.setdefault(cam_key, []).append(entry["id"])
            # Best-effort audit trail (actions log); never fail snapshots.
            try:
                from app.services import audit as _audit

                _audit.append_audit_entry(
                    action="camera.config.snapshot",
                    event_type="camera.config.snapshot",
                    camera_id=cam_key,
                    entity_type="camera",
                    entity_id=cam_key,
                    details={"version": version, "changed_fields": entry.get("changed_fields", [])},
                    actor=actor,
                    correlation_id=None,
                )
            except Exception:
                pass
            return entry

    def snapshot(self, *args: Any, **kwargs: Any) -> ConfigHistoryEntry:
        return self.snapshot_camera(*args, **kwargs)

    def record_snapshot(self, *args: Any, **kwargs: Any) -> ConfigHistoryEntry:
        return self.snapshot_camera(*args, **kwargs)

    def record(self, *args: Any, **kwargs: Any) -> ConfigHistoryEntry:
        return self.snapshot_camera(*args, **kwargs)

    def add(self, *args: Any, **kwargs: Any) -> ConfigHistoryEntry:
        return self.snapshot_camera(*args, **kwargs)

    def create(self, *args: Any, **kwargs: Any) -> ConfigHistoryEntry:
        return self.snapshot_camera(*args, **kwargs)

    def save_snapshot(self, *args: Any, **kwargs: Any) -> ConfigHistoryEntry:
        return self.snapshot_camera(*args, **kwargs)

    def snapshot_config(self, *args: Any, **kwargs: Any) -> ConfigHistoryEntry:
        return self.snapshot_camera(*args, **kwargs)

    # -- get --
    def get_version(self, version_id: Any) -> Optional[ConfigHistoryEntry]:
        if version_id is None:
            return None
        key = str(version_id)
        with self._lock:
            found = CONFIG_HISTORY.get(key)
            if found is not None:
                return found
            for e in CONFIG_HISTORY.values():
                if str(e.get("version")) == key:
                    return e
        return None

    def get(self, version_id: Any) -> Optional[ConfigHistoryEntry]:
        return self.get_version(version_id)

    def get_entry(self, version_id: Any) -> Optional[ConfigHistoryEntry]:
        return self.get_version(version_id)

    def fetch(self, version_id: Any) -> Optional[ConfigHistoryEntry]:
        return self.get_version(version_id)

    # -- list --
    def list_history(
        self,
        camera_id: Any = None,
        limit: Any = None,
        offset: Any = None,
        skip: Any = None,
        **filters: Any,
    ) -> List[ConfigHistoryEntry]:
        if isinstance(camera_id, dict):
            filters.update(camera_id)
            camera_id = None
        if camera_id is None:
            for key in ("camera", "cam", "cameraId", "cam_id"):
                if filters.get(key) is not None:
                    camera_id = filters.pop(key)
                    break
        lim = filters.pop("limit", limit)
        for alias in ("n", "count", "size", "max", "top"):
            if lim is None and filters.get(alias) is not None:
                lim = filters.pop(alias)
        sk = filters.pop("skip", skip)
        if sk is None:
            sk = filters.pop("offset", offset if offset is not None else 0)
        for alias in ("start_index", "page"):
            filters.pop(alias, None)
        version_filter = filters.pop("version", filters.pop("revision", None))

        with self._lock:
            if camera_id is not None:
                ids = list(_CAMERA_INDEX.get(str(camera_id), []))
                items = [CONFIG_HISTORY[i] for i in ids if i in CONFIG_HISTORY]
            else:
                # All entries, ordered by creation.
                items = []
                for cam_key in sorted(_CAMERA_INDEX.keys()):
                    for vid in _CAMERA_INDEX[cam_key]:
                        if vid in CONFIG_HISTORY:
                            items.append(CONFIG_HISTORY[vid])
                # Include any orphan entries.
                known = {id(e) for e in items}
                for e in CONFIG_HISTORY.values():
                    if id(e) not in known:
                        items.append(e)
        if version_filter is not None:
            items = [e for e in items if str(e.get("version")) == str(version_filter)]
        # Generic tolerant filters on config top-level + metadata.
        generic = {k: v for k, v in filters.items() if v is not None}
        if generic:
            kept = []
            for e in items:
                ok = True
                for k, expected in generic.items():
                    ev_val = e.get(k)
                    if ev_val is None and isinstance(e.get("config"), dict):
                        ev_val = e["config"].get(k)
                    if ev_val is None or str(ev_val) != str(expected):
                        ok = False
                        break
                if ok:
                    kept.append(e)
            items = kept
        try:
            sk_i = int(sk or 0)
        except (TypeError, ValueError):
            sk_i = 0
        if sk_i:
            items = items[sk_i:]
        if lim is not None:
            try:
                lim_i = int(lim)
            except (TypeError, ValueError):
                lim_i = None
            if lim_i is not None and lim_i >= 0:
                items = items[:lim_i]
        return items

    def list(self, *args: Any, **kwargs: Any) -> List[ConfigHistoryEntry]:
        return self.list_history(*args, **kwargs)

    def get_history(self, *args: Any, **kwargs: Any) -> List[ConfigHistoryEntry]:
        return self.list_history(*args, **kwargs)

    def fetch_history(self, *args: Any, **kwargs: Any) -> List[ConfigHistoryEntry]:
        return self.list_history(*args, **kwargs)

    def list_versions(self, *args: Any, **kwargs: Any) -> List[ConfigHistoryEntry]:
        return self.list_history(*args, **kwargs)

    def get_versions(self, *args: Any, **kwargs: Any) -> List[ConfigHistoryEntry]:
        return self.list_history(*args, **kwargs)

    def history_for_camera(self, *args: Any, **kwargs: Any) -> List[ConfigHistoryEntry]:
        return self.list_history(*args, **kwargs)

    def all(self, *args: Any, **kwargs: Any) -> List[ConfigHistoryEntry]:
        return self.list_history(*args, **kwargs)

    # -- latest --
    def latest(self, camera_id: Any) -> Optional[ConfigHistoryEntry]:
        items = self.list_history(camera_id=camera_id)
        return items[-1] if items else None

    def get_latest(self, camera_id: Any) -> Optional[ConfigHistoryEntry]:
        return self.latest(camera_id)

    def latest_config(self, camera_id: Any) -> Dict[str, Any]:
        entry = self.latest(camera_id)
        if entry is None:
            return {}
        return dict(entry.get("config", {}) or {})

    # -- diff --
    def diff_versions(self, first: Any, second: Any) -> List[Dict[str, Any]]:
        a = self.get_version(first)
        b = self.get_version(second)
        if a is None or b is None:
            missing = first if a is None else second
            raise KeyError(f"config history version not found: {missing}")
        return diff_configs(a.get("config", {}), b.get("config", {}))

    def diff(self, first: Any, second: Any) -> List[Dict[str, Any]]:
        return self.diff_versions(first, second)

    def compare(self, first: Any, second: Any) -> List[Dict[str, Any]]:
        return self.diff_versions(first, second)

    def diff_against_previous(self, version_id: Any) -> List[Dict[str, Any]]:
        entry = self.get_version(version_id)
        if entry is None:
            raise KeyError(f"config history version not found: {version_id}")
        cam_key = str(entry.get("camera_id"))
        with self._lock:
            ids = list(_CAMERA_INDEX.get(cam_key, []))
        try:
            idx = [str(CONFIG_HISTORY[i].get("id")) for i in ids].index(str(entry.get("id")))
        except ValueError:
            return list(entry.get("changes", []) or [])
        if idx == 0:
            return list(entry.get("changes", []) or [])
        prev = CONFIG_HISTORY.get(ids[idx - 1])
        if prev is None:
            return list(entry.get("changes", []) or [])
        return diff_configs(prev.get("config", {}), entry.get("config", {}))

    # -- delete / clear --
    def delete_version(self, version_id: Any) -> bool:
        if version_id is None:
            return False
        key = str(version_id)
        with self._lock:
            if CONFIG_HISTORY.pop(key, None) is not None:
                for cam_key, ids in list(_CAMERA_INDEX.items()):
                    if key in ids:
                        _CAMERA_INDEX[cam_key] = [i for i in ids if i != key]
                        if not _CAMERA_INDEX[cam_key]:
                            _VERSION_COUNTERS.pop(cam_key, None)
                return True
        return False

    def delete(self, version_id: Any) -> bool:
        return self.delete_version(version_id)

    def remove(self, version_id: Any) -> bool:
        return self.delete_version(version_id)

    def clear_history(self, camera_id: Any = None, **filters: Any) -> int:
        with self._lock:
            if camera_id is None and not filters:
                n = len(CONFIG_HISTORY)
                CONFIG_HISTORY.clear()
                _CAMERA_INDEX.clear()
                _VERSION_COUNTERS.clear()
                return n
        targets = self.list_history(camera_id=camera_id, **filters)
        n = 0
        with self._lock:
            for t in targets:
                key = str(t.get("id"))
                if CONFIG_HISTORY.pop(key, None) is not None:
                    n += 1
                    cam_key = str(t.get("camera_id"))
                    if cam_key in _CAMERA_INDEX and key in _CAMERA_INDEX[cam_key]:
                        _CAMERA_INDEX[cam_key] = [i for i in _CAMERA_INDEX[cam_key] if i != key]
        return n

    def clear(self, *args: Any, **kwargs: Any) -> int:
        # clear() with a positional camera id.
        if args and not kwargs:
            return self.clear_history(camera_id=args[0])
        return self.clear_history(*args, **kwargs)

    def reset(self, *args: Any, **kwargs: Any) -> int:
        if args and not kwargs:
            return self.clear_history(camera_id=args[0])
        return self.clear_history(*args, **kwargs)

    def purge(self, *args: Any, **kwargs: Any) -> int:
        if args and not kwargs:
            return self.clear_history(camera_id=args[0])
        return self.clear_history(*args, **kwargs)

    def count(self, *args: Any, **kwargs: Any) -> int:
        return len(self.list_history(*args, **kwargs))

    def __len__(self) -> int:
        return len(CONFIG_HISTORY)


config_history_service = ConfigHistoryService()
history_service = config_history_service
service = config_history_service
manager = config_history_service
store = config_history_service
default_service = config_history_service


def snapshot_camera(*args: Any, **kwargs: Any) -> ConfigHistoryEntry:
    return config_history_service.snapshot_camera(*args, **kwargs)


def snapshot(*args: Any, **kwargs: Any) -> ConfigHistoryEntry:
    return config_history_service.snapshot_camera(*args, **kwargs)


def record_snapshot(*args: Any, **kwargs: Any) -> ConfigHistoryEntry:
    return config_history_service.snapshot_camera(*args, **kwargs)


def snapshot_camera_config(*args: Any, **kwargs: Any) -> ConfigHistoryEntry:
    return config_history_service.snapshot_camera(*args, **kwargs)


def record(*args: Any, **kwargs: Any) -> ConfigHistoryEntry:
    return config_history_service.snapshot_camera(*args, **kwargs)


def get_version(version_id: Any) -> Optional[ConfigHistoryEntry]:
    return config_history_service.get_version(version_id)


def get_entry(version_id: Any) -> Optional[ConfigHistoryEntry]:
    return config_history_service.get_version(version_id)


def list_history(*args: Any, **kwargs: Any) -> List[ConfigHistoryEntry]:
    return config_history_service.list_history(*args, **kwargs)


def list_camera_history(*args: Any, **kwargs: Any) -> List[ConfigHistoryEntry]:
    return config_history_service.list_history(*args, **kwargs)


def fetch_history(*args: Any, **kwargs: Any) -> List[ConfigHistoryEntry]:
    return config_history_service.list_history(*args, **kwargs)


def latest(*args: Any, **kwargs: Any) -> Optional[ConfigHistoryEntry]:
    return config_history_service.latest(*args, **kwargs)


def get_latest(*args: Any, **kwargs: Any) -> Optional[ConfigHistoryEntry]:
    return config_history_service.latest(*args, **kwargs)


def diff_versions(first: Any, second: Any) -> List[Dict[str, Any]]:
    return config_history_service.diff_versions(first, second)


def delete_version(version_id: Any) -> bool:
    return config_history_service.delete_version(version_id)


def clear_history(*args: Any, **kwargs: Any) -> int:
    # Positional camera id convenience.
    if args and not kwargs:
        return config_history_service.clear_history(camera_id=args[0])
    return config_history_service.clear_history(*args, **kwargs)


def clear(*args: Any, **kwargs: Any) -> int:
    if args and not kwargs:
        return config_history_service.clear_history(camera_id=args[0])
    return config_history_service.clear_history(*args, **kwargs)


def latest_config(camera_id: Any) -> Dict[str, Any]:
    return config_history_service.latest_config(camera_id)


# Compatibility aliases.
snapshot_config = snapshot_camera_config
save_snapshot = snapshot_camera_config
log_snapshot = snapshot_camera_config
add_snapshot = snapshot_camera_config
create_snapshot = snapshot_camera_config
get = get_version
fetch = get_version
list_all = list_history
all_entries = list_history
get_entries = list_history
remove = delete_version
delete = delete_version
reset = clear
purge = clear
compare = diff_versions

ConfigHistoryServiceClass = ConfigHistoryService
ConfigHistoryManager = ConfigHistoryService
HistoryService = ConfigHistoryService
HistoryManager = ConfigHistoryService

__all__ = [
    "CONFIG_HISTORY",
    "ConfigHistoryEntry",
    "ConfigHistoryService",
    "config_history_service",
    "snapshot_camera",
    "snapshot_camera_config",
    "snapshot",
    "record_snapshot",
    "get_version",
    "get_entry",
    "list_history",
    "list_camera_history",
    "fetch_history",
    "latest",
    "get_latest",
    "latest_config",
    "diff_configs",
    "diff_versions",
    "delete_version",
    "clear_history",
    "clear",
]
