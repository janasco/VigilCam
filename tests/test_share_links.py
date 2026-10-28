import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.services import share_links


def test_share_links_import():
    import app.services.share_links  # noqa: F401
    import app.api.share_links  # noqa: F401
    from app.services.share_links import create_share_link, verify_share_token  # noqa: F401
    from app.api.share_links import router  # noqa: F401


def test_no_duplication_of_snapshot_or_upload_storage():
    """Share links must reference resources, not re-implement storage/copy."""
    src = Path("app/services/share_links.py").read_text()
    if not Path("app/services/share_links.py").is_absolute():
        # Resolve relative to repo root regardless of cwd.
        for candidate in (Path("/opt/repo/vigilcam/app/services/share_links.py"),):
            if candidate.is_file():
                src = candidate.read_text()
                break
    lowered = src.lower()
    assert "write_bytes" not in lowered
    assert "shutil.copy" not in lowered
    assert "from app.services.snapshots import" not in lowered
    assert "from app.services.cloud_upload import" not in lowered
    assert "import snapshots" not in lowered


def test_share_links_service_snapshot_basic():
    share_links.clear()
    created = share_links.create_share_link(resource_type="snapshot", snapshot_path="events/1.jpg", ttl_seconds=3600)
    assert isinstance(created, dict)
    assert created.get("token")
    assert created.get("status") == "active"
    assert created.get("resource_type") == "snapshot"
    link_id = created.get("id")
    token = created.get("token")

    fetched = share_links.get_share_link(link_id)
    assert fetched is not None

    verified = share_links.verify_share_token(token)
    assert verified is not None
    assert verified.get("id") == link_id

    assert share_links.verify_share_token("bad.token") is None


def test_share_links_service_event_and_revoke():
    share_links.clear()
    created = share_links.create_share_link(resource_type="event", resource_id=42, ttl_seconds=600)
    assert created.get("resource_type") == "event"
    token = created["token"]
    assert share_links.verify_share_token(token) is not None

    revoked = share_links.revoke_share_link(created["id"])
    assert revoked is not None
    assert revoked.get("revoked") is True
    assert share_links.verify_share_token(token) is None


def test_share_links_expiry():
    share_links.clear()
    created = share_links.create_share_link(resource_type="snapshot", snapshot_path="a.jpg", ttl_seconds=1)
    token = created["token"]
    assert share_links.verify_share_token(token) is not None
    # Force expiry by backdating the record and waiting for stateless exp is 1s.
    time.sleep(1.2)
    assert share_links.verify_share_token(token) is None


def test_share_links_rejects_bad_resource():
    share_links.clear()
    try:
        share_links.create_share_link(resource_type="camera", resource_id="x")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for unsupported resource_type")
    try:
        share_links.create_share_link(resource_type="event")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for missing event id")


def test_share_links_endpoints():
    share_links.clear()
    client = TestClient(app)

    r = client.post("/api/share_links", json={"resource_type": "snapshot", "snapshot_path": "snap/1.jpg", "ttl_seconds": 3600})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("ok") is True
    link_id = body.get("id") or body.get("link_id")
    token = body.get("token")
    assert link_id and token

    r2 = client.get("/api/share_links")
    assert r2.status_code == 200, r2.text
    assert isinstance(r2.json(), list)
    assert len(r2.json()) == 1

    r3 = client.get(f"/api/share_links/{link_id}")
    assert r3.status_code == 200, r3.text

    r4 = client.post("/api/share_links/verify", json={"token": token})
    assert r4.status_code == 200, r4.text
    assert r4.json().get("valid") is True

    r5 = client.get(f"/api/share/{token}")
    assert r5.status_code == 200, r5.text
    assert r5.json().get("valid") is True

    r6 = client.delete(f"/api/share_links/{link_id}")
    assert r6.status_code == 200, r6.text
    assert r6.json().get("revoked") is True

    r7 = client.post("/api/share_links/verify", json={"token": token})
    assert r7.status_code == 200, r7.text
    assert r7.json().get("valid") is False


def test_share_links_event_endpoint():
    share_links.clear()
    client = TestClient(app)
    r = client.post("/api/share_links", json={"resource_type": "event", "resource_id": 7, "ttl_seconds": 300})
    assert r.status_code == 200, r.text
    assert r.json().get("resource_type") == "event"


def test_share_links_status():
    share_links.clear()
    client = TestClient(app)
    r = client.get("/api/share_links/status")
    assert r.status_code == 200, r.text
    assert r.json().get("ok") is True
