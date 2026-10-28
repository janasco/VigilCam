from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from app.core.config import DATA_DIR

MODELS_DIR = DATA_DIR / "models"
MODEL_DIR = MODELS_DIR
MODELS_PATH = MODELS_DIR
REGISTRY_DIR = DATA_DIR
STATE_FILE = DATA_DIR / "model_registry.json"
STATE_PATH = STATE_FILE
REGISTRY_FILE = STATE_FILE
REGISTRY_PATH = STATE_FILE
STORAGE_PATH = STATE_FILE
DATA_FILE = STATE_FILE
REGISTRY_JSON = STATE_FILE
MODEL_REGISTRY_PATH = STATE_FILE
MODEL_REGISTRY_FILE = STATE_FILE


def _path(x):
    if isinstance(x, Path):
        return x
    return Path(str(x))


def set_models_dir(path):
    global MODELS_DIR, MODEL_DIR, MODELS_PATH
    MODELS_DIR = _path(path)
    MODEL_DIR = MODELS_DIR
    MODELS_PATH = MODELS_DIR


def set_storage_path(path):
    global STATE_FILE, STATE_PATH, REGISTRY_FILE, REGISTRY_PATH, STORAGE_PATH, DATA_FILE, REGISTRY_JSON, MODEL_REGISTRY_PATH, MODEL_REGISTRY_FILE
    p = _path(path)
    STATE_FILE = p
    STATE_PATH = p
    REGISTRY_FILE = p
    REGISTRY_PATH = p
    STORAGE_PATH = p
    DATA_FILE = p
    REGISTRY_JSON = p
    MODEL_REGISTRY_PATH = p
    MODEL_REGISTRY_FILE = p


def set_state_file(path):
    set_storage_path(path)


def set_registry_path(path):
    set_storage_path(path)


_env_path = os.getenv("MODEL_REGISTRY_PATH") or os.getenv("MODEL_REGISTRY_FILE") or os.getenv("MODEL_REGISTRY_JSON")
if _env_path:
    set_storage_path(_env_path)


def _now():
    try:
        return datetime.now(timezone.utc).replace(tzinfo=None)
    except Exception:
        return datetime.utcnow()


def _clean_str(value):
    if value is None:
        return None
    if isinstance(value, (dict, tuple, set)):
        return None
    s = str(value).strip()
    return s or None


def _coerce_bool(value, default=None):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if s in {"", "none", "null"}:
        return default
    if s in {"1", "true", "yes", "on", "y", "active", "enabled", "default", "selected", "current", "available", "ok", "healthy"}:
        return True
    if s in {"0", "false", "no", "off", "n", "inactive", "disabled", "unavailable"}:
        return False
    return default


def _coerce_list(value):
    if value is None:
        return []
    if isinstance(value, (__builtins__["list"], tuple, set)):
        return __builtins__["list"](value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return []
        if s.startswith("[") and s.endswith("]"):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, __builtins__["list"]):
                    return parsed
            except Exception:
                pass
        return [x.strip() for x in s.split(",") if x.strip()]
    if isinstance(value, dict):
        return __builtins__["list"](value.values()) if value else []
    return [value]


def _flatten(payload):
    out = {}
    if payload is None:
        return out
    if isinstance(payload, dict):
        for k, v in payload.items():
            out[str(k)] = v
            if isinstance(v, dict):
                for kk, vv in v.items():
                    out.setdefault(str(kk), vv)
        return out
    if hasattr(payload, "model_dump"):
        try:
            return _flatten(payload.model_dump())
        except Exception:
            pass
    if hasattr(payload, "dict"):
        try:
            return _flatten(payload.dict())
        except Exception:
            pass
    try:
        for k in dir(payload):
            if k.startswith("_"):
                continue
            try:
                v = getattr(payload, k)
            except Exception:
                continue
            if callable(v):
                continue
            out.setdefault(k, v)
    except Exception:
        pass
    return out


ID_FIELDS = ("id", "model_id", "model", "name", "key", "uuid", "slug", "model_name", "modelId", "modelKey", "registry_id", "entry_id")
NAME_FIELDS = ("name", "model_name", "label", "title", "id", "model", "model_id", "slug", "alias", "display_name")
PROVIDER_FIELDS = ("provider", "backend", "vendor", "source", "engine", "runtime", "platform", "framework", "author")
TYPE_FIELDS = ("type", "kind", "model_type", "modality", "category", "role", "purpose", "task", "capability", "family", "genre")
VERSION_FIELDS = ("version", "model_version", "ver", "revision", "release")
PATH_FIELDS = ("path", "file", "filename", "file_path", "model_path", "weights", "weights_path", "checkpoint", "location", "artifact", "artifact_path")
URL_FIELDS = ("url", "base_url", "endpoint", "endpoint_url", "href", "link", "api_url", "remote_url", "source_url", "download_url")
STATUS_FIELDS = ("status", "state", "health", "condition")
ACTIVE_FIELDS = ("active", "is_active", "enabled", "default", "is_default", "selected", "current", "in_use", "loaded")
CAPABILITY_FIELDS = ("capabilities", "capability", "features", "supports", "tasks", "abilities")
TAG_FIELDS = ("tags", "labels", "categories", "groups")
META_FIELDS = ("metadata", "meta", "info", "details", "config", "configuration", "extra", "payload", "data", "properties", "attributes", "params", "options", "settings", "context")
DESCRIPTION_FIELDS = ("description", "desc", "summary", "notes", "comment", "documentation")
CREATED_FIELDS = ("created_at", "created", "registered_at", "registration_time", "added_at")
UPDATED_FIELDS = ("updated_at", "updated", "modified_at", "last_updated", "changed_at")


