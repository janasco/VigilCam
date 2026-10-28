"""zmfilter-style auto event rules (in-memory, no DB migration).

ZoneMinder concept (read-only reference: ``scripts/zmfilter.pl.in`` +
``scripts/ZoneMinder/lib/ZoneMinder/Filter.pm`` in the ZoneMinder tree):

* A stored **filter** has a name, a ``Query_json`` term list
  (``[{attr, op, val, cnj, obr, cbr}, ...]`` combined with AND/OR) and a set
  of **auto actions**: ``AutoArchive / AutoUnarchive / AutoVideo / AutoUpload /
  AutoEmail / AutoMessage / AutoExecute / AutoDelete / AutoMove / AutoCopy /
  UpdateDiskSpace``.  Background filters run on an ``ExecuteInterval`` via the
  ``zmfilter.pl`` daemon (``checkFilter`` -> ``checkFilterEvent`` per event).

VigilCam verification (see ``app/services/retention.py``):

* ``RetentionManager`` enforces **only** ``max_age_days`` (mtime age of
  segmented ``.mp4`` files under the recording root) and ``max_total_bytes``
  (oldest-first size pruning).  ``select_for_deletion`` accepts no
  event-condition parameters (no camera label/kind, confidence/score, tags,
  cause, monitor id or time-of-day conditions) -- it operates on recording
  files, not on the ``events`` table.  Event listing (``app/api/events.py``)
  can *filter* for display, but performs no automatic action.

This module adds the missing zmfilter-style layer for **events**:

* Named rules with **conditions** (simple criteria dict and/or ZM-style
  ``terms`` list) matched against event fields (``camera_id``, ``label``,
  ``kind``, ``confidence``, ``tags``, ``created_at``/age).
* **Auto actions** mirroring ZM flags, adapted to VigilCam's ``Event`` model
  (``id, camera_id, kind, label, confidence, payload, tags, snapshot_path,
  created_at`` -- no archived/videoed/emailed columns, so archive state is
  tracked with an ``archived`` tag): ``auto_delete``, ``auto_archive``,
  ``auto_unarchive``, ``auto_tag``, ``auto_acknowledge``, ``auto_email``,
  ``auto_message``, ``auto_execute``, ``auto_export``/``auto_video``,
  ``auto_upload``, ``auto_move_to``/``auto_copy_to`` (reported only),
  ``update_disk_space`` (reported only).
* ``enabled``/``background`` + ``execute_interval`` scheduling metadata with
  ``due_rules()`` (mirrors ZM ``Background``/``ExecuteInterval``/``last_ran``).
* ``dry_run`` evaluation (``preview``/``evaluate``) that reports matches and
  planned actions without mutating events, and ``run`` which applies the
  safe in-memory actions (tag/archive/delete markers on dict events).

The store is intentionally in-memory (thread-safe), so the feature works
without DB migrations and without requiring ``app/main.py`` edits (the API
router is auto-registered by the pkgutil auto-loader).  An optional
``db``/``session`` argument is accepted by the async entrypoints for
forward-compatibility but is currently ignored (best-effort no-op).
"""

from __future__ import annotations

import copy
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

_lock = threading.Lock()
_RULES: Dict[int, Dict[str, Any]] = {}
_ID_COUNTER = 0
_LAST_RAN: Dict[int, float] = {}

ARCHIVED_TAG = "archived"

NAME_KEYS = (
    "name",
    "rule_name",
    "ruleName",
    "filter_name",
    "filterName",
    "title",
    "display_name",
    "displayName",
    "label",
)
ID_KEYS = (
    "id",
    "rule_id",
    "ruleId",
    "filter_id",
    "filterId",
    "saved_filter_id",
    "key",
    "uuid",
)
DESCRIPTION_KEYS = (
    "description",
    "desc",
    "detail",
    "details",
    "notes",
    "note",
    "comment",
    "summary",
)
CONDITION_KEYS = (
    "conditions",
    "condition",
    "criteria",
    "criterion",
    "filter",
    "filters",
    "query",
    "query_json",
    "queryJson",
    "params",
    "spec",
    "definition",
    "config",
    "options",
    "match",
    "matcher",
    "where",
)
TERMS_KEYS = ("terms", "term_list", "termList", "clauses", "predicates", "rules")
ACTION_KEYS = (
    "actions",
    "action",
    "auto",
    "effects",
    "operations",
    "do",
    "then",
    "consequences",
)

# Event-query keys that may be supplied top-level and are mirrored into conditions.
EVENT_CONDITION_KEYS = (
    "camera_id",
    "camera_ids",
    "cameras",
    "camera",
    "cam",
    "monitor_id",
    "monitorId",
    "label",
    "labels",
    "kind",
    "kinds",
    "type",
    "types",
    "object",
    "objects",
    "tag",
    "tags",
    "event_tag",
    "event_tags",
    "min_confidence",
    "confidence_min",
    "min_score",
    "score_min",
    "max_confidence",
    "confidence_max",
    "max_score",
    "score_max",
    "older_than_days",
    "olderThanDays",
    "newer_than_days",
    "newerThanDays",
    "max_age_days",
    "min_age_days",
    "age_days",
    "start",
    "start_time",
    "end",
    "end_time",
)

# ZM-style action flags (canonical snake_case) + accepted aliases.
ACTION_ALIASES: Dict[str, tuple] = {
    "auto_delete": ("auto_delete", "autoDelete", "AutoDelete", "delete", "delete_events", "remove"),
    "auto_archive": ("auto_archive", "autoArchive", "AutoArchive", "archive", "archive_events"),
    "auto_unarchive": ("auto_unarchive", "autoUnarchive", "AutoUnarchive", "unarchive"),
    "auto_tag": ("auto_tag", "autoTag", "tags_to_add", "tagsToAdd", "add_tags", "addTags", "tag"),
    "auto_acknowledge": ("auto_acknowledge", "autoAcknowledge", "acknowledge", "ack"),
    "auto_email": ("auto_email", "autoEmail", "AutoEmail", "email", "notify_email"),
    "auto_message": ("auto_message", "autoMessage", "AutoMessage", "message", "notify"),
    "auto_execute": ("auto_execute", "autoExecute", "AutoExecute", "execute", "execute_cmd"),
    "auto_export": ("auto_export", "autoExport", "export", "auto_video", "autoVideo", "AutoVideo", "video"),
    "auto_upload": ("auto_upload", "autoUpload", "AutoUpload", "upload"),
    "auto_move_to": ("auto_move_to", "autoMoveTo", "AutoMoveTo", "move_to", "moveTo"),
    "auto_copy_to": ("auto_copy_to", "autoCopyTo", "AutoCopyTo", "copy_to", "copyTo"),
    "update_disk_space": ("update_disk_space", "updateDiskSpace", "UpdateDiskSpace", "recount"),
}
EMAIL_KEYS = ("email_to", "emailTo", "EmailTo", "email", "notify_to", "to")
EXECUTE_CMD_KEYS = ("execute_cmd", "executeCmd", "AutoExecuteCmd", "auto_execute_cmd", "command", "cmd")

