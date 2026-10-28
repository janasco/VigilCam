"""Plate allowlist/blocklist watcher for VigilCam.

This module does NOT perform license-plate recognition. Recognition/OCR is
owned by ``app.services.license_plate`` (and its ``plate``/``lpr``/``anpr``
aliases). This watcher only matches already-read plate strings against
operator-maintained watchlists and raises alerts on blocklist hits or
unknown plates.

Concepts
--------
* **allowlist** (alias: whitelist): known-good plates. No alert when matched.
* **blocklist** (alias: denylist/blacklist): plates of interest. Always alert.
* **unknown**: plate in neither list. Alerts when ``alert_on_unknown`` is on
  (default True).
"""

from __future__ import annotations

import re
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

_ALLOW: set = set()
_BLOCK: set = set()
_READS: List[Dict[str, Any]] = []
_ALERTS: List[Dict[str, Any]] = []
_LOCK = threading.RLock()

_CONFIG: Dict[str, Any] = {
    "alert_on_unknown": True,
    "alert_on_blocklist": True,
    "enabled": True,
}

_MAX_HISTORY = 1000


def _now() -> datetime:
    return datetime.utcnow()


def normalize_plate(value: Any) -> str:
    """Normalize a plate string for comparison: upper, no spaces/dashes."""
    if value is None:
        return ""
    s = str(value).strip().upper()
    s = re.sub(r"[\s\-_.]+", "", s)
    return s


def _coerce_plate(data: Dict[str, Any]) -> str:
    for key in ("plate", "plate_number", "plate_text", "text", "number",
                "license_plate", "licensePlate", "plateNumber", "value", "query"):
        if data.get(key) is not None and str(data.get(key)).strip():
            return normalize_plate(data.get(key))
    return ""


