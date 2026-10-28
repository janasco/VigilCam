from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

BASE = Path(__file__).resolve().parents[1]
HTML = (BASE / "app" / "templates" / "index.html").read_text(encoding="utf-8")


def test_email_digest_ui_present():
    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200
    html = r.text
    for token in [
        'id="emailDigestForm"',
        'id="emailDigestStatus"',
        'id="emailDigestSchedulerStatus"',
        'id="emailDigestList"',
        'id="emailDigestSchedules"',
        'id="emailDigestScheduler"',
        'name="to"',
        'name="recipients"',
        'name="subject"',
        'name="body"',
        'name="message"',
        'name="schedule"',
        'name="cron"',
        'name="frequency"',
        'name="interval"',
        'name="interval_seconds"',
        'name="enabled"',
        'name="active"',
        'name="name"',
        'name="title"',
        'loadEmailDigests',
        'scheduleEmailDigest',
        'runEmailDigest',
        'runEmailDigestNow',
        '/api/email_digest',
        '/api/email-digest',
    ]:
        assert token in html, token


def test_email_digest_ui_scheduler_aliases():
    for token in [
        'id="emailDigestSchedulerForm"',
        'id="email-digest-form"',
        'id="set-email-digest"',
        'id="set-emailDigest"',
        'id="set-digest"',
        'EmailDigestScheduler',
        'email_digest_scheduler',
        'loadEmailDigestSchedules',
        'runDueEmailDigests',
        'processEmailDigests',
        'executeEmailDigests',
        'triggerEmailDigest',
        'startEmailDigestScheduler',
        'stopEmailDigestScheduler',
    ]:
        assert token in HTML, token


def test_email_digest_ui_endpoints_available():
    client = TestClient(app)
    for path in (
        "/api/email_digest",
        "/api/email-digest",
        "/api/emailDigest",
        "/api/email_digests",
        "/api/email-digests",
    ):
        r = client.get(path)
        assert r.status_code == 200, (path, r.status_code)


def test_email_digest_ui_js_handles_response():
    html = HTML
    assert "Scheduled" in html
    assert "Processed" in html
    assert "Sent" in html