VALID_OPS = {"=", "!=", ">", ">=", "<", "<=", "LIKE", "NOT LIKE", "IN", "NOT IN"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _next_id() -> int:
    global _ID_COUNTER
    _ID_COUNTER += 1
    return _ID_COUNTER


def _coerce_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(str(value).strip()))
        except (TypeError, ValueError):
            return default


def _coerce_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        try:
            return float(str(value).strip())
        except (TypeError, ValueError):
            return default


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    return s in {"1", "true", "yes", "y", "on", "enabled", "active"}


def _first_present(data: Dict[str, Any], keys: tuple) -> Any:
    for k in keys:
        if k in data and data.get(k) is not None:
            return data.get(k)
    lower = {str(k).lower(): v for k, v in data.items()}
    for k in keys:
        if k.lower() in lower and lower[k.lower()] is not None:
            return lower[k.lower()]
    return None


def _flatten(data: Any) -> Dict[str, Any]:
    if data is None:
        return {}
    if hasattr(data, "model_dump"):
        try:
            return dict(data.model_dump())
        except Exception:
            pass
    if isinstance(data, dict):
        out = dict(data)
        for nested_key in CONDITION_KEYS + ACTION_KEYS + (
            "payload", "data", "body", "request", "fields", "entry", "rule", "auto_rule", "autoRule",
        ):
            nested = out.get(nested_key)
            if isinstance(nested, dict):
                for k, v in nested.items():
                    out.setdefault(k, v)
        return out
    try:
        return dict(data)
    except Exception:
        return {}


def _normalize_inputs(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    flat: Dict[str, Any] = {}
    for a in args:
        if a is None:
            continue
        if isinstance(a, dict):
            flat.update(a)
        elif hasattr(a, "model_dump"):
            try:
                flat.update(dict(a.model_dump()))
            except Exception:
                pass
        elif isinstance(a, str):
            s = a.strip()
            if not s:
                continue
            if "name" not in flat:
                flat["name"] = s
            else:
                flat.setdefault("description", s)
        else:
            try:
                flat.update(dict(a))
            except Exception:
                pass
    flat.update(kwargs or {})
    for nested_key in ("rule", "auto_rule", "autoRule", "payload", "params", "data", "body", "request", "fields", "entry"):
        nested = flat.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                flat.setdefault(k, v)
    return flat


def _extract_terms(flat: Dict[str, Any]) -> List[Dict[str, Any]]:
    for key in TERMS_KEYS:
        val = flat.get(key)
        if isinstance(val, list):
            out = []
            for t in val:
                if isinstance(t, dict):
                    out.append(dict(t))
            if out:
                return out
        if isinstance(val, dict) and isinstance(val.get("terms"), list):
            return [dict(t) for t in val["terms"] if isinstance(t, dict)]
    for key in CONDITION_KEYS:
        val = flat.get(key)
        if isinstance(val, dict) and isinstance(val.get("terms"), list):
            return [dict(t) for t in val["terms"] if isinstance(t, dict)]
        if isinstance(val, list) and val and all(isinstance(t, dict) and "attr" in t for t in val):
            return [dict(t) for t in val]
    return []


def _extract_conditions(flat: Dict[str, Any]) -> Dict[str, Any]:
    conditions: Dict[str, Any] = {}
    for key in CONDITION_KEYS:
        val = flat.get(key)
        if isinstance(val, dict):
            for k, v in val.items():
                if k in TERMS_KEYS and isinstance(v, list):
                    continue
                if k in ACTION_KEYS:
                    continue
                conditions.setdefault(k, v)
    # Mirror known event-filter keys found top-level.
    for key in EVENT_CONDITION_KEYS:
        if key in flat and flat.get(key) is not None and key not in conditions:
            conditions[key] = flat.get(key)
        if key not in conditions:
            for fk, fv in flat.items():
                if str(fk).lower() == key.lower() and fv is not None and key not in conditions:
                    conditions[key] = fv
                    break
    return conditions


def _extract_actions(flat: Dict[str, Any]) -> Dict[str, Any]:
    actions: Dict[str, Any] = {}
    for key in ACTION_KEYS:
        val = flat.get(key)
        if isinstance(val, dict):
            for k, v in val.items():
                actions.setdefault(k, v)
    # Map ZM-style / alias action keys found anywhere in flat.
    for canonical, aliases in ACTION_ALIASES.items():
        for alias in aliases:
            if alias in flat and flat.get(alias) is not None:
                actions.setdefault(canonical, flat.get(alias))
                break
            for fk, fv in flat.items():
                if str(fk).lower() == alias.lower() and fv is not None and canonical not in actions:
                    actions[canonical] = fv
                    break
    # Email recipient / execute command live alongside actions.
    email_to = _first_present(flat, EMAIL_KEYS)
    if email_to is not None:
        actions.setdefault("email_to", email_to)
    exec_cmd = _first_present(flat, EXECUTE_CMD_KEYS)
    if exec_cmd is not None:
        actions.setdefault("execute_cmd", exec_cmd)
    # Normalise auto_tag to a list.
    if "auto_tag" in actions:
        actions["auto_tag"] = _coerce_tag_list(actions.get("auto_tag"))
    return actions


def _coerce_tag_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return []
        if "," in s:
            return [p.strip() for p in s.split(",") if p.strip()]
        return [s]
    if isinstance(value, (list, tuple, set)):
        out: List[str] = []
        for item in value:
            out.extend(_coerce_tag_list(item))
        seen: set = set()
        deduped: List[str] = []
        for t in out:
            if t.lower() not in seen:
                seen.add(t.lower())
                deduped.append(t)
        return deduped
    s = str(value).strip()
    return [s] if s else []


def _event_field(event: Any, *names: str) -> Any:
    if isinstance(event, dict):
        for n in names:
            if n in event and event.get(n) is not None:
                return event.get(n)
        lower = {str(k).lower(): v for k, v in event.items()}
        for n in names:
            if n.lower() in lower and lower[n.lower()] is not None:
                return lower[n.lower()]
        return None
    for n in names:
        if hasattr(event, n):
            try:
                v = getattr(event, n)
            except Exception:
                continue
            if v is not None:
                return v() if callable(v) else v
    # case-insensitive attribute fallback
    try:
        attrs = {a.lower(): a for a in dir(event) if not a.startswith("_")}
    except Exception:
        return None
    for n in names:
        if n.lower() in attrs:
            try:
                v = getattr(event, attrs[n.lower()])
            except Exception:
                continue
            if v is not None:
                return v() if callable(v) else v
    return None


def _event_tags(event: Any) -> List[str]:
    raw = _event_field(event, "tags", "tag", "event_tags", "labels")
    if raw is None:
        # payload-embedded tags?
        payload = _event_field(event, "payload")
        if isinstance(payload, str) and payload.strip().startswith("{"):
            try:
                import json as _json

                obj = _json.loads(payload)
                if isinstance(obj, dict):
                    for k in ("tags", "tag", "labels"):
                        if obj.get(k) is not None:
                            return _coerce_tag_list(obj.get(k))
            except Exception:
                pass
        return []
    return _coerce_tag_list(raw)


def _event_created_at(event: Any) -> Optional[datetime]:
    raw = _event_field(event, "created_at", "createdAt", "start_time", "startDateTime", "timestamp", "time")
    if raw is None:
        return None
    if isinstance(raw, datetime):
        dt = raw
    elif isinstance(raw, (int, float)):
        try:
            ts = float(raw)
            if abs(ts) > 1e12:
                ts = ts / 1000.0
            dt = datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)
        except Exception:
            return None
    else:
        s = str(raw).strip()
        if not s:
            return None
        s2 = s[:-1] + "+00:00" if s.endswith("Z") else s
        try:
            dt = datetime.fromisoformat(s2)
        except ValueError:
            try:
                dt = datetime.fromisoformat(s + "T00:00:00")
            except ValueError:
                return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _event_age_days(event: Any) -> Optional[float]:
    dt = _event_created_at(event)
    if dt is None:
        return None
    return (datetime.now() - dt).total_seconds() / 86400.0


