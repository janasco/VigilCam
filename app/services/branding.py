"""White-label branding settings service for VigilCam.

Single global branding record (site name, logo URL, colors) served via
``app.api.branding``. In-memory, thread-safe, dependency-free.

No ``app/main.py`` or ``app/templates/index.html`` edits are required
(the API auto-loader in ``app/main.py`` picks up the router).
"""

from __future__ import annotations

import copy
import re
import threading
from typing import Any, Dict, List, Optional

_lock = threading.RLock()

DEFAULT_BRANDING: Dict[str, Any] = {
    "site_name": "VigilCam",
    "logo_url": "",
    "primary_color": "#1a73e8",
    "secondary_color": "#202124",
    "accent_color": "#34a853",
}

_BRANDING: Dict[str, Any] = copy.deepcopy(DEFAULT_BRANDING)

_HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")

SITE_NAME_KEYS = (
    "site_name",
    "siteName",
    "site_title",
    "siteTitle",
    "title",
    "app_name",
    "appName",
    "brand_name",
    "brandName",
    "name",
)

LOGO_KEYS = (
    "logo_url",
    "logoUrl",
    "logo",
    "logo_path",
    "logoPath",
    "brand_logo",
    "site_logo",
)

PRIMARY_KEYS = (
    "primary_color",
    "primaryColor",
    "primary",
    "brand_color",
    "brandColor",
    "theme_color",
)

SECONDARY_KEYS = (
    "secondary_color",
    "secondaryColor",
    "secondary",
)

ACCENT_KEYS = (
    "accent_color",
    "accentColor",
    "accent",
)

NESTED_KEYS = (
    "branding",
    "brand",
    "white_label",
    "whiteLabel",
    "whitelabel",
    "settings",
    "options",
    "config",
    "params",
    "data",
    "body",
    "payload",
)


def _first(data: Dict[str, Any], keys: tuple) -> Any:
    for k in keys:
        if k in data and data.get(k) is not None:
            return data.get(k)
    lower = {str(k).lower(): v for k, v in data.items()}
    for k in keys:
        lk = str(k).lower()
        if lk in lower and lower[lk] is not None:
            return lower[lk]
    return None


