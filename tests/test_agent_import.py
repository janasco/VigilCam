import asyncio
import json

from fastapi.testclient import TestClient


AGENT_DEVICES = {
    "devices": [
        {
            "oid": 1,
            "ot": 2,
            "name": "Front Door",
            "group": "Outdoor",
            "enabled": True,
            "settings": {
                "videosource": "rtsp://admin:pw@192.168.1.10:554/live",
                "username": "admin",
                "password": "pw",
            },
        },
        {
            "oid": 2,
            "ot": 2,
            "name": "Back Yard",
            "location": "Home",
            "settings": {"url": "onvif://192.168.1.11:80"},
        },
        {
            "oid": 3,
            "ot": 1,
            "name": "Porch Mic",
            "settings": {"url": "rtsp://192.168.1.12/audio"},
        },
    ]
}

MEDIA_OBJECTS = {
    "MediaObjects": [
        {
            "id": 7,
            "ot": 2,
            "name": "Gate",
            "group": "Perimeter",
            "state": "ok",
            "type": "ONVIF",
            "settings": {
                "mainurl": "rtsp://192.168.1.20:554/h264",
                "onvifport": 80,
                "width": 1920,
                "height": 1080,
            },
        }
    ]
}


def test_agent_import_imports():
    import app.services.agent_import  # noqa: F401
    import app.api.agent_import  # noqa: F401
    from app.services.agent_import import import_agent_db, preview_agent_db  # noqa: F401
    from app.api.agent_import import router  # noqa: F401


def test_agent_import_paths_present():
    from app.main import app

    paths = list(app.openapi()["paths"].keys())
    assert any("agent-import" in p or "agent_import" in p for p in paths)
    assert any(p.endswith("/agent-import/import") or p.endswith("/agent_import/import") for p in paths)
    assert any(p.endswith("/agent-import/preview") or p.endswith("/agent_import/preview") for p in paths)
    assert any(p.endswith("/agent-import/parse") or p.endswith("/agent_import/parse") for p in paths)


def test_extract_envelopes():
    from app.services import agent_import as svc

    assert len(svc.extract_devices(AGENT_DEVICES)) == 3
    assert len(svc.extract_devices(MEDIA_OBJECTS)) == 1
    # Single device dict.
    single = {"oid": 9, "ot": 2, "name": "Solo", "settings": {"url": "rtsp://9.9.9.9/live"}}
    assert len(svc.extract_devices(single)) == 1
    # JSON string.
    assert len(svc.extract_devices(json.dumps(AGENT_DEVICES))) == 3
    # Plain list.
    assert len(svc.extract_devices(AGENT_DEVICES["devices"])) == 3
    assert svc.extract_devices(None) == []


def test_normalize_agent_shapes():
    from app.services import agent_import as svc

    raws = svc.extract_devices(AGENT_DEVICES)
    front = svc.normalize_agent_device(raws[0], 0)
    assert front["name"] == "Front Door"
    assert front["source"] == "rtsp://admin:pw@192.168.1.10:554/live"
    assert front["username"] == "admin"
    assert front["password"] == "pw"
    assert front["ip_address"] == "192.168.1.10"
    assert front["group"] == "Outdoor"
    assert front["kind"] == "ip"
    assert front["enabled"] is True

    back = svc.normalize_agent_device(raws[1], 1)
    assert back["kind"] == "onvif"
    assert back["ip_address"] == "192.168.1.11"
    assert back["group"] == "Home"  # location fallback

    gate = svc.normalize_agent_device(svc.extract_devices(MEDIA_OBJECTS)[0])
    assert gate["name"] == "Gate"
    assert gate["kind"] == "onvif"
    assert "192.168.1.20" in gate["source"]
    assert gate["onvif_port"] == 80

    mic = svc.normalize_agent_device(raws[2], 2)
    assert mic["agent_microphone"] is True
    assert not svc.validate_device(raws[2])
    assert svc.validate_device(raws[0])
    assert not svc.validate_device({"name": "NoSource"})

    payload = svc.device_to_camera_payload(raws[0])
    assert payload.name == "Front Door"
    assert "192.168.1.10" in payload.source

    # Credentials embedded in URL.
    embedded = svc.normalize_agent_device({"name": "Emb", "settings": {"url": "rtsp://u:p@10.0.0.5/live"}})
    assert embedded["username"] == "u"
    assert embedded["password"] == "p"
    assert embedded["ip_address"] == "10.0.0.5"

    # IP-only device synthesizes a source.
    iponly = svc.normalize_agent_device({"name": "IPOnly", "settings": {"ip": "10.0.0.6"}})
    assert iponly["source"] and "10.0.0.6" in iponly["source"]
    assert svc.validate_device(iponly)


def test_import_agent_db_service():
    from app.services import agent_import as svc

    made = []

    class FakeResult:
        def scalars(self):
            class S:
                def first(self_inner):
                    return None

                def all(self_inner):
                    return list(made)
            return S()

        def all(self):
            return []

    class FakeDB:
        async def execute(self, *a, **k):
            return FakeResult()

        def add(self, obj):
            obj.id = len(made) + 1
            made.append(obj)

        async def commit(self):
            return None

        async def refresh(self, obj):
            return None

        async def rollback(self):
            return None

    result = asyncio.run(svc.import_agent_db(FakeDB(), AGENT_DEVICES))
    # 2 cameras imported, 1 microphone skipped.
    assert result["count"] == 2
    assert len(result["imported"]) == 2
    assert result["total_errors"] == 0
    assert result["total_skipped"] == 1
    names = {row["name"] for row in result["imported"]}
    assert {"Front Door", "Back Yard"} <= names

    bad = {"devices": [{"name": "NoSource", "ot": 2}]}
    result2 = asyncio.run(svc.import_agent_db(FakeDB(), bad))
    assert result2["count"] == 0
    assert len(result2["errors"]) == 1


def test_agent_import_endpoints(monkeypatch):
    from app.main import app
    from app.api.deps import db_session

    made = []

    class FakeResult:
        def scalars(self):
            class S:
                def first(self_inner):
                    return None

                def all(self_inner):
                    return list(made)
            return S()

        def all(self):
            return []

    class FakeDB:
        async def execute(self, *a, **k):
            return FakeResult()

        def add(self, obj):
            from app.models.entities import Camera

            obj.id = 99
            made.append(obj)

        async def commit(self):
            return None

        async def refresh(self, obj):
            return None

        async def rollback(self):
            return None

    async def fake_session():
        yield FakeDB()

    app.dependency_overrides[db_session] = fake_session
    try:
        client = TestClient(app)
        r = client.get("/api/agent-import/template")
        assert r.status_code == 200, r.text
        assert "devices" in r.json()

        r2 = client.post("/api/agent-import/parse", json=AGENT_DEVICES)
        assert r2.status_code == 200, r2.text
        assert r2.json()["count"] == 3
        assert r2.json()["valid"] == 2

        r3 = client.post("/api/agent-import/preview", json=AGENT_DEVICES)
        assert r3.status_code == 200, r3.text
        assert isinstance(r3.json(), list)
        assert r3.json()[0]["name"] == "Front Door"

        r4 = client.post("/api/agent-import/import", json=AGENT_DEVICES)
        assert r4.status_code == 200, r4.text
        data = r4.json()
        assert data["count"] == 2
        assert len(data["cameras"]) == 2
        assert data["total_skipped"] == 1

        r5 = client.post("/api/agent_import/import", json=MEDIA_OBJECTS)
        assert r5.status_code == 200, r5.text
        assert r5.json()["count"] == 1
    finally:
        app.dependency_overrides.pop(db_session, None)
