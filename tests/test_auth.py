import uuid

from fastapi.testclient import TestClient

from app.main import app


def test_register_login_me():
    client = TestClient(app)
    username = "user_" + uuid.uuid4().hex[:8]
    r = client.post("/api/auth/register", json={"username": username, "password": "secret123"})
    assert r.status_code in (200, 201), r.text
    data = r.json()
    assert data.get("username") == username

    r2 = client.post("/api/auth/login", json={"username": username, "password": "secret123"})
    assert r2.status_code == 200, r2.text
    token = r2.json().get("access_token")
    assert token

    me = client.get("/api/auth/me", headers={"Authorization": "Bearer " + token})
    assert me.status_code == 200, me.text
    assert me.json().get("username") == username


def test_login_form_data():
    client = TestClient(app)
    username = "form_" + uuid.uuid4().hex[:8]
    client.post("/api/auth/register", json={"username": username, "password": "secret123"})
    r = client.post("/api/auth/login", data={"username": username, "password": "secret123"})
    assert r.status_code == 200, r.text
    assert "access_token" in r.json()


def test_protected_me_requires_token():
    client = TestClient(app)
    r = client.get("/api/auth/me")
    assert r.status_code in (401, 403)