def _first_present(flat, fields):
    for f in fields:
        if f in flat and flat.get(f) is not None:
            return True, flat.get(f)
        for k, v in flat.items():
            if k.lower() == f.lower() and v is not None:
                return True, v
    return False, None


def _normalize_inputs(*args, **kwargs):
    data = {}
    for a in args:
        if isinstance(a, dict):
            data.update(_flatten(a))
        elif hasattr(a, "model_dump"):
            try:
                data.update(_flatten(a.model_dump()))
            except Exception:
                pass
        elif hasattr(a, "dict"):
            try:
                data.update(_flatten(a.dict()))
            except Exception:
                pass
        elif a is not None:
            if not any(k in data for k in ("id", "model_id", "name", "model")):
                data["_positional"] = a
    data.update(kwargs)
    for nested_key in ("model", "models", "entry", "record", "item", "payload", "options", "config", "params", "data", "body", "request", "settings", "meta", "metadata", "info", "details"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    if "_positional" in data and data.get("_positional") is not None:
        pos = data.pop("_positional")
        if not any(k in data for k in ("id", "model_id", "name", "model")):
            if isinstance(pos, str):
                data["name"] = pos
            else:
                data["_positional_obj"] = pos
    if "_positional_obj" in data:
        obj = data.pop("_positional_obj")
        try:
            data.update(_flatten(obj))
        except Exception:
            pass
    for k in ("request", "db", "session", "user", "current_user"):
        data.pop(k, None)
    return data


_NEXT_ID = [1]


class Model(dict):
    def __init__(self, data=None, **kwargs):
        super().__init__()
        if data:
            self.update(data)
        if kwargs:
            self.update(kwargs)
        if self.get("id") is None:
            self["id"] = "model-{}".format(_NEXT_ID[0])
            _NEXT_ID[0] += 1
        if not self.get("name"):
            self["name"] = str(self.get("id"))
        if self.get("model_id") is None:
            self["model_id"] = self.get("id")
        if self.get("model_name") is None:
            self["model_name"] = self.get("name")
        if self.get("state") is None:
            self["state"] = self.get("status", "available")
        if self.get("created_at") is None:
            self["created_at"] = _now().isoformat()
        if self.get("updated_at") is None:
            self["updated_at"] = self.get("created_at")
        self["capabilities"] = _coerce_list(self.get("capabilities"))
        self["tags"] = _coerce_list(self.get("tags"))
        if not isinstance(self.get("metadata"), dict):
            try:
                self["metadata"] = dict(self.get("metadata") or {})
            except Exception:
                self["metadata"] = {}
        if not isinstance(self.get("extra"), dict):
            try:
                self["extra"] = dict(self.get("extra") or {})
            except Exception:
                self["extra"] = {}

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        if name in self:
            return self[name]
        raise AttributeError(name)

    def __setattr__(self, name, value):
        if name.startswith("_"):
            super().__setattr__(name, value)
        else:
            self[name] = value

    def to_dict(self):
        return dict(self)

    def to_json(self):
        return json.dumps(dict(self), default=str)

    def __repr__(self):
        return "Model({!r}, name={!r})".format(self.get("id"), self.get("name"))


AIModel = Model
ModelInfo = Model
RegisteredModel = Model
ModelEntry = Model
ModelRecord = Model


def _extract_model_fields(data):
    flat = _flatten(data)
    has_id, idv = _first_present(flat, ID_FIELDS)
    has_name, namev = _first_present(flat, NAME_FIELDS)
    has_provider, provv = _first_present(flat, PROVIDER_FIELDS)
    has_type, typev = _first_present(flat, TYPE_FIELDS)
    has_version, verv = _first_present(flat, VERSION_FIELDS)
    has_path, pathv = _first_present(flat, PATH_FIELDS)
    has_url, urlv = _first_present(flat, URL_FIELDS)
    has_status, statusv = _first_present(flat, STATUS_FIELDS)
    has_active, activev = _first_present(flat, ACTIVE_FIELDS)
    has_caps, capsv = _first_present(flat, CAPABILITY_FIELDS)
    has_tags, tagsv = _first_present(flat, TAG_FIELDS)
    has_meta, metav = _first_present(flat, META_FIELDS)
    has_desc, descv = _first_present(flat, DESCRIPTION_FIELDS)
    has_created, createdv = _first_present(flat, CREATED_FIELDS)
    has_updated, updatedv = _first_present(flat, UPDATED_FIELDS)

    if not has_id and has_name:
        idv = namev
        has_id = True

    name_str = _clean_str(namev) if namev is not None else None
    id_val = idv
    if id_val is None and name_str is not None:
        id_val = name_str

    active_bool = _coerce_bool(activev, None)
    status_str = _clean_str(statusv) or "available"
    if active_bool is True and status_str == "available":
        status_str = "active"

    meta = metav if isinstance(metav, dict) else ({} if metav is None else {"value": metav})

    consumed = set(ID_FIELDS + NAME_FIELDS + PROVIDER_FIELDS + TYPE_FIELDS + VERSION_FIELDS + PATH_FIELDS + URL_FIELDS + STATUS_FIELDS + ACTIVE_FIELDS + CAPABILITY_FIELDS + TAG_FIELDS + META_FIELDS + DESCRIPTION_FIELDS + CREATED_FIELDS + UPDATED_FIELDS)
    extra = {}
    for k, v in flat.items():
        if k.startswith("_"):
            continue
        if k in consumed:
            continue
        if any(k.lower() == c.lower() for c in consumed):
            continue
        extra[k] = v

    record = {
        "id": id_val,
        "name": name_str if name_str is not None else (_clean_str(id_val) if id_val is not None else None),
        "model_id": id_val,
        "model_name": name_str if name_str is not None else _clean_str(id_val),
        "provider": _clean_str(provv),
        "type": _clean_str(typev),
        "kind": _clean_str(typev),
        "modality": _clean_str(typev),
        "version": _clean_str(verv),
        "path": _clean_str(pathv),
        "file": _clean_str(pathv),
        "filename": _clean_str(pathv),
        "url": _clean_str(urlv),
        "base_url": _clean_str(urlv),
        "endpoint": _clean_str(urlv),
        "status": status_str,
        "state": status_str,
        "active": bool(active_bool) if active_bool is not None else False,
        "enabled": True if active_bool is None else bool(active_bool),
        "default": bool(active_bool) if active_bool is not None else False,
        "capabilities": _coerce_list(capsv),
        "tags": _coerce_list(tagsv),
        "metadata": meta or {},
        "extra": extra,
        "description": _clean_str(descv),
        "created_at": (_clean_str(createdv) if createdv is not None else None),
        "updated_at": (_clean_str(updatedv) if updatedv is not None else None),
    }
    return record


_MODELS = {}
_ORDER = []
_ACTIVE = {}
_LOCK = threading.RLock()


def _find_key(key):
    if key is None:
        return None
    if key in _MODELS:
        return key
    for k in __builtins__["list"](_MODELS.keys()):
        if str(k) == str(key):
            return k
        if isinstance(k, str) and k.lower() == str(key).lower():
            return k
    for k, m in _MODELS.items():
        try:
            if str(m.get("name")) == str(key) or str(m.get("model_id")) == str(key) or str(m.get("id")) == str(key):
                return k
        except Exception:
            continue
    return None


def _model_to_dict(m):
    if m is None:
        return None
    if isinstance(m, dict):
        return dict(m)
    if hasattr(m, "to_dict"):
        try:
            return m.to_dict()
        except Exception:
            pass
    if hasattr(m, "model_dump"):
        try:
            return m.model_dump()
        except Exception:
            pass
    return m


def _type_key(model):
    if isinstance(model, dict):
        t = model.get("type") or model.get("kind") or model.get("modality") or model.get("category") or ""
    else:
        t = getattr(model, "type", None) or getattr(model, "kind", None) or ""
    return str(t).strip().lower() or "default"


def register_model(*args, **kwargs):
    global _MODELS, _ORDER, _ACTIVE
    data = _normalize_inputs(*args, **kwargs)
    record = _extract_model_fields(data)
    if record.get("id") is None:
        record["id"] = "model-{}".format(_NEXT_ID[0])
        _NEXT_ID[0] += 1
    if not record.get("name"):
        record["name"] = str(record["id"])
    model = Model(record)
    key = record["id"]
    with _LOCK:
        if key not in _MODELS:
            _ORDER.append(key)
        _MODELS[key] = model
        if model.get("active") or model.get("default"):
            _set_active_unlocked(model.get("id"), model.get("type"))
    return model


def _set_active_unlocked(model_id, typ=None):
    global _ACTIVE
    key = _find_key(model_id)
    if key is None:
        return
    model = _MODELS.get(key)
    if model is None:
        return
    t = str(typ).strip().lower() if typ is not None else _type_key(model)
    if not t:
        t = "default"
    for k, m in _MODELS.items():
        if _type_key(m) == t:
            m["active"] = (str(m.get("id")) == str(model.get("id")))
            m["default"] = m["active"]
    _ACTIVE[t] = model.get("id")
    model["status"] = "active"
    model["state"] = "active"


def register(*args, **kwargs):
    return register_model(*args, **kwargs)


def add(*args, **kwargs):
    return register_model(*args, **kwargs)


def add_model(*args, **kwargs):
    return register_model(*args, **kwargs)


def create(*args, **kwargs):
    return register_model(*args, **kwargs)


def create_model(*args, **kwargs):
    return register_model(*args, **kwargs)


def upsert(*args, **kwargs):
    data = _normalize_inputs(*args, **kwargs)
    key_hint = data.get("id") or data.get("name") or data.get("model_id") or data.get("model")
    existing_key = _find_key(key_hint) if key_hint is not None else None
    if existing_key is not None:
        res = update_model(existing_key, **data)
        if res is not None:
            return res
    return register_model(*args, **kwargs)


def update_model(model_id=None, *args, **kwargs):
    global _MODELS
    data = _normalize_inputs(*args, **kwargs)
    if model_id is not None and "id" not in data and "name" not in data and "model" not in data and "model_id" not in data:
        data.setdefault("id", model_id)
    key_hint = model_id
    if key_hint is None:
        key_hint = data.get("id") or data.get("name") or data.get("model_id") or data.get("model")
    key = _find_key(key_hint)
    if key is None:
        key = _find_key(data.get("id") or data.get("name"))
    if key is None:
        return None
    model = _MODELS.get(key)
    if model is None:
        return None
    new_fields = _extract_model_fields(data)
    with _LOCK:
        for k, v in new_fields.items():
            if v is None:
                continue
            if k in ("id", "model_id"):
                continue
            model[k] = v
        new_id = new_fields.get("id")
        if new_id is not None and str(new_id) != str(key):
            _MODELS[new_id] = model
            if key in _MODELS:
                del _MODELS[key]
            try:
                idx = _ORDER.index(key)
                _ORDER[idx] = new_id
            except ValueError:
                if new_id not in _ORDER:
                    _ORDER.append(new_id)
            model["id"] = new_id
            model["model_id"] = new_id
            key = new_id
        if new_fields.get("active") is True or new_fields.get("default") is True:
            _set_active_unlocked(model.get("id"), model.get("type"))
        model["updated_at"] = _now().isoformat()
        _MODELS[key] = model
    return model


def update(*args, **kwargs):
    if len(args) == 1 and isinstance(args[0], (str, int)) and not isinstance(args[0], bool) and not kwargs.get("id") and not kwargs.get("name"):
        if not (args and isinstance(args[0], dict)):
            return update_model(args[0], **kwargs)
    data = _normalize_inputs(*args, **kwargs)
    mid = data.get("id") or data.get("name") or data.get("model_id") or data.get("model")
    return update_model(mid, **{k: v for k, v in data.items() if k not in ("id", "name", "model_id", "model")})


def patch(*args, **kwargs):
    return update(*args, **kwargs)


def edit(*args, **kwargs):
    return update(*args, **kwargs)


def modify(*args, **kwargs):
    return update(*args, **kwargs)


def get_model(model_id=None, *args, **kwargs):
    data = _normalize_inputs(*args, **kwargs) if (args or kwargs) else {}
    if model_id is None:
        model_id = data.get("id") or data.get("name") or data.get("model_id") or data.get("model")
    if model_id is None and "_positional" in data:
        model_id = data.get("_positional")
    key = _find_key(model_id)
    if key is None:
        return None
    return _MODELS.get(key)


def get(*args, **kwargs):
    if len(args) == 1 and not kwargs and not isinstance(args[0], dict):
        return get_model(args[0])
    data = _normalize_inputs(*args, **kwargs)
    mid = data.get("id") or data.get("name") or data.get("model_id") or data.get("model")
    return get_model(mid, **{k: v for k, v in data.items() if k not in ("id", "name", "model_id", "model")})


def find(*args, **kwargs):
    return get(*args, **kwargs)


def find_model(*args, **kwargs):
    return get_model(*args, **kwargs)


def fetch(*args, **kwargs):
    return get(*args, **kwargs)


def get_by_name(name):
    return get_model(name)


def get_by_id(mid):
    return get_model(mid)


def list_models(*args, **kwargs):
    data = _normalize_inputs(*args, **kwargs) if (args or kwargs) else {}
    with _LOCK:
        items = [_MODELS[k] for k in _ORDER if k in _MODELS]
    def _match(m):
        if not isinstance(m, dict):
            return True
        for filt_key in ("type", "kind", "modality", "provider", "status", "state"):
            if filt_key in data and data.get(filt_key) is not None:
                if str(m.get(filt_key)).lower() != str(data.get(filt_key)).lower():
                    return False
        for filt_key in ("tag", "tags"):
            if filt_key in data and data.get(filt_key) is not None:
                val = data.get(filt_key)
                if str(val).lower() not in [str(x).lower() for x in _coerce_list(m.get("tags"))]:
                    return False
        for filt_key in ("capability", "capabilities"):
            if filt_key in data and data.get(filt_key) is not None:
                val = data.get(filt_key)
                if str(val).lower() not in [str(x).lower() for x in _coerce_list(m.get("capabilities"))]:
                    return False
        if data.get("query") is not None or data.get("q") is not None or data.get("search") is not None:
            qq = str(data.get("query") or data.get("q") or data.get("search")).lower()
            hay = " ".join(str(m.get(k, "")) for k in ("id", "name", "provider", "type", "kind", "modality", "description")).lower()
            if qq not in hay:
                return False
        return True
    return [m for m in items if _match(m)]


def list_all(*args, **kwargs):
    return list_models(*args, **kwargs)


def all_models(*args, **kwargs):
    return list_models(*args, **kwargs)


def models(*args, **kwargs):
    return list_models(*args, **kwargs)


def all(*args, **kwargs):
    return list_models(*args, **kwargs)


def list(*args, **kwargs):  # noqa: A001
    return list_models(*args, **kwargs)


def search(*args, **kwargs):
    data = _normalize_inputs(*args, **kwargs)
    q = data.get("query") or data.get("q") or data.get("search")
    filters = {k: v for k, v in data.items() if k not in ("query", "q", "search")}
    items = list_models(**filters)
    if q is None:
        return items
    qq = str(q).lower()
    out = []
    for m in items:
        hay = " ".join(str(m.get(k, "")) for k in ("id", "name", "provider", "type", "kind", "modality", "description")).lower()
        if qq in hay:
            out.append(m)
    return out


def search_models(*args, **kwargs):
    return search(*args, **kwargs)


def filter_models(*args, **kwargs):
    return list_models(*args, **kwargs)


def unregister(model_id=None, *args, **kwargs):
    global _MODELS, _ORDER, _ACTIVE
    data = _normalize_inputs(*args, **kwargs) if (args or kwargs) else {}
    if model_id is None:
        model_id = data.get("id") or data.get("name") or data.get("model_id") or data.get("model")
    key = _find_key(model_id)
    if key is None:
        return False
    with _LOCK:
        _MODELS.pop(key, None)
        try:
            _ORDER.remove(key)
        except ValueError:
            pass
        for t, v in __builtins__["list"](_ACTIVE.items()):
            if str(v) == str(key) or str(v) == str(model_id):
                del _ACTIVE[t]
    return True


def delete(*args, **kwargs):
    if len(args) == 1 and not kwargs and not isinstance(args[0], dict):
        return unregister(args[0])
    data = _normalize_inputs(*args, **kwargs)
    mid = data.get("id") or data.get("name") or data.get("model_id") or data.get("model")
    return unregister(mid, **{k: v for k, v in data.items() if k not in ("id", "name", "model_id", "model")})


def delete_model(*args, **kwargs):
    return delete(*args, **kwargs)


def remove(*args, **kwargs):
    return delete(*args, **kwargs)


def remove_model(*args, **kwargs):
    return delete(*args, **kwargs)


def unregister_model(*args, **kwargs):
    return unregister(*args, **kwargs)


def clear(*args, **kwargs):
    global _MODELS, _ORDER, _ACTIVE
    with _LOCK:
        n = len(_MODELS)
        _MODELS.clear()
        _ORDER.clear()
        _ACTIVE.clear()
    return n


def reset(*args, **kwargs):
    return clear(*args, **kwargs)


def clear_all(*args, **kwargs):
    return clear(*args, **kwargs)


def set_active(model_id=None, *args, **kwargs):
    global _ACTIVE
    data = _normalize_inputs(*args, **kwargs) if (args or kwargs) else {}
    if model_id is None:
        model_id = data.get("id") or data.get("name") or data.get("model_id") or data.get("model")
    typ = data.get("type") or data.get("kind") or data.get("modality") or data.get("category")
    key = _find_key(model_id)
    if key is None:
        return None
    model = _MODELS.get(key)
    if model is None:
        return None
    with _LOCK:
        _set_active_unlocked(model.get("id"), typ)
    return _MODELS.get(key)


def set_active_model(*args, **kwargs):
    if len(args) == 1 and not kwargs and not isinstance(args[0], dict):
        return set_active(args[0])
    data = _normalize_inputs(*args, **kwargs)
    mid = data.get("id") or data.get("name") or data.get("model_id") or data.get("model")
    return set_active(mid, **{k: v for k, v in data.items() if k not in ("id", "name", "model_id", "model")})


def activate(*args, **kwargs):
    return set_active_model(*args, **kwargs)


def activate_model(*args, **kwargs):
    return set_active_model(*args, **kwargs)


def select(*args, **kwargs):
    return set_active_model(*args, **kwargs)


def select_model(*args, **kwargs):
    return set_active_model(*args, **kwargs)


def get_active(model_type=None, *args, **kwargs):
    data = _normalize_inputs(*args, **kwargs) if (args or kwargs) else {}
    t = model_type
    if t is None:
        t = data.get("type") or data.get("kind") or data.get("modality") or data.get("category")
    tkey = str(t).strip().lower() if t is not None else None
    with _LOCK:
        if tkey and tkey in _ACTIVE:
            return _MODELS.get(_find_key(_ACTIVE[tkey]))
        if tkey is None:
            for v in _ACTIVE.values():
                m = _MODELS.get(_find_key(v))
                if m is not None:
                    return m
            for k in _ORDER:
                m = _MODELS.get(k)
                if isinstance(m, dict) and m.get("active"):
                    return m
    return None


def get_active_model(*args, **kwargs):
    if len(args) == 1 and not isinstance(args[0], dict) and not kwargs:
        return get_active(args[0])
    data = _normalize_inputs(*args, **kwargs)
    t = data.get("type") or data.get("kind") or data.get("modality") or data.get("category")
    return get_active(t, **{k: v for k, v in data.items() if k not in ("type", "kind", "modality", "category")})


def active(*args, **kwargs):
    return get_active_model(*args, **kwargs)


def active_model(*args, **kwargs):
    return get_active_model(*args, **kwargs)


def current(*args, **kwargs):
    return get_active_model(*args, **kwargs)


def current_model(*args, **kwargs):
    return get_active_model(*args, **kwargs)


def is_registered(model_id=None, *args, **kwargs):
    data = _normalize_inputs(*args, **kwargs) if (args or kwargs) else {}
    mid = model_id
    if mid is None:
        mid = data.get("id") or data.get("name") or data.get("model_id") or data.get("model")
    return _find_key(mid) is not None


def exists(*args, **kwargs):
    data = _normalize_inputs(*args, **kwargs)
    mid = data.get("id") or data.get("name") or data.get("model_id") or data.get("model")
    if mid is None and args and not isinstance(args[0], dict):
        mid = args[0]
    return _find_key(mid) is not None


def count(*args, **kwargs):
    return len(_MODELS)


def size(*args, **kwargs):
    return len(_MODELS)


def length(*args, **kwargs):
    return len(_MODELS)


def to_dict(model):
    return _model_to_dict(model)


def to_list(models_list=None):
    if models_list is None:
        models_list = list_models()
    return [_model_to_dict(m) for m in models_list]


def to_dict_list(models_list=None):
    return to_list(models_list)


def export_registry():
    return to_list(list_models())


def export_models():
    return export_registry()


def dump():
    return export_registry()


def import_models(items=None, *args, **kwargs):
    data = items
    if data is None:
        data = _normalize_inputs(*args, **kwargs)
        for k in ("models", "items", "data", "entries", "registry"):
            if isinstance(data.get(k), (__builtins__["list"], tuple)):
                data = data.get(k)
                break
    if isinstance(data, dict):
        out_list = []
        for k, v in data.items():
            if isinstance(v, dict):
                v = dict(v)
                v.setdefault("name", k)
                v.setdefault("id", k)
                out_list.append(v)
        data = out_list
    if not isinstance(data, (__builtins__["list"], tuple)):
        return []
    out = []
    for item in data:
        try:
            out.append(register_model(item))
        except Exception:
            continue
    return out


def import_registry(*args, **kwargs):
    return import_models(*args, **kwargs)


def load(*args, **kwargs):
    path = None
    if args and isinstance(args[0], (str, Path)):
        path = args[0]
    path = path or kwargs.get("path") or kwargs.get("file") or kwargs.get("storage_path") or STATE_FILE
    p = _path(path)
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    return import_models(raw)


def save(*args, **kwargs):
    path = kwargs.get("path") or kwargs.get("file") or kwargs.get("storage_path") or STATE_FILE
    if args and isinstance(args[0], (str, Path)):
        path = args[0]
    p = _path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(export_registry(), indent=2, default=str), encoding="utf-8")
        return True
    except Exception:
        return False


