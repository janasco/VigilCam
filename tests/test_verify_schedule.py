from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _good_mp4(path: Path, size: int = 4096) -> None:
    blob = bytearray(b"\x00\x00\x00\x20ftypisom\x00\x00\x00\x00isomiso2")
    blob += b"\x00\x00\x00\x08mdat" + b"\x01" * 64
    blob += b"\x00\x00\x00\x08moov" + b"\x02" * 64
    while len(blob) < size:
        blob += b"\x00" * 1024
    path.write_bytes(bytes(blob[:size]))


def test_imports():
    import app.services.verify_schedule as svc  # noqa: F401
    import app.api.verify_schedule as api  # noqa: F401

    assert hasattr(api, "router")
    assert hasattr(svc, "VerifyScheduler")
    assert hasattr(svc, "schedule")
    assert hasattr(svc, "run")
    assert hasattr(svc, "verify_scheduler")
    assert callable(svc.is_due)


def test_service_basic(tmp_path):
    from app.services import verify_schedule as svc

    svc.clear()
    job = svc.schedule(path=str(tmp_path), schedule="*", name="v1")
    assert isinstance(job, dict)
    assert job.get("status") == "scheduled"
    assert svc.get(job["id"]) is not None
    assert len(svc.list_schedules()) == 1


def test_aliases():
    from app.services import verify_schedule as svc

    svc.clear()
    job = svc.schedule_verification(path="data/recordings", schedule="*")
    assert job.get("path") == "data/recordings"
    assert svc.get_schedule(job["id"]) is not None
    assert svc.get_verify_schedule(job["id"]) is not None
    assert len(svc.list_verify_schedules()) == 1
    assert callable(svc.is_due)
    assert svc.is_due("*") is True


def test_run_due_scans_integrity(tmp_path):
    from app.services import verify_schedule as svc

    svc.clear()
    root = tmp_path / "rec"
    root.mkdir()
    _good_mp4(root / "a.mp4")
    (root / "b.mp4").write_bytes(b"junk-bytes" * 200)

    svc.schedule(path=str(root), schedule="*")
    res = svc.run(force=True)
    assert isinstance(res, dict)
    assert res.get("processed") == 1 or res.get("count") == 1
    assert res.get("success") is True
    executed = res.get("executed") or []
    assert len(executed) == 1
    report = executed[0].get("report", {})
    assert report.get("total") == 2
    assert report.get("bad_count") == 1


def test_run_skips_disabled():
    from app.services import verify_schedule as svc

    svc.clear()
    svc.schedule(path="data/recordings", schedule="*", enabled=False)
    res = svc.run()
    assert res.get("skipped") == 1 or res.get("skipped_count") == 1


def _mounted_client() -> TestClient:
    from app.api.verify_schedule import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_api_list_and_status():
    from app.services import verify_schedule as svc

    svc.clear()
    client = _mounted_client()
    r = client.get("/api/verify_schedule")
    assert r.status_code == 200, r.text
    assert isinstance(r.json(), list)
    r2 = client.get("/api/verify_schedule/status")
    assert r2.status_code == 200, r2.text
    assert "status" in r2.json()


def test_endpoint_schedule_and_run(tmp_path):
    from app.services import verify_schedule as svc

    svc.clear()
    client = _mounted_client()
    root = tmp_path / "api-rec"
    root.mkdir()
    _good_mp4(root / "ok.mp4")
    r = client.post("/api/verify_schedule/schedule", json={"path": str(root), "schedule": "*"})
    assert r.status_code == 200, r.text
    assert r.json().get("status") == "scheduled"
    r2 = client.post("/api/verify_schedule/run", json={"force": True})
    assert r2.status_code == 200, r2.text
    assert r2.json().get("processed") == 1 or r2.json().get("count") == 1


def test_scheduler_class_start_stop():
    from app.services import verify_schedule as svc

    svc.clear()
    svc.schedule(path="data/recordings", schedule="*")
    sched = svc.VerifyScheduler(interval_seconds=10_000)
    res = sched.run_now()
    assert (res.get("processed") == 1 or res.get("count") == 1
            or res.get("executed_count") == 1)
    sched2 = svc.VerifyScheduler(interval_seconds=10_000)
    sched2.start()
    assert sched2.is_running is True
    sched2.stop()
    assert sched2.is_running is False


def test_router_prefix():
    from app.api.verify_schedule import router

    assert any("/api/verify_schedule" in getattr(r, "path", "") for r in router.routes)


def test_verify_schedule_on_full_app():
    from app.main import app

    client = TestClient(app)
    paths = list(app.openapi()["paths"].keys())
    assert any("verify" in p and "schedule" in p for p in paths)
    r = client.get("/api/verify_schedule/status")
    assert r.status_code == 200, r.text
