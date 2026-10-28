"""Compatibility shim for per-camera activity tracking."""

from app.services import activity as _a

# Re-export everything.
globals().update({k: v for k, v in vars(_a).items() if not k.startswith("__")})
from app.services.activity import *  # noqa: F401,F403
PY