def _normalize_inputs(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for a in args:
        if a is None:
            continue
        if isinstance(a, dict):
            data.update(a)
        elif hasattr(a, "model_dump"):
            try:
                data.update(dict(a.model_dump()))
            except Exception:  # noqa: BLE001
                pass
        elif isinstance(a, str):
            s = a.strip()
            if s and data.get("site_name") is None:
                data["site_name"] = s
        else:
            try:
                data.update(dict(a))
            except Exception:  # noqa: BLE001
                pass
    data.update(kwargs or {})
    for nested_key in NESTED_KEYS:
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                data.setdefault(k, v)
    # Also merge a nested "colors" dict if present.
    colors = data.get("colors")
    if isinstance(colors, dict):
        for k, v in colors.items():
            data.setdefault(k, v)
    return data


def _validate_color(value: Any, field: str) -> str:
    s = str(value).strip()
    if not _HEX_COLOR_RE.match(s):
        raise ValueError(f"{field} must be a hex color like '#1a73e8'")
    return s


def _coerce_branding(data: Dict[str, Any], base: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    merged = copy.deepcopy(base) if base is not None else copy.deepcopy(DEFAULT_BRANDING)

    site = _first(data, SITE_NAME_KEYS)
    if site is not None:
        s = str(site).strip()
        if not s:
            raise ValueError("site_name must not be empty")
        merged["site_name"] = s

    logo = _first(data, LOGO_KEYS)
    if logo is not None:
        merged["logo_url"] = str(logo).strip()

    primary = _first(data, PRIMARY_KEYS)
    if primary is not None:
        merged["primary_color"] = _validate_color(primary, "primary_color")

    secondary = _first(data, SECONDARY_KEYS)
    if secondary is not None:
        merged["secondary_color"] = _validate_color(secondary, "secondary_color")

    accent = _first(data, ACCENT_KEYS)
    if accent is not None:
        merged["accent_color"] = _validate_color(accent, "accent_color")

    return merged


def _to_public(stored: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(stored)
    # Common aliases so varied clients can read values back.
    out.setdefault("siteName", out.get("site_name"))
    out.setdefault("title", out.get("site_name"))
    out.setdefault("logoUrl", out.get("logo_url"))
    out.setdefault("logo", out.get("logo_url"))
    out.setdefault("primaryColor", out.get("primary_color"))
    out.setdefault("secondaryColor", out.get("secondary_color"))
    out.setdefault("accentColor", out.get("accent_color"))
    out.setdefault("colors", {
        "primary": out.get("primary_color"),
        "secondary": out.get("secondary_color"),
        "accent": out.get("accent_color"),
    })
    return out


def get_branding(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Return a copy of the current branding settings."""
    with _lock:
        snapshot = copy.deepcopy(_BRANDING)
    return _to_public(snapshot)


def set_branding(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Replace branding settings (missing fields fall back to defaults)."""
    data = _normalize_inputs(*args, **kwargs)
    coerced = _coerce_branding(data, base=dict(DEFAULT_BRANDING))
    with _lock:
        globals()["_BRANDING"] = copy.deepcopy(coerced)
        snapshot = copy.deepcopy(_BRANDING)
    return _to_public(snapshot)


def update_branding(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Merge the given fields into the current branding settings."""
    data = _normalize_inputs(*args, **kwargs)
    with _lock:
        current = copy.deepcopy(_BRANDING)
    coerced = _coerce_branding(data, base=current)
    with _lock:
        globals()["_BRANDING"] = copy.deepcopy(coerced)
        snapshot = copy.deepcopy(_BRANDING)
    return _to_public(snapshot)


def reset_branding(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Restore default branding settings."""
    with _lock:
        globals()["_BRANDING"] = copy.deepcopy(DEFAULT_BRANDING)
        snapshot = copy.deepcopy(_BRANDING)
    return _to_public(snapshot)


def clear(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Alias for :func:`reset_branding` (test helper)."""
    return reset_branding(*args, **kwargs)


async def get_branding_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return get_branding(*args, **kwargs)


async def set_branding_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return set_branding(*args, **kwargs)


async def update_branding_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return update_branding(*args, **kwargs)


async def reset_branding_async(*args: Any, db: Any = None, session: Any = None, **kwargs: Any) -> Dict[str, Any]:
    return reset_branding(*args, **kwargs)


class BrandingService:
    """Object-oriented facade over the module-level store."""

    def get(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return get_branding(*args, **kwargs)

    def set(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return set_branding(*args, **kwargs)

    def update(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return update_branding(*args, **kwargs)

    def reset(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return reset_branding(*args, **kwargs)

    def clear(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return clear(*args, **kwargs)

    # aliases
    get_branding = get
    fetch_branding = get
    set_branding = set
    save_branding = set
    update_branding = update
    patch_branding = update
    reset_branding = reset


branding_service = BrandingService()
brand_service = branding_service
service = branding_service
manager = branding_service
store = branding_service


def get_service() -> BrandingService:
    return branding_service


def create_service() -> BrandingService:
    return BrandingService()


# Compatibility aliases (function level).
get = get_branding
fetch = get_branding
fetch_branding = get_branding
read = get_branding
read_branding = get_branding
retrieve_branding = get_branding
load_branding = get_branding

set = set_branding
save = set_branding
save_branding = set_branding
put_branding = set_branding
create = set_branding
create_branding = set_branding
store_branding = set_branding

update = update_branding
patch = update_branding
patch_branding = update_branding
edit = update_branding
edit_branding = update_branding
merge_branding = update_branding

reset = reset_branding
restore_defaults = reset_branding
restore_default_branding = reset_branding
clear_branding = reset_branding
delete_branding = reset_branding

__all__ = [
    "DEFAULT_BRANDING",
    "get_branding",
    "set_branding",
    "update_branding",
    "reset_branding",
    "clear",
    "get_branding_async",
    "set_branding_async",
    "update_branding_async",
    "reset_branding_async",
    "BrandingService",
    "branding_service",
    "get_service",
    "create_service",
]
