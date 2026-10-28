"""Tests for action template variable substitution ({CAMERA}/{TIME}/{LABEL}/{CONFIDENCE}/{EVENT_ID})."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import action_templates as action_templates_api
from app.api.action_templates import router as action_templates_router
from app.services import action_templates as action_templates_mod
from app.services.action_templates import (
    SUPPORTED_VARIABLES,
    build_context,
    render_action,
    render_body,
    render_dict,
    render_string,
    render_subject,
    render_url,
    status,
)


CTX = {
    "camera": "FrontDoor",
    "time": "2026-10-28T12:00:00Z",
    "label": "person",
    "confidence": "0.92",
    "event_id": "123",
}


def test_imports():
    import app.services.action_templates  # noqa: F401
    import app.api.action_templates  # noqa: F401
    from app.services.action_templates import (  # noqa: F401
        build_context, render_string, render_url, render_body,
        render_subject, render_dict, render_action, status,
    )
    from app.api.action_templates import router  # noqa: F401
    assert router is not None


def test_supported_variables():
    for var in ("CAMERA", "TIME", "LABEL", "CONFIDENCE", "EVENT_ID"):
        assert var in SUPPORTED_VARIABLES


def test_build_context_aliases():
    ctx = build_context(camera="FrontDoor", timestamp="T", label="person", score=0.92, eventId=7)
    assert ctx["CAMERA"] == "FrontDoor"
    assert ctx["TIME"] == "T"
    assert ctx["LABEL"] == "person"
    assert ctx["CONFIDENCE"] == "0.92"
    assert ctx["EVENT_ID"] == "7"
    # Direct UPPER keys win.
    ctx2 = build_context({"CAMERA": "Gate", "camera": "Other"})
    assert ctx2["CAMERA"] == "Gate"


def test_render_url():
    url = "http://example.com/hook?cam={CAMERA}&event={EVENT_ID}&label={LABEL}"
    out = render_url(url, CTX)
    assert out == "http://example.com/hook?cam=FrontDoor&event=123&label=person"


def test_render_body_string():
    body = "Motion on {CAMERA} at {TIME}: {LABEL} ({CONFIDENCE}) #{EVENT_ID}"
    out = render_body(body, CTX)
    assert out == "Motion on FrontDoor at 2026-10-28T12:00:00Z: person (0.92) #123"


def test_render_body_dict_recursive():
    payload = {"msg": "Hi {CAMERA}", "nested": {"label": "{LABEL}", "ids": ["{EVENT_ID}", "static"]}}
    out = render_body(payload, CTX)
    assert out["msg"] == "Hi FrontDoor"
    assert out["nested"]["label"] == "person"
    assert out["nested"]["ids"] == ["123", "static"]


def test_render_subject():
    out = render_subject("Alert from {CAMERA}: {LABEL} {CONFIDENCE}", CTX)
    assert out == "Alert from FrontDoor: person 0.92"


def test_double_brace_and_case_insensitive():
    assert render_string("Hi {{CAMERA}}", CTX) == "Hi FrontDoor"
    assert render_string("Hi {{ camera }}", CTX) == "Hi FrontDoor"
    assert render_string("Hi {camera}", CTX) == "Hi FrontDoor"


def test_unknown_placeholders_preserved():
    assert render_string("Hi {UNKNOWN} {CAMERA}", CTX) == "Hi {UNKNOWN} FrontDoor"


def test_render_action_triplet():
    res = render_action(
        url="http://x/{CAMERA}/{EVENT_ID}",
        body={"text": "{LABEL} {CONFIDENCE}"},
        subject="Sub {CAMERA}",
        context=CTX,
    )
    assert res["ok"] is True
    assert res["rendered_url"] == "http://x/FrontDoor/123"
    assert res["rendered_body"] == {"text": "person 0.92"}
    assert res["rendered_subject"] == "Sub FrontDoor"


def _test_client():
    a = FastAPI()
    a.include_router(action_templates_router)
    return TestClient(a)


def test_router_render():
    client = _test_client()
    r = client.post(
        "/api/action_templates/render",
        json={
            "url": "http://example.com/{CAMERA}/{EVENT_ID}",
            "body": "Hi {CAMERA} {LABEL}",
            "subject": "Sub {CAMERA}",
            **CTX,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["rendered_url"] == "http://example.com/FrontDoor/123"
    assert body["rendered_body"] == "Hi FrontDoor person"
    assert body["rendered_subject"] == "Sub FrontDoor"


def test_router_status_and_variables():
    client = _test_client()
    r = client.get("/api/action_templates/status")
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    r = client.get("/api/action_templates/variables")
    assert r.status_code == 200, r.text
    assert "CAMERA" in r.json()["variables"]


def test_main_app_endpoints():
    # Lazy import: app.main pulls in ~300 routers (~3min). Keep collection fast.
    from app.main import app as main_app
    client = TestClient(main_app)
    r = client.get("/api/action_templates")
    assert r.status_code == 200, r.text
    assert "CAMERA" in r.json()["variables"]
    r = client.post(
        "/api/action_templates/render",
        json={"url": "http://x/{CAMERA}", "camera": "Porch"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["rendered_url"] == "http://x/Porch"
