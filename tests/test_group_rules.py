import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import group_rules as group_rules_api
from app.services import group_rules as group_rules_service


@pytest.fixture()
def isolated_rules():
    before = group_rules_service.list_rules()
    yield
    # Remove any rules created during the test; restore nothing else.
    after = group_rules_service.list_rules()
    before_ids = {r["id"] for r in before}
    for rule in after:
        if rule["id"] not in before_ids:
            try:
                group_rules_service.delete_rule(rule["id"])
            except Exception:  # noqa: BLE001
                pass


def test_imports():
    import app.services.group_rules  # noqa: F401
    import app.api.group_rules  # noqa: F401
    from app.services.group_rules import (  # noqa: F401
        GroupRule,
        apply_rules,
        create_rule,
        delete_rule,
        evaluate_camera,
        list_rules,
        match_camera,
        preview_cameras,
        rule_matches_camera,
    )
    from app.api.group_rules import router  # noqa: F401
    assert router is not None


def test_service_crud_and_matching(isolated_rules):
    suffix = uuid.uuid4().hex[:6]
    rule = group_rules_service.create_rule(
        {"name": f"rule_{suffix}", "group": f"Lobby_{suffix}", "field": "name", "operator": "contains", "value": "lobby"}
    )
    assert rule["id"]
    assert rule["group"] == f"Lobby_{suffix}"

    assert any(r["id"] == rule["id"] for r in group_rules_service.list_rules())
    assert group_rules_service.get_rule(rule["id"])["id"] == rule["id"]

    assert group_rules_service.rule_matches_camera(rule, {"name": "Lobby Cam 1"}) is True
    assert group_rules_service.rule_matches_camera(rule, {"name": "Parking Lot"}) is False
    # Disabled rules never match.
    group_rules_service.update_rule(rule["id"], {"enabled": False})
    assert group_rules_service.rule_matches_camera(group_rules_service.get_rule(rule["id"]), {"name": "lobby x"}) is False
    group_rules_service.update_rule(rule["id"], {"enabled": True})

    matched = group_rules_service.match_camera({"name": "main lobby"})
    assert matched is not None and matched["id"] == rule["id"]
    assert group_rules_service.evaluate_camera({"name": "main lobby"}) == f"Lobby_{suffix}"
    assert group_rules_service.evaluate_camera({"name": "roof"}) is None

    # Priority ordering: lower number wins.
    rule2 = group_rules_service.create_rule(
        {
            "name": f"other_{suffix}",
            "group": f"Other_{suffix}",
            "field": "name",
            "operator": "contains",
            "value": "lobby",
            "priority": 1,
        }
    )
    assert group_rules_service.evaluate_camera({"name": "lobby"}) == f"Other_{suffix}"

    # Operators.
    assert group_rules_service.normalize_operator("prefix") == "startswith"
    assert group_rules_service.normalize_field("camera_name") == "name"
    rx = group_rules_service.create_rule(
        {"group": f"Rx_{suffix}", "field": "name", "operator": "regex", "value": r"^cam-\d+$"}
    )
    assert group_rules_service.rule_matches_camera(rx, {"name": "cam-42"}) is True
    assert group_rules_service.rule_matches_camera(rx, {"name": "cam-abc"}) is False

    # Validation.
    with pytest.raises(ValueError):
        group_rules_service.create_rule({"group": "x", "field": "bogus-field", "value": "y"})
    with pytest.raises(ValueError):
        group_rules_service.create_rule({"group": "x", "field": "name", "operator": "bogus-op", "value": "y"})
    with pytest.raises(ValueError):
        group_rules_service.create_rule({"group": "", "field": "name", "value": "y"})

    assert group_rules_service.delete_rule(rule["id"]) is True
    assert group_rules_service.delete_rule(rule2["id"]) is True
    assert group_rules_service.delete_rule(rx["id"]) is True
    assert group_rules_service.get_rule(rule["id"]) is None


