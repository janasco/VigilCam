from fastapi.testclient import TestClient

from app.main import app
from app.services import bitrate as br


def _all_paths(_app):
    out = []
    for r in _app.routes:
        if r.__class__.__name__ == "_IncludedRouter":
            try:
                for sub in r.original_router.routes:
                    p = getattr(sub, "path", None)
                    if p:
                        out.append(p)
            except Exception:  # noqa: BLE001
                continue
        else:
            p = getattr(r, "path", None)
            if p:
                out.append(p)
    # Also cover plain APIRoute entries if present.
    try:
        for r in getattr(_app.router, "routes", []):
            p = getattr(r, "path", None)
            if p and p not in out:
                out.append(p)
    except Exception:  # noqa: BLE001
        pass
    return out


def test_bitrate_imports():
    from app.main import app as _app
    from app.api import bitrate as api_mod

    assert hasattr(api_mod, "router")
    # Fast route check (avoids app.openapi() over 13k paths which is very slow).
    paths = _all_paths(_app)
    assert "/api/bitrate/status" in paths
    assert "/api/bitrate/sample" in paths
    assert "/api/bitrate/probe" in paths
    # stream aliases served as well
    assert "/api/stream_bitrate/status" in paths
    assert "/api/stream-bitrate/status" in paths


def _bytes_sampler(url, duration):
    return {"bytes_received": 8000, "duration_seconds": 2.0}


def _zero_sampler(url, duration):
    return 0


def _failing_sampler(url, duration):
    raise RuntimeError("boom")


def test_bitrate_service_basic():
    br.clear()
    r = br.sample_url("http://127.0.0.1:9/stream", duration=2.0, sampler=_bytes_sampler)
    assert r.status == "measured"
    assert r.bytes_received == 8000
    # 8000 bytes over 2s = 32000 bps = 32 kbps
    assert r.bitrate_bps == 32000.0
    assert r.bitrate_kbps == 32.0
    assert r["bitrate_bps"] == 32000.0
    assert r["ok"] is True

    got = br.get_status("http://127.0.0.1:9/stream")
    assert got is not None
    assert got.bitrate_bps == 32000.0

    r2 = br.sample_url("http://127.0.0.1:9/empty", duration=2.0, sampler=_zero_sampler)
    assert r2.status == "measured"
    assert r2.bitrate_bps == 0.0

    summary = br.get_summary()
    assert summary["total"] >= 2
    assert summary["measured"] >= 2
    br.clear()


def test_bitrate_empty_url_unknown():
    br.clear()
    r = br.sample_url("", sampler=_bytes_sampler)
    assert r.status == "unknown"
    assert r.bitrate_bps == 0.0
    br.clear()


def test_bitrate_sampler_error_never_raises():
    br.clear()
    r = br.sample_url("http://127.0.0.1:9/bad", duration=1.0, sampler=_failing_sampler)
    assert r.status == "error"
    assert r.bitrate_bps == 0.0
    assert r.error is not None
    br.clear()


def test_bitrate_camera_no_url_unknown():
    br.clear()
    r = br.sample_camera(99331)
    assert r.status == "unknown"
    assert r.bitrate_bps == 0.0
    br.clear()


def test_bitrate_endpoints():
    br.clear()
    client = TestClient(app)

    r = client.get("/api/bitrate/status")
    assert r.status_code == 200
    assert "measured" in r.json()

    r2 = client.get("/api/bitrate/summary")
    assert r2.status_code == 200
    assert "total" in r2.json()

    r422 = client.post("/api/bitrate/sample", json={})
    assert r422.status_code == 422

    r422b = client.post("/api/bitrate/probe", json={})
    assert r422b.status_code == 422

    # Seed a sample via service then fetch via results list.
    br.sample_url("http://127.0.0.1:9/seeded", duration=2.0, sampler=_bytes_sampler)
    rl = client.get("/api/bitrate/results")
    assert rl.status_code == 200
    assert isinstance(rl.json(), list)
    assert any(x.get("url") == "http://127.0.0.1:9/seeded" for x in rl.json())

    # Camera endpoint falls back to unknown (200 with status field).
    rc = client.get("/api/bitrate/camera/999996")
    assert rc.status_code == 200
    assert rc.json()["camera_id"] == 999996

    # Aliases work.
    ra = client.get("/api/stream_bitrate/status")
    assert ra.status_code == 200
    rh = client.get("/api/stream-bitrate/status")
    assert rh.status_code == 200

    # 404 for unknown cached target.
    r404 = client.get("/api/bitrate/no-such-target-xyz-123")
    assert r404.status_code == 404

    # Clear.
    rd = client.delete("/api/bitrate/results")
    assert rd.status_code == 200
    assert rd.json()["ok"] is True
    br.clear()
