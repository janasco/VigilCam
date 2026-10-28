import base64

from fastapi.testclient import TestClient

from app.main import app
from app.services import cloud_s3
from app.services import cloud_upload


def test_cloud_s3_import():
    import app.services.cloud_s3  # noqa: F401
    import app.api.cloud_s3  # noqa: F401
    from app.services.cloud_s3 import upload, upload_file, list_providers  # noqa: F401
    from app.services.cloud_s3 import cloud_upload_service  # noqa: F401
    from app.services.cloud_s3 import get_config, build_object_url, choose_upload_method  # noqa: F401


def test_s3_provider_registered():
    assert "s3" in cloud_s3.list_providers()
    assert "s3" in cloud_upload.list_providers()


def test_s3_single_vs_multipart():
    assert cloud_s3.choose_upload_method(100) == "single"
    assert cloud_s3.choose_upload_method(0) == "single"
    assert cloud_s3.choose_upload_method(cloud_s3.SINGLE_UPLOAD_MAX_BYTES + 1) == "multipart"
    assert cloud_s3.MULTIPART_PART_SIZE == 5 * 1024 * 1024


def test_s3_object_url_building():
    url = cloud_s3.build_object_url("https://minio.example.com", "mybucket", "VigilCam/a.txt")
    assert "mybucket" in url and "VigilCam" in url
    virtual = cloud_s3.build_object_url(
        "https://minio.example.com", "mybucket", "a.txt", addressing_style="virtual"
    )
    assert "mybucket" in virtual and "a.txt" in virtual
    default = cloud_s3.build_object_url("", "mybucket", "a.txt", region="us-east-1")
    assert "amazonaws.com" in default and "mybucket" in default


def test_s3_key_normalization():
    assert cloud_s3.normalize_object_key_with_prefix("VigilCam", "a.txt") == "VigilCam/a.txt"
    assert cloud_s3.normalize_object_key_with_prefix("", "a.txt") == "a.txt"
    assert cloud_s3.normalize_addressing_style("virtual") == "virtual"
    assert cloud_s3.normalize_addressing_style("bogus") == "path"
    parsed = cloud_s3.parse_s3_url("s3://mybucket/VigilCam/a.txt")
    assert parsed["bucket"] == "mybucket"
    assert parsed["key"] == "VigilCam/a.txt"


def test_s3_stub_upload_base64(tmp_path):
    b64 = base64.b64encode(b"hello-s3").decode()
    out = cloud_s3.upload(file_base64=b64, filename="s.txt", bucket="mybucket", prefix="VigilCam")
    assert out["provider"] == "s3"
    assert out["success"] is True and out["ok"] is True
    assert out["stub"] is True
    assert out["method"] == "single"
    assert out["bucket"] == "mybucket"
    assert out["key"] == "VigilCam/s.txt"
    assert out["object_url"] and "mybucket" in out["object_url"]
    assert out["staged_path"] is not None


def test_s3_stub_upload_file_path(tmp_path):
    src = tmp_path / "c.txt"
    src.write_text("clip-bytes")
    out = cloud_s3.upload(str(src), bucket="mybucket", prefix="VigilCam/Events")
    assert out["key"] == "VigilCam/Events/c.txt"
    assert out["size"] == len(b"clip-bytes")


def test_s3_stub_upload_s3_url_target(tmp_path):
    src = tmp_path / "d.txt"
    src.write_text("data")
    out = cloud_s3.upload(file_path=str(src), target="s3://otherbucket/prefix/d.txt")
    assert out["bucket"] == "otherbucket"
    assert out["key"] == "prefix/d.txt"


def test_s3_config_defaults():
    cfg = cloud_s3.get_config()
    assert cfg["provider"] == "s3"
    assert "bucket" in cfg and "region" in cfg and "endpoint" in cfg
    assert "configured" in cfg


def test_s3_api_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/cloud/s3/upload" in paths
    assert "/api/cloud/s3/config" in paths
    assert "/api/cloud/s3/providers" in paths


def test_s3_api_upload_and_providers(tmp_path):
    client = TestClient(app)
    r = client.get("/api/cloud/s3/providers")
    assert r.status_code == 200, r.text
    assert "s3" in str(r.json())
    src = tmp_path / "e.txt"
    src.write_text("hello-api")
    r2 = client.post(
        "/api/cloud/s3/upload",
        json={"file_path": str(src), "bucket": "mybucket", "prefix": "VigilCam"},
    )
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["provider"] == "s3"
    assert body["key"] == "VigilCam/e.txt"