def _normalize_inputs(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for a in args:
        if a is None:
            continue
        if isinstance(a, dict):
            data.update(a)
        elif isinstance(a, str):
            if _coerce_plate(data) == "":
                data.setdefault("plate", a)
            elif data.get("camera_id") is None:
                data.setdefault("camera_id", a)
            else:
                data.setdefault("note", a)
        else:
            try:
                data.update(dict(a))
            except Exception:  # noqa: BLE001
                pass
    data.update(kwargs or {})
    for nested in ("payload", "options", "params", "data", "body", "request",
                   "plate_read", "read", "result", "recognition"):
        n = data.get(nested)
        if isinstance(n, dict):
            for k, v in n.items():
                data.setdefault(k, v)
    return data


def _plates_from(data: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    single = _coerce_plate(data)
    if single:
        out.append(single)
    for key in ("plates", "allowlist", "allow_list", "whitelist", "white_list",
                "blocklist", "block_list", "denylist", "deny_list", "blacklist",
                "items", "values", "list"):
        v = data.get(key)
        if v is None:
            continue
        if isinstance(v, str):
            parts = re.split(r"[,\n;]+", v)
            out.extend(normalize_plate(p) for p in parts if normalize_plate(p))
        elif isinstance(v, (list, tuple, set)):
            out.extend(normalize_plate(p) for p in v if str(p).strip())
    seen: set = set()
    dedup: List[str] = []
    for p in out:
        if p and p not in seen:
            seen.add(p)
            dedup.append(p)
    return dedup


# ---------------------------------------------------------------------------
# Watchlist management
# ---------------------------------------------------------------------------

def add_allowlist(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _normalize_inputs(*args, **kwargs)
    plates = _plates_from(data)
    with _LOCK:
        for p in plates:
            _ALLOW.add(p)
            _BLOCK.discard(p)  # a plate cannot be on both lists; allow wins on add
        return {"ok": True, "allowlist": sorted(_ALLOW), "added": plates}


def remove_allowlist(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _normalize_inputs(*args, **kwargs)
    plates = _plates_from(data)
    with _LOCK:
        for p in plates:
            _ALLOW.discard(p)
        return {"ok": True, "allowlist": sorted(_ALLOW), "removed": plates}


def add_blocklist(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _normalize_inputs(*args, **kwargs)
    plates = _plates_from(data)
    with _LOCK:
        for p in plates:
            _BLOCK.add(p)
            _ALLOW.discard(p)  # block wins on add
        return {"ok": True, "blocklist": sorted(_BLOCK), "added": plates}


def remove_blocklist(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _normalize_inputs(*args, **kwargs)
    plates = _plates_from(data)
    with _LOCK:
        for p in plates:
            _BLOCK.discard(p)
        return {"ok": True, "blocklist": sorted(_BLOCK), "removed": plates}


def list_allowlist(*args: Any, **kwargs: Any) -> List[str]:
    with _LOCK:
        return sorted(_ALLOW)


def list_blocklist(*args: Any, **kwargs: Any) -> List[str]:
    with _LOCK:
        return sorted(_BLOCK)


def get_watchlists(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    with _LOCK:
        return {"ok": True, "allowlist": sorted(_ALLOW), "blocklist": sorted(_BLOCK),
                "config": dict(_CONFIG)}


def get_config(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    with _LOCK:
        cfg = dict(_CONFIG)
    cfg.setdefault("ok", True)
    return cfg


def configure(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _normalize_inputs(*args, **kwargs)
    with _LOCK:
        for key in ("alert_on_unknown", "alert_unknown", "unknown_alert"):
            if key in data and data[key] is not None:
                _CONFIG["alert_on_unknown"] = bool(data[key])
        for key in ("alert_on_blocklist", "alert_blocklist", "block_alert"):
            if key in data and data[key] is not None:
                _CONFIG["alert_on_blocklist"] = bool(data[key])
        if "enabled" in data and data["enabled"] is not None:
            _CONFIG["enabled"] = bool(data["enabled"])
        return dict(_CONFIG)


def enable(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    with _LOCK:
        _CONFIG["enabled"] = True
        return dict(_CONFIG)


def disable(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    with _LOCK:
        _CONFIG["enabled"] = False
        return dict(_CONFIG)


def is_allowed(plate: Any) -> bool:
    return normalize_plate(plate) in _ALLOW


def is_blocked(plate: Any) -> bool:
    return normalize_plate(plate) in _BLOCK


def is_known(plate: Any) -> bool:
    p = normalize_plate(plate)
    return p in _ALLOW or p in _BLOCK


# ---------------------------------------------------------------------------
# Matching / alerting
# ---------------------------------------------------------------------------

def _publish_event(kind: str, payload: Dict[str, Any]) -> bool:
    try:
        from app.services.event_bus import event_bus as _bus
        if _bus is None:
            return False
        import asyncio
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        if loop is not None and loop.is_running():
            loop.create_task(_bus.publish(kind, payload))
            return True
        return False
    except Exception:  # noqa: BLE001
        return False


def check_plate(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Match one incoming plate read against the watchlists.

    Returns a dict with ``plate``, ``allowed``, ``blocked``, ``unknown``,
    ``alert`` and ``reason``. Matching reads are recorded in history; reads
    that raise an alert are also appended to the alert log.
    """
    data = _normalize_inputs(*args, **kwargs)
    raw = data.get("plate") if data.get("plate") is not None else data.get("text")
    plate = normalize_plate(raw if raw is not None else _coerce_plate(data))
    if not plate:
        return {"ok": False, "success": False, "alert": False,
                "error": "plate is required", "detail": "plate is required"}
    camera_id = data.get("camera_id", data.get("camera", data.get("cam_id")))
    confidence = data.get("confidence", data.get("score"))
    now = _now()

    with _LOCK:
        enabled = bool(_CONFIG.get("enabled", True))
        alert_on_unknown = bool(_CONFIG.get("alert_on_unknown", True))
        alert_on_blocklist = bool(_CONFIG.get("alert_on_blocklist", True))
        blocked = plate in _BLOCK
        allowed = plate in _ALLOW and not blocked
        unknown = not blocked and not allowed

    alert = False
    reason = "allowed" if allowed else ("blocklist" if blocked else "unknown")
    if enabled:
        if blocked and alert_on_blocklist:
            alert = True
        elif unknown and alert_on_unknown:
            alert = True

    record = {
        "plate": plate,
        "allowed": allowed,
        "blocked": blocked,
        "known": not unknown,
        "unknown": unknown,
        "alert": alert,
        "reason": reason,
        "camera_id": camera_id,
        "confidence": confidence,
        "at": now.isoformat(),
        "timestamp": now.isoformat(),
    }
    with _LOCK:
        _READS.append(dict(record))
        if len(_READS) > _MAX_HISTORY:
            del _READS[: len(_READS) - _MAX_HISTORY]
        if alert:
            _ALERTS.append(dict(record))
            if len(_ALERTS) > _MAX_HISTORY:
                del _ALERTS[: len(_ALERTS) - _MAX_HISTORY]
    if alert:
        _publish_event("plate_watch_alert", {"plate": plate, "reason": reason,
                                             "camera_id": camera_id})
    out = dict(record)
    out.update({"ok": True, "success": True})
    return out


def list_reads(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    with _LOCK:
        items = list(_READS)
    data = _normalize_inputs(*args, **kwargs)
    plate = _coerce_plate(data)
    if plate:
        items = [r for r in items if r.get("plate") == plate]
    limit = data.get("limit", data.get("n"))
    try:
        if limit is not None:
            items = items[-max(0, int(limit)):] if int(limit) >= 0 else items
    except (TypeError, ValueError):
        pass
    return [dict(r) for r in items]


def list_alerts(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    with _LOCK:
        items = list(_ALERTS)
    data = _normalize_inputs(*args, **kwargs)
    plate = _coerce_plate(data)
    if plate:
        items = [a for a in items if a.get("plate") == plate]
    reason = data.get("reason")
    if reason:
        items = [a for a in items if a.get("reason") == str(reason)]
    return [dict(a) for a in items]


def get_summary(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    with _LOCK:
        return {
            "ok": True, "success": True,
            "allowlist_count": len(_ALLOW),
            "blocklist_count": len(_BLOCK),
            "reads": len(_READS),
            "alerts": len(_ALERTS),
            "alert_on_unknown": bool(_CONFIG.get("alert_on_unknown", True)),
            "alert_on_blocklist": bool(_CONFIG.get("alert_on_blocklist", True)),
            "enabled": bool(_CONFIG.get("enabled", True)),
        }


def clear(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _normalize_inputs(*args, **kwargs)
    with _LOCK:
        if data.get("watchlists") is False or data.get("keep_lists"):
            _READS.clear()
            _ALERTS.clear()
        else:
            _ALLOW.clear()
            _BLOCK.clear()
            _READS.clear()
            _ALERTS.clear()
        return {"ok": True, "success": True, "cleared": True}


def reset(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    return clear(*args, **kwargs)


# Aliases for flexible callers / alternate naming (watchlist vs whitelist).
add_allow = add_allowlist
allow = add_allowlist
whitelist_add = add_allowlist
add_whitelist = add_allowlist
allow_add = add_allowlist
remove_allow = remove_allowlist
whitelist_remove = remove_allowlist
remove_whitelist = remove_allowlist

add_block = add_blocklist
block = add_blocklist
denylist_add = add_blocklist
add_denylist = add_blocklist
blacklist_add = add_blocklist
add_blacklist = add_blocklist
remove_block = remove_blocklist
denylist_remove = remove_blocklist
remove_denylist = remove_blocklist
blacklist_remove = remove_blocklist
remove_blacklist = remove_blocklist

get_allowlist = list_allowlist
get_blocklist = list_blocklist
get_allowed = list_allowlist
get_blocked = list_blocklist
lists = get_watchlists
watchlists = get_watchlists
list_watchlists = get_watchlists

match_plate = check_plate
match = check_plate
check = check_plate
process_read = check_plate
handle_read = check_plate
watch = check_plate
evaluate = check_plate
scan = check_plate

get_reads = list_reads
get_alerts = list_alerts
history = list_reads
alerts = list_alerts
reads = list_reads
summary = get_summary
stats = get_summary
status = get_summary
health = get_summary
config = get_config
set_config = configure
update_config = configure

__all__ = [
    "normalize_plate",
    "add_allowlist", "remove_allowlist", "add_blocklist", "remove_blocklist",
    "list_allowlist", "list_blocklist", "get_watchlists",
    "get_config", "configure", "enable", "disable",
    "is_allowed", "is_blocked", "is_known",
    "check_plate", "list_reads", "list_alerts", "get_summary",
    "clear", "reset",
]
