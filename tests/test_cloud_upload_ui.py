from fastapi.testclient import TestClient

from app.main import app


def test_cloud_upload_ui_present():
    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200
    html = r.text
    for token in [
        'id="cloudUploadForm"',
        'id="cloudUploadStatus"',
        'id="cloudProvider"',
        'name="provider"',
        'name="file_path"',
        'name="target"',
        'name="filename"',
        'name="host"',
        'name="port"',
        'name="username"',
        'name="password"',
        'name="remote_dir"',
        'id="cloudFileInput"',
        'id="cloudFtpFields"',
        'id="cloudProvidersList"',
        'loadCloudProviders',
        '/api/cloud/providers',
        '/api/cloud/upload',
    ]:
        assert token in html, token


def test_cloud_upload_ui_provider_options():
    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200
    html = r.text
    assert "Local path copy" in html or "local" in html
    assert "FTP upload" in html or "ftp" in html


def test_cloud_upload_ui_js_handles_response():
    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200
    html = r.text
    assert "Uploaded:" in html
    assert "FileReader" in html
    assert "file_base64" in html