from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.login_history import router as login_history_router
import app.services.login_history as svc


def _make_client():
    app = FastAPI()
    app.include_router(login_history_router)
    return TestClient(app)


def test_imports():
    import app.api.login_history  # noqa: F401
    import app.services.login_history  # noqa: F401
    assert login_history_router is not None
    assert hasattr(svc, "record_login")
    assert hasattr(svc, "list_history")
    assert hasattr(svc, "get_entry")
    assert hasattr(svc, "delete_entry")
    assert hasattr(svc, "clear_history")


def test_service_record_list_get_delete():
    svc.clear_history()
    entry = svc.record_login(username="alice", success=True, ip="10.0.0.1",
                             user_agent="pytest", method="password")
    assert entry.get("id")
    assert entry.get("username") == "alice"
    assert entry.get("success") is True
    assert entry.get("status") == "success"
    assert entry.get("ip") == "10.0.0.1"

    items = svc.list_history()
    assert any(str(e.get("id")) == str(entry.get("id")) for e in items)

    fetched = svc.get_entry(entry.get("id"))
    assert fetched is not None
    assert str(fetched.get("id")) == str(entry.get("id"))

    # filter by username
    filtered = svc.list_history(username="alice")
    assert filtered
    assert all(str(e.get("username")) == "alice" for e in filtered)

    # filter by success
    failed = svc.record_login(username="bob", success=False, reason="bad password")
    assert failed.get("success") is False
    assert failed.get("status") == "failed"
    only_failed = svc.list_history(success=False)
    assert any(str(e.get("id")) == str(failed.get("id")) for e in only_failed)
    assert all(e.get("success") is False for e in only_failed)

    assert svc.delete_entry(entry.get("id")) is True
    assert svc.get_entry(entry.get("id")) is None
    svc.clear_history()


def test_service_aliases_and_clear():
    svc.clear_history()
    a = svc.log_login(username="u1", status="success", ip="1.1.1.1")
    b = svc.add_entry(username="u2", status="failed", ip="2.2.2.2")
    assert a.get("id") and b.get("id")
    assert len(svc.fetch_history()) >= 2
    assert svc.get(a.get("id")) is not None
    # status filter
    assert len(svc.list_history(status="failed")) >= 1
    n = svc.clear()
    assert n >= 2
    assert svc.list_history() == []


def test_service_status_counts():
    svc.clear_history()
    svc.record_login(username="a", success=True)
    svc.record_login(username="b", success=False)
    st = svc.get_status()
    assert st.get("ok") is True
    assert st.get("total") == 2
    assert st.get("succeeded") == 1
    assert st.get("failed") == 1
    svc.clear_history()


def test_router_list_create_get_delete():
    svc.clear_history()
    client = _make_client()

    r = client.post("/api/login/history", json={"username": "carol", "success": True, "ip": "9.9.9.9"})
    assert r.status_code in (200, 201), r.text
    created = r.json()
    eid = created.get("id") or created.get("entry_id") or created.get("login_id")
    assert eid

    r_list = client.get("/api/login/history")
    assert r_list.status_code == 200, r_list.text
    assert isinstance(r_list.json(), list)
    assert any(str(e.get("id")) == str(eid) for e in r_list.json())

    r_get = client.get(f"/api/login/history/{eid}")
    assert r_get.status_code == 200, r_get.text

    r_del = client.delete(f"/api/login/history/{eid}")
    assert r_del.status_code == 200, r_del.text

    r_missing = client.get(f"/api/login/history/{eid}")
    assert r_missing.status_code == 404


def test_router_alias_paths_and_filters():
    svc.clear_history()
    svc.record_login(username="dave", success=True, ip="3.3.3.3")
    svc.record_login(username="erin", success=False, ip="4.4.4.4")
    client = _make_client()

    r = client.get("/api/logins/history", params={"username": "dave"})
    assert r.status_code == 200, r.text
    assert all(str(e.get("username")) == "dave" for e in r.json())

    r2 = client.get("/api/login-history")
    assert r2.status_code == 200, r2.text
    assert isinstance(r2.json(), list)

    r3 = client.get("/api/login_history")
    assert r3.status_code == 200, r3.text

    r_health = client.get("/api/login/history/health")
    assert r_health.status_code == 200
    assert r_health.json().get("status") == "ok"

    r_count = client.get("/api/login/history/count")
    assert r_count.status_code == 200, r_count.text
    assert r_count.json().get("count") >= 2


def test_paths_present_in_main_app():
    from app.main import app
    paths = list(app.openapi()["paths"].keys())
    assert "/api/login/history" in paths or "/api/login-history" in paths
