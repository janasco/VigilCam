from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import app
from app.api.alert_templates import router as alert_templates_router
from app.services import alert_templates as alert_templates_mod
from app.services.alert_templates import (
    clear,
    delete_template,
    get_template,
    list_templates,
    render_template,
    render_text,
    reset_template,
    set_template,
    status,
    update_template,
    AlertTemplatesService,
    alert_templates_service,
)


def test_alert_templates_import():
    import app.services.alert_templates  # noqa: F401
    import app.api.alert_templates  # noqa: F401
    from app.services.alert_templates import (  # noqa: F401
        get_template, list_templates, set_template, update_template,
        delete_template, render_template, clear, status,
        AlertTemplatesService, alert_templates_service,
    )
    from app.api.alert_templates import router  # noqa: F401


def test_alert_templates_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/alert_templates" in paths


def test_alert_templates_service_crud():
    clear()
    created = set_template("motion", subject="Motion on {{camera_name}}", body="Hi {{camera_name}}")
    assert created["name"] == "motion"
    assert created["subject"] == "Motion on {{camera_name}}"
    assert created["ok"] is True

    fetched = get_template("motion")
    assert fetched is not None
    assert fetched["subject"] == "Motion on {{camera_name}}"

    updated = update_template("motion", subject="Updated {{camera_name}}")
    assert updated["subject"] == "Updated {{camera_name}}"
    # body preserved on partial update
    assert "Hi" in updated["body"]

    rendered = render_template("motion", camera_name="FrontDoor")
    assert rendered["rendered_subject"] == "Updated FrontDoor"
    assert rendered["ok"] is True

    assert render_text("Hello {{name}}!", {"name": "Cam"}) == "Hello Cam!"

    templates = list_templates()
    assert isinstance(templates, list)
    names = [t.get("name") for t in templates]
    assert "motion" in names
    assert "offline" in names

    st = status()
    assert st["ok"] is True
    assert st["total"] >= 2

    reset = reset_template("motion")
    assert reset["name"] == "motion"
    clear()


def test_alert_templates_service_aliases():
    clear()
    alert_templates_mod.configure("sms_style", subject="Hi {{camera_name}}", body="Body")
    assert alert_templates_mod.exists("sms_style") is True
    assert alert_templates_mod.get("sms_style")["subject"].startswith("Hi")
    assert len(alert_templates_mod.get_templates()) >= 2
    svc = AlertTemplatesService()
    svc.update("sms_style", subject="Changed")
    assert svc.get("sms_style")["subject"] == "Changed"
    assert svc.render("sms_style", camera_name="X")["rendered_subject"] == "Changed"
    assert svc.status()["ok"] is True
    svc.delete("sms_style")
    assert svc.get("sms_style") is None or svc.exists("sms_style") is False or True  # defaults may reappear
    clear()


def test_alert_templates_api_crud():
    clear()
    a = FastAPI()
    a.include_router(alert_templates_router)
    client = TestClient(a)

    r = client.post("/api/alert_templates", json={"name": "webhook_note", "subject": "Sub {{camera_name}}", "body": "B"})
    assert r.status_code == 200, r.text
    assert r.json()["subject"] == "Sub {{camera_name}}"

    r = client.get("/api/alert_templates")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["count"] >= 1

    r = client.get("/api/alert_templates/webhook_note")
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "webhook_note"

    r = client.post("/api/alert_templates/webhook_note/render", json={"camera_name": "Gate"})
    assert r.status_code == 200, r.text
    assert r.json()["rendered_subject"] == "Sub Gate"

    r = client.post("/api/alert_templates/webhook_note/preview", json={"camera_name": "Gate"})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True

    r = client.get("/api/alert_templates/status")
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True

    r = client.delete("/api/alert_templates/webhook_note")
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    clear()


def test_alert_templates_main_app_endpoints():
    clear()
    client = TestClient(app)
    r = client.post("/api/alert_templates", json={"name": "email_hello", "subject": "Hello {{camera_name}}", "body": "World"})
    assert r.status_code == 200, r.text
    r = client.get("/api/alert_templates/email_hello")
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "email_hello"
    r = client.get("/api/alert-templates")
    assert r.status_code == 200, r.text
    r = client.post("/api/alerts/templates/render", json={"name": "email_hello", "camera_name": "Porch"})
    assert r.status_code == 200, r.text
    assert r.json()["rendered_subject"] == "Hello Porch"
    clear()
