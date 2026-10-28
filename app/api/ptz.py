"""PTZ endpoints: schedules (Wave 26) plus compatibility alias."""

from app.api.ptz_schedules import router  # noqa: F401

# Back-compat: also expose the onvif PTZ router under an alternate name.
try:
    from app.api.onvif_ptz import router as onvif_router  # noqa: F401
except Exception:  # noqa: BLE001
    onvif_router = None

__all__ = ["router", "onvif_router"]
