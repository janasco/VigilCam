from datetime import datetime

from fastapi.testclient import TestClient

from app.main import app
from app.services import rec_calendar


def test_import():
    import app.services.rec_calendar  # noqa: F401
    import app.api.rec_calendar  # noqa: F401
    from app.services.rec_calendar import (  # noqa: F401
        get_calendar,
        build_month_calendar,
        aggregate_daily,
        daily_counts,
        per_day_counts,
        rec_calendar_service,
        RecCalendarService,
    )
    from app.api.rec_calendar import router  # noqa: F401


def test_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/rec_calendar" in paths or "/api/recordings/calendar" in paths
    assert any("calendar" in p for p in paths)


def test_aggregate_daily():
    counts = rec_calendar.aggregate_daily([
        datetime(2026, 10, 5, 10, 0, 0),
        datetime(2026, 10, 5, 12, 0, 0),
        datetime(2026, 10, 6, 9, 0, 0),
    ])
    assert counts.get("2026-10-05") == 2
    assert counts.get("2026-10-06") == 1
    assert rec_calendar.count_by_day([datetime(2026, 10, 5)])["2026-10-05"] == 1


def test_build_month_calendar():
    out = rec_calendar.build_month_calendar(
        2026, 10,
        [datetime(2026, 10, 5, 10), datetime(2026, 10, 5, 11)],
        [datetime(2026, 10, 6, 9)],
    )
    assert out.get("year") == 2026
    assert out.get("month") == 10
    assert out.get("ok") is True
    assert out.get("total_recordings") == 2
    assert out.get("total_events") == 1
    by_date = out.get("by_date") or {}
    assert by_date["2026-10-05"]["recordings"] == 2
    assert by_date["2026-10-05"]["total"] == 2
    assert by_date["2026-10-06"]["events"] == 1
    days = out.get("days") or []
    assert len(days) == 31
    assert out.get("num_days") == 31


def test_calendar_aliases():
    out = rec_calendar.calendar(year=2026, month=10)
    assert out.get("month_key") == "2026-10"
    assert len(out.get("days") or []) == 31
    out2 = rec_calendar.daily_counts(year=2026, month=10)
    assert out2.get("month_key") == "2026-10"
    out3 = rec_calendar.per_day_counts(year=2026, month=10)
    assert out3.get("total") == 0


def test_scan_files_for_calendar(tmp_path):
    import os
    import time

    f1 = tmp_path / "a.mp4"
    f1.write_bytes(b"x" * 10)
    # Pin mtime to 2026-10-05 UTC noon.
    ts = datetime(2026, 10, 5, 12, 0, 0).timestamp()
    os.utime(f1, (ts, ts))
    dts = rec_calendar.scan_files_for_calendar(tmp_path)
    assert len(dts) == 1
    counts = rec_calendar.aggregate_daily(dts)
    # mtime round-trips through local timezone; just check one bucket got the file.
    assert sum(counts.values()) == 1


def test_api_calendar_empty_month():
    client = TestClient(app)
    paths = list(app.openapi()["paths"].keys())
    prefix = None
    for cand in ("/api/rec_calendar", "/api/recordings/calendar", "/api/calendar"):
        if cand in paths:
            prefix = cand
            break
    assert prefix is not None, paths
    r = client.get(prefix, params={"year": 2026, "month": 10})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data.get("year") == 2026
    assert data.get("month") == 10
    assert isinstance(data.get("days"), list)
    assert len(data["days"]) == 31

    r2 = client.post(prefix, json={"year": 2026, "month": 10})
    assert r2.status_code == 200, r2.text
    assert r2.json().get("month_key") == "2026-10"


def test_api_calendar_with_path(tmp_path):
    import os

    cam = tmp_path / "cam1"
    cam.mkdir(parents=True)
    f = cam / "clip.mp4"
    f.write_bytes(b"v" * 32)
    ts = datetime(2026, 10, 7, 8, 0, 0).timestamp()
    # utime uses local time; compute expected day from actual mtime instead of assuming.
    os.utime(f, (ts, ts))
    from datetime import timezone as _tz

    expected_key = datetime.fromtimestamp(float(f.stat().st_mtime), tz=_tz.utc).replace(tzinfo=None).strftime("%Y-%m-%d")

    client = TestClient(app)
    r = client.get("/api/rec_calendar", params={"year": 2026, "month": 10, "path": str(tmp_path)})
    # October has 31 days; if mtime lands in a neighboring month due to TZ, accept that month instead.
    if r.json().get("by_date", {}).get(expected_key) is None:
        y, m = expected_key.split("-")[:2]
        r = client.get("/api/rec_calendar", params={"year": int(y), "month": int(m), "path": str(tmp_path)})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data.get("total", 0) >= 1
    assert data.get("by_date", {}).get(expected_key, {}).get("total", 0) >= 1
