from __future__ import annotations

import sys as _sys

from app.services import genai_description as _gd

for _name in dir(_gd):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_gd, _name)

router = getattr(_gd, "router", None)
_sys.modules[__name__] = _gd
_sys.modules.setdefault("services.description_service", _gd)
