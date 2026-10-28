import uuid

from fastapi.testclient import TestClient

from app.main import app
from app.services.linked_monitor_trigger import (
    MonitorLinkExpression,
    LinkedMonitorTriggerService,
)


def test_import_linked_monitor_trigger():
    from app.main import app as _app
    paths = list(_app.openapi()["paths"].keys())
    assert "/api/linked-monitor-triggers" in paths
    assert "/api/cameras/{camera_id}/linked-monitors" in paths


def test_expression_parse_and_evaluate():
    expr = MonitorLinkExpression("1|2")
    assert expr.parse() is True
    assert expr.evaluate(scores={"1": 0, "2": 5}) is True
    assert expr.evaluate(scores={"1": 0, "2": 0}) is False
    res = expr.result(scores={"1": 2, "2": 3})
    assert res.score == 5
    assert res.success is True


def test_expression_and():
    expr = MonitorLinkExpression("1&2")
    assert expr.evaluate(scores={"1": 4, "2": 5}) is True
    assert expr.evaluate(scores={"1": 4, "2": 0}) is False


def test_service_basic():
    svc = LinkedMonitorTriggerService()
    t = svc.create(source_camera_id=1, target_camera_id=2)
    assert t.source_camera_id == 1
    assert t.target_camera_id == 2
    assert svc.trigger(1) == [2]
    assert svc.trigger(3) == []


def test_api_crud():
    client = TestClient(app)
    c1 = client.post("/api/cameras", json={"name": "cam_" + uuid.uuid4().hex[:6], "kind": "usb", "source": "0"}).json()["id"]
    c2 = client.post("/api/cameras", json={"name": "cam_" + uuid.uuid4().hex[:6], "kind": "usb", "source": "1"}).json()["id"]
    r = client.post("/api/linked-monitor-triggers", json={"source_camera_id": c1, "target_camera_id": c2})
    assert r.status_code == 201
    tid = r.json()["id"]
    assert client.get("/api/linked-monitor-triggers").status_code == 200
    assert client.get(f"/api/linked-monitor-triggers/{tid}").status_code == 200
    assert client.get(f"/api/cameras/{c1}/linked-monitors").status_code == 200
    tr = client.post(f"/api/cameras/{c1}/trigger")
    assert tr.status_code == 200
    assert c2 in tr.json().get("triggered", [])
    assert client.delete(f"/api/linked-monitor-triggers/{tid}").status_code == 204
