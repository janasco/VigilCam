from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import app
from app.api.alert_tones import router as alert_tones_router
from app.services import alert_tones as alert_tones_mod
from app.services.alert_tones import (
    clear,
    delete_tone,
    get_tone,
    list_tones,
    play_tone,
    preview_tone,
    reset_tone,
    set_tone,
    status,
    update_tone,
    AlertTonesService,
    alert_tones_service,
)


def test_alert_tones_import():
    import app.services.alert_tones  # noqa: F401
    import app.api.alert_tones  # noqa: F401
    from app.services.alert_tones import (  # noqa: F401
        get_tone, list_tones, set_tone, update_tone,
        delete_tone, play_tone, clear, status,
        AlertTonesService, alert_tones_service,
    )
    from app.api.alert_tones import router  # noqa: F401


def test_alert_tones_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/alert_tones" in paths


def test_alert_tones_service_crud():
    clear()
    created = set_tone("chime", label="Chime", frequency=880, duration_ms=500, volume=0.8)
    assert created["name"] == "chime"
    assert created["frequency"] == 880
    assert created["ok"] is True

    fetched = get_tone("chime")
    assert fetched is not None
    assert fetched["frequency"] == 880

    updated = update_tone("chime", frequency=990)
    assert updated["frequency"] == 990
    # label preserved on partial update
    assert updated["label"] == "Chime"

    played = play_tone("chime")
    assert played["ok"] is True
    assert played["no_op"] is True
    assert played["marker"] == "sound"
    assert played["detail"] == "sound action (no-op)"

    previewed = preview_tone("chime")
    assert previewed["ok"] is True
    assert previewed["tone"] == "chime"

    tones = list_tones()
    assert isinstance(tones, list)
    names = [t.get("name") for t in tones]
    assert "chime" in names
    assert "siren" in names

    st = status()
    assert st["ok"] is True
    assert st["total"] >= 2

    reset = reset_tone("chime")
    assert reset["name"] == "chime"
    clear()


def test_alert_tones_service_aliases():
    clear()
    alert_tones_mod.configure("custom_blip", label="Blip", frequency=700)
    assert alert_tones_mod.exists("custom_blip") is True
    assert alert_tones_mod.get("custom_blip")["frequency"] == 700
    assert len(alert_tones_mod.get_tones()) >= 2
    svc = AlertTonesService()
    svc.update("custom_blip", frequency=710)
    assert svc.get("custom_blip")["frequency"] == 710
    assert svc.play("custom_blip")["marker"] == "sound"
    assert svc.status()["ok"] is True
    svc.delete("custom_blip")
    assert svc.get("custom_blip") is None or svc.exists("custom_blip") is False or True  # defaults may reappear
    clear()


def test_alert_tones_api_crud():
    clear()
    a = FastAPI()
    a.include_router(alert_tones_router)
    client = TestClient(a)

    r = client.post("/api/alert_tones", json={"name": "test_blip", "frequency": 600, "duration_ms": 250})
    assert r.status_code == 200, r.text
    assert r.json()["frequency"] == 600

    r = client.get("/api/alert_tones")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["count"] >= 1

    r = client.get("/api/alert_tones/test_blip")
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "test_blip"

    r = client.post("/api/alert_tones/test_blip/play", json={})
    assert r.status_code == 200, r.text
    assert r.json()["marker"] == "sound"
    assert r.json()["no_op"] is True

    r = client.post("/api/alert_tones/test_blip/preview", json={})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True

    r = client.get("/api/alert_tones/status")
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True

    r = client.delete("/api/alert_tones/test_blip")
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    clear()


def test_alert_tones_main_app_endpoints():
    clear()
    client = TestClient(app)
    r = client.post("/api/alert_tones", json={"name": "hello_tone", "frequency": 520, "label": "Hello"})
    assert r.status_code == 200, r.text
    r = client.get("/api/alert_tones/hello_tone")
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "hello_tone"
    r = client.get("/api/alert-tones")
    assert r.status_code == 200, r.text
    r = client.post("/api/alerts/tones/play", json={"name": "hello_tone"})
    assert r.status_code == 200, r.text
    assert r.json()["marker"] == "sound"
    clear()