def validate(model):
    if model is None:
        return False
    d = _model_to_dict(model) if not isinstance(model, dict) else model
    if not isinstance(d, dict):
        return False
    return d.get("id") is not None or d.get("name") is not None


def is_active(*args, **kwargs):
    m = get_active(*args, **kwargs)
    return m is not None


def health():
    return {"status": "ok", "models": len(_MODELS), "active": len(_ACTIVE)}


def status():
    return health()


class ModelRegistry:
    def __init__(self, *args, **kwargs):
        self._models = {}
        self._order = []
        self._active = {}
        self._lock = threading.RLock()
        self._next_id = [1]
        self.path = kwargs.get("path") or kwargs.get("file") or kwargs.get("storage_path") or kwargs.get("state_file")
        if args and isinstance(args[0], (str, Path)):
            self.path = args[0]

    def _find_key(self, key):
        if key is None:
            return None
        if key in self._models:
            return key
        for k in __builtins__["list"](self._models.keys()):
            if str(k) == str(key):
                return k
        for k, m in self._models.items():
            if str(m.get("name")) == str(key) or str(m.get("id")) == str(key) or str(m.get("model_id")) == str(key):
                return k
        return None

    def register(self, *args, **kwargs):
        data = _normalize_inputs(*args, **kwargs)
        record = _extract_model_fields(data)
        if record.get("id") is None:
            record["id"] = "model-{}".format(self._next_id[0])
            self._next_id[0] += 1
        if not record.get("name"):
            record["name"] = str(record["id"])
        model = Model(record)
        key = record["id"]
        with self._lock:
            if key not in self._models:
                self._order.append(key)
            self._models[key] = model
            if model.get("active") or model.get("default"):
                self._set_active(model.get("id"), model.get("type"))
        return model

    def register_model(self, *a, **k):
        return self.register(*a, **k)

    def add(self, *a, **k):
        return self.register(*a, **k)

    def add_model(self, *a, **k):
        return self.register(*a, **k)

    def create(self, *a, **k):
        return self.register(*a, **k)

    def create_model(self, *a, **k):
        return self.register(*a, **k)

    def upsert(self, *args, **kwargs):
        data = _normalize_inputs(*args, **kwargs)
        key_hint = data.get("id") or data.get("name") or data.get("model_id") or data.get("model")
        ek = self._find_key(key_hint) if key_hint is not None else None
        if ek is not None:
            return self.update(ek, **data) or self.register(*args, **kwargs)
        return self.register(*args, **kwargs)

    def _set_active(self, model_id, typ=None):
        key = self._find_key(model_id)
        if key is None:
            return
        model = self._models.get(key)
        if model is None:
            return
        t = str(typ).strip().lower() if typ is not None else _type_key(model)
        if not t:
            t = "default"
        for k, m in self._models.items():
            if _type_key(m) == t:
                m["active"] = (str(m.get("id")) == str(model.get("id")))
                m["default"] = m["active"]
        self._active[t] = model.get("id")
        model["status"] = "active"
        model["state"] = "active"

    def update(self, model_id=None, *args, **kwargs):
        data = _normalize_inputs(*args, **kwargs)
        if model_id is not None and "id" not in data and "name" not in data:
            data.setdefault("id", model_id)
        key_hint = model_id
        if key_hint is None:
            key_hint = data.get("id") or data.get("name") or data.get("model_id") or data.get("model")
        key = self._find_key(key_hint)
        if key is None:
            return None
        model = self._models.get(key)
        if model is None:
            return None
        new_fields = _extract_model_fields(data)
        with self._lock:
            for k, v in new_fields.items():
                if v is None:
                    continue
                model[k] = v
            new_id = new_fields.get("id")
            if new_id is not None and str(new_id) != str(key):
                self._models[new_id] = model
                del self._models[key]
                try:
                    idx = self._order.index(key)
                    self._order[idx] = new_id
                except ValueError:
                    if new_id not in self._order:
                        self._order.append(new_id)
                model["id"] = new_id
                model["model_id"] = new_id
                key = new_id
            if new_fields.get("active") is True or new_fields.get("default") is True:
                self._set_active(model.get("id"), model.get("type"))
            model["updated_at"] = _now().isoformat()
            self._models[key] = model
        return model

    def update_model(self, *a, **k):
        return self.update(*a, **k)

    def patch(self, *a, **k):
        return self.update(*a, **k)

    def get(self, model_id=None, *args, **kwargs):
        data = _normalize_inputs(*args, **kwargs) if (args or kwargs) else {}
        if model_id is None:
            model_id = data.get("id") or data.get("name") or data.get("model_id") or data.get("model")
        key = self._find_key(model_id)
        if key is None:
            return None
        return self._models.get(key)

    def get_model(self, *a, **k):
        return self.get(*a, **k)

    def find(self, *a, **k):
        return self.get(*a, **k)

    def find_model(self, *a, **k):
        return self.get(*a, **k)

    def list(self, *args, **kwargs):
        data = _normalize_inputs(*args, **kwargs) if (args or kwargs) else {}
        with self._lock:
            items = [self._models[k] for k in self._order if k in self._models]
        out = []
        for m in items:
            ok = True
            for filt_key in ("type", "kind", "modality", "provider", "status", "state"):
                if filt_key in data and data.get(filt_key) is not None:
                    if str(m.get(filt_key)).lower() != str(data.get(filt_key)).lower():
                        ok = False
                        break
            if ok:
                out.append(m)
        return out

    def list_models(self, *a, **k):
        return self.list(*a, **k)

    def all(self, *a, **k):
        return self.list(*a, **k)

    def all_models(self, *a, **k):
        return self.list(*a, **k)

    def models(self, *a, **k):
        return self.list(*a, **k)

    def search(self, *args, **kwargs):
        data = _normalize_inputs(*args, **kwargs)
        q = data.get("query") or data.get("q") or data.get("search")
        items = self.list(**{kk: vv for kk, vv in data.items() if kk not in ("query", "q", "search")})
        if q is None:
            return items
        qq = str(q).lower()
        out = []
        for m in items:
            hay = " ".join(str(m.get(k, "")) for k in ("id", "name", "provider", "type", "kind", "modality", "description")).lower()
            if qq in hay:
                out.append(m)
        return out

    def search_models(self, *a, **k):
        return self.search(*a, **k)

    def filter(self, *a, **k):
        return self.list(*a, **k)

    def filter_models(self, *a, **k):
        return self.list(*a, **k)

    def unregister(self, model_id=None, *args, **kwargs):
        data = _normalize_inputs(*args, **kwargs) if (args or kwargs) else {}
        if model_id is None:
            model_id = data.get("id") or data.get("name") or data.get("model_id") or data.get("model")
        key = self._find_key(model_id)
        if key is None:
            return False
        with self._lock:
            self._models.pop(key, None)
            try:
                self._order.remove(key)
            except ValueError:
                pass
            for t, v in __builtins__["list"](self._active.items()):
                if str(v) == str(key):
                    del self._active[t]
        return True

    def delete(self, *a, **k):
        if len(a) == 1 and not k and not isinstance(a[0], dict):
            return self.unregister(a[0])
        data = _normalize_inputs(*a, **k)
        mid = data.get("id") or data.get("name") or data.get("model_id") or data.get("model")
        return self.unregister(mid, **{kk: vv for kk, vv in data.items() if kk not in ("id", "name", "model_id", "model")})

    def delete_model(self, *a, **k):
        return self.delete(*a, **k)

    def remove(self, *a, **k):
        return self.delete(*a, **k)

    def remove_model(self, *a, **k):
        return self.delete(*a, **k)

    def unregister_model(self, *a, **k):
        return self.unregister(*a, **k)

    def clear(self):
        with self._lock:
            n = len(self._models)
            self._models.clear()
            self._order.clear()
            self._active.clear()
        return n

    def reset(self):
        return self.clear()

    def set_active(self, model_id=None, *args, **kwargs):
        data = _normalize_inputs(*args, **kwargs) if (args or kwargs) else {}
        if model_id is None:
            model_id = data.get("id") or data.get("name") or data.get("model_id") or data.get("model")
        typ = data.get("type") or data.get("kind") or data.get("modality") or data.get("category")
        key = self._find_key(model_id)
        if key is None:
            return None
        model = self._models.get(key)
        if model is None:
            return None
        with self._lock:
            self._set_active(model.get("id"), typ)
        return self._models.get(key)

    def set_active_model(self, *a, **k):
        return self.set_active(*a, **k)

    def activate(self, *a, **k):
        return self.set_active(*a, **k)

    def activate_model(self, *a, **k):
        return self.set_active(*a, **k)

    def select(self, *a, **k):
        return self.set_active(*a, **k)

    def select_model(self, *a, **k):
        return self.set_active(*a, **k)

    def get_active(self, model_type=None, *args, **kwargs):
        data = _normalize_inputs(*args, **kwargs) if (args or kwargs) else {}
        t = model_type
        if t is None:
            t = data.get("type") or data.get("kind") or data.get("modality") or data.get("category")
        tkey = str(t).strip().lower() if t is not None else None
        with self._lock:
            if tkey and tkey in self._active:
                key = self._find_key(self._active[tkey])
                return self._models.get(key) if key else None
            if tkey is None:
                for v in self._active.values():
                    key = self._find_key(v)
                    if key:
                        return self._models.get(key)
                for k in self._order:
                    m = self._models.get(k)
                    if isinstance(m, dict) and m.get("active"):
                        return m
        return None

    def get_active_model(self, *a, **k):
        return self.get_active(*a, **k)

    def active(self, *a, **k):
        return self.get_active(*a, **k)

    def active_model(self, *a, **k):
        return self.get_active(*a, **k)

    def current(self, *a, **k):
        return self.get_active(*a, **k)

    def current_model(self, *a, **k):
        return self.get_active(*a, **k)

    def count(self):
        return len(self._models)

    def size(self):
        return len(self._models)

    def length(self):
        return len(self._models)

    def exists(self, *a, **k):
        data = _normalize_inputs(*a, **k)
        mid = data.get("id") or data.get("name") or data.get("model_id") or data.get("model")
        if mid is None and a and not isinstance(a[0], dict):
            mid = a[0]
        return self._find_key(mid) is not None

    def is_registered(self, *a, **k):
        return self.exists(*a, **k)

    def validate(self, model):
        if model is None:
            return False
        d = model if isinstance(model, dict) else _model_to_dict(model)
        if not isinstance(d, dict):
            return False
        return d.get("id") is not None or d.get("name") is not None

    def health(self):
        return {"status": "ok", "models": len(self._models), "active": len(self._active)}

    def status(self):
        return self.health()

    def to_list(self):
        return [_model_to_dict(m) for m in self.list()]

    def to_dict_list(self):
        return self.to_list()

    def export(self):
        return self.to_list()

    def dump(self):
        return self.to_list()

    def import_models(self, items):
        if isinstance(items, dict):
            out_list = []
            for k, v in items.items():
                if isinstance(v, dict):
                    v = dict(v)
                    v.setdefault("name", k)
                    v.setdefault("id", k)
                    out_list.append(v)
            items = out_list
        if not isinstance(items, (__builtins__["list"], tuple)):
            return []
        out = []
        for item in items:
            try:
                out.append(self.register(item))
            except Exception:
                continue
        return out

    def clear(self):
        with self._lock:
            n = len(self._models)
            self._models.clear()
            self._order.clear()
            self._active.clear()
        return n

    def reset(self):
        return self.clear()


