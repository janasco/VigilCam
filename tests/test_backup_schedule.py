"""Tests for scheduled backups (backup_schedule service + router)."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_backup_schedule_imports():
    import app.api.backup_schedule as api_mod  # noqa: F401
    import app.services.backup_schedule as svc_mod  # noqa: F401
    from app.services.backup_schedule import (  # noqa: F401
        schedule,
        run,
        backup_schedule_service,
        BackupScheduleScheduler,
        backup_schedule_scheduler,
    )
    assert hasattr(api_mod, "router")
    assert hasattr(svc_mod, "backup_schedule_scheduler")


def test_backup_schedule_routes_present():
    from app.main import app

    paths = list(app.openapi()["paths"].keys())
    assert "/api/backup_schedule" in paths
    assert "/api/backup-schedule" in paths or "/api/backups/schedule" in paths


def test_service_basic():
    from app.services import backup_schedule

    backup_schedule.clear()
    job = backup_schedule.schedule(name="nightly", schedule="*", keep_last=5)
    assert isinstance(job, dict)
    assert job.get("status") == "scheduled"
    assert job.get("keep_last") == 5
    assert backup_schedule.get(job["id"]) is not None
    assert len(backup_schedule.list_schedules()) == 1


def test_aliases_and_due():
    from app.services import backup_schedule

    backup_schedule.clear()
    job = backup_schedule.schedule_backup(name="alias-check", schedule="*")
    assert backup_schedule.get_schedule(job["id"]) is not None
    assert backup_schedule.get_backup_schedule(job["id"]) is not None
    assert len(backup_schedule.list_backup_schedules()) == 1
    assert callable(backup_schedule.is_due)
    assert backup_schedule.is_due("*") is True
    assert backup_schedule.create_backup_schedule(name="x2", schedule="*") is not None
    backup_schedule.clear()


def test_run_due_tmpdir(tmp_path):
    from app.services import backup_schedule
    from app.services import backup as backup_mod

    backup_schedule.clear()
    sched = backup_schedule.schedule(
        name="tmp-run",
        schedule="*",
        backup_dir=str(tmp_path / "backups"),
    )
    res = backup_schedule.run(force=True, backup_dir=str(tmp_path / "backups"))
    assert isinstance(res, dict)
    assert res.get("processed") == 1 or res.get("count") == 1
    assert res.get("success") is True
    # Backups landed in the tmp dir, not the real data dir.
    svc = backup_mod.BackupService(backup_dir=tmp_path / "backups")
    assert len(svc.list_backups()) >= 1
    # Single-schedule targeted run.
    res2 = backup_schedule.run(id=sched["id"], force=True, backup_dir=str(tmp_path / "backups"))
    assert res2.get("processed") == 1 or res2.get("count") == 1
    backup_schedule.clear()


def test_run_skips_disabled():
    from app.services import backup_schedule

    backup_schedule.clear()
    backup_schedule.schedule(name="off", schedule="*", enabled=False)
    res = backup_schedule.run()
    assert res.get("skipped") == 1 or res.get("skipped_count") == 1
    backup_schedule.clear()


def test_retention_keep_last(tmp_path):
    from app.services import backup_schedule
    from app.services import backup as backup_mod

    backup_schedule.clear()
    backup_schedule.schedule(
        name="retained",
        schedule="*",
        keep_last=2,
        backup_dir=str(tmp_path / "backups"),
    )
    for _ in range(3):
        out = backup_schedule.run(force=True, backup_dir=str(tmp_path / "backups"))
        assert out.get("success") is True
    svc = backup_mod.BackupService(backup_dir=tmp_path / "backups")
    assert len(svc.list_backups()) <= 2
    backup_schedule.clear()


def test_endpoint_crud_and_run(tmp_path):
    from app.main import app
    from app.services import backup_schedule
    from app.services import backup as backup_mod

    backup_schedule.clear()
    client = TestClient(app)

    r = client.post(
        "/api/backup_schedule/schedule",
        json={"name": "api-nightly", "schedule": "*", "backup_dir": str(tmp_path / "backups")},
    )
    assert r.status_code == 200, r.text
    sid = r.json().get("id") or r.json().get("schedule_id")
    assert sid

    lst = client.get("/api/backup_schedule")
    assert lst.status_code == 200, lst.text
    assert any((i.get("id") or i.get("schedule_id")) == sid for i in lst.json())

    one = client.get(f"/api/backup_schedule/{sid}")
    assert one.status_code == 200, one.text

    # Disable then re-enable via dedicated endpoints.
    dis = client.post(f"/api/backup_schedule/{sid}/disable")
    assert dis.status_code == 200, dis.text
    assert dis.json().get("enabled") is False
    ena = client.post(f"/api/backup_schedule/{sid}/enable")
    assert ena.status_code == 200, ena.text
    assert ena.json().get("enabled") is True

    # Force a run through the API into the tmp backup dir.
    r2 = client.post("/api/backup_schedule/run", json={"force": True, "backup_dir": str(tmp_path / "backups")})
    assert r2.status_code == 200, r2.text
    assert (r2.json().get("processed") == 1 or r2.json().get("count") == 1)

    svc = backup_mod.BackupService(backup_dir=tmp_path / "backups")
    assert len(svc.list_backups()) >= 1

    missing = client.get("/api/backup_schedule/does-not-exist-123")
    assert missing.status_code == 404

    rm = client.delete(f"/api/backup_schedule/{sid}")
    assert rm.status_code in (200, 204), rm.text
    backup_schedule.clear()


def test_scheduler_class(tmp_path):
    from app.services import backup_schedule

    backup_schedule.clear()
    backup_schedule.schedule(name="cls", schedule="*", backup_dir=str(tmp_path / "backups"))
    sched = backup_schedule.BackupScheduleScheduler(interval_seconds=10)
    res = sched.run_now(backup_dir=str(tmp_path / "backups"))
    assert res.get("processed") == 1 or res.get("count") == 1 or res.get("executed_count") == 1
    backup_schedule.clear()
