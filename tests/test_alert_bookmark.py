import inspect

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import app
from app.api.alert_bookmark import router as alert_bookmark_router  # noqa: F401
from app.services import alert_bookmark as alert_mod  # noqa: F401
from app.services import bookmarks as bookmarks_mod  # noqa: F401
from app.services.alert_bookmark import (  # noqa: F401
    backfill,
    bookmark_for_alert,
    disable,
    enable,
    get_status,
    is_enabled,
    set_config,
    toggle,
    alert_bookmark_service,
)


def test_alert_bookmark_import():
    import app.api.alert_bookmark  # noqa: F401
    import app.services.alert_bookmark  # noqa: F401
    from app.api.alert_bookmark import router  # noqa: F401
    from app.services.alert_bookmark import (  # noqa: F401
        is_enabled, enable, disable, bookmark_for_alert, backfill,
    )


def test_bookmarks_are_manual_only_without_auto():
    """Bookmarks must have no auto-creation on alerts by default."""
    import app.services.bookmarks as bm_mod
    import app.api.bookmarks as bm_api_mod
    import app.services.cameras as cameras_mod

    bm_src = inspect.getsource(bm_mod)
    assert "alert" not in bm_src.lower()

    api_src = inspect.getsource(bm_api_mod)
    assert "alert" not in api_src.lower()

    # Default toggle is off -> auto-bookmark is a no-op, proving manual-only.
    alert_mod.clear()
    bookmarks_mod.clear()
    assert is_enabled() is False
    out = bookmark_for_alert({"id": 999, "camera_id": 1, "label": "motion"})
    assert out["attached"] == 0
    assert out.get("skipped") is True
    assert bookmarks_mod.list_bookmarks() == []

    # record_event core implementation only persists + marks activity.
    src = inspect.getsource(cameras_mod.record_event)
    # Unwrap our hook (and autolink hook) to inspect the core implementation.
    fn = cameras_mod.record_event
    seen_wrappers = 0
    while getattr(fn, "__alert_bookmark_wrapped__", False) or getattr(fn, "__event_autolink_wrapped__", False):
        seen_wrappers += 1
        closure = getattr(fn, "__closure__", None) or ()
        nxt = None
        for cell in closure:
            try:
                val = cell.cell_contents
            except Exception:  # noqa: BLE001
                continue
            if callable(val) and (hasattr(val, "__code__") or hasattr(val, "__closure__")):
                # Heuristic: original record_event has 'camera_id' + 'label' params.
                try:
                    import inspect as _insp

                    params = list(_insp.signature(val).parameters)
                    if "camera_id" in params and "label" in params:
                        nxt = val
                        break
                except Exception:  # noqa: BLE001
                    continue
        if nxt is None:
            break
        fn = nxt
        if seen_wrappers > 5:
            break
    core_src = inspect.getsource(fn)
    assert "bookmark" not in core_src.lower()
    assert "link_event_to_segment" not in core_src


def test_alert_bookmark_toggle():
    alert_mod.clear()
    assert is_enabled() is False
    s = enable()
    assert s["enabled"] is True
    assert is_enabled() is True
    s = disable()
    assert s["enabled"] is False
    assert is_enabled() is False
    s = toggle()
    assert s["enabled"] is True
    s = toggle()
    assert s["enabled"] is False
    s = toggle(enabled=True)
    assert s["enabled"] is True
    alert_mod.clear()
    assert is_enabled() is False


def test_alert_bookmark_service_auto():
    alert_mod.clear()
    bookmarks_mod.clear()
    enable()
    out = bookmark_for_alert({"id": 11, "camera_id": 3, "recording_id": 7,
                              "offset_seconds": 42, "label": "motion"})
    assert out["enabled"] is True
    assert out["attached"] == 1
    assert out["bookmark"] is not None
    assert out["bookmark"]["camera_id"] == 3
    assert out["bookmark"]["recording_id"] == 7
    assert out["bookmark"]["offset_seconds"] == 42
    assert len(bookmarks_mod.list_bookmarks()) == 1
    # disabling stops future auto-bookmarks
    disable()
    out2 = bookmark_for_alert({"id": 12, "camera_id": 3, "label": "motion"})
    assert out2["attached"] == 0
    assert len(bookmarks_mod.list_bookmarks()) == 1


