from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.bitrate_history import router as bitrate_history_router
import app.services.bitrate_history as svc
import app.services.bitrate as br


def _make_client():
    app = FastAPI()
    app.include_router(bitrate_history_router)
    return TestClient(app)


def _all_paths(_app):
    out = []
    for r in _app.routes:
        if r.__class__.__name__ == "_IncludedRouter":
            try:
                for sub in r.original_router.routes:
                    p = getattr(sub, "path", None)
                    if p:
                        out.append(p)
            except Exception:  # noqa: BLE001
                continue
        else:
            p = getattr(r, "path", None)
            if p:
                out.append(p)
    try:
        for r in getattr(_app.router, "routes", []):
            p = getattr(r, "path", None)
            if p and p not in out:
                out.append(p)
    except Exception:  # noqa: BLE001
        pass
    return out


def _bytes_sampler(url, duration):
    return {"bytes_received": 8000, "duration_seconds": 2.0}


def test_imports():
    import app.api.bitrate_history  # noqa: F401
    import app.services.bitrate_history  # noqa: F401
    assert bitrate_history_router is not None
    assert hasattr(svc, "record_sample")
    assert hasattr(svc, "record_history") or hasattr(svc, "log_sample")
    assert hasattr(svc, "list_history")
    assert hasattr(svc, "get_entry")
    assert hasattr(svc, "delete_entry")
    assert hasattr(svc, "clear_history")
    assert hasattr(svc, "sample_and_record") or hasattr(svc, "get_stats")
    assert hasattr(svc, "status")


def test_bitrate_point_in_time_has_no_history():
    # Verify base sampler is point-in-time only: single last-known result per target.
    br.clear()
    r1 = br.sample_url("http://127.0.0.1:9/hist-verify", duration=2.0, sampler=_bytes_sampler)
    assert r1.status == "measured"
    n1 = len(br.list_results())
    r2 = br.sample_url("http://127.0.0.1:9/hist-verify", duration=2.0, sampler=_bytes_sampler)
    assert r2.status == "measured"
    # Same target overwrites; no history accumulation in bitrate service.
    assert len(br.list_results()) == n1
    assert not hasattr(br, "BITRATE_HISTORY")
    assert not hasattr(br, "list_history")
    br.clear()


def test_main_app_paths_present():
    from app.main import app as main_app
    paths = _all_paths(main_app)
    assert any(
        p.startswith("/api/bitrate/history")
        or p.startswith("/api/bitrates/history")
        or p.startswith("/api/bitrate-history")
        or p.startswith("/api/bitrate_history")
        for p in paths
    ), paths


def test_service_record_list_get_delete():
    svc.clear_history()
    entry = svc.record_sample(target="http://127.0.0.1:9/cam1", bytes_received=8000,
                              duration_seconds=2.0, status="measured")
    assert entry.get("id")
    assert entry.get("target") == "http://127.0.0.1:9/cam1"
    assert entry.get("bitrate_bps") == 32000.0
    assert entry.get("bitrate_kbps") == 32.0

    items = svc.list_history()
    assert any(str(e.get("id")) == str(entry.get("id")) for e in items)

    fetched = svc.get_entry(entry.get("id"))
    assert fetched is not None
    assert str(fetched.get("id")) == str(entry.get("id"))

    filtered = svc.list_history(target="http://127.0.0.1:9/cam1")
    assert filtered
    assert all(str(e.get("target")) == "http://127.0.0.1:9/cam1" for e in filtered)

    assert svc.delete_entry(entry.get("id")) is True
    assert svc.get_entry(entry.get("id")) is None
    svc.clear_history()


def test_service_history_tracking_appends():
    svc.clear_history()
    br.clear()
    # Same target sampled twice -> bitrate keeps 1 point-in-time result...
    br.sample_url("http://127.0.0.1:9/dup", duration=2.0, sampler=_bytes_sampler)
    br.sample_url("http://127.0.0.1:9/dup", duration=2.0, sampler=_bytes_sampler)
    assert len([r for r in br.list_results() if r.target == "http://127.0.0.1:9/dup"]) == 1
    # ...while history appends every sample as its own entry.
    svc.record_sample(target="http://127.0.0.1:9/dup", bytes_received=8000, duration_seconds=2.0)
    svc.record_sample(target="http://127.0.0.1:9/dup", bytes_received=4000, duration_seconds=2.0)
    items = svc.list_history(target="http://127.0.0.1:9/dup")
    assert len(items) == 2
    assert items[0].get("bytes_received") == 8000
    assert items[1].get("bytes_received") == 4000
    # Per-target stats.
    stats = svc.get_stats(target="http://127.0.0.1:9/dup")
    assert stats["total"] == 2
    assert stats["avg_bitrate_bps"] == round((32000.0 + 16000.0) / 2, 2)
    assert stats["max_bitrate_bps"] == 32000.0
    assert stats["min_bitrate_bps"] == 16000.0
    # Latest helper.
    latest = svc.get_latest_for_target("http://127.0.0.1:9/dup")
    assert latest is not None
    assert latest.get("bytes_received") == 4000
    svc.clear_history()
    br.clear()