def test_router_crud_preview():
    app = FastAPI()
    app.include_router(group_rules_api.router)
    client = TestClient(app)

    before = {r["id"] for r in group_rules_service.list_rules()}
    suffix = uuid.uuid4().hex[:6]
    created_ids: list[str] = []
    try:
        r = client.post(
            "/api/group-rules",
            json={"name": f"api_{suffix}", "group": f"ApiGroup_{suffix}", "field": "kind", "operator": "equals", "value": "usb"},
        )
        assert r.status_code == 201, r.text
        rid = r.json()["id"]
        created_ids.append(rid)

        assert client.get("/api/group-rules").status_code == 200
        assert client.get(f"/api/group-rules/{rid}").status_code == 200
        assert client.get("/api/group-rules/does-not-exist").status_code == 404

        p = client.patch(f"/api/group-rules/{rid}", json={"priority": 5})
        assert p.status_code == 200
        assert p.json()["priority"] == 5

        # Alias paths resolve too.
        assert client.get("/api/group_rules").status_code == 200
        assert client.get("/api/groups/rules").status_code == 200

        # Inline preview without touching the DB.
        pv = client.post(
            "/api/group-rules/preview",
            json={"cameras": [{"id": 1, "name": "x", "kind": "usb"}, {"id": 2, "name": "y", "kind": "ip"}]},
        )
        assert pv.status_code == 200, pv.text
        body = pv.json()
        previews = body["preview"] if isinstance(body, dict) else body
        by_id = {item["camera_id"]: item for item in previews}
        assert by_id[1]["target_group"] == f"ApiGroup_{suffix}"
        assert by_id[2]["target_group"] is None

        # Bad payload -> 400.
        bad = client.post("/api/group-rules", json={"group": "g", "field": "nope", "value": "v"})
        assert bad.status_code == 400
    finally:
        for rid in created_ids:
            try:
                group_rules_service.delete_rule(rid)
            except Exception:  # noqa: BLE001
                pass
        # Safety: remove anything else the test may have left behind.
        for rule in group_rules_service.list_rules():
            if rule["id"] not in before:
                try:
                    group_rules_service.delete_rule(rule["id"])
                except Exception:  # noqa: BLE001
                    pass


def test_apply_assigns_group_via_main_app():
    from app.main import app

    client = TestClient(app)
    suffix = uuid.uuid4().hex[:6]
    target_group = f"AutoGroup_{suffix}"
    cam_name = f"autocam_{suffix}_lobby"
    camera_id = None
    group_id = None
    before_rules = {r["id"] for r in group_rules_service.list_rules()}
    try:
        rule = group_rules_service.create_rule(
            {"name": f"apply_{suffix}", "group": target_group, "field": "name", "operator": "contains", "value": "lobby"}
        )
        cr = client.post("/api/cameras", json={"name": cam_name, "kind": "usb", "source": "0"})
        assert cr.status_code == 201, cr.text
        camera_id = cr.json()["id"]

        ar = client.post("/api/group-rules/apply")
        assert ar.status_code == 200, ar.text
        assert ar.json().get("assigned", 0) >= 1

        gr = client.get("/api/groups")
        assert gr.status_code == 200
        match = [g for g in gr.json() if g.get("name") == target_group]
        assert match, f"expected auto-created group {target_group}"
        group_id = match[0]["id"]

        # Camera should now reference the group.
        cc = client.get(f"/api/cameras/{camera_id}")
        assert cc.status_code == 200, cc.text
        assert cc.json().get("group_id") == group_id
    finally:
        if camera_id is not None:
            client.delete(f"/api/cameras/{camera_id}")
        if group_id is not None:
            client.delete(f"/api/groups/{group_id}")
        for rule in group_rules_service.list_rules():
            if rule["id"] not in before_rules:
                try:
                    group_rules_service.delete_rule(rule["id"])
                except Exception:  # noqa: BLE001
                    pass


def test_paths_present_in_main_app():
    from app.main import app

    paths = list(app.openapi()["paths"].keys())
    assert "/api/group-rules" in paths
    assert "/api/group-rules/apply" in paths
    assert "/api/group-rules/preview" in paths
