from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.edl_export import router as edl_export_router
from app.services import bookmarks
from app.services import edl_export as svc


def _make_client():
    app = FastAPI()
    app.include_router(edl_export_router)
    return TestClient(app)


def test_edl_export_import():
    import app.api.edl_export  # noqa: F401
    import app.services.edl_export  # noqa: F401
    from app.services.edl_export import (  # noqa: F401
        EdlExportService,
        build_edl,
        edl_export_service,
        fetch_bookmarks,
        seconds_to_timecode,
    )
    from app.api.edl_export import router  # noqa: F401
    assert edl_export_router is not None
    assert hasattr(svc, "build_edl")
    assert hasattr(svc, "seconds_to_timecode")
    assert hasattr(svc, "fetch_bookmarks")


def test_seconds_to_timecode():
    assert svc.seconds_to_timecode(0, fps=25) == "00:00:00:00"
    assert svc.seconds_to_timecode(0, fps=30) == "00:00:00:00"
    assert svc.seconds_to_timecode(61, fps=25) == "00:01:01:00"
    assert svc.seconds_to_timecode(3661, fps=25) == "01:01:01:00"
    assert svc.seconds_to_timecode(3661, fps=30) == "01:01:01:00"
    # Fractional: 1.04s @25fps = 26 frames = 1s + 1f.
    assert svc.seconds_to_timecode(1.04, fps=25) == "00:00:01:01"
    # 12s exact.
    assert svc.seconds_to_timecode(12, fps=25) == "00:00:12:00"
    # Negative clamps to zero.
    assert svc.seconds_to_timecode(-5, fps=25) == "00:00:00:00"
    # Aliases agree.
    assert svc.to_timecode(61, fps=25) == svc.seconds_to_timecode(61, fps=25)


def test_build_edl_basic():
    bookmarks.clear()
    bm1 = bookmarks.create_bookmark(recording_id=1, camera_id=2, offset_seconds=12.5, label="doorbell")
    bm2 = bookmarks.create_bookmark(recording_id=1, camera_id=2, offset_seconds=40, label="motion")
    text = svc.build_edl([bm1, bm2], title="Test EDL", fps=25, duration=2.0)
    assert "TITLE: Test EDL" in text
    assert "FCM: NON-DROP FRAME" in text
    assert "001" in text
    assert "002" in text
    assert "doorbell" in text
    assert "motion" in text
    assert "FROM CLIP NAME:" in text
    # Source timecodes present (12.5s @25fps).
    assert "00:00:12:" in text
    # Record timeline starts at 01:00:00:00.
    assert "01:00:00:00" in text


def test_build_edl_empty_and_sorting():
    bookmarks.clear()
    text = svc.build_edl([], title="Empty")
    assert "TITLE: Empty" in text
    assert "FCM: NON-DROP FRAME" in text
    # Out-of-order inputs are sorted by offset.
    b_late = bookmarks.create_bookmark(offset_seconds=100, label="late")
    b_early = bookmarks.create_bookmark(offset_seconds=5, label="early")
    ordered = svc.build_edl([b_late, b_early], title="Sorted")
    assert ordered.index("early") < ordered.index("late")


def test_fetch_bookmarks_filter():
    bookmarks.clear()
    bookmarks.create_bookmark(recording_id=11, label="a", offset_seconds=1)
    bookmarks.create_bookmark(recording_id=22, label="b", offset_seconds=2)
    all_items = svc.fetch_bookmarks()
    assert len(all_items) >= 2
    filt = svc.fetch_bookmarks(recording_id=11)
    assert filt
    assert all(str(x.get("recording_id")) == "11" for x in filt)
    # Service class wrapper.
    srv = svc.EdlExportService()
    assert "TITLE:" in srv.to_edl(filt)
    assert "TITLE:" in srv.export(filt)


def test_edl_router_edl_and_json():
    bookmarks.clear()
    bookmarks.create_bookmark(recording_id=7, camera_id=3, offset_seconds=42, label="motion")
    client = _make_client()

    r = client.get("/api/bookmarks/export/edl")
    assert r.status_code == 200, r.text
    assert "TITLE:" in r.text
    assert "FCM: NON-DROP FRAME" in r.text
    assert "motion" in r.text
    assert "text/plain" in r.headers.get("content-type", "")

    r_filt = client.get("/api/bookmarks/export/edl", params={"recording_id": 7})
    assert r_filt.status_code == 200
    assert "motion" in r_filt.text

    r_empty = client.get("/api/bookmarks/export/edl", params={"recording_id": 9999})
    assert r_empty.status_code == 200
    assert "TITLE:" in r_empty.text

    r_json = client.get("/api/bookmarks/export/edl", params={"format": "json"})
    assert r_json.status_code == 200, r_json.text
    body = r_json.json()
    assert isinstance(body, dict)
    assert "edl" in body
    assert "TITLE:" in body["edl"]

    r_alias = client.get("/api/edl/export")
    assert r_alias.status_code == 200, r_alias.text
    assert "TITLE:" in r_alias.text

    r_health = client.get("/api/bookmarks/export/edl/health")
    assert r_health.status_code == 200
    assert r_health.json().get("status") == "ok"


def test_edl_router_nested_and_post():
    bookmarks.clear()
    client = _make_client()
    r = client.post("/api/recordings/9/bookmarks/export/edl", json={"label": "nested", "offset_seconds": 5})
    # POST export with empty store still returns a valid (possibly empty) EDL.
    assert r.status_code == 200, r.text
    assert "TITLE:" in r.text

    bookmarks.create_bookmark(recording_id=9, label="nested", offset_seconds=5)
    g = client.get("/api/recordings/9/bookmarks/export/edl")
    assert g.status_code == 200
    assert "nested" in g.text


def test_edl_paths_present_in_main_app():
    from app.main import app

    paths = list(app.openapi()["paths"].keys())
    assert any("edl" in p.lower() for p in paths), paths
