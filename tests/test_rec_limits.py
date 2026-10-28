"""Tests for recording stop-condition limits (max-duration, min-duration, inactivity)."""

from app.services import rec_limits as rec_limits_mod
from app.services.rec_limits import RecLimitsService


def _reset():
    rec_limits_mod.clear_limits()


def test_rec_limits_imports():
    import app.services.rec_limits  # noqa: F401
    import app.api.rec_limits  # noqa: F401
    from app.services.rec_limits import (  # noqa: F401
        RecLimitsService,
        rec_limits_service,
        set_limits,
        get_limits,
        clear_limits,
        evaluate,
        should_stop,
        should_split,
        should_discard,
    )
    from app.api.rec_limits import router  # noqa: F401


def test_ingestion_segments_fixed_size_no_stop_conditions():
    """Guard: ingestion keeps fixed-size segments; retention/preview prune only."""
    import inspect
    from app.services import ingestion as ing_mod
    from app.services import retention as ret_mod
    from app.services import retention_preview as prev_mod

    src_ing = inspect.getsource(ing_mod.StreamCopyIngestor.start_camera)
    assert "segment_time" in src_ing
    assert "segment_seconds" in src_ing
    # no auto stop conditions inside the ingestor
    low = src_ing.lower()
    assert "max_duration" not in low
    assert "min_duration" not in low
    assert "inactivity" not in low

    src_ret = inspect.getsource(ret_mod.RetentionManager)
    assert "max_age" in src_ret.lower() or "max_total" in src_ret.lower()
    assert "inactivity" not in src_ret.lower()

    src_prev = inspect.getsource(prev_mod.RetentionPreviewService)
    assert "preview" in src_prev.lower()
    assert "inactivity" not in src_prev.lower()


def test_max_duration_split():
    _reset()
    rec_limits_mod.set_limits(max_duration_seconds=300)
    res = rec_limits_mod.evaluate(elapsed_seconds=301)
    assert res["should_stop"] is True
    assert res["should_split"] is True
    assert "max_duration" in res["reasons"]
    assert res["action"] == "split"

    res2 = rec_limits_mod.evaluate(elapsed_seconds=10)
    assert res2["should_stop"] is False
    assert res2["should_split"] is False
    _reset()


def test_min_duration_discard():
    _reset()
    rec_limits_mod.set_limits(min_duration_seconds=5)
    short = rec_limits_mod.evaluate(final_duration_seconds=2)
    assert short["should_discard"] is True
    assert short["keep"] is False
    assert "min_duration" in short["reasons"]

    long = rec_limits_mod.evaluate(final_duration_seconds=30)
    assert long["should_discard"] is False
    assert long["keep"] is True
    _reset()


def test_inactivity_stop():
    _reset()
    rec_limits_mod.set_limits(inactivity_timeout_seconds=60)
    idle = rec_limits_mod.evaluate(elapsed_seconds=10, inactive_seconds=61)
    assert idle["should_stop"] is True
    assert idle["hit_inactivity"] is True
    assert "inactivity" in idle["reasons"]

    active = rec_limits_mod.evaluate(elapsed_seconds=10, inactive_seconds=5)
    assert active["should_stop"] is False
    _reset()


def test_per_camera_override():
    _reset()
    rec_limits_mod.set_limits(max_duration_seconds=3600)
    rec_limits_mod.set_limits(camera="cam1", max_duration_seconds=60)
    r1 = rec_limits_mod.evaluate(camera="cam1", elapsed_seconds=61)
    assert r1["should_split"] is True
    r2 = rec_limits_mod.evaluate(camera="cam2", elapsed_seconds=61)
    assert r2["should_split"] is False
    svc = RecLimitsService()
    assert svc.get_limits(camera="cam1")["max_duration_seconds"] == 60
    _reset()


def test_service_instance_aliases():
    _reset()
    svc = RecLimitsService()
    svc.set_limits(max_duration_seconds=100, min_duration_seconds=2,
                   inactivity_timeout_seconds=30)
    assert svc.should_stop(elapsed_seconds=101)["should_stop"] is True
    assert svc.should_split(elapsed_seconds=101)["should_split"] is True
    assert svc.should_discard(final_duration_seconds=1)["should_discard"] is True
    assert svc.is_too_long(elapsed_seconds=101) is True
    assert svc.is_too_short(final_duration_seconds=1) is True
    assert svc.is_inactive(inactive_seconds=31) is True
    _reset()


def test_rec_limits_api():
    _reset()
    # Functional check on an isolated app (fast: avoids full app.openapi()).
    from fastapi import FastAPI
    from fastapi.testclient import TestClient as _TC
    from app.api.rec_limits import router as rec_limits_router

    probe = FastAPI()
    probe.include_router(rec_limits_router)
    client = _TC(probe)
    prefix = "/api/rec-limits"

    r = client.post(f"{prefix}/set", json={"max_duration_seconds": 300})
    assert r.status_code == 200, r.text
    assert r.json().get("max_duration_seconds") == 300

    r2 = client.get(f"{prefix}/get")
    assert r2.status_code == 200, r2.text
    assert r2.json().get("max_duration_seconds") == 300

    r3 = client.post(f"{prefix}/evaluate", json={"elapsed_seconds": 301})
    assert r3.status_code == 200, r3.text
    assert r3.json().get("should_stop") is True
    assert r3.json().get("should_split") is True

    r4 = client.post(f"{prefix}/clear", json={})
    assert r4.status_code == 200, r4.text
    _reset()


def test_rec_limits_router_registered_in_main_app():
    # Auto-registration: app.main imports every app.api module and includes
    # its router, so no app/main.py edit is needed. Newer FastAPI keeps
    # included routers as _IncludedRouter entries, so check the registry
    # plus any flattened APIRoute paths (cheap; avoids full OpenAPI build).
    import sys
    from app.main import app as main_app
    from app.api import rec_limits as rec_limits_api

    assert "app.api.rec_limits" in sys.modules

    registered_ids = getattr(main_app, "_registered_router_ids", set()) or set()
    if registered_ids:
        assert id(rec_limits_api.router) in registered_ids
        _reset()
        return

    # Fallback for older FastAPI flattening: scan flattened paths.
    route_paths: set = set()
    for route in getattr(main_app, "routes", []):
        path = getattr(route, "path", None)
        if path:
            route_paths.add(path)
    # Also inspect nested routers if present.
    for route in getattr(main_app, "routes", []):
        inner = getattr(route, "original_router", None) or getattr(
            getattr(route, "include_context", None), "included_router", None
        )
        if inner is not None and inner is rec_limits_api.router:
            _reset()
            return
    assert any(
        p == "/api/rec-limits/set" or p.startswith("/api/rec-limits")
        or p.startswith("/api/rec_limits") or p.startswith("/api/recording-limits")
        or p.startswith("/api/recordings/limits")
        for p in route_paths
    ), sorted(route_paths)[:10]
    _reset()
