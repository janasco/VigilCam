from app.services.evidence_pack import *  # noqa: F401,F403
import app.services.evidence_pack as _ep

# Common class/instance/function aliases
for _name in ("build_pack", "build", "generate", "generate_pack", "create", "create_pack", "collect", "redact", "render_report", "findings", "main"):
    if hasattr(_ep, _name):
        globals()[_name] = getattr(_ep, _name)

class EvidencePackService:
    def build_pack(self, *a, **k):
        return _ep.build_pack(*a, **k)
    def build(self, *a, **k):
        return _ep.build_pack(*a, **k)
    def generate(self, *a, **k):
        return _ep.build_pack(*a, **k)
    def create(self, *a, **k):
        return _ep.build_pack(*a, **k)
    def collect(self, *a, **k):
        return _ep.collect(*a, **k)

class ComplianceEvidencePackService(EvidencePackService):
    pass

class ComplianceEvidenceService(EvidencePackService):
    pass

evidence_pack_service = EvidencePackService()
compliance_evidence_pack_service = ComplianceEvidencePackService()
compliance_evidence_service = ComplianceEvidenceService()
service = evidence_pack_service

# module-level aliases for fetcher etc.
Fetcher = getattr(_ep, "Fetcher", None)
TOOL_VERSION = getattr(_ep, "TOOL_VERSION", "1.0")
COLLECT = getattr(_ep, "COLLECT", [])
CONTROL_MAP = getattr(_ep, "CONTROL_MAP", [])
