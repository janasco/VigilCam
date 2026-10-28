from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

BASE = Path(__file__).resolve().parents[1]
HTML = (BASE / "app" / "templates" / "index.html").read_text(encoding="utf-8")


def test_email_digest_scheduler_ui_present():
    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200
    html = r.text
    for token in [
        'id="emailDigestSchedulerForm"',
        'id="emailDigestScheduler"',
        'id="emailDigestSchedulerStatus"',
        'id="set-email-digest-scheduler"',
        'id="set-emailDigestScheduler"',
        'id="set-digest-scheduler"',
        'id="set-digestScheduler"',
        'loadEmailDigestScheduler',
        'saveEmailDigestScheduler',
        'startEmailDigestScheduler',
        'stopEmailDigestScheduler',
        'EmailDigestScheduler',
        'emailDigestScheduler',
    ]:
        assert token in html, token
