from __future__ import annotations

import app.api.cloud_inference as _primary

from app.api.cloud_inference import *  # noqa: F401,F403

router = _primary.router
underscore_router = getattr(_primary, "underscore_router", None)
hyphen_router = getattr(_primary, "hyphen_router", None)
service_router = router
api_router = router