def test_alert_bookmark_explicit_bypasses_toggle():
    alert_mod.clear()
    bookmarks_mod.clear()
    assert is_enabled() is False
    out = bookmark_for_alert({"id": 21, "camera_id": 5, "label": "doorbell"}, force=True)
    assert out["attached"] == 1
    assert len(bookmarks_mod.list_bookmarks()) == 1


def test_alert_bookmark_backfill():
    alert_mod.clear()
    bookmarks_mod.clear()
    # backfill is explicit: works even when toggle is off
    assert is_enabled() is False
    alerts = [
        {"id": 31, "camera_id": 1, "label": "a"},
        {"id": 32, "camera_id": 2, "label": "b"},
    ]
    out = backfill(alerts=alerts)
    assert out["success"] is True
    assert out["alerts_processed"] == 2
    assert out["attached"] == 2
    assert len(bookmarks_mod.list_bookmarks()) == 2
    # dry run attaches nothing
    bookmarks_mod.clear()
    out2 = backfill(alerts=alerts, dry_run=True)
    assert out2["dry_run"] is True
    assert out2["attached"] == 0
    assert bookmarks_mod.list_bookmarks() == []


def test_alert_bookmark_hook_installed():
    import app.services.cameras as cameras_mod

    assert alert_mod.ensure_hook_installed() is True
    assert getattr(cameras_mod.record_event, "__alert_bookmark_wrapped__", False) is True


def test_alert_bookmark_api_toggle_and_status():
    alert_mod.clear()
    a = FastAPI()
    a.include_router(alert_bookmark_router)
    client = TestClient(a)

    s = client.get("/api/alert-bookmarks")
    assert s.status_code == 200, s.text
    assert s.json()["enabled"] is False

    e = client.post("/api/alert-bookmarks/enable", json={})
    assert e.status_code == 200
    assert e.json()["enabled"] is True

    d = client.post("/api/alert-bookmarks/disable", json={})
    assert d.status_code == 200
    assert d.json()["enabled"] is False

    t = client.post("/api/alert-bookmarks/toggle", json={})
    assert t.status_code == 200
    assert t.json()["enabled"] is True
    alert_mod.clear()


def test_alert_bookmark_api_explicit():
    alert_mod.clear()
    bookmarks_mod.clear()
    a = FastAPI()
    a.include_router(alert_bookmark_router)
    client = TestClient(a)
    # explicit bypasses disabled toggle
    assert is_enabled() is False
    r = client.post(
        "/api/alert-bookmarks/bookmark",
        json={"id": 41, "camera_id": 9, "label": "alert-test"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["attached"] == 1
    assert len(bookmarks_mod.list_bookmarks()) >= 1


def test_alert_bookmark_api_backfill():
    alert_mod.clear()
    bookmarks_mod.clear()
    a = FastAPI()
    a.include_router(alert_bookmark_router)
    client = TestClient(a)
    r = client.post(
        "/api/alert-bookmarks/backfill",
        json={"alerts": [{"id": 51, "camera_id": 1, "label": "x"}]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    assert body["attached"] >= 1


def test_alert_bookmark_paths_present():
    router_paths = sorted(set(r.path for r in alert_bookmark_router.routes))
    assert "/api/alert-bookmarks" in router_paths
    assert any("backfill" in p for p in router_paths)
    assert any("enable" in p for p in router_paths)
    # pkgutil auto-loader registers without main.py edits
    assert any(
        getattr(r, "original_router", None) is alert_bookmark_router for r in app.routes
    ), "alert_bookmark router not registered on app"
    client = TestClient(app)
    assert client.get("/api/alert-bookmarks").status_code == 200
    assert client.get("/api/alert_bookmarks").status_code == 200
