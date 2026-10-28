"""Per-group notification digest service for VigilCam.

Complements ``app.services.notify_digest`` which builds **global** digests
only (no per-group filtering -- see ``_matches``/``build_digest`` there:
they filter on type/severity/camera/channel/time but never on group).

This module adds the missing per-group layer on top of the same
``notify_digest`` notification store:

- notifications may carry an explicit group (``group`` / ``group_id`` /
  ``group_name`` / ``group_slug`` / ``camera_group``); the fields are
  preserved verbatim by ``notify_digest._build_notification`` passthrough.
- ``build_digest(group=...)`` returns a digest scoped to one group.
- ``build_all_digests()`` returns one digest per distinct group.
- optional ``camera_group_map`` (``{camera_id: group}``) assigns groups
  dynamically from camera membership without requiring the field on the
  stored notification.
- optional async DB helpers resolve camera -> group via the
  ``Camera.group`` / ``Camera.group_id`` columns and the ``Group`` table.

All entry points accept tolerant ``*args/**kwargs`` like ``notify_digest``.
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.services import notify_digest as _nd

# ---------------------------------------------------------------------------
# In-memory state (built digests only; notifications live in notify_digest)
# ---------------------------------------------------------------------------

GROUP_DIGESTS: Dict[str, Dict[str, Any]] = {}
GROUP_DIGEST_DIGESTS = GROUP_DIGESTS
DIGESTS = GROUP_DIGESTS

_lock = threading.Lock()

GROUP_KEYS = (
    "group",
    "group_id",
    "group_name",
    "group_slug",
    "camera_group",
    "groupName",
    "groupId",
    "groupname",
)
GROUP_ID_KEYS = ("group_id", "groupId", "group-id")
GROUP_NAME_KEYS = ("group", "group_name", "group_slug", "camera_group", "groupName", "groupname")

UNKNOWN_GROUP = "unknown"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_ts() -> float:
    return time.time()


def _to_datetime(value: Any) -> Optional[datetime]:
    try:
        return _nd._to_datetime(value)  # noqa: SLF001
    except Exception:  # noqa: BLE001
        return None


def _first(data: Dict[str, Any], keys: tuple) -> Any:
    for k in keys:
        if k in data and data.get(k) is not None:
            return data.get(k)
    lower = {str(k).lower(): v for k, v in data.items()}
    for k in keys:
        if k.lower() in lower and lower[k.lower()] is not None:
            return lower[k.lower()]
    return None


def _normalize_args(args: tuple, kwargs: Dict[str, Any]) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for a in args or ():
        if isinstance(a, dict):
            data.update(a)
        elif isinstance(a, str):
            s = a.strip()
            if not s:
                continue
            # Single bare string is most likely a group name.
            if data.get("group") is None and data.get("group_id") is None and data.get("group_name") is None:
                data["group"] = s
            elif data.get("message") is None:
                data["message"] = s
            else:
                data.setdefault("text", s)
        else:
            try:
                attrs = {
                    k: getattr(a, k)
                    for k in dir(a)
                    if not k.startswith("_") and not callable(getattr(a, k, None))
                }
                if attrs:
                    data.update(attrs)
            except Exception:  # noqa: BLE001
                pass
    data.update(kwargs or {})
    for nested_key in ("payload", "options", "config", "params", "request", "data", "notification", "event"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _extract_group_filter(data: Dict[str, Any]) -> Optional[str]:
    """Return the single-group filter string, or None for no filtering."""
    for key in ("group_id", "groupId", "group", "group_name", "groupName", "group_slug", "camera_group"):
        if data.get(key) is not None and str(data.get(key)).strip():
            return str(data.get(key)).strip()
    # Plural aliases: take first entry when a single group is needed.
    for key in ("groups", "group_ids", "group_names"):
        val = data.get(key)
        if isinstance(val, (list, tuple)) and val:
            first = val[0]
            if isinstance(first, dict):
                for k in ("group", "group_id", "group_name", "name", "id", "slug"):
                    if first.get(k) is not None:
                        return str(first.get(k)).strip()
            elif str(first).strip():
                return str(first).strip()
        elif isinstance(val, str) and val.strip():
            parts = [p.strip() for p in val.split(",") if p.strip()]
            if parts:
                return parts[0]
    return None


def _extract_group_list(data: Dict[str, Any]) -> Optional[List[str]]:
    """Return an explicit multi-group filter, or None."""
    for key in ("groups", "group_ids", "group_names"):
        val = data.get(key)
        if val is None:
            continue
        if isinstance(val, (list, tuple, set)):
            out: List[str] = []
            for item in val:
                if item is None:
                    continue
                if isinstance(item, dict):
                    for k in ("group", "group_id", "group_name", "name", "id", "slug"):
                        if item.get(k) is not None and str(item.get(k)).strip():
                            out.append(str(item.get(k)).strip())
                            break
                elif str(item).strip():
                    out.append(str(item).strip())
            if out:
                return out
        elif isinstance(val, str) and val.strip():
            parts = [p.strip() for p in val.replace(";", ",").split(",") if p.strip()]
            if parts:
                return parts
    single = _extract_group_filter(data)
    return [single] if single is not None else None


def _notification_groups(notif: Dict[str, Any], camera_group_map: Optional[Dict[str, Any]] = None) -> List[str]:
    """Return all group labels attached to a notification."""
    groups: List[str] = []
    for key in GROUP_KEYS:
        if notif.get(key) is not None and str(notif.get(key)).strip():
            groups.append(str(notif.get(key)).strip())
    # Case-insensitive fallback for odd-cased keys.
    lower = {str(k).lower(): v for k, v in notif.items()}
    for key in GROUP_KEYS:
        if key.lower() in lower and lower[key.lower()] is not None and str(lower[key.lower()]).strip():
            val = str(lower[key.lower()]).strip()
            if val not in groups:
                groups.append(val)
    # Dynamic mapping via camera membership.
    if not groups and camera_group_map:
        for cam_key in ("camera_id", "camera", "cam", "camera_name", "source", "device_id"):
            cam_val = notif.get(cam_key)
            if cam_val is None:
                continue
            for map_k, map_v in camera_group_map.items():
                if str(map_k) == str(cam_val) and map_v is not None and str(map_v).strip():
                    groups.append(str(map_v).strip())
    if not groups:
        groups.append(UNKNOWN_GROUP)
    return groups


def _matches_group(notif: Dict[str, Any], data: Dict[str, Any], camera_group_map: Optional[Dict[str, Any]] = None) -> bool:
    wanted = _extract_group_list(data)
    if not wanted:
        # Also honour a singular filter embedded in plural-less payloads.
        single = _extract_group_filter(data)
        if single is None:
            return True
        wanted = [single]
    # Match when ANY wanted group equals ANY attached group (case-insensitive).
    attached = {g.lower() for g in _notification_groups(notif, camera_group_map)}
    # "all"/"*"/"" means no filtering.
    lowered_wanted = [w.lower() for w in wanted]
    if any(w in ("all", "*", "") for w in lowered_wanted):
        return True
    return any(w.lower() in attached for w in wanted)


def _snapshot() -> List[Dict[str, Any]]:
    try:
        return [dict(n) for n in _nd.list_notifications()]
    except Exception:  # noqa: BLE001
        with _nd._lock:  # noqa: SLF001
            return [dict(n) for n in _nd.NOTIFICATIONS]


def _filtered_items(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    camera_group_map = data.get("camera_group_map") or data.get("camera_groups") or data.get("group_map")
    if isinstance(camera_group_map, dict):
        pass
    else:
        camera_group_map = None
    snapshot = _snapshot()
    out: List[Dict[str, Any]] = []
    for n in snapshot:
        try:
            if not _nd._matches(n, data):  # noqa: SLF001
                continue
        except Exception:  # noqa: BLE001
            pass
        if not _matches_group(n, data, camera_group_map):
            continue
        out.append(n)
    return out


def _aggregate(items: List[Dict[str, Any]], group_label: Optional[str], data: Dict[str, Any]) -> Dict[str, Any]:
    try:
        limit = int(data.get("limit", 50))
    except (TypeError, ValueError):
        limit = 50
    if limit is not None and limit >= 0:
        recent = items[-limit:] if limit else []
    else:
        recent = list(items)

    by_type: Dict[str, int] = {}
    by_severity: Dict[str, int] = {}
    by_camera: Dict[str, int] = {}
    by_channel: Dict[str, int] = {}
    by_group: Dict[str, int] = {}
    for n in items:
        t = str(n.get("type", "info"))
        by_type[t] = by_type.get(t, 0) + 1
        s = str(n.get("severity", "info"))
        by_severity[s] = by_severity.get(s, 0) + 1
        c = n.get("camera_id") or n.get("camera")
        ckey = str(c) if c is not None else "unknown"
        by_camera[ckey] = by_camera.get(ckey, 0) + 1
        ch = str(n.get("channel", "general"))
        by_channel[ch] = by_channel.get(ch, 0) + 1
        for g in _notification_groups(n, data.get("camera_group_map") if isinstance(data.get("camera_group_map"), dict) else None):
            by_group[str(g)] = by_group.get(str(g), 0) + 1

    unread = sum(1 for n in items if not n.get("read", False))
    timestamps = [n.get("timestamp") for n in items if n.get("timestamp")]
    digest_id = str(uuid.uuid4())
    now_iso = _now_iso()
    label = group_label if group_label is not None else (_extract_group_filter(data) or "all")
    digest: Dict[str, Any] = {
        "id": digest_id,
        "digest_id": digest_id,
        "summary_id": digest_id,
        "group_digest_id": digest_id,
        "ok": True,
        "success": True,
        "status": "completed",
        "group": label,
        "group_id": label,
        "group_name": label,
        "total": len(items),
        "count": len(items),
        "num_notifications": len(items),
        "processed": len(items),
        "processed_count": len(items),
        "unread": unread,
        "unread_count": unread,
        "by_type": by_type,
        "counts_by_type": dict(by_type),
        "by_severity": by_severity,
        "counts_by_severity": dict(by_severity),
        "by_camera": by_camera,
        "counts_by_camera": dict(by_camera),
        "by_channel": by_channel,
        "counts_by_channel": dict(by_channel),
        "by_group": by_group,
        "counts_by_group": dict(by_group),
        "items": recent,
        "notifications": recent,
        "results": recent,
        "latest": dict(items[-1]) if items else None,
        "oldest": dict(items[0]) if items else None,
        "first": dict(items[0]) if items else None,
        "last": dict(items[-1]) if items else None,
        "time_range": {
            "start": min(timestamps) if timestamps else None,
            "end": max(timestamps) if timestamps else None,
        },
        "generated_at": now_iso,
        "created_at": now_iso,
    }
    with _lock:
        GROUP_DIGESTS[digest_id] = digest
    return dict(digest)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def record_notification(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Record a notification (delegates to notify_digest store)."""
    data = _normalize_args(args, kwargs)
    return _nd.record_notification(**data)


