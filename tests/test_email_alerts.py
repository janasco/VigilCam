import base64
from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient

from app.main import app
from app.services import email_alerts


def test_email_alerts_import():
    import app.services.email_alerts  # noqa: F401
    import app.api.email_alerts  # noqa: F401
    from app.services.email_alerts import send_email  # noqa: F401
    from app.api.email_alerts import router  # noqa: F401


def test_email_alerts_path_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/alerts/email" in paths


def test_email_alerts_service_mocked():
    mock_smtp = MagicMock()
    with patch("app.services.email_alerts.smtplib.SMTP", return_value=mock_smtp):
        res = email_alerts.send_email(to="user@example.com", subject="hi", body="hello")
    assert res.get("ok") is True, res
    assert mock_smtp.send_message.called


def test_email_alerts_endpoint_mocked():
    client = TestClient(app)
    mock_smtp = MagicMock()
    with patch("app.services.email_alerts.smtplib.SMTP", return_value=mock_smtp):
        r = client.post("/api/alerts/email", json={"to": "user@example.com", "subject": "hi", "body": "hello"})
    assert r.status_code == 200, r.text
    assert mock_smtp.send_message.called
    data = r.json()
    assert data.get("ok") is True
