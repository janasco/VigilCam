from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

_NODES: Dict[Any, "Node"] = {}
_NODES_BY_NAME: Dict[str, Any] = {}
_LOCK = threading.RLock()
_NEXT_ID = [1]


ID_FIELDS = ("id", "node_id", "node", "nodeId", "uuid", "key", "node_key", "token_id")
NAME_FIELDS = ("name", "node_name", "label", "title", "hostname", "host_name", "alias", "node_alias", "display_name")
HOST_FIELDS = ("host", "ip", "ip_address", "address", "hostname", "remote_host", "server", "server_host", "endpoint_host", "remote")
PORT_FIELDS = ("port", "node_port", "server_port", "listen_port", "http_port")
URL_FIELDS = ("url", "base_url", "endpoint", "endpoint_url", "remote_url", "server_url", "address_url", "href", "link", "node_url")
TOKEN_FIELDS = ("token", "api_token", "auth_token", "access_token", "secret", "api_key", "key", "password")
STATUS_FIELDS = ("status", "state", "health", "condition", "node_status", "node_state")
ONLINE_FIELDS = ("online", "is_online", "connected", "reachable", "alive", "available", "up")
META_FIELDS = ("metadata", "meta", "info", "details", "config", "configuration", "extra", "payload", "data", "properties", "attributes", "tags")
CAMERA_FIELDS = ("cameras", "camera_ids", "cameras_ids", "monitors", "camera_list", "monitor_ids")
VERSION_FIELDS = ("version", "app_version", "software_version", "node_version", "build", "release")
CPU_FIELDS = ("cpu", "cpu_usage", "cpu_percent", "load", "load_average", "cpu_load")
LAST_SEEN_FIELDS = ("last_seen", "last_seen_at", "last_heartbeat", "heartbeat_at", "seen_at", "timestamp", "time", "updated_at", "last_update", "last_contact")
CREATED_FIELDS = ("created_at", "created", "registered_at", "registration_time")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _clean_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (dict, list, tuple, set)):
        return None
    s = str(value).strip()
    return s or None


def _coerce_id(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if hasattr(value, "id") and not isinstance(value, (int, str, float)):
        try:
            value = getattr(value, "id")
        except Exception:
            pass
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        try:
            if value.is_integer():
                return int(value)
        except Exception:
            pass
        return value
    s = str(value).strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        try:
            f = float(s)
            if f.is_integer():
                return int(f)
        except ValueError:
            pass
    return s


def _coerce_bool(value: Any, default: Optional[bool] = None) -> Optional[bool]:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if s in {"", "none", "null"}:
        return default
    if s in {"1", "true", "yes", "on", "y", "online", "connected", "reachable", "alive", "available", "up", "healthy", "ok", "active"}:
        return True
    if s in {"0", "false", "no", "off", "n", "offline", "disconnected", "unreachable", "down", "inactive", "dead"}:
        return False
    return default


def _coerce_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return None


def _to_iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (int, float)):
        stamp = float(value)
        if abs(stamp) > 1e12:
            stamp = stamp / 1000.0
        try:
            return datetime.utcfromtimestamp(stamp).isoformat()
        except (OSError, ValueError, OverflowError):
            return None
    s = str(value).strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s2 = s[:-1] + "+00:00"
        else:
            s2 = s
        dt = datetime.fromisoformat(s2)
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        return dt.isoformat()
    except ValueError:
        pass
    try:
        stamp = float(s)
        if abs(stamp) > 1e12:
            stamp = stamp / 1000.0
        return datetime.utcfromtimestamp(stamp).isoformat()
    except (ValueError, OSError, OverflowError):
        return s


def _flatten(payload: Any) -> Dict[str, Any]:
    if isinstance(payload, dict):
        out: Dict[str, Any] = {}
        for key in ("node", "child", "child_node", "registration", "status", "heartbeat", "data", "payload", "body", "params", "options", "config", "settings", "meta", "metadata", "info"):
            nested = payload.get(key)
            if isinstance(nested, dict):
                out.update(nested)
        out.update(payload)
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
    return {}


def _first_present(flat: Dict[str, Any], fields: Iterable[str]) -> tuple[bool, Any]:
    for name in fields:
        if name in flat and flat.get(name) is not None:
            return True, flat.get(name)
    lower = {str(k).lower(): v for k, v in flat.items()}
    for name in fields:
        if name.lower() in lower and lower[name.lower()] is not None:
            return True, lower[name.lower()]
    return False, None


