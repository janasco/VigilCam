"""Tests for email-with-snapshot (SMTP email + JPEG attachment)."""
import base64
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import email_snapshot as email_snapshot_api
from app.services import email_snapshot as email_snapshot_mod


FAKE_JPEG = (
    b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    b"\xff\xd9"
)


def test_imports():
    import app.services.email_snapshot  # noqa: F401
    import app.api.email_snapshot  # noqa: F401
    from app.services.email_snapshot import send_email_with_snapshot  # noqa: F401
    from app.api.email_snapshot import router  # noqa: F401
    assert router is not None


def test_requires_recipient():
    res = email_snapshot_mod.send_email_with_snapshot(image_bytes=FAKE_JPEG)
    assert res["ok"] is False
    assert "recipient" in res["detail"].lower()


def test_sends_text_only_without_image():
    mock_smtp = MagicMock()
    with patch("app.services.email_snapshot.smtplib.SMTP", return_value=mock_smtp):
        res = email_snapshot_mod.send_email_with_snapshot(
            to="user@example.com", subject="hi", body="hello"
        )
    assert res["ok"] is True
    assert res["attached"] is False
    assert mock_smtp.send_message.called
    sent_msg = mock_smtp.send_message.call_args[0][0]
    assert sent_msg["Subject"] == "hi"


def test_attaches_snapshot_bytes():
    mock_smtp = MagicMock()
    with patch("app.services.email_snapshot.smtplib.SMTP", return_value=mock_smtp):
        res = email_snapshot_mod.send_email_with_snapshot(
            to="user@example.com", subject="snap", body="see attached",
            image_bytes=FAKE_JPEG,
        )
    assert res["ok"] is True, res
    assert res["attached"] is True
    assert res["image_source"] == "bytes"
    assert res["image_bytes"] == len(FAKE_JPEG)
    assert mock_smtp.send_message.called
    sent_msg = mock_smtp.send_message.call_args[0][0]
    # EmailMessage with attachment becomes multipart.
    assert sent_msg.is_multipart()
    attachments = list(sent_msg.iter_attachments())
    assert len(attachments) == 1
    assert attachments[0].get_content_type() == "image/jpeg"
    assert attachments[0].get_filename() == "snapshot.jpg"


def test_base64_image_accepted():
    mock_smtp = MagicMock()
    b64 = base64.b64encode(FAKE_JPEG).decode("ascii")
    with patch("app.services.email_snapshot.smtplib.SMTP", return_value=mock_smtp):
        res = email_snapshot_mod.send_email_with_snapshot(
            to="user@example.com", image_base64=b64
        )
    assert res["ok"] is True
    assert res["attached"] is True
    assert res["image_source"] == "base64"


def test_path_image_accepted(tmp_path):
    p = tmp_path / "snap.jpg"
    p.write_bytes(FAKE_JPEG)
    mock_smtp = MagicMock()
    with patch("app.services.email_snapshot.smtplib.SMTP", return_value=mock_smtp):
        res = email_snapshot_mod.send_email_with_snapshot(
            to="user@example.com", image_path=str(p)
        )
    assert res["ok"] is True
    assert res["image_source"] == "path"


def test_oversize_image_rejected():
    big = b"\xff\xd8" + b"\x00" * (email_snapshot_mod.MAX_IMAGE_BYTES + 1)
    mock_smtp = MagicMock()
    with patch("app.services.email_snapshot.smtplib.SMTP", return_value=mock_smtp):
        res = email_snapshot_mod.send_email_with_snapshot(
            to="user@example.com", image_bytes=big
        )
    assert res["ok"] is False
    assert not mock_smtp.send_message.called


def _test_client():
    app = FastAPI()
    app.include_router(email_snapshot_api.router)
    return TestClient(app)


def test_router_send_with_snapshot():
    mock_smtp = MagicMock()
    b64 = base64.b64encode(FAKE_JPEG).decode("ascii")
    with patch("app.services.email_snapshot.smtplib.SMTP", return_value=mock_smtp):
        client = _test_client()
        r = client.post(
            "/api/email_snapshot/send",
            json={"to": "user@example.com", "subject": "hi", "body": "hello", "image_base64": b64},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["action"] == "email_snapshot"
    assert body["attached"] is True


def test_router_status():
    client = _test_client()
    r = client.get("/api/email_snapshot/status")
    assert r.status_code == 200
    assert r.json()["filename"] == "snapshot.jpg"
