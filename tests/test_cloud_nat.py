import base64
import os

from fastapi.testclient import TestClient

from app.main import app
from app.services import cloud_onedrive, nat_status
from app.services import cloud_upload


def test_imports():
    import app.services.cloud_onedrive  # noqa: F401
    import app.services.nat_status  # noqa: F401
    import app.api.nat_status  # noqa: F401
    from app.services.cloud_onedrive import upload, list_providers  # noqa: F401
    from app.services.nat_status import get_nat_status, detect_upnp_availability, recommend  # noqa: F401


def test_onedrive_provider_registered():
    assert "onedrive" in cloud_onedrive.list_providers()
    # Import-time registration extends the main gateway lists.
    assert "onedrive" in cloud_upload.list_providers()


def test_onedrive_simple_vs_resumable():
    assert cloud_onedrive.choose_upload_method(100) == "simple"
    assert cloud_onedrive.choose_upload_method(0) == "simple"
    assert cloud_onedrive.choose_upload_method(cloud_onedrive.SIMPLE_UPLOAD_MAX_BYTES + 1) == "resumable"
    small = cloud_onedrive.build_simple_upload_url("VigilCam/a.txt")
    assert "graph.microsoft.com" in small and "createUploadSession" not in small
    large = cloud_onedrive.build_resumable_session_url("VigilCam/b.txt")
    assert "createUploadSession" in large


def test_onedrive_conflict_behavior():
    assert cloud_onedrive.normalize_conflict_behavior("overwrite") == "replace"
    assert cloud_onedrive.normalize_conflict_behavior("bogus") == "rename"
    assert cloud_onedrive.normalize_conflict_behavior(None) == "rename"


def test_onedrive_stub_upload_base64(tmp_path):
    b64 = base64.b64encode(b"hello-onedrive").decode()
    out = cloud_onedrive.upload(file_base64=b64, filename="od.txt", folder="VigilCam")
    assert out["provider"] == "onedrive"
    assert out["success"] is True and out["ok"] is True
    assert out["stub"] is True
    assert out["method"] == "simple"
    assert out["remote_path"] == "VigilCam/od.txt"
    assert out["conflict_behavior"] in ("fail", "replace", "rename")
    assert "graph.microsoft.com" in out["upload_url"]


def test_onedrive_stub_upload_file_path(tmp_path):
    src = tmp_path / "c.txt"
    src.write_text("clip-bytes")
    out = cloud_onedrive.upload(str(src), folder="VigilCam/Events")
    assert out["remote_path"] == "VigilCam/Events/c.txt"
    assert out["size"] == len(b"clip-bytes")


def test_nat_recommendation_logic():
    fwd = nat_status.recommend(True)
    assert fwd["recommendation"] == "port-forward"
    assert fwd["use_relay"] is False
    rel = nat_status.recommend(False)
    assert rel["recommendation"] == "relay"
    assert rel["use_relay"] is True


def test_nat_status_forced(monkeypatch):
    monkeypatch.setenv("VIGILCAM_FORCE_UPNP", "0")
    off = nat_status.get_nat_status(include_discovery=True)
    assert off["upnp_available"] is False
    assert off["recommendation"] == "relay"
    monkeypatch.setenv("VIGILCAM_FORCE_UPNP", "1")
    on = nat_status.get_nat_status(include_discovery=True)
    assert on["upnp_available"] is True
    assert on["recommendation"] == "port-forward"


def test_nat_status_no_discovery():
    s = nat_status.get_nat_status(include_discovery=False)
    assert s["ok"] is True
    assert "recommendation" in s and "reason" in s
    assert s["pmp_available"] is False


def test_nat_api_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/nat/status" in paths


def test_nat_api_status(monkeypatch):
    monkeypatch.setenv("VIGILCAM_FORCE_UPNP", "0")
    client = TestClient(app)
    r = client.get("/api/nat/status", params={"force_upnp": "false"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["upnp_available"] is False
    assert data["recommendation"] == "relay"
    assert "reason" in data


def test_nat_api_upnp_and_check(monkeypatch):
    monkeypatch.setenv("VIGILCAM_FORCE_UPNP", "1")
    client = TestClient(app)
    r = client.get("/api/nat/upnp", params={"force_upnp": "true"})
    assert r.status_code == 200, r.text
    assert r.json()["available"] is True
    r2 = client.post("/api/nat/check", json={"force_upnp": True, "include_discovery": True})
    assert r2.status_code == 200, r2.text
    assert r2.json()["recommendation"] == "port-forward"
    # Env cleanup safety: ensure override keys do not leak as failures.
    os.environ.pop("VIGILCAM_FORCE_UPNP", None)
