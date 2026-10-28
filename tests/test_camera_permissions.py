from fastapi.testclient import TestClient

from app.main import app
from app.models.entities import CameraPermission
from app.models import CameraPermission as CameraPermissionAlias
from app.models.auth import CameraPermission as AuthCameraPermission
from app.schemas import CameraPermissionRead, CameraPermissionCreate


def test_camera_permission_imports():
    import app.api.permissions  # noqa: F401
    import app.services.camera_permissions  # noqa: F401
    assert CameraPermission.__tablename__ == "camera_permissions"


def test_camera_permission_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/cameras/{camera_id}/permissions" in paths
    assert "/api/cameras/{camera_id}/permissions/{user_id}" in paths


def test_camera_permission_crud(tmp_path):
    client = TestClient(app)
    user = client.post("/api/auth/register", json={"username": "perm_" + __import__("uuid").uuid4().hex[:6], "password": "secret123"}).json()
    uid = user["id"]
    cam = client.post("/api/cameras", json={"name": "pcam", "kind": "ip", "source": "rtsp://10.0.0.1/x"}).json()
    cid = cam["id"]

    r = client.post(f"/api/cameras/{cid}/permissions", json={"user_id": uid, "permission": "view"})
    assert r.status_code in (200, 201), r.text
    body = r.json()
    assert body["user_id"] == uid
    assert body["camera_id"] == cid
    assert body["permission"] == "view"

    # upsert
    r2 = client.post(f"/api/cameras/{cid}/permissions", json={"user_id": uid, "permission": "manage"})
    assert r2.status_code in (200, 201), r2.text
    assert r2.json()["permission"] == "manage"

    lst = client.get(f"/api/cameras/{cid}/permissions")
    assert lst.status_code == 200
    assert len(lst.json()) == 1
    assert lst.json()[0]["permission"] == "manage"

    delr = client.delete(f"/api/cameras/{cid}/permissions/{uid}")
    assert delr.status_code in (200, 204), delr.text

    missing = client.delete(f"/api/cameras/{cid}/permissions/{uid}")
    assert missing.status_code == 404

    assert client.get(f"/api/cameras/{cid}/permissions").json() == []