def _normalize_status(value: Any) -> str:
    if value is None:
        return "online"
    s = str(value).strip()
    if not s:
        return "online"
    low = s.lower()
    if low in {"online", "ok", "healthy", "active", "connected", "reachable", "alive", "available", "up", "good", "running"}:
        return "online"
    if low in {"offline", "down", "inactive", "disconnected", "unreachable", "dead", "stopped"}:
        return "offline"
    if low in {"error", "failed", "unhealthy", "degraded"}:
        return low
    return s


def _normalize_inputs(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
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
            if not any(k in data for k in ("id", "node_id", "name")):
                data["_positional"] = a
    data.update(kwargs)
    for nested_key in ("payload", "options", "config", "params", "data", "body", "request", "settings"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    if "_positional" in data and data.get("_positional") is not None:
        pos = data.pop("_positional")
        if "id" not in data and "node_id" not in data and "name" not in data:
            data["id"] = pos
    return data


@dataclass
class Node:
    id: Any = None
    name: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    url: Optional[str] = None
    token: Optional[str] = None
    status: str = "online"
    online: bool = True
    healthy: bool = True
    last_seen: Optional[datetime] = None
    last_heartbeat: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    cameras: List[Any] = field(default_factory=list)
    version: Optional[str] = None
    cpu: Optional[Any] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.id is None:
            self.id = _NEXT_ID[0]
            _NEXT_ID[0] += 1
        if self.name is None:
            self.name = f"node-{self.id}"
        now = _now()
        if self.created_at is None:
            self.created_at = now
        if self.updated_at is None:
            self.updated_at = self.created_at
        if self.last_seen is None:
            self.last_seen = self.created_at
        if self.last_heartbeat is None:
            self.last_heartbeat = self.last_seen
        if self.metadata is None:
            self.metadata = {}
        if self.cameras is None:
            self.cameras = []
        if self.extra is None:
            self.extra = {}

    def _all_fields(self) -> Dict[str, Any]:
        out = {
            "id": self.id,
            "node_id": self.id,
            "name": self.name,
            "node_name": self.name,
            "host": self.host,
            "ip": self.host,
            "ip_address": self.host,
            "address": self.host,
            "hostname": self.host,
            "port": self.port,
            "url": self.url,
            "base_url": self.url,
            "endpoint": self.url,
            "endpoint_url": self.url,
            "remote_url": self.url,
            "server_url": self.url,
            "address_url": self.url,
            "href": self.url,
            "link": self.url,
            "node_url": self.url,
            "token": self.token,
            "api_token": self.token,
            "auth_token": self.token,
            "access_token": self.token,
            "secret": self.token,
            "api_key": self.token,
            "key": self.token,
            "password": self.token,
            "status": self.status,
            "state": self.status,
            "node_status": self.status,
            "node_state": self.status,
            "condition": self.status,
            "health": self.status,
            "online": self.online,
            "is_online": self.online,
            "connected": self.online,
            "reachable": self.online,
            "alive": self.online,
            "available": self.online,
            "up": self.online,
            "healthy": self.healthy,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
            "last_seen_at": self.last_seen.isoformat() if self.last_seen else None,
            "last_heartbeat": self.last_heartbeat.isoformat() if self.last_heartbeat else None,
            "heartbeat_at": self.last_heartbeat.isoformat() if self.last_heartbeat else None,
            "seen_at": self.last_seen.isoformat() if self.last_seen else None,
            "timestamp": self.last_seen.isoformat() if self.last_seen else None,
            "time": self.last_seen.isoformat() if self.last_seen else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "last_update": self.updated_at.isoformat() if self.updated_at else None,
            "last_contact": self.last_seen.isoformat() if self.last_seen else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "created": self.created_at.isoformat() if self.created_at else None,
            "registered_at": self.created_at.isoformat() if self.created_at else None,
            "registration_time": self.created_at.isoformat() if self.created_at else None,
            "metadata": self.metadata,
            "meta": self.metadata,
            "info": self.metadata,
            "details": self.metadata,
            "config": self.metadata,
            "configuration": self.metadata,
            "extra": self.metadata,
            "payload": self.metadata,
            "data": self.metadata,
            "properties": self.metadata,
            "attributes": self.metadata,
            "tags": self.metadata,
            "cameras": self.cameras,
            "camera_ids": self.cameras,
            "cameras_ids": self.cameras,
            "monitors": self.cameras,
            "camera_list": self.cameras,
            "monitor_ids": self.cameras,
            "version": self.version,
            "app_version": self.version,
            "software_version": self.version,
            "node_version": self.version,
            "build": self.version,
            "release": self.version,
            "cpu": self.cpu,
            "cpu_usage": self.cpu,
            "cpu_percent": self.cpu,
            "load": self.cpu,
            "load_average": self.cpu,
            "cpu_load": self.cpu,
            "ok": True,
            "success": True,
            "registered": True,
        }
        out.update(self.extra or {})
        return out

    def to_dict(self) -> Dict[str, Any]:
        return dict(self._all_fields())

    def dict(self) -> Dict[str, Any]:
        return self.to_dict()

    def model_dump(self) -> Dict[str, Any]:
        return self.to_dict()

    def as_dict(self) -> Dict[str, Any]:
        return self.to_dict()

    def __getitem__(self, key: Any) -> Any:
        fields = self._all_fields()
        if key in fields:
            return fields[key]
        alias_map = {
            "state": "status",
            "node_state": "status",
            "node_status": "status",
            "condition": "status",
            "health": "status",
            "online_status": "status",
            "is_online": "online",
            "connected": "online",
            "reachable": "online",
            "alive": "online",
            "available": "online",
            "up": "online",
            "meta": "metadata",
            "details": "metadata",
            "info": "metadata",
            "config": "metadata",
            "data": "metadata",
            "extra": "metadata",
            "monitors": "cameras",
            "camera_list": "cameras",
        }
        if key in alias_map:
            return fields.get(alias_map[key])
        if hasattr(self, key):
            try:
                return getattr(self, key)
            except Exception:
                pass
        return (self.extra or {}).get(key)

    def get(self, key: Any, default: Any = None) -> Any:
        try:
            val = self[key]
        except Exception:
            val = None
        if val is None and key not in self._all_fields():
            return default
        return val

    def __bool__(self) -> bool:
        return True


def _find_node(node_id: Any = None, name: Any = None, host: Any = None, data: Optional[Dict[str, Any]] = None) -> Optional[Node]:
    data = data or {}
    if node_id is None:
        present, val = _first_present(data, ID_FIELDS)
        if present:
            node_id = val
    if name is None:
        present, val = _first_present(data, NAME_FIELDS)
        if present:
            name = val
    if host is None:
        present, val = _first_present(data, HOST_FIELDS)
        if present:
            host = val
    cid = _coerce_id(node_id)
    if cid is not None and cid in _NODES:
        return _NODES[cid]
    if name is not None:
        n = str(name).strip()
        if n in _NODES_BY_NAME:
            nid = _NODES_BY_NAME[n]
            if nid in _NODES:
                return _NODES[nid]
        for node in _NODES.values():
            if str(node.name) == n:
                return node
    if host is not None:
        h = str(host).strip()
        for node in _NODES.values():
            if node.host is not None and str(node.host).strip() == h:
                return node
    return None


def _extract_node_fields(data: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    present, val = _first_present(data, ID_FIELDS)
    if present:
        out["id"] = _coerce_id(val)
    present, val = _first_present(data, NAME_FIELDS)
    if present:
        out["name"] = _clean_str(val)
    present, val = _first_present(data, HOST_FIELDS)
    if present:
        out["host"] = _clean_str(val)
    present, val = _first_present(data, PORT_FIELDS)
    if present:
        out["port"] = _coerce_int(val)
    present, val = _first_present(data, URL_FIELDS)
    if present:
        out["url"] = _clean_str(val)
    present, val = _first_present(data, TOKEN_FIELDS)
    if present:
        out["token"] = _clean_str(val)
    present, val = _first_present(data, STATUS_FIELDS)
    if present:
        out["status"] = _normalize_status(val)
    present, val = _first_present(data, ONLINE_FIELDS)
    if present:
        b = _coerce_bool(val)
        if b is not None:
            out["online"] = b
    present, val = _first_present(data, META_FIELDS)
    if present:
        if isinstance(val, dict):
            out["metadata"] = val
        else:
            out["metadata"] = {"value": val}
    present, val = _first_present(data, CAMERA_FIELDS)
    if present:
        if isinstance(val, (list, tuple, set)):
            out["cameras"] = list(val)
        else:
            out["cameras"] = [val]
    present, val = _first_present(data, VERSION_FIELDS)
    if present:
        out["version"] = _clean_str(val)
    present, val = _first_present(data, CPU_FIELDS)
    if present:
        out["cpu"] = val
    present, val = _first_present(data, LAST_SEEN_FIELDS)
    if present:
        iso = _to_iso(val)
        if iso is not None:
            out["last_seen"] = iso
    present, val = _first_present(data, CREATED_FIELDS)
    if present:
        iso = _to_iso(val)
        if iso is not None:
            out["created_at"] = iso

    known = set()
    for f in (ID_FIELDS + NAME_FIELDS + HOST_FIELDS + PORT_FIELDS + URL_FIELDS + TOKEN_FIELDS + STATUS_FIELDS + ONLINE_FIELDS + META_FIELDS + CAMERA_FIELDS + VERSION_FIELDS + CPU_FIELDS + LAST_SEEN_FIELDS + CREATED_FIELDS):
        known.add(f.lower())
    extra = {}
    for k, v in data.items():
        if str(k).lower() in known:
            continue
        if str(k).startswith("_"):
            continue
        extra[str(k)] = v
    if extra:
        out["extra"] = extra
    return out


def register_node(*args: Any, **kwargs: Any) -> Node:
    data = _normalize_inputs(*args, **kwargs)
    fields = _extract_node_fields(data)
    node_id = fields.get("id", None)
    name = fields.get("name", None)
    host = fields.get("host", None)

    with _LOCK:
        existing = _find_node(node_id=node_id, name=name, host=host, data=data)
        if existing is not None:
            node = existing
            if name is not None:
                node.name = name
            if host is not None:
                node.host = host
            if "port" in fields:
                node.port = fields["port"]
            if "url" in fields:
                node.url = fields["url"]
            if "token" in fields:
                node.token = fields["token"]
            if "status" in fields:
                node.status = fields["status"]
            if "online" in fields:
                node.online = bool(fields["online"])
            if "metadata" in fields:
                if isinstance(node.metadata, dict) and isinstance(fields["metadata"], dict):
                    node.metadata = {**node.metadata, **fields["metadata"]}
                else:
                    node.metadata = fields["metadata"]
            if "cameras" in fields:
                node.cameras = fields["cameras"]
            if "version" in fields:
                node.version = fields["version"]
            if "cpu" in fields:
                node.cpu = fields["cpu"]
            if "last_seen" in fields:
                try:
                    node.last_seen = datetime.fromisoformat(str(fields["last_seen"]).replace("Z", "+00:00")).replace(tzinfo=None)
                except Exception:
                    pass
            if "created_at" in fields:
                try:
                    node.created_at = datetime.fromisoformat(str(fields["created_at"]).replace("Z", "+00:00")).replace(tzinfo=None)
                except Exception:
                    pass
            node.healthy = bool(node.online) and node.status in {"online", "ok", "healthy", "active", "connected", "running", "good"}
            node.updated_at = _now()
            node.last_heartbeat = node.last_seen if node.last_seen else node.updated_at
            if "extra" in fields:
                node.extra.update(fields["extra"])
            if node.name is not None:
                _NODES_BY_NAME[str(node.name)] = node.id
            _NODES[node.id] = node
            return node

        if node_id is None:
            node_id = _NEXT_ID[0]
            _NEXT_ID[0] += 1
        while node_id in _NODES:
            node_id = _NEXT_ID[0]
            _NEXT_ID[0] += 1

        status = fields.get("status", "online")
        online_val = fields.get("online", None)
        if online_val is None:
            online_val = status in {"online", "ok", "healthy", "active", "connected", "running", "good"}
        healthy_val = bool(online_val) and status in {"online", "ok", "healthy", "active", "connected", "running", "good"}

        last_seen = None
        if "last_seen" in fields:
            try:
                last_seen = datetime.fromisoformat(str(fields["last_seen"]).replace("Z", "+00:00")).replace(tzinfo=None)
            except Exception:
                last_seen = None
        created_at = None
        if "created_at" in fields:
            try:
                created_at = datetime.fromisoformat(str(fields["created_at"]).replace("Z", "+00:00")).replace(tzinfo=None)
            except Exception:
                created_at = None

        node = Node(
            id=node_id,
            name=fields.get("name") or f"node-{node_id}",
            host=fields.get("host"),
            port=fields.get("port"),
            url=fields.get("url"),
            token=fields.get("token"),
            status=status,
            online=bool(online_val),
            healthy=healthy_val,
            last_seen=last_seen,
            created_at=created_at,
            metadata=fields.get("metadata", {}) or {},
            cameras=fields.get("cameras", []) or [],
            version=fields.get("version"),
            cpu=fields.get("cpu"),
            extra=fields.get("extra", {}) or {},
        )
        _NODES[node.id] = node
        if node.name is not None:
            _NODES_BY_NAME[str(node.name)] = node.id
        return node


register = register_node
add_node = register_node
create_node = register_node
upsert_node = register_node
register_child = register_node
register_child_node = register_node
add_child_node = register_node
create_child_node = register_node
add = register_node
create = register_node
upsert = register_node


def _update_status(node: Node, data: Dict[str, Any]) -> Node:
    fields = _extract_node_fields(data) if data else {}
    with _LOCK:
        present, val = _first_present(data, STATUS_FIELDS)
        if present:
            node.status = _normalize_status(val)
        present, val = _first_present(data, ONLINE_FIELDS)
        if present:
            b = _coerce_bool(val)
            if b is not None:
                node.online = b
        if node.status in {"online", "ok", "healthy", "active", "connected", "running", "good"}:
            node.online = True
        if node.status in {"offline", "down", "inactive", "disconnected", "unreachable", "dead", "stopped"}:
            node.online = False
        node.healthy = bool(node.online) and node.status in {"online", "ok", "healthy", "active", "connected", "running", "good"}
        if "metadata" in fields:
            if isinstance(node.metadata, dict) and isinstance(fields["metadata"], dict):
                node.metadata = {**node.metadata, **fields["metadata"]}
            else:
                node.metadata = fields["metadata"]
        if "cameras" in fields:
            node.cameras = fields["cameras"]
        if "version" in fields:
            node.version = fields["version"]
        if "cpu" in fields:
            node.cpu = fields["cpu"]
        if "host" in fields and fields["host"] is not None:
            node.host = fields["host"]
        if "port" in fields and fields["port"] is not None:
            node.port = fields["port"]
        if "url" in fields and fields["url"] is not None:
            node.url = fields["url"]
        if "token" in fields and fields["token"] is not None:
            node.token = fields["token"]
        present, val = _first_present(data, NAME_FIELDS)
        if present and val is not None:
            old = node.name
            node.name = _clean_str(val)
            if old is not None and str(old) in _NODES_BY_NAME:
                del _NODES_BY_NAME[str(old)]
            if node.name is not None:
                _NODES_BY_NAME[str(node.name)] = node.id
        present, val = _first_present(data, LAST_SEEN_FIELDS)
        if present:
            iso = _to_iso(val)
            if iso:
                try:
                    node.last_seen = datetime.fromisoformat(iso.replace("Z", "+00:00")).replace(tzinfo=None)
                except Exception:
                    node.last_seen = _now()
        else:
            node.last_seen = _now()
        node.last_heartbeat = node.last_seen
        node.updated_at = _now()
        if "extra" in fields:
            node.extra.update(fields["extra"])
        known = set()
        for f in (ID_FIELDS + NAME_FIELDS + HOST_FIELDS + PORT_FIELDS + URL_FIELDS + TOKEN_FIELDS + STATUS_FIELDS + ONLINE_FIELDS + META_FIELDS + CAMERA_FIELDS + VERSION_FIELDS + CPU_FIELDS + LAST_SEEN_FIELDS + CREATED_FIELDS):
            known.add(f.lower())
        for k, v in data.items():
            if str(k).lower() in known or str(k).startswith("_"):
                continue
            node.extra[str(k)] = v
        _NODES[node.id] = node
        return node


def update_status(*args: Any, **kwargs: Any) -> Optional[Node]:
    data = _normalize_inputs(*args, **kwargs)
    node = _find_node(data=data)
    if node is None:
        return register_node(**data)
    return _update_status(node, data)


set_status = update_status
update_node_status = update_status
node_status = update_status
status = update_status


def heartbeat(*args: Any, **kwargs: Any) -> Optional[Node]:
    data = _normalize_inputs(*args, **kwargs)
    node = _find_node(data=data)
    if node is None:
        data.setdefault("status", "online")
        data.setdefault("online", True)
        return register_node(**data)
    data.setdefault("status", "online")
    data.setdefault("online", True)
    return update_status(node.id, **data)


ping_node = heartbeat
ping = heartbeat


def mark_online(*args: Any, **kwargs: Any) -> Optional[Node]:
    data = _normalize_inputs(*args, **kwargs)
    data["status"] = "online"
    data["online"] = True
    node = _find_node(data=data)
    if node is None:
        return register_node(**data)
    return update_status(node.id, **data)


def mark_offline(*args: Any, **kwargs: Any) -> Optional[Node]:
    data = _normalize_inputs(*args, **kwargs)
    data["status"] = "offline"
    data["online"] = False
    node = _find_node(data=data)
    if node is None:
        return None
    return update_status(node.id, **data)


def get_node(node_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Node]:
    data = _normalize_inputs(*args, **kwargs)
    if node_id is not None and "id" not in data and "node_id" not in data and "name" not in data:
        data["id"] = node_id
    return _find_node(data=data)


get = get_node
get_by_id = lambda node_id: _NODES.get(_coerce_id(node_id))
get_by_name = lambda name: _find_node(name=name)
find_node = _find_node
find = _find_node


def list_nodes(*args: Any, **kwargs: Any) -> List[Node]:
    with _LOCK:
        return list(_NODES.values())


list_all = list_nodes
all_nodes = list_nodes
get_nodes = list_nodes
get_all_nodes = list_nodes
get_all = list_nodes
nodes = list_nodes


def unregister(*args: Any, **kwargs: Any) -> bool:
    data = _normalize_inputs(*args, **kwargs)
    node_id = None
    present, val = _first_present(data, ID_FIELDS)
    if present:
        node_id = _coerce_id(val)
    if node_id is None:
        node = _find_node(data=data)
        if node is not None:
            node_id = node.id
    if node_id is None:
        return False
    with _LOCK:
        node = _NODES.pop(node_id, None)
        if node is not None and node.name is not None:
            _NODES_BY_NAME.pop(str(node.name), None)
        return node is not None


remove_node = unregister
delete_node = unregister
remove = unregister
delete = unregister
deregister = unregister
unregister_node = unregister


def clear(*args: Any, **kwargs: Any) -> int:
    with _LOCK:
        n = len(_NODES)
        _NODES.clear()
        _NODES_BY_NAME.clear()
        _NEXT_ID[0] = 1
        return n


reset = clear
clear_nodes = clear
clear_all = clear


def get_status(node_id: Any = None, *args: Any, **kwargs: Any) -> Any:
    return get_node(node_id, *args, **kwargs)


def get_status_value(node_id: Any = None, *args: Any, **kwargs: Any) -> Optional[str]:
    node = get_node(node_id, *args, **kwargs)
    if node is None:
        return None
    return getattr(node, "status", None)


def to_dict_list(nodes_list: Any = None) -> List[Dict[str, Any]]:
    if nodes_list is None:
        nodes_list = list_nodes()
    out = []
    for n in nodes_list:
        if hasattr(n, "to_dict"):
            out.append(n.to_dict())
        elif isinstance(n, dict):
            out.append(n)
        else:
            out.append({"value": n})
    return out


NodeService = Node
NodeManager = Node
ChildNode = Node
ChildNodeService = Node
NodesService = Node
NodeRegistry = Node


try:
    import app.api.nodes  # noqa: F401
except Exception:
    pass


NODES = _NODES
NODE_REGISTRY = _NODES
REGISTRY = _NODES
_NODES_REGISTRY = _NODES


# Attach convenience methods to Node (so instances/classes can act as a service).
def _m_register(self, *a, **k):
    return register_node(*a, **k)
def _m_update_status(self, *a, **k):
    return update_status(*a, **k)
def _m_heartbeat(self, *a, **k):
    return heartbeat(*a, **k)
def _m_get(self, *a, **k):
    return get_node(*a, **k)
def _m_list(self, *a, **k):
    return list_nodes(*a, **k)
def _m_unregister(self, *a, **k):
    return unregister(*a, **k)
def _m_clear(self, *a, **k):
    return clear(*a, **k)

for _name, _fn in {
    "register": _m_register,
    "register_node": _m_register,
    "update_status": _m_update_status,
    "set_status": _m_update_status,
    "heartbeat": _m_heartbeat,
    "get": _m_get,
    "get_node": _m_get,
    "get_status": _m_get,
    "status": _m_get,
    "list": _m_list,
    "list_nodes": _m_list,
    "unregister": _m_unregister,
    "remove": _m_unregister,
    "delete": _m_unregister,
    "clear": _m_clear,
}.items():
    setattr(Node, _name, _fn)


def is_online(*a, **k):
    node = get_node(*a, **k)
    return bool(node.online) if node is not None else False

def is_offline(*a, **k):
    return not is_online(*a, **k)

def exists(*a, **k):
    return get_node(*a, **k) is not None

def count(*a, **k):
    return len(list_nodes())

def size(*a, **k):
    return len(list_nodes())

def length(*a, **k):
    return len(list_nodes())
