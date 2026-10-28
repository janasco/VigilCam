import os

from fastapi.testclient import TestClient

from app.main import app
from app.services import onvif_event_subscription as es


SAMPLE_PULL = """
<PullMessagesResponse>
  <wsnt:NotificationMessage>
    <wsnt:Topic>tns1:RuleEngine/CellMotionDetector/Motion</wsnt:Topic>
    <wsnt:Message>
      <tt:Message UtcTime="2026-07-04T18:00:00Z">
        <tt:Data>
          <tt:SimpleItem Name="IsMotion" Value="true"/>
        </tt:Data>
      </tt:Message>
    </wsnt:Message>
  </wsnt:NotificationMessage>
</PullMessagesResponse>
"""


def test_onvif_event_subscription_imports():
    import app.services.onvif_event_subscription  # noqa: F401
    import app.api.onvif_event_subscription  # noqa: F401
    from app.services.onvif_event_subscription import (
        OnvifEventSubscriptionService,
        OnvifEventSubscriptionRuntime,
        OnvifEventSubscriptionManager,
        OnvifEventSubscription,
    )  # noqa: F401


def test_onvif_event_subscription_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert any("subscription" in p for p in paths)
    assert any("subscribe" in p for p in paths)


def test_service_basic():
    es.clear()
    sub = es.subscribe(camera_id=1, ip="10.0.0.1", username="admin", password="pass")
    assert sub is not None
    assert sub.status == "active"
    assert sub.events_url is not None
    evs = es.pull_messages(sub.id, SAMPLE_PULL)
    assert len(evs) == 1
    st = es.status(sub.id)
    assert st["event_count"] == 1
    assert es.unsubscribe(sub.id) is True
    es.clear()


def test_service_aliases():
    es.clear()
    svc = es.OnvifEventSubscriptionService()
    sub = svc.create_subscription(ip="10.0.0.2")
    assert sub is not None
    svc.start(sub.id)
    assert svc.get(sub.id) is not None
    svc.stop(sub.id)
    svc.clear()


def test_api_flow():
    client = TestClient(app)
    es.clear()
    r = client.post("/api/onvif/event-subscription/subscribe", json={"camera_id": 2, "ip": "10.0.0.3"})
    assert r.status_code == 200
    sid = r.json()["id"]
    r2 = client.post("/api/onvif/event-subscription/pull", json={"id": sid, "xml": SAMPLE_PULL})
    assert r2.status_code == 200
    assert r2.json()["count"] == 1
    r3 = client.get("/api/onvif/event-subscription")
    assert r3.status_code == 200
    assert isinstance(r3.json(), list)
    r4 = client.delete(f"/api/onvif/event-subscription/{sid}")
    assert r4.status_code == 200
    es.clear()