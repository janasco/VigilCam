from fastapi.testclient import TestClient

from app.main import app
from app.services import api_keys


def test_api_keys_import():
    import app.services.api_keys  # noqa: F401
    import app.api.api_keys  # noqa: F401
    from app.services.api_keys import create_api_key, api_keys_service  # noqa: F401
    from app.api.api_keys import router  # noqa: F401


def test_api_keys_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/api_keys" in paths


def test_api_keys_service_basic():
    api_keys.clear()
    created = api_keys.create_api_key(name="test-key", scopes=["read"])
    assert isinstance(created, dict)
    assert created.get("key") or created.get("api_key")
    assert created.get("status") == "active"
    key_id = created.get("id")
    raw = created.get("key") or created.get("api_key")

    fetched = api_keys.get_api_key(key_id)
    assert fetched is not None
    assert fetched.get("name") == "test-key"

    listed = api_keys.list_api_keys()
    assert len(listed) == 1

    verified = api_keys.verify_api_key(raw)
    assert verified is not None
    assert verified.get("id") == key_id

    assert api_keys.verify_api_key("invalid-key-value") is None

    revoked = api_keys.revoke_api_key(key_id)
    assert revoked is not None
    assert revoked.get("revoked") is True
    assert api_keys.verify_api_key(raw) is None


def test_api_keys_endpoint():
    api_keys.clear()
    client = TestClient(app)

    r = client.post("/api/api_keys", json={"name": "http-key", "scopes": ["read", "write"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("ok") is True
    key_id = body.get("id") or body.get("key_id")
    raw = body.get("key") or body.get("api_key")
    assert key_id
    assert raw

    r2 = client.get("/api/api_keys")
    assert r2.status_code == 200, r2.text
    assert isinstance(r2.json(), list)
    assert len(r2.json()) == 1

    r3 = client.get(f"/api/api_keys/{key_id}")
    assert r3.status_code == 200, r3.text

    r4 = client.post("/api/api_keys/verify", json={"key": raw})
    assert r4.status_code == 200, r4.text
    assert r4.json().get("valid") is True

    r5 = client.delete(f"/api/api_keys/{key_id}")
    assert r5.status_code == 200, r5.text
    assert r5.json().get("revoked") is True

    r6 = client.post("/api/api_keys/verify", json={"key": raw})
    assert r6.status_code == 200, r6.text
    assert r6.json().get("valid") is False


def test_api_keys_status():
    api_keys.clear()
    client = TestClient(app)
    r = client.get("/api/api_keys/status")
    assert r.status_code == 200, r.text
    assert r.json().get("ok") is True
