from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.key_scopes import router as key_scopes_router
import app.services.key_scopes as svc
from app.services import api_keys


def _make_client():
    app = FastAPI()
    app.include_router(key_scopes_router)
    return TestClient(app)


def test_imports():
    import app.api.key_scopes  # noqa: F401
    import app.services.key_scopes  # noqa: F401
    assert key_scopes_router is not None
    assert hasattr(svc, "verify_with_scope")
    assert hasattr(svc, "has_scope")
    assert hasattr(svc, "has_all_scopes")
    assert hasattr(svc, "has_any_scope")
    assert hasattr(svc, "grant_scope")
    assert hasattr(svc, "revoke_scope")
    assert hasattr(svc, "set_scopes")
    assert hasattr(svc, "get_key_scopes")
    assert hasattr(svc, "get_status")


def test_baseline_verify_has_no_scope_restriction():
    """Document baseline: api_keys.verify only checks hash/expiry, not scopes."""
    api_keys.clear()
    created = api_keys.create_api_key(name="base-key", scopes=["read"])
    raw = created.get("key") or created.get("api_key")
    assert raw
    # Valid key verifies even when caller asks for an ungranted scope.
    assert api_keys.verify_api_key(raw) is not None
    assert api_keys.verify_api_key(raw, scope="admin") is not None
    assert api_keys.verify_api_key(raw, scopes=["admin"]) is not None
    api_keys.clear()


def test_service_scoped_verify():
    api_keys.clear()
    created = api_keys.create_api_key(name="scoped-key", scopes=["read", "cameras:read"])
    key_id = created.get("id")
    raw = created.get("key") or created.get("api_key")
    assert key_id and raw

    # No required scope -> behaves like plain verify.
    assert svc.verify_with_scope(raw) is not None
    assert svc.verify_with_scope(raw, None) is not None
    assert svc.verify_with_scope(raw, []) is not None

    # Granted scopes pass.
    assert svc.verify_with_scope(raw, "read") is not None
    assert svc.verify_with_scope(raw, "cameras:read") is not None
    assert svc.verify_with_scope(raw, ["read", "cameras:read"]) is not None

    # Ungranted scopes fail.
    assert svc.verify_with_scope(raw, "write") is None
    assert svc.verify_with_scope(raw, "admin") is None
    assert svc.verify_with_scope(raw, ["read", "write"]) is None

    # has_* helpers agree.
    assert svc.has_scope(created, "read") is True
    assert svc.has_scope(created, "write") is False
    assert svc.has_all_scopes(created, ["read", "cameras:read"]) is True
    assert svc.has_all_scopes(created, ["read", "write"]) is False
    assert svc.has_any_scope(created, ["write", "read"]) is True
    assert svc.has_any_scope(created, ["write", "admin"]) is False

    # Invalid key still fails even without scope requirement.
    assert svc.verify_with_scope("bogus-value", "read") is None
    assert svc.verify_with_scope("bogus-value") is None

    # Revoked / expired keys fail regardless of scope.
    api_keys.revoke_api_key(key_id)
    assert svc.verify_with_scope(raw, "read") is None
    api_keys.clear()


def test_service_wildcard_and_admin():
    api_keys.clear()
    admin = api_keys.create_api_key(name="admin-key", scopes=["admin"])
    admin_raw = admin.get("key") or admin.get("api_key")
    assert svc.verify_with_scope(admin_raw, "read") is not None
    assert svc.verify_with_scope(admin_raw, "cameras:write") is not None
    assert svc.verify_with_scope(admin_raw, "anything:else") is not None

    star = api_keys.create_api_key(name="star-key", scopes=["*"])
    star_raw = star.get("key") or star.get("api_key")
    assert svc.verify_with_scope(star_raw, "read") is not None
    assert svc.verify_with_scope(star_raw, "cameras:write") is not None

    prefix = api_keys.create_api_key(name="prefix-key", scopes=["cameras:*"])
    prefix_raw = prefix.get("key") or prefix.get("api_key")
    assert svc.verify_with_scope(prefix_raw, "cameras:read") is not None
    assert svc.verify_with_scope(prefix_raw, "cameras:write") is not None
    assert svc.verify_with_scope(prefix_raw, "events:read") is None
    api_keys.clear()


