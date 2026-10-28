from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

BASE = Path(__file__).resolve().parents[1]
HTML = (BASE / "app" / "templates" / "index.html").read_text(encoding="utf-8")


def test_web_push_ui_present():
    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200
    html = r.text
    for token in [
        'id="webPushForm"',
        'id="webPushStatus"',
        'id="webPushSubscribeButton"',
        'id="webPushUnsubscribeButton"',
        'id="webPushVapidPublicKey"',
        'id="webPushSubscriptions"',
        'loadWebPush',
        'subscribeWebPush',
        'unsubscribeWebPush',
        'requestNotificationPermission',
        'registerServiceWorker',
        'urlBase64ToUint8Array',
        '/api/web_push',
        '/api/web_push/subscribe',
        '/api/web_push/unsubscribe',
        '/api/web_push/vapid-public-key',
        'Notification.permission',
        'serviceWorker',
        'PushManager',
        'applicationServerKey',
    ]:
        assert token in html, token


def test_web_push_ui_service_worker_file():
    sw = (BASE / "app" / "static" / "sw.js").read_text(encoding="utf-8")
    assert "push" in sw
    assert "showNotification" in sw