from fastapi.testclient import TestClient

from app.main import app
from app.services import tamper as tm


def _mid_gray(n=256, value=128):
    return [value] * n


def _dark(n=256):
    return [2] * n


def test_tamper_imports():
    from app.api import tamper as tamper_api

    assert hasattr(tamper_api, "router")
    paths = app.openapi()["paths"].keys()
    assert "/api/tamper/config" in paths
    assert "/api/tamper/reference/{camera_id}" in paths
    assert "/api/tamper/compare/{camera_id}" in paths


def test_tamper_reference_store_and_compare():
    tm.clear()
    cid = 9811
    # No baseline yet -> compared False, not tampered.
    res = tm.compare_frame(cid, _mid_gray())
    assert res["compared"] is False
    assert res["tampered"] is False

    ref = tm.set_reference(cid, _mid_gray())
    assert ref["camera_id"] == cid
    assert len(ref["histogram"]) == 32
    assert abs(sum(ref["histogram"]) - 1.0) < 1e-6

    got = tm.get_reference(cid)
    assert got is not None
    assert got["camera_id"] == cid

    # Identical frame -> near-zero drift, not tampered.
    same = tm.compare_frame(cid, _mid_gray())
    assert same["compared"] is True
    assert same["drift"] is not None
    assert same["drift"] < 0.05
    assert same["tampered"] is False

    # Covered lens (near-black) -> tampered.
    dark = tm.compare_frame(cid, _dark())
    assert dark["compared"] is True
    assert dark["tampered"] is True
    assert dark["drift"] is not None and dark["drift"] > same["drift"]

    # Histogram primitive sanity.
    h1 = tm.compute_histogram(_mid_gray())
    h2 = tm.compute_histogram(_dark())
    assert h1 is not None and h2 is not None
    assert tm.compare_histograms(h1, h1) == 0.0
    assert tm.compare_histograms(h1, h2) > 0.5

    tm.clear(cid)
    assert tm.get_reference(cid) is None
    tm.clear()


def test_tamper_config_roundtrip():
    tm.configure(threshold=0.4, bins=16)
    cfg = tm.get_config()
    assert cfg["threshold"] == 0.4
    assert cfg["bins"] == 16
    tm.configure(threshold=0.35, bins=32)


def test_tamper_endpoints():
    tm.clear()
    client = TestClient(app)
    cid = 9812

    r = client.get("/api/tamper/config")
    assert r.status_code == 200
    assert "threshold" in r.json()

    # Store baseline via API.
    rr = client.post(f"/api/tamper/reference/{cid}", json={"frame": _mid_gray()})
    assert rr.status_code == 200
    assert rr.json()["camera_id"] == cid

    rg = client.get(f"/api/tamper/reference/{cid}")
    assert rg.status_code == 200
    assert rg.json()["camera_id"] == cid

    # Compare identical -> not tampered.
    rc = client.post(f"/api/tamper/compare/{cid}", json={"frame": _mid_gray()})
    assert rc.status_code == 200
    body = rc.json()
    assert body["compared"] is True
    assert body["tampered"] is False

    # Compare dark -> tampered.
    rd = client.post(f"/api/tamper/compare/{cid}", json={"frame": _dark()})
    assert rd.status_code == 200
    assert rd.json()["tampered"] is True

    rs = client.get(f"/api/tamper/status/{cid}")
    assert rs.status_code == 200
    assert rs.json()["has_reference"] is True

    r404 = client.get("/api/tamper/reference/999998")
    assert r404.status_code == 404

    # Unknown camera compare -> not tampered, compared False.
    ru = client.post("/api/tamper/compare/999997", json={"frame": _mid_gray()})
    assert ru.status_code == 200
    assert ru.json()["compared"] is False

    tm.clear()
