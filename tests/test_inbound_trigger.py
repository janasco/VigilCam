import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.services import inbound_trigger


def test_inbound_trigger_imports():
    import app.services.inbound_trigger  # noqa: F401
    import app.api.inbound_trigger  # noqa: F401
    from app.services.inbound_trigger import (  # noqa: F401
        handle_inbound_trigger,
        validate_token,
        normalize_trigger,
    )
    from app.api.inbound_trigger import router  # noqa: F401


def test_inbound_trigger_paths_present():
    from app.api.inbound_trigger import router as inbound_router

    paths = [getattr(r, "path", None) for r in inbound_router.routes]
    assert "/api/inbound-trigger" in paths
    assert "/api/doorbell" in paths
    # Auto-loader in app.main must have picked the router up (FastAPI 0.14x
    # defers include_router() via _IncludedRouter placeholders, so scan those).
    included = [r for r in app.routes if type(r).__name__ == "_IncludedRouter"]
    assert any(getattr(r, "original_router", None) is inbound_router for r in included)


def test_inbound_trigger_is_not_outbound():
    # INBOUND service must not perform outbound delivery: no HTTP client
    # imports and no calls into the outbound actions/webhook_queue path.
    # (Docstring contrast-notes naming those modules are allowed.)
    import inspect

    source = inspect.getsource(inbound_trigger)
    assert "import httpx" not in source
    assert "urllib.request" not in source
    assert "ActionExecutor" not in source
    assert "action_executor" not in source
    assert "webhook_queue." not in source
    assert "enqueue" not in source


def test_validate_token_open_by_default(monkeypatch):
    for key in inbound_trigger.TOKEN_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    assert inbound_trigger.validate_token(None) is True
    assert inbound_trigger.validate_token("anything") is True
    assert inbound_trigger.get_expected_token() is None


def test_validate_token_enforced(monkeypatch):
    monkeypatch.setenv("INBOUND_TRIGGER_TOKEN", "s3cret")
    try:
        assert inbound_trigger.get_expected_token() == "s3cret"
        assert inbound_trigger.validate_token("s3cret") is True
        assert inbound_trigger.validate_token("Bearer s3cret") is True
        assert inbound_trigger.validate_token("wrong") is False
        assert inbound_trigger.validate_token(None) is False
    finally:
        monkeypatch.delenv("INBOUND_TRIGGER_TOKEN", raising=False)


def test_normalize_trigger_defaults():
    norm = inbound_trigger.normalize_trigger({})
    assert norm["kind"]
    assert norm["label"]
    doorbell = inbound_trigger.normalize_trigger({"kind": "doorbell"})
    assert doorbell["kind"] == "doorbell"
    assert doorbell["label"] == "doorbell"
    cam = inbound_trigger.normalize_trigger({"camera_id": "7", "label": "person"})
    assert cam["camera_id"] == 7
    assert cam["label"] == "person"


def test_inbound_trigger_status():
    client = TestClient(app)
    r = client.get("/api/inbound-trigger/status")
    assert r.status_code == 200, r.text
    assert r.json().get("enabled") is True


def _make_camera(client):
    r = client.post(
        "/api/cameras",
        json={"name": "inbound_" + uuid.uuid4().hex[:8], "kind": "usb", "source": "0"},
    )
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def test_inbound_trigger_creates_event():
    client = TestClient(app)
    cam_id = _make_camera(client)
    r = client.post(
        "/api/inbound-trigger",
        json={"camera_id": cam_id, "kind": "motion", "label": "person", "confidence": 0.9},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body.get("success") is True
    assert body.get("camera_id") == cam_id
    assert body.get("label") == "person"
    assert body.get("id") is not None


def test_doorbell_creates_event():
    client = TestClient(app)
    cam_id = _make_camera(client)
    r = client.post("/api/doorbell", json={"camera_id": cam_id})
    assert r.status_code == 201, r.text
    assert r.json().get("kind") == "doorbell"


def test_camera_scoped_trigger():
    client = TestClient(app)
    cam_id = _make_camera(client)
    r = client.post(f"/api/cameras/{cam_id}/inbound-trigger", json={"label": "motion"})
    assert r.status_code == 201, r.text
    assert r.json().get("camera_id") == cam_id


def test_inbound_trigger_token_rejected_when_configured():
    client = TestClient(app)
    cam_id = _make_camera(client)
    with patch.object(inbound_trigger, "get_expected_token", return_value="required-secret"):
        r = client.post("/api/inbound-trigger", json={"camera_id": cam_id, "label": "person"})
        assert r.status_code == 401, r.text
        r2 = client.post(
            "/api/inbound-trigger?token=required-secret",
            json={"camera_id": cam_id, "label": "person"},
        )
        assert r2.status_code == 201, r2.text
