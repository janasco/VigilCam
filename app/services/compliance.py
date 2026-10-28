from app.services.evidence_pack import *  # noqa: F401,F403
from app.services.security_compliance import *  # noqa: F401,F403
import app.services.evidence_pack as _ep
import app.services.security_compliance as _sc

# expose both
for _name in ("check_cameras", "covered_status"):
    if hasattr(_sc, _name):
        globals()[_name] = getattr(_sc, _name)
for _name in ("build_pack", "build", "generate", "generate_pack", "create", "create_pack", "collect", "redact"):
    if hasattr(_ep, _name):
        globals()[_name] = getattr(_ep, _name)
