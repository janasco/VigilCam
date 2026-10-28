"""Tests for the image-attached webhook action (iSpy URL + grab parity)."""
import base64
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import image_webhook as image_webhook_api
from app.services import image_webhook as image_webhook_mod


FAKE_JPEG = (
    b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    b"\xff\xd9"
)


def test_imports():
    import app.services.image_webhook  # noqa: F401
    import app.api.image_webhook  # noqa: F401
    from app.services.image_webhook import send_image_webhook  # noqa: F401
    from app.api.image_webhook import router  # noqa: F401
    assert router is not None


def test_gap_plain_webhook_has_no_image_upload():
    """Confirm the gap: app/services/actions.py only does plain JSON webhooks."""
    src = Path("app/services/actions.py").read_text(encoding="utf-8")
    assert "httpx.post(target, json=" in src or "json=out_payload" in src
    assert "multipart" not in src.lower()
    assert "grab.jpg" not in src
    # image_webhook must be the module that introduces multipart + grab.jpg.
    assert image_webhook_mod.FIELD_NAME == "file"
    assert image_webhook_mod.FILENAME == "grab.jpg"
    assert image_webhook_mod.CONTENT_TYPE == "image/jpeg"


def test_missing_url():
    res = image_webhook_mod.send_image_webhook(image_bytes=FAKE_JPEG)
    assert res["ok"] is False
    assert "url" in res["detail"].lower()


def test_bad_scheme_rejected():
    res = image_webhook_mod.send_image_webhook(url="ftp://example.com/x", image_bytes=FAKE_JPEG)
    assert res["ok"] is False
    assert "http" in res["detail"].lower()


def test_multipart_post_uses_ispy_field():
    mock = MagicMock()
    mock.post.return_value = MagicMock(status_code=200, text="ok")
    with patch("app.services.image_webhook.httpx", mock):
        res = image_webhook_mod.send_image_webhook(
            url="https://example.com/hook", image_bytes=FAKE_JPEG, event_id=7
        )
    assert res["ok"] is True
    assert res["mode"] == "multipart"
    assert res["attached"] is True
    assert res["status_code"] == 200
    assert mock.post.called
    _, kwargs = mock.post.call_args
    assert kwargs["files"]["file"][0] == "grab.jpg"
    assert kwargs["files"]["file"][1] == FAKE_JPEG
    assert kwargs["files"]["file"][2] == "image/jpeg"
    assert kwargs["data"]["event_id"] == "7"


def test_base64_image_accepted():
    mock = MagicMock()
    mock.post.return_value = MagicMock(status_code=200, text="ok")
    b64 = base64.b64encode(FAKE_JPEG).decode("ascii")
    with patch("app.services.image_webhook.httpx", mock):
        res = image_webhook_mod.send_image_webhook(url="https://example.com/hook", image_base64=b64)
    assert res["ok"] is True
    assert res["image_source"] == "base64"


def test_path_image_accepted(tmp_path):
    p = tmp_path / "snap.jpg"
    p.write_bytes(FAKE_JPEG)
    mock = MagicMock()
    mock.post.return_value = MagicMock(status_code=200, text="ok")
    with patch("app.services.image_webhook.httpx", mock):
        res = image_webhook_mod.send_image_webhook(url="https://example.com/hook", image_path=str(p))
    assert res["ok"] is True
    assert res["image_source"] == "path"


def test_no_image_falls_back_to_get_like_ispy():
    mock = MagicMock()
    mock.get.return_value = MagicMock(status_code=200, text="ok")
    with patch("app.services.image_webhook.httpx", mock):
        res = image_webhook_mod.send_image_webhook(url="https://example.com/ping")
    assert res["ok"] is True
    assert res["mode"] == "get"
    assert res["attached"] is False
    assert mock.get.called
    assert not mock.post.called


def test_does_not_send_plain_json_webhook():
    """Image webhook must never duplicate actions.py plain JSON POST."""
    mock = MagicMock()
    mock.post.return_value = MagicMock(status_code=200, text="ok")
    with patch("app.services.image_webhook.httpx", mock):
        image_webhook_mod.send_image_webhook(url="https://example.com/hook", image_bytes=FAKE_JPEG)
    _, kwargs = mock.post.call_args
    assert "json" not in kwargs
    assert "files" in kwargs


def _test_client():
    app = FastAPI()
    app.include_router(image_webhook_api.router)
    return TestClient(app)


def test_router_send_multipart():
    mock = MagicMock()
    mock.post.return_value = MagicMock(status_code=200, text="ok")
    b64 = base64.b64encode(FAKE_JPEG).decode("ascii")
    with patch("app.services.image_webhook.httpx", mock):
        client = _test_client()
        r = client.post(
            "/api/image_webhook/send",
            json={"url": "https://example.com/hook", "image_base64": b64, "event_id": 3},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["action"] == "image_webhook"


def test_router_status():
    client = _test_client()
    r = client.get("/api/image_webhook/status")
    assert r.status_code == 200
    assert r.json()["filename"] == "grab.jpg"


def test_async_variant():
    import asyncio

    async def _run():
        mock = MagicMock()
        resp = MagicMock(status_code=200, text="ok")
        client = MagicMock()
        client.post.return_value = asyncio.Future()
        client.post.return_value.set_result(resp)
        client.__aenter__.return_value = client
        client.__aexit__.return_value = asyncio.Future()
        client.__aexit__.return_value.set_result(None)
        mock.AsyncClient.return_value = client
        with patch("app.services.image_webhook.httpx", mock):
            return await image_webhook_mod.send_image_webhook_async(
                url="https://example.com/hook", image_bytes=FAKE_JPEG
            )

    res = asyncio.run(_run())
    assert res["ok"] is True
    assert res["mode"] == "multipart"
