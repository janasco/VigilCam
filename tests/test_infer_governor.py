import time

from fastapi.testclient import TestClient

from app.main import app
import app.services.infer_governor as gov


def _fresh(**overrides):
    gov.clear()
    gov.configure(mode="shadow", max_per_minute=60, max_concurrent=4,
                  cooldown_s=30, heartbeat_s=0, budget_s=5.0,
                  trigger="motion", critical_labels=[], always_analyze_cameras=[],
                  interesting_labels=None)
    if overrides:
        gov.configure(**overrides)
    return gov.get_config()


def test_infer_governor_imports():
    import app.services.infer_governor  # noqa: F401
    import app.api.infer_governor  # noqa: F401
    from app.services.infer_governor import (  # noqa: F401
        check_and_acquire, evaluate, infer_governor_service,
        Gate, GateConfig, InferenceBudgetController,
    )
    from app.api.infer_governor import router  # noqa: F401
    assert hasattr(router, "routes")


def test_infer_governor_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/infer-governor/status" in paths
    assert "/api/infer-governor/summary" in paths
    assert "/api/infer_governor/status" in paths


def test_infer_governor_shadow_default_never_refuses():
    _fresh(mode="shadow", max_per_minute=1, max_concurrent=1)
    first = gov.check_and_acquire(camera_id="cam1", adapter="caption", key="t1")
    assert first["allowed"] is True
    assert first["shadow"] is True
    # Budget is now exhausted, but shadow still admits while flagging would_refuse.
    second = gov.check_and_acquire(camera_id="cam1", adapter="caption", key="t2-unique")
    assert second["allowed"] is True
    assert second["would_refuse"] is True
    gov.release(camera_id="cam1", key="t1")
    gov.release(camera_id="cam1", key="t2-unique")
    _fresh()


def test_infer_governor_enforce_rate_budget():
    _fresh(mode="enforce", max_per_minute=1, max_concurrent=10, cooldown_s=0)
    first = gov.check_and_acquire(camera_id="cam1", adapter="caption", key="e1")
    assert first["allowed"] is True
    second = gov.check_and_acquire(camera_id="cam1", adapter="caption", key="e2")
    assert second["allowed"] is False
    assert second["refused"] is True
    assert "budget" in second["reason"] or "rate" in second["reason"]
    metrics = gov.get_metrics()
    assert metrics["refused_total"] >= 1
    _fresh()


def test_infer_governor_enforce_cooldown_and_release():
    _fresh(mode="enforce", max_per_minute=100, max_concurrent=10, cooldown_s=60)
    a = gov.check_and_acquire(camera_id="cam9", adapter="caption", key="same-track")
    assert a["allowed"] is True
    gov.release(camera_id="cam9", key="same-track")
    b = gov.check_and_acquire(camera_id="cam9", adapter="caption", key="same-track")
    # Same key within cooldown -> gate refuses (cooldown), even though slot is free.
    assert b["allowed"] is False
    assert "cooldown" in b["reason"]
    assert gov.should_allow(camera_id="cam9", adapter="caption", key="other-track") is True
    _fresh()


def test_infer_governor_critical_and_always_analyze_rails():
    # Critical labels bypass stationary suppression.
    _fresh(mode="enforce", max_per_minute=100, max_concurrent=10,
           cooldown_s=0, critical_labels=["person"])
    out = gov.check_and_acquire(camera_id="c1", adapter="caption",
                                label="person", stationary=True, key="crit-1")
    assert out["allowed"] is True
    # Non-critical stationary is suppressed by the gate.
    out2 = gov.check_and_acquire(camera_id="c1", adapter="caption",
                                 label="car", stationary=True, key="stat-1")
    assert out2["allowed"] is False
    assert "stationary" in out2["reason"]
    # always_analyze camera bypasses the gate entirely.
    _fresh(mode="enforce", max_per_minute=100, max_concurrent=10,
           cooldown_s=0, always_analyze_cameras=["gate-cam"])
    out3 = gov.check_and_acquire(camera_id="gate-cam", adapter="caption",
                                 label="car", stationary=True, key="gate-1")
    assert out3["allowed"] is True
    assert "always_analyze" in out3["reason"]
    _fresh()


def test_infer_governor_concurrency_and_camera_share():
    _fresh(mode="enforce", max_per_minute=100, max_concurrent=2, cooldown_s=0)
    a = gov.check_and_acquire(camera_id="camA", adapter="caption", key="a1")
    assert a["allowed"] is True
    # Per-camera share is max(1, max_concurrent-1) == 1, so camA is capped here.
    b = gov.check_and_acquire(camera_id="camA", adapter="caption", key="a2")
    assert b["allowed"] is False
    assert "share" in b["reason"] or "concurrency" in b["reason"]
    # A different camera still has the one reserved global slot.
    c = gov.check_and_acquire(camera_id="camB", adapter="caption", key="b1")
    assert c["allowed"] is True
    # Now the box is full.
    d = gov.check_and_acquire(camera_id="camC", adapter="caption", key="c1")
    assert d["allowed"] is False
    gov.release(camera_id="camA", key="a1")
    gov.release(camera_id="camB", key="b1")
    _fresh()


def test_infer_governor_budget_controller_sheds_and_recovers():
    ctl = gov.InferenceBudgetController(configured=4, budget_s=1.0,
                                        settle_frames=2, recover_at=0.6)
    assert ctl.current == 4
    # Sustained over-budget observations halve the budget.
    for _ in range(4):
        ctl.observe(5.0, units_run=1)
    assert ctl.current < 4
    assert ctl.shedding is True
    # Idle samples are not evidence either way.
    before = ctl.current
    assert ctl.observe(0.001, units_run=0) == 0
    assert ctl.current == before
    # Sustained under-budget observations step back up (below ceiling is free).
    ctl._ceiling = None
    for _ in range(15):
        ctl.observe(0.05, units_run=1)
    assert ctl.current > before
    # observe() service endpoint wraps the same controller.
    _fresh(mode="shadow", max_concurrent=4)
    out = gov.observe(latency_s=0.05, units_run=1)
    assert out["ok"] is True
    assert "current" in out
    _fresh()


def test_infer_governor_endpoints():
    _fresh(mode="enforce", max_per_minute=10, max_concurrent=4, cooldown_s=0)
    client = TestClient(app)
    r = client.get("/api/infer-governor/status")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    r = client.get("/api/infer-governor/config")
    assert r.status_code == 200
    assert r.json()["mode"] == "enforce"
    r = client.post("/api/infer-governor/check",
                    json={"camera_id": "camX", "adapter": "caption", "key": "ep1"})
    assert r.status_code == 200
    assert "allowed" in r.json()
    r = client.post("/api/infer-governor/acquire",
                    json={"camera_id": "camX", "adapter": "caption", "key": "ep2"})
    assert r.status_code == 200
    assert r.json()["allowed"] is True
    r = client.post("/api/infer-governor/release",
                    json={"camera_id": "camX", "key": "ep2"})
    assert r.status_code == 200
    r = client.get("/api/infer-governor/metrics")
    assert r.status_code == 200
    assert "allowed_total" in r.json()
    r = client.get("/api/infer-governor/health")
    assert r.status_code == 200
    _fresh()
