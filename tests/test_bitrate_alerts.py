from fastapi.testclient import TestClient

from app.main import app
import app.services.bitrate_alerts as ba


def _fresh():
    ba.clear()
    ba.enable()
    ba.configure(min_bitrate_bps=100_000, max_bitrate_bps=20_000_000,
                 cooldown_seconds=300)


def test_bitrate_alerts_imports():
    import app.services.bitrate_alerts  # noqa: F401
    import app.api.bitrate_alerts  # noqa: F401
    from app.services.bitrate_alerts import check, bitrate_alerts_service  # noqa: F401
    from app.api.bitrate_alerts import router  # noqa: F401
    assert hasattr(router, "routes")


def test_resource_watch_has_no_bitrate_thresholds():
    # Verify the premise: resource_watch covers cpu/memory/disk only.
    import app.services.resource_watch as rw
    assert rw.RESOURCES == ("cpu", "memory", "disk")
    cfg = rw.get_config()
    assert "cpu_threshold" in cfg
    assert "memory_threshold" in cfg
    assert "disk_threshold" in cfg
    assert "bitrate" not in str(list(cfg.keys())).lower()
    assert "min_bitrate_bps" not in cfg
    assert "max_bitrate_bps" not in cfg


def test_bitrate_alerts_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/bitrate-alerts/status" in paths
    assert "/api/bitrate-alerts/summary" in paths
    assert "/api/bitrate_alerts/status" in paths


def test_bitrate_alerts_evaluate_basic():
    _fresh()
    # Healthy bitrate within range: no breach.
    ev = ba.evaluate(target="cam1", bitrate_bps=500_000)
    assert ev["ok"] is True
    assert ev["breached"] is False
    assert ev["low"] is False
    assert ev["high"] is False
    # Low bitrate (stall): breach.
    ev2 = ba.evaluate(target="cam1", bitrate_bps=10_000)
    assert ev2["breached"] is True
    assert ev2["low"] is True
    # High bitrate: breach.
    ev3 = ba.evaluate(target="cam1", bitrate_bps=50_000_000)
    assert ev3["breached"] is True
    assert ev3["high"] is True
    # kbps alias input.
    ev4 = ba.evaluate(target="cam1", kbps=5)
    assert ev4["breached"] is True
    assert ev4["low"] is True
    _fresh()


def test_bitrate_alerts_check_and_cooldown():
    _fresh()
    ba.configure(cooldown_seconds=60)
    first = ba.check(target="cam-cool", bitrate_bps=5_000)
    assert first["ok"] is True
    assert first["breached"] is True
    assert first["alerted"] is True
    assert ba.cooldown_remaining(target="cam-cool") > 0
    # Second immediate check is throttled.
    second = ba.check(target="cam-cool", bitrate_bps=5_000)
    assert second["throttled"] is True
    assert second["alerted"] is False
    # Forced check bypasses cooldown.
    forced = ba.check(target="cam-cool", bitrate_bps=5_000, force=True)
    assert forced["alerted"] is True
    # No breach -> not throttled.
    assert ba.is_throttled(target="cam-cool", bitrate_bps=500_000) is False
    _fresh()


def test_bitrate_alerts_threshold_config():
    _fresh()
    ba.configure(min_bitrate_bps=500_000, max_bitrate_bps=1_000_000)
    cfg = ba.get_config()
    assert cfg["min_bitrate_bps"] == 500_000
    assert cfg["max_bitrate_bps"] == 1_000_000
    # 100k now breaches the raised minimum.
    assert ba.evaluate(target="t1", bitrate_bps=100_000)["breached"] is True
    # 750k is healthy.
    assert ba.evaluate(target="t1", bitrate_bps=750_000)["breached"] is False
    # Disabled watcher does not emit.
    ba.disable()
    out = ba.check(target="t1", bitrate_bps=1_000)
    assert out["disabled"] is True
    assert out["alerted"] is False
    _fresh()


def test_bitrate_alerts_bytes_duration_input():
    _fresh()
    # bytes + duration -> bps conversion (8000 bytes over 2s = 32000 bps < min).
    ev = ba.evaluate(target="t-bytes", bytes_received=8000, duration_seconds=2.0)
    assert ev["bitrate_bps"] == 32_000.0
    assert ev["breached"] is True
    out = ba.check(target="t-bytes", bytes_received=8000, duration_seconds=2.0)
    assert out["alerted"] is True
    _fresh()


def test_bitrate_alerts_status_summary_resolve():
    _fresh()
    ba.check(target="cam-res", bitrate_bps=1_000)
    status = ba.get_status()
    assert status["ok"] is True
    assert len(status["active"]) >= 1
    summary = ba.get_summary()
    assert summary["total_alerts"] >= 1
    assert "cooldown_seconds" in summary
    hist = ba.get_history()
    assert isinstance(hist, list) and len(hist) >= 1
    ba.resolve(target="cam-res")
    assert all(a["target"] != "cam-res" for a in ba.list_alerts())
    _fresh()


def test_bitrate_alerts_endpoints():
    _fresh()
    client = TestClient(app)
    r = client.get("/api/bitrate-alerts/status")
    assert r.status_code == 200, r.text
    assert "status" in r.json() or "evaluation" in r.json()

    r2 = client.get("/api/bitrate-alerts/config")
    assert r2.status_code == 200, r2.text
    assert "min_bitrate_bps" in r2.json()

    r3 = client.get("/api/bitrate-alerts/summary")
    assert r3.status_code == 200, r3.text
    assert "total_alerts" in r3.json()

    # Explicit low-bitrate breach via check endpoint.
    rc = client.post("/api/bitrate-alerts/check",
                     json={"target": "cam-http", "bitrate_bps": 1000})
    assert rc.status_code == 200, rc.text
    assert rc.json()["breached"] is True
    assert rc.json()["alerted"] is True

    # Throttled second breach.
    rc2 = client.post("/api/bitrate-alerts/check",
                      json={"target": "cam-http", "bitrate_bps": 1000})
    assert rc2.status_code == 200, rc2.text
    assert rc2.json()["throttled"] is True

    ra = client.get("/api/bitrate-alerts/alerts")
    assert ra.status_code == 200, ra.text
    assert isinstance(ra.json(), list)

    rh = client.get("/api/bitrate-alerts/history")
    assert rh.status_code == 200, rh.text
    assert isinstance(rh.json(), list)

    # Underscore alias serves the same data.
    r_alias = client.get("/api/bitrate_alerts/summary")
    assert r_alias.status_code == 200, r_alias.text

    # Alerts-prefixed alias.
    r_sys = client.get("/api/alerts/bitrate/summary")
    assert r_sys.status_code == 200, r_sys.text

    rh2 = client.get("/api/bitrate-alerts/health")
    assert rh2.status_code == 200, rh2.text
    assert rh2.json()["service"] == "bitrate_alerts"
    _fresh()
