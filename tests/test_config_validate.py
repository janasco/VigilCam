"""Tests for Frigate-style config dry-run validator (no writes)."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient


def _sample_valid():
    return {
        "groups": [{"name": "ValidateGroup", "description": "dry-run group"}],
        "cameras": [
            {
                "name": "ValidateCam",
                "kind": "ip",
                "source": "rtsp://127.0.0.1/validate",
                "enabled": True,
                "fps": 15,
                "group": "ValidateGroup",
            }
        ],
        "layouts": [{"name": "ValidateLayout", "json_layout": {"grid": [1]}}],
        "storage_areas": [{"name": "ValidateStore", "path": "/data/validate"}],
    }


def test_imports():
    import app.api.config_validate as api_mod  # noqa: F401
    import app.services.config_validate as svc_mod  # noqa: F401
    from app.services.config_validate import (  # noqa: F401
        config_validate_service,
        validate_config_dry_run,
        validate_config,
        extract_sections,
        errors_for,
        warnings_for,
    )

    assert hasattr(api_mod, "router")
    assert hasattr(svc_mod, "config_validate_service")


def test_routes_present():
    from app.api.config_validate import router

    paths = {getattr(r, "path", "") for r in router.routes}
    assert "/api/config-validate/validate" in paths
    assert "/api/config_validate/validate" in paths
    assert "/api/config-validate/dry-run" in paths
    assert "/api/config-validate/check" in paths
    assert "/api/config-validate/health" in paths
    assert "/api/config-validate/sections" in paths


def _client():
    from fastapi import FastAPI

    from app.api.config_validate import router as config_validate_router

    app = FastAPI()
    app.include_router(config_validate_router)
    return TestClient(app)


def test_pure_valid_envelope():
    from app.services import config_validate as svc

    report = svc.validate_config_dry_run(_sample_valid())
    assert report["valid"] is True
    assert report["dry_run"] is True
    assert report["applied"] is False
    assert report["persisted"] is False
    assert report["errors"] == []
    assert report["counts"]["cameras"] == 1
    assert svc.validate_config(_sample_valid()) is True
    assert svc.is_valid(_sample_valid()) is True


def test_pure_envelope_and_string_inputs():
    from app.services import config_validate as svc

    sections = _sample_valid()
    envelope = {"sections": sections, "version": "1.0"}
    assert svc.validate_config_dry_run(envelope)["valid"] is True
    assert svc.validate_config_dry_run(json.dumps(envelope))["valid"] is True
    assert svc.validate_config_dry_run(json.dumps(envelope).encode("utf-8"))["valid"] is True
    # Flat section map also parses.
    assert svc.extract_sections(sections)["cameras"][0]["name"] == "ValidateCam"
    assert svc.extract_sections(envelope)["groups"][0]["name"] == "ValidateGroup"


def test_missing_camera_name_and_source():
    from app.services import config_validate as svc

    bad = {"cameras": [{"source": "rtsp://127.0.0.1/x"}]}
    report = svc.validate_config_dry_run(bad)
    assert report["valid"] is False
    assert any(e["path"] == "name" for e in report["errors"])

    bad2 = {"cameras": [{"name": "NoSource"}]}
    report2 = svc.validate_config_dry_run(bad2)
    assert report2["valid"] is False
    assert any(e["path"] == "source" for e in report2["errors"])

    bad3 = {"cameras": [{"name": "BadSrc", "source": "not a url with spaces !!!"}]}
    assert svc.validate_config_dry_run(bad3)["valid"] is False


def test_duplicate_cameras_and_groups():
    from app.services import config_validate as svc

    dup = {
        "groups": [{"name": "DupGroup"}, {"name": "DupGroup"}],
        "cameras": [
            {"name": "DupCam", "source": "rtsp://127.0.0.1/a"},
            {"name": "DupCam", "source": "rtsp://127.0.0.1/b"},
        ],
    }
    report = svc.validate_config_dry_run(dup)
    assert report["valid"] is False
    codes = [e.get("code") for e in report["errors"]]
    assert "duplicate" in codes


def test_cross_reference_errors():
    from app.services import config_validate as svc

    payload = {
        "groups": [{"name": "RealGroup"}],
        "cameras": [
            {"name": "RefCam", "source": "rtsp://127.0.0.1/x", "group": "MissingGroup"}
        ],
        "ptz_schedules": [{"schedule": "not cron", "camera_id": 99999}],
        "linked_monitor_triggers": [
            {"source_camera_id": "Ghost", "target_camera_id": "Ghost"}
        ],
        "standing_rules": [
            {"sentence": "", "target_object": "", "expires_at": "not-a-date"}
        ],
        "storage_areas": [{"name": "BadStore", "path": "relative/path"}],
        "camera_permissions": [{"user_id": 1, "camera_id": "Ghost", "permission": "bogus"}],
    }
    report = svc.validate_config_dry_run(payload)
    assert report["valid"] is False
    by_section = {}
    for e in report["errors"]:
        by_section.setdefault(e["section"], []).append(e)
    assert "cameras" in by_section
    assert "ptz_schedules" in by_section
    assert "linked_monitor_triggers" in by_section
    assert "standing_rules" in by_section
    assert "storage_areas" in by_section
    assert "camera_permissions" in by_section


def test_warnings_do_not_fail():
    from app.services import config_validate as svc

    payload = {
        "cameras": [
            {
                "name": "WarnCam",
                "source": "rtsp://127.0.0.1/x",
                "enabled": False,
                "fps": 45,
            }
        ],
        "mystery_section": [{"a": 1}],
    }
    report = svc.validate_config_dry_run(payload)
    assert report["valid"] is True
    assert report["warning_count"] >= 2
    assert "mystery_section" in report["unknown_sections"]
    strict_report = svc.validate_config_dry_run(payload, strict=True)
    assert strict_report["strict_valid"] is False


def test_empty_payload_invalid():
    from app.services import config_validate as svc

    assert svc.validate_config_dry_run({})["valid"] is False
    assert svc.validate_config_dry_run(None)["valid"] is False
    assert svc.validate_config_dry_run("")["valid"] is False
    assert svc.validate_config({"bogus": [{"a": 1}]}) is False


def test_api_dry_run_endpoints_do_not_persist():
    from app.services import config_validate as svc

    client = _client()
    # Pure-service snapshot: dry-run must be side-effect free, so two runs
    # over the same payload agree and touch nothing.
    valid = _sample_valid()
    first = svc.validate_config_dry_run({"sections": valid})
    for path in (
        "/api/config-validate/validate",
        "/api/config_validate/validate",
        "/api/config-validate/dry-run",
        "/api/config-validate/check",
        "/api/config-validate",
    ):
        r = client.post(path, json={"sections": valid})
        assert r.status_code == 200, (path, r.text)
        body = r.json()
        assert body["valid"] is True, (path, body)
        assert body["dry_run"] is True
        assert body.get("persisted") is False
        assert body.get("applied") is False

    invalid = {"cameras": [{"name": "", "source": ""}]}
    r = client.post("/api/config-validate/validate", json=invalid)
    assert r.status_code == 200, r.text
    assert r.json()["valid"] is False
    assert len(r.json()["errors"]) >= 1

    second = svc.validate_config_dry_run({"sections": valid})
    assert second == first


def test_api_health_and_sections():
    client = _client()
    h = client.get("/api/config-validate/health")
    assert h.status_code == 200, h.text
    assert h.json()["status"] == "ok"
    assert h.json()["dry_run"] is True

    s = client.get("/api/config-validate/sections")
    assert s.status_code == 200, s.text
    body = s.json()
    assert "cameras" in body["sections"]
    assert "groups" in body["sections"]
    assert "settings" in body["sections"]

    h2 = client.get("/api/config_validate/health")
    assert h2.status_code == 200, h2.text
