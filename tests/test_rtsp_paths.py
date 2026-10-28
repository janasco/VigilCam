from fastapi.testclient import TestClient

from app.main import app
from app.services import rtsp_paths as svc


def _ok_describer(host, port, path, timeout, username=None, password=None):
    # Only two paths "exist".
    if path in ("/Streaming/Channels/101", "/live"):
        return {
            "status_code": 200,
            "headers": {"content-base": f"rtsp://{host}:{port}{path}/"},
            "body": "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\nm=video 0 RTP/AVP 96\r\n",
            "sdp": True,
        }
    if path == "/private":
        return {"status_code": 401, "headers": {}, "body": ""}
    return {"status_code": 404, "headers": {}, "body": ""}


def _raw_describer(host, port, path, timeout, username=None, password=None):
    if path == "/live":
        return b"RTSP/1.0 200 OK\r\nCSeq: 1\r\nContent-Type: application/sdp\r\n\r\nv=0\r\nm=video 0 RTP/AVP 96\r\n"
    return b"RTSP/1.0 404 Not Found\r\nCSeq: 1\r\n\r\n"


def test_imports_and_routes():
    from app.api import rtsp_paths as api_mod

    assert hasattr(api_mod, "router")
    paths = list(app.openapi()["paths"].keys())
    assert "/api/rtsp-paths/paths" in paths
    assert "/api/rtsp-paths/scan" in paths
    assert "/api/rtsp-paths/probe" in paths
    assert "/api/rtsp_paths/paths" in paths


def test_common_path_list():
    assert len(svc.COMMON_RTSP_PATHS) >= 10
    assert len(svc.COMMON_RTSP_PATHS) <= svc.MAX_PATHS
    for p in svc.COMMON_RTSP_PATHS:
        assert p.startswith("/")
    # Spot-check vendor defaults.
    assert "/Streaming/Channels/101" in svc.COMMON_RTSP_PATHS
    assert "/cam/realmonitor?channel=1&subtype=0" in svc.COMMON_RTSP_PATHS
    assert "/axis-media/media.amp" in svc.COMMON_RTSP_PATHS
    assert "/live" in svc.COMMON_RTSP_PATHS
    # Deduped.
    assert len(set(svc.COMMON_RTSP_PATHS)) == len(svc.COMMON_RTSP_PATHS)


def test_build_describe_request():
    req = svc.build_describe_request("rtsp://192.168.1.10:554/live", cseq=1)
    assert req.startswith("DESCRIBE rtsp://192.168.1.10:554/live RTSP/1.0")
    assert "CSeq: 1" in req
    assert "Accept: application/sdp" in req

    authed = svc.build_describe_request("rtsp://h:554/live", username="admin", password="pw")
    assert "Authorization: Basic " in authed


def test_parse_describe_response():
    ok = svc.parse_describe_response(
        b"RTSP/1.0 200 OK\r\nCSeq: 1\r\nContent-Type: application/sdp\r\n\r\nv=0\r\nm=video 0 RTP/AVP 96\r\n"
    )
    assert ok["status_code"] == 200
    assert ok["ok"] is True
    assert ok["sdp"] is True

    nf = svc.parse_describe_response(b"RTSP/1.0 404 Not Found\r\nCSeq: 1\r\n\r\n")
    assert nf["status_code"] == 404
    assert nf["ok"] is False

    unauth = svc.parse_describe_response(b"RTSP/1.0 401 Unauthorized\r\nCSeq: 1\r\n\r\n")
    assert unauth["status_code"] == 401
    assert unauth["ok"] is False

    garbage = svc.parse_describe_response(b"not rtsp at all")
    assert garbage["status_code"] is None
    assert garbage["ok"] is False


def test_timeout_caps():
    assert svc._coerce_timeout(9999) == svc.MAX_TIMEOUT_SECONDS
    assert svc._coerce_timeout(0) == svc.DEFAULT_TIMEOUT_SECONDS
    assert svc._coerce_timeout(-5) == svc.DEFAULT_TIMEOUT_SECONDS
    assert svc._coerce_timeout(None) == svc.DEFAULT_TIMEOUT_SECONDS
    assert svc.MAX_TIMEOUT_SECONDS <= 10.0


