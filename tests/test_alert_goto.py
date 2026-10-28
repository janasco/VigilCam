import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.alert_goto import router as alert_goto_router
from app.main import app
from app.services import alert_goto
from app.services.alert_goto import (
    alert_goto_service,
    clear,
    configure_rule,
    get_status,
    handle_detection,
    list_rules,
)


def _fresh():
    clear()


def test_imports():
    import app.services.alert_goto  # noqa: F401
    import app.api.alert_goto  # noqa: F401
    from app.api.alert_goto import router  # noqa: F401
    from app.services.alert_goto import (  # noqa: F401
        alert_goto_service,
        configure_rule,
        handle_detection,
    )


def test_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert any("alert_goto" in p or "alert-goto" in p or "alerts/goto" in p for p in paths)


def test_no_autofire_without_rule():
    # PTZ presets/tours/schedules never fire on detection events: with no
    # alert_goto rule configured, a detection must not move any camera.
    _fresh()
    res = handle_detection(camera_id=1, label="person")
    assert res["moved"] is False
    assert res["matched"] is False
    assert res.get("ok") is True


def test_existing_ptz_engines_have_no_detection_hooks():
    # Tours run on explicit start/step; schedules on cron/time; idle on
    # elapsed idle time; preset library on explicit goto. None of them
    # hook into detection events.
    import app.services.ptz_tour as tour_mod
    import app.services.ptz_scheduler as sched_mod
    import app.services.ptz_idle as idle_mod
    import app.services.preset_library as lib_mod

    for mod in (tour_mod, sched_mod, idle_mod, lib_mod):
        assert not hasattr(mod, "handle_detection"), mod.__name__
        assert not hasattr(mod, "on_detection"), mod.__name__
        assert not hasattr(mod, "on_event"), mod.__name__


def test_label_match_moves_to_preset():
    _fresh()
    configure_rule(camera_id=1, label="person", preset_token="door", cooldown_seconds=60)
    res = handle_detection(camera_id=1, label="person")
    assert res["moved"] is True
    assert res["matched"] is True
    assert res["preset_token"] == "door"
    assert res["move_result"]["moved"] is True


def test_label_mismatch_does_not_move():
    _fresh()
    configure_rule(camera_id=1, label="person", preset_token="door", cooldown_seconds=60)
    res = handle_detection(camera_id=1, label="cat")
    assert res["moved"] is False
    assert res["matched"] is False


def test_camera_mismatch_does_not_move():
    _fresh()
    configure_rule(camera_id=1, label="person", preset_token="door", cooldown_seconds=60)
    res = handle_detection(camera_id=2, label="person")
    assert res["moved"] is False
    assert res["matched"] is False


def test_label_match_case_insensitive_and_wildcard():
    _fresh()
    configure_rule(camera_id=1, label="Person", preset_token="p1", cooldown_seconds=0)
    assert handle_detection(camera_id=1, label="PERSON")["moved"] is True
    _fresh()
    configure_rule(camera_id=1, label="*", preset_token="p1", cooldown_seconds=0)
    assert handle_detection(camera_id=1, label="anything")["moved"] is True


def test_cooldown_suppresses_repeat_goto():
    _fresh()
    now = time.time()
    configure_rule(camera_id=1, label="person", preset_token="door", cooldown_seconds=60)
    first = handle_detection(camera_id=1, label="person", now=now)
    assert first["moved"] is True
    second = handle_detection(camera_id=1, label="person", now=now + 5)
    assert second["moved"] is False
    assert second["suppressed"] is True
    assert second["cooldown_remaining"] > 0
    # After cooldown expiry the goto fires again.
    third = handle_detection(camera_id=1, label="person", now=now + 61)
    assert third["moved"] is True


def test_force_bypasses_cooldown():
    _fresh()
    configure_rule(camera_id=1, label="person", preset_token="door", cooldown_seconds=60)
    assert handle_detection(camera_id=1, label="person")["moved"] is True
    assert handle_detection(camera_id=1, label="person", force=True)["moved"] is True


def test_service_singleton_mirrors_module():
    _fresh()
    alert_goto_service.configure_rule(camera_id=9, label="dog", preset_token="yard")
    res = alert_goto_service.handle_detection(camera_id=9, label="dog")
    assert res["moved"] is True
    st = alert_goto_service.get_status()
    assert st["count"] == 1


def test_mover_called_best_effort():
    _fresh()
    calls = []

    class FakeMover:
        def goto_preset(self, camera_id, preset):
            calls.append((camera_id, preset))
            return {"ok": True}

    configure_rule(camera_id=3, label="person", preset_token="gate",
                   cooldown_seconds=0, mover=FakeMover())
    res = handle_detection(camera_id=3, label="person")
    assert res["moved"] is True
    assert calls == [(3, "gate")]


def test_router_event_flow():
    _fresh()
    api = FastAPI()
    api.include_router(alert_goto_router)
    client = TestClient(api)

    r = client.post("/api/alert_goto", json={"camera_id": 7, "label": "person",
                                             "preset_token": "front", "cooldown_seconds": 60})
    assert r.status_code == 200, r.text
    assert r.json()["preset_token"] == "front"

    r = client.post("/api/alert_goto/event", json={"camera_id": 7, "label": "person"})
    assert r.status_code == 200, r.text
    assert r.json()["moved"] is True

    # Immediate repeat is cooldown-suppressed.
    r = client.post("/api/alert_goto/trigger", json={"camera_id": 7, "label": "person"})
    assert r.status_code == 200, r.text
    assert r.json()["moved"] is False
    assert r.json()["suppressed"] is True

    # Non-matching label does not move.
    r = client.post("/api/alert_goto/detection", json={"camera_id": 7, "label": "car"})
    assert r.status_code == 200, r.text
    assert r.json()["moved"] is False

    r = client.get("/api/alert_goto")
    assert r.status_code == 200, r.text

    r = client.get("/api/alert-goto/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_main_app_event_endpoint():
    _fresh()
    configure_rule(camera_id=11, label="person", preset_token="lobby", cooldown_seconds=0)
    client = TestClient(app)
    r = client.post("/api/alert_goto/event", json={"camera_id": 11, "label": "person"})
    assert r.status_code == 200, r.text
    assert r.json()["moved"] is True
    assert list_rules()
    assert get_status()["count"] >= 1
    assert alert_goto.ALERT_GOTO_RULES
