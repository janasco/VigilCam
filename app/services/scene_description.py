from __future__ import annotations

import sys as _sys

from app.services import genai_description as _gd

# Re-export everything from the canonical module and make this module an alias.
for _name in dir(_gd):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_gd, _name)
__all__ = getattr(_gd, "__all__", [n for n in dir(_gd) if not n.startswith("_")])

_sys.modules[__name__] = _gd
# top-level services alias
_sys.modules.setdefault("services.scene_description", _gd)
_sys.modules.setdefault("services.genai_description", _gd)
