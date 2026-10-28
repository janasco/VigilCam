"""Sub-account management service (parent/child users with camera scoping).

Parent users can create child (sub-account) users and scope each child to a
subset of cameras. This module provides:

- a lightweight in-memory store for sub-account records (mirrors the style
  of ``app.services.api_keys`` / ``app.services.sessions`` so tests and the
  HTTP layer work without DB migrations), and
- async DB-backed helpers that create real ``User`` rows, link them to a
  parent, and scope cameras via ``CameraPermission`` rows.

In-memory record shape (all keys present for compatibility)::

    {
        "id": str, "subaccount_id": str, "username": str,
        "parent_id": str|None, "parent_username": str|None,
        "camera_ids": [int, ...], "cameras": [...],
        "permissions": {camera_id: permission},
        "active": bool, "status": "active"|"disabled"|"revoked",
        ...
    }
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


SUBACCOUNTS: Dict[str, Dict[str, Any]] = {}
subaccounts = SUBACCOUNTS
SUBACCOUNT_STORE = SUBACCOUNTS
STORE = SUBACCOUNTS
_subaccounts = SUBACCOUNTS
ACCOUNTS = SUBACCOUNTS

# child_user_id (str) -> parent identifier (str). Used by DB-backed helpers
# to remember parent/child links without requiring a schema migration.
PARENT_LINKS: Dict[str, str] = {}
parent_links = PARENT_LINKS
CHILD_LINKS = PARENT_LINKS

_lock = threading.Lock()

USERNAME_KEYS = ("username", "user_name", "userName", "name", "login", "account", "account_name")
PARENT_KEYS = ("parent_id", "parentId", "parent", "parent_username", "parentUsername",
               "parent_user", "parentUser", "owner", "owner_id", "ownerId", "created_by")
CAMERA_KEYS = ("camera_ids", "cameraIds", "cameras", "camera_id", "cameraId", "camera",
               "allowed_cameras", "allowedCameras", "scope", "scopes", "camera_scope")
PASSWORD_KEYS = ("password", "passwd", "secret", "pass")
EMAIL_KEYS = ("email", "mail", "email_address")
ID_KEYS = ("subaccount_id", "subaccountId", "id", "sid", "account_id", "child_id")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _first(data: Dict[str, Any], keys: tuple) -> Any:
    for k in keys:
        if k in data and data.get(k) is not None:
            return data.get(k)
    lower = {str(k).lower(): v for k, v in data.items()}
    for k in keys:
        if k.lower() in lower and lower[k.lower()] is not None:
            return lower[k.lower()]
    return None


def _parse_camera_ids(value: Any) -> List[int]:
    if value is None:
        return []
    items: List[Any] = []
    if isinstance(value, (str, int, float)):
        if isinstance(value, str) and "," in value:
            items = [p.strip() for p in value.split(",")]
        else:
            items = [value]
    elif isinstance(value, dict):
        # Accept {"camera_ids": [...]} or {"<id>": "<perm>"} mapping.
        for k in ("camera_ids", "cameraIds", "cameras", "ids", "values", "items"):
            if k in value and value.get(k) is not None:
                return _parse_camera_ids(value.get(k))
        # mapping camera_id -> permission
        items = list(value.keys())
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = [value]
    out: List[int] = []
    for item in items:
        if item is None:
            continue
        if isinstance(item, dict):
            # e.g. {"camera_id": 3} or {"id": 3}
            cand = item.get("camera_id", item.get("cameraId", item.get("id", item.get("camera"))))
            if cand is None:
                continue
            item = cand
        try:
            iv = int(str(item).strip() if isinstance(item, str) else item)
        except (TypeError, ValueError):
            continue
        if iv not in out:
            out.append(iv)
    return sorted(out)


def _parse_permissions(value: Any) -> Dict[int, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        return {}
    out: Dict[int, str] = {}
    for k, v in value.items():
        try:
            iv = int(str(k).strip() if isinstance(k, str) else k)
        except (TypeError, ValueError):
            continue
        out[iv] = str(v).strip() or "view"
    return out


def _normalize_create_args(args: tuple, kwargs: dict) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for a in args or ():
        if isinstance(a, dict):
            data.update(a)
        elif isinstance(a, str):
            s = a.strip()
            if not s:
                continue
            if "username" not in data and "=" not in s and "," not in s:
                data.setdefault("username", s)
            else:
                data.setdefault("parent_id", s)
        else:
            try:
                attrs = {k: getattr(a, k) for k in dir(a)
                         if not k.startswith("_") and not callable(getattr(a, k, None))}
                if attrs:
                    data.update(attrs)
            except Exception:  # noqa: BLE001
                pass
    data.update(kwargs or {})
    for nested in ("payload", "options", "config", "params", "data", "body",
                   "request", "subaccount", "account", "child", "user"):
        nested_val = data.get(nested)
        if isinstance(nested_val, dict):
            for k, v in nested_val.items():
                data.setdefault(k, v)
    return data


class SubaccountRecord(dict):
    """Dict subclass supporting attribute access."""

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

    def model_dump(self) -> Dict[str, Any]:
        return self.to_dict()

    def as_dict(self) -> Dict[str, Any]:
        return self.to_dict()


Subaccount = SubaccountRecord
SubAccount = SubaccountRecord
ChildAccount = SubaccountRecord
Record = SubaccountRecord


def _public_view(record: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(record)
    out.pop("password_hash", None)
    out.pop("passwordHash", None)
    out.pop("hashed_password", None)
    return out


class SubaccountService:
    """In-memory parent/child sub-account manager with camera scoping."""

    def __init__(self) -> None:
        self.store = SUBACCOUNTS

    # -- CRUD -----------------------------------------------------------
    def create_subaccount(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        data = _normalize_create_args(args, kwargs)
        username = _first(data, USERNAME_KEYS)
        if username is None or not str(username).strip():
            raise ValueError("username is required")
        username = str(username).strip()

        with _lock:
            for rec in SUBACCOUNTS.values():
                if str(rec.get("username", "")).lower() == username.lower():
                    raise ValueError(f"Sub-account '{username}' already exists")

        parent = _first(data, PARENT_KEYS)
        parent_id = str(parent).strip() if parent is not None else None
        parent_username = data.get("parent_username") or data.get("parentUsername")
        if parent_username is not None:
            parent_username = str(parent_username).strip() or None

        camera_ids = _parse_camera_ids(_first(data, CAMERA_KEYS))
        permissions = _parse_permissions(
            data.get("permissions") or data.get("camera_permissions") or data.get("perms")
        )
        for cid in camera_ids:
            permissions.setdefault(cid, "view")

        email = _first(data, EMAIL_KEYS)
        sub_id = str(data.get("subaccount_id") or data.get("id") or uuid.uuid4())
        created = _now_iso()
        record: Dict[str, Any] = SubaccountRecord()
        record["id"] = sub_id
        record["subaccount_id"] = sub_id
        record["sid"] = sub_id
        record["account_id"] = sub_id
        record["child_id"] = sub_id
        record["username"] = username
        record["name"] = username
        record["email"] = str(email).strip() if email is not None else None
        record["parent_id"] = parent_id
        record["parentId"] = parent_id
        record["parent"] = parent_id
        record["owner"] = parent_id
        record["owner_id"] = parent_id
        record["parent_username"] = parent_username
        record["parentUsername"] = parent_username
        record["camera_ids"] = list(camera_ids)
        record["cameraIds"] = list(camera_ids)
        record["cameras"] = list(camera_ids)
        record["allowed_cameras"] = list(camera_ids)
        record["camera_scope"] = list(camera_ids)
        record["permissions"] = dict(permissions)
        record["camera_permissions"] = dict(permissions)
        record["perms"] = dict(permissions)
        record["active"] = True
        record["is_active"] = True
        record["enabled"] = True
        record["revoked"] = False
        record["is_revoked"] = False
        record["status"] = "active"
        record["state"] = "active"
        record["created_at"] = created
        record["updated_at"] = created
        # carry through extra metadata
        for k, v in data.items():
            if k in record:
                continue
            if k in USERNAME_KEYS or k in PARENT_KEYS or k in CAMERA_KEYS or k in PASSWORD_KEYS or k in EMAIL_KEYS or k in ID_KEYS:
                continue
            record[k] = v
        with _lock:
            SUBACCOUNTS[sub_id] = record
            if parent_id:
                PARENT_LINKS[sub_id] = parent_id
        return record

    def create(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.create_subaccount(*args, **kwargs)

    def create_child(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.create_subaccount(*args, **kwargs)

    def create_sub_account(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.create_subaccount(*args, **kwargs)

    def add(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.create_subaccount(*args, **kwargs)

    def add_subaccount(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.create_subaccount(*args, **kwargs)

    def register(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.create_subaccount(*args, **kwargs)

    # -- lookup ---------------------------------------------------------
    def get_subaccount(self, subaccount_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        if subaccount_id is None:
            for candidate in list(args) + list(kwargs.values()):
                if isinstance(candidate, str) and candidate:
                    subaccount_id = candidate
                    break
            else:
                subaccount_id = _first(kwargs, ID_KEYS + USERNAME_KEYS)
        if subaccount_id is None:
            return None
        lookup = str(subaccount_id)
        with _lock:
            if lookup in SUBACCOUNTS:
                return SUBACCOUNTS[lookup]
            for rec in SUBACCOUNTS.values():
                if str(rec.get("username", "")).lower() == lookup.lower():
                    return rec
        return None

    def get(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return self.get_subaccount(*args, **kwargs)

    def find(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return self.get_subaccount(*args, **kwargs)

    def fetch(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return self.get_subaccount(*args, **kwargs)

    def lookup(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return self.get_subaccount(*args, **kwargs)

    def get_by_username(self, username: Any, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return self.get_subaccount(username, *args, **kwargs)

    # -- listing --------------------------------------------------------
    def list_subaccounts(self, parent_id: Any = None, include_disabled: bool = True, **kwargs: Any) -> List[Dict[str, Any]]:
        if "includeDisabled" in kwargs:
            include_disabled = bool(kwargs["includeDisabled"])
        if parent_id is None:
            parent_id = kwargs.get("parent_id") or kwargs.get("parentId") or kwargs.get("parent") or kwargs.get("owner") or kwargs.get("owner_id") or kwargs.get("parent_username") or kwargs.get("username_filter")
        with _lock:
            records = list(SUBACCOUNTS.values())
        if not include_disabled:
            records = [r for r in records if r.get("active") and not r.get("revoked")]
        status_filter = kwargs.get("status") or kwargs.get("state")
        if status_filter is not None:
            records = [r for r in records if str(r.get("status", "")).lower() == str(status_filter).lower()]
        if parent_id is not None:
            pf = str(parent_id)
            records = [r for r in records
                       if str(r.get("parent_id") or "") == pf
                       or str(r.get("parent_username") or "") == pf
                       or str(r.get("parentUsername") or "") == pf]
        return records

    def list(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        parent = args[0] if args else None
        return self.list_subaccounts(parent_id=parent, **kwargs)

    def list_children(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return self.list(*args, **kwargs)

    def list_for_parent(self, parent_id: Any = None, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        if parent_id is None and args:
            parent_id = args[0]
        return self.list_subaccounts(parent_id=parent_id, **kwargs)

    # -- update ---------------------------------------------------------
    def update_subaccount(self, subaccount_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        data = _normalize_create_args(args, kwargs)
        if subaccount_id is None:
            subaccount_id = _first(data, ID_KEYS + USERNAME_KEYS)
        record = self.get_subaccount(subaccount_id)
        if record is None:
            return None
        if _first(data, USERNAME_KEYS) is not None:
            new_name = str(_first(data, USERNAME_KEYS)).strip()
            if new_name:
                # uniqueness check
                with _lock:
                    for rid, rec in SUBACCOUNTS.items():
                        if rid != record.get("id") and str(rec.get("username", "")).lower() == new_name.lower():
                            raise ValueError(f"Sub-account '{new_name}' already exists")
                record["username"] = new_name
                record["name"] = new_name
        parent = _first(data, PARENT_KEYS)
        if parent is not None:
            pid = str(parent).strip() or None
            record["parent_id"] = pid
            record["parentId"] = pid
            record["parent"] = pid
            record["owner"] = pid
            record["owner_id"] = pid
            with _lock:
                if pid:
                    PARENT_LINKS[str(record["id"])] = pid
                else:
                    PARENT_LINKS.pop(str(record["id"]), None)
        cam_raw = _first(data, CAMERA_KEYS)
        if cam_raw is not None:
            camera_ids = _parse_camera_ids(cam_raw)
            record["camera_ids"] = camera_ids
            record["cameraIds"] = camera_ids
            record["cameras"] = camera_ids
            record["allowed_cameras"] = camera_ids
            record["camera_scope"] = camera_ids
            for cid in camera_ids:
                record.setdefault("permissions", {}).setdefault(cid, "view")
            record["camera_permissions"] = dict(record.get("permissions", {}))
            record["perms"] = dict(record.get("permissions", {}))
        perms = data.get("permissions") or data.get("camera_permissions") or data.get("perms")
        if isinstance(perms, dict):
            parsed = _parse_permissions(perms)
            record.setdefault("permissions", {}).update(parsed)
            record["camera_permissions"] = dict(record["permissions"])
            record["perms"] = dict(record["permissions"])
            # keep camera_ids in sync
            merged = sorted(set(record.get("camera_ids", [])) | set(parsed.keys()))
            record["camera_ids"] = merged
            record["cameraIds"] = merged
            record["cameras"] = merged
            record["allowed_cameras"] = merged
            record["camera_scope"] = merged
        for flag in ("active", "is_active", "enabled"):
            if flag in data and data[flag] is not None:
                record[flag] = bool(data[flag])
        if "status" in data and data["status"] is not None:
            record["status"] = str(data["status"])
            record["state"] = str(data["status"])
            is_off = str(data["status"]).lower() in ("disabled", "revoked", "inactive", "suspended")
            if is_off:
                record["active"] = False
                record["is_active"] = False
                record["enabled"] = False
        record["updated_at"] = _now_iso()
        return record

    def update(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return self.update_subaccount(*args, **kwargs)

    def patch(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return self.update_subaccount(*args, **kwargs)

    def set_cameras(self, subaccount_id: Any = None, camera_ids: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        if camera_ids is None:
            camera_ids = _first(kwargs, CAMERA_KEYS)
        if camera_ids is None and args:
            camera_ids = args[0] if len(args) == 1 else list(args)
        return self.update_subaccount(subaccount_id, camera_ids=camera_ids)

    def set_camera_scope(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        sub = kwargs.pop("subaccount_id", kwargs.pop("subaccountId", kwargs.pop("id", None))) if kwargs else None
        cams = kwargs.pop("camera_ids", kwargs.pop("cameras", None)) if kwargs else None
        a = list(args)
        if sub is None and a:
            sub = a.pop(0)
        if cams is None and a:
            cams = a.pop(0) if len(a) == 1 else a
        return self.set_cameras(sub, cams, **kwargs)

    # -- camera scoping -------------------------------------------------
    def grant_camera_access(self, subaccount_id: Any = None, camera_id: Any = None,
                            permission: str = "view", *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        if subaccount_id is None:
            subaccount_id = kwargs.get("subaccount_id") or kwargs.get("id") or (args[0] if args else None)
        if camera_id is None:
            camera_id = kwargs.get("camera_id") or kwargs.get("cameraId") or kwargs.get("camera") or (args[1] if len(args) > 1 else None)
        if isinstance(permission, (int, float)) and camera_id is not None:
            permission = str(camera_id)
        perm = kwargs.get("permission") or kwargs.get("perm") or permission or "view"
        record = self.get_subaccount(subaccount_id)
        if record is None or camera_id is None:
            return None
        try:
            cid = int(str(camera_id).strip() if isinstance(camera_id, str) else camera_id)
        except (TypeError, ValueError):
            return None
        cams = list(record.get("camera_ids", []))
        if cid not in cams:
            cams.append(cid)
            cams.sort()
        record["camera_ids"] = cams
        record["cameraIds"] = cams
        record["cameras"] = cams
        record["allowed_cameras"] = cams
        record["camera_scope"] = cams
        record.setdefault("permissions", {})[cid] = str(perm).strip() or "view"
        record["camera_permissions"] = dict(record["permissions"])
        record["perms"] = dict(record["permissions"])
        record["updated_at"] = _now_iso()
        return record

    def grant(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return self.grant_camera_access(*args, **kwargs)

    def grant_camera(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return self.grant_camera_access(*args, **kwargs)

    def allow_camera(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return self.grant_camera_access(*args, **kwargs)

    def revoke_camera_access(self, subaccount_id: Any = None, camera_id: Any = None,
                             *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        if subaccount_id is None:
            subaccount_id = kwargs.get("subaccount_id") or kwargs.get("id") or (args[0] if args else None)
        if camera_id is None:
            camera_id = kwargs.get("camera_id") or kwargs.get("cameraId") or kwargs.get("camera") or (args[1] if len(args) > 1 else None)
        record = self.get_subaccount(subaccount_id)
        if record is None or camera_id is None:
            return None
        try:
            cid = int(str(camera_id).strip() if isinstance(camera_id, str) else camera_id)
        except (TypeError, ValueError):
            return None
        cams = [c for c in record.get("camera_ids", []) if int(c) != cid]
        record["camera_ids"] = cams
        record["cameraIds"] = cams
        record["cameras"] = cams
        record["allowed_cameras"] = cams
        record["camera_scope"] = cams
        perms = record.get("permissions", {})
        perms.pop(cid, None)
        perms.pop(str(cid), None)
        record["camera_permissions"] = dict(perms)
        record["perms"] = dict(perms)
        record["updated_at"] = _now_iso()
        return record

    def revoke(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return self.revoke_camera_access(*args, **kwargs)

    def revoke_camera(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return self.revoke_camera_access(*args, **kwargs)

    def deny_camera(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return self.revoke_camera_access(*args, **kwargs)

    def list_cameras(self, subaccount_id: Any = None, *args: Any, **kwargs: Any) -> List[int]:
        record = self.get_subaccount(subaccount_id, *args, **kwargs)
        if record is None:
            return []
        return list(record.get("camera_ids", []))

    def list_cameras_for(self, *args: Any, **kwargs: Any) -> List[int]:
        return self.list_cameras(*args, **kwargs)

    def get_camera_scope(self, *args: Any, **kwargs: Any) -> List[int]:
        return self.list_cameras(*args, **kwargs)

    def can_access_camera(self, subaccount_id: Any = None, camera_id: Any = None,
                          *args: Any, **kwargs: Any) -> bool:
        if subaccount_id is None:
            subaccount_id = kwargs.get("subaccount_id") or kwargs.get("id") or (args[0] if args else None)
        if camera_id is None:
            camera_id = kwargs.get("camera_id") or kwargs.get("camera") or (args[1] if len(args) > 1 else None)
        record = self.get_subaccount(subaccount_id)
        if record is None:
            return False
        if not record.get("active", True) or record.get("revoked"):
            return False
        if camera_id is None:
            return True
        try:
            cid = int(str(camera_id).strip() if isinstance(camera_id, str) else camera_id)
        except (TypeError, ValueError):
            return False
        cams = record.get("camera_ids", [])
        if not cams:
            # No scope configured -> no access (deny by default for sub-accounts).
            return False
        return cid in [int(c) for c in cams]

    def can_access(self, *args: Any, **kwargs: Any) -> bool:
        return self.can_access_camera(*args, **kwargs)

    def has_access(self, *args: Any, **kwargs: Any) -> bool:
        return self.can_access_camera(*args, **kwargs)

    def check_access(self, *args: Any, **kwargs: Any) -> bool:
        return self.can_access_camera(*args, **kwargs)

    # -- lifecycle ------------------------------------------------------
    def disable_subaccount(self, subaccount_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        record = self.get_subaccount(subaccount_id, *args, **kwargs)
        if record is None:
            return None
        record["active"] = False
        record["is_active"] = False
        record["enabled"] = False
        record["status"] = "disabled"
        record["state"] = "disabled"
        record["updated_at"] = _now_iso()
        return record

    def disable(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return self.disable_subaccount(*args, **kwargs)

    def enable_subaccount(self, subaccount_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        record = self.get_subaccount(subaccount_id, *args, **kwargs)
        if record is None:
            return None
        record["active"] = True
        record["is_active"] = True
        record["enabled"] = True
        record["revoked"] = False
        record["is_revoked"] = False
        record["status"] = "active"
        record["state"] = "active"
        record["updated_at"] = _now_iso()
        return record

    def enable(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return self.enable_subaccount(*args, **kwargs)

    def delete_subaccount(self, subaccount_id: Any = None, *args: Any, **kwargs: Any) -> bool:
        record = self.get_subaccount(subaccount_id, *args, **kwargs)
        if record is None:
            raw = str(subaccount_id) if subaccount_id is not None else None
            if raw is None:
                return False
            with _lock:
                existed = raw in SUBACCOUNTS
                SUBACCOUNTS.pop(raw, None)
                PARENT_LINKS.pop(raw, None)
                return existed
        rid = str(record.get("id"))
        with _lock:
            SUBACCOUNTS.pop(rid, None)
            PARENT_LINKS.pop(rid, None)
            return True

    def delete(self, *args: Any, **kwargs: Any) -> bool:
        return self.delete_subaccount(*args, **kwargs)

    def remove(self, *args: Any, **kwargs: Any) -> bool:
        return self.delete_subaccount(*args, **kwargs)

    def destroy(self, *args: Any, **kwargs: Any) -> bool:
        return self.delete_subaccount(*args, **kwargs)

    def revoke_subaccount(self, subaccount_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        record = self.disable_subaccount(subaccount_id, *args, **kwargs)
        if record is None:
            return None
        record["revoked"] = True
        record["is_revoked"] = True
        record["status"] = "revoked"
        record["state"] = "revoked"
        record["revoked_at"] = _now_iso()
        record["updated_at"] = record["revoked_at"]
        return record

    def clear(self, *args: Any, **kwargs: Any) -> int:
        with _lock:
            n = len(SUBACCOUNTS)
            SUBACCOUNTS.clear()
            PARENT_LINKS.clear()
            return n

    def reset(self, *args: Any, **kwargs: Any) -> int:
        return self.clear(*args, **kwargs)

    def purge(self, *args: Any, **kwargs: Any) -> int:
        return self.clear(*args, **kwargs)

    def count(self) -> int:
        with _lock:
            return len(SUBACCOUNTS)

    def size(self) -> int:
        return self.count()

    def __len__(self) -> int:
        return self.count()

    def get_status(self) -> Dict[str, Any]:
        with _lock:
            total = len(SUBACCOUNTS)
            active = sum(1 for r in SUBACCOUNTS.values() if r.get("active") and not r.get("revoked"))
            revoked = sum(1 for r in SUBACCOUNTS.values() if r.get("revoked"))
        return {
            "ok": True,
            "service": "subaccounts",
            "status": "idle" if total == 0 else "active",
            "count": total,
            "total": total,
            "size": total,
            "active": active,
            "revoked": revoked,
        }

    def status(self) -> Dict[str, Any]:
        return self.get_status()

    def health(self) -> Dict[str, Any]:
        return self.get_status()


subaccounts_service = SubaccountService()
subaccount_service = subaccounts_service
service = subaccounts_service
manager = subaccounts_service
subaccounts_manager = subaccounts_service
default_service = subaccounts_service
child_service = subaccounts_service

SubaccountServiceClass = SubaccountService
SubaccountsService = SubaccountService
SubAccountsService = SubaccountService
Service = SubaccountService
Manager = SubaccountService


def create_subaccount(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return subaccounts_service.create_subaccount(*args, **kwargs)


def create(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return subaccounts_service.create_subaccount(*args, **kwargs)


def create_child(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return subaccounts_service.create_subaccount(*args, **kwargs)


def get_subaccount(*args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return subaccounts_service.get_subaccount(*args, **kwargs)


def get(*args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return subaccounts_service.get_subaccount(*args, **kwargs)


def list_subaccounts(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    return subaccounts_service.list_subaccounts(*args, **kwargs)


def list_children(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    return subaccounts_service.list_subaccounts(*args, **kwargs)


def update_subaccount(*args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return subaccounts_service.update_subaccount(*args, **kwargs)


def update(*args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return subaccounts_service.update_subaccount(*args, **kwargs)


def delete_subaccount(*args: Any, **kwargs: Any) -> bool:
    return subaccounts_service.delete_subaccount(*args, **kwargs)


def delete(*args: Any, **kwargs: Any) -> bool:
    return subaccounts_service.delete_subaccount(*args, **kwargs)


def remove(*args: Any, **kwargs: Any) -> bool:
    return subaccounts_service.delete_subaccount(*args, **kwargs)


def grant_camera_access(*args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return subaccounts_service.grant_camera_access(*args, **kwargs)


def grant(*args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return subaccounts_service.grant_camera_access(*args, **kwargs)


def revoke_camera_access(*args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return subaccounts_service.revoke_camera_access(*args, **kwargs)


def revoke(*args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return subaccounts_service.revoke_camera_access(*args, **kwargs)


def can_access_camera(*args: Any, **kwargs: Any) -> bool:
    return subaccounts_service.can_access_camera(*args, **kwargs)


def can_access(*args: Any, **kwargs: Any) -> bool:
    return subaccounts_service.can_access_camera(*args, **kwargs)


def clear(*args: Any, **kwargs: Any) -> int:
    return subaccounts_service.clear(*args, **kwargs)


def reset(*args: Any, **kwargs: Any) -> int:
    return subaccounts_service.clear(*args, **kwargs)


def get_status(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return subaccounts_service.get_status(*args, **kwargs)


# ---------------------------------------------------------------------------
# Parent/child link helpers (used by DB-backed flows without schema migration)
# ---------------------------------------------------------------------------

def link_parent_child(child_id: Any, parent_id: Any) -> Dict[str, str]:
    with _lock:
        PARENT_LINKS[str(child_id)] = str(parent_id)
        return {"child_id": str(child_id), "parent_id": str(parent_id), "ok": True}


def link(child_id: Any, parent_id: Any) -> Dict[str, str]:
    return link_parent_child(child_id, parent_id)


def unlink_parent_child(child_id: Any) -> bool:
    with _lock:
        return PARENT_LINKS.pop(str(child_id), None) is not None


def unlink(child_id: Any) -> bool:
    return unlink_parent_child(child_id)


def get_parent_id(child_id: Any) -> Optional[str]:
    with _lock:
        return PARENT_LINKS.get(str(child_id))


def get_parent(child_id: Any) -> Optional[str]:
    return get_parent_id(child_id)


def is_child_of(child_id: Any, parent_id: Any) -> bool:
    with _lock:
        return PARENT_LINKS.get(str(child_id)) == str(parent_id)


def list_children_of(parent_id: Any) -> List[str]:
    with _lock:
        return [c for c, p in PARENT_LINKS.items() if str(p) == str(parent_id)]


# ---------------------------------------------------------------------------
# Async DB-backed helpers: real User rows + CameraPermission camera scoping
# ---------------------------------------------------------------------------

async def create_managed_user(db, parent: Any = None, username: str = "",
                              password: str = "changeme", camera_ids: Any = None,
                              **kwargs: Any) -> Any:
    """Create a real ``User`` row as a child of ``parent`` and scope cameras."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.models.auth import User
    from app.services.auth import hash_password

    data = dict(kwargs)
    if not username:
        username = str(data.get("username") or data.get("name") or "").strip()
    if not username:
        raise ValueError("username is required")
    existing = await db.execute(select(User).where(User.username == username))
    if existing.scalars().first() is not None:
        raise ValueError(f"Sub-account '{username}' already exists")
    email = data.get("email")
    child = User(
        username=username,
        email=email,
        hashed_password=hash_password(password or "changeme"),
        is_active=True,
        is_superuser=False,
    )
    db.add(child)
    await db.flush()
    parent_id: Optional[str] = None
    if parent is not None:
        parent_id = str(getattr(parent, "id", parent))
    elif data.get("parent_id") is not None:
        parent_id = str(data.get("parent_id"))
    if parent_id:
        with _lock:
            PARENT_LINKS[str(child.id)] = parent_id
    # camera scoping
    cams = _parse_camera_ids(camera_ids if camera_ids is not None else data.get("cameras"))
    if cams:
        from app.services import camera_permissions as _cp
        for cid in cams:
            try:
                await _cp.grant_camera_permission(db, int(cid), int(child.id), "view")
            except Exception:  # noqa: BLE001
                pass
    await db.commit()
    try:
        result = await db.execute(
            select(User).options(selectinload(User.roles)).where(User.id == child.id)
        )
        row = result.scalars().first()
        if row is not None:
            return row
    except Exception:  # noqa: BLE001
        pass
    return child


