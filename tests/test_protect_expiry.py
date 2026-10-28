"""Tests for protection expiry (auto-unlock after N days + policies).

Ground truth verified here: ``app.services.rec_protect`` locks are
permanent -- ``protect_segment`` stores no ``expires_at``/``ttl`` and
``is_protected`` has no time check; only manual ``unprotect``/``clear``
removes a lock. ``app.services.protect_expiry`` layers opt-in expiry on
top (auto-unlock via ``sweep_expired`` + expiry policies).
"""

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.main import app
from app.services import protect_expiry as exp_mod
from app.services import rec_protect as rec_protect_mod
from app.services.protect_expiry import ProtectExpiryService


def _reset():
    rec_protect_mod.clear()
    exp_mod.reset_policy()


def test_protect_expiry_imports():
    import app.services.protect_expiry  # noqa: F401
    import app.api.protect_expiry  # noqa: F401
    from app.services.protect_expiry import (  # noqa: F401
        ProtectExpiryService,
        protect_expiry_service,
        protect_with_expiry,
        is_protected,
        is_expired,
        status_for,
        list_protected,
        list_expired,
        list_expiring,
        sweep_expired,
        exclude_protected,
        set_expiry,
        clear_expiry,
        extend_protection,
        get_expiry,
        get_policy,
        set_policy,
        reset_policy,
    )
    from app.api.protect_expiry import router  # noqa: F401


def test_rec_protect_locks_have_no_expiry(tmp_path):
    """Verify the permanent-lock ground truth before expiry is layered on."""
    _reset()
    # Source-level check: rec_protect owns no expiry concept.
    import inspect

    src = inspect.getsource(rec_protect_mod)
    lowered = src.lower()
    for token in ("expires_at", "expiry", "ttl", "timeout", "sweep"):
        assert token not in lowered, token
    seg = tmp_path / "perm.mp4"
    seg.write_bytes(b"x" * 8)
    rec = rec_protect_mod.protect_segment(str(seg), reason="hold")
    assert "expires_at" not in rec
    assert "expiry" not in {k.lower() for k in rec.keys()}
    # Permanent until manual unprotect.
    assert rec_protect_mod.is_protected(str(seg)) is True
    _reset()


def test_protect_permanent_by_default(tmp_path):
    _reset()
    seg = tmp_path / "a.mp4"
    seg.write_bytes(b"x")
    rec = exp_mod.protect_with_expiry(str(seg), reason="case")
    assert rec.get("protected") is True or rec.get("ok") is True
    assert rec.get("expires_at") is None
    assert exp_mod.is_protected(str(seg)) is True
    assert exp_mod.is_expired(str(seg)) is False
    _reset()


def test_protect_with_expiry_roundtrip(tmp_path):
    _reset()
    seg = tmp_path / "timed.mp4"
    seg.write_bytes(b"x" * 4)
    rec = exp_mod.protect_with_expiry(str(seg), reason="30d hold", expires_in_days=30)
    assert rec.get("expires_at") is not None
    assert exp_mod.is_protected(str(seg)) is True
    detail = exp_mod.get_expiry(str(seg))
    assert detail.get("protected") is True
    assert detail.get("expired") is False
    assert detail.get("remaining_seconds") is not None and detail["remaining_seconds"] > 0
    st = exp_mod.status_for(str(seg))
    assert st.get("protected") is True and st.get("expired") is False
    _reset()


def test_expired_auto_unlock(tmp_path):
    _reset()
    seg = tmp_path / "old.mp4"
    seg.write_bytes(b"z")
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    exp_mod.protect_with_expiry(str(seg), expires_at=past)
    # The raw rec_protect lock exists (permanent layer knows nothing of expiry)...
    assert rec_protect_mod.is_protected(str(seg)) is True
    assert exp_mod.is_expired(str(seg)) is True
    # ...but the expiry-aware check auto-unlocks and reports unprotected.
    assert exp_mod.is_protected(str(seg)) is False
    assert rec_protect_mod.is_protected(str(seg)) is False
    _reset()


def test_sweep_expired(tmp_path):
    _reset()
    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    a.write_bytes(b"a")
    b.write_bytes(b"b")
    past = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    exp_mod.protect_with_expiry(str(a), expires_at=past)
    exp_mod.protect_with_expiry(str(b), expires_in_days=30)
    report = exp_mod.sweep_expired(dry_run=True)
    assert report.get("expired_count", 0) >= 1
    assert rec_protect_mod.is_protected(str(a)) is True  # dry run removes nothing
    report2 = exp_mod.sweep_expired()
    assert report2.get("unlocked_count", 0) >= 1
    assert rec_protect_mod.is_protected(str(a)) is False
    assert rec_protect_mod.is_protected(str(b)) is True
    _reset()


