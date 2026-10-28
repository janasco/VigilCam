import base64

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.noise_trigger import router as noise_trigger_router  # noqa: F401
from app.services import noise_trigger as nt  # noqa: F401
from app.services.noise_trigger import (  # noqa: F401
    check,
    configure,
    evaluate,
    get_config,
    list_events,
    clear,
    reset,
    noise_trigger_service,
)


def setup_function(_):
    reset()
    clear()


def teardown_function(_):
    reset()
    clear()


def test_noise_trigger_import():
    import app.api.noise_trigger  # noqa: F401
    import app.services.noise_trigger  # noqa: F401
    from app.api.noise_trigger import router  # noqa: F401
    from app.services.noise_trigger import check, evaluate, configure  # noqa: F401


def test_noise_trigger_paths_present():
    router_paths = [getattr(r, "path", "") for r in noise_trigger_router.routes]
    assert "/api/noise_trigger/check" in router_paths
    assert "/api/noise_trigger/config" in router_paths
    assert "/api/noise_trigger/events" in router_paths
    assert "/api/noise_trigger/status" in router_paths
    # Hyphen alias should also be registered.
    assert "/api/noise-trigger/check" in router_paths


def test_audio_detector_is_mock_only():
    """audio_detector never gates on threshold: silence + high min_volume still 'detected'."""
    import base64 as _b64

    from app.services import audio_detector as ad

    ad.clear()
    silent_b64 = _b64.b64encode(bytes(256)).decode()
    res = ad.detect(audio_base64=silent_b64, min_volume=0.99)
    assert res.get("detected") is True
    assert res.get("min_volume") == 0.99
    ad.clear()


def test_evaluate_pure_threshold_no_state():
    reset()
    assert evaluate({"level": 20.0})["triggered"] is False
    assert evaluate({"level": 85.0})["triggered"] is True
    # No measurable input never triggers and stores nothing.
    assert evaluate({})["triggered"] is False
    assert list_events() == []


def test_check_threshold_gating():
    reset()
    below = check({"level": 10.0, "camera_id": "cam-gate"})
    assert below["triggered"] is False
    assert below["event_created"] is False
    assert below["event"] is None
    assert list_events() == []

    loud = check({"level": 90.0, "camera_id": "cam-gate"})
    assert loud["triggered"] is True
    assert loud["event_created"] is True
    assert loud["event"] is not None
    assert loud["event"]["level"] == 90.0
    assert len(list_events()) == 1


def test_check_cooldown_suppresses_repeat():
    reset()
    configure({"threshold": 50.0, "cooldown_seconds": 60.0})
    first = check({"level": 80.0, "camera_id": "cam-cd"})
    assert first["event_created"] is True

    # Same source within cooldown: threshold breached but no new event.
    repeat = check({"level": 95.0, "camera_id": "cam-cd"})
    assert repeat["triggered"] is True
    assert repeat["event_created"] is False
    assert repeat["suppressed_by_cooldown"] is True
    assert repeat["event"] is None
    assert len(list_events()) == 1

    # Different source is not suppressed.
    other = check({"level": 95.0, "camera_id": "cam-other"})
    assert other["event_created"] is True
    assert len(list_events()) == 2

    # Zero cooldown disables suppression.
    configure({"cooldown_seconds": 0.0})
    again = check({"level": 95.0, "camera_id": "cam-cd"})
    assert again["event_created"] is True


def test_check_volume_and_audio_inputs():
    reset()
    # 0-1 volume scale is normalized to 0-100.
    assert check({"volume": 0.9, "camera_id": "cam-vol"})["event_created"] is True
    assert check({"volume": 0.1, "camera_id": "cam-vol2"})["triggered"] is False
    # Raw audio payload is scored via RMS like audio_detector.
    loud_b64 = base64.b64encode(bytes(bytearray([255] * 256))).decode()
    res = check({"audio_base64": loud_b64, "camera_id": "cam-raw"})
    assert res["level"] is not None
    assert res["triggered"] is True
    assert res["event_created"] is True


def test_noise_trigger_api_check_and_events():
    reset()
    a = FastAPI()
    a.include_router(noise_trigger_router)
    client = TestClient(a)

    cfg = client.get("/api/noise_trigger/config")
    assert cfg.status_code == 200
    assert "threshold" in cfg.json()

    quiet = client.post("/api/noise_trigger/check", json={"level": 5.0, "camera_id": "api-cam"})
    assert quiet.status_code == 200, quiet.text
    assert quiet.json()["triggered"] is False

    loud = client.post("/api/noise_trigger/check", json={"level": 88.0, "camera_id": "api-cam"})
    assert loud.status_code == 200, loud.text
    body = loud.json()
    assert body["triggered"] is True
    assert body["event_created"] is True

    # Repeat within default cooldown is suppressed.
    repeat = client.post("/api/noise_trigger/check", json={"level": 99.0, "camera_id": "api-cam"})
    assert repeat.status_code == 200
    assert repeat.json()["suppressed_by_cooldown"] is True

    events = client.get("/api/noise_trigger/events")
    assert events.status_code == 200
    assert events.json()["count"] == 1

    one = client.get(f"/api/noise_trigger/events/{body['event_id']}")
    assert one.status_code == 200
    assert one.json()["id"] == body["event_id"]

    missing = client.get("/api/noise_trigger/events/does-not-exist")
    assert missing.status_code == 404

    status_r = client.get("/api/noise_trigger/status", params={"camera_id": "api-cam"})
    assert status_r.status_code == 200
    assert status_r.json()["in_cooldown"] is True

    cleared = client.delete("/api/noise_trigger/events")
    assert cleared.status_code == 200
    assert client.get("/api/noise_trigger/events").json()["count"] == 0
    reset()
