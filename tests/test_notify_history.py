from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.notify_history import router as notify_history_router
import app.services.notify_history as svc


def _make_client():
    app = FastAPI()
    app.include_router(notify_history_router)
    return TestClient(app)


def test_imports():
    import app.api.notify_history  # noqa: F401
    import app.services.notify_history  # noqa: F401
    assert notify_history_router is not None
    assert hasattr(svc, "record_history")
    assert hasattr(svc, "list_history")
    assert hasattr(svc, "get_entry")
    assert hasattr(svc, "delete_entry")
    assert hasattr(svc, "clear_history")
    # Tolerant aliases expected by harnesses.
    assert hasattr(svc, "record_notification") or hasattr(svc, "log_notification")
    assert hasattr(svc, "status")


def test_main_app_paths_present():
    # Ensures auto-registration via pkgutil loader in app.main.
    from app.main import app as main_app
    paths = list(main_app.openapi()["paths"].keys())
    assert any(
        p.startswith("/api/notify_history")
        or p.startswith("/api/notify-history")
        or p.startswith("/api/notifications/history")
        or p.startswith("/api/notify/history")
        for p in paths
    ), paths


def test_service_record_list_get_delete():
    svc.clear_history()
    entry = svc.record_history(channel="email", recipient="a@example.com",
                               type="motion", severity="high",
                               title="Motion", message="motion detected",
                               camera_id="cam1", status="sent")
    assert entry.get("id")
    assert entry.get("channel") == "email"
    assert entry.get("camera_id") == "cam1"

    items = svc.list_history(include_merged=False)
    assert any(str(e.get("id")) == str(entry.get("id")) for e in items)

    fetched = svc.get_entry(entry.get("id"))
    assert fetched is not None
    assert str(fetched.get("id")) == str(entry.get("id"))

    # filter by channel
    filtered = svc.list_history(channel="email", include_merged=False)
    assert filtered
    assert all(str(e.get("channel")) == "email" for e in filtered)

    # filter by status / camera
    assert svc.list_history(status="sent", include_merged=False)
    assert svc.list_history(camera_id="cam1", include_merged=False)

    assert svc.delete_entry(entry.get("id")) is True
    assert svc.get_entry(entry.get("id")) is None


def test_service_aliases_and_clear():
    svc.clear_history()
    # Ensure merged sources don't leak into assertions.
    try:
        from app.services import notify_digest as _nd
        _nd.clear()
    except Exception:
        pass
    try:
        from app.services import webhook_queue as _wq
        _wq.clear()
    except Exception:
        pass
    a = svc.log_history(channel="sms", status="sent", message="hi")
    b = svc.record_notification(channel="webhook", status="delivered", message="hook")
    c = svc.log_notification(channel="email", message="mail")
    assert a.get("id") and b.get("id") and c.get("id")
    assert len(svc.fetch_history(include_merged=False)) >= 3
    assert svc.get(a.get("id")) is not None
    assert svc.status().get("total") >= 3
    n = svc.clear()
    assert n >= 3
    assert svc.list_history(include_merged=False) == []


def test_router_list_create_get_delete():
    svc.clear_history()
    client = _make_client()

    r = client.post("/api/notify/history", json={"channel": "email", "status": "sent",
                                                 "message": "hello", "camera_id": "cam1"})
    assert r.status_code in (200, 201), r.text
    created = r.json()
    eid = created.get("id") or created.get("entry_id") or created.get("history_id")
    assert eid

    r_list = client.get("/api/notify/history")
    assert r_list.status_code == 200, r_list.text
    assert isinstance(r_list.json(), list)
    assert any(str(e.get("id")) == str(eid) for e in r_list.json())

    r_get = client.get(f"/api/notify/history/{eid}")
    assert r_get.status_code == 200, r_get.text

    r_del = client.delete(f"/api/notify/history/{eid}")
    assert r_del.status_code == 200, r_del.text

    r_missing = client.get(f"/api/notify/history/{eid}")
    assert r_missing.status_code == 404


def test_router_alias_paths_and_filters():
    svc.clear_history()
    try:
        from app.services import notify_digest as _nd
        _nd.clear()
    except Exception:
        pass
    try:
        from app.services import webhook_queue as _wq
        _wq.clear()
    except Exception:
        pass
    svc.record_history(channel="email", status="sent", camera_id="cam10")
    svc.record_history(channel="sms", status="failed", camera_id="cam11")
    client = _make_client()

    r = client.get("/api/notifications/history", params={"channel": "email"})
    assert r.status_code == 200, r.text
    assert all(str(e.get("channel")) == "email" for e in r.json())

    r2 = client.get("/api/notify-history")
    assert r2.status_code == 200, r2.text
    assert isinstance(r2.json(), list)

    r3 = client.get("/api/notify_history")
    assert r3.status_code == 200, r3.text

    r_health = client.get("/api/notify/history/health")
    assert r_health.status_code == 200
    assert r_health.json().get("status") == "ok"

    r_count = client.get("/api/notify/history/count")
    assert r_count.status_code == 200
    assert r_count.json().get("count") >= 2

    r_status = client.get("/api/notify/history/status")
    assert r_status.status_code == 200
    assert r_status.json().get("ok") is True
    svc.clear_history()
