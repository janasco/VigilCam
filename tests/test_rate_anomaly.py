from datetime import datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import app as fastapi_app


def _ev(minutes_ago: float, now: datetime, camera_id: int = 1, label: str = "person"):
    return {
        "camera_id": camera_id,
        "label": label,
        "created_at": (now - timedelta(minutes=minutes_ago)).isoformat(),
    }


def test_rate_anomaly_imports():
    import app.api.rate_anomaly  # noqa: F401
    import app.services.rate_anomaly  # noqa: F401
    from app.api.rate_anomaly import router  # noqa: F401
    from app.services.rate_anomaly import (  # noqa: F401
        analyze_rate,
        detect_rate_anomaly,
        get_rate_anomaly,
    )


def test_spike_detection():
    from app.services.rate_anomaly import analyze_rate

    now = datetime(2026, 1, 1, 12, 0, 0)
    # Sparse baseline: one event per hour for 4 hours.
    events = [_ev(60 * h + 30, now) for h in range(1, 5)]
    # Burst: 12 events in the last 10 minutes.
    events += [_ev(m, now) for m in range(1, 11)] + [_ev(2, now), _ev(3, now)]
    out = analyze_rate(
        events,
        baseline_minutes=240,
        current_minutes=15,
        spike_factor=3.0,
        now=now.isoformat(),
    )
    assert out["spike"] is True
    assert out["anomaly"] is True
    assert out["status"] == "spike"
    assert out["current_count"] >= 10
    assert out["baseline_count"] >= 1
    assert any(a["type"] == "spike" for a in out["alerts"])


def test_quiet_detection():
    from app.services.rate_anomaly import analyze_rate

    now = datetime(2026, 1, 1, 12, 0, 0)
    # Busy baseline: 20 events spread over the prior 2 hours (outside current window).
    events = [_ev(30 + i * 5, now) for i in range(20)]
    out = analyze_rate(
        events,
        baseline_minutes=180,
        current_minutes=15,
        quiet_factor=0.25,
        now=now.isoformat(),
    )
    assert out["current_count"] == 0
    assert out["baseline_count"] >= 10
    assert out["quiet"] is True
    assert out["anomaly"] is True
    assert out["status"] == "quiet"


def test_normal_no_anomaly():
    from app.services.rate_anomaly import analyze_rate

    now = datetime(2026, 1, 1, 12, 0, 0)
    # Steady rate: ~1 event per 15 min in both baseline and current.
    events = [_ev(m, now) for m in (10, 40, 70, 100, 130, 160)]
    out = analyze_rate(
        events,
        baseline_minutes=180,
        current_minutes=30,
        spike_factor=3.0,
        quiet_factor=0.25,
        now=now.isoformat(),
    )
    assert out["spike"] is False
    assert out["quiet"] is False
    assert out["anomaly"] is False
    assert out["status"] == "ok"


def test_rate_anomaly_empty_no_crash():
    import asyncio

    from app.services.rate_anomaly import get_rate_anomaly

    data = asyncio.run(get_rate_anomaly(None))
    assert isinstance(data, dict)
    assert data["anomaly"] is False
    assert data["spike"] is False
    assert data["quiet"] is False


def test_rate_anomaly_paths_present():
    # Ensure the router is imported so side-effect registration ran.
    import app.api.rate_anomaly  # noqa: F401

    paths = list(fastapi_app.openapi()["paths"].keys())
    assert "/api/rate_anomaly" in paths or "/api/rate-anomaly" in paths
    assert any("rate_anomaly" in p or "rate-anomaly" in p for p in paths)


def test_rate_anomaly_api_returns_payload():
    import app.api.rate_anomaly  # noqa: F401

    # Pure router (no DB).
    from app.api.rate_anomaly import router

    a = FastAPI()
    a.include_router(router)
    client = TestClient(a)

    now = datetime(2026, 1, 1, 12, 0, 0)
    spike_events = [_ev(60 * h + 30, now) for h in range(1, 5)]
    spike_events += [_ev(m, now) for m in range(1, 11)]

    r = client.post(
        "/api/rate_anomaly/detect",
        json={
            "events": spike_events,
            "baseline_minutes": 240,
            "current_minutes": 15,
            "now": now.isoformat(),
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["spike"] is True
    assert body["anomaly"] is True

    r2 = client.get(
        "/api/rate-anomaly/status",
        params={"baseline_minutes": 240, "current_minutes": 15},
    )
    assert r2.status_code == 200, r2.text
    assert isinstance(r2.json(), dict)


def test_rate_anomaly_api_on_full_app():
    client = TestClient(fastapi_app)
    paths = list(fastapi_app.openapi()["paths"].keys())
    prefix = None
    for cand in ("/api/rate_anomaly", "/api/rate-anomaly", "/api/event_rate"):
        if cand in paths:
            prefix = cand
            break
    assert prefix is not None, paths
    r = client.get(prefix)
    assert r.status_code == 200, r.text
    assert isinstance(r.json(), dict)
