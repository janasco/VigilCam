from fastapi.testclient import TestClient
from app.main import app
from app.services import voice_routines


def test_import():
    import app.services.voice_routines
    import app.api.voice_routines
    from app.services.voice_routines import schedule, run, voice_routine_service, VoiceRoutineScheduler, voice_routine_scheduler
    from app.api.voice_routines import router


def test_path():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/voice_routines" in paths


def test_service_basic():
    voice_routines.clear()
    job = voice_routines.schedule(camera_id=1, schedule="*", prompt="Summarize this camera.", name="routine1")
    assert isinstance(job, dict)
    assert job.get("status") == "scheduled"
    assert job.get("prompt") == "Summarize this camera."
    assert voice_routines.get(job["id"]) is not None
    assert len(voice_routines.list_routines()) == 1


def test_aliases():
    voice_routines.clear()
    job = voice_routines.schedule_voice_routine(camera_id=2, schedule="*")
    assert job.get("camera_id") == 2
    assert voice_routines.get_routine(job["id"]) is not None
    assert voice_routines.get_voice_routine(job["id"]) is not None
    assert len(voice_routines.list_voice_routines()) == 1
    # due-run logic aliases
    assert callable(voice_routines.is_due)
    assert voice_routines.is_due("*") is True
    # VLM summary helper
    summary = voice_routines.generate_vlm_summary(camera_id=2, prompt="What do you see?")
    assert isinstance(summary, dict)
    assert summary.get("summary")
    assert summary.get("success") is True


def test_run_due():
    voice_routines.clear()
    voice_routines.schedule(camera_id=1, schedule="*", prompt="Summarize.")
    res = voice_routines.run(force=True)
    assert isinstance(res, dict)
    assert res.get("processed") == 1 or res.get("count") == 1
    assert res.get("success") is True
    summaries = res.get("summaries") or []
    assert summaries and summaries[0]


def test_run_skips_disabled():
    voice_routines.clear()
    voice_routines.schedule(camera_id=1, schedule="*", enabled=False)
    res = voice_routines.run()
    assert res.get("skipped") == 1 or res.get("skipped_count") == 1


def test_endpoint():
    voice_routines.clear()
    client = TestClient(app)
    r = client.post("/api/voice_routines/schedule", json={"camera_id": 1, "schedule": "*", "prompt": "Summarize."})
    assert r.status_code == 200, r.text
    r2 = client.post("/api/voice_routines/run", json={"force": True})
    assert r2.status_code == 200, r2.text
    assert r2.json().get("processed") == 1 or r2.json().get("count") == 1


def test_scheduler_class():
    voice_routines.clear()
    voice_routines.schedule(camera_id=1, schedule="*")
    sched = voice_routines.VoiceRoutineScheduler(interval_seconds=10)
    res = sched.run_now()
    assert (res.get("processed") == 1 or res.get("count") == 1 or res.get("executed_count") == 1)
