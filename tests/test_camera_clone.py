import uuid

from fastapi.testclient import TestClient

from app.main import app
from app.api.camera_clone import router as camera_clone_router  # noqa: F401
from app.services import camera_clone as camera_clone_service_mod  # noqa: F401
from app.services.camera_clone import (  # noqa: F401
    build_clone_data,
    build_clone_name,
    clone_camera,
)


def _make_camera(client: TestClient, **overrides):
    name = overrides.pop("name", "clone_src_" + uuid.uuid4().hex[:6])
    payload = {"name": name, "kind": "ip", "source": "rtsp://10.0.0.1/" + uuid.uuid4().hex[:6]}
    payload.update(overrides)
    r = client.post("/api/cameras", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def test_camera_clone_imports():
    import app.api.camera_clone  # noqa: F401
    import app.services.camera_clone  # noqa: F401
    from app.api.camera_clone import router  # noqa: F401
    from app.services.camera_clone import clone_camera, build_clone_data  # noqa: F401


def test_camera_clone_path_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/cameras/{camera_id}/clone" in paths


def test_build_clone_name():
    assert build_clone_name("Front", None) == "Front (copy)"
    assert build_clone_name("Front", "Back") == "Back"
    assert build_clone_name("Front", "  ") == "Front (copy)"


def test_camera_clone_api():
    client = TestClient(app)
    src = _make_camera(client)
    src_id = src["id"]

    # Default name -> "<src> (copy)".
    r = client.post(f"/api/cameras/{src_id}/clone", json={})
    assert r.status_code == 201, r.text
    cloned = r.json()
    assert cloned["id"] != src_id
    assert cloned["name"] == f"{src['name']} (copy)"
    assert cloned["source"] == src["source"]
    assert cloned["kind"] == src["kind"]
    clone_id = cloned["id"]

    # Explicit name override.
    r2 = client.post(f"/api/cameras/{src_id}/clone", json={"name": "custom-clone"})
    assert r2.status_code == 201, r2.text
    assert r2.json()["name"] == "custom-clone"
    clone2_id = r2.json()["id"]
    assert clone2_id not in (src_id, clone_id)

    # Unknown source -> 404.
    r404 = client.post("/api/cameras/999999/clone", json={})
    assert r404.status_code == 404

    # Cleanup.
    for cid in (clone_id, clone2_id, src_id):
        client.delete(f"/api/cameras/{cid}")


def test_camera_clone_service_copies_fields():
    client = TestClient(app)
    src = _make_camera(
        client,
        motion_zones=[{"points": [[0, 0]], "mode": "include"}],
        group="g1",
    )
    src_id = src["id"]
    try:
        r = client.post(f"/api/cameras/{src_id}/clone")
        assert r.status_code == 201, r.text
        cloned = r.json()
        assert cloned["motion_zones"] == src["motion_zones"]
        assert cloned["group"] == src["group"]
        clone_id = cloned["id"]
    finally:
        # fetch clone id even if asserts above fail partially
        clone_id = locals().get("clone_id")
        if clone_id is not None:
            client.delete(f"/api/cameras/{clone_id}")
        client.delete(f"/api/cameras/{src_id}")
