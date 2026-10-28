from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient

from app.main import app
from app.services import webhook_queue


def test_webhook_queue_import():
    import app.services.webhook_queue  # noqa: F401
    import app.api.webhook_queue  # noqa: F401
    from app.services.webhook_queue import enqueue, webhook_queue_service  # noqa: F401
    from app.api.webhook_queue import router  # noqa: F401


def test_webhook_queue_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/webhook_queue" in paths


def test_webhook_queue_service_basic():
    webhook_queue.clear()
    job = webhook_queue.enqueue(url="https://example.com/hook", payload={"a": 1})
    assert isinstance(job, dict)
    assert job.get("status") == "queued"
    assert job.get("url") == "https://example.com/hook"
    assert webhook_queue.get(job["id"]) is not None
    assert len(webhook_queue.list_queue()) == 1


def test_webhook_queue_retry_backoff():
    webhook_queue.clear()
    assert webhook_queue.compute_backoff(1) == 2.0
    assert webhook_queue.compute_backoff(2) == 4.0


def test_webhook_queue_process_mocked():
    webhook_queue.clear()
    mock = MagicMock()
    mock.post.return_value = MagicMock(status_code=200)
    with patch("app.services.webhook_queue.httpx", mock):
        job = webhook_queue.enqueue(url="https://example.com/hook", payload={"a": 1})
        res = webhook_queue.process()
        assert isinstance(res, dict)
        assert res.get("processed") == 1
        assert job.get("status") == "delivered"
        assert job.get("ok") is True


def test_webhook_queue_endpoint_mocked():
    webhook_queue.clear()
    client = TestClient(app)
    mock = MagicMock()
    mock.post.return_value = MagicMock(status_code=200)
    with patch("app.services.webhook_queue.httpx", mock):
        r = client.post("/api/webhook_queue", json={"url": "https://example.com/hook", "payload": {"a": 1}})
        assert r.status_code == 200, r.text
        assert r.json().get("status") == "queued"
        r2 = client.get("/api/webhook_queue")
        assert r2.status_code == 200
