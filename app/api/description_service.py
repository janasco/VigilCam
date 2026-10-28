from __future__ import annotations

import sys as _sys

from app.api import genai_description as _gd

for _name in dir(_gd):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_gd, _name)

router = _gd.router
_sys.modules[__name__] = _gd
_sys.modules.setdefault("api.description_service", _gd)
