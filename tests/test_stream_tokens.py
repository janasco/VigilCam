import time

from fastapi.testclient import TestClient

from app.main import app
from app.services import stream_tokens


def test_stream_tokens_import():
    import app.services.stream_tokens  # noqa: F401
    import app.api.stream_tokens  # noqa: F401
    from app.services.stream_tokens import create_stream_token, verify_stream_token  # noqa: F401
    from app.api.stream_tokens import router, require_stream_token  # noqa: F401


def test_share_links_covers_snapshots_events_only():
    """Guard: share_links must not mint stream/live tokens."""
    from app.services import share_links

    try:
        share_links.create_share_link(resource_type="stream", resource_id="1")
    except ValueError:
        pass
    else:
        raise AssertionError("share_links should reject resource_type='stream'")
    try:
        share_links.create_share_link(resource_type="live", resource_id="1")
    except ValueError:
        pass
    else:
        raise AssertionError("share_links should reject resource_type='live'")
    try:
        share_links.create_share_link(resource_type="camera", resource_id="1")
    except ValueError:
        pass
    else:
        raise AssertionError("share_links should reject resource_type='camera'")


def test_stream_tokens_service_basic():
    stream_tokens.clear()
    created = stream_tokens.create_stream_token(camera_id="cam-1", ttl_seconds=600)
    assert isinstance(created, dict)
    assert created.get("token")
    assert created.get("status") == "active"
    assert str(created.get("camera_id")) == "cam-1"
    token_id = created.get("id")
    token = created.get("token")

    fetched = stream_tokens.get_stream_token(token_id)
    assert fetched is not None

    verified = stream_tokens.verify_stream_token(token, camera_id="cam-1")
    assert verified is not None
    assert verified.get("id") == token_id

    # Camera binding: token for cam-1 must not verify for cam-2.
    assert stream_tokens.verify_stream_token(token, camera_id="cam-2") is None
    assert stream_tokens.verify_for_camera(token, "cam-2") is None
    assert stream_tokens.is_token_valid_for_camera(token, "cam-2") is False
    assert stream_tokens.is_token_valid_for_camera(token, "cam-1") is True

    assert stream_tokens.verify_stream_token("bad.token") is None


def test_stream_tokens_service_revoke():
    stream_tokens.clear()
    created = stream_tokens.create_stream_token(camera_id=7, ttl_seconds=600)
    token = created["token"]
    assert stream_tokens.verify_stream_token(token, camera_id=7) is not None

    revoked = stream_tokens.revoke_stream_token(created["id"])
    assert revoked is not None
    assert revoked.get("revoked") is True
    assert stream_tokens.verify_stream_token(token, camera_id=7) is None


def test_stream_tokens_expiry():
    stream_tokens.clear()
    created = stream_tokens.create_stream_token(camera_id="cam-exp", ttl_seconds=1)
    token = created["token"]
    assert stream_tokens.verify_stream_token(token, camera_id="cam-exp") is not None
    time.sleep(1.2)
    assert stream_tokens.verify_stream_token(token, camera_id="cam-exp") is None


def test_stream_tokens_requires_camera():
    stream_tokens.clear()
    try:
        stream_tokens.create_stream_token(ttl_seconds=60)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for missing camera_id")


def test_stream_tokens_endpoints():
    stream_tokens.clear()
    client = TestClient(app)

    r = client.post("/api/stream_tokens", json={"camera_id": "cam-9", "ttl_seconds": 600})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("ok") is True
    token_id = body.get("id") or body.get("token_id")
    token = body.get("token")
    assert token_id and token

    r2 = client.get("/api/stream_tokens")
    assert r2.status_code == 200, r2.text
    assert isinstance(r2.json(), list)
    assert len(r2.json()) == 1

    r3 = client.get(f"/api/stream_tokens/{token_id}")
    assert r3.status_code == 200, r3.text

    r4 = client.post("/api/stream_tokens/verify", json={"token": token, "camera_id": "cam-9"})
    assert r4.status_code == 200, r4.text
    assert r4.json().get("valid") is True

    # Wrong camera must not validate.
    r4b = client.post("/api/stream_tokens/verify", json={"token": token, "camera_id": "other-cam"})
    assert r4b.status_code == 200, r4b.text
    assert r4b.json().get("valid") is False

    r6 = client.delete(f"/api/stream_tokens/{token_id}")
    assert r6.status_code == 200, r6.text
    assert r6.json().get("revoked") is True

    r7 = client.post("/api/stream_tokens/verify", json={"token": token, "camera_id": "cam-9"})
    assert r7.status_code == 200, r7.text
    assert r7.json().get("valid") is False


def test_stream_tokens_gated_mjpeg():
    stream_tokens.clear()
    client = TestClient(app)

    r = client.post("/api/stream_tokens", json={"camera_id": "1", "ttl_seconds": 600})
    assert r.status_code == 200, r.text
    token = r.json().get("token")
    assert token

    # No token -> 401.
    r_no = client.get("/api/stream_tokens/mjpeg/1?frames=1&fps=5")
    assert r_no.status_code == 401, r_no.text

    # Bad token -> 403.
    r_bad = client.get("/api/stream_tokens/mjpeg/1?frames=1&fps=5&token=bad.token")
    assert r_bad.status_code in (403, 404), r_bad.text

    # Token for another camera -> 403.
    r_wrong = client.get(f"/api/stream_tokens/mjpeg/2?frames=1&fps=5&token={token}")
    assert r_wrong.status_code in (403, 404), r_wrong.text

    # Valid token -> MJPEG stream.
    with client.stream("GET", f"/api/stream_tokens/mjpeg/1?frames=2&fps=5&token={token}") as resp:
        assert resp.status_code == 200, resp.text if hasattr(resp, "text") else resp.status_code
        assert "multipart/x-mixed-replace" in resp.headers.get("content-type", "")
        data = b""
        for chunk in resp.iter_bytes():
            data += chunk
            if len(data) > 10000:
                break
        assert b"--frame" in data


def test_stream_tokens_status():
    stream_tokens.clear()
    client = TestClient(app)
    r = client.get("/api/stream_tokens/status")
    assert r.status_code == 200, r.text
    assert r.json().get("ok") is True
