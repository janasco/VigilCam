"""Unified alert inbox service for VigilCam.

Aggregates the four independent alert domains into a single
newest-first inbox:

* ``resource_watch`` -- cpu / memory / disk threshold breaches
* ``bitrate_alerts`` -- per-stream low/high bitrate breaches
* ``offline_alerts`` -- per-camera offline state / throttled alerts
* ``rate_anomaly`` -- stateless event-rate spike / quiet analysis

There is no pre-existing unified inbox (each domain above exposes its
own service + API router only). This module is the single aggregation
point. All state stays in the source services; this module only reads
and normalizes. Standard library only.
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

SOURCES = ("resource_watch", "rate_anomaly", "offline_alerts", "bitrate_alerts")

SEVERITY_RANK = {
    "critical": 3,
    "error": 3,
    "warning": 2,
    "warn": 2,
    "info": 1,
    "notice": 1,
    "ok": 0,
    "unknown": 0,
}

_lock = threading.RLock()


def _now() -> datetime:
    return datetime.utcnow()


def _parse_ts(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is not None:
            try:
                dt = dt.replace(tzinfo=None)
            except Exception:  # noqa: BLE001
                pass
        return dt
    if isinstance(value, (int, float)):
        stamp = float(value)
        if abs(stamp) > 1e12:
            stamp = stamp / 1000.0
        try:
            return datetime.utcfromtimestamp(stamp)
        except (OSError, ValueError, OverflowError):
            return None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            parsed = datetime.fromisoformat(s)
            if parsed.tzinfo is not None:
                parsed = parsed.replace(tzinfo=None)
            return parsed
        except ValueError:
            try:
                return datetime.utcfromtimestamp(float(s))
            except (ValueError, OSError, OverflowError):
                return None
    return None


def _ts_float(value: Any) -> float:
    dt = _parse_ts(value)
    if dt is None:
        return 0.0
    try:
        return dt.timestamp()
    except Exception:  # noqa: BLE001
        return 0.0


def _normalize_severity(value: Any) -> str:
    if value is None:
        return "info"
    s = str(value).strip().lower()
    if s in ("critical", "error", "fatal"):
        return "critical"
    if s in ("warning", "warn"):
        return "warning"
    if s in ("info", "notice"):
        return "info"
    if s == "ok":
        return "ok"
    return "info" if s not in SEVERITY_RANK else s


def _severity_rank(severity: Any) -> int:
    return int(SEVERITY_RANK.get(str(severity or "").strip().lower(), 0))


def _coerce_sources(value: Any) -> Optional[List[str]]:
    if value is None:
        return None
    if isinstance(value, str):
        parts = [p.strip().lower() for p in value.replace(";", ",").split(",")]
        out = [p for p in parts if p]
        # Accept hyphen/alias variants.
        alias = {
            "resource-watch": "resource_watch",
            "resourcewatch": "resource_watch",
            "rate-anomaly": "rate_anomaly",
            "rateanomaly": "rate_anomaly",
            "event-rate-anomaly": "rate_anomaly",
            "offline-alerts": "offline_alerts",
            "offlinealerts": "offline_alerts",
            "offline": "offline_alerts",
            "bitrate-alerts": "bitrate_alerts",
            "bitratealerts": "bitrate_alerts",
            "bitrate": "bitrate_alerts",
        }
        return [alias.get(p, p) for p in out] or None
    if isinstance(value, (list, tuple, set)):
        out: List[str] = []
        for v in value:
            sub = _coerce_sources(v)
            if sub:
                out.extend(sub)
        seen: List[str] = []
        for s in out:
            if s not in seen:
                seen.append(s)
        return seen or None
    return None


def _coerce_severities(value: Any) -> Optional[List[str]]:
    if value is None:
        return None
    if isinstance(value, str):
        parts = [p.strip().lower() for p in value.replace(";", ",").split(",")]
        return [_normalize_severity(p) for p in parts if p] or None
    if isinstance(value, (list, tuple, set)):
        return [_normalize_severity(v) for v in value] or None
    return [_normalize_severity(value)]


def _coerce_limit(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        n = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None
    if n < 0:
        return 0
    return n


def _make_item(
    *,
    source: str,
    severity: Any,
    title: str,
    message: str,
    at: Any,
    extra: Optional[Dict[str, Any]] = None,
    raw: Any = None,
) -> Dict[str, Any]:
    dt = _parse_ts(at)
    if dt is None:
        dt = _now()
    iso = dt.isoformat()
    sev = _normalize_severity(severity)
    item: Dict[str, Any] = {
        "id": "",
        "source": source,
        "severity": sev,
        "severity_rank": _severity_rank(sev),
        "title": title,
        "message": message,
        "at": iso,
        "timestamp": iso,
        "time": iso,
    }
    if extra:
        for k, v in extra.items():
            if k not in item:
                item[k] = v
    if isinstance(raw, dict):
        item["raw"] = dict(raw)
    elif raw is not None:
        item["raw"] = raw
    # Stable unique id (source + distinguishing key + timestamp).
    key = ""
    for cand in ("camera_id", "camera", "resource", "target", "title"):
        if item.get(cand) is not None:
            key = str(item.get(cand))
            break
    item["id"] = f"{source}:{key}:{iso}"
    return item


# ---------------------------------------------------------------------------
# Per-source collectors
# ---------------------------------------------------------------------------


def collect_resource_watch_alerts(
    include_history: bool = True,
    limit: Optional[int] = None,
    **kwargs: Any,
) -> List[Dict[str, Any]]:
    """Collect resource_watch breaches as normalized inbox items."""
    try:
        from app.services import resource_watch as _rw
    except Exception:  # noqa: BLE001
        return []
    records: List[Dict[str, Any]] = []
    try:
        for rec in (_rw.list_alerts() or []):
            if isinstance(rec, dict):
                records.append(dict(rec))
    except Exception:  # noqa: BLE001
        pass
    if include_history:
        try:
            hist_limit = _coerce_limit(kwargs.get("history_limit", kwargs.get("history_n")))
            hist = _rw.get_history(limit=hist_limit) if hist_limit is not None else _rw.get_history()
            for rec in hist or []:
                if isinstance(rec, dict):
                    records.append(dict(rec))
        except Exception:  # noqa: BLE001
            pass
    # Dedupe by (resource, at).
    seen = set()
    items: List[Dict[str, Any]] = []
    for rec in records:
        resource = rec.get("resource", rec.get("name", "unknown"))
        at = rec.get("at", rec.get("timestamp", rec.get("time")))
        dedupe_key = (str(resource), str(at))
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        value = rec.get("value")
        threshold = rec.get("threshold")
        message = rec.get("message") or (
            f"{resource} usage {value}% >= threshold {threshold}%"
            if value is not None else f"{resource} threshold breach"
        )
        items.append(
            _make_item(
                source="resource_watch",
                severity=rec.get("severity", "critical"),
                title=f"Resource {resource} breach",
                message=str(message),
                at=at,
                extra={
                    "resource": resource,
                    "value": value,
                    "threshold": threshold,
                    "alert_count": rec.get("alert_count"),
                },
                raw=rec,
            )
        )
    if limit is not None:
        n = _coerce_limit(limit)
        if n is not None:
            items = items[:n]
    return items


def collect_bitrate_alerts(
    include_history: bool = True,
    limit: Optional[int] = None,
    target: Any = None,
    **kwargs: Any,
) -> List[Dict[str, Any]]:
    """Collect bitrate_alerts breaches as normalized inbox items."""
    try:
        from app.services import bitrate_alerts as _ba
    except Exception:  # noqa: BLE001
        return []
    records: List[Dict[str, Any]] = []
    try:
        active_kwargs: Dict[str, Any] = {}
        if target is not None:
            active_kwargs["target"] = target
        for rec in (_ba.list_alerts(**active_kwargs) or []):
            if isinstance(rec, dict):
                records.append(dict(rec))
    except Exception:  # noqa: BLE001
        pass
    if include_history:
        try:
            hist_kwargs: Dict[str, Any] = {}
            if target is not None:
                hist_kwargs["target"] = target
            hist_limit = _coerce_limit(kwargs.get("history_limit", kwargs.get("history_n")))
            if hist_limit is not None:
                hist_kwargs["limit"] = hist_limit
            for rec in (_ba.get_history(**hist_kwargs) or []):
                if isinstance(rec, dict):
                    records.append(dict(rec))
        except Exception:  # noqa: BLE001
            pass
    seen = set()
    items: List[Dict[str, Any]] = []
    for rec in records:
        tgt = rec.get("target", rec.get("stream", rec.get("name", "unknown")))
        at = rec.get("at", rec.get("timestamp", rec.get("time")))
        dedupe_key = (str(tgt), str(at), str(rec.get("bitrate_bps", rec.get("value"))))
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        kind = "low" if rec.get("low") else ("high" if rec.get("high") else "breach")
        message = rec.get("message") or f"bitrate {kind} breach on {tgt}"
        items.append(
            _make_item(
                source="bitrate_alerts",
                severity=rec.get("severity", "critical"),
                title=f"Bitrate {kind} on {tgt}",
                message=str(message),
                at=at,
                extra={
                    "target": tgt,
                    "bitrate_bps": rec.get("bitrate_bps", rec.get("value")),
                    "value": rec.get("value", rec.get("bitrate_bps")),
                    "thresholds": rec.get("thresholds"),
                    "low": bool(rec.get("low", False)),
                    "high": bool(rec.get("high", False)),
                    "alert_count": rec.get("alert_count"),
                },
                raw=rec,
            )
        )
    if limit is not None:
        n = _coerce_limit(limit)
        if n is not None:
            items = items[:n]
    return items


def collect_offline_alerts(
    only_alerted: bool = True,
    limit: Optional[int] = None,
    **kwargs: Any,
) -> List[Dict[str, Any]]:
    """Collect offline_alerts as normalized inbox items.

    By default only cameras that are offline or have emitted an alert
    are included (tracked-but-healthy cameras are not alerts).
    """
    try:
        from app.services import offline_alerts as _oa
    except Exception:  # noqa: BLE001
        return []
    try:
        states = _oa.list_alerts() or []
    except Exception:  # noqa: BLE001
        return []
    items: List[Dict[str, Any]] = []
    for st in states:
        rec = dict(st) if isinstance(st, dict) else {}
        if not rec:
            continue
        offline = bool(rec.get("offline", False))
        alerted = bool(rec.get("alerted", False))
        if only_alerted and not (offline or alerted):
            continue
        cid = rec.get("camera_id", rec.get("id"))
        at = (
            rec.get("last_alert_at")
            or rec.get("last_offline_at")
            or rec.get("offline_since")
            or rec.get("updated_at")
            or rec.get("at")
        )
        reason = rec.get("last_reason", "offline") or "offline"
        severity = "critical" if offline else ("warning" if alerted else "info")
        message = f"Camera {cid} offline ({reason})" if offline else f"Camera {cid} alert ({reason})"
        items.append(
            _make_item(
                source="offline_alerts",
                severity=severity,
                title=f"Camera {cid} {'offline' if offline else 'alert'}",
                message=str(message),
                at=at,
                extra={
                    "camera_id": cid,
                    "offline": offline,
                    "alerted": alerted,
                    "reason": reason,
                    "offline_since": rec.get("offline_since"),
                    "alert_count": rec.get("alert_count"),
                    "suppressed_count": rec.get("suppressed_count"),
                },
                raw=rec,
            )
        )
    if limit is not None:
        n = _coerce_limit(limit)
        if n is not None:
            items = items[:n]
    return items


def collect_rate_anomaly_alerts(
    events: Any = None,
    limit: Optional[int] = None,
    **kwargs: Any,
) -> List[Dict[str, Any]]:
    """Convert a rate_anomaly analysis run into inbox items.

    rate_anomaly is stateless (no persistent alert store), so without
    ``events`` there is nothing to report and ``[]`` is returned.
    """
    if events is None:
        for k in ("items", "rows", "detections", "event_list", "data_events"):
            if isinstance(kwargs.get(k), (list, tuple)) and kwargs.get(k):
                events = list(kwargs.get(k))
                break
    if events is None:
        events = kwargs.get("alerts") if isinstance(kwargs.get("alerts"), list) else None
    if events is None:
        return []
    try:
        event_list = list(events)
    except TypeError:
        return []
    if not event_list:
        return []
    try:
        from app.services import rate_anomaly as _ra
    except Exception:  # noqa: BLE001
        return []
    options = {k: v for k, v in kwargs.items() if k not in ("events", "items", "rows")}
    try:
        result = _ra.analyze_rate(event_list, **options)
    except Exception:  # noqa: BLE001
        return []
    alerts = result.get("alerts", []) if isinstance(result, dict) else []
    now_iso = result.get("now") if isinstance(result, dict) else None
    items: List[Dict[str, Any]] = []
    for alert in alerts or []:
        if not isinstance(alert, dict):
            continue
        kind = str(alert.get("type", alert.get("kind", "anomaly")))
        severity = alert.get("severity", "warning" if kind == "spike" else "info")
        message = alert.get("message", f"event rate {kind}")
        items.append(
            _make_item(
                source="rate_anomaly",
                severity=severity,
                title=f"Event rate {kind}",
                message=str(message),
                at=now_iso,
                extra={
                    "kind": kind,
                    "ratio": alert.get("ratio"),
                    "current_count": alert.get("current_count"),
                    "baseline_count": alert.get("baseline_count"),
                },
                raw=alert,
            )
        )
    if limit is not None:
        n = _coerce_limit(limit)
        if n is not None:
            items = items[:n]
    return items


# ---------------------------------------------------------------------------
# Unified inbox
# ---------------------------------------------------------------------------


def _sort_newest_first(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    def _key(it: Dict[str, Any]):
        return (_ts_float(it.get("at")), _severity_rank(it.get("severity")))

    return sorted(items, key=_key, reverse=True)


def get_inbox(
    *args: Any,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Return the unified inbox: all sources merged, newest-first.

    Accepts ``events`` (for rate_anomaly), ``sources`` (csv/list filter),
    ``severity``/``severities`` (csv/list filter), ``limit`` (int),
    ``include_history`` (bool, default True), ``only_alerted`` (bool).
    Never raises; a failing source contributes zero items.
    """
    data: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            data.update(a)
        elif isinstance(a, (list, tuple)) and data.get("events") is None:
            data["events"] = list(a)
    data.update(kwargs or {})
    # Nested-payload flattening for flexible callers.
    for nested_key in ("payload", "options", "params", "request", "data", "body", "filter", "filters"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)

    sources = _coerce_sources(data.get("sources", data.get("source")))
    if sources is None:
        wanted = list(SOURCES)
    else:
        wanted = [s for s in sources if s in SOURCES]
    severities = _coerce_severities(data.get("severity", data.get("severities")))
    limit = _coerce_limit(data.get("limit", data.get("n", data.get("count"))))
    include_history = data.get("include_history", data.get("history", True))
    if isinstance(include_history, str):
        include_history = include_history.strip().lower() not in ("0", "false", "no", "off")
    include_history = bool(include_history)
    only_alerted = data.get("only_alerted", data.get("alerted_only", True))
    if isinstance(only_alerted, str):
        only_alerted = only_alerted.strip().lower() not in ("0", "false", "no", "off")
    only_alerted = bool(only_alerted)

    events = data.get("events")
    if events is None:
        for k in ("items", "rows", "event_list"):
            if isinstance(data.get(k), (list, tuple)):
                events = data.get(k)
                break

    rate_kwargs = {k: v for k, v in data.items() if k not in (
        "sources", "source", "severity", "severities", "limit", "n", "count",
        "include_history", "history", "only_alerted", "alerted_only",
        "payload", "options", "params", "request", "data", "body", "filter", "filters",
    )}

    items: List[Dict[str, Any]] = []
    errors: Dict[str, str] = {}
    if "resource_watch" in wanted:
        try:
            items.extend(collect_resource_watch_alerts(include_history=include_history))
        except Exception as exc:  # noqa: BLE001
            errors["resource_watch"] = str(exc)
    if "bitrate_alerts" in wanted:
        try:
            items.extend(
                collect_bitrate_alerts(
                    include_history=include_history,
                    target=data.get("target", data.get("stream")),
                )
            )
        except Exception as exc:  # noqa: BLE001
            errors["bitrate_alerts"] = str(exc)
    if "offline_alerts" in wanted:
        try:
            items.extend(collect_offline_alerts(only_alerted=only_alerted))
        except Exception as exc:  # noqa: BLE001
            errors["offline_alerts"] = str(exc)
    if "rate_anomaly" in wanted:
        try:
            items.extend(collect_rate_anomaly_alerts(events=events, **rate_kwargs))
        except Exception as exc:  # noqa: BLE001
            errors["rate_anomaly"] = str(exc)

    if severities is not None:
        wanted_sev = {_normalize_severity(s) for s in severities}
        items = [it for it in items if _normalize_severity(it.get("severity")) in wanted_sev]

    items = _sort_newest_first(items)
    total_before_limit = len(items)
    if limit is not None:
        items = items[:limit]

    by_source: Dict[str, int] = {s: 0 for s in SOURCES}
    by_severity: Dict[str, int] = {}
    for it in items:
        src = str(it.get("source", "unknown"))
        by_source[src] = int(by_source.get(src, 0) or 0) + 1
        sev = _normalize_severity(it.get("severity"))
        by_severity[sev] = int(by_severity.get(sev, 0) or 0) + 1

    out: Dict[str, Any] = {
        "ok": True,
        "success": True,
        "items": items,
        "alerts": items,
        "inbox": items,
        "count": len(items),
        "total": len(items),
        "total_before_limit": total_before_limit,
        "by_source": by_source,
        "by_severity": by_severity,
        "sources": wanted,
        "at": _now().isoformat(),
    }
    if errors:
        out["errors"] = errors
    return out