def test_expiry_aware_exclude(tmp_path):
    _reset()
    cam = tmp_path / "cam"
    cam.mkdir(parents=True)
    old = cam / "old.mp4"
    old.write_bytes(b"x" * 16)
    selected = [str(old)]
    past = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    exp_mod.protect_with_expiry(str(old), expires_at=past)
    kept = exp_mod.exclude_protected(selected)
    # Expired lock auto-unlocks so the file becomes deletable again.
    assert str(old) in kept
    _reset()


def test_expiry_policies():
    _reset()
    p0 = exp_mod.get_policy()
    assert p0.get("default_expires_in_days") is None
    exp_mod.set_policy({"default_expires_in_days": 14, "max_expires_in_days": 30})
    p1 = exp_mod.get_policy()
    assert p1.get("default_expires_in_days") == 14
    assert p1.get("max_expires_in_days") == 30
    # Default policy applies when no explicit expiry is passed.
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as td:
        seg = Path(td) / "d.mp4"
        seg.write_bytes(b"q")
        rec = exp_mod.protect_with_expiry(str(seg))
        assert rec.get("expires_at") is not None
        rec2 = exp_mod.protect_with_expiry(str(seg) + "2", expires_in_days=90)
        # Clamped to max 30 days.
        dt = datetime.fromisoformat(str(rec2["expires_at"]).replace("Z", "+00:00"))
        remaining = (dt - datetime.now(timezone.utc)).total_seconds() / 86400.0
        assert remaining <= 31
    exp_mod.reset_policy()
    assert exp_mod.get_policy().get("default_expires_in_days") is None
    _reset()


def test_extend_and_clear_expiry(tmp_path):
    _reset()
    seg = tmp_path / "e.mp4"
    seg.write_bytes(b"e")
    exp_mod.protect_with_expiry(str(seg), expires_in_days=1)
    first = exp_mod.get_expiry(str(seg))["expires_at"]
    exp_mod.extend_protection(str(seg), extra_days=10)
    second = exp_mod.get_expiry(str(seg))["expires_at"]
    assert second is not None and second != first
    exp_mod.clear_expiry(str(seg))
    cleared = exp_mod.get_expiry(str(seg))
    assert cleared.get("expires_at") is None
    assert exp_mod.is_protected(str(seg)) is True
    _reset()


def test_list_expired_and_expiring(tmp_path):
    _reset()
    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    a.write_bytes(b"a")
    b.write_bytes(b"b")
    past = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    exp_mod.protect_with_expiry(str(a), expires_at=past)
    exp_mod.protect_with_expiry(str(b), expires_in_days=2)
    expired = exp_mod.list_expired()
    assert any(str(a) in str(i.get("path")) for i in expired)
    expiring = exp_mod.list_expiring(within_days=7)
    assert any(str(b) in str(i.get("path")) for i in expiring)
    assert not any(str(a) in str(i.get("path")) for i in expiring)
    _reset()


def test_service_instance_aliases(tmp_path):
    _reset()
    svc = ProtectExpiryService()
    seg = tmp_path / "s.mp4"
    seg.write_bytes(b"s")
    svc.protect(str(seg), expires_in_days=30)
    assert svc.is_protected(str(seg)) is True
    assert svc.status_for(str(seg))["protected"] is True
    assert len(svc.list_protected()) >= 1
    svc.clear_expiry(str(seg))
    assert svc.get_expiry(str(seg))["expires_at"] is None
    svc.set_expiry(str(seg), expires_in_days=5)
    assert svc.get_expiry(str(seg))["expires_at"] is not None
    _reset()


def test_protect_expiry_api(tmp_path):
    _reset()
    client = TestClient(app)
    paths = list(app.openapi()["paths"].keys())
    prefix = None
    for cand in ("/api/protect-expiry", "/api/protect_expiry", "/api/rec-protect/expiry"):
        if cand in paths or f"{cand}/protect" in paths:
            prefix = cand
            break
    assert prefix is not None, paths

    seg = tmp_path / "ev.mp4"
    seg.write_bytes(b"e" * 8)

    r = client.post(f"{prefix}/protect", json={"path": str(seg), "expires_in_days": 30, "reason": "case-1"})
    assert r.status_code == 200, r.text
    assert r.json().get("expires_at") is not None

    r2 = client.get(f"{prefix}/status", params={"path": str(seg)})
    assert r2.status_code == 200, r2.text
    assert r2.json().get("protected") is True

    r3 = client.get(f"{prefix}/list")
    assert r3.status_code == 200, r3.text
    assert r3.json().get("count", 0) >= 1

    r4 = client.get(f"{prefix}/expired")
    assert r4.status_code == 200, r4.text

    r5 = client.get(f"{prefix}/policy")
    assert r5.status_code == 200, r5.text

    r6 = client.post(f"{prefix}/policy/set", json={"default_expires_in_days": 7})
    assert r6.status_code == 200, r6.text
    assert r6.json().get("default_expires_in_days") == 7

    r7 = client.post(f"{prefix}/sweep", json={})
    assert r7.status_code == 200, r7.text
    assert r7.json().get("ok") is True
    _reset()
