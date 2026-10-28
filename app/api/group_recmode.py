"""Group recording-mode bulk router (auto-loaded by ``app.main``).

No edits to ``app/main.py`` are needed: this module exposes ``router``
and the pkgutil auto-loader in ``app.main`` picks it up.

Endpoints (POST, JSON bodies are tolerant of alias keys):

- ``/api/groups/bulk-recording-mode`` -- set recording_mode for member
  cameras of groups

Hyphen/underscore and ``/api/camera-groups/...`` aliases are registered
for every endpoint.

Verification note: ``app/api/group_bulk.py`` only toggles boolean member
camera flags (``enabled``/``recording_enabled``/``detection_enabled``/...)
and assigns/unassigns cameras. There is no ``recording_mode`` string
setting there -- these endpoints fill that gap.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import db_session
from app.services import group_recmode as svc

router = APIRouter(tags=["group_recmode"])


class BulkRecModeRequest(BaseModel):
    group_ids: Optional[Any] = None
    ids: Optional[Any] = None
    group_id: Optional[Any] = None
    groups: Optional[Any] = None
    names: Optional[Any] = None
    recording_mode: Optional[Any] = None
    rec_mode: Optional[Any] = None
    record_mode: Optional[Any] = None
    mode: Optional[Any] = None
    profile: Optional[Any] = None
    recording_profile: Optional[Any] = None

    model_config = {"extra": "allow"}


def _payload_dict(payload: Any, request: Request | None = None) -> Dict[str, Any]:
    if payload is None:
        data: Dict[str, Any] = {}
    elif hasattr(payload, "model_dump"):
        try:
            data = dict(payload.model_dump(exclude_unset=False))
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(payload, dict):
        data = dict(payload)
    else:
        try:
            data = dict(payload)
        except Exception:  # noqa: BLE001
            data = {}
    data = {k: v for k, v in data.items() if v is not None}
    if request is not None:
        try:
            for k, v in request.query_params.items():
                if k not in data or data.get(k) is None:
                    data[k] = v
        except Exception:  # noqa: BLE001
            pass
    for nested_key in ("payload", "params", "data", "body"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    return data


def _extract_group_ids(data: Dict[str, Any]) -> Any:
    for key in ("group_ids", "ids", "groups", "names", "group_id"):
        if data.get(key) is not None:
            return data.get(key)
    return data


def _extract_recording_mode(data: Dict[str, Any]) -> Any:
    for key in (
        "recording_mode",
        "recordingMode",
        "rec_mode",
        "recMode",
        "record_mode",
        "mode",
        "profile",
        "recording_profile",
        "schedule_mode",
    ):
        if data.get(key) is not None:
            return data.get(key)
    return None


async def _recmode_impl(db: AsyncSession, data: Dict[str, Any]) -> Dict[str, Any]:
    group_ids = _extract_group_ids(data)
    mode = _extract_recording_mode(data)
    if mode is None or (isinstance(mode, str) and not mode.strip()):
        raise HTTPException(status_code=400, detail="recording_mode is required")
    # group_ids required (non-empty)
    from app.services import group_bulk as _gb

    cids = _gb._coerce_int_list(group_ids)  # noqa: SLF001
    has_names = False
    if isinstance(group_ids, dict):
        raw_names = group_ids.get("names", group_ids.get("group_names"))
        has_names = bool(raw_names)
    elif isinstance(group_ids, str):
        has_names = bool(group_ids.strip())
    elif isinstance(group_ids, (list, tuple, set)):
        has_names = any(isinstance(x, str) and x.strip() for x in group_ids)
    if not cids and not has_names:
        if not group_ids:
            raise HTTPException(status_code=400, detail="group_ids is required")
    try:
        return await svc.bulk_set_group_recording_mode(db, group_ids, mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _make_recmode_endpoint():
    async def _endpoint(
        request: Request,
        payload: Any = Body(default=None),
        db: AsyncSession = Depends(db_session),
    ):
        data = _payload_dict(payload, request)
        return await _recmode_impl(db, data)

    return _endpoint


_RECMODE_PATHS = (
    "/api/groups/bulk-recording-mode",
    "/api/groups/bulk_recording_mode",
    "/api/groups/bulk/recording-mode",
    "/api/groups/bulk/recording_mode",
    "/api/camera-groups/bulk-recording-mode",
    "/api/camera-groups/bulk_recording_mode",
    "/api/camera-groups/bulk/recording-mode",
    "/api/cameras/groups/bulk-recording-mode",
    "/api/cameras/groups/bulk/recording-mode",
)


def _slug(p: str) -> str:
    return p.strip("/").replace("/", "_").replace("-", "_")


for _p in _RECMODE_PATHS:
    router.add_api_route(
        _p,
        _make_recmode_endpoint(),
        methods=["POST"],
        operation_id=f"group_recmode_set_{_slug(_p)}",
    )


# Compatibility aliases.
group_recmode_router = router
recmode_router = router
api_router = router

__all__ = ["router", "BulkRecModeRequest"]
