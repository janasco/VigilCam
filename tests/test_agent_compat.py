"""Tests for the Agent DVR compatibility layer."""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app.main import app
from app.services import agent_compat as svc


def _make_camera(client: TestClient) -> int:
    name = "agent_" + uuid.uuid4().hex[:8]
    r = client.post("/api/cameras", json={"name": name, "kind": "usb", "source": "0"})
    assert r.status_code == 201, r.text
    return int(r.json()["id"])


def test_agent_compat_imports():
    import app.api.agent_compat as api  # noqa: F401
    import app.services.agent_compat as service  # noqa: F401

    assert hasattr(api, "router")
    for fn in (
        "is_armed",
        "set_armed",
        "to_agent_device",
        "build_status",
        "build_system_stats",
        "get_profiles",
        "set_profile_index",
        "count_events_since",
    ):
        assert hasattr(service, fn), fn


def test_agent_compat_paths_registered():
    paths = set(app.openapi()["paths"].keys())
    for p in (
        "/command/ping",
        "/command/getStatus",
        "/command/getObjects",
        "/command/getObject",
        "/command/arm",
        "/command/disarm",
        "/command/switchOn",
        "/command/switchOff",
        "/command/allOn",
        "/command/allOff",
        "/command/record",
        "/command/recordStop",
        "/command/triggerRecord",
        "/command/snapshot",
        "/grab.jpg",
        "/alerts.json",
        "/eventcounts.json",
        "/command/alert",
        "/command/detect",
        "/command/getSystemStats",
        "/command/getProfiles",
        "/command/setProfile",
        "/q/getEvents",
        "/ptz.cgi",
        "/command/ptzpreset",
    ):
        assert p in paths, p


def test_ping_and_profiles():
    client = TestClient(app)
    r = client.get("/command/ping")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}

    r = client.get("/command/getProfiles")
    assert r.status_code == 200
    body = r.json()
    assert [p["name"] for p in body["profiles"]] == ["Home", "Away", "Night"]

    r = client.get("/command/setProfile", params={"ind": 1})
    assert r.status_code == 200
    assert r.json()["name"] == "Away"

    r = client.get("/command/setProfile", params={"ind": 99})
    assert r.status_code == 400

    # restore default for other tests
    client.get("/command/setProfile", params={"ind": 0})


def test_arm_disarm_and_status():
    client = TestClient(app)
    cid = _make_camera(client)
    try:
        assert client.get("/command/disarm").status_code == 200
        st = client.get("/command/getStatus").json()
        assert st["armed"] is False
        assert st["devices"] >= 1
        assert "version" in st and "timestamp" in st

        assert client.get("/command/arm").status_code == 200
        assert client.get("/command/getStatus").json()["armed"] is True
    finally:
        client.get("/command/disarm")
        client.delete(f"/api/cameras/{cid}")


def test_get_objects_roundtrip():
    client = TestClient(app)
    cid = _make_camera(client)
    try:
        objs = client.get("/command/getObjects").json()
        assert any(d["oid"] == cid and d["ot"] == 2 for d in objs["devices"])

        one = client.get("/command/getObject", params={"oid": cid, "ot": 2}).json()
        assert one["device"]["oid"] == cid
        assert one["device"]["name"].startswith("agent_")

        bad = client.get("/command/getObject", params={"oid": 999999999, "ot": 2})
        assert bad.status_code == 404

        mic = client.get("/command/getObject", params={"oid": cid, "ot": 1})
        assert mic.status_code == 404
    finally:
        client.delete(f"/api/cameras/{cid}")


def test_switch_on_off_and_all():
    client = TestClient(app)
    cid = _make_camera(client)
    try:
        r = client.get("/command/switchOff", params={"oid": cid, "ot": 2})
        assert r.status_code == 200
        assert r.json()["enabled"] is False

        objs = client.get("/command/getObjects").json()
        assert next(d for d in objs["devices"] if d["oid"] == cid)["enabled"] is False

        r = client.get("/command/switchOn", params={"oid": cid, "ot": 2})
        assert r.json()["enabled"] is True
    finally:
        client.delete(f"/api/cameras/{cid}")


def test_alert_detect_counts():
    client = TestClient(app)
    cid = _make_camera(client)
    try:
        r = client.get(
            "/command/alert",
            params={"oid": cid, "ot": 2, "tag": "persontest", "message": "hello"},
        )
        assert r.status_code == 200
        assert r.json()["eventId"]

        r = client.get("/command/detect", params={"oid": cid, "ot": 2})
        assert r.status_code == 200

        alerts = client.get("/alerts.json").json()["alerts"]
        assert any(a["oid"] == cid for a in alerts)

        counts = client.get(
            "/eventcounts.json", params={"oid": cid, "ot": 2, "secs": 3600}
        ).json()
        assert counts["count"] >= 2
    finally:
        client.delete(f"/api/cameras/{cid}")


def test_record_stop_and_get_events():
    client = TestClient(app)
    cid = _make_camera(client)
    try:
        r = client.get("/command/record", params={"oid": cid, "ot": 2})
        assert r.status_code == 200

        r = client.get("/command/recordStop", params={"oid": cid, "ot": 2})
        assert r.status_code == 200
        assert r.json()["stopped"] >= 1

        events = client.get("/q/getEvents", params={"oid": cid, "ot": 2}).json()
        assert isinstance(events, list)
        assert any(e["oid"] == cid for e in events)
    finally:
        client.delete(f"/api/cameras/{cid}")


def test_snapshot_and_grab_are_jpeg():
    client = TestClient(app)
    cid = _make_camera(client)
    try:
        r = client.get("/command/snapshot", params={"oid": cid})
        assert r.status_code == 200

        r = client.get("/grab.jpg", params={"oid": cid})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("image/jpeg")
        assert r.content[:2] == b"\xff\xd8"
    finally:
        client.delete(f"/api/cameras/{cid}")


def test_system_stats_and_misc():
    client = TestClient(app)
    stats = client.get("/command/getSystemStats").json()
    for key in ("cpu_used", "ram_free", "disk_free", "cpup", "ramp", "diskp"):
        assert key in stats, key

    assert client.get("/command/checkRemote").json() == {"status": "offline"}
    assert client.get("/q/getStreamingStatus").json() == {"servers": []}
    bc = client.get("/command/broadcast", params={"text": "hi"})
    assert bc.status_code == 200


def test_ptz_validation():
    client = TestClient(app)
    cid = _make_camera(client)
    try:
        r = client.get("/ptz.cgi", params={"oid": cid})
        assert r.status_code == 400

        r = client.get("/ptz.cgi", params={"oid": cid, "dir": 5})
        assert r.status_code == 200
        assert r.json()["dir"] == 5

        r = client.get("/ptz.cgi", params={"oid": cid, "dir": 99})
        assert r.status_code == 400

        r = client.get(
            "/command/ptzpreset", params={"oid": cid, "ot": 2, "preset": "Gate"}
        )
        assert r.status_code == 200

        r = client.get("/ptz.cgi", params={"oid": 999999999, "dir": 5})
        assert r.status_code == 404
    finally:
        client.delete(f"/api/cameras/{cid}")


def test_service_helpers_unit():
    svc.reset_state()
    assert svc.is_armed() is False
    svc.set_armed(True)
    assert svc.is_armed() is True
    svc.reset_state()

    assert [p["index"] for p in svc.get_profiles()] == [0, 1, 2]
    try:
        svc.set_profile_index(42)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")
    try:
        svc.validate_ot(3)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")
