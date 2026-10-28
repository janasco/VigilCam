"""Tests for the guided setup wizard backend."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


def test_setup_is_single_complete_flag_no_stepwise_state():
    from app.core import state as st
    from pathlib import Path

    assert hasattr(st, "is_initialized")
    assert hasattr(st, "mark_initialized")
    # Single complete flag file.
    assert Path(str(st.LOCK_PATH)).name == "initialized.lock"
    assert "initialized.lock" in str(st.LOCK_PATH)
    # No stepwise state machine module / files.
    import importlib.util as _ilu

    assert _ilu.find_spec("app.services.setup_progress") is None
    assert _ilu.find_spec("app.services.setup_state") is None
    import pathlib

    repo_files = list(pathlib.Path("app/services").glob("setup_*"))
    names = sorted(p.name for p in repo_files)
    # Only the wizard service itself (no stepwise state machine).
    assert names == ["setup_wizard.py"], names


def test_imports():
    import app.services.setup_wizard  # noqa: F401
    import app.api.setup_wizard  # noqa: F401
    import app.main  # noqa: F401


def test_steps_order_and_progress():
    from app.services import setup_wizard as svc

    assert svc.WIZARD_STEPS == ["environment", "discovery", "storage", "recording", "finish"]
    steps = svc.list_steps()
    assert [s["id"] for s in steps] == svc.WIZARD_STEPS

    assert svc.get_step_index("environment") == 0
    assert svc.get_step_index("finish") == 4

    first = svc.get_progress(current_step="environment")
    assert first["total_steps"] == 5
    assert first["current_step"] == "environment"
    assert first["percent"] == 20.0
    assert first["next_step"] == "discovery"

    mid = svc.get_progress(current_step="storage")
    assert mid["percent"] == 60.0
    assert mid["current_step"] == "storage"

    last = svc.get_progress(current_step="finish")
    assert last["percent"] == 100.0
    assert last["next_step"] is None

    explicit = svc.get_progress(completed_steps=["environment", "discovery"])
    assert explicit["completed_count"] == 2
    assert explicit["percent"] == 40.0

    with pytest.raises(ValueError):
        svc.validate_step("nope")


def test_environment_storage_discovery_recording_helpers():
    from app.services import setup_wizard as svc

    env = svc.get_environment_info()
    assert env["service"]
    assert "storage_root" in env
    assert "initialized" in env

    stor = svc.check_storage()
    assert stor["path"]
    assert "free_bytes" in stor
    assert "total_bytes" in stor

    disc = svc.summarize_discovery([{"ip": "1.2.3.4", "new": True}], [{"id": 1}])
    assert disc["total"] == 1
    assert disc["camera_count"] == 1

    rec = svc.summarize_recording({"active": ["a"], "active_count": 1})
    assert rec["active_count"] == 1


def test_router_registered_and_endpoints():
    import app.api.setup_wizard as api_mod
    from app.main import app

    assert hasattr(api_mod, "router")
    paths = list(app.openapi()["paths"].keys())
    assert "/api/setup-wizard/steps" in paths
    assert "/api/setup-wizard/progress" in paths
    assert "/api/setup-wizard/environment" in paths
    assert "/api/setup-wizard/storage" in paths
    assert "/api/setup-wizard/recording/status" in paths
    assert "/api/setup-wizard/status" in paths

    client = TestClient(app)

    r = client.get("/api/setup-wizard/steps")
    assert r.status_code == 200
    assert [s["id"] for s in r.json()] == ["environment", "discovery", "storage", "recording", "finish"]

    r = client.get("/api/setup-wizard/progress", params={"current_step": "storage"})
    assert r.status_code == 200
    assert r.json()["percent"] == 60.0

    r = client.get("/api/setup-wizard/environment")
    assert r.status_code == 200
    assert "storage_root" in r.json()

    r = client.get("/api/setup-wizard/storage")
    assert r.status_code == 200
    assert "path" in r.json()

    r = client.get("/api/setup-wizard/recording/status")
    assert r.status_code == 200
    assert "active_count" in r.json()

    r = client.get("/api/setup-wizard/status")
    assert r.status_code == 200
    body = r.json()
    assert "progress" in body
    assert "environment" in body
    assert "initialized" in body


def test_finish_uses_single_flag(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    import app.services.setup_wizard as svc

    seen = {}

    def fake_mark():
        seen["marked"] = True

    def fake_is():
        return True

    monkeypatch.setattr(svc, "mark_initialized", fake_mark, raising=False)
    # Patch where finish_setup looks it up (app.core.state).
    import app.core.state as st

    monkeypatch.setattr(st, "mark_initialized", fake_mark)
    monkeypatch.setattr(st, "is_initialized", fake_is)

    client = TestClient(app)
    r = client.post("/api/setup-wizard/finish")
    assert r.status_code == 200
    assert r.json()["initialized"] is True
    assert seen.get("marked") is True
