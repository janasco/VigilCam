import uuid

from fastapi.testclient import TestClient

from app.main import app


def test_import_groups():
    import app.main as _appmain  # noqa: F401
    from app.api import groups  # noqa: F401
    paths = list(app.openapi()["paths"].keys())
    assert "/api/groups" in paths
    assert "/api/cameras/{camera_id}/group" in paths


def test_group_crud_and_camera_assignment():
    client = TestClient(app)
    name = "cg_" + uuid.uuid4().hex[:8]
    r = client.post("/api/groups", json={"name": name, "description": "d"})
    assert r.status_code == 201, r.text
    gid = r.json()["id"]
    try:
        assert client.get("/api/groups").status_code == 200
        assert client.get(f"/api/groups/{gid}").status_code == 200
        cr = client.post("/api/cameras", json={"name": "cam_" + uuid.uuid4().hex[:6], "kind": "usb", "source": "0"})
        assert cr.status_code == 201
        cid = cr.json()["id"]
        ar = client.post(f"/api/cameras/{cid}/group", json={"group_id": gid})
        assert ar.status_code == 200
        assert ar.json().get("group_id") == gid
        gr = client.get(f"/api/groups/{gid}/cameras")
        assert gr.status_code == 200
        assert any(c.get("id") == cid for c in gr.json())
        ur = client.delete(f"/api/cameras/{cid}/group")
        assert ur.status_code == 200
        client.delete(f"/api/cameras/{cid}")
    finally:
        client.delete(f"/api/groups/{gid}")
