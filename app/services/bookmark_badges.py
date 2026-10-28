"""Bookmark badge counts service (read-only aggregates for UI badges).

Read-only layer over :mod:`app.services.bookmarks`. This module does NOT
modify ``bookmarks.py``; it only reads via its public ``list_bookmarks`` /
``BOOKMARKS`` API so UI code can render per-recording / per-camera badge
counts without fetching full bookmark lists.

No ``app/main.py`` edits are required: the companion
``app/api/bookmark_badges.py`` router is auto-registered by the pkgutil
auto-loader in ``app/main.py``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import app.services.bookmarks as _bookmarks_mod

UNASSIGNED_KEY = "unassigned"


def _key(value: Any) -> str:
    if value is None:
        return UNASSIGNED_KEY
    text = str(value).strip()
    return text if text else UNASSIGNED_KEY


def _all() -> List[Dict[str, Any]]:
    try:
        return list(_bookmarks_mod.list_bookmarks())
    except Exception:
        try:
            return list(_bookmarks_mod.BOOKMARKS.values())
        except Exception:
            return []


def total_count() -> int:
    """Total number of bookmarks."""
    return len(_all())


def count_for_recording(recording_id: Any) -> int:
    """Number of bookmarks attached to one recording."""
    if recording_id is None:
        return 0
    want = str(recording_id)
    return sum(1 for bm in _all() if str(bm.get("recording_id")) == want)


def count_for_camera(camera_id: Any) -> int:
    """Number of bookmarks attached to one camera."""
    if camera_id is None:
        return 0
    want = str(camera_id)
    return sum(1 for bm in _all() if str(bm.get("camera_id")) == want)


def counts_by_recording() -> Dict[str, int]:
    """Mapping of ``recording_id -> bookmark count`` (str keys)."""
    counts: Dict[str, int] = {}
    for bm in _all():
        k = _key(bm.get("recording_id"))
        counts[k] = counts.get(k, 0) + 1
    return counts


def counts_by_camera() -> Dict[str, int]:
    """Mapping of ``camera_id -> bookmark count`` (str keys)."""
    counts: Dict[str, int] = {}
    for bm in _all():
        k = _key(bm.get("camera_id"))
        counts[k] = counts.get(k, 0) + 1
    return counts


def badge_for_recording(recording_id: Any) -> Dict[str, Any]:
    count = count_for_recording(recording_id)
    return {
        "recording_id": recording_id,
        "count": count,
        "has_bookmarks": count > 0,
    }


def badge_for_camera(camera_id: Any) -> Dict[str, Any]:
    count = count_for_camera(camera_id)
    return {
        "camera_id": camera_id,
        "count": count,
        "has_bookmarks": count > 0,
    }


def _unique_values(items: List[Dict[str, Any]], field: str) -> List[Any]:
    seen: List[Any] = []
    for bm in items:
        v = bm.get(field)
        if v is None:
            continue
        if v not in seen:
            seen.append(v)
    return seen


def badges_for_recordings(
    recording_ids: Optional[List[Any]] = None,
) -> List[Dict[str, Any]]:
    """Badge dicts for each known (or requested) recording id."""
    items = _all()
    ids = list(recording_ids) if recording_ids is not None else _unique_values(items, "recording_id")
    counts = counts_by_recording()
    out: List[Dict[str, Any]] = []
    for rid in ids:
        count = counts.get(str(rid), 0)
        out.append({"recording_id": rid, "count": count, "has_bookmarks": count > 0})
    return out


def badges_for_cameras(
    camera_ids: Optional[List[Any]] = None,
) -> List[Dict[str, Any]]:
    """Badge dicts for each known (or requested) camera id."""
    items = _all()
    ids = list(camera_ids) if camera_ids is not None else _unique_values(items, "camera_id")
    counts = counts_by_camera()
    out: List[Dict[str, Any]] = []
    for cid in ids:
        count = counts.get(str(cid), 0)
        out.append({"camera_id": cid, "count": count, "has_bookmarks": count > 0})
    return out


def get_badge_counts(
    recording_id: Any = None,
    camera_id: Any = None,
) -> Dict[str, Any]:
    """Aggregate counts payload for UI badges.

    When ``recording_id``/``camera_id`` filters are given, ``filtered_count``
    reflects the scoped count while ``total``/``by_recording``/``by_camera``
    always describe the whole store.
    """
    items = _all()
    scoped = list(items)
    if recording_id is not None:
        want = str(recording_id)
        scoped = [b for b in scoped if str(b.get("recording_id")) == want]
    if camera_id is not None:
        want = str(camera_id)
        scoped = [b for b in scoped if str(b.get("camera_id")) == want]
    by_recording = counts_by_recording()
    by_camera = counts_by_camera()
    return {
        "total": len(items),
        "count": len(scoped),
        "filtered_count": len(scoped),
        "recording_id": recording_id,
        "camera_id": camera_id,
        "by_recording": by_recording,
        "by_camera": by_camera,
        "recording_badges": badges_for_recordings(),
        "camera_badges": badges_for_cameras(),
    }


def get_summary(
    recording_id: Any = None,
    camera_id: Any = None,
) -> Dict[str, Any]:
    """Alias of :func:`get_badge_counts` (``summary`` naming)."""
    return get_badge_counts(recording_id=recording_id, camera_id=camera_id)


# Compatibility aliases.
summary = get_summary
badge_counts = get_badge_counts
counts = get_badge_counts
counts_summary = get_badge_counts
get_counts = get_badge_counts
count = total_count

__all__ = [
    "UNASSIGNED_KEY",
    "total_count",
    "count_for_recording",
    "count_for_camera",
    "counts_by_recording",
    "counts_by_camera",
    "badge_for_recording",
    "badge_for_camera",
    "badges_for_recordings",
    "badges_for_cameras",
    "get_badge_counts",
    "get_summary",
]