def test_service_aliases_and_clear():
    svc.clear_history()
    a = svc.log_sample(target="t1", bytes_received=1000, duration_seconds=2.0)
    b = svc.record_history(target="t1", bytes_received=2000, duration_seconds=2.0)
    c = svc.add_entry(target="t2", bitrate_bps=8000)
    assert a.get("id") and b.get("id") and c.get("id")
    assert len(svc.fetch_history()) >= 3
    assert svc.get(a.get("id")) is not None
    assert svc.status().get("total") >= 3
    n = svc.clear()
    assert n >= 3
    assert svc.list_history() == []
    svc.clear_history()


def test_service_sample_and_record():
    svc.clear_history()
    br.clear()
    entry = svc.sample_and_record("http://127.0.0.1:9/live", duration=2.0, sampler=_bytes_sampler)
    assert entry.get("target") == "http://127.0.0.1:9/live"
    assert entry.get("bitrate_bps") == 32000.0
    assert entry.get("status") == "measured"
    assert len(svc.list_history(target="http://127.0.0.1:9/live")) == 1
    # CSV / JSON helpers.
    text = svc.to_csv(svc.list_history())
    assert "bitrate_bps" in text
    js = svc.to_json(svc.list_history())
    assert "32000" in js
    svc.clear_history()
    br.clear()


def test_router_list_create_get_delete():
    svc.clear_history()
    client = _make_client()

    r = client.post("/api/bitrate/history", json={"target": "http://127.0.0.1:9/rcam",
                                                  "bytes_received": 8000, "duration_seconds": 2.0})
    assert r.status_code in (200, 201), r.text
    created = r.json()
    eid = created.get("id") or created.get("entry_id") or created.get("history_id")
    assert eid

    r_list = client.get("/api/bitrate/history")
    assert r_list.status_code == 200, r_list.text
    assert isinstance(r_list.json(), list)
    assert any(str(e.get("id")) == str(eid) for e in r_list.json())

    r_get = client.get(f"/api/bitrate/history/{eid}")
    assert r_get.status_code == 200, r_get.text

    r_del = client.delete(f"/api/bitrate/history/{eid}")
    assert r_del.status_code == 200, r_del.text

    r_missing = client.get(f"/api/bitrate/history/{eid}")
    assert r_missing.status_code == 404
    svc.clear_history()


def test_router_alias_paths_and_filters():
    svc.clear_history()
    svc.record_sample(target="camA", bytes_received=8000, duration_seconds=2.0, status="measured")
    svc.record_sample(target="camB", bytes_received=100, duration_seconds=2.0, status="error")
    client = _make_client()

    r = client.get("/api/bitrates/history", params={"target": "camA"})
    assert r.status_code == 200, r.text
    assert all(str(e.get("target")) == "camA" for e in r.json())

    r2 = client.get("/api/bitrate-history")
    assert r2.status_code == 200, r2.text
    assert isinstance(r2.json(), list)

    r3 = client.get("/api/bitrate_history")
    assert r3.status_code == 200, r3.text

    r_health = client.get("/api/bitrate/history/health")
    assert r_health.status_code == 200
    assert r_health.json().get("status") == "ok"

    r_count = client.get("/api/bitrate/history/count")
    assert r_count.status_code == 200
    assert r_count.json().get("count") >= 2

    r_status = client.get("/api/bitrate/history/status")
    assert r_status.status_code == 200
    assert r_status.json().get("ok") is True

    r_summary = client.get("/api/bitrate/history/summary")
    assert r_summary.status_code == 200
    assert r_summary.json().get("total") >= 2

    r_target = client.get("/api/bitrate/history/target/camA")
    assert r_target.status_code == 200
    assert all(str(e.get("target")) == "camA" for e in r_target.json())

    r_latest = client.get("/api/bitrate/history/latest/camA")
    assert r_latest.status_code == 200
    assert str(r_latest.json().get("target")) == "camA"
    svc.clear_history()
