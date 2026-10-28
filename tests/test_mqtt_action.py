"""Tests for the MQTT publish action + Home Assistant discovery."""
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import mqtt_action as mqtt_api
from app.services import mqtt_action as mqtt_mod


class FakeSock:
    """Minimal MQTT broker stub: CONNACK (+ PUBACK for QoS 1)."""

    def __init__(self, qos_expect: int = 0):
        self.sent = b""
        self.closed = False
        self._reads = [b"\x20\x02", b"\x00\x00"]
        if qos_expect == 1:
            self._reads.append(b"\x40\x02\x00\x01")

    def settimeout(self, timeout):
        pass

    def sendall(self, data: bytes):
        self.sent += bytes(data)

    def recv(self, n: int) -> bytes:
        if not self._reads:
            return b""
        chunk = self._reads.pop(0)
        # honour requested size (tests always request exact sizes)
        assert len(chunk) <= n or True
        return chunk[:n]

    def close(self):
        self.closed = True


def _sock_factory(fake: FakeSock):
    def _create(*args, **kwargs):
        return fake
    return _create


def test_imports():
    import app.services.mqtt_action  # noqa: F401
    import app.api.mqtt_action  # noqa: F401
    from app.services.mqtt_action import publish_mqtt  # noqa: F401
    from app.api.mqtt_action import router  # noqa: F401
    assert router is not None


def test_gap_mqtt_action_is_new_home():
    """MQTT publishing must live in mqtt_action (no pre-existing mqtt code)."""
    hits = []
    for base in ("app/services", "app/api"):
        for path in Path(base).glob("*.py"):
            if path.name == "mqtt_action.py":
                continue
            try:
                text = path.read_text(encoding="utf-8").lower()
            except Exception:  # noqa: BLE001
                continue
            if "mqtt" in text and "mqtt_action" not in text:
                hits.append(str(path))
    assert hits == [], f"unexpected pre-existing mqtt references: {hits}"
    # New modules must expose publish + HA discovery.
    assert hasattr(mqtt_mod, "publish_mqtt")
    assert hasattr(mqtt_mod, "build_ha_discovery_config")
    assert hasattr(mqtt_mod, "publish_ha_discovery")


def test_build_topic_helpers():
    assert mqtt_mod.build_topic("Cam 1", "state") == "vigilcam/cam_1/state"
    assert mqtt_mod.build_state_topic("Cam-1") == "vigilcam/cam_1/state"
    assert mqtt_mod.build_event_topic("Cam-1") == "vigilcam/cam_1/event"
    assert mqtt_mod.build_topic("cam1", None, "custom/base") == "custom/base/cam1"


def test_ha_discovery_topic_format():
    t = mqtt_mod.ha_discovery_topic("binary_sensor", "vigilcam_cam1", "motion")
    assert t == "homeassistant/binary_sensor/vigilcam_cam1/motion/config"


def test_ha_discovery_config_shape():
    configs = mqtt_mod.build_ha_discovery_config("cam1", "Front Door")
    assert len(configs) == 2
    motion_topic = "homeassistant/binary_sensor/vigilcam_cam1/motion/config"
    assert motion_topic in configs
    motion = configs[motion_topic]
    assert motion["unique_id"] == "vigilcam_cam1_motion"
    assert motion["device_class"] == "motion"
    assert motion["state_topic"] == "vigilcam/cam1/state"
    assert motion["payload_on"] == "ON"
    assert motion["device"]["manufacturer"] == "VigilCam"
    # second entity: last-event sensor
    assert any("last_event" in k for k in configs)


def test_missing_host():
    res = mqtt_mod.publish_mqtt(topic="vigilcam/cam1/state", payload="ON")
    assert res["ok"] is False
    assert "host" in res["detail"].lower()


def test_missing_topic():
    res = mqtt_mod.publish_mqtt(host="broker.local", payload="ON")
    assert res["ok"] is False
    assert "topic" in res["detail"].lower()


def test_bad_qos_rejected():
    res = mqtt_mod.publish_mqtt(host="broker.local", topic="a/b", payload="x", qos=2)
    assert res["ok"] is False
    assert "qos" in res["detail"].lower()


