from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.key_usage import router as key_usage_router
import app.services.key_usage as svc


def _make_client():
    app = FastAPI()
    app.include_router(key_usage_router)
    return TestClient(app)


def test_imports():
    import app.api.key_usage  # noqa: F401
    import app.services.key_usage  # noqa: F401
    assert key_usage_router is not None
    assert hasattr(svc, "record_usage")
    assert hasattr(svc, "list_usage")
    assert hasattr(svc, "get_entry")
    assert hasattr(svc, "delete_entry")
    assert hasattr(svc, "clear_usage")
    assert hasattr(svc, "get_summary")
    assert hasattr(svc, "get_status")


def test_no_prior_usage_log():
    # api_keys only keeps inline counters; dedicated usage history lives here.
    import app.services.api_keys as api_keys

    assert not hasattr(api_keys, "record_usage")
    assert not hasattr(api_keys, "list_usage")
    assert not hasattr(api_keys, "get_summary")


def test_service_record_list_get_delete():
    svc.clear_usage()
    entry = svc.record_usage(key_id="k-1", key_name="svc-key",
                             endpoint="/api/cameras", method="GET",
                             ip="10.0.0.1", user_agent="pytest",
                             success=True)
    assert entry.get("id")
    assert entry.get("key_id") == "k-1"
    assert entry.get("endpoint") == "/api/cameras"
    assert entry.get("method") == "GET"
    assert entry.get("success") is True
    assert entry.get("status") == "success"
    assert entry.get("ip") == "10.0.0.1"
    assert entry.get("timestamp")

    items = svc.list_usage()
    assert any(str(e.get("id")) == str(entry.get("id")) for e in items)

    fetched = svc.get_entry(entry.get("id"))
    assert fetched is not None
    assert str(fetched.get("id")) == str(entry.get("id"))

    filtered = svc.list_usage(key_id="k-1")
    assert filtered
    assert all(str(e.get("key_id")) == "k-1" for e in filtered)

    failed = svc.record_usage(key_id="k-1", endpoint="/api/cameras",
                              method="POST", success=False)
    assert failed.get("success") is False
    assert failed.get("status") == "failed"
    only_failed = svc.list_usage(success=False)
    assert any(str(e.get("id")) == str(failed.get("id")) for e in only_failed)
    assert all(e.get("success") is False for e in only_failed)

    assert svc.delete_entry(entry.get("id")) is True
    assert svc.get_entry(entry.get("id")) is None
    svc.clear_usage()


def test_service_aliases_filters_summary_status():
    svc.clear_usage()
    a = svc.log_usage(key_id="ka", endpoint="/api/a", method="get", status="success", ip="1.1.1.1")
    b = svc.add_entry(key_id="kb", endpoint="/api/b", method="POST", status="failed", ip="2.2.2.2")
    assert a.get("id") and b.get("id")
    assert a.get("method") == "GET"
    assert len(svc.fetch_usage()) >= 2
    assert svc.get(a.get("id")) is not None
    assert len(svc.list_usage(status="failed")) >= 1
    assert len(svc.list_usage(endpoint="/api/a")) >= 1
    assert len(svc.list_usage(method="POST")) >= 1

    summary = svc.get_summary("ka")
    assert summary.get("total") >= 1
    assert summary.get("key_id") == "ka"

    st = svc.get_status()
    assert st.get("ok") is True
    assert st.get("service") == "key_usage"
    assert st.get("total") >= 2

    n = svc.clear()
    assert n >= 2
    assert svc.list_usage() == []


def test_service_with_real_api_key():
    from app.services import api_keys

    svc.clear_usage()
    api_keys.clear()
    created = api_keys.create_api_key(name="usage-key", scopes=["read"])
    key_id = created.get("id")
    raw = created.get("key") or created.get("api_key")
    assert key_id and raw

    verified = api_keys.verify_api_key(raw)
    assert verified is not None
    # Inline counters exist on the key record ...
    assert verified.get("usage_count", 0) >= 1
    # ... but no per-request history lives on api_keys itself.
    assert "endpoint" not in verified

    entry = svc.record_usage(key_id=key_id, endpoint="/api/cameras",
                             method="GET", success=True, ip="127.0.0.1")
    assert entry.get("key_id") == key_id
    # key name is denormalized from the api_keys store.
    assert entry.get("key_name") == "usage-key"

    summary = svc.summary_for_key(key_id)
    assert summary.get("total") == 1
    assert summary.get("last_used_at")

    svc.clear_usage()
    api_keys.clear()


def test_router_list_create_get_delete():
    svc.clear_usage()
    client = _make_client()

    r = client.post("/api/key_usage", json={"key_id": "rk-1", "endpoint": "/api/cameras",
                                            "method": "GET", "success": True, "ip": "9.9.9.9"})
    assert r.status_code in (200, 201), r.text
    created = r.json()
    eid = created.get("id") or created.get("entry_id") or created.get("usage_id")
    assert eid

    r_list = client.get("/api/key_usage")
    assert r_list.status_code == 200, r_list.text
    assert isinstance(r_list.json(), list)
    assert any(str(e.get("id")) == str(eid) for e in r_list.json())

    r_get = client.get(f"/api/key_usage/{eid}")
    assert r_get.status_code == 200, r_get.text

    r_del = client.delete(f"/api/key_usage/{eid}")
    assert r_del.status_code == 200, r_del.text

    r_missing = client.get(f"/api/key_usage/{eid}")
    assert r_missing.status_code == 404


def test_router_alias_paths_filters_summary():
    svc.clear_usage()
    svc.record_usage(key_id="dave-key", endpoint="/api/a", method="GET", success=True, ip="3.3.3.3")
    svc.record_usage(key_id="erin-key", endpoint="/api/b", method="POST", success=False, ip="4.4.4.4")
    client = _make_client()

    r = client.get("/api/key-usage", params={"key_id": "dave-key"})
    assert r.status_code == 200, r.text
    assert all(str(e.get("key_id")) == "dave-key" for e in r.json())

    r2 = client.get("/api/key-usage")
    assert r2.status_code == 200, r2.text
    assert isinstance(r2.json(), list)

    r3 = client.get("/api/api_key_usage")
    assert r3.status_code == 200, r3.text

    r_health = client.get("/api/key_usage/health")
    assert r_health.status_code == 200
    assert r_health.json().get("status") == "ok"

    r_count = client.get("/api/key_usage/count")
    assert r_count.status_code == 200, r_count.text
    assert r_count.json().get("count") >= 2

    r_summary = client.get("/api/key_usage/summary", params={"key_id": "dave-key"})
    assert r_summary.status_code == 200, r_summary.text
    assert r_summary.json().get("total") >= 1

    r_by_key = client.get("/api/key_usage/key/erin-key")
    assert r_by_key.status_code == 200, r_by_key.text
    assert all(str(e.get("key_id")) == "erin-key" for e in r_by_key.json())

    svc.clear_usage()


def test_paths_present_in_main_app():
    from app.main import app
    # NB: app.openapi() is prohibitively slow with ~300 auto-registered
    # routers (78s+ import, minutes for schema build); the auto-loader
    # records ids in app._registered_router_ids, so check that instead.
    registered = getattr(app, "_registered_router_ids", set())
    assert id(key_usage_router) in registered
    # And the router itself exposes the expected paths.
    own_paths = {getattr(r, "path", "") for r in key_usage_router.routes}
    assert "/api/key_usage" in own_paths
    assert "/api/key-usage" in own_paths
