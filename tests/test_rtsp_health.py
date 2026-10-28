"""Tests for the RTSP resilience service (RtspClientSharp patterns)."""

from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app.main import app
from app.services import rtsp_health as rh


def _reset():
    rh.clear()
    try:
        rh.reset_backoff("default")
    except Exception:  # noqa: BLE001
        pass


def test_rtsp_health_imports():
    from app.api import rtsp_health as rtsp_health_api

    assert hasattr(rtsp_health_api, "router")
    paths = list(app.openapi()["paths"].keys())
    assert "/api/rtsp-health/status" in paths
    assert "/api/rtsp-health/streams" in paths


def test_keepalive_scheduling_bounds():
    import random

    rng = random.Random(1234)
    for _ in range(50):
        interval = rh.get_next_keepalive_interval(60.0, rng=rng)
        assert 30.0 <= interval <= 45.0
    # Fallback session timeout behaves like RtspClientInternal default.
    assert rh.parse_session_timeout(None) == 60.0
    assert rh.parse_session_timeout("abc123") == 60.0


def test_session_and_public_header_parsing():
    parsed = rh.parse_session_header("a1b2c3;timeout=30")
    assert parsed["session_id"] == "a1b2c3"
    assert parsed["timeout"] == 30.0
    assert rh.supports_get_parameter("OPTIONS, DESCRIBE, SETUP, GET_PARAMETER, PLAY") is True
    assert rh.supports_get_parameter("OPTIONS, DESCRIBE, SETUP, PLAY") is False
    assert rh.supports_get_parameter(None) is False
    # Round-trip through registration.
    _reset()
    st = rh.register_stream("cam-parse", session_header="sess9;timeout=25",
                            public_header="OPTIONS, GET_PARAMETER")
    assert st.session_id == "sess9"
    assert st.session_timeout_seconds == 25.0
    assert st.server_supports_get_parameter is True
    _reset()


def test_options_keepalive_requests():
    options = rh.build_options_request("rtsp://10.0.0.1/live", session_id="s1")
    assert options["method"] == "OPTIONS"
    assert "RTSP/1.0" in options["text"]
    get_param = rh.build_get_parameter_request("rtsp://10.0.0.1/live", session_id="s1")
    assert get_param["method"] == "GET_PARAMETER"
    teardown = rh.build_teardown_request("rtsp://10.0.0.1/live", session_id="s1")
    assert teardown["method"] == "TEARDOWN"
    assert teardown["cseq"] > options["cseq"]


def test_stale_stream_detection():
    _reset()
    sid = "cam-stale-1"
    # Never received a frame -> stale (mirrors ReceiveTimeout with no frames).
    st = rh.register_stream(sid)
    assert rh.is_stale(sid) is True
    # Fresh frame -> not stale.
    rh.mark_frame_received(sid)
    assert rh.is_stale(sid) is False
    assert rh.get_health(sid).online is True
    # Old frame beyond receive timeout -> stale + evaluate demotes.
    old = datetime.utcnow() - timedelta(seconds=rh.RECEIVE_TIMEOUT_SECONDS + 5)
    rh.mark_frame_received(sid, when=old)
    assert rh.is_stale(sid) is True
    rh.evaluate(stream_id=sid)
    assert rh.get_health(sid).online is False
    _reset()


def test_backoff_reconnect_policy():
    _reset()
    assert rh.compute_backoff(0) == 0.0
    assert rh.compute_backoff(1) == rh.BASE_RECONNECT_DELAY_SECONDS
    assert rh.compute_backoff(2) == rh.BASE_RECONNECT_DELAY_SECONDS * 2
    # Capped like the service max delay.
    assert rh.compute_backoff(100) <= rh.MAX_RECONNECT_DELAY_SECONDS
    sid = "cam-reconnect-1"
    rh.register_stream(sid)
    s1 = rh.record_failure(sid, error="connect refused")
    assert s1.reconnect_attempts == 1
    assert s1.backoff_seconds > 0
    assert s1.online is False
    assert rh.should_reconnect(sid) is False  # backoff not elapsed yet
    # Force the retry window open.
    state = rh.get_health(sid)
    state.next_retry_at = datetime.utcnow() - timedelta(seconds=1)
    assert rh.should_reconnect(sid) is True
    rh.record_success(sid)
    assert rh.get_health(sid).reconnect_attempts == 0
    assert rh.should_reconnect(sid) is False
    _reset()


def test_keepalive_due_scheduling():
    _reset()
    sid = "cam-keepalive-1"
    rh.register_stream(sid, session_header="s;timeout=60")
    assert rh.should_send_keepalive(stream_id=sid) is True  # never sent
    rh.mark_keepalive_sent(sid)
    assert rh.should_send_keepalive(stream_id=sid) is False  # just scheduled
    state = rh.get_health(sid)
    state.next_keepalive_at = datetime.utcnow() - timedelta(seconds=1)
    assert rh.should_send_keepalive(stream_id=sid) is True
    rh.record_keepalive_response(sid, ok=True)
    assert rh.get_health(sid).last_keepalive_response_at is not None
    _reset()


def test_rtsp_health_endpoints():
    _reset()
    client = TestClient(app)
    r = client.get("/api/rtsp-health/status")
    assert r.status_code == 200
    assert "summary" in r.json()
    assert "streams" in r.json()

    r2 = client.get("/api/rtsp-health/config")
    assert r2.status_code == 200
    assert r2.json()["receive_timeout_seconds"] == rh.RECEIVE_TIMEOUT_SECONDS

    rr = client.post("/api/rtsp-health/streams", json={"stream_id": "ep-cam-1"})
    assert rr.status_code == 200
    assert rr.json()["stream_id"] == "ep-cam-1"

    rf = client.post("/api/rtsp-health/streams/ep-cam-1/frame", json={})
    assert rf.status_code == 200
    assert rf.json()["online"] is True

    rk = client.post("/api/rtsp-health/streams/ep-cam-1/keepalive", json={})
    assert rk.status_code == 200

    rfail = client.post("/api/rtsp-health/streams/ep-cam-1/failure",
                        json={"error": "timeout"})
    assert rfail.status_code == 200
    assert rfail.json()["reconnect_attempts"] >= 1

    rget = client.get("/api/rtsp-health/streams/ep-cam-1")
    assert rget.status_code == 200
    assert rget.json()["stream_id"] == "ep-cam-1"

    rcheck = client.post("/api/rtsp-health/check", json={})
    assert rcheck.status_code == 200
    assert rcheck.json()["ok"] is True

    r404 = client.get("/api/rtsp-health/streams/does-not-exist-xyz")
    assert r404.status_code == 404

    # Underscore alias route serves the same registry.
    ru = client.get("/api/rtsp_health/status")
    assert ru.status_code == 200

    _reset()