def test_describe_path_with_fake_describer():
    svc.clear()
    hit = svc.describe_path("192.168.1.10", 554, "/live", timeout=2.0, describer=_ok_describer)
    assert hit.ok is True
    assert hit.reachable is True
    assert hit.status_code == 200
    assert hit.sdp is True
    assert hit.url == "rtsp://192.168.1.10:554/live"

    miss = svc.describe_path("192.168.1.10", 554, "/nope", timeout=2.0, describer=_ok_describer)
    assert miss.ok is False
    assert miss.status_code == 404

    raw = svc.describe_path("192.168.1.10", 554, "/live", describer=_raw_describer)
    assert raw.ok is True

    empty = svc.describe_path("", 554, "/live", describer=_ok_describer)
    assert empty.ok is False
    assert empty.status == "unknown"
    svc.clear()


def test_brute_force_paths_caps_and_dedup():
    svc.clear()
    paths = ["/live", "/live", "/nope", "/Streaming/Channels/101", "/also-missing"]
    results = svc.brute_force_paths("192.168.1.10", 554, paths, timeout=2.0, describer=_ok_describer)
    # Deduped: 4 unique.
    assert len(results) == 4
    by_path = {r.path: r for r in results}
    assert by_path["/live"].ok is True
    assert by_path["/Streaming/Channels/101"].ok is True
    assert by_path["/nope"].ok is False

    # max_paths cap respected.
    many = [f"/path{i}" for i in range(200)]
    capped = svc.brute_force_paths("192.168.1.10", 554, many, timeout=9999, describer=_ok_describer)
    assert len(capped) == svc.MAX_PATHS
    # Per-path timeout capped.
    assert all(r.timeout_seconds <= svc.MAX_TIMEOUT_SECONDS for r in capped)

    # Default (None) scans the common list.
    defaulted = svc.brute_force_paths("192.168.1.10", 554, None, timeout=2.0, describer=_ok_describer)
    assert len(defaulted) == len(svc.COMMON_RTSP_PATHS)
    found = [r for r in defaulted if r.ok]
    assert {r.path for r in found} == {"/live", "/Streaming/Channels/101"}

    # Empty host never probes.
    assert svc.brute_force_paths("", 554, ["/live"], describer=_ok_describer) == []
    assert svc.brute_force_paths(None, 554, ["/live"], describer=_ok_describer) == []
    svc.clear()


def test_endpoints():
    svc.clear()
    client = TestClient(app)

    rp = client.get("/api/rtsp-paths/paths")
    assert rp.status_code == 200
    body = rp.json()
    assert "/live" in body["paths"]
    assert body["max_timeout_seconds"] <= 10.0

    rp2 = client.get("/api/rtsp_paths/paths")
    assert rp2.status_code == 200

    # Missing host -> 422.
    r422 = client.post("/api/rtsp-paths/scan", json={})
    assert r422.status_code == 422
    r422b = client.post("/api/rtsp-paths/probe", json={})
    assert r422b.status_code == 422

    # Real scan against closed localhost port: must return 200 payload with results,
    # never raise; each entry has timeout cap applied.
    r = client.post(
        "/api/rtsp-paths/scan",
        json={"host": "127.0.0.1", "port": 9, "paths": ["/live", "/nope"], "timeout": 1.0},
    )
    assert r.status_code == 200
    payload = r.json()
    assert payload["host"] == "127.0.0.1"
    assert payload["tried"] == 2
    assert len(payload["results"]) == 2
    assert payload["found_count"] == 0

    r1 = client.post(
        "/api/rtsp-paths/probe",
        json={"host": "127.0.0.1", "port": 9, "path": "/live", "timeout": 1.0},
    )
    assert r1.status_code == 200
    assert r1.json()["ok"] is False
    assert r1.json()["timeout_seconds"] <= svc.MAX_TIMEOUT_SECONDS

    # Timeout beyond cap is clamped, not rejected.
    rc = client.post(
        "/api/rtsp-paths/probe",
        json={"host": "127.0.0.1", "port": 9, "path": "/live", "timeout": 9999},
    )
    assert rc.status_code == 200
    assert rc.json()["timeout_seconds"] == svc.MAX_TIMEOUT_SECONDS

    # Underscore alias scan works.
    ra = client.post("/api/rtsp_paths/scan", json={"host": "127.0.0.1", "port": 9, "paths": ["/live"]})
    assert ra.status_code == 200

    # Brute-force alias works.
    rb = client.post("/api/rtsp-paths/brute-force", json={"host": "127.0.0.1", "port": 9, "paths": ["/live"]})
    assert rb.status_code == 200

    rs = client.get("/api/rtsp-paths/status")
    assert rs.status_code == 200

    rd = client.delete("/api/rtsp-paths/results")
    assert rd.status_code == 200
    svc.clear()
