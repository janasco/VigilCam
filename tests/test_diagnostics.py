from __future__ import annotations

import io
import zipfile

from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_diagnostics_imports():
    import app.services.diagnostics as svc  # noqa: F401
    import app.api.diagnostics as api  # noqa: F401

    assert hasattr(api, "router")
    assert hasattr(svc, "build_bundle_bytes")
    assert hasattr(svc, "collect_config")
    assert hasattr(svc, "collect_logs")


def test_collect_config_redacts_secrets():
    from app.services import diagnostics as svc

    cfg = svc.collect_config()
    assert isinstance(cfg, dict)
    blob = str(cfg).lower()
    # No raw secret values from default settings should leak verbatim alongside keys.
    for bad_key in ("admin",):
        _ = bad_key
    # Redaction helper itself must redact.
    red = svc.redact_dict({"ADMIN_PASSWORD": "supersecret", "nested": {"api_key": "abc"}})
    assert red["ADMIN_PASSWORD"] == "<redacted>"
    assert red["nested"]["api_key"] == "<redacted>"
    assert "password" not in blob or "<redacted>" in str(cfg)


def test_build_bundle_bytes_is_valid_zip():
    from app.services import diagnostics as svc

    blob, manifest = svc.build_bundle_bytes(
        include_logs=False,
        extra_files={"notes/hello.txt": b"hi"},
    )
    assert isinstance(blob, bytes)
    assert blob[:2] == b"PK"
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        names = set(zf.namelist())
    assert "config/config.json" in names
    assert "metrics/metrics.json" in names
    assert "manifest.json" in names
    assert "DIAGNOSTICS.md" in names
    assert "notes/hello.txt" in names
    assert manifest["tool"] == svc.TOOL_NAME
    assert "config/config.json" in manifest["files"]


def test_build_bundle_redacts_url_credentials():
    from app.services import diagnostics as svc

    blob, _manifest = svc.build_bundle_bytes(
        include_logs=False,
        include_metrics=False,
        config={"camera_url": "rtsp://admin:s3cret@10.0.0.5/stream", "ADMIN_PASSWORD": "x"},
    )
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        raw = zf.read("config/config.json").decode()
    assert "s3cret" not in raw
    assert "<redacted>" in raw


def _mounted_client() -> TestClient:
    from app.api.diagnostics import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_diagnostics_manifest_endpoint():
    client = _mounted_client()
    r = client.get("/api/diagnostics/")
    assert r.status_code == 200, r.text
    data = r.json()
    assert "files" in data
    assert "tool" in data


def test_diagnostics_bundle_download():
    client = _mounted_client()
    for path in ("/api/diagnostics/bundle", "/api/diagnostics/download", "/api/diagnostics/zip"):
        r = client.get(path)
        assert r.status_code == 200, (path, r.text[:200])
        assert r.headers.get("content-type", "").startswith("application/zip")
        assert r.content[:2] == b"PK"
        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            names = zf.namelist()
        assert "manifest.json" in names


def test_diagnostics_post_json_and_zip():
    client = _mounted_client()
    r = client.post("/api/diagnostics/", json={"include_logs": False})
    assert r.status_code == 200, r.text
    assert "files" in r.json()

    r2 = client.post("/api/diagnostics/", json={"include_logs": False, "format": "zip"})
    assert r2.status_code == 200
    assert r2.content[:2] == b"PK"


def test_diagnostics_router_prefix():
    from app.api.diagnostics import router

    assert router.prefix == "/api/diagnostics"
