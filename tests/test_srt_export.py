from datetime import datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.srt_export import router as srt_router
import app.services.srt_export as svc


def _make_client():
    app = FastAPI()
    app.include_router(srt_router)
    return TestClient(app)


def test_imports():
    import app.api.srt_export  # noqa: F401
    import app.services.srt_export  # noqa: F401
    assert srt_router is not None
    for attr in ("build_srt", "events_to_srt", "to_srt", "format_srt_timestamp",
                 "filter_events", "write_srt", "fetch_events_for_srt",
                 "export_srt", "SrtExportService", "srt_export_service"):
        assert hasattr(svc, attr), attr


def test_export_baseline_is_video_only_no_srt_sidecar():
    """Verify pre-existing export produces video only (no SRT/subtitle)."""
    import inspect
    import app.services.export as exp
    import app.api.export as exp_api

    src = inspect.getsource(exp) + inspect.getsource(exp_api)
    lowered = src.lower()
    assert ".srt" not in lowered
    assert "subtitle" not in lowered.replace("subtitle export", "")
    # video-only markers: ffmpeg concat demuxer present
    assert "concat" in lowered
    assert "ffmpeg" in lowered
    # no srt-related helpers on the legacy export service
    for attr in ("build_srt", "events_to_srt", "to_srt", "srt_export", "subtitle"):
        assert not hasattr(exp.export_service, attr), attr


def test_format_timestamp():
    assert svc.format_srt_timestamp(0) == "00:00:00,000"
    assert svc.format_srt_timestamp(61.5) == "00:01:01,500"
    assert svc.format_srt_timestamp(3661.007) == "01:01:01,007"
    assert svc.format_timestamp(1.001) == "00:00:01,001"


def test_build_srt_basic_numbering_and_timing():
    base = datetime(2026, 1, 1, 12, 0, 0)
    events = [
        {"camera_id": 1, "label": "person", "confidence": 0.9, "created_at": base},
        {"camera_id": 1, "label": "car", "created_at": base + timedelta(seconds=5)},
    ]
    text = svc.build_srt(events)
    assert "1\n00:00:00,000 --> 00:00:03,000" in text
    assert "2\n00:00:05,000 --> 00:00:08,000" in text
    assert "person" in text
    assert "car" in text
    # cues separated by blank line, ends with newline
    assert text.endswith("\n")
    assert "\n\n" in text


def test_build_srt_empty():
    assert svc.build_srt([]) == ""
    assert svc.build_srt(None) == ""


def test_build_srt_filters_and_anchor():
    base = datetime(2026, 1, 1, 0, 0, 0)
    events = [
        {"camera_id": 1, "label": "person", "created_at": base},
        {"camera_id": 2, "label": "car", "created_at": base + timedelta(hours=1)},
        {"camera_id": 1, "label": "car", "created_at": base + timedelta(hours=2)},
    ]
    only_cam1 = svc.build_srt(events, camera_id=1)
    assert "person" in only_cam1
    assert only_cam1.count("\n\n") + 1 == 2 or only_cam1.count("-->") == 2
    only_car = svc.build_srt(events, label="car")
    assert "person" not in only_car
    assert only_car.count("-->") == 2
    ranged = svc.build_srt(events, start=(base + timedelta(minutes=30)).isoformat(),
                            end=(base + timedelta(hours=90 / 60)).isoformat())
    assert ranged.count("-->") == 1
    assert "car" in ranged


def test_build_srt_sanitizes_arrows_and_blank_lines():
    base = datetime(2026, 1, 1, 0, 0, 0)
    events = [{"label": "a --> b\n\ninjected", "created_at": base}]
    text = svc.build_srt(events)
    # cue timing arrow must be exactly one per cue; injected arrow neutralized
    assert text.count("-->") == 1
    assert "->" in text


def test_build_srt_custom_duration_and_explicit_end():
    base = datetime(2026, 1, 1, 0, 0, 0)
    events = [{"label": "person", "created_at": base}]
    short = svc.build_srt(events, duration=1.5)
    assert "00:00:00,000 --> 00:00:01,500" in short
    with_end = [{"label": "person", "created_at": base,
                 "end": (base + timedelta(seconds=10)).isoformat()}]
    text = svc.build_srt(with_end)
    assert "00:00:00,000 --> 00:00:10,000" in text


def test_service_class_and_filename_and_write(tmp_path):
    service = svc.SrtExportService()
    base = datetime(2026, 1, 1, 0, 0, 0)
    text = service.build([{"label": "person", "created_at": base}])
    assert "-->" in text
    assert service.format_timestamp(60) == "00:01:00,000"
    name = svc.srt_filename(camera_name="front door", start=base.isoformat())
    assert name.endswith(".srt")
    assert " " not in name
    out = svc.write_srt(tmp_path / "events", text)
    assert out.endswith(".srt")
    with open(out, encoding="utf-8") as fh:
        assert "-->" in fh.read()


def test_router_get_returns_srt():
    client = _make_client()
    r = client.get("/api/export/srt")
    assert r.status_code == 200, r.text
    assert "subrip" in r.headers.get("content-type", "")
    assert 'filename=' in r.headers.get("content-disposition", "")


def test_router_post_with_events_returns_cues():
    client = _make_client()
    base = datetime(2026, 1, 1, 12, 0, 0)
    payload = {"events": [
        {"camera_id": 1, "label": "person", "created_at": base.isoformat()},
        {"camera_id": 1, "label": "car", "created_at": (base + timedelta(seconds=4)).isoformat()},
    ]}
    r = client.post("/api/export/srt", json=payload)
    assert r.status_code == 200, r.text
    body = r.text
    assert "1\n00:00:00,000 -->" in body
    assert "person" in body
    assert "car" in body


def test_router_alias_paths():
    client = _make_client()
    for path in ("/api/events/srt", "/api/srt-export", "/api/export/events.srt"):
        r = client.get(path)
        assert r.status_code == 200, (path, r.text)


def test_router_health():
    client = _make_client()
    r = client.get("/api/srt-export/health")
    assert r.status_code == 200, r.text
    assert r.json().get("status") == "ok"
