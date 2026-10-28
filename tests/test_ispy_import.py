import asyncio

from fastapi.testclient import TestClient


ISPY_XML = """<?xml version="1.0" encoding="utf-8"?>
<objects>
  <cameras>
    <camera id="1" name="Front Door" description="Entry" directory="video\\FrontDoor" ptz="-1" resolution="1920x1080">
      <settings>
        <sourceindex>2</sourceindex>
        <videosourcestring>rtsp://admin:pw@192.168.1.10:554/live</videosourcestring>
        <login>admin</login>
        <password>pw</password>
        <active>true</active>
        <framerate>15</framerate>
        <maxframerate>15</maxframerate>
        <accessgroups>Outdoor</accessgroups>
      </settings>
      <detector type="TwoFrames" recordondetect="true" recordonalert="false" sensitivity="70">
        <motionzones>
          <zone left="0" top="0" width="320" height="240" />
        </motionzones>
      </detector>
      <recorder bufferseconds="5" inactiverecord="10" maxrecordtime="3600" timelapse="10" timelapseframes="100" />
      <alerts minimuminterval="30" objectcountalert="0" active="true" mode="Sound" />
      <notifications emailgrabinterval="60" sendemail="false" sendsms="false" />
      <schedule active="true" />
    </camera>
    <camera id="2" name="Gate" description="" directory="video\\Gate" ptz="-5" resolution="1280x720">
      <settings>
        <sourceindex>9</sourceindex>
        <videosourcestring>http://192.168.1.11:80/onvif/device_service</videosourcestring>
        <onvifident>http://192.168.1.11:80/onvif/device_service</onvifident>
        <login>admin</login>
        <password>pw2</password>
        <active>true</active>
        <maxframerate>10</maxframerate>
        <namevaluesettings>profilename=0,use=FFMPEG</namevaluesettings>
        <onvif>
          <rtspport>554</rtspport>
          <timeout>15</timeout>
        </onvif>
      </settings>
      <detector type="None" recordondetect="false" recordonalert="false" sensitivity="50" />
      <recorder bufferseconds="5" inactiverecord="10" maxrecordtime="3600" timelapse="10" timelapseframes="100" />
      <alerts minimuminterval="30" objectcountalert="0" active="false" mode="Sound" />
      <notifications emailgrabinterval="60" sendemail="false" sendsms="false" />
      <schedule active="false" />
    </camera>
    <camera id="3" name="Clone: Front Door" description="" directory="video\\Clone" ptz="-1">
      <settings>
        <sourceindex>10</sourceindex>
        <videosourcestring>1</videosourcestring>
        <active>true</active>
      </settings>
      <detector type="TwoFrames" recordondetect="false" recordonalert="false" sensitivity="50" />
      <recorder bufferseconds="5" inactiverecord="10" maxrecordtime="3600" timelapse="10" timelapseframes="100" />
      <alerts minimuminterval="30" objectcountalert="0" active="false" mode="Sound" />
      <notifications emailgrabinterval="60" sendemail="false" sendsms="false" />
      <schedule active="true" />
    </camera>
  </cameras>
  <microphones />
  <floorplans />
  <remotecommands/>
</objects>"""


def test_ispy_import_imports():
    import app.services.ispy_import  # noqa: F401
    import app.api.ispy_import  # noqa: F401
    from app.services.ispy_import import import_ispy_db, preview_ispy_db  # noqa: F401
    from app.api.ispy_import import router  # noqa: F401


def test_ispy_import_paths_present():
    from app.main import app

    paths = list(app.openapi()["paths"].keys())
    assert any("ispy-import" in p or "ispy_import" in p for p in paths)
    assert any(p.endswith("/ispy-import/import") or p.endswith("/ispy_import/import") for p in paths)
    assert any(p.endswith("/ispy-import/preview") or p.endswith("/ispy_import/preview") for p in paths)
    assert any(p.endswith("/ispy-import/parse") or p.endswith("/ispy_import/parse") for p in paths)


def test_extract_envelopes():
    from app.services import ispy_import as svc

    assert len(svc.extract_cameras(ISPY_XML)) == 3
    assert len(svc.extract_cameras({"xml": ISPY_XML})) == 3
    assert len(svc.extract_cameras(ISPY_XML.encode("utf-8"))) == 3
    assert len(svc.extract_cameras(svc.extract_cameras(ISPY_XML))) == 3
    assert svc.extract_cameras(None) == []
    assert svc.extract_cameras("") == []
    assert svc.extract_cameras("<objects><cameras/></objects>") == []


