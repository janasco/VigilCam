import uuid

from fastapi.testclient import TestClient


def test_bulk_cameras_imports():
    import app.services.bulk_cameras  # noqa: F401
    import app.api.bulk_cameras  # noqa: F401
    from app.services.bulk_cameras import (  # noqa: F401
        bulk_delete_cameras,
        bulk_disable_cameras,
        bulk_enable_cameras,
        bulk_set_cameras,
    )
    from app.api.bulk_cameras import router  # noqa: F401
    assert router is not None


def test_bulk_cameras_paths_present():
    from app.main import app

    paths = list(app.openapi()["paths"].keys())
    assert "/api/cameras/bulk-enable" in paths
    assert "/api/cameras/bulk-disable" in paths
    assert "/api/cameras/bulk-delete" in paths


def _unique(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def test_bulk_cameras_enable_disable_flow():
    from app.main import app

    client = TestClient(app)
    cids = []
    try:
        for i in range(2):
            cr = client.post(
                "/api/cameras",
                json={"name": _unique(f"bulkcam{i}"), "kind": "usb", "source": "0"},
            )
            assert cr.status_code == 201, cr.text
            cids.append(cr.json()["id"])

        dis = client.post("/api/cameras/bulk-disable", json={"camera_ids": cids})
        assert dis.status_code == 200, dis.text
        body = dis.json()
        assert body.get("updated_count") == 2
        assert sorted(body.get("updated_camera_ids", [])) == sorted(cids)

        for cid in cids:
            got = client.get(f"/api/cameras/{cid}")
            assert got.status_code == 200, got.text
            assert got.json().get("enabled") is False

        en = client.post("/api/cameras/bulk-enable", json={"camera_ids": cids})
        assert en.status_code == 200, en.text
        assert en.json().get("updated_count") == 2

        for cid in cids:
            got = client.get(f"/api/cameras/{cid}")
            assert got.status_code == 200
            assert got.json().get("enabled") is True

        # underscore alias + ids alias key
        dis2 = client.post("/api/cameras/bulk_disable", json={"ids": cids})
        assert dis2.status_code == 200, dis2.text
        en2 = client.post("/api/cameras/bulk_enable", json={"ids": cids})
        assert en2.status_code == 200, en2.text

        # missing ids -> 400
        bad = client.post("/api/cameras/bulk-enable", json={})
        assert bad.status_code == 400, bad.text
    finally:
        for cid in cids:
            try:
                client.delete(f"/api/cameras/{cid}")
            except Exception:  # noqa: BLE001
                pass


def test_bulk_cameras_delete():
    from app.main import app

    client = TestClient(app)
    cids = []
    for i in range(2):
        cr = client.post(
            "/api/cameras",
            json={"name": _unique(f"bulkdel{i}"), "kind": "usb", "source": "0"},
        )
        assert cr.status_code == 201, cr.text
        cids.append(cr.json()["id"])
    d = client.post("/api/cameras/bulk-delete", json={"camera_ids": cids})
    assert d.status_code == 200, d.text
    assert d.json().get("deleted_count") == 2
    assert sorted(d.json().get("deleted_camera_ids", [])) == sorted(cids)
    for cid in cids:
        got = client.get(f"/api/cameras/{cid}")
        assert got.status_code == 404, got.text


def test_bulk_cameras_service_helpers():
    from app.services import bulk_cameras as svc

    assert svc._coerce_int_list([1, "2", "x", None]) == [1, 2]
    assert svc._coerce_int_list("3,4") == [3, 4]
    assert svc.bulk_enable is svc.bulk_enable_cameras
    assert svc.bulk_disable is svc.bulk_disable_cameras
    assert svc.bulk_delete is svc.bulk_delete_cameras
    assert svc.bulk_set is svc.bulk_set_cameras