def test_service_grant_revoke_set():
    api_keys.clear()
    created = api_keys.create_api_key(name="mutable-key", scopes=["read"])
    key_id = created.get("id")
    raw = created.get("key") or created.get("api_key")

    assert svc.verify_with_scope(raw, "write") is None
    granted = svc.grant_scope(key_id, "write")
    assert granted is not None
    assert svc.has_scope(key_id, "write") is True
    assert svc.verify_with_scope(raw, "write") is not None

    revoked = svc.revoke_scope(key_id, "write")
    assert revoked is not None
    assert svc.has_scope(key_id, "write") is False
    assert svc.verify_with_scope(raw, "write") is None

    replaced = svc.set_scopes(key_id, ["events:read", "events:write"])
    assert replaced is not None
    assert svc.has_scope(key_id, "events:read") is True
    assert svc.has_scope(key_id, "read") is False

    assert svc.get_key_scopes(key_id) == ["events:read", "events:write"]
    assert svc.list_scopes()
    st = svc.get_status()
    assert st.get("ok") is True
    assert st.get("service") == "key_scopes"
    api_keys.clear()


def test_router_verify_check_scopes_status():
    api_keys.clear()
    created = api_keys.create_api_key(name="http-scope-key", scopes=["read"])
    raw = created.get("key") or created.get("api_key")
    key_id = created.get("id")
    client = _make_client()

    r = client.post("/api/key_scopes/verify", json={"key": raw, "scope": "read"})
    assert r.status_code == 200, r.text
    assert r.json().get("valid") is True
    assert r.json().get("authorized") is True

    r2 = client.post("/api/key_scopes/verify", json={"key": raw, "scope": "write"})
    assert r2.status_code == 200, r2.text
    assert r2.json().get("valid") is False
    assert r2.json().get("reason") == "insufficient_scope"

    r3 = client.post("/api/key-scopes/check", json={"key": raw, "scope": "read"})
    assert r3.status_code == 200, r3.text
    assert r3.json().get("valid") is True

    r4 = client.post("/api/key_scopes/verify", json={"key": "nope", "scope": "read"})
    assert r4.status_code == 200, r4.text
    assert r4.json().get("valid") is False

    r5 = client.get("/api/key_scopes/scopes")
    assert r5.status_code == 200, r5.text
    assert "read" in r5.json().get("scopes", [])

    r6 = client.get("/api/key_scopes/status")
    assert r6.status_code == 200, r6.text
    assert r6.json().get("ok") is True

    r7 = client.get(f"/api/key_scopes/key/{key_id}")
    assert r7.status_code == 200, r7.text
    assert "read" in r7.json().get("scopes", [])

    api_keys.clear()


def test_router_grant_revoke_create_list():
    api_keys.clear()
    client = _make_client()

    r = client.post("/api/key_scopes", json={"name": "router-key", "scopes": ["read"]})
    assert r.status_code == 200, r.text
    key_id = r.json().get("id") or r.json().get("key_id")
    raw = r.json().get("key") or r.json().get("api_key")
    assert key_id and raw

    g = client.post(f"/api/key_scopes/{key_id}/grant", json={"scope": "write"})
    assert g.status_code == 200, g.text
    assert "write" in g.json().get("scopes", [])

    v = client.post("/api/key_scopes/verify", json={"key": raw, "scope": "write"})
    assert v.status_code == 200, v.text
    assert v.json().get("valid") is True

    s = client.post(f"/api/key_scopes/{key_id}/scopes", json={"scopes": ["events:read"]})
    assert s.status_code == 200, s.text
    assert s.json().get("scopes") == ["events:read"]

    v2 = client.post("/api/key_scopes/verify", json={"key": raw, "scope": "write"})
    assert v2.json().get("valid") is False

    lst = client.get("/api/key_scopes")
    assert lst.status_code == 200, lst.text
    assert isinstance(lst.json(), list)
    assert any(str(e.get("id")) == str(key_id) for e in lst.json())

    lst_hyphen = client.get("/api/key-scopes")
    assert lst_hyphen.status_code == 200, lst_hyphen.text

    api_keys.clear()


def test_paths_present_in_main_app():
    from app.main import app
    registered = getattr(app, "_registered_router_ids", set())
    assert id(key_scopes_router) in registered
    own_paths = {getattr(r, "path", "") for r in key_scopes_router.routes}
    assert "/api/key_scopes" in own_paths
    assert "/api/key-scopes" in own_paths
