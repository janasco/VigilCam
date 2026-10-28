from __future__ import annotations
from app.api import app_sdk_template as _m

router = _m.router
underscore_router = getattr(_m, "underscore_router", None)
hyphen_router = getattr(_m, "hyphen_router", None)

# Provide prefixed aliases if tests expect
try:
    from fastapi import APIRouter
    # no-op
except Exception:
    pass
