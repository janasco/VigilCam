import uuid

from fastapi.testclient import TestClient


def test_group_bulk_imports():
    import app.services.group_bulk  # noqa: F401
    import app.api.group_bulk  # noqa: F401
    from app.services.group_bulk import (  # noqa: F401
        bulk_assign_cameras,
        bulk_create_groups,
        bulk_delete_groups,
        bulk_disable_group_cameras,
        bulk_enable_group_cameras,
        bulk_set_group_cameras,
        bulk_unassign_cameras,
        get_group_cameras,
        resolve_groups,
    )
    from app.api.group_bulk import router  # noqa: F401
    assert router is not None


def test_group_bulk_paths_present():
    from app.main import app

    paths = list(app.openapi()["paths"].keys())
    assert "/api/groups/bulk-enable" in paths
    assert "/api/groups/bulk-disable" in paths
    assert "/api/groups/bulk-assign" in paths
    assert "/api/groups/bulk-unassign" in paths
    assert "/api/groups/bulk-delete" in paths


def _unique(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def test_group_bulk_enable_disable_flow():
    from app.main import app

    client = TestClient(app)
    gname = _unique("gb")
    r = client.post("/api/groups", json={"name": gname})
    assert r.status_code == 201, r.text
    gid = r.json()["id"]
    cids = []
    try:
        for i in range(2):
            cr = client.post(
                "/api/cameras",
                json={"name": _unique(f"gbcam{i}"), "kind": "usb", "source": "0"},
            )
            assert cr.status_code == 201, cr.text
            cids.append(cr.json()["id"])
        ar = client.post("/api/groups/bulk-assign", json={"camera_ids": cids, "group_id": gid})
        assert ar.status_code == 200, ar.text
        assert ar.json().get("updated_count") == 2

        dis = client.post("/api/groups/bulk-disable", json={"group_ids": [gid]})
        assert dis.status_code == 200, dis.text
        body = dis.json()
        assert body.get("updated_count") == 2
        assert sorted(body.get("updated_camera_ids", [])) == sorted(cids)

        for cid in cids:
            got = client.get(f"/api/cameras/{cid}")
            assert got.status_code == 200, got.text
            assert got.json().get("enabled") is False

        en = client.post("/api/groups/bulk-enable", json={"group_ids": [gid]})
        assert en.status_code == 200, en.text
        assert en.json().get("updated_count") == 2

        for cid in cids:
            got = client.get(f"/api/cameras/{cid}")
            assert got.status_code == 200
            assert got.json().get("enabled") is True

        # underscore alias also works
        dis2 = client.post("/api/groups/bulk_disable", json={"group_ids": [gid]})
        assert dis2.status_code == 200, dis2.text
        en2 = client.post("/api/groups/bulk_enable", json={"group_ids": [gid]})
        assert en2.status_code == 200, en2.text

        # unassign via bulk
        un = client.post("/api/groups/bulk-unassign", json={"camera_ids": cids})
        assert un.status_code == 200, un.text
        assert un.json().get("updated_count") == 2
    finally:
        for cid in cids:
            try:
                client.delete(f"/api/cameras/{cid}")
            except Exception:  # noqa: BLE001
                pass
        try:
            client.delete(f"/api/groups/{gid}")
        except Exception:  # noqa: BLE001
            pass


def test_group_bulk_delete_and_create():
    from app.main import app

    client = TestClient(app)
    names = [_unique("gbdel"), _unique("gbdel")]
    r = client.post("/api/groups/bulk-create", json={"groups": [{"name": n} for n in names]})
    assert r.status_code == 200, r.text
    created_ids = r.json().get("created_group_ids", [])
    assert len(created_ids) == 2
    try:
        d = client.post("/api/groups/bulk-delete", json={"group_ids": created_ids})
        assert d.status_code == 200, d.text
        assert d.json().get("deleted_count") == 2
    finally:
        for gid in created_ids:
            try:
                client.delete(f"/api/groups/{gid}")
            except Exception:  # noqa: BLE001
                pass


def test_group_bulk_service_helpers():
    from app.services import group_bulk as svc

    assert svc._coerce_int_list([1, "2", "x", None]) == [1, 2]
    assert svc._coerce_int_list("3,4") == [3, 4]
    assert svc.bulk_enable is svc.bulk_enable_group_cameras
    assert svc.bulk_disable is svc.bulk_disable_group_cameras
    assert svc.bulk_assign is svc.bulk_assign_cameras