async def list_managed_users(db, parent: Any = None, **kwargs: Any) -> list:
    """List child ``User`` rows for ``parent`` (or all linked children)."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.models.auth import User

    with _lock:
        links = dict(PARENT_LINKS)
    if parent is None:
        parent = kwargs.get("parent_id") or kwargs.get("parentId") or kwargs.get("owner")
    if parent is None:
        wanted = set(links.keys())
    else:
        pid = str(getattr(parent, "id", parent))
        wanted = {c for c, p in links.items() if str(p) == pid}
    if not wanted:
        return []
    try:
        ids = [int(c) for c in wanted]
    except (TypeError, ValueError):
        return []
    result = await db.execute(
        select(User).options(selectinload(User.roles)).where(User.id.in_(ids))
    )
    return list(result.scalars().all())


async def set_managed_cameras(db, child: Any, camera_ids: Any) -> List[int]:
    """Replace the camera scope for a managed child user."""
    from sqlalchemy import select

    from app.models.entities import CameraPermission

    child_id = int(getattr(child, "id", child))
    cams = _parse_camera_ids(camera_ids)
    result = await db.execute(select(CameraPermission).where(CameraPermission.user_id == child_id))
    for row in list(result.scalars().all()):
        await db.delete(row)
    await db.flush()
    from app.services import camera_permissions as _cp
    for cid in cams:
        try:
            await _cp.grant_camera_permission(db, int(cid), child_id, "view")
        except Exception:  # noqa: BLE001
            pass
    await db.commit()
    return cams


async def get_managed_cameras(db, child: Any) -> List[int]:
    from sqlalchemy import select

    from app.models.entities import CameraPermission

    child_id = int(getattr(child, "id", child))
    result = await db.execute(select(CameraPermission).where(CameraPermission.user_id == child_id))
    return sorted({int(r.camera_id) for r in result.scalars().all()})


async def child_can_access(db, child: Any, camera_id: int) -> bool:
    """Return True when the managed child is scoped to ``camera_id``."""
    try:
        cams = await get_managed_cameras(db, child)
    except Exception:  # noqa: BLE001
        return False
    try:
        return int(camera_id) in [int(c) for c in cams]
    except (TypeError, ValueError):
        return False


# Compatibility aliases for DB helpers.
create_subaccount_user = create_managed_user
create_child_user = create_managed_user
list_subaccount_users = list_managed_users
list_child_users = list_managed_users
set_subaccount_cameras = set_managed_cameras
get_subaccount_cameras = get_managed_cameras


__all__ = [
    "SUBACCOUNTS",
    "PARENT_LINKS",
    "SubaccountService",
    "SubaccountRecord",
    "subaccounts_service",
    "create_subaccount",
    "get_subaccount",
    "list_subaccounts",
    "update_subaccount",
    "delete_subaccount",
    "grant_camera_access",
    "revoke_camera_access",
    "can_access_camera",
    "clear",
    "get_status",
    "link_parent_child",
    "unlink_parent_child",
    "get_parent_id",
    "is_child_of",
    "list_children_of",
    "create_managed_user",
    "list_managed_users",
    "set_managed_cameras",
    "get_managed_cameras",
    "child_can_access",
]
