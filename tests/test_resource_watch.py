from fastapi.testclient import TestClient

from app.main import app
import app.services.resource_watch as rw


def _fresh():
    rw.clear()
    rw.enable()
    rw.configure(cpu_threshold=85, memory_threshold=90,
                 disk_threshold=90, cooldown_seconds=300)


def test_resource_watch_imports():
    import app.services.resource_watch  # noqa: F401
    import app.api.resource_watch  # noqa: F401
    from app.services.resource_watch import check, resource_watch_service  # noqa: F401
    from app.api.resource_watch import router  # noqa: F401
    assert hasattr(router, "routes")
    # system_metrics remains read-only (no threshold logic there).
    import app.services.system_metrics as sm
    import inspect
    src = inspect.getsource(sm)
    assert "threshold" not in src.lower()
    assert "alert" not in src.lower()


def test_resource_watch_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/resource-watch/status" in paths
    assert "/api/resource-watch/summary" in paths
    assert "/api/resource_watch/status" in paths


def test_resource_watch_evaluate_basic():
    _fresh()
    # Below thresholds: no breach.
    ev = rw.evaluate(cpu=10, memory=20, disk=30)
    assert ev["ok"] is True
    assert ev["breached"] is False
    assert all(a["breached"] is False for a in ev["alerts"])
    # Above CPU threshold: breach.
    ev2 = rw.evaluate(cpu=99, memory=20, disk=30)
    assert ev2["breached"] is True
    cpu_alert = [a for a in ev2["alerts"] if a["resource"] == "cpu"][0]
    assert cpu_alert["breached"] is True
    assert cpu_alert["value"] == 99
    _fresh()


def test_resource_watch_check_and_cooldown():
    _fresh()
    rw.configure(cooldown_seconds=60)
    first = rw.check(cpu=99, memory=10, disk=10)
    assert first["ok"] is True
    assert first["breached"] is True
    assert first["alerted"] is True
    assert rw.cooldown_remaining(resource="cpu") > 0
    # Second immediate check is throttled.
    second = rw.check(cpu=99, memory=10, disk=10)
    assert second["throttled"] is True
    assert second["alerted"] is False
    # Forced check bypasses cooldown.
    forced = rw.check(cpu=99, memory=10, disk=10, force=True)
    assert forced["alerted"] is True
    # No breach -> not throttled.
    assert rw.is_throttled(cpu=1, memory=1, disk=1) is False
    _fresh()


def test_resource_watch_threshold_config():
    _fresh()
    rw.configure(cpu_threshold=50, memory_threshold=50, disk_threshold=50)
    cfg = rw.get_config()
    assert cfg["cpu_threshold"] == 50
    assert cfg["memory_threshold"] == 50
    assert cfg["disk_threshold"] == 50
    # 60 now breaches the lowered threshold.
    assert rw.evaluate(cpu=60, memory=1, disk=1)["breached"] is True
    # Disabled watcher does not emit.
    rw.disable()
    out = rw.check(cpu=99, memory=99, disk=99)
    assert out["disabled"] is True
    assert out["alerted"] is False
    _fresh()


def test_resource_watch_metrics_dict_input():
    _fresh()
    metrics = {
        "cpu": {"percent": 95.0},
        "memory": {"percent": 10.0},
        "disk": {"percent": 10.0},
    }
    ev = rw.evaluate(metrics=metrics)
    assert ev["breached"] is True
    out = rw.check(metrics=metrics)
    assert out["alerted"] is True
    _fresh()


def test_resource_watch_status_summary_resolve():
    _fresh()
    rw.check(cpu=95, memory=5, disk=5)
    status = rw.get_status()
    assert status["ok"] is True
    assert len(status["active"]) >= 1
    summary = rw.get_summary()
    assert summary["total_alerts"] >= 1
    assert "cooldown_seconds" in summary
    hist = rw.get_history()
    assert isinstance(hist, list) and len(hist) >= 1
    rw.resolve(resource="cpu")
    assert all(a["resource"] != "cpu" for a in rw.list_alerts())
    _fresh()


def test_resource_watch_endpoints():
    _fresh()
    client = TestClient(app)
    r = client.get("/api/resource-watch/status")
    assert r.status_code == 200, r.text
    assert "values" in r.json() or "evaluation" in r.json()

    r2 = client.get("/api/resource-watch/config")
    assert r2.status_code == 200, r2.text
    assert "cpu_threshold" in r2.json()

    r3 = client.get("/api/resource-watch/summary")
    assert r3.status_code == 200, r3.text
    assert "total_alerts" in r3.json()

    # Explicit breach via check endpoint.
    rc = client.post("/api/resource-watch/check",
                     json={"cpu": 99, "memory": 5, "disk": 5})
    assert rc.status_code == 200, rc.text
    assert rc.json()["breached"] is True
    assert rc.json()["alerted"] is True

    # Throttled second breach.
    rc2 = client.post("/api/resource-watch/check",
                      json={"cpu": 99, "memory": 5, "disk": 5})
    assert rc2.status_code == 200, rc2.text
    assert rc2.json()["throttled"] is True

    ra = client.get("/api/resource-watch/alerts")
    assert ra.status_code == 200, ra.text
    assert isinstance(ra.json(), list)

    rh = client.get("/api/resource-watch/history")
    assert rh.status_code == 200, rh.text
    assert isinstance(rh.json(), list)

    # Underscore alias serves the same data.
    r_alias = client.get("/api/resource_watch/summary")
    assert r_alias.status_code == 200, r_alias.text

    # System-prefixed alias.
    r_sys = client.get("/api/system/resource-watch/summary")
    assert r_sys.status_code == 200, r_sys.text

    rh2 = client.get("/api/resource-watch/health")
    assert rh2.status_code == 200, rh2.text
    assert rh2.json()["service"] == "resource_watch"
    _fresh()
