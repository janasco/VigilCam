from fastapi.testclient import TestClient
from app.main import app
from app.services import snapshot_schedule


def test_import():
    import app.services.snapshot_schedule
    import app.api.snapshot_schedule
    from app.services.snapshot_schedule import schedule, run, snapshot_schedule_service, SnapshotScheduleScheduler, snapshot_schedule_scheduler
    from app.api.snapshot_schedule import router


def test_path():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/snapshot_schedule" in paths


def test_service_basic():
    snapshot_schedule.clear()
    job = snapshot_schedule.schedule(camera_id=1, schedule="*", name="snap1")
    assert isinstance(job, dict)
    assert job.get("status") == "scheduled"
    assert snapshot_schedule.get(job["id"]) is not None
    assert len(snapshot_schedule.list_schedules()) == 1


def test_aliases():
    snapshot_schedule.clear()
    job = snapshot_schedule.schedule_snapshot(camera_id=2, schedule="*")
    assert job.get("camera_id") == 2
    assert snapshot_schedule.get_schedule(job["id"]) is not None
    assert snapshot_schedule.get_snapshot_schedule(job["id"]) is not None
    assert len(snapshot_schedule.list_snapshot_schedules()) == 1
    # due-run logic aliases
    assert callable(snapshot_schedule.is_due)
    assert snapshot_schedule.is_due("*") is True


def test_run_due():
    snapshot_schedule.clear()
    snapshot_schedule.schedule(camera_id=1, schedule="*")
    res = snapshot_schedule.run(force=True)
    assert isinstance(res, dict)
    assert res.get("processed") == 1 or res.get("count") == 1
    assert res.get("success") is True


def test_run_skips_disabled():
    snapshot_schedule.clear()
    snapshot_schedule.schedule(camera_id=1, schedule="*", enabled=False)
    res = snapshot_schedule.run()
    assert res.get("skipped") == 1 or res.get("skipped_count") == 1


def test_endpoint():
    snapshot_schedule.clear()
    client = TestClient(app)
    r = client.post("/api/snapshot_schedule/schedule", json={"camera_id": 1, "schedule": "*"})
    assert r.status_code == 200, r.text
    r2 = client.post("/api/snapshot_schedule/run", json={"force": True})
    assert r2.status_code == 200, r2.text
    assert r2.json().get("processed") == 1 or r2.json().get("count") == 1


def test_scheduler_class():
    snapshot_schedule.clear()
    snapshot_schedule.schedule(camera_id=1, schedule="*")
    sched = snapshot_schedule.SnapshotScheduleScheduler(interval_seconds=10)
    res = sched.run_now()
    assert (res.get("processed") == 1 or res.get("count") == 1 or res.get("executed_count") == 1)
