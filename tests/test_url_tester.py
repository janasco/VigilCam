from fastapi.testclient import TestClient

from app.main import app
from app.services import url_tester as svc


def _jpeg_body() -> bytes:
    return b"\xff\xd8\xff\xe0\x00\x10JFIF\x00test-image-bytes"


def _fake_jpeg_fetcher(url, timeout, headers=None):
    return (200, {"content-type": "image/jpeg", "content-length": "1234"}, _jpeg_body())


def _fake_mjpeg_fetcher(url, timeout, headers=None):
    body = (
        b"--boundary\r\nContent-Type: image/jpeg\r\n\r\n"
        + _jpeg_body()
        + b"\r\n--boundary--\r\n"
    )
    return (200, {"content-type": "multipart/x-mixed-replace; boundary=boundary"}, body)


def _fail_fetcher(url, timeout, headers=None):
    raise ConnectionError("refused")


def test_url_tester_imports_and_routes():
    from app.api import url_tester as api_mod

    assert hasattr(api_mod, "router")
    paths = list(app.openapi()["paths"].keys())
    assert "/api/url-tester/status" in paths
    assert "/api/url-tester/test" in paths
    assert "/api/url_tester/status" in paths


def test_expand_template_sources_xml_tokens():
    out = svc.expand_template(
        "videostream.asf?user=[USERNAME]&pwd=[PASSWORD]&resolution=[WIDTH]x[HEIGHT]",
        username="admin", password="secret", width=640, height=480,
    )
    assert out == "videostream.asf?user=admin&pwd=secret&resolution=640x480"

    out2 = svc.expand_template("axis-cgi/jpg/image.cgi?camera=[CHANNEL]", channel=3)
    assert out2 == "axis-cgi/jpg/image.cgi?camera=3"

    assert svc.has_template_tokens("a/[USERNAME]/b") is True
    assert svc.has_template_tokens("plain/path.jpg") is False


def test_build_url_from_catalog_parts():
    # Sources.xml style: prefix + host + port + path template.
    built = svc.build_url("mjpg/1/video.mjpg", host="192.168.1.10", prefix="http://")
    assert built == "http://192.168.1.10/mjpg/1/video.mjpg"

    built2 = svc.build_url(
        "videostream.cgi?user=[USERNAME]&pwd=[PASSWORD]",
        host="cam.local", port=81, prefix="http://",
        username="admin", password="pw",
    )
    assert built2 == "http://cam.local:81/videostream.cgi?user=admin&pwd=pw"

    # Absolute template passes through with expansion.
    abs_url = svc.build_url(
        "http://[USERNAME]:[PASSWORD]@cam.local/jpg/image.jpg",
        username="u", password="p",
    )
    assert abs_url == "http://u:p@cam.local/jpg/image.jpg"


def test_classify_url_kinds():
    assert svc.classify_url("http://cam/jpg/1/image.jpg", source="JPEG")["kind"] == "snapshot"
    assert svc.classify_url("http://cam/mjpg/1/video.mjpg", source="MJPEG")["kind"] == "mjpeg"
    assert svc.classify_url("http://cam/mjpg/video.cgi", source="MJPEG")["kind"] == "mjpeg"
    assert svc.classify_url("rtsp://cam:554/axis-media/media.amp", source="FFMPEG")["kind"] == "stream"
    assert svc.classify_url("http://cam/grab.jpg")["kind"] == "snapshot"


def test_fetch_snapshot_reports_status_latency_content_type():
    svc.clear()
    r = svc.test_url("http://cam.local/jpg/image.jpg", timeout=5.0, fetcher=_fake_jpeg_fetcher)
    assert r.ok is True
    assert r.reachable is True
    assert r.status == "ok"
    assert r.http_status == 200
    assert r.content_type == "image/jpeg"
    assert r.latency_ms is not None
    assert r.is_image is True
    assert r.detected == "snapshot"
    assert r["content_type"] == "image/jpeg"

    got = svc.get_status("http://cam.local/jpg/image.jpg")
    assert got is not None and got.ok is True

    rm = svc.test_url("http://cam.local/mjpg/video.mjpg", timeout=5.0, fetcher=_fake_mjpeg_fetcher)
    assert rm.ok is True
    assert rm.detected == "mjpeg"
    assert "multipart" in (rm.content_type or "")


def test_fetch_error_and_unknown():
    svc.clear()
    r = svc.test_url("http://127.0.0.1:9/nope.jpg", timeout=1.0, fetcher=_fail_fetcher)
    assert r.ok is False
    assert r.status == "error"
    assert r.error

    u = svc.test_url("", fetcher=_fake_jpeg_fetcher)
    assert u.status == "unknown"
    assert u.ok is False
    svc.clear()


def test_test_template_preset():
    svc.clear()
    preset = {"prefix": "http://", "url": "jpg/1/image.jpg", "port": None, "source": "JPEG"}
    r = svc.test_template(preset, host="cam.local", timeout=5.0, fetcher=_fake_jpeg_fetcher)
    assert r.ok is True
    assert r.expanded_url == "http://cam.local/jpg/1/image.jpg"
    svc.clear()


def test_endpoints():
    svc.clear()
    client = TestClient(app)

    rs = client.get("/api/url-tester/status")
    assert rs.status_code == 200
    assert "total" in rs.json()

    rc = client.get("/api/url-tester/classify", params={"url": "http://cam/mjpg/video.mjpg", "source": "MJPEG"})
    assert rc.status_code == 200
    assert rc.json()["kind"] == "mjpeg"

    rc422 = client.get("/api/url-tester/classify")
    assert rc422.status_code == 422

    re_ = client.post("/api/url-tester/expand", json={"template": "jpg/[CHANNEL]/image.jpg", "channel": 2})
    assert re_.status_code == 200
    assert re_.json()["expanded"] == "jpg/2/image.jpg"

    r422 = client.post("/api/url-tester/test", json={})
    assert r422.status_code == 422

    # Closed localhost port returns a 200 payload with status=error (no raise).
    r = client.post("/api/url-tester/test", json={"url": "http://127.0.0.1:9/nonexistent.jpg", "timeout": 1.0})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in ("ok", "error")
    assert "latency_ms" in body
    assert "content_type" in body

    rl = client.get("/api/url-tester/results")
    assert rl.status_code == 200
    assert isinstance(rl.json(), list)

    ra = client.get("/api/url_tester/status")
    assert ra.status_code == 200

    r404 = client.get("/api/url-tester/no-such-target-xyz-123")
    assert r404.status_code == 404

    rd = client.delete("/api/url-tester/results")
    assert rd.status_code == 200
    assert rd.json()["ok"] is True
    svc.clear()