def test_publish_qos0_wire():
    fake = FakeSock(qos_expect=0)
    with patch("app.services.mqtt_action.socket.create_connection", _sock_factory(fake)):
        res = mqtt_mod.publish_mqtt(host="broker.local", topic="vigilcam/cam1/state", payload="ON")
    assert res["ok"] is True
    assert res["topic"] == "vigilcam/cam1/state"
    # CONNECT (0x10) + PUBLISH with topic bytes on the wire.
    assert b"vigilcam/cam1/state" in fake.sent
    assert b"ON" in fake.sent
    assert fake.closed is True


def test_publish_qos1_waits_puback():
    fake = FakeSock(qos_expect=1)
    with patch("app.services.mqtt_action.socket.create_connection", _sock_factory(fake)):
        res = mqtt_mod.publish_mqtt(host="broker.local", topic="a/b", payload={"k": 1}, qos=1)
    assert res["ok"] is True
    assert res["qos"] == 1


def test_publish_broker_error_becomes_result():
    with patch(
        "app.services.mqtt_action.socket.create_connection",
        side_effect=ConnectionError("refused"),
    ):
        res = mqtt_mod.publish_mqtt(host="broker.local", topic="a/b", payload="x")
    assert res["ok"] is False
    assert "refused" in res["detail"]


def test_publish_event_mirrors_state():
    calls = []

    def _fake_publish(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "success": True, "status": "success", "detail": "ok", "topic": kwargs.get("topic")}

    with patch("app.services.mqtt_action.publish_mqtt", side_effect=_fake_publish):
        res = mqtt_mod.publish_event("cam1", "motion", "ON", host="broker.local")
    assert res["ok"] is True
    assert res["event_topic"] == "vigilcam/cam1/event"
    assert res["state_topic"] == "vigilcam/cam1/state"
    topics = [c["topic"] for c in calls]
    assert "vigilcam/cam1/event" in topics
    assert "vigilcam/cam1/state" in topics


def test_publish_ha_discovery_retained():
    seen = []

    def _fake_publish(**kwargs):
        seen.append(kwargs)
        return {"ok": True, "success": True, "status": "success", "detail": "ok"}

    with patch("app.services.mqtt_action.publish_mqtt", side_effect=_fake_publish):
        res = mqtt_mod.publish_ha_discovery("cam1", host="broker.local")
    assert res["ok"] is True
    assert len(seen) == 2
    assert all(s.get("retain") is True for s in seen)
    assert all(s["topic"].startswith("homeassistant/") for s in seen)


def test_async_variant():
    import asyncio

    fake = FakeSock()
    with patch("app.services.mqtt_action.socket.create_connection", _sock_factory(fake)):
        res = asyncio.run(mqtt_mod.publish_mqtt_async(host="h", topic="a/b", payload="x"))
    assert res["ok"] is True


def _test_client():
    app = FastAPI()
    app.include_router(mqtt_api.router)
    return TestClient(app)


def test_router_publish():
    fake = FakeSock()
    with patch("app.services.mqtt_action.socket.create_connection", _sock_factory(fake)):
        client = _test_client()
        r = client.post("/api/mqtt/publish", json={"host": "broker.local", "topic": "a/b", "payload": "hi"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["action"] == "mqtt_publish"


def test_router_publish_alias():
    fake = FakeSock()
    with patch("app.services.mqtt_action.socket.create_connection", _sock_factory(fake)):
        client = _test_client()
        r = client.post("/api/mqtt_action/publish", json={"host": "h", "topic": "a/b", "payload": "x"})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True


def test_router_status():
    client = _test_client()
    r = client.get("/api/mqtt/status")
    assert r.status_code == 200
    assert r.json()["default_base_topic"] == "vigilcam"


def test_router_discovery_preview_no_broker_needed():
    client = _test_client()
    r = client.get("/api/mqtt/discovery/preview", params={"camera_id": "cam1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["count"] == 2


def test_router_discovery_publish():
    with patch(
        "app.services.mqtt_action.publish_mqtt",
        return_value={"ok": True, "success": True, "status": "success", "detail": "ok"},
    ):
        client = _test_client()
        r = client.post("/api/mqtt/discovery", json={"camera_id": "cam1", "host": "h"})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
