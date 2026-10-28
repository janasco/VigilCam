import time

from fastapi.testclient import TestClient

from app.main import app
from app.services import ptz_idle


def test_ptz_idle_imports():
    import app.services.ptz_idle  # noqa: F401
    import app.api.ptz_idle  # noqa: F401
    from app.services.ptz_idle import (  # noqa: F401
        PTZIdleService,
        check,
        clear,
        configure,
        get_status,
        notify_activity,
        ptz_idle_service,
        return_home,
    )
    from app.api.ptz_idle import router  # noqa: F401


def test_ptz_idle_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert any("idle" in p.lower() for p in paths)
    assert any(
        p.rstrip("/").endswith(("/ptz_idle", "/ptz-idle", "/ptz/idle"))
        for p in paths
    )


def test_ptz_idle_service_basic():
    ptz_idle.clear()
    cfg = ptz_idle.configure(camera_id=1, home_preset="home1", idle_seconds=60)
    assert isinstance(cfg, dict)
    assert cfg.get("camera_id") == 1
    assert cfg.get("home_preset") == "home1"
    assert cfg.get("idle_seconds") == 60
    assert len(ptz_idle.list_configs()) == 1


def test_ptz_idle_returns_home_after_n_seconds():
    ptz_idle.clear()
    ptz_idle.configure(camera_id=2, home_preset="dock", idle_seconds=10)
    # Simulate last activity 30s ago -> idle timeout exceeded.
    ptz_idle.notify_activity(camera_id=2, now=time.time() - 30)
    assert ptz_idle.is_due(camera_id=2) is True
    res = ptz_idle.check()
    assert isinstance(res, dict)
    assert res.get("processed") == 1 or res.get("count") == 1
    returned = res.get("returned") or res.get("items") or []
    assert len(returned) == 1
    assert returned[0].get("home_preset") == "dock"
    assert returned[0].get("camera_id") == 2


def test_ptz_idle_no_return_when_active():
    ptz_idle.clear()
    ptz_idle.configure(camera_id=3, home_preset="base", idle_seconds=120)
    ptz_idle.notify_activity(camera_id=3)  # just now
    assert ptz_idle.is_due(camera_id=3) is False
    res = ptz_idle.check()
    assert res.get("processed") == 0 or res.get("count") == 0


def test_ptz_idle_disabled_skipped():
    ptz_idle.clear()
    ptz_idle.configure(camera_id=4, home_preset="h", idle_seconds=5, enabled=False)
    ptz_idle.notify_activity(camera_id=4, now=time.time() - 100)
    res = ptz_idle.check()
    assert res.get("processed") == 0 or res.get("count") == 0
    forced = ptz_idle.return_home(camera_id=4)
    assert forced.get("returned") is True or forced.get("status") == "returned_home"


def test_ptz_idle_status_and_activity():
    ptz_idle.clear()
    ptz_idle.configure(camera_id=5, home_preset="p1", idle_seconds=60)
    st = ptz_idle.get_status(camera_id=5)
    assert st.get("configured") is True
    assert st.get("home_preset") == "p1"
    assert "elapsed" in st


def test_ptz_idle_endpoint_basic():
    ptz_idle.clear()
    client = TestClient(app)
    r = client.post("/api/ptz_idle", json={"camera_id": 6, "home_preset": "home6", "idle_seconds": 60})
    assert r.status_code == 200, r.text
    assert r.json().get("home_preset") == "home6"
    r2 = client.get("/api/ptz_idle")
    assert r2.status_code == 200
    assert isinstance(r2.json(), list)
    r3 = client.post("/api/ptz_idle/activity", json={"camera_id": 6})
    assert r3.status_code == 200
    r4 = client.post("/api/ptz_idle/check", json={})
    assert r4.status_code == 200
    assert r4.json().get("ok") is True


def test_ptz_idle_endpoint_return_path():
    ptz_idle.clear()
    client = TestClient(app)
    client.post("/api/ptz_idle", json={"camera_id": 7, "home_preset": "h7", "idle_seconds": 1})
    ptz_idle.notify_activity(camera_id=7, now=time.time() - 30)
    r = client.post("/api/ptz_idle/return", json={"camera_id": 7})
    assert r.status_code == 200, r.text
    assert r.json().get("status") == "returned_home"
