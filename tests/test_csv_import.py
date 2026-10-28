import asyncio

from fastapi.testclient import TestClient


SAMPLE_CSV = """name,kind,source,username,password,onvif_host,ip_address,onvif_port
Front Door,onvif,onvif://192.168.1.10:80,admin,pw,192.168.1.10,192.168.1.10,80
Back Yard,ip,rtsp://192.168.1.11:554/live,,,
"""


def test_csv_import_imports():
    import app.services.csv_import  # noqa: F401
    import app.api.csv_import  # noqa: F401
    from app.services.csv_import import import_csv_db, preview_csv_db  # noqa: F401
    from app.api.csv_import import router  # noqa: F401


def test_csv_import_paths_present():
    from app.main import app

    paths = list(app.openapi()["paths"].keys())
    assert any("csv-import" in p or "csv_import" in p for p in paths)
    assert any(p.endswith("/csv-import/import") or p.endswith("/csv_import/import") for p in paths)
    assert any(p.endswith("/csv-import/preview") or p.endswith("/csv_import/preview") for p in paths)


def test_parse_and_normalize():
    from app.services import csv_import as svc

    rows = svc.parse_csv(SAMPLE_CSV)
    assert len(rows) == 2
    normed = [svc.normalize_row(r) for r in rows]
    assert normed[0]["name"] == "Front Door"
    assert normed[0]["kind"] == "onvif"
    assert normed[0]["ip_address"] == "192.168.1.10"
    assert svc.validate_row(normed[0])
    assert svc.validate_row(normed[1])

    payload = svc.row_to_camera_payload(normed[0])
    assert payload.name == "Front Door"
    assert payload.kind == "onvif"
    assert "192.168.1.10" in payload.source

    # Header aliases.
    aliased = svc.normalize_row({"camera_name": "Side", "url": "rtsp://1.2.3.4/live", "user": "admin"})
    assert aliased["name"] == "Side"
    assert aliased["source"] == "rtsp://1.2.3.4/live"
    assert aliased["username"] == "admin"

    # Invalid rows.
    assert not svc.validate_row({"name": "NoSource"})
    assert not svc.validate_row({"source": "rtsp://x/live"})


def test_template_and_roundtrip():
    from app.services import csv_import as svc

    template = svc.build_template_csv()
    assert "name" in template.splitlines()[0]
    assert "source" in template.splitlines()[0]
    rows = svc.parse_csv(template)
    assert len(rows) >= 1
    assert svc.validate_row(rows[0])

    exported = svc.cameras_to_csv([{"name": "A", "kind": "ip", "source": "rtsp://1.1.1.1/live"}])
    assert "name" in exported.splitlines()[0]
    reparsed = svc.parse_csv(exported)
    assert reparsed[0]["name"] == "A"


def test_import_csv_db_service(monkeypatch):
    from app.services import csv_import as svc
    from app.models.entities import Camera

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

    result = asyncio.run(svc.import_csv_db(FakeDB(), SAMPLE_CSV))
    assert result["count"] == 2
    assert len(result["imported"]) == 2
    assert result["total_errors"] == 0

    # Invalid rows are reported, not created.
    bad = "name,kind,source\nOnlyName,ip,\n"
    result2 = asyncio.run(svc.import_csv_db(FakeDB(), bad))
    assert result2["count"] == 0
    assert len(result2["errors"]) == 1


def test_csv_import_endpoints(monkeypatch):
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
        r = client.get("/api/csv-import/template")
        assert r.status_code == 200, r.text
        assert "name" in r.text.splitlines()[0]

        r2 = client.post("/api/csv-import/parse", json={"csv_text": SAMPLE_CSV})
        assert r2.status_code == 200, r2.text
        assert r2.json()["count"] == 2
        assert r2.json()["valid"] == 2

        r3 = client.post("/api/csv-import/preview", json={"csv_text": SAMPLE_CSV})
        assert r3.status_code == 200, r3.text
        assert isinstance(r3.json(), list)
        assert r3.json()[0]["name"] == "Front Door"

        r4 = client.post("/api/csv-import/import", json={"csv_text": SAMPLE_CSV})
        assert r4.status_code == 200, r4.text
        data = r4.json()
        assert data["count"] == 2
        assert len(data["cameras"]) == 2

        r5 = client.post("/api/csv_import/import", json={"rows": [{"name": "Solo", "source": "rtsp://9.9.9.9/live"}]})
        assert r5.status_code == 200, r5.text

        r6 = client.post(
            "/api/csv-import/import-file",
            files={"file": ("cameras.csv", SAMPLE_CSV, "text/csv")},
        )
        assert r6.status_code == 200, r6.text
    finally:
        app.dependency_overrides.pop(db_session, None)
