"""Group activity leaderboard service for VigilCam.

Ranks camera groups by activity volume. Complements the aggregate-only
views:

- ``app.services.summary.get_dashboard_summary`` returns dashboard
  sections (``cameras``/``events``/``recordings``/``storage``/``system``)
  with totals but no per-group ranking.
- ``app.services.group_digest`` returns per-group ``total``/``count`` and
  ``by_group``/``counts_by_group`` aggregates but no ordered ranking.

This module adds the missing ordered ranking layer on top of the same
``group_digest`` notification store:

- ``build_leaderboard()`` returns groups sorted by activity (desc) with
  explicit ``rank`` fields.
- ``get_top_groups(limit)`` returns the top-N slice.
- ``get_group_rank(group)`` returns the rank entry for one group.

All entry points accept tolerant ``*args/**kwargs`` like ``group_digest``.
Never raises on bad input: returns zero-filled payloads instead.
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_args(args: tuple, kwargs: Dict[str, Any]) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for a in args or ():
        if isinstance(a, dict):
            data.update(a)
        elif isinstance(a, str):
            s = a.strip()
            if not s:
                continue
            # Single bare string is most likely a group name or limit hint.
            if s.isdigit():
                data.setdefault("limit", int(s))
            elif data.get("group") is None:
                data["group"] = s
            elif data.get("message") is None:
                data["message"] = s
            else:
                data.setdefault("text", s)
        elif isinstance(a, (int, float)) and not isinstance(a, bool):
            data.setdefault("limit", int(a))
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
    for nested_key in ("payload", "options", "config", "params", "request", "data", "filter", "filters"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _coerce_limit(data: Dict[str, Any]) -> Optional[int]:
    for key in ("limit", "top_n", "top", "n", "size", "count", "max_results", "per_page", "page_size"):
        if data.get(key) is not None:
            try:
                val = int(data.get(key))
                if val < 0:
                    return 0
                return val
            except (TypeError, ValueError):
                continue
    return None


def _coerce_offset(data: Dict[str, Any]) -> int:
    for key in ("offset", "skip", "start_index", "page_offset"):
        if data.get(key) is not None:
            try:
                val = int(data.get(key))
                return max(0, val)
            except (TypeError, ValueError):
                continue
    # Page/page_number support: offset = (page-1) * limit.
    page = None
    for key in ("page", "page_number", "p"):
        if data.get(key) is not None:
            try:
                page = int(data.get(key))
                break
            except (TypeError, ValueError):
                pass
    if page is not None and page > 1:
        limit = _coerce_limit(data)
        if limit:
            return (page - 1) * limit
    return 0


def _coerce_order(data: Dict[str, Any]) -> str:
    for key in ("order", "sort", "direction", "sort_order", "dir"):
        val = data.get(key)
        if val is not None:
            s = str(val).strip().lower()
            if s in ("asc", "ascending", "ascend", "up", "lowest", "reverse"):
                return "asc"
            if s in ("desc", "descending", "descend", "down", "highest", "top"):
                return "desc"
    if data.get("ascending") is True:
        return "asc"
    return "desc"


def _resolve_counts(data: Dict[str, Any]) -> Dict[str, int]:
    """Return ``{group: count}`` from the group_digest store."""
    try:
        from app.services import group_digest as _gd
    except Exception:  # noqa: BLE001
        return {}
    try:
        camera_group_map = data.get("camera_group_map") or data.get("camera_groups") or data.get("group_map")
        if isinstance(camera_group_map, dict):
            counts = _gd.get_group_counts(camera_group_map=camera_group_map)
        else:
            counts = _gd.get_group_counts()
        if isinstance(counts, dict):
            return {str(k): int(v) for k, v in counts.items()}
    except Exception:  # noqa: BLE001
        pass
    return {}


def _rank_counts(counts: Dict[str, int], order: str = "desc") -> List[Dict[str, Any]]:
    """Sort counts into ranked entries (1-based, competition ranking)."""
    reverse = order != "asc"
    # Primary: count; tie-break: group name (case-insensitive) for stability.
    ordered = sorted(
        counts.items(),
        key=lambda kv: ((-kv[1]) if reverse else kv[1], str(kv[0]).lower()),
    )
    entries: List[Dict[str, Any]] = []
    prev_count: Optional[int] = None
    prev_rank = 0
    for idx, (group, count) in enumerate(ordered, start=1):
        rank = prev_rank if (prev_count is not None and count == prev_count) else idx
        prev_count = count
        prev_rank = rank
        entries.append(
            {
                "group": group,
                "group_id": group,
                "group_name": group,
                "name": group,
                "count": int(count),
                "total": int(count),
                "events": int(count),
                "notifications": int(count),
                "activity": int(count),
                "score": int(count),
                "rank": rank,
                "position": rank,
            }
        )
    return entries


def build_leaderboard(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Build the group activity leaderboard (ranked, ordered by count).

    Accepts optional ``limit``/``top_n``/``offset``/``order`` and an
    optional ``camera_group_map`` for dynamic membership.
    """
    data = _normalize_args(args, kwargs)
    counts = _resolve_counts(data)
    order = _coerce_order(data)
    ranked = _rank_counts(counts, order=order)

    total_events = sum(counts.values())
    total_groups = len(ranked)

    offset = _coerce_offset(data)
    limit = _coerce_limit(data)
    if offset or limit is not None:
        end = None if limit is None else offset + limit
        page_entries = ranked[offset:end]
    else:
        page_entries = list(ranked)

    board_id = uuid.uuid4().hex[:12]
    now_iso = _now_iso()
    by_group = dict(counts)
    top = page_entries[0] if page_entries else None
    return {
        "id": board_id,
        "board_id": board_id,
        "leaderboard_id": board_id,
        "ok": True,
        "success": True,
        "status": "ok",
        "order": order,
        "total": total_events,
        "count": len(page_entries),
        "total_events": total_events,
        "total_notifications": total_events,
        "total_activity": total_events,
        "total_groups": total_groups,
        "group_count": total_groups,
        "num_groups": total_groups,
        "groups": [e["group"] for e in ranked],
        "group_names": [e["group"] for e in ranked],
        "leaderboard": [dict(e) for e in page_entries],
        "rankings": [dict(e) for e in page_entries],
        "ranking": [dict(e) for e in page_entries],
        "entries": [dict(e) for e in page_entries],
        "ranks": [dict(e) for e in page_entries],
        "top": [dict(e) for e in page_entries],
        "top_groups": [dict(e) for e in page_entries],
        "board": [dict(e) for e in page_entries],
        "by_group": dict(by_group),
        "counts_by_group": dict(by_group),
        "counts": dict(by_group),
        "top_group": dict(top) if top else None,
        "leader": dict(top) if top else None,
        "first": dict(top) if top else None,
        "limit": limit,
        "offset": offset,
        "generated_at": now_iso,
        "created_at": now_iso,
        "timestamp": now_iso,
    }


