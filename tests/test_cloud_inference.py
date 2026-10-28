import base64
from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient

from app.main import app
from app.services import cloud_inference
from app.services.cloud_inference import (
    run_inference,
    infer,
    cloud_inference_service,
    clear,
)


def test_cloud_inference_import():
    import app.services.cloud_inference  # noqa: F401
    import app.services.cloud_inference_service  # noqa: F401
    import app.api.cloud_inference  # noqa: F401
    import app.api.cloud_inference_service  # noqa: F401
    from app.services.cloud_inference import run_inference, infer, cloud_inference_service  # noqa: F401
    from app.api.cloud_inference import router  # noqa: F401


def test_cloud_inference_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/cloud_inference" in paths
    assert "/api/cloud/inference" in paths


def test_cloud_inference_service_basic_mocked():
    clear()
    class Resp:
        status_code = 200

        def json(self):
            return {"result": {"labels": ["person"]}}

    with patch("app.services.cloud_inference.httpx.post", return_value=Resp()):
        res = run_inference(model_id=1, inputs={"image": "x"})
        assert res["status"] == "completed"
        assert res["result"] == {"result": {"labels": ["person"]}}


def test_cloud_inference_alias_mocked():
    clear()
    from app.services import cloud_inference_service as cis
    cis.clear()

    class Resp:
        status_code = 200

        def json(self):
            return {"ok": True}

    mod = MagicMock()
    mod.post.return_value = Resp()
    with patch("app.services.cloud_inference_service.httpx", mod):
        res = cis.infer(model="m", inputs={"a": 1})
        assert res["status"] == "completed"
        assert mod.post.called


def test_cloud_inference_job_mocked():
    clear()
    class Resp:
        status_code = 200

        def json(self):
            return {"result": "done"}

    with patch("app.services.cloud_inference.httpx.post", return_value=Resp()):
        job = cloud_inference.create_async_job(model_id=1, inputs={"image": "x"})
        assert job["status"] == "queued"
        out = cloud_inference.process_job(job["id"])
        assert out["status"] == "completed"
        assert out["result_id"] is not None


def test_cloud_inference_endpoint_mocked():
    clear()
    client = TestClient(app)

    class Resp:
        status_code = 200

        def json(self):
            return {"ok": True}

    with patch("app.services.cloud_inference.httpx.post", return_value=Resp()):
        r = client.post("/api/cloud_inference", json={"model_id": 1, "inputs": {"image": "x"}})
        assert r.status_code == 200, r.text
        assert r.json().get("status") == "completed"


def test_cloud_inference_clear():
    clear()
    assert cloud_inference.list_results() == []
    assert cloud_inference.list_jobs() == []
