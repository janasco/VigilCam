"""Label-based recording search API router.

Auto-registered by ``app/main.py`` (pkgutil auto-loader), so no edits to
``app/main.py`` are required.

Answers "which recordings cover events labelled X": given an event ``label``
(e.g. ``person``), returns the ``Recording`` rows whose
``[started_at, ended_at]`` overlaps any matching event's time window. A
``POST .../discover`` endpoint also accepts explicit ``events`` + ``recordings``
candidate lists for offline use (no DB required).

This complements ``app.api.event_recordings`` (verified): that router finds
recordings by **event id** only, with no label-based recording search.
"""

from __future__ import annotations

from typing import Any, List, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request, status

import app.services.label_recordings as rec_mod
from app.services.label_recordings import find_recordings_for_label

router = APIRouter(tags=["label_recordings"])


def _normalize(payload: Any, request: Optional[Request] = None) -> dict:
    if payload is None:
        data: dict = {}
    elif isinstance(payload, str):
        data = {"label": payload}
    elif hasattr(payload, "model_dump"):
        try:
            data = dict(payload.model_dump())
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    elif isinstance(payload, (bytes, bytearray)):
        try:
            data = {"label": bytes(payload).decode("utf-8", errors="replace")}
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, (list, tuple)):
        data = {"events": list(payload)}
    else:
        try:
            data = dict(payload)
        except Exception:  # noqa: BLE001
            data = {"label": str(payload)}
    if request is not None:
        try:
            for k, v in request.query_params.items():
                if k not in data or data.get(k) is None:
                    vals = request.query_params.getlist(k)
                    data[k] = vals[0] if len(vals) == 1 else vals
        except Exception:  # noqa: BLE001
            pass
    for nested_key in ("event", "link", "payload", "options", "config",
                       "params", "data", "body", "request", "fields",
                       "filter", "filters"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _coerce_int_or_none(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(str(value).strip()))
        except (TypeError, ValueError):
            return None


def _has_candidates(data: dict) -> bool:
    for k in ("events", "events_list", "recordings", "recordings_list",
              "candidates", "rows", "items"):
        v = data.get(k)
        if isinstance(v, (list, tuple)) and len(v) > 0:
            return True
        if v is not None and not isinstance(v, (list, tuple)):
            return True
    return False


def _require_label(data: dict) -> Any:
    raw = data.get("label", data.get("labels", data.get("detection_label",
            data.get("object", data.get("class")))))
    labels = rec_mod.normalize_labels(raw)
    if not labels:
        raise HTTPException(status_code=400, detail="label is required")
    return labels[0] if len(labels) == 1 else labels


async def _do_find(data: dict, db: Any) -> List[dict]:
    label = _require_label(data)
    data = dict(data)
    data["label"] = label
    # Offline path: explicit candidate lists need no DB.
    if _has_candidates(data):
        try:
            return find_recordings_for_label(data, **data)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
    if db is not None:
        try:
            return await rec_mod.find_recordings_for_label_async(db=db, **data)
        except Exception:  # noqa: BLE001
            pass
        # DB reachable but nothing overlapped (or no matching events): [].
        try:
            out = await rec_mod.find_recordings_for_label_async(db=db, **data)
            return out
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
    raise HTTPException(
        status_code=400,
        detail="label search needs events/recordings candidates or database access",
    )


def _register_top_level(r: APIRouter, base: str) -> None:
    from fastapi import Depends as _Depends  # noqa: E402

    from app.api.deps import db_session as _db_session  # noqa: E402

    async def _list_all(
        request: Request,
        label: Optional[str] = Query(default=None),
        camera_id: Optional[int] = Query(default=None),
        match: Optional[str] = Query(default=None),
        limit: Optional[int] = Query(default=None),
        db: Any = _Depends(_db_session),
    ):
        data = _normalize(None, request)
        if label is not None and data.get("label") is None:
            data["label"] = label
        if camera_id is not None and data.get("camera_id") is None:
            data["camera_id"] = camera_id
        if match is not None and data.get("match") is None:
            data["match"] = match
        if limit is not None and data.get("limit") is None:
            data["limit"] = limit
        return await _do_find(data, db)

    async def _discover_top(
        payload: Any = Body(default=None),
        request: Request = None,  # type: ignore[assignment]
        db: Any = _Depends(_db_session),
    ):
        data = _normalize(payload, request)
        return await _do_find(data, db)

    _list_all.__name__ = f"list_label_recordings_top_{abs(hash(base)) % 10_000_000}"
    _discover_top.__name__ = f"discover_label_recordings_top_{abs(hash(base)) % 10_000_000}"

    r.get(base, summary="List recordings covering events with a label")(_list_all)
    r.get(base + "/", include_in_schema=False)(_list_all)
    r.post(base + "/discover", summary="Discover recordings for a label")(_discover_top)
    r.post(base + "/find", include_in_schema=False)(_discover_top)
    r.post(base + "/search", include_in_schema=False)(_discover_top)
    r.post(base, status_code=status.HTTP_200_OK, include_in_schema=False)(_discover_top)


_TOP_BASES = (
    "/api/label-recordings",
    "/api/label_recordings",
    "/api/labelrecordings",
)
_seen_top: set = set()
for _base in _TOP_BASES:
    _norm = _base.rstrip("/") or "/"
    if _norm in _seen_top:
        continue
    _seen_top.add(_norm)
    _register_top_level(router, _base)


# Compatibility aliases.
label_recordings_router = router
recordings_router = router
label_recording_router = router
api_router = router

__all__ = ["router"]
