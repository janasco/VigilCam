from fastapi.testclient import TestClient

from app.main import app
from app.services import dual_nic_hardening
from app.services.dual_nic_hardening import DualNicHardeningService


def test_dual_nic_hardening_import():
    import app.api.dual_nic_hardening  # noqa: F401
    import app.services.dual_nic_hardening  # noqa: F401


def test_dual_nic_hardening_service_basic():
    svc = DualNicHardeningService()
    st = svc.status()
    assert isinstance(st, dict)
    assert "hardened" in st
    applied = svc.apply(management_interface="eth0", camera_interface="eth1")
    assert applied.get("ok") is True
    assert applied.get("status") == "hardened"
    assert applied.get("hardened") is True


def test_dual_nic_hardening_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/dual-nic-hardening" in paths or "/api/dual_nic_hardening" in paths
    assert "/api/dual-nic-hardening/status" in paths or "/api/dual_nic_hardening/status" in paths


def test_dual_nic_hardening_api_apply():
    client = TestClient(app)
    r = client.post("/api/dual-nic-hardening/apply", json={"management_interface": "eth0", "camera_interface": "eth1"})
    assert r.status_code == 200
    data = r.json()
    assert data.get("ok") is True
    assert data.get("status") == "hardened"