from __future__ import annotations
from app.api import floorplan_editor as _m

router = _m.router
floorplan_editor_router = _m.router
layout_editor_router = _m.router

try:
    from app.main import app as _app
    _app.include_router(router)
except Exception:  # noqa: BLE001
    pass