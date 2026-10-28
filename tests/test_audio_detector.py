import base64

from fastapi.testclient import TestClient

from app.main import app
from app.services import audio_detector as ad


def test_audio_detector_import():
    import app.services.audio_detector  # noqa: F401
    import app.api.audio_detector  # noqa: F401
    from app.services.audio_detector import detect_audio, audio_detector_service  # noqa: F401
    from app.api.audio_detector import router  # noqa: F401


def test_audio_detector_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/audio-detector" in paths or "/api/audio/detector" in paths or "/api/audio/detect" in paths


def test_audio_detector_service_basic():
    ad.clear()
    b64 = base64.b64encode(b"audio-bytes").decode()
    res = ad.detect_audio(audio_base64=b64)
    assert isinstance(res, dict)
    assert res.get("detected") is True
    assert res.get("ok") is True
    assert res.get("label") is not None


def test_audio_detector_api_queued():
    client = TestClient(app)
    b64 = base64.b64encode(b"audio-bytes").decode()
    r = client.post("/api/audio-detector/detect", json={"audio_base64": b64})
    assert r.status_code == 200, r.text
    assert r.json().get("detected") is True


def test_audio_detector_api_aliases():
    client = TestClient(app)
    b64 = base64.b64encode(b"audio-bytes").decode()
    for payload in ({"audio": b64}, {"data": b64}, {"base64": b64}):
        r = client.post("/api/audio-detector", json=payload)
        assert r.status_code == 200, r.text
        assert r.json().get("detected") is True


def test_audio_detector_camera_path():
    client = TestClient(app)
    b64 = base64.b64encode(b"audio-bytes").decode()
    r = client.post("/api/audio-detector/1", json={"audio_base64": b64})
    assert r.status_code == 200, r.text
    assert r.json().get("camera_id") == 1
