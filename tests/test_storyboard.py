from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.services import storyboard as sb
from app.api import storyboard as storyboard_api


def _range():
    end = datetime.now(timezone.utc).timestamp()
    start = end - 3600.0
    return start, end


def test_imports():
    import app.services.storyboard  # noqa: F401
    import app.api.storyboard  # noqa: F401
    from app.services.storyboard import (  # noqa: F401
        build_storyboard,
        build_storyboard_with_meta,
        compute_timestamps,
        validate_storyboard_params,
    )
    from app.api.storyboard import router  # noqa: F401
    assert router is not None
    assert storyboard_api.router is not None


def test_single_frame_helpers_have_no_time_range():
    # Snapshots/thumbnails are single-frame only: no time-range strip.
    import inspect

    import app.services.snapshots as snaps
    import app.services.thumbnails as thumbs

    snap_src = inspect.getsource(snaps.save_snapshot)
    thumb_src = inspect.getsource(thumbs.get_thumbnail)
    for token in ("start_time", "end_time", "contact sheet", "contact_sheet"):
        assert token not in snap_src
        assert token not in thumb_src
    # Thumbnails serve one cached JPEG per camera (bytes + JPEG magic).
    thumbs.clear()
    one = thumbs.get_thumbnail("storyboard-single-check")
    assert isinstance(one, (bytes, bytearray))
    assert bytes(one)[:2] == b"\xff\xd8"


def test_parse_time_and_count():
    assert sb.parse_time(None) is None
    assert sb.parse_time(1700000000) == 1700000000.0
    # millis -> seconds
    assert sb.parse_time(1700000000000) == 1700000000.0
    assert sb.parse_time("2024-01-02 03:04:05") is not None
    assert sb.parse_time("not-a-time") is None
    assert sb.parse_count(6) == 6
    assert sb.parse_count(0) is None
    assert sb.parse_count(100) is None
    assert sb.parse_count(None) == sb.DEFAULT_COUNT


def test_validate_ok():
    start, end = _range()
    info = sb.validate_storyboard_params("cam1", start=start, end=end, count=6)
    assert info["camera"] == "cam1"
    assert info["count"] == 6
    assert len(info["timestamps"]) == 6
    assert info["start"] < info["end"]
    assert info["columns"] >= 1
    assert info["rows"] >= 1


def test_validate_aliases():
    start, end = _range()
    info = sb.validate_storyboard_params(
        camera_id="cam-alias", start_time=start, end_time=end, n=4,
        thumb_width=160, thumb_height=90, cols=2,
    )
    assert info["camera"] == "cam-alias"
    assert info["count"] == 4
    assert info["columns"] == 2
    assert info["thumb_width"] == 160


def test_validate_bad_count():
    start, end = _range()
    try:
        sb.validate_storyboard_params("c", start=start, end=end, count=0)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for count=0")
    try:
        sb.validate_storyboard_params("c", start=start, end=end, count=999)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for count=999")


def test_validate_bad_time_range():
    start, end = _range()
    try:
        sb.validate_storyboard_params("c", start=end, end=start, count=4)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for end<=start")


def test_compute_timestamps_evenly_spaced_inclusive():
    start, end = 1000.0, 2000.0
    ts = sb.compute_timestamps(start, end, 5)
    assert len(ts) == 5
    assert ts[0] == start
    assert ts[-1] == end
    # even spacing
    steps = [b - a for a, b in zip(ts, ts[1:])]
    assert max(steps) - min(steps) < 1e-6
    single = sb.compute_timestamps(start, end, 1)
    assert len(single) == 1
    assert start <= single[0] <= end


def test_layout_grid():
    rows, cols = sb.layout_grid(6)
    assert rows * cols >= 6
    rows2, cols2 = sb.layout_grid(6, columns=2)
    assert cols2 == 2
    assert rows2 == 3


def test_build_storyboard_png_bytes():
    start, end = _range()
    data, meta = sb.build_storyboard_with_meta("cam1", start, end, 6)
    assert isinstance(data, (bytes, bytearray))
    assert bytes(data)[:8] == b"\x89PNG\r\n\x1a\n"
    assert meta["count"] == 6
    assert len(meta["timestamps"]) == 6
    assert meta["content_type"] == "image/png"
    assert meta["size_bytes"] == len(data)
    # grid covers N tiles
    assert meta["rows"] * meta["columns"] >= 6


def test_build_storyboard_aliases_and_service():
    start, end = _range()
    assert sb.render_contact_sheet("c", start, end, 4)[:8] == b"\x89PNG\r\n\x1a\n"
    assert sb.generate_storyboard("c", start, end, 4)[:8] == b"\x89PNG\r\n\x1a\n"
    svc = sb.StoryboardService()
    assert svc.build("c", start, end, 4)[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(svc.compute_timestamps(start, end, 4)) == 4


def test_build_storyboard_file(tmp_path):
    start, end = _range()
    out = tmp_path / "board.png"
    result = sb.build_storyboard_file("cam-file", start, end, 4, output=str(out))
    assert Path(str(result)).exists()
    assert Path(str(result)).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_build_jpeg_format():
    start, end = _range()
    data, meta = sb.build_storyboard_with_meta("c", start, end, 4, format="jpeg")
    assert bytes(data)[:2] == b"\xff\xd8"
    assert meta["content_type"] == "image/jpeg"


def test_jobs_lifecycle():
    start, end = _range()
    job = sb.create_job("cam-job", start, end, 4)
    assert job["status"] == "queued"
    assert sb.get_job(job["job_id"]) is not None
    assert any(j["job_id"] == job["job_id"] for j in sb.list_jobs())
    sb.delete_job(job["job_id"])
    assert sb.get_job(job["job_id"]) is None


def test_router_status():
    from app.main import app

    client = TestClient(app)
    for path in ("/api/storyboard/status", "/api/storyboard/health", "/api/storyboards/status"):
        r = client.get(path)
        assert r.status_code == 200, (path, r.text)
        assert r.json().get("status") == "ok"


def test_router_preview_image():
    from app.main import app

    client = TestClient(app)
    start, end = _range()
    r = client.get(
        "/api/storyboard/preview",
        params={"camera": "cam-prev", "start": start, "end": end, "count": 6},
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("image/")
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert r.headers.get("X-Storyboard-Count") == "6"


def test_router_camera_image_time_range():
    from app.main import app

    client = TestClient(app)
    start, end = _range()
    r = client.get(f"/api/storyboard/cam-range", params={"start": start, "end": end, "count": 4})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("image/")
    # N-tile contact sheet is larger than a single thumbnail placeholder
    assert len(r.content) > 1000


def test_router_build_post(tmp_path):
    from app.main import app

    client = TestClient(app)
    start, end = _range()
    out = tmp_path / "router_board.png"
    r = client.post(
        "/api/storyboard/build",
        json={"camera": "cam-post", "start": start, "end": end, "count": 4, "output": str(out)},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("ok") is True
    assert body.get("count") == 4
    assert len(body.get("timestamps", [])) == 4
    assert Path(body["output_path"]).exists()


def test_router_build_bad_count():
    from app.main import app

    client = TestClient(app)
    start, end = _range()
    r = client.post(
        "/api/storyboard/build",
        json={"camera": "c", "start": start, "end": end, "count": 0},
    )
    assert r.status_code == 400


def test_router_build_bad_timerange():
    from app.main import app

    client = TestClient(app)
    start, end = _range()
    r = client.post(
        "/api/storyboard/build",
        json={"camera": "c", "start": end, "end": start, "count": 4},
    )
    assert r.status_code == 400
