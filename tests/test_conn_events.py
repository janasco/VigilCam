import asyncio

from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.main import app
from app.core.database import SessionLocal, init_db
from app.models.entities import Event


def _run(coro):
    return asyncio.run(coro)


def _unique_cid(base=99200):
    # Millisecond-ish uniqueness without randomness flakiness.
    import time

    return int(base + (int(time.time() * 1000) % 700))


async def _cleanup_camera(cid: int) -> None:
    await init_db()
    async with SessionLocal() as db:
        await db.execute(delete(Event).where(Event.camera_id == cid))
        await db.commit()


def test_conn_events_imports():
    import app.services.conn_events  # noqa: F401
    import app.api.conn_events  # noqa: F401
    from app.services.conn_events import (  # noqa: F401
        emit_disconnect,
        emit_reconnect,
        record_transition,
        list_conn_events,
        get_last_conn_event,
    )
    from app.api.conn_events import router  # noqa: F401
    assert hasattr(router, "routes")


def test_conn_events_paths_present():
    # Fast router inspection (app.openapi() costs 130s+ with 19k paths).
    from app.api import conn_events as ce_api

    router_paths = [getattr(r, "path", "") for r in ce_api.router.routes]
    assert any(p.startswith("/api/conn-events") for p in router_paths), router_paths[:5]
    assert any(p.startswith("/api/conn_events") for p in router_paths), router_paths[:5]
    assert any(p.endswith("/health") for p in router_paths)
    assert any(p.endswith("/summary") for p in router_paths)
    # Auto-loader in app.main must have picked the router up (FastAPI keeps
    # auto-loaded routers as lazy _IncludedRouter placeholders, so scan those).
    included = [r for r in app.routes if type(r).__name__ == "_IncludedRouter"]
    assert any(getattr(r, "original_router", None) is ce_api.router for r in included)


def test_offline_alerts_do_not_persist_events():
    """Premise: offline_alerts sends alerts without persistent Event rows."""
    from app.services import offline_alerts as oa
    from app.services import camera_health as ch

    cid = _unique_cid(99300)
    _run(_cleanup_camera(cid))
    try:
        oa.clear()
        ch.clear()
        ch.mark_offline(cid)
        before = _run(_count_for_camera(cid))
        res = oa.send_offline_alert(cid, reason="offline")
        assert res["alerted"] is True
        after = _run(_count_for_camera(cid))
        assert after == before == 0
    finally:
        oa.clear()
        ch.clear()
        _run(_cleanup_camera(cid))


async def _count_for_camera(cid: int) -> int:
    await init_db()
    async with SessionLocal() as db:
        rows = (await db.execute(select(Event).where(Event.camera_id == cid))).scalars().all()
        return len([r for r in rows if r.kind in ("disconnect", "reconnect")])


def test_conn_events_emit_persists_rows():
    import app.services.conn_events as ce

    cid = _unique_cid(99400)
    _run(_cleanup_camera(cid))
    try:
        before = _run(_count_for_camera(cid))
        assert before == 0

        ev_off = _run(ce.emit_disconnect(None, cid, reason="offline"))
        assert ev_off.kind == "disconnect"
        assert ev_off.camera_id == cid

        ev_on = _run(ce.emit_reconnect(None, cid, reason="online"))
        assert ev_on.kind == "reconnect"

        after = _run(_count_for_camera(cid))
        assert after == 2

        listed = _run(ce.list_conn_events(None, camera_id=cid))
        assert len(listed) >= 2
        assert {e.kind for e in listed} >= {"disconnect", "reconnect"}

        last = _run(ce.get_last_conn_event(None, cid))
        assert last is not None
        assert last.kind == "reconnect"
    finally:
        _run(_cleanup_camera(cid))


def test_conn_events_transition_dedups():
    import app.services.conn_events as ce

    cid = _unique_cid(99500)
    _run(_cleanup_camera(cid))
    try:
        first = _run(ce.record_transition(None, cid, online=False))
        assert first.kind == "disconnect"
        count1 = _run(_count_for_camera(cid))
        # Duplicate offline transition must not insert another row.
        dup = _run(ce.record_transition(None, cid, online=False))
        assert dup is not None
        assert dup.kind == "disconnect"
        count2 = _run(_count_for_camera(cid))
        assert count2 == count1
        # Flip to online inserts exactly one more row.
        up = _run(ce.record_transition(None, cid, online=True))
        assert up.kind == "reconnect"
        assert _run(_count_for_camera(cid)) == count1 + 1
        # Forced duplicate bypasses dedup.
        forced = _run(ce.record_transition(None, cid, online=True, force=True))
        assert forced.kind == "reconnect"
        assert _run(_count_for_camera(cid)) == count1 + 2
    finally:
        _run(_cleanup_camera(cid))


def test_conn_events_endpoints():
    cid = _unique_cid(99600)
    _run(_cleanup_camera(cid))
    try:
        client = TestClient(app)
        r = client.get("/api/conn-events/health")
        assert r.status_code == 200, r.text
        assert r.json()["service"] == "conn_events"

        rd = client.post(f"/api/conn-events/disconnect/{cid}", json={"reason": "offline"})
        assert rd.status_code == 200, rd.text
        assert rd.json()["kind"] == "disconnect"

        rr = client.post(f"/api/conn-events/reconnect/{cid}", json={"reason": "online"})
        assert rr.status_code == 200, rr.text
        assert rr.json()["kind"] == "reconnect"

        rl = client.get(f"/api/conn-events/list?camera_id={cid}")
        assert rl.status_code == 200, rl.text
        assert isinstance(rl.json(), list)
        assert len(rl.json()) >= 2

        rs = client.get("/api/conn-events/summary")
        assert rs.status_code == 200, rs.text
        assert rs.json()["total"] >= 2

        # Underscore alias serves the same data.
        ra = client.get("/api/conn_events/health")
        assert ra.status_code == 200, ra.text
    finally:
        _run(_cleanup_camera(cid))
