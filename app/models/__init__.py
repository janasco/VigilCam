from app.models.entities import *  # noqa: F401,F403
try:
    from app.models.auth import User, Role  # noqa: F401
except Exception:  # noqa: BLE001
    pass