def _canonical_attr(attr: Any) -> str:
    s = str(attr or "").strip()
    low = s.lower()
    mapping = {
        "monitorid": "camera_id", "monitor_id": "camera_id", "cameraid": "camera_id",
        "camera": "camera_id", "cam": "camera_id",
        "name": "label", "cause": "label", "object": "label",
        "class": "label", "classname": "label",
        "score": "confidence", "maxscore": "confidence", "avgscore": "confidence",
        "tag": "tags", "event_tag": "tags",
        "startdatetime": "created_at", "enddatetime": "created_at", "date": "created_at",
        "startdate": "created_at", "datetime": "created_at", "time": "created_at",
        "archived": "archived",
    }
    return mapping.get(low, low)


def _event_value_for_attr(event: Any, attr: Any) -> Any:
    key = _canonical_attr(attr)
    if key == "camera_id":
        return _event_field(event, "camera_id", "cameraId", "camera", "monitor_id", "cam")
    if key == "label":
        return _event_field(event, "label", "name", "cause", "object", "class")
    if key == "kind":
        return _event_field(event, "kind", "type", "event_type")
    if key == "confidence":
        return _event_field(event, "confidence", "score", "max_score", "probability")
    if key == "tags":
        return _event_tags(event)
    if key == "created_at":
        return _event_created_at(event)
    if key == "archived":
        tags = [t.lower() for t in _event_tags(event)]
        return ARCHIVED_TAG in tags
    if key in {"id", "event_id", "eventid"}:
        return _event_field(event, "id", "event_id", "eventId")
    # generic: try the raw attribute name on the event.
    return _event_field(event, str(attr), key)


def _compare(op: str, actual: Any, expected: Any) -> bool:
    norm = (op or "=").strip().upper()
    if norm in {"=[]", "IN"}:
        norm = "IN"
    elif norm in {"![]", "NOT IN"}:
        norm = "NOT IN"
    elif norm in {"=~"}:
        norm = "LIKE"
    elif norm in {"!~"}:
        norm = "NOT LIKE"

    if norm in {"IN", "NOT IN"}:
        if isinstance(expected, str):
            exp_list = [p.strip() for p in expected.split(",") if p.strip()]
        elif isinstance(expected, (list, tuple, set)):
            exp_list = [str(v) for v in expected]
        else:
            exp_list = [str(expected)]
        if isinstance(actual, list):
            hit = any(str(a).lower() in {e.lower() for e in exp_list} for a in actual)
        else:
            hit = str(actual).lower() in {e.lower() for e in exp_list} if actual is not None else False
        return hit if norm == "IN" else not hit

    if isinstance(actual, list):
        # tag-list semantics: = means "contains", != means "does not contain".
        if norm == "=":
            return any(str(a).lower() == str(expected).lower() for a in actual)
        if norm == "!=":
            return not any(str(a).lower() == str(expected).lower() for a in actual)
        if norm in {"LIKE", "NOT LIKE"}:
            import re as _re

            pat = str(expected).replace("%", ".*")
            hit = any(_re.search(pat, str(a), _re.IGNORECASE) for a in actual)
            return hit if norm == "LIKE" else not hit
        return False

    if norm in {"LIKE", "NOT LIKE"}:
        import re as _re

        pat = str(expected).replace("%", ".*")
        hit = bool(_re.search(pat, str(actual or ""), _re.IGNORECASE))
        return hit if norm == "LIKE" else not hit

    if norm in {"=", "==", "IS"}:
        if isinstance(actual, bool) or isinstance(expected, bool):
            return bool(actual) is _coerce_bool(expected) if isinstance(expected, str) else bool(actual) == bool(expected)
        try:
            return float(actual) == float(expected)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return str(actual or "").lower() == str(expected or "").lower()
    if norm in {"!=", "<>", "IS NOT"}:
        try:
            return float(actual) != float(expected)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return str(actual or "").lower() != str(expected or "").lower()
    # Ordering: numeric when possible, else string/datetime.
    try:
        a_num, e_num = float(actual), float(expected)  # type: ignore[arg-type]
        if norm == ">":
            return a_num > e_num
        if norm == ">=":
            return a_num >= e_num
        if norm == "<":
            return a_num < e_num
        if norm == "<=":
            return a_num <= e_num
    except (TypeError, ValueError):
        a_s, e_s = str(actual or ""), str(expected or "")
        if norm == ">":
            return a_s > e_s
        if norm == ">=":
            return a_s >= e_s
        if norm == "<":
            return a_s < e_s
        if norm == "<=":
            return a_s <= e_s
    return False


