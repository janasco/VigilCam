import os
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

BASE = Path(__file__).resolve().parents[1]


def test_onvif_event_subscription_ui_present():
    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200
    html = r.text
    tokens = [
        'id="onvifEventSubscriptionForm"',
        'id="onvifEventSubscriptionsList"',
        'id="onvifEventSubscriptionStatus"',
        'id="onvifEventCameraId"',
        'id="onvifEventHost"',
        'id="onvifEventPort"',
        'id="onvifEventUsername"',
        'id="onvifEventPassword"',
        'id="onvifEventsUrl"',
        'id="onvifPullPoint"',
        'id="onvifEventSubscriptionId"',
        'id="onvifEventXml"',
        'id="onvifEventResult"',
        'loadOnvifEventSubscriptions',
        'subscribeOnvifEvent',
        'pullOnvifEvents',
        'unsubscribeOnvifEvent',
        'startOnvifEventSubscription',
        'stopOnvifEventSubscription',
        'clearOnvifEventSubscriptions',
        '/api/onvif/event-subscription',
    ]
    for t in tokens:
        assert t in html, t


def test_onvif_event_subscription_ui_fields():
    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200
    html = r.text
    for t in ['name="camera_id"', 'name="ip"', 'name="port"', 'name="username"', 'name="password"', 'name="events_url"', 'name="pull_point"', 'name="xml"', 'name="id"']:
        assert t in html, t


def test_onvif_event_subscription_endpoints_available():
    client = TestClient(app)
    for path in (
        "/api/onvif/event-subscription",
        "/api/onvif/event-subscriptions",
        "/api/onvif/event_subscription",
    ):
        r = client.get(path)
        assert r.status_code == 200, (path, r.status_code)