AIModelRegistry = ModelRegistry
Registry = ModelRegistry
ModelManager = ModelRegistry
ModelStore = ModelRegistry

register = register_model
add = register_model
add_model = register_model
create = register_model
create_model = register_model
upsert = upsert
update = update
update_model = update_model
patch = patch
edit = edit
modify = modify
get = get
get_model = get_model
find = find
find_model = find_model
fetch = fetch
get_by_name = get_by_name
get_by_id = get_by_id
list = list_models  # noqa: A001
list_models = list_models
list_all = list_all
all_models = all_models
all = all  # noqa: A001
models = models
search = search
search_models = search_models
filter_models = filter_models
unregister = unregister
delete = delete
delete_model = delete_model
remove = remove
remove_model = remove_model
unregister_model = unregister_model
clear = clear
reset = reset
clear_all = clear_all
set_active = set_active
set_active_model = set_active_model
activate = activate
activate_model = activate_model
select = select
select_model = select_model
get_active = get_active
get_active_model = get_active_model
active = active
active_model = active_model
current = current
current_model = current_model
exists = exists
is_registered = is_registered
count = count
size = size
length = length
to_dict = to_dict
to_list = to_list
to_dict_list = to_dict_list
export = export_registry
export_registry = export_registry
export_models = export_models
dump = dump
import_models = import_models
import_registry = import_registry
load = load
save = save
validate = validate
is_active = is_active
health = health
status = status
ModelRegistryClass = ModelRegistry

__all__ = ["Model", "ModelRegistry", "AIModel", "ModelInfo", "RegisteredModel", "ModelEntry", "ModelRecord", "AIModelRegistry", "Registry", "ModelManager", "ModelStore"]
