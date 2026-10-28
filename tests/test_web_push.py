import os
from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient

from app.main import app
from app.services import web_push
from app.services.web_push import send_web_push, send, web_push_service, subscribe, clear


def test_web_push_import():
    import app.services.web_push  # noqa: F401
    import app.api.web_push  # noqa: F401
    from app.services.web_push import send_web_push, send, web_push_service  # noqa: F401
    from app.api.web_push import router  # noqa: F401


def test_web_push_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/web_push" in paths
    assert "/api/web-push" in paths or "/api/web_push" in paths


def test_web_push_service_basic_mocked():
    clear()
    class Resp:
        status_code = 201

        def json(self):
            return {"id": "wp1"}

    with patch("app.services.web_push.httpx.post", return_value=Resp()) as m:
        res = send_web_push(
            endpoint="https://example.com/push",
            title="hi",
            body="hello",
        )
        assert res["ok"] is True
        assert m.called
        args, kwargs = m.call_args
        assert args[0] == "https://example.com/push"
        assert kwargs["json"]["title"] == "hi"


def test_web_push_subscribe_and_list():
    clear()
    sub = subscribe(endpoint="https://example.com/push", keys={"p256dh": "a", "auth": "b"})
    assert isinstance(sub, dict)
    assert sub.get("endpoint") == "https://example.com/push"
    subs = web_push.list_subscriptions()
    assert len(subs) == 1


def test_web_push_router_endpoint_mocked():
    clear()
    client = TestClient(app)
    class Resp:
        status_code = 201

        def json(self):
            return {"id": "wp999"}

    with patch("app.services.web_push.httpx.post", return_value=Resp()):
        r = client.post(
            "/api/web_push/send",
            json={"endpoint": "https://example.com/push", "title": "hi", "body": "hello"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True


def test_web_push_vapid_key():
    key = web_push.get_vapid_public_key()
    assert isinstance(key, str)
    assert len(key) > 0