def get_notification(*args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    data = _normalize_args(args, kwargs)
    group_filter = _extract_group_filter(data)
    res = _nd.get_notification(**{k: v for k, v in data.items() if k not in ("group", "group_id", "group_name")})
    if res is None:
        # Fall back to positional lookup.
        return _nd.get_notification(*args, **kwargs)
    if group_filter is not None and group_filter.lower() not in ("all", "*"):
        attached = {g.lower() for g in _notification_groups(res)}
        if group_filter.lower() not in attached:
            return None
    return res


def list_notifications(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    data = _normalize_args(args, kwargs)
    if _extract_group_filter(data) is None and _extract_group_list(data) is None:
        return _nd.list_notifications()
    return _filtered_items(data)


def list_groups(*args: Any, **kwargs: Any) -> List[str]:
    """Return sorted distinct group labels present in stored notifications."""
    data = _normalize_args(args, kwargs)
    camera_group_map = data.get("camera_group_map")
    if not isinstance(camera_group_map, dict):
        camera_group_map = None
    seen: Dict[str, str] = {}
    for n in _snapshot():
        for g in _notification_groups(n, camera_group_map):
            seen[g.lower()] = g
    # When nothing recorded yet, return empty (not ["unknown"]).
    if len(seen) == 1 and UNKNOWN_GROUP in seen:
        snapshot = _snapshot()
        if not snapshot:
            return []
    return sorted(seen.values(), key=lambda s: s.lower())


def get_group_counts(*args: Any, **kwargs: Any) -> Dict[str, int]:
    data = _normalize_args(args, kwargs)
    camera_group_map = data.get("camera_group_map")
    if not isinstance(camera_group_map, dict):
        camera_group_map = None
    counts: Dict[str, int] = {}
    for n in _snapshot():
        for g in _notification_groups(n, camera_group_map):
            counts[g] = counts.get(g, 0) + 1
    return counts


def build_digest(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Build a digest scoped to a single group (or global when no group given)."""
    data = _normalize_args(args, kwargs)
    items = _filtered_items(data)
    label = _extract_group_filter(data) or "all"
    return _aggregate(items, label, data)


def build_group_digest(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Alias requiring a group; falls back to ``build_digest``."""
    return build_digest(*args, **kwargs)


def build_all_digests(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Build one digest per distinct group plus a global summary."""
    data = _normalize_args(args, kwargs)
    camera_group_map = data.get("camera_group_map")
    if not isinstance(camera_group_map, dict):
        camera_group_map = None
    # Distinct groups across the whole store (ignore any group filter for discovery).
    groups: List[str] = []
    seen: Dict[str, str] = {}
    for n in _snapshot():
        for g in _notification_groups(n, camera_group_map):
            if g.lower() not in seen:
                seen[g.lower()] = g
                groups.append(g)
    groups.sort(key=lambda s: s.lower())
    if not groups:
        summary: Dict[str, Any] = {
            "ok": True,
            "success": True,
            "status": "completed",
            "total": 0,
            "count": 0,
            "total_groups": 0,
            "group_count": 0,
            "groups": [],
            "group_names": [],
            "digests": {},
            "by_group": {},
            "counts_by_group": {},
            "items": [],
            "results": [],
            "generated_at": _now_iso(),
        }
        return summary
    digests: Dict[str, Dict[str, Any]] = {}
    for g in groups:
        gdata = dict(data)
        gdata["group"] = g
        gdata.pop("groups", None)
        gdata.pop("group_ids", None)
        gdata.pop("group_names", None)
        items = _filtered_items(gdata)
        digests[g] = _aggregate(items, g, gdata)
    total = sum(d.get("total", 0) for d in digests.values())
    by_group = {g: d.get("total", 0) for g, d in digests.items()}
    return {
        "ok": True,
        "success": True,
        "status": "completed",
        "total": total,
        "count": len(digests),
        "num_groups": len(digests),
        "total_groups": len(digests),
        "group_count": len(digests),
        "groups": sorted(digests.keys(), key=lambda s: s.lower()),
        "group_names": sorted(digests.keys(), key=lambda s: s.lower()),
        "digests": {k: dict(v) for k, v in digests.items()},
        "by_group": dict(by_group),
        "counts_by_group": dict(by_group),
        "group_digests": {k: dict(v) for k, v in digests.items()},
        "items": list(digests.values()),
        "results": list(digests.values()),
        "generated_at": _now_iso(),
        "created_at": _now_iso(),
    }


def clear(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _normalize_args(args, kwargs)
    with _lock:
        d = len(GROUP_DIGESTS)
        GROUP_DIGESTS.clear()
    preserve = bool(data.get("preserve_notifications", False) or data.get("preserve", False))
    removed = 0
    if not preserve:
        try:
            res = _nd.clear()
            removed = int(res.get("removed", 0)) if isinstance(res, dict) else 0
        except Exception:  # noqa: BLE001
            removed = 0
    return {
        "ok": True,
        "success": True,
        "status": "cleared",
        "cleared": True,
        "count": 0,
        "removed": removed,
        "digests_removed": d,
    }


def status(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    try:
        base = _nd.status()
    except Exception:  # noqa: BLE001
        base = {}
    with _lock:
        digests = len(GROUP_DIGESTS)
    groups = list_groups()
    return {
        "ok": True,
        "success": True,
        "status": "ok",
        "service": "group_digest",
        "total": int(base.get("total", 0)) if isinstance(base, dict) else 0,
        "count": int(base.get("total", 0)) if isinstance(base, dict) else 0,
        "size": int(base.get("total", 0)) if isinstance(base, dict) else 0,
        "pending": int(base.get("total", 0)) if isinstance(base, dict) else 0,
        "unread": int(base.get("unread", 0)) if isinstance(base, dict) else 0,
        "unread_count": int(base.get("unread", 0)) if isinstance(base, dict) else 0,
        "groups": groups,
        "group_names": groups,
        "total_groups": len(groups),
        "group_count": len(groups),
        "digests": digests,
        "digest_count": digests,
    }


def mark_read(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _normalize_args(args, kwargs)
    return _nd.mark_read(**data)


# ---------------------------------------------------------------------------
# Async DB-backed helpers (camera membership -> group)
# ---------------------------------------------------------------------------


async def _camera_group_map_from_db(db) -> Dict[str, str]:
    """Build ``{camera_id: group}`` plus ``{camera_name: group}`` from the DB."""
    from sqlalchemy import select as _select

    mapping: Dict[str, str] = {}
    try:
        from app.models.entities import Camera as _Camera
    except Exception:  # noqa: BLE001
        return mapping
    try:
        result = await db.execute(_select(_Camera))
        cameras = list(result.scalars().all())
    except Exception:  # noqa: BLE001
        return mapping
    # Resolve group names for FK references when possible.
    group_names: Dict[Any, str] = {}
    try:
        from app.models.entities import Group as _Group

        gres = await db.execute(_select(_Group))
        for g in gres.scalars().all():
            group_names[g.id] = g.name
            group_names[str(g.id)] = g.name
    except Exception:  # noqa: BLE001
        pass
    for cam in cameras:
        label = getattr(cam, "group", None) or group_names.get(getattr(cam, "group_id", None))
        if label is None:
            continue
        label = str(label).strip()
        if not label:
            continue
        cid = getattr(cam, "id", None)
        if cid is not None:
            mapping[str(cid)] = label
        name = getattr(cam, "name", None)
        if name is not None and str(name).strip():
            mapping[str(name).strip()] = label
    return mapping


async def build_digest_for_group(db=None, group: Any = None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Build a digest for one DB group (matches by group name or member cameras)."""
    data = _normalize_args(args, kwargs)
    if group is not None:
        if isinstance(group, dict):
            for k in ("group", "group_id", "group_name", "name", "id", "slug"):
                if group.get(k) is not None:
                    data.setdefault("group", str(group.get(k)))
                    break
        elif str(group).strip():
            data.setdefault("group", str(group).strip())
    # Enrich with DB membership when a db session is available.
    if db is not None:
        try:
            mapping = await _camera_group_map_from_db(db)
            if mapping and "camera_group_map" not in data:
                data["camera_group_map"] = mapping
        except Exception:  # noqa: BLE001
            pass
    return build_digest(**data)


async def build_all_for_db(db=None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Build per-group digests with DB camera membership applied."""
    data = _normalize_args(args, kwargs)
    if db is not None:
        try:
            mapping = await _camera_group_map_from_db(db)
            if mapping and "camera_group_map" not in data:
                data["camera_group_map"] = mapping
        except Exception:  # noqa: BLE001
            pass
    return build_all_digests(**data)


# ---------------------------------------------------------------------------
# Aliases (tolerant naming for API/test compatibility)
# ---------------------------------------------------------------------------

notify = record_notification
send = record_notification
push = record_notification
add = record_notification
add_notification = record_notification
create_notification = record_notification
record = record_notification
log_notification = record_notification
log = record_notification
push_notification = record_notification
send_notification = record_notification
create = record_notification
save = record_notification
store = record_notification
post_notification = record_notification

build_summary = build_digest
get_summary = build_digest
get_digest = build_digest
summarize = build_digest
summary = build_digest
digest = build_digest
generate_digest = build_digest
generate_summary = build_digest
create_digest = build_digest
create_summary = build_digest
summarize_notifications = build_digest
build_notify_digest = build_digest
get_notify_digest = build_digest
notify_digest = build_digest
digest_summary = build_digest
group_summary = build_digest
get_group_digest = build_digest
get_group_summary = build_digest

build_all = build_all_digests
build_digests = build_all_digests
build_digests_by_group = build_all_digests
build_per_group_digest = build_all_digests
build_per_group_digests = build_all_digests
build_group_digests = build_all_digests
generate_all = build_all_digests
generate_group_digests = build_all_digests
summarize_all = build_all_digests
summarize_groups = build_all_digests
summary_all = build_all_digests

get = get_notification
fetch = get_notification
find = get_notification
get_one = get_notification

list_all = list_notifications
get_notifications = list_notifications
get_all = list_notifications
all_notifications = list_notifications
items = list_notifications
values = list_notifications
all = list_notifications

list_all_groups = list_groups
get_groups = list_groups
get_all_groups = list_groups
groups = list_groups

reset = clear
purge = clear
clear_notifications = clear
clear_digests = clear

get_status = status

mark_as_read = mark_read


class GroupDigestService:
    """Object-oriented facade over the module functions."""

    def record(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return record_notification(*a, **k)

    def notify(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return record_notification(*a, **k)

    def add_notification(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return record_notification(*a, **k)

    def send(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return record_notification(*a, **k)

    def push(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return record_notification(*a, **k)

    def build_digest(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return build_digest(*a, **k)

    def build_group_digest(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return build_group_digest(*a, **k)

    def build_summary(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return build_digest(*a, **k)

    def get_summary(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return build_digest(*a, **k)

    def summarize(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return build_digest(*a, **k)

    def summary(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return build_digest(*a, **k)

    def build_all(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return build_all_digests(*a, **k)

    def build_all_digests(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return build_all_digests(*a, **k)

    def list_notifications(self, *a: Any, **k: Any) -> List[Dict[str, Any]]:
        return list_notifications(*a, **k)

    def list(self, *a: Any, **k: Any) -> List[Dict[str, Any]]:
        return list_notifications(*a, **k)

    def list_groups(self, *a: Any, **k: Any) -> List[str]:
        return list_groups(*a, **k)

    def get(self, *a: Any, **k: Any) -> Optional[Dict[str, Any]]:
        return get_notification(*a, **k)

    def clear(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return clear(*a, **k)

    def status(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return status(*a, **k)

    def mark_read(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return mark_read(*a, **k)


GroupDigest = GroupDigestService
DigestService = GroupDigestService
GroupNotificationDigestService = GroupDigestService
SummaryService = GroupDigestService

group_digest_service = GroupDigestService()
service = group_digest_service
digest_service = group_digest_service
summary_service = group_digest_service


__all__ = [
    "record_notification",
    "notify",
    "build_digest",
    "build_group_digest",
    "build_all_digests",
    "build_summary",
    "get_summary",
    "summarize",
    "summary",
    "list_notifications",
    "list_groups",
    "get_group_counts",
    "get_notification",
    "get",
    "clear",
    "status",
    "mark_read",
    "build_digest_for_group",
    "build_all_for_db",
    "GroupDigestService",
    "group_digest_service",
    "service",
    "GROUP_DIGESTS",
]
