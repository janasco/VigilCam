from __future__ import annotations
from app.services import app_sdk_template as _m

for _n in dir(_m):
    if not _n.startswith("__"):
        globals()[_n] = getattr(_m, _n)

# extra aliases
for _src, _dsts in {
    "register": ["register_template", "register_templates", "register_app_sdk_template"],
    "register_model": ["register_template", "register_templates", "register_app_sdk_template"],
    "list_models": ["list_templates", "list_app_sdk_templates", "templates"],
    "get_model": ["get_template", "get_app_sdk_template"],
    "delete": ["delete_template", "delete_app_sdk_template"],
    "clear": ["clear_templates", "clear_app_sdk_templates"],
    "exists": ["exists_template", "template_exists"],
    "count": ["count_templates", "template_count"],
}.items():
    if _src in globals():
        for _d in _dsts:
            globals().setdefault(_d, globals()[_src])

__all__ = getattr(_m, "__all__", [])
