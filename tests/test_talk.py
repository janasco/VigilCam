import base64

from fastapi.testclient import TestClient

from app.main import app
from app.services import talk


def test_talk_import():
    import app.services.talk  # noqa: F401
    import app.api.talk  # noqa: F401
    from app.services.talk import queue_talk, talk_service  # noqa: F401
    from app.api.talk import router  # noqa: F401


def test_talk_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/talk/{camera_id}" in paths


def test_talk_service_queued():
    b64 = base64.b64encode(b"audio-bytes").decode()
    res = talk.queue_talk(camera_id=1, audio_base64=b64)
    assert isinstance(res, dict)
    assert res.get("status") == "queued"


def test_talk_api_queued():
    client = TestClient(app)
    b64 = base64.b64encode(b"audio-bytes").decode()
    r = client.post("/api/talk/1", json={"audio_base64": b64})
    assert r.status_code == 200, r.text
    assert r.json().get("status") == "queued"


def test_talk_api_aliases():
    client = TestClient(app)
    b64 = base64.b64encode(b"audio-bytes").decode()
    for payload in ({"audio": b64}, {"data": b64}, {"base64": b64}):
        r = client.post("/api/talk/2", json=payload)
        assert r.status_code == 200, r.text
        assert r.json().get("status") == "queued"
