from fastapi.testclient import TestClient

from app.main import app
from app.services import subaccounts


def test_subaccounts_import():
    import app.services.subaccounts  # noqa: F401
    import app.api.subaccounts  # noqa: F401
    from app.services.subaccounts import create_subaccount, subaccounts_service  # noqa: F401
    from app.api.subaccounts import router  # noqa: F401


def test_subaccounts_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/subaccounts" in paths


def test_subaccounts_service_basic():
    subaccounts.clear()
    created = subaccounts.create_subaccount(username="child-1", parent_id="parent-1", camera_ids=[1, 2])
    assert isinstance(created, dict)
    assert created.get("username") == "child-1"
    assert created.get("parent_id") == "parent-1"
    assert created.get("status") == "active"
    assert sorted(created.get("camera_ids", [])) == [1, 2]
    sub_id = created.get("id")
    assert sub_id

    fetched = subaccounts.get_subaccount(sub_id)
    assert fetched is not None
    assert fetched.get("username") == "child-1"

    listed = subaccounts.list_subaccounts()
    assert len(listed) == 1

    scoped = subaccounts.list_subaccounts(parent_id="parent-1")
    assert len(scoped) == 1
    assert subaccounts.list_subaccounts(parent_id="other-parent") == []

    # camera scoping
    assert subaccounts.can_access_camera(sub_id, 1) is True
    assert subaccounts.can_access_camera(sub_id, 99) is False

    subaccounts.grant_camera_access(sub_id, 99, "view")
    assert subaccounts.can_access_camera(sub_id, 99) is True

    subaccounts.revoke_camera_access(sub_id, 99)
    assert subaccounts.can_access_camera(sub_id, 99) is False

    updated = subaccounts.update_subaccount(sub_id, camera_ids=[5])
    assert updated is not None
    assert updated.get("camera_ids") == [5]

    assert subaccounts.delete_subaccount(sub_id) is True
    assert subaccounts.get_subaccount(sub_id) is None


def test_subaccounts_parent_child_linking():
    subaccounts.clear()
    parent = subaccounts.create_subaccount(username="parent-user", camera_ids=[])
    child = subaccounts.create_subaccount(username="child-user", parent_id=parent["id"], camera_ids=[3])
    assert child.get("parent_id") == parent["id"]
    assert subaccounts.is_child_of(child["id"], parent["id"]) is True
    assert subaccounts.is_child_of(child["id"], "someone-else") is False
    assert child["id"] in subaccounts.list_children_of(parent["id"])


def test_subaccounts_endpoint():
    subaccounts.clear()
    client = TestClient(app)

    r = client.post("/api/subaccounts", json={"username": "http-child", "parent_id": "http-parent", "camera_ids": [1, 2]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("ok") is True
    sub_id = body.get("id") or body.get("subaccount_id")
    assert sub_id
    assert sorted(body.get("camera_ids", [])) == [1, 2]

    r2 = client.get("/api/subaccounts")
    assert r2.status_code == 200, r2.text
    assert isinstance(r2.json(), list)
    assert len(r2.json()) == 1

    r3 = client.get(f"/api/subaccounts/{sub_id}")
    assert r3.status_code == 200, r3.text
    assert r3.json().get("username") == "http-child"

    # camera scoping endpoints
    r4 = client.get(f"/api/subaccounts/{sub_id}/cameras")
    assert r4.status_code == 200, r4.text
    assert sorted(r4.json().get("camera_ids", [])) == [1, 2]

    r5 = client.post(f"/api/subaccounts/{sub_id}/cameras", json={"camera_id": 7, "permission": "view"})
    assert r5.status_code == 200, r5.text
    assert 7 in r5.json().get("camera_ids", [])

    r6 = client.delete(f"/api/subaccounts/{sub_id}/cameras/7")
    assert r6.status_code == 200, r6.text
    assert 7 not in r6.json().get("camera_ids", [])

    r7 = client.delete(f"/api/subaccounts/{sub_id}")
    assert r7.status_code == 200, r7.text
    assert r7.json().get("revoked") is True

    r8 = client.get(f"/api/subaccounts/{sub_id}")
    assert r8.status_code == 404, r8.text


def test_subaccounts_status():
    subaccounts.clear()
    client = TestClient(app)
    r = client.get("/api/subaccounts/status")
    assert r.status_code == 200, r.text
    assert r.json().get("ok") is True
