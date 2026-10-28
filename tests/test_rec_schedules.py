from datetime import datetime, timezone

from fastapi.testclient import TestClient
from app.main import app
from app.services import rec_schedules


def test_import():
    import app.services.rec_schedules
    import app.api.rec_schedules
    from app.services.rec_schedules import schedule, run, rec_schedule_service, RecScheduleScheduler, rec_schedule_scheduler
    from app.api.rec_schedules import router


def test_path():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/rec_schedules" in paths


def test_service_basic():
    rec_schedules.clear()
    job = rec_schedules.schedule(camera_id=1, schedule="*", name="rec1")
    assert isinstance(job, dict)
    assert job.get("status") == "scheduled"
    assert rec_schedules.get(job["id"]) is not None
    assert len(rec_schedules.list_schedules()) == 1


def test_aliases():
    rec_schedules.clear()
    job = rec_schedules.schedule_recording(camera_id=2, schedule="*")
    assert job.get("camera_id") == 2
    assert rec_schedules.get_schedule(job["id"]) is not None
    assert rec_schedules.get_rec_schedule(job["id"]) is not None
    assert rec_schedules.get_recording_schedule(job["id"]) is not None
    assert len(rec_schedules.list_rec_schedules()) == 1
    assert len(rec_schedules.list_recording_schedules()) == 1
    assert callable(rec_schedules.is_due)
    assert callable(rec_schedules.is_active)
    assert rec_schedules.is_due("*") is True


def test_time_profile_window():
    rec_schedules.clear()
    # Always-on profile
    job = rec_schedules.schedule(camera_id=1, mode="continuous",
                                 days=[0, 1, 2, 3, 4, 5, 6],
                                 start_time="00:00", end_time="23:59")
    assert job.get("mode") == "continuous"
    now = datetime.now(timezone.utc)
    assert rec_schedules.is_active(job, now) is True
    assert rec_schedules.should_record(1, now) is True
    # Narrow window should be inactive outside it
    rec_schedules.clear()
    narrow = rec_schedules.schedule(camera_id=1, mode="continuous",
                                    start_time="00:00", end_time="00:01")
    moment_outside = now.replace(hour=12, minute=0, second=0, microsecond=0)
    # 12:00 is outside 00:00-00:01
    assert rec_schedules.is_active(narrow, moment_outside) is False


def test_overnight_and_days():
    rec_schedules.clear()
    job = rec_schedules.schedule(camera_id=1, mode="continuous",
                                 start_time="22:00", end_time="06:00")
    late = datetime(2026, 1, 5, 23, 0, tzinfo=timezone.utc)  # Monday
    early = datetime(2026, 1, 6, 2, 0, tzinfo=timezone.utc)  # Tuesday
    midday = datetime(2026, 1, 6, 12, 0, tzinfo=timezone.utc)
    assert rec_schedules.is_active(job, late) is True
    assert rec_schedules.is_active(job, early) is True
    assert rec_schedules.is_active(job, midday) is False
    # Day filter: only Mondays (0)
    rec_schedules.clear()
    mon_only = rec_schedules.schedule(camera_id=1, days=[0],
                                      start_time="00:00", end_time="23:59")
    assert rec_schedules.is_active(mon_only, late) is True
    assert rec_schedules.is_active(mon_only, early) is False


def test_mode_off_never_active():
    rec_schedules.clear()
    job = rec_schedules.schedule(camera_id=1, mode="off",
                                 start_time="00:00", end_time="23:59")
    now = datetime.now(timezone.utc)
    assert rec_schedules.is_active(job, now) is False
    assert rec_schedules.should_record(1, now) is False


def test_run_due():
    rec_schedules.clear()
    rec_schedules.schedule(camera_id=1, schedule="*")
    res = rec_schedules.run(force=True)
    assert isinstance(res, dict)
    assert res.get("processed") == 1 or res.get("count") == 1
    assert res.get("success") is True


def test_run_skips_disabled():
    rec_schedules.clear()
    rec_schedules.schedule(camera_id=1, schedule="*", enabled=False)
    res = rec_schedules.run()
    assert res.get("skipped") == 1 or res.get("skipped_count") == 1


def test_endpoint():
    rec_schedules.clear()
    client = TestClient(app)
    r = client.post("/api/rec_schedules/schedule", json={"camera_id": 1, "schedule": "*"})
    assert r.status_code == 200, r.text
    r2 = client.post("/api/rec_schedules/run", json={"force": True})
    assert r2.status_code == 200, r2.text
    assert r2.json().get("processed") == 1 or r2.json().get("count") == 1


def test_scheduler_class():
    rec_schedules.clear()
    rec_schedules.schedule(camera_id=1, schedule="*")
    sched = rec_schedules.RecScheduleScheduler(interval_seconds=10)
    res = sched.run_now()
    assert (res.get("processed") == 1 or res.get("count") == 1 or res.get("executed_count") == 1)
