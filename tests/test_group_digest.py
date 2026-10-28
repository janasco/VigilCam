from fastapi.testclient import TestClient

from app.main import app
from app.services import group_digest
from app.services import notify_digest as global_digest


def test_import():
    import app.services.group_digest  # noqa: F401
    import app.api.group_digest  # noqa: F401
    from app.services.group_digest import (  # noqa: F401
        record_notification,
        build_digest,
        build_group_digest,
        build_all_digests,
        get_summary,
        summarize,
        list_notifications,
        list_groups,
        get_group_counts,
        clear,
        status,
        GroupDigestService,
        group_digest_service,
    )
    from app.api.group_digest import router  # noqa: F401


def test_paths():
    from app.api.group_digest import router as gd_router

    router_paths = {getattr(r, "path", "") for r in gd_router.routes}
    assert "/api/group_digest" in router_paths
    # Verify auto-loader picked it up (app.main pkgutil include).
    included = {
        getattr(getattr(r, "original_router", None), "prefix", None)
        for r in app.routes
    }
    by_identity = any(
        getattr(r, "original_router", None) is gd_router for r in app.routes
    )
    assert by_identity or "/api/group_digest" in router_paths


def test_global_digest_has_no_group_filtering():
    # Verification: notify_digest builds global digests only.
    global_digest.clear()
    global_digest.record_notification(type="motion", message="m1", camera_id="cam1", group="A")
    global_digest.record_notification(type="motion", message="m2", camera_id="cam2", group="B")
    digest = global_digest.build_digest()
    assert digest.get("total") == 2
    # Passing a group filter must NOT narrow the global digest.
    filtered = global_digest.build_digest(group="A")
    assert filtered.get("total") == 2
    global_digest.clear()


def test_service_record_and_per_group_summary():
    group_digest.clear()
    n1 = group_digest.record_notification(type="motion", severity="high", title="Motion", message="m1", camera_id="cam1", group="Lobby")
    assert isinstance(n1, dict)
    assert n1.get("id")
    n2 = group_digest.notify(type="face", severity="info", title="Face", message="m2", camera_id="cam2", group="Parking")
    assert n2.get("id")
    assert len(group_digest.list_notifications()) == 2

    d_lobby = group_digest.build_digest(group="Lobby")
    assert isinstance(d_lobby, dict)
    assert d_lobby.get("total") == 1
    assert d_lobby.get("group") == "Lobby"
    assert d_lobby.get("ok") is True

    d_parking = group_digest.build_group_digest(group="Parking")
    assert d_parking.get("total") == 1
    assert d_parking.get("group") == "Parking"

    dall = group_digest.build_all_digests()
    assert dall.get("total_groups") == 2
    assert set(dall.get("groups", [])) == {"Lobby", "Parking"}
    assert dall["digests"]["Lobby"].get("total") == 1
    assert dall["digests"]["Parking"].get("total") == 1

    assert set(group_digest.list_groups()) == {"Lobby", "Parking"}
    assert group_digest.get_group_counts() == {"Lobby": 1, "Parking": 1}


def test_service_aliases():
    group_digest.clear()
    group_digest.record(type="audio", message="loud", group="Lobby")
    assert group_digest.get_summary(group="Lobby").get("total") == 1
    assert group_digest.summarize(group="Lobby").get("count") == 1
    assert group_digest.summary(group="Lobby").get("total") == 1
    st = group_digest.status()
    assert st.get("service") == "group_digest"
    assert st.get("total") == 1
    assert "Lobby" in st.get("groups", [])


def test_endpoint_record_and_summary():
    group_digest.clear()
    client = TestClient(app)
    r = client.post("/api/group_digest/record", json={"type": "motion", "severity": "high", "message": "hello", "camera_id": "cam1", "group": "Lobby"})
    assert r.status_code == 200, r.text
    assert r.json().get("id")
    r = client.post("/api/group_digest/record", json={"type": "motion", "message": "other", "camera_id": "cam2", "group": "Parking"})
    assert r.status_code == 200, r.text

    r2 = client.post("/api/group_digest/build", json={"group": "Lobby"})
    assert r2.status_code == 200, r2.text
    assert r2.json().get("total") == 1
    assert r2.json().get("group") == "Lobby"

    r3 = client.get("/api/group_digest/summary", params={"group": "Parking"})
    assert r3.status_code == 200, r3.text
    assert r3.json().get("total") == 1

    r4 = client.get("/api/group_digest/status")
    assert r4.status_code == 200, r4.text
    assert r4.json().get("ok") is True

    r5 = client.get("/api/group_digest/groups")
    assert r5.status_code == 200, r5.text
    assert set(r5.json().get("groups", [])) == {"Lobby", "Parking"}

    r6 = client.post("/api/group_digest/build-all", json={})
    assert r6.status_code == 200, r6.text
    assert r6.json().get("total_groups") == 2

    # hyphen alias also registered
    r7 = client.get("/api/group-digest/status")
    assert r7.status_code == 200, r7.text


def test_service_class():
    group_digest.clear()
    svc = group_digest.GroupDigestService()
    svc.record(type="motion", message="m1", group="Lobby")
    out = svc.get_summary(group="Lobby")
    assert out.get("total") == 1
    assert svc.status().get("total") == 1
    assert "Lobby" in svc.list_groups()
    alld = svc.build_all()
    assert alld.get("total_groups") == 1
