import asyncio
import base64

from fastapi.testclient import TestClient

from app.main import app
from app.services import genai_description
from app.services import scene_description


def test_genai_description_import():
    import app.services.genai_description  # noqa: F401
    import app.services.scene_description  # noqa: F401
    import app.api.genai_description  # noqa: F401
    import app.api.scene_description  # noqa: F401
    from app.services.genai_description import (
        RateLimited, Busy, pick, text_of, describe, _allow,
    )  # noqa: F401
    from app.api.genai_description import router  # noqa: F401


def test_genai_description_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/cameras/{camera_id}/describe" in paths
    assert "/api/v1/cameras/{camera_id}/describe" in paths


def test_pick_and_text_of():
    adapters = [
        {"name": "blip", "tasks_advertised": ["scene_caption"]},
        {"name": "moondream", "tasks_advertised": ["visual_qa", "scene_caption"]},
    ]
    assert genai_description.pick(adapters, None) == ("blip", "scene_caption")
    assert genai_description.pick(adapters, "is the gate open?") == ("moondream", "visual_qa")
    assert genai_description.pick(adapters[:1], "is the gate open?") == ("blip", "scene_caption")
    assert genai_description.pick([{"name": "yolo", "tasks_advertised": ["x"]}], None) is None
    assert genai_description.text_of({"result": {"answer": " Yes. "}}) == "Yes."
    assert genai_description.text_of({"caption": "a car"}) == "a car"
    assert genai_description.text_of({"result": {}}) is None


def test_rate_limit_is_per_caller():
    genai_description._calls.clear()
    for i in range(genai_description.PER_MINUTE):
        assert genai_description._allow("a", now=100.0 + i)
    assert not genai_description._allow("a", now=110.0)
    assert genai_description._allow("b", now=110.0)
    assert genai_description._allow("a", now=161.0)


def test_describe_without_adapter():
    async def none():
        return []

    genai_description._calls.clear()
    genai_description._registry = none
    cam = type("C", (), {"id": 1, "rtsp_url": "rtsp://x"})()
    out = asyncio.run(genai_description.describe(cam, "t"))
    assert out == {"available": False, "description": None, "model": None, "task": None}


def test_describe_api_basic():
    client = TestClient(app)
    cam = client.post("/api/cameras", json={"name": "genaicam", "kind": "ip", "source": "rtsp://y"}).json()

    async def fake(camera, caller, question=None):
        return {"available": True, "description": "a red car", "model": "m", "task": "scene_caption"}

    genai_description.describe = fake
    r = client.post(f"/api/cameras/{cam['id']}/describe", json={"question": "any cars?"})
    assert r.status_code == 200, r.text
    assert r.json()["description"] == "a red car"
    assert r.json()["camera_id"] == cam["id"]

    async def busy(camera, caller, question=None):
        raise genai_description.Busy()

    genai_description.describe = busy
    r = client.post(f"/api/cameras/{cam['id']}/describe")
    assert r.status_code == 503
    assert r.headers.get("Retry-After") == "30"