def test_normalize_ispy_shapes():
    from app.services import ispy_import as svc

    raws = svc.extract_cameras(ISPY_XML)
    front = svc.normalize_ispy_camera(raws[0], 0)
    assert front["name"] == "Front Door"
    assert front["source"] == "rtsp://admin:pw@192.168.1.10:554/live"
    assert front["username"] == "admin"
    assert front["password"] == "pw"
    assert front["ip_address"] == "192.168.1.10"
    assert front["group"] == "Outdoor"
    assert front["kind"] == "ip"
    assert front["enabled"] is True
    assert front["width"] == 1920
    assert front["height"] == 1080
    assert front["fps"] == 15
    assert front["ispy_id"] == 1
    assert front["ispy_sourceindex"] == 2
    assert front["motion_zones"] == [{"left": 0, "top": 0, "width": 320, "height": 240}]

    gate = svc.normalize_ispy_camera(raws[1], 1)
    assert gate["name"] == "Gate"
    assert gate["kind"] == "onvif"
    assert gate["ip_address"] == "192.168.1.11"
    assert gate["onvif_port"] == 80
    assert gate["detection_enabled"] is False  # detector type None

    clone = svc.normalize_ispy_camera(raws[2], 2)
    assert clone["ispy_clone_of"] == 1
    assert clone["source"] is None
    assert not svc.validate_camera(raws[2])
    assert svc.validate_camera(raws[0])
    assert not svc.validate_camera({"name": "NoSource"})

    payload = svc.camera_to_camera_payload(raws[0])
    assert payload.name == "Front Door"
    assert "192.168.1.10" in payload.source

    # Credentials embedded in URL.
    embedded = svc.normalize_ispy_camera(
        {"name": "Emb", "settings": {"videosourcestring": "rtsp://u:p@10.0.0.5/live"}}
    )
    assert embedded["username"] == "u"
    assert embedded["password"] == "p"
    assert embedded["ip_address"] == "10.0.0.5"

    # USB moniker maps to usb kind.
    usb = svc.normalize_ispy_camera(
        {"name": "USB", "settings": {"sourceindex": "3",
                                     "videosourcestring": "@device:pnp:\\\\usb#vid_1234"}}
    )
    assert usb["kind"] == "usb"
    assert svc.validate_camera(usb)


def test_import_ispy_db_service():
    from app.services import ispy_import as svc

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

    result = asyncio.run(svc.import_ispy_db(FakeDB(), ISPY_XML))
    # 2 cameras imported, 1 clone skipped.
    assert result["count"] == 2
    assert len(result["imported"]) == 2
    assert result["total_errors"] == 0
    assert result["total_skipped"] == 1
    names = {row["name"] for row in result["imported"]}
    assert {"Front Door", "Gate"} <= names

    bad = "<objects><cameras><camera id='9' name='NoSource'><settings><sourceindex>2</sourceindex></settings></camera></cameras></objects>"
    result2 = asyncio.run(svc.import_ispy_db(FakeDB(), bad))
    assert result2["count"] == 0
    assert len(result2["errors"]) == 1


def test_ispy_import_endpoints(monkeypatch):
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
        r = client.get("/api/ispy-import/template")
        assert r.status_code == 200, r.text
        assert "xml" in r.json()

        r2 = client.post("/api/ispy-import/parse", json={"xml": ISPY_XML})
        assert r2.status_code == 200, r2.text
        assert r2.json()["count"] == 3
        assert r2.json()["valid"] == 2

        r3 = client.post("/api/ispy-import/preview", json={"xml": ISPY_XML})
        assert r3.status_code == 200, r3.text
        assert isinstance(r3.json(), list)
        assert r3.json()[0]["name"] == "Front Door"

        r4 = client.post("/api/ispy-import/import", json={"xml": ISPY_XML})
        assert r4.status_code == 200, r4.text
        data = r4.json()
        assert data["count"] == 2
        assert len(data["cameras"]) == 2
        assert data["total_skipped"] == 1

        r5 = client.post("/api/ispy_import/import", json={"xml": ISPY_XML})
        assert r5.status_code == 200, r5.text
        assert r5.json()["count"] == 2
    finally:
        app.dependency_overrides.pop(db_session, None)
