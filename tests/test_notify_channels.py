from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import app
from app.api.notify_channels import router as notify_channels_router
from app.services import notify_channels as notify_channels_mod
from app.services.notify_channels import (
    clear,
    delete_channel,
    disable_channel,
    enable_channel,
    get_channel,
    is_enabled,
    list_channels,
    reset_channel,
    set_channel,
    set_enabled,
    status,
    update_channel,
    NotifyChannelsService,
    notify_channels_service,
)


def test_notify_channels_import():
    import app.services.notify_channels  # noqa: F401
    import app.api.notify_channels  # noqa: F401
    from app.services.notify_channels import (  # noqa: F401
        get_channel, list_channels, set_channel, update_channel,
        enable_channel, disable_channel, clear, status,
        NotifyChannelsService, notify_channels_service,
    )
    from app.api.notify_channels import router  # noqa: F401


def test_notify_channels_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/notify_channels" in paths


def test_notify_channels_service_crud():
    clear()
    cfg = set_channel("email", enabled=True, config={"smtp_host": "localhost"})
    assert cfg["channel"] == "email"
    assert cfg["enabled"] is True
    assert cfg["config"]["smtp_host"] == "localhost"
    assert cfg["ok"] is True

    fetched = get_channel("email")
    assert fetched is not None
    assert fetched["enabled"] is True

    updated = update_channel("email", config={"smtp_port": 25})
    assert updated["config"]["smtp_host"] == "localhost"
    assert updated["config"]["smtp_port"] == 25

    disabled = disable_channel("email")
    assert disabled["enabled"] is False
    assert is_enabled("email") is False

    enabled = enable_channel("email")
    assert enabled["enabled"] is True
    assert is_enabled("email") is True

    channels = list_channels()
    assert isinstance(channels, list)
    names = [c.get("channel") for c in channels]
    assert "email" in names
    assert "sms" in names

    st = status()
    assert st["ok"] is True
    assert st["total"] >= 2

    reset = reset_channel("email")
    assert reset["channel"] == "email"
    assert reset["enabled"] is False
    clear()


def test_notify_channels_service_aliases():
    clear()
    notify_channels_mod.configure("sms", enabled=True, config={"from": "+1000"})
    assert notify_channels_mod.is_enabled("sms") is True
    assert notify_channels_mod.get("sms")["enabled"] is True
    assert len(notify_channels_mod.get_channels()) >= 2
    svc = NotifyChannelsService()
    svc.disable("sms")
    assert svc.is_enabled("sms") is False
    svc.enable("sms")
    assert svc.is_enabled("sms") is True
    assert svc.status()["ok"] is True
    clear()


def test_notify_channels_api_crud():
    clear()
    a = FastAPI()
    a.include_router(notify_channels_router)
    client = TestClient(a)

    r = client.post("/api/notify_channels", json={"channel": "webhook", "enabled": True, "config": {"url": "http://x"}})
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is True

    r = client.get("/api/notify_channels")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["count"] >= 1

    r = client.get("/api/notify_channels/webhook")
    assert r.status_code == 200, r.text
    assert r.json()["channel"] == "webhook"

    r = client.post("/api/notify_channels/webhook/disable", json={})
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is False

    r = client.post("/api/notify_channels/webhook/enable", json={})
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is True

    r = client.get("/api/notify_channels/status")
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True

    r = client.delete("/api/notify_channels/webhook")
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    clear()


def test_notify_channels_main_app_endpoints():
    clear()
    client = TestClient(app)
    r = client.post("/api/notify_channels", json={"channel": "email", "enabled": False, "config": {}})
    assert r.status_code == 200, r.text
    r = client.get("/api/notify_channels/email")
    assert r.status_code == 200, r.text
    assert r.json()["channel"] == "email"
    r = client.get("/api/notify-channels")
    assert r.status_code == 200, r.text
    clear()
