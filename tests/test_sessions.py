from fastapi.testclient import TestClient

from app.main import app
from app.services import sessions


def test_sessions_import():
    import app.services.sessions  # noqa: F401
    import app.api.sessions  # noqa: F401
    from app.services.sessions import create_session, sessions_service  # noqa: F401
    from app.api.sessions import router  # noqa: F401


def test_sessions_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/sessions" in paths


def test_sessions_service_basic():
    sessions.clear()
    created = sessions.create_session(user_id="user-1", device="pytest")
    assert isinstance(created, dict)
    assert created.get("token") or created.get("session_token")
    assert created.get("status") == "active"
    session_id = created.get("id")
    raw = created.get("token") or created.get("session_token")

    fetched = sessions.get_session(session_id)
    assert fetched is not None
    assert fetched.get("user_id") == "user-1"

    listed = sessions.list_sessions()
    assert len(listed) == 1

    validated = sessions.validate_session(raw)
    assert validated is not None
    assert validated.get("id") == session_id

    assert sessions.validate_session("invalid-session-value") is None

    revoked = sessions.revoke_session(session_id)
    assert revoked is not None
    assert revoked.get("revoked") is True
    assert sessions.validate_session(raw) is None


def test_sessions_endpoint():
    sessions.clear()
    client = TestClient(app)

    r = client.post("/api/sessions", json={"user_id": "http-user", "device": "pytest-client"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("ok") is True
    session_id = body.get("id") or body.get("session_id")
    raw = body.get("token") or body.get("session_token")
    assert session_id
    assert raw

    r2 = client.get("/api/sessions")
    assert r2.status_code == 200, r2.text
    assert isinstance(r2.json(), list)
    assert len(r2.json()) == 1

    r3 = client.get(f"/api/sessions/{session_id}")
    assert r3.status_code == 200, r3.text

    r4 = client.post("/api/sessions/verify", json={"token": raw})
    assert r4.status_code == 200, r4.text
    assert r4.json().get("valid") is True

    r5 = client.delete(f"/api/sessions/{session_id}")
    assert r5.status_code == 200, r5.text
    assert r5.json().get("revoked") is True

    r6 = client.post("/api/sessions/verify", json={"token": raw})
    assert r6.status_code == 200, r6.text
    assert r6.json().get("valid") is False


def test_sessions_status():
    sessions.clear()
    client = TestClient(app)
    r = client.get("/api/sessions/status")
    assert r.status_code == 200, r.text
    assert r.json().get("ok") is True