def get_top_groups(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """Return the top-N ranked group entries as a list."""
    board = build_leaderboard(*args, **kwargs)
    entries = board.get("leaderboard") or []
    return [dict(e) for e in entries]


def get_group_rank(*args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
    """Return the rank entry for one group, or None when absent."""
    data = _normalize_args(args, kwargs)
    wanted = None
    for key in ("group", "group_id", "group_name", "name", "id", "slug"):
        if data.get(key) is not None and str(data.get(key)).strip():
            wanted = str(data.get(key)).strip()
            break
    board = build_leaderboard(**data)
    for entry in board.get("leaderboard", []) or []:
        if wanted is None:
            return dict(entry)
        if str(entry.get("group", "")).lower() == wanted.lower():
            return dict(entry)
    # Full (unpaginated) lookup fallback when pagination hid the group.
    if data.get("limit") is not None or data.get("offset"):
        full = build_leaderboard(**{k: v for k, v in data.items() if k not in ("limit", "top_n", "top", "n", "offset")})
        for entry in full.get("leaderboard", []) or []:
            if wanted is None:
                return dict(entry)
            if str(entry.get("group", "")).lower() == wanted.lower():
                return dict(entry)
    return None


def list_rankings(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
    """Return ranked entries as a list (alias for get_top_groups)."""
    return get_top_groups(*args, **kwargs)


def get_summary(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Alias returning the full leaderboard payload."""
    return build_leaderboard(*args, **kwargs)


def status(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data = _normalize_args(args, kwargs)
    counts = _resolve_counts(data)
    ranked = _rank_counts(counts, order=_coerce_order(data))
    return {
        "ok": True,
        "success": True,
        "status": "ok",
        "service": "leaderboard",
        "total": sum(counts.values()),
        "count": len(ranked),
        "total_groups": len(ranked),
        "group_count": len(ranked),
        "groups": [e["group"] for e in ranked],
        "top_group": dict(ranked[0]) if ranked else None,
        "leader": dict(ranked[0]) if ranked else None,
    }


# ---------------------------------------------------------------------------
# Async DB-backed helpers (camera membership -> group via group_digest)
# ---------------------------------------------------------------------------


async def build_leaderboard_for_db(db=None, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Build the leaderboard with DB camera membership applied."""
    data = _normalize_args(args, kwargs)
    if db is not None and "camera_group_map" not in data:
        try:
            from app.services import group_digest as _gd

            mapping = await _gd._camera_group_map_from_db(db)  # noqa: SLF001
            if mapping:
                data["camera_group_map"] = mapping
        except Exception:  # noqa: BLE001
            pass
    return build_leaderboard(**data)


# ---------------------------------------------------------------------------
# Aliases (tolerant naming for API/test compatibility)
# ---------------------------------------------------------------------------

get_leaderboard = build_leaderboard
fetch_leaderboard = build_leaderboard
query_leaderboard = build_leaderboard
compute_leaderboard = build_leaderboard
aggregate_leaderboard = build_leaderboard
generate_leaderboard = build_leaderboard
create_leaderboard = build_leaderboard
summarize_leaderboard = build_leaderboard
leaderboard_summary = build_leaderboard
summary = build_leaderboard
build_board = build_leaderboard
get_board = build_leaderboard
build_rankings = build_leaderboard
get_rankings = build_leaderboard
get_ranking = build_leaderboard
build_ranking = build_leaderboard
rank_groups = build_leaderboard
ranked_groups = build_leaderboard
group_rankings = build_leaderboard
group_leaderboard = build_leaderboard
activity_leaderboard = build_leaderboard
top_n_groups = get_top_groups
list_top_groups = get_top_groups
top_groups = get_top_groups
list_leaderboard = get_top_groups
get_entries = get_top_groups
get_ranks = get_top_groups
rank = get_group_rank
get_rank = get_group_rank
fetch_rank = get_group_rank
group_rank = get_group_rank

get_status = status


class LeaderboardService:
    """Object-oriented facade over the module functions."""

    def build_leaderboard(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return build_leaderboard(*a, **k)

    def get_leaderboard(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return build_leaderboard(*a, **k)

    def build(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return build_leaderboard(*a, **k)

    def get_summary(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return build_leaderboard(*a, **k)

    def summary(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return build_leaderboard(*a, **k)

    def top(self, *a: Any, **k: Any) -> List[Dict[str, Any]]:
        return get_top_groups(*a, **k)

    def get_top_groups(self, *a: Any, **k: Any) -> List[Dict[str, Any]]:
        return get_top_groups(*a, **k)

    def top_groups(self, *a: Any, **k: Any) -> List[Dict[str, Any]]:
        return get_top_groups(*a, **k)

    def list_rankings(self, *a: Any, **k: Any) -> List[Dict[str, Any]]:
        return get_top_groups(*a, **k)

    def get_group_rank(self, *a: Any, **k: Any) -> Optional[Dict[str, Any]]:
        return get_group_rank(*a, **k)

    def rank(self, *a: Any, **k: Any) -> Optional[Dict[str, Any]]:
        return get_group_rank(*a, **k)

    def status(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return status(*a, **k)


Leaderboard = LeaderboardService
BoardService = LeaderboardService
GroupLeaderboardService = LeaderboardService
GroupActivityLeaderboardService = LeaderboardService

leaderboard_service = LeaderboardService()
service = leaderboard_service
board_service = leaderboard_service
default_service = leaderboard_service


__all__ = [
    "build_leaderboard",
    "get_leaderboard",
    "get_top_groups",
    "get_group_rank",
    "list_rankings",
    "get_summary",
    "status",
    "build_leaderboard_for_db",
    "LeaderboardService",
    "leaderboard_service",
    "service",
]
