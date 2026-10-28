"""Compatibility shim: activity may be tracked by motion workers too."""

from app.workers.motion_worker import *  # noqa: F401,F403
from app.services.activity import *  # noqa: F401,F403
PY