"""Cross-camera RECORDING trigger tests.

Verification premise (see app/services/linked_monitor_trigger.py):
``LinkedMonitorTriggerService.trigger`` / ``trigger_linked_monitors`` only
resolve linked target ids -- they never call the recording backend. This
module (app/services/cross_record.py + app/api/cross_record.py) adds the
missing RECORDING fan-out without modifying linked_monitor_trigger.py.

NOTE: these tests intentionally avoid importing app.main (which
auto-imports 300+ routers and is very slow to import). API coverage uses a
minimal FastAPI app mounting only the cross_record router.
"""

from __future__ import annotations

import inspect
import uuid

import app.services.linked_monitor_trigger as lmt
from app.services.cross_record import (
    CrossRecordService,
    get_cross_record_targets,
    trigger_cross_recording,
)


def test_linked_monitor_trigger_has_no_recording_side_effect():
    src = inspect.getsource(lmt)
    lowered = src.lower()
    # It may mention "alert" in a score helper, but must not drive recording.
    assert "recording_manager" not in lowered
    assert "start_recording" not in lowered
    assert "Recording(" not in src
    # Behavioural check: trigger() returns ids only, no recorder invoked.
    svc = lmt.LinkedMonitorTriggerService()
    svc.create(source_camera_id=11, target_camera_id=22)
    assert svc.trigger(11) == [22]


def test_in_memory_service_triggers_recording_not_just_alert():
    calls = []

    def fake_recorder(camera_name, main_url, sub_url=None, **kw):
        calls.append({"camera_name": camera_name, "main_url": main_url,
                      "kwargs": kw})
        return {"started": camera_name}

    svc = CrossRecordService(recorder=fake_recorder)
    svc.create(source_camera_id=101, target_camera_id=202)
    svc.create(source_camera_id=101, target_camera_id=303)
    res = svc.trigger(101, reason="motion")
    assert set(res.started) == {202, 303}
    assert set(res.targets) == {202, 303}
    assert len(calls) == 2
    assert res.recordings_created == 2
    assert res.ok is True
    d = res.to_dict()
    assert set(d["started"]) == {202, 303}
    # aliases
    assert set(svc.fire(101).started) == {202, 303}


def test_in_memory_service_dry_run_does_not_record():
    calls = []

    def fake_recorder(*a, **k):
        calls.append((a, k))
        return True

    svc = CrossRecordService(recorder=fake_recorder)
    svc.create(source_camera_id=1, target_camera_id=2)
    res = svc.trigger(1, dry_run=True)
    assert res.dry_run is True
    assert res.started == []
    assert calls == []
    assert res.targets == [2]


def test_in_memory_service_no_links_returns_empty():
    svc = CrossRecordService(recorder=lambda *a, **k: True)
    res = svc.trigger(9999)
    assert res.targets == []
    assert res.started == []


def test_router_exposes_cross_record_paths():
    from app.api.cross_record import router

    paths = set()
    for route in router.routes:
        path = getattr(route, "path", "")
        if path:
            paths.add(path)
    assert "/api/cross-record/trigger" in paths
    assert "/api/cross_record/trigger" in paths
    assert any("cross-record" in p and "cameras" in p for p in paths)


def _build_test_app():
    """Minimal FastAPI app with only the cross_record router + test DB."""
    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.api.cross_record import router as cross_record_router
    from app.core.database import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    async def _override_session():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as session:
            yield session

    app = FastAPI()
    app.include_router(cross_record_router)
    from app.api.deps import db_session

    app.dependency_overrides[db_session] = _override_session
    return app, SessionLocal, engine


async def _create_camera_and_link(SessionLocal, engine, src_name="src", dst_name="dst"):
    from app.core.database import Base
    from app.models.entities import Camera, LinkedMonitorTrigger

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with SessionLocal() as s:
        c1 = Camera(name=f"{src_name}_{uuid.uuid4().hex[:6]}", kind="usb", source="0")
        c2 = Camera(name=f"{dst_name}_{uuid.uuid4().hex[:6]}", kind="usb", source="1")
        s.add(c1)
        s.add(c2)
        await s.commit()
        await s.refresh(c1)
        await s.refresh(c2)
        link = LinkedMonitorTrigger(source_camera_id=c1.id, target_camera_id=c2.id,
                                    enabled=True, active=True, is_enabled=True)
        s.add(link)
        await s.commit()
        return c1.id, c2.id


def test_db_trigger_starts_recording_and_persists_rows():
    import asyncio

    from app.services import cross_record as cr

    app, SessionLocal, engine = _build_test_app()

    async def _run():
        src, dst = await _create_camera_and_link(SessionLocal, engine)
        calls = []

        def fake_recorder(camera_name, main_url, sub_url=None, **kw):
            calls.append({"camera_name": camera_name, "kw": kw})
            return {"started": camera_name}

        async with SessionLocal() as s:
            res = await trigger_cross_recording(s, src, recorder=fake_recorder,
                                                reason="motion")
            assert dst in res.started
            assert res.recordings_created == 1
            assert res.events_created >= 1
            assert len(calls) == 1
            # rows persisted
            from sqlalchemy import select

            from app.models.entities import Event, Recording

            events = (await s.execute(select(Event))).scalars().all()
            recs = (await s.execute(select(Recording))).scalars().all()
            assert any(e.camera_id == dst and e.kind == "cross_record" for e in events)
            assert any(r.camera_id == dst for r in recs)
        # dry-run: no recorder call, no new rows
        async with SessionLocal() as s2:
            calls2 = []

            def fake2(*a, **k):
                calls2.append((a, k))
                return True

            res2 = await trigger_cross_recording(s2, src, recorder=fake2, dry_run=True)
            assert res2.dry_run is True
            assert res2.started == []
            assert calls2 == []
            targets = await get_cross_record_targets(s2, src)
            assert dst in targets
        await engine.dispose()

    asyncio.run(_run())


def test_api_cross_record_endpoints_minimal_app(monkeypatch):
    from fastapi.testclient import TestClient

    from app.services import cross_record as cr

    app, SessionLocal, engine = _build_test_app()
    calls = []

    def fake_default_recorder(camera_name, main_url, sub_url=None, **kw):
        calls.append({"camera_name": camera_name, "main_url": main_url})
        return {"started": camera_name}

    monkeypatch.setattr(cr, "_default_recorder", fake_default_recorder)
    # Router calls `trigger_cross_recording` which falls back to
    # `cr._default_recorder` at call time, so patching the attribute works.

    import asyncio

    src_dst = asyncio.run(_create_camera_and_link(SessionLocal, engine))
    src, dst = src_dst

    client = TestClient(app)
    g = client.get(f"/api/cameras/{src}/cross-record-targets")
    assert g.status_code == 200
    assert dst in g.json().get("targets", [])

    tr = client.post(f"/api/cameras/{src}/cross-record", json={"reason": "motion"})
    assert tr.status_code == 200
    body = tr.json()
    assert dst in body.get("started", body.get("triggered", []))
    assert len(calls) >= 1

    tr2 = client.post("/api/cross_record/trigger",
                      json={"source_camera_id": src, "dry_run": True})
    assert tr2.status_code == 200
    assert dst in tr2.json().get("targets", [])

    tr3 = client.post("/api/cameras/999999/cross-record", json={"dry_run": True})
    assert tr3.status_code == 200
    assert tr3.json().get("started", []) == []