def _match_term(event: Any, term: Dict[str, Any]) -> bool:
    if not isinstance(term, dict):
        return False
    attr = term.get("attr", term.get("field", term.get("key")))
    op = term.get("op", term.get("operator", "="))
    val = term.get("val", term.get("value", term.get("values")))
    if attr is None:
        return False
    key = _canonical_attr(attr)
    # Age pseudo-attributes handled against event age.
    if key in {"older_than_days", "age_days", "max_age_days"}:
        age = _event_age_days(event)
        if age is None:
            return False
        try:
            return age > float(val)
        except (TypeError, ValueError):
            return False
    if key in {"newer_than_days", "min_age_days"}:
        age = _event_age_days(event)
        if age is None:
            return False
        try:
            return age < float(val)
        except (TypeError, ValueError):
            return False
    actual = _event_value_for_attr(event, attr)
    if key == "created_at" and isinstance(actual, datetime):
        # Allow date-string comparisons.
        expected_dt = None
        if isinstance(val, str):
            s2 = val[:-1] + "+00:00" if val.endswith("Z") else val
            try:
                expected_dt = datetime.fromisoformat(s2)
            except ValueError:
                expected_dt = None
            if expected_dt is not None and expected_dt.tzinfo is not None:
                expected_dt = expected_dt.astimezone(timezone.utc).replace(tzinfo=None)
        if expected_dt is not None:
            norm = (op or "=").strip()
            if norm == ">=":
                return actual >= expected_dt
            if norm == ">":
                return actual > expected_dt
            if norm == "<=":
                return actual <= expected_dt
            if norm == "<":
                return actual < expected_dt
            return actual == expected_dt
        return _compare(str(op), actual.isoformat(), val)
    return _compare(str(op), actual, val)


def _match_terms(event: Any, terms: List[Dict[str, Any]]) -> bool:
    if not terms:
        return True
    result: Optional[bool] = None
    pending_cnj = "and"
    for term in terms:
        cnj = str(term.get("cnj", term.get("conjunction", pending_cnj)) or "and").lower()
        if result is None:
            # First term: conjunction ignored.
            result = _match_term(event, term)
        else:
            matched = _match_term(event, term)
            if cnj == "or":
                result = result or matched
            else:
                result = result and matched
        pending_cnj = cnj
    return bool(result)


def _match_simple_conditions(event: Any, conditions: Dict[str, Any]) -> bool:
    if not conditions:
        return True
    # camera
    cam_ids: List[str] = []
    for k in ("camera_id", "camera_ids", "cameras", "camera", "cam", "monitor_id", "monitorId"):
        v = conditions.get(k)
        if v is None:
            continue
        if isinstance(v, (list, tuple, set)):
            cam_ids.extend(str(x) for x in v)
        elif isinstance(v, str) and "," in v:
            cam_ids.extend(p.strip() for p in v.split(",") if p.strip())
        else:
            cam_ids.append(str(v))
    if cam_ids:
        actual = _event_field(event, "camera_id", "cameraId", "camera", "monitor_id", "cam")
        if actual is None or str(actual) not in {c for c in cam_ids}:
            # also allow numeric comparison
            try:
                if int(actual) not in {int(c) for c in cam_ids}:  # type: ignore[arg-type]
                    return False
            except (TypeError, ValueError):
                return False
    # label
    labels: List[str] = []
    for k in ("label", "labels", "kind", "kinds", "type", "types", "object", "objects"):
        v = conditions.get(k)
        if v is None:
            continue
        if isinstance(v, (list, tuple, set)):
            labels.extend(str(x).lower() for x in v)
        elif isinstance(v, str) and "," in v:
            labels.extend(p.strip().lower() for p in v.split(",") if p.strip())
        else:
            labels.append(str(v).lower())
    if labels:
        actual_label = _event_field(event, "label", "name", "cause", "object")
        actual_kind = _event_field(event, "kind", "type")
        hay = {str(actual_label or "").lower(), str(actual_kind or "").lower()}
        if not (hay & set(labels)):
            return False
    # tags: event must contain at least one requested tag.
    tag_req: List[str] = []
    for k in ("tags", "tag", "event_tag", "event_tags"):
        v = conditions.get(k)
        if v is None:
            continue
        tag_req.extend(t.lower() for t in _coerce_tag_list(v))
    if tag_req:
        ev_tags = [t.lower() for t in _event_tags(event)]
        if not any(t in ev_tags for t in tag_req):
            return False
    # confidence bounds
    min_conf = _coerce_float(
        _first_present(conditions, ("min_confidence", "confidence_min", "min_score", "score_min")), None
    )
    if min_conf is not None:
        actual = _event_field(event, "confidence", "score")
        if actual is None:
            return False
        try:
            if float(actual) < min_conf:
                return False
        except (TypeError, ValueError):
            return False
    max_conf = _coerce_float(
        _first_present(conditions, ("max_confidence", "confidence_max", "max_score", "score_max")), None
    )
    if max_conf is not None:
        actual = _event_field(event, "confidence", "score")
        if actual is None:
            return False
        try:
            if float(actual) > max_conf:
                return False
        except (TypeError, ValueError):
            return False
    # age bounds
    older = _coerce_float(_first_present(conditions, ("older_than_days", "olderThanDays", "max_age_days", "age_days")), None)
    if older is not None:
        age = _event_age_days(event)
        if age is None or not age > older:
            return False
    newer = _coerce_float(_first_present(conditions, ("newer_than_days", "newerThanDays", "min_age_days")), None)
    if newer is not None:
        age = _event_age_days(event)
        if age is None or not age < newer:
            return False
    # time range on created_at
    start_raw = _first_present(conditions, ("start", "start_time", "since", "after", "from"))
    end_raw = _first_present(conditions, ("end", "end_time", "until", "before", "to"))
    if start_raw is not None or end_raw is not None:
        dt = _event_created_at(event)
        if dt is None:
            return False
        if start_raw is not None:
            s = str(start_raw)
            s2 = s[:-1] + "+00:00" if s.endswith("Z") else s
            try:
                sdt = datetime.fromisoformat(s2)
                if sdt.tzinfo is not None:
                    sdt = sdt.astimezone(timezone.utc).replace(tzinfo=None)
                if dt < sdt:
                    return False
            except ValueError:
                pass
        if end_raw is not None:
            s = str(end_raw)
            s2 = s[:-1] + "+00:00" if s.endswith("Z") else s
            try:
                edt = datetime.fromisoformat(s2)
                if edt.tzinfo is not None:
                    edt = edt.astimezone(timezone.utc).replace(tzinfo=None)
                if dt > edt:
                    return False
            except ValueError:
                pass
    return True


