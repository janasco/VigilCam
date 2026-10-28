import base64
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient

from app.main import app
from app.services import cloud_upload


def test_cloud_upload_import():
    import app.services.cloud_upload  # noqa: F401
    import app.api.cloud_upload  # noqa: F401
    from app.services.cloud_upload import upload, upload_file, list_providers  # noqa: F401
    from app.services.cloud_upload import cloud_upload_service  # noqa: F401


def test_cloud_upload_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/cloud/upload" in paths
    assert "/api/cloud/providers" in paths


def test_cloud_upload_local_copy(tmp_path):
    client = TestClient(app)
    src = tmp_path / "a.txt"
    src.write_text("hello")
    dst = tmp_path / "out"
    r = client.post("/api/cloud/upload", json={"file_path": str(src), "provider": "local", "target": str(dst)})
    assert r.status_code == 200, r.text
    assert (dst / "a.txt").read_text() == "hello"


def test_cloud_upload_base64(tmp_path):
    client = TestClient(app)
    b64 = base64.b64encode(b"world").decode()
    dst = tmp_path / "out2"
    r = client.post("/api/cloud/upload", json={"file_base64": b64, "filename": "b.txt", "provider": "local", "target": str(dst)})
    assert r.status_code == 200, r.text
    assert (dst / "b.txt").read_text() == "world"


def test_cloud_upload_providers():
    client = TestClient(app)
    r = client.get("/api/cloud/providers")
    assert r.status_code == 200
    data = r.json()
    providers = data.get("providers", data) if isinstance(data, dict) else data
    names = [p.get("name") if isinstance(p, dict) else p for p in providers]
    assert "local" in names
    assert "ftp" in names


def test_cloud_upload_ftp_mocked(tmp_path):
    client = TestClient(app)
    src = tmp_path / "a.txt"
    src.write_text("hello")
    mock_ftp = MagicMock()
    with patch("app.services.cloud_upload.ftplib.FTP", return_value=mock_ftp):
        r = client.post("/api/cloud/upload", json={"file_path": str(src), "provider": "ftp", "target": "ftp://u:p@h.example.com/up"})
        assert r.status_code == 200, r.text
        assert mock_ftp.storbinary.called
