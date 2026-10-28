import os
import time

from fastapi.testclient import TestClient

from app.main import app
from app.services import cleanup_advisor as advisor_mod


def _make_tree(base):
    cam_a = base / "cam_a"
    cam_b = base / "cam_b"
    cam_a.mkdir(parents=True, exist_ok=True)
    cam_b.mkdir(parents=True, exist_ok=True)
    old = cam_a / "old.mp4"
    old.write_bytes(b"x" * 1000)
    old_mtime = time.time() - (10 * 86400)
    os.utime(old, (old_mtime, old_mtime))
    mid = cam_a / "mid.mp4"
    mid.write_bytes(b"y" * 100)
    mid_mtime = time.time() - (5 * 86400)
    os.utime(mid, (mid_mtime, mid_mtime))
    new = cam_b / "new.mp4"
    new.write_bytes(b"z" * 100)
    return old, mid, new


def test_cleanup_advisor_import():
    import app.services.cleanup_advisor  # noqa: F401
    import app.api.cleanup_advisor  # noqa: F401
    from app.services.cleanup_advisor import (  # noqa: F401
        CleanupAdvisor,
        cleanup_advisor,
        suggest,
        advise,
        recommend,
        rank,
        get_suggestions,
    )
    from app.api.cleanup_advisor import router  # noqa: F401


def test_cleanup_advisor_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/cleanup_advisor" in paths or "/api/cleanup-advisor" in paths
    assert any("cleanup" in p and "advisor" in p for p in paths) or any(
        "cleanup" in p and "suggestion" in p for p in paths
    )


def test_cleanup_advisor_suggest_ranked_with_reasons(tmp_path):
    old, mid, new = _make_tree(tmp_path)
    mgr = advisor_mod.CleanupAdvisor(storage_root=str(tmp_path))
    result = mgr.suggest(max_age_days=7)
    assert result.get("advisory") is True
    assert result.get("dry_run") is True
    assert result.get("never_deletes") is True
    suggestions = result.get("suggestions") or []
    assert len(suggestions) >= 1
    # Ranked: oldest over-retention file first.
    assert suggestions[0]["path"] == str(old)
    assert suggestions[0]["rank"] == 1
    for s in suggestions:
        assert s.get("reason")
        assert s.get("reasons")
        assert isinstance(s.get("score"), (int, float))
        assert s.get("rank") >= 1
    # Oldest carries an older_than_retention reason.
    assert any("older_than_retention" in r for r in suggestions[0]["reasons"])
    # Alias parity.
    assert (result.get("ranked") or []) == suggestions or [s["path"] for s in (result.get("ranked") or [])] == [s["path"] for s in suggestions]
    assert result.get("reclaimable_bytes") == sum(s["size_bytes"] for s in suggestions)
    # Nothing deleted.
    assert old.exists() and mid.exists() and new.exists()


def test_cleanup_advisor_quota_reasons(tmp_path):
    old, mid, new = _make_tree(tmp_path)
    mgr = advisor_mod.CleanupAdvisor(storage_root=str(tmp_path))
    result = mgr.suggest(max_age_days=3650, max_total_bytes=150)
    suggestions = result.get("suggestions") or []
    assert len(suggestions) >= 1
    reasons_all = [r for s in suggestions for r in s.get("reasons", [])]
    assert any("quota" in r for r in reasons_all)
    # Oldest file needed to meet quota ranked first.
    assert suggestions[0]["path"] == str(old)


def test_cleanup_advisor_aliases_and_limit(tmp_path):
    _make_tree(tmp_path)
    mgr = advisor_mod.CleanupAdvisor(storage_root=str(tmp_path))
    for fn_name in ("advise", "recommend", "rank", "get_suggestions", "preview", "analyze", "run"):
        fn = getattr(mgr, fn_name)
        result = fn(max_age_days=3650)
        assert isinstance(result, dict)
        assert (result.get("suggestions") or result.get("ranked")) is not None
    limited = mgr.suggest(max_age_days=3650, top_n=1)
    assert len(limited.get("suggestions") or []) == 1


def test_cleanup_advisor_protected_never_suggested(tmp_path):
    from app.services import rec_protect as rp

    old, mid, new = _make_tree(tmp_path)
    try:
        rp.protect_segment(str(old), storage_root=str(tmp_path))
        mgr = advisor_mod.CleanupAdvisor(storage_root=str(tmp_path))
        result = mgr.suggest(max_age_days=1)
        paths = [s["path"] for s in (result.get("suggestions") or [])]
        assert str(old) not in paths
        assert int(result.get("protected_excluded") or 0) >= 1
        assert old.exists()
    finally:
        try:
            rp.clear(storage_root=str(tmp_path))
        except Exception:  # noqa: BLE001
            pass


def test_cleanup_advisor_api(tmp_path):
    client = TestClient(app)
    target = tmp_path / "api"
    old, mid, new = _make_tree(target)

    paths = list(app.openapi()["paths"].keys())
    prefix = None
    for cand in ("/api/cleanup_advisor", "/api/cleanup-advisor", "/api/storage/advisor"):
        if cand in paths:
            prefix = cand
            break
    assert prefix is not None, paths

    r = client.post(prefix, json={"storage_root": str(target), "max_age_days": 7})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data.get("advisory") is True
    suggestions = data.get("suggestions") or []
    assert len(suggestions) >= 1
    assert suggestions[0].get("reason")
    assert suggestions[0].get("reasons")
    assert old.exists()

    r2 = client.get(prefix, params={"storage_root": str(target), "max_age_days": 7})
    assert r2.status_code == 200, r2.text
    assert (r2.json().get("suggestions") or []) != []
    assert old.exists()
