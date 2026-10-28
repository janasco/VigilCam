from fastapi.testclient import TestClient

from app.main import app
from app.services import notify_digest


def test_import():
    import app.services.notify_digest  # noqa: F401
    import app.api.notify_digest  # noqa: F401
    from app.services.notify_digest import (  # noqa: F401
        record_notification,
        build_digest,
        get_summary,
        summarize,
        list_notifications,
        clear,
        status,
        NotifyDigestService,
        notify_digest_service,
    )
    from app.api.notify_digest import router  # noqa: F401


def test_paths():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/notify_digest" in paths


def test_service_record_and_summary():
    notify_digest.clear()
    n1 = notify_digest.record_notification(type="motion", severity="high", title="Motion", message="motion detected", camera_id="cam1")
    assert isinstance(n1, dict)
    assert n1.get("id")
    n2 = notify_digest.notify(type="face", severity="info", title="Face", message="face seen", camera_id="cam2")
    assert n2.get("id")
    assert len(notify_digest.list_notifications()) == 2
    digest = notify_digest.build_digest()
    assert isinstance(digest, dict)
    assert digest.get("total") == 2
    assert digest.get("by_type", {}).get("motion") == 1
    assert digest.get("by_severity", {}).get("high") == 1
    assert digest.get("ok") is True


def test_service_aliases():
    notify_digest.clear()
    notify_digest.record(type="audio", message="loud")
    assert notify_digest.get_summary().get("total") == 1
    assert notify_digest.summarize().get("count") == 1
    assert notify_digest.summary().get("total") == 1
    st = notify_digest.status()
    assert st.get("total") == 1


def test_endpoint_record_and_summary():
    notify_digest.clear()
    client = TestClient(app)
    r = client.post("/api/notify_digest/record", json={"type": "motion", "severity": "high", "message": "hello", "camera_id": "cam1"})
    assert r.status_code == 200, r.text
    assert r.json().get("id")
    r2 = client.post("/api/notify_digest/build", json={})
    assert r2.status_code == 200, r2.text
    assert r2.json().get("total") == 1
    r3 = client.get("/api/notify_digest/summary")
    assert r3.status_code == 200, r3.text
    assert r3.json().get("total") == 1
    r4 = client.get("/api/notify_digest/status")
    assert r4.status_code == 200, r4.text
    assert r4.json().get("ok") is True


def test_service_class():
    notify_digest.clear()
    svc = notify_digest.NotifyDigestService()
    svc.record(type="motion", message="m1")
    out = svc.get_summary()
    assert out.get("total") == 1
    assert svc.status().get("total") == 1