def rule_matches(rule: Dict[str, Any], event: Any) -> bool:
    """Return True when an enabled rule's conditions match an event."""
    if not isinstance(rule, dict):
        return False
    if rule.get("enabled") is False or rule.get("background") is False:
        # Explicitly disabled rules never match (ZM Background=0 skips daemon runs).
        if str(rule.get("enabled", "")).lower() in {"0", "false", "no", "off"}:
            return False
    conditions = rule.get("conditions") or {}
    terms = rule.get("terms") or []
    if not _match_simple_conditions(event, conditions if isinstance(conditions, dict) else {}):
        return False
    if terms and not _match_terms(event, terms):
        return False
    return True


def _to_public(record: Dict[str, Any]) -> Dict[str, Any]:
    return copy.deepcopy(record)


def _normalize_actions_input(actions: Any, flat: Dict[str, Any]) -> Dict[str, Any]:
    if isinstance(actions, dict):
        merged = dict(flat)
        merged.update(actions)
        return _extract_actions(merged)
    if actions is None:
        return _extract_actions(flat)
    return _extract_actions(flat)


def _build_record(flat: Dict[str, Any], existing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    base = dict(existing) if existing else {}
    name = _first_present(flat, NAME_KEYS)
    description = _first_present(flat, DESCRIPTION_KEYS)
    terms = _extract_terms(flat)
    conditions = _extract_conditions(flat)
    explicit_actions = None
    for k in ACTION_KEYS:
        if isinstance(flat.get(k), dict):
            explicit_actions = flat.get(k)
            break
    actions = _normalize_actions_input(explicit_actions, flat)
    if existing is not None:
        # Preserve stored actions/conditions unless the update supplies new ones.
        if not terms and not conditions:
            terms = list(base.get("terms") or [])
            conditions = dict(base.get("conditions") or {})
        if not actions:
            actions = dict(base.get("actions") or {})

    enabled_raw = _first_present(
        flat,
        ("enabled", "background", "active", "is_active", "is_enabled", "on", "run_in_background", "Background"),
    )
    if enabled_raw is None and existing is not None:
        enabled = bool(base.get("enabled", True))
    elif enabled_raw is None:
        enabled = True
    else:
        enabled = _coerce_bool(enabled_raw)

    interval_raw = _first_present(
        flat, ("execute_interval", "executeInterval", "ExecuteInterval", "interval", "interval_seconds", "run_interval")
    )
    if interval_raw is None and existing is not None:
        execute_interval = base.get("execute_interval", 300)
    elif interval_raw is None:
        execute_interval = 300
    else:
        try:
            execute_interval = int(float(str(interval_raw)))
        except (TypeError, ValueError):
            execute_interval = 300

    concurrent_raw = _first_present(flat, ("concurrent", "Concurrent", "allow_concurrent"))
    lock_raw = _first_present(flat, ("lock_rows", "lockRows", "LockRows", "lock"))
    now = _now_iso()
    if existing is None:
        rid = _next_id()
        record: Dict[str, Any] = {
            "id": rid,
            "rule_id": rid,
            "filter_id": rid,
            "ruleId": rid,
            "name": str(name).strip() if name is not None else "",
            "rule_name": str(name).strip() if name is not None else "",
            "filter_name": str(name).strip() if name is not None else "",
            "title": str(name).strip() if name is not None else "",
            "description": str(description).strip() if description is not None else str(flat.get("description", "")),
            "conditions": copy.deepcopy(conditions),
            "criteria": copy.deepcopy(conditions),
            "terms": copy.deepcopy(terms),
            "actions": copy.deepcopy(actions),
            "enabled": bool(enabled),
            "background": bool(enabled),
            "active": bool(enabled),
            "execute_interval": int(execute_interval),
            "ExecuteInterval": int(execute_interval),
            "concurrent": _coerce_bool(concurrent_raw) if concurrent_raw is not None else False,
            "lock_rows": _coerce_bool(lock_raw) if lock_raw is not None else False,
            "created_at": now,
            "updated_at": now,
            "last_ran": None,
        }
        for k, v in conditions.items():
            record.setdefault(k, copy.deepcopy(v))
        return record
    record = copy.deepcopy(base)
    if name is not None and str(name).strip():
        s = str(name).strip()
        record["name"] = s
        record["rule_name"] = s
        record["filter_name"] = s
        record["title"] = s
    if description is not None:
        record["description"] = str(description)
    record["conditions"] = copy.deepcopy(conditions)
    record["criteria"] = copy.deepcopy(conditions)
    record["terms"] = copy.deepcopy(terms)
    record["actions"] = copy.deepcopy(actions)
    record["enabled"] = bool(enabled)
    record["background"] = bool(enabled)
    record["active"] = bool(enabled)
    record["execute_interval"] = int(execute_interval)
    record["ExecuteInterval"] = int(execute_interval)
    if concurrent_raw is not None:
        record["concurrent"] = _coerce_bool(concurrent_raw)
    if lock_raw is not None:
        record["lock_rows"] = _coerce_bool(lock_raw)
    record["updated_at"] = _now_iso()
    return record


def create_rule(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Create a zmfilter-style auto rule. ``name`` is required.

    Accepts flexible inputs, e.g.::

        create_rule(name="purge low-conf", conditions={"max_confidence": 0.3},
                    actions={"auto_delete": True})
        create_rule(name="archive door", terms=[{"attr": "label", "op": "=", "val": "door"}],
                    actions={"auto_archive": True})
    """
    flat = _normalize_inputs(*args, **kwargs)
    name = _first_present(flat, NAME_KEYS)
    if name is None or not str(name).strip():
        raise ValueError("name is required")
    with _lock:
        record = _build_record(flat, None)
        _RULES[int(record["id"])] = record
        return _to_public(record)


def list_rules(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """List auto rules, optionally filtered by name/enabled."""
    flat = _flatten(args[0]) if len(args) == 1 and isinstance(args[0], dict) else {}
    for a in args:
        if isinstance(a, str) and "q" not in flat and "name" not in flat:
            flat["q"] = a
    flat.update(kwargs or {})
    q = _first_present(flat, ("q", "query", "search", "text_search", "name_search"))
    name = _first_present(flat, ("name", "rule_name", "ruleName", "filter_name", "title"))
    enabled = _first_present(flat, ("enabled", "background", "active"))
    limit = _coerce_int(_first_present(flat, ("limit", "n", "count", "size", "page_size")), None)
    offset = _coerce_int(_first_present(flat, ("offset", "skip", "start_index")), 0) or 0
    with _lock:
        items = [_to_public(_RULES[k]) for k in sorted(_RULES.keys())]
    if name is not None and str(name).strip():
        ns = str(name).strip().lower()
        items = [r for r in items if ns == str(r.get("name", "")).strip().lower()]
    if q is not None and str(q).strip():
        qs = str(q).strip().lower()
        items = [r for r in items if qs in str(r.get("name", "")).lower() or qs in str(r.get("description", "")).lower()]
    if enabled is not None and str(enabled).strip() != "":
        want = _coerce_bool(enabled)
        items = [r for r in items if bool(r.get("enabled", True)) is want]
    if offset:
        items = items[offset:]
    if limit is not None and limit >= 0:
        items = items[:limit]
    return items


def get_rule(rule_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    flat = _flatten(args[0]) if args and isinstance(args[0], dict) else {}
    flat.update(kwargs or {})
    if rule_id is None:
        rule_id = _first_present(flat, ID_KEYS)
    if rule_id is None and args:
        for a in args:
            if isinstance(a, (int, float, str)) and not isinstance(a, bool):
                rule_id = a
                break
    if rule_id is None:
        name = _first_present(flat, NAME_KEYS)
        if name is not None:
            return get_rule_by_name(name)
        return None
    nid = _coerce_int(rule_id, None)
    with _lock:
        if nid is not None and int(nid) in _RULES:
            return _to_public(_RULES[int(nid)])
        s = str(rule_id).strip()
        for record in _RULES.values():
            if str(record.get("id")) == s or str(record.get("rule_id")) == s:
                return _to_public(record)
        lowered = s.lower()
        for record in _RULES.values():
            if str(record.get("name", "")).strip().lower() == lowered:
                return _to_public(record)
    return None


def get_rule_by_name(name: Any) -> Optional[Dict[str, Any]]:
    if name is None:
        return None
    s = str(name).strip().lower()
    if not s:
        return None
    with _lock:
        for record in _RULES.values():
            if str(record.get("name", "")).strip().lower() == s:
                return _to_public(record)
    return None


def update_rule(rule_id: Any = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    flat: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            flat.update(a)
        elif hasattr(a, "model_dump"):
            try:
                flat.update(dict(a.model_dump()))
            except Exception:
                pass
        elif isinstance(a, str):
            s = a.strip()
            if s and "name" not in flat and rule_id is None:
                flat.setdefault("_positional", s)
    flat.update(kwargs or {})
    if rule_id is None:
        rule_id = _first_present(flat, ID_KEYS)
    positional = flat.pop("_positional", None)
    if rule_id is None and positional is not None:
        existing = get_rule(positional)
        if existing is not None:
            rule_id = existing["id"]
        else:
            return None
    if rule_id is None and args:
        for a in args:
            if isinstance(a, (int, float)) and not isinstance(a, bool):
                rule_id = a
                break
    nid = _coerce_int(rule_id, None)
    with _lock:
        target_id: Optional[int] = None
        if nid is not None and int(nid) in _RULES:
            target_id = int(nid)
        elif rule_id is not None:
            s = str(rule_id).strip()
            for k, record in _RULES.items():
                if str(record.get("id")) == s or str(record.get("name", "")).strip().lower() == s.lower():
                    target_id = k
                    break
        if target_id is None:
            return None
        updated = _build_record(flat, _RULES[target_id])
        updated["id"] = target_id
        updated["rule_id"] = target_id
        _RULES[target_id] = updated
        return _to_public(updated)


def delete_rule(rule_id: Any = None, *args: Any, **kwargs: Any) -> bool:
    flat: Dict[str, Any] = {}
    for a in args:
        if isinstance(a, dict):
            flat.update(a)
    flat.update(kwargs or {})
    if rule_id is None:
        rule_id = _first_present(flat, ID_KEYS)
    if rule_id is None and args:
        for a in args:
            if isinstance(a, (int, float, str)) and not isinstance(a, bool):
                rule_id = a
                break
    nid = _coerce_int(rule_id, None)
    with _lock:
        if nid is not None and int(nid) in _RULES:
            _RULES.pop(int(nid), None)
            _LAST_RAN.pop(int(nid), None)
            return True
        if rule_id is not None:
            s = str(rule_id).strip()
            for k, record in list(_RULES.items()):
                if str(record.get("id")) == s or str(record.get("name", "")).strip().lower() == s.lower():
                    _RULES.pop(k, None)
                    _LAST_RAN.pop(k, None)
                    return True
        return False


def count_rules(*args: Any, **kwargs: Any) -> int:
    return len(list_rules(*args, **kwargs))


def clear(*args: Any, **kwargs: Any) -> int:
    with _lock:
        n = len(_RULES)
        _RULES.clear()
        _LAST_RAN.clear()
        return n


def due_rules(*args: Any, now: Optional[float] = None, **kwargs: Any) -> List[Dict[str, Any]]:
    """Return enabled/background rules whose ExecuteInterval has elapsed."""
    ts = float(now) if now is not None else time.time()
    rules = list_rules(*args, **kwargs)
    out = []
    for r in rules:
        if not r.get("enabled", True):
            continue
        interval = r.get("execute_interval", 300) or 300
        try:
            interval = int(interval)
        except (TypeError, ValueError):
            interval = 300
        last = _LAST_RAN.get(int(r["id"]), 0.0)
        last_ran_field = r.get("last_ran")
        if last_ran_field:
            try:
                last = max(last, datetime.fromisoformat(str(last_ran_field)).timestamp())
            except Exception:
                pass
        if ts - last >= interval:
            out.append(r)
    return out


def _event_id(event: Any) -> Any:
    v = _event_field(event, "id", "event_id", "eventId")
    return v


def _apply_actions_to_event(event: Any, actions: Dict[str, Any], dry_run: bool = True) -> Dict[str, Any]:
    """Compute (and optionally apply) a rule's actions for one event."""
    applied: Dict[str, Any] = {
        "delete": False,
        "archive": False,
        "unarchive": False,
        "tagged": [],
        "acknowledged": False,
        "emailed": False,
        "messaged": False,
        "executed": False,
        "exported": False,
        "uploaded": False,
        "moved": False,
        "copied": False,
    }
    if _coerce_bool(actions.get("auto_delete")):
        applied["delete"] = True
    if _coerce_bool(actions.get("auto_archive")):
        applied["archive"] = True
    if _coerce_bool(actions.get("auto_unarchive")):
        applied["unarchive"] = True
    extra_tags = _coerce_tag_list(actions.get("auto_tag"))
    if _coerce_bool(actions.get("auto_archive")) and ARCHIVED_TAG not in [t.lower() for t in extra_tags]:
        extra_tags = extra_tags + [ARCHIVED_TAG]
    applied["tagged"] = extra_tags
    if _coerce_bool(actions.get("auto_acknowledge")):
        applied["acknowledged"] = True
    if _coerce_bool(actions.get("auto_email")):
        applied["emailed"] = True
    if _coerce_bool(actions.get("auto_message")):
        applied["messaged"] = True
    if _coerce_bool(actions.get("auto_execute")) or actions.get("execute_cmd"):
        applied["executed"] = True
    if _coerce_bool(actions.get("auto_export")):
        applied["exported"] = True
    if _coerce_bool(actions.get("auto_upload")):
        applied["uploaded"] = True
    if actions.get("auto_move_to"):
        applied["moved"] = True
    if actions.get("auto_copy_to"):
        applied["copied"] = True

    if not dry_run and isinstance(event, dict):
        # Mutate tag state in place for dict-style events.
        current = _coerce_tag_list(event.get("tags", event.get("tag", [])))
        lowered = {t.lower() for t in current}
        for t in extra_tags:
            if t.lower() not in lowered:
                current.append(t)
                lowered.add(t.lower())
        if applied["unarchive"]:
            current = [t for t in current if t.lower() != ARCHIVED_TAG]
        event["tags"] = current
        event["tag"] = current
        if applied["archive"]:
            event["archived"] = True
        if applied["unarchive"]:
            event["archived"] = False
    return applied


def match_events(rule: Dict[str, Any], events: List[Any]) -> List[Any]:
    """Return the subset of events matching a rule (no actions applied)."""
    return [e for e in (events or []) if rule_matches(rule, e)]


def evaluate_rule(
    rule: Dict[str, Any],
    events: List[Any],
    dry_run: bool = True,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Evaluate one rule against events (zmfilter ``checkFilter`` equivalent)."""
    if "dryRun" in kwargs:
        dry_run = bool(kwargs.pop("dryRun"))
    if "dry-run" in kwargs:
        dry_run = bool(kwargs.pop("dry-run"))
    matched = match_events(rule, events or [])
    actions = rule.get("actions") or {}
    per_event = []
    deleted_ids: List[Any] = []
    for ev in matched:
        applied = _apply_actions_to_event(ev, actions, dry_run=dry_run)
        eid = _event_id(ev)
        per_event.append({"event_id": eid, "event": ev if isinstance(ev, dict) else None, "actions": applied})
        if applied["delete"]:
            deleted_ids.append(eid)
    return {
        "rule_id": rule.get("id"),
        "rule_name": rule.get("name"),
        "name": rule.get("name"),
        "dry_run": bool(dry_run),
        "enabled": bool(rule.get("enabled", True)),
        "matched_count": len(matched),
        "matched_ids": [_event_id(e) for e in matched],
        "matched": [_event_id(e) for e in matched],
        "events": matched if dry_run else matched,
        "deleted_ids": deleted_ids,
        "deleted": deleted_ids,
        "delete_count": len(deleted_ids),
        "actions": dict(actions),
        "per_event": per_event,
        "execute_interval": rule.get("execute_interval", 300),
    }


def evaluate_rules(
    events: List[Any],
    dry_run: bool = True,
    rules: Optional[List[Dict[str, Any]]] = None,
    only_enabled: bool = True,
    **kwargs: Any,
) -> List[Dict[str, Any]]:
    """Evaluate every (or supplied) rule against events (zmfilter scan loop)."""
    if rules is None:
        rules = list_rules()
    if only_enabled:
        rules = [r for r in rules if r.get("enabled", True)]
    return [evaluate_rule(r, events or [], dry_run=dry_run) for r in rules]


def run_rules(
    events: List[Any],
    dry_run: bool = False,
    rules: Optional[List[Dict[str, Any]]] = None,
    only_enabled: bool = True,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Run auto rules over an event list and apply safe in-memory actions.

    Mirrors ``zmfilter.pl`` ``checkFilter``/``checkFilterEvent``: per matching
    event apply ``AutoArchive/AutoUnarchive/AutoDelete/AutoTag/...``.  With
    ``dry_run=True`` nothing is mutated; otherwise dict events gain tags and
    ``deleted_ids`` reports what a DB-backed caller should delete.
    """
    if "dryRun" in kwargs:
        dry_run = bool(kwargs.pop("dryRun"))
    if "dry-run" in kwargs:
        dry_run = bool(kwargs.pop("dry-run"))
    if rules is None:
        rules = list_rules()
    if only_enabled:
        rules = [r for r in rules if r.get("enabled", True)]
    results = [evaluate_rule(r, events or [], dry_run=dry_run) for r in rules]
    matched_ids: List[Any] = []
    deleted_ids: List[Any] = []
    for res in results:
        matched_ids.extend(res.get("matched_ids") or [])
        deleted_ids.extend(res.get("deleted_ids") or [])
    # De-dupe preserving order.
    def _dedup(items: List[Any]) -> List[Any]:
        seen: set = set()
        out: List[Any] = []
        for i in items:
            key = str(i)
            if key not in seen:
                seen.add(key)
                out.append(i)
        return out

    matched_ids = _dedup(matched_ids)
    deleted_ids = _dedup(deleted_ids)
    now = time.time()
    if not dry_run:
        with _lock:
            for r in rules:
                try:
                    _LAST_RAN[int(r["id"])] = now
                except (TypeError, ValueError, KeyError):
                    pass
    return {
        "dry_run": bool(dry_run),
        "rules_evaluated": len(results),
        "rules": results,
        "results": results,
        "matched_count": len(matched_ids),
        "matched_ids": matched_ids,
        "matched": matched_ids,
        "deleted_count": len(deleted_ids),
        "deleted_ids": deleted_ids,
        "deleted": deleted_ids,
    }


# --- Async entrypoints (db-optional, ignored) --------------------------------

async def create_rule_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> Dict[str, Any]:
    kwargs.pop("db", None)
    kwargs.pop("session", None)
    return create_rule(*args, **kwargs)


async def list_rules_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> List[Dict[str, Any]]:
    return list_rules(*args, **kwargs)


async def get_rule_async(rule_id: Any = None, *args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return get_rule(rule_id, *args, **kwargs)


async def update_rule_async(rule_id: Any = None, *args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> Optional[Dict[str, Any]]:
    return update_rule(rule_id, *args, **kwargs)


async def delete_rule_async(rule_id: Any = None, *args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> bool:
    return delete_rule(rule_id, *args, **kwargs)


class AutoRulesService:
    """Object-oriented facade over the module-level store."""

    def create(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return create_rule(*args, **kwargs)

    def list(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return list_rules(*args, **kwargs)

    def get(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return get_rule(*args, **kwargs)

    def update(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return update_rule(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> bool:
        return delete_rule(*args, **kwargs)

    def clear(self, *args: Any, **kwargs: Any) -> int:
        return clear(*args, **kwargs)

    def matches(self, *args: Any, **kwargs: Any) -> bool:
        return rule_matches(*args, **kwargs)

    def match(self, *args: Any, **kwargs: Any) -> List[Any]:
        return match_events(*args, **kwargs)

    def evaluate(self, *args: Any, **kwargs: Any) -> Any:
        if args and isinstance(args[0], dict) and ("conditions" in args[0] or "terms" in args[0] or "actions" in args[0]):
            events = kwargs.pop("events", [])
            return evaluate_rule(args[0], events, **kwargs)
        return evaluate_rules(*args, **kwargs)

    def run(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return run_rules(*args, **kwargs)

    # aliases
    create_rule = create
    list_rules = list
    get_rule = get
    update_rule = update
    delete_rule = delete
    rule_matches = matches
    match_events = match
    evaluate_rules = evaluate
    run_rules = run


AutoRuleService = AutoRulesService
EventAutoRulesService = AutoRulesService
ZmFilterService = AutoRulesService

auto_rules_service = AutoRulesService()
auto_rule_service = auto_rules_service
event_auto_rules_service = auto_rules_service
zmfilter_service = auto_rules_service
service = auto_rules_service
manager = auto_rules_service
store = auto_rules_service


# Compatibility aliases (function level).
create_auto_rule = create_rule
add_auto_rule = create_rule
save_auto_rule = create_rule
new_rule = create_rule
post_rule = create_rule
create_filter_rule = create_rule
create_zmfilter_rule = create_rule

list_auto_rules = list_rules
get_auto_rules = list_rules
fetch_rules = list_rules
fetch_auto_rules = list_rules
all_rules = list_rules

get_auto_rule = get_rule
fetch_rule = get_rule
find_rule = get_rule
get_filter_rule = get_rule
get_by_name = get_rule_by_name

edit_rule = update_rule
update_auto_rule = update_rule
edit_auto_rule = update_rule
patch_rule = update_rule

remove_rule = delete_rule
delete_auto_rule = delete_rule
remove_auto_rule = delete_rule

clear_rules = clear
clear_auto_rules = clear
reset = clear
purge = clear

filter_events = match_events
match_rule = match_events
select_events = match_events
apply_rule = evaluate_rule
check_rule = evaluate_rule
check_filter = evaluate_rule
evaluate = evaluate_rules
preview = evaluate_rules
preview_rules = evaluate_rules
run = run_rules
execute = run_rules
apply_rules = run_rules
run_all = run_rules

__all__ = [
    "create_rule",
    "list_rules",
    "get_rule",
    "get_rule_by_name",
    "update_rule",
    "delete_rule",
    "count_rules",
    "clear",
    "due_rules",
    "rule_matches",
    "match_events",
    "evaluate_rule",
    "evaluate_rules",
    "run_rules",
    "create_rule_async",
    "list_rules_async",
    "get_rule_async",
    "update_rule_async",
    "delete_rule_async",
    "AutoRulesService",
    "auto_rules_service",
]
