from fastapi.testclient import TestClient
from app.main import app
from app.services import patrol_schedule


def test_import():
    import app.services.patrol_schedule
    import app.api.patrol_schedule
    from app.services.patrol_schedule import schedule, run, patrol_schedule_service, PatrolScheduleScheduler, patrol_schedule_scheduler
    from app.api.patrol_schedule import router


def test_path():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/patrol_schedule" in paths


def test_service_basic():
    patrol_schedule.clear()
    job = patrol_schedule.schedule(camera_id=1, schedule="*", name="patrol1")
    assert isinstance(job, dict)
    assert job.get("status") == "scheduled"
    assert patrol_schedule.get(job["id"]) is not None
    assert len(patrol_schedule.list_schedules()) == 1


def test_aliases():
    patrol_schedule.clear()
    job = patrol_schedule.schedule_patrol(camera_id=2, schedule="*")
    assert job.get("camera_id") == 2
    assert patrol_schedule.get_schedule(job["id"]) is not None
    assert patrol_schedule.get_patrol_schedule(job["id"]) is not None
    assert len(patrol_schedule.list_patrol_schedules()) == 1
    # due-run logic aliases
    assert callable(patrol_schedule.is_due)
    assert patrol_schedule.is_due("*") is True


def test_run_due():
    patrol_schedule.clear()
    patrol_schedule.schedule(camera_id=1, schedule="*")
    res = patrol_schedule.run(force=True)
    assert isinstance(res, dict)
    assert res.get("processed") == 1 or res.get("count") == 1
    assert res.get("success") is True


def test_run_skips_disabled():
    patrol_schedule.clear()
    patrol_schedule.schedule(camera_id=1, schedule="*", enabled=False)
    res = patrol_schedule.run()
    assert res.get("skipped") == 1 or res.get("skipped_count") == 1


def test_time_based_tour_triggering():
    from app.services import ptz_tour
    patrol_schedule.clear()
    ptz_tour.clear()
    tour = ptz_tour.create_tour(camera_id=9, presets=["p1", "p2"], name="night-tour")
    assert tour.get("id")
    job = patrol_schedule.schedule(camera_id=9, tour_id=tour["id"], schedule="*")
    assert job.get("tour_id") == tour["id"]
    res = patrol_schedule.run(force=True)
    assert isinstance(res, dict)
    executed = res.get("executed") or res.get("triggered") or []
    assert len(executed) == 1
    first = executed[0]
    assert first.get("tour_id") == tour["id"]
    assert first.get("triggered") is True or first.get("executed") is True


def test_endpoint():
    patrol_schedule.clear()
    client = TestClient(app)
    r = client.post("/api/patrol_schedule/schedule", json={"camera_id": 1, "schedule": "*"})
    assert r.status_code == 200, r.text
    r2 = client.post("/api/patrol_schedule/run", json={"force": True})
    assert r2.status_code == 200, r2.text
    assert r2.json().get("processed") == 1 or r2.json().get("count") == 1


def test_scheduler_class():
    patrol_schedule.clear()
    patrol_schedule.schedule(camera_id=1, schedule="*")
    sched = patrol_schedule.PatrolScheduleScheduler(interval_seconds=10)
    res = sched.run_now()
    assert (res.get("processed") == 1 or res.get("count") == 1 or res.get("executed_count") == 1)
