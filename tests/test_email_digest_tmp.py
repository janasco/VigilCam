from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from app.main import app
from app.services import email_digest

def test_import():
    import app.services.email_digest
    import app.api.email_digest
    from app.services.email_digest import schedule, run, email_digest_service, EmailDigestScheduler, email_digest_scheduler
    from app.api.email_digest import router

def test_path():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/email_digest" in paths

def test_service_basic():
    email_digest.clear()
    job = email_digest.schedule(to="user@example.com", subject="Digest", body="hello", schedule="*")
    assert isinstance(job, dict)
    assert job.get("status") == "scheduled"
    assert email_digest.get(job["id"]) is not None
    assert len(email_digest.list_schedules()) == 1

def test_run_mocked():
    email_digest.clear()
    email_digest.schedule(to="user@example.com", subject="Digest", body="hello", schedule="*")
    mock_smtp = MagicMock()
    with patch("app.services.email_digest.smtplib.SMTP", return_value=mock_smtp):
        res = email_digest.run(force=True)
    print("RES", res)
    assert isinstance(res, dict)
    assert res.get("processed") == 1
    assert res.get("sent") == 1
    assert mock_smtp.send_message.called

def test_endpoint_mocked():
    email_digest.clear()
    client = TestClient(app)
    mock_smtp = MagicMock()
    with patch("app.services.email_digest.smtplib.SMTP", return_value=mock_smtp):
        r = client.post("/api/email_digest/schedule", json={"to": "user@example.com", "subject": "Digest", "body": "hello", "schedule": "*"})
        assert r.status_code == 200, r.text
        r2 = client.post("/api/email_digest/run", json={"force": True})
        assert r2.status_code == 200, r2.text
        assert r2.json().get("processed") == 1
        assert mock_smtp.send_message.called

def test_scheduler_class():
    mock_smtp = MagicMock()
    with patch("app.services.email_digest.smtplib.SMTP", return_value=mock_smtp):
        sched = email_digest.EmailDigestScheduler(interval_seconds=10, to="user@example.com", subject="hi", body="hello")
        res = sched.run_now()
        print("SCHED RES", res)
        assert res.get("sent") == 1 or res.get("sent_count") == 1
        assert mock_smtp.send_message.called

def test_scheduler_mock_sender():
    mock_rm = MagicMock()
    mock_rm.run.return_value = {"sent_count": 1, "sent": 1, "processed": 1}
    sched = email_digest.EmailDigestScheduler(retention_manager=mock_rm, interval_seconds=10)
    res = sched.run_now()
    assert res.get("sent_count") == 1
    mock_rm.run.assert_called()
