from fastapi.testclient import TestClient

from app.main import app
from app.services import stream_probe as probe


def test_stream_probe_imports():
    from app.main import app as _app
    from app.api import stream_probe as api_mod

    assert hasattr(api_mod, "router")
    paths = list(_app.openapi()["paths"].keys())
    assert "/api/stream-probe/status" in paths
    assert "/api/stream-probe/probe" in paths
    # underscore alias served as well
    assert "/api/stream_probe/status" in paths


def _ok_prober(url, timeout):
    return True


def _fail_prober(url, timeout):
    return False


def test_stream_probe_service_basic():
    probe.clear()
    r = probe.probe_url("rtsp://127.0.0.1:8554/cam", timeout=1.0, prober=_ok_prober)
    assert r.reachable is True
    assert r.healthy is True
    assert r.status == "healthy"
    assert r.latency_ms is not None
    assert r["reachable"] is True

    got = probe.get_status("rtsp://127.0.0.1:8554/cam")
    assert got is not None
    assert got.reachable is True

    r2 = probe.probe_url("rtsp://127.0.0.1:8554/bad", timeout=1.0, prober=_fail_prober)
    assert r2.reachable is False
    assert r2.status == "unhealthy"

    assert probe.is_healthy("rtsp://127.0.0.1:8554/cam") is True
    assert probe.is_reachable("rtsp://127.0.0.1:8554/bad") is False

    summary = probe.get_summary()
    assert summary["total"] >= 2
    assert summary["healthy"] >= 1
    probe.clear()


def test_stream_probe_empty_url_unknown():
    probe.clear()
    r = probe.probe_url("", prober=_ok_prober)
    assert r.status == "unknown"
    assert r.reachable is False
    probe.clear()


def test_stream_probe_camera_fallback_to_health():
    from app.services import camera_health as ch

    probe.clear()
    ch.clear()
    cid = 9921
    ch.mark_frame_received(cid)
    r = probe.probe_camera(cid)
    assert r.reachable is True
    assert r.healthy is True
    ch.mark_offline(cid)
    r2 = probe.probe_camera(cid)
    assert r2.reachable is False
    probe.clear()
    ch.clear()


def test_stream_probe_endpoints():
    probe.clear()
    client = TestClient(app)

    r = client.get("/api/stream-probe/status")
    assert r.status_code == 200
    assert "healthy" in r.json()

    r2 = client.get("/api/stream-probe/summary")
    assert r2.status_code == 200
    assert "total" in r2.json()

    # Query probe with unreachable port should return unhealthy, not raise.
    # Use TEST-NET-1 address (unroutable) with short timeout via service prober path:
    # endpoint itself does a real socket check; use invalid host with tiny timeout
    # by posting an explicit unknown scheme? Instead exercise validation first.
    r422 = client.post("/api/stream-probe/probe", json={})
    assert r422.status_code == 422

    # Probe via service-backed cache: seed a result then fetch it by target.
    probe.probe_url("rtsp://127.0.0.1:8554/seeded", timeout=1.0, prober=_ok_prober)
    rg = client.get("/api/stream-probe/rtsp://127.0.0.1:8554/seeded")
    # Path with slashes won't match single segment; use results list instead.
    rl = client.get("/api/stream-probe/results")
    assert rl.status_code == 200
    assert isinstance(rl.json(), list)
    assert any(x.get("url") == "rtsp://127.0.0.1:8554/seeded" for x in rl.json())

    # Camera probe endpoint falls back to health (unknown -> 200 with status field).
    rc = client.get("/api/stream-probe/camera/999997")
    assert rc.status_code == 200
    assert rc.json()["camera_id"] == 999997

    # Underscore alias works.
    ra = client.get("/api/stream_probe/status")
    assert ra.status_code == 200

    # 404 for unknown cached target.
    r404 = client.get("/api/stream-probe/no-such-target-xyz-123")
    assert r404.status_code == 404

    # Clear.
    rd = client.delete("/api/stream-probe/results")
    assert rd.status_code == 200
    assert rd.json()["ok"] is True
    probe.clear()