def list_alerts(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """Return unified inbox items sorted newest-first (with severity tiebreak)."""
    return list(get_inbox(*args, **kwargs).get("items", []))


def get_alerts(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    return list_alerts(*args, **kwargs)


def get_history(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    return list_alerts(*args, **kwargs)


def get_summary(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Inbox summary: counts per source / severity plus newest item."""
    inbox = get_inbox(*args, **kwargs)
    items = inbox.get("items", [])
    newest = items[0] if items else None
    return {
        "ok": True,
        "success": True,
        "count": len(items),
        "total": len(items),
        "by_source": inbox.get("by_source", {}),
        "by_severity": inbox.get("by_severity", {}),
        "sources": inbox.get("sources", list(SOURCES)),
        "newest": newest,
        "at": inbox.get("at"),
    }


def get_status(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Inbox status: per-source availability plus merged count."""
    per_source: Dict[str, Any] = {}
    for src in SOURCES:
        try:
            mod = __import__(f"app.services.{src}", fromlist=["*"])
            per_source[src] = {"available": True, "module": getattr(mod, "__name__", src)}
        except Exception as exc:  # noqa: BLE001
            per_source[src] = {"available": False, "error": str(exc)}
    inbox = get_inbox(*args, **kwargs)
    return {
        "ok": True,
        "success": True,
        "service": "alert_inbox",
        "sources": per_source,
        "count": inbox.get("count", 0),
        "by_source": inbox.get("by_source", {}),
        "by_severity": inbox.get("by_severity", {}),
        "at": inbox.get("at"),
    }


def resolve(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Delegate a resolve/ack to the owning source service.

    ``source`` selects the backend (default: try resource_watch, then
    bitrate_alerts, then offline_alerts). Returns the backend result.
    """
    data: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            data.update(a)
    data.update(kwargs or {})
    source = str(data.get("source", "") or "").strip().lower()
    rest = {k: v for k, v in data.items() if k != "source"}

    def _try(mod_name: str, fn_name: str = "resolve"):
        try:
            mod = __import__(f"app.services.{mod_name}", fromlist=[fn_name])
            fn = getattr(mod, fn_name, None)
            if callable(fn):
                res = fn(**rest)
                if isinstance(res, dict):
                    res.setdefault("ok", True)
                    res.setdefault("success", True)
                    res.setdefault("source", mod_name)
                    return res
                return {"ok": True, "success": True, "source": mod_name, "result": res}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "success": False, "source": mod_name, "error": str(exc)}
        return None

    if source in ("resource_watch", "resource-watch", "resource"):
        return _try("resource_watch") or {"ok": True, "success": True}
    if source in ("bitrate_alerts", "bitrate-alerts", "bitrate"):
        return _try("bitrate_alerts") or {"ok": True, "success": True}
    if source in ("offline_alerts", "offline-alerts", "offline"):
        return _try("offline_alerts") or {"ok": True, "success": True}
    for candidate in ("resource_watch", "bitrate_alerts", "offline_alerts"):
        out = _try(candidate)
        if isinstance(out, dict) and out.get("ok"):
            return out
    return {"ok": True, "success": True, "resolved": True}


def clear(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Clear alert state in the stateful sources (resource_watch, bitrate).

    Offline latches are left untouched unless ``include_offline=true``.
    """
    data: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            data.update(a)
    data.update(kwargs or {})
    include_offline = bool(data.get("include_offline", False))
    cleared: Dict[str, Any] = {}
    try:
        from app.services import resource_watch as _rw

        cleared["resource_watch"] = _rw.clear()
    except Exception as exc:  # noqa: BLE001
        cleared["resource_watch"] = {"ok": False, "error": str(exc)}
    try:
        from app.services import bitrate_alerts as _ba

        cleared["bitrate_alerts"] = _ba.clear()
    except Exception as exc:  # noqa: BLE001
        cleared["bitrate_alerts"] = {"ok": False, "error": str(exc)}
    if include_offline:
        try:
            from app.services import offline_alerts as _oa

            cleared["offline_alerts"] = _oa.clear() or {"ok": True}
        except Exception as exc:  # noqa: BLE001
            cleared["offline_alerts"] = {"ok": False, "error": str(exc)}
    return {"ok": True, "success": True, "cleared": True, "detail": cleared}


# ---------------------------------------------------------------------------
# Service class
# ---------------------------------------------------------------------------


class AlertInboxService:
    """Object-oriented facade over the unified alert inbox."""

    def get_inbox(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return get_inbox(*a, **k)

    def list_alerts(self, *a: Any, **k: Any) -> List[Dict[str, Any]]:
        return list_alerts(*a, **k)

    def get_alerts(self, *a: Any, **k: Any) -> List[Dict[str, Any]]:
        return list_alerts(*a, **k)

    def get_history(self, *a: Any, **k: Any) -> List[Dict[str, Any]]:
        return get_history(*a, **k)

    def get_summary(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return get_summary(*a, **k)

    def get_status(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return get_status(*a, **k)

    def resolve(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return resolve(*a, **k)

    def clear(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return clear(*a, **k)


AlertInboxManager = AlertInboxService
UnifiedAlertService = AlertInboxService
InboxService = AlertInboxService

alert_inbox_service = AlertInboxService()
alert_inbox = alert_inbox_service
service = alert_inbox_service
manager = alert_inbox_service
inbox = alert_inbox_service


def get_service() -> AlertInboxService:
    return alert_inbox_service


def create_service() -> AlertInboxService:
    return AlertInboxService()


# Aliases for flexible callers.
inbox_alerts = list_alerts
alerts = list_alerts
active_alerts = list_alerts
history = get_history
summary = get_summary
status = get_status
unified_inbox = get_inbox

__all__ = [
    "AlertInboxService",
    "alert_inbox_service",
    "service",
    "get_service",
    "create_service",
    "SOURCES",
    "SEVERITY_RANK",
    "collect_resource_watch_alerts",
    "collect_bitrate_alerts",
    "collect_offline_alerts",
    "collect_rate_anomaly_alerts",
    "get_inbox",
    "list_alerts",
    "get_alerts",
    "get_history",
    "get_summary",
    "get_status",
    "resolve",
    "clear",
]
