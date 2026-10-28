import os
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.core.config import settings
from app.services.actions import ActionExecutor, action_executor


def test_app_import():
    import app.main  # noqa: F401


def test_action_types_blocked_command_by_default():
    ex = ActionExecutor()
    res = ex.execute(type="command", target="echo hi")
    assert isinstance(res, dict)
    assert res.get("status") in ("blocked", "error")
    assert res.get("ok") is False


def test_action_command_enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("ALLOW_COMMAND_ACTIONS", "true")
    monkeypatch.setattr(settings, "ALLOW_COMMAND_ACTIONS", True, raising=False)
    ex = ActionExecutor()
    res = ex.execute(type="command", target="echo hello-wave24")
    assert res.get("ok") is True, res
    assert "hello-wave24" in (res.get("stdout") or "")


def test_action_sound_noop():
    ex = ActionExecutor()
    res = ex.execute(type="sound", target="beep")
    assert res.get("ok") is True
    assert res.get("no_op") is True or res.get("noop") is True


def test_action_file_append_still_works(tmp_path):
    ex = ActionExecutor()
    f = tmp_path / "log.txt"
    res = ex.execute(type="file_append", target=str(f), message="hi")
    assert res.get("ok") is True
    assert f.read_text().strip() == "hi"


def test_mjpeg_endpoint():
    client = TestClient(app)
    with client.stream("GET", "/api/streams/1/mjpeg?frames=2&fps=5") as r:
        assert r.status_code == 200
        assert "multipart/x-mixed-replace" in r.headers.get("content-type", "")
        data = b""
        for chunk in r.iter_bytes():
            data += chunk
            if len(data) > 10000:
                break
        assert b"--frame" in data


def test_timelapse_endpoint_alias():
    client = TestClient(app)
    paths = list(app.openapi()["paths"].keys())
    assert "/api/export/timelapse" in paths


def test_sources_endpoint_returns_list():
    client = TestClient(app)
    r = client.get("/api/sources")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    if os.path.exists("/opt/others/ispy/XML/Sources.xml"):
        assert len(data) > 0
        assert "manufacturer" in data[0]


def test_email_action_smtp_mocked():
    from unittest.mock import patch, MagicMock
    ex = ActionExecutor()
    mock_smtp = MagicMock()
    with patch("app.services.actions.smtplib.SMTP", return_value=mock_smtp):
        res = ex.execute(type="email", target="user@example.com", subject="hi", body="hello")
    assert res.get("ok") is True, res
    assert mock_smtp.send_message.called


def test_camera_control_action_no_crash():
    ex = ActionExecutor()
    res = ex.execute(type="camera_control", target="cam1", operation="start", main_url="rtsp://x")
    assert isinstance(res, dict)
    # recording manager may start ffmpeg; status should be success or error but no exception
    assert res.get("action") == "camera_control" or res.get("type") == "camera_control"
