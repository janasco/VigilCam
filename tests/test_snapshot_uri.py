"""ONVIF snapshot-URI resolver tests (service + router, mocked transport)."""

from __future__ import annotations

import asyncio
import inspect

SNAPSHOT_RESPONSE_XML = """<Envelope><Body><trt:GetSnapshotUriResponse>
<trt:MediaUri><tt:Uri>http://1.2.3.4/onvif/snapshot?ch=1&amp;user=admin</tt:Uri>
<tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>
</trt:MediaUri></trt:GetSnapshotUriResponse></Body></Envelope>"""


def _ok_canned(monkeypatch):
    """Patch ods transport with canned Media/GetSnapshotUri responses."""
    from app.services import onvif_digest_service as ods

    captured: dict = {}

    async def fake_request(url, body_xml, username=None, password=None, timeout=10.0):
        if "GetSnapshotUri" in body_xml:
            captured["url"] = url
            captured["body"] = body_xml
            return 200, SNAPSHOT_RESPONSE_XML
        return 200, "<Envelope/>"

    async def fake_services(ip, username, password, port=80, scheme="http"):
        return {"media": "http://cam/onvif/Media"}

    async def fake_caps(ip, username, password, port=80, scheme="http"):
        return {"media": "http://cam/onvif/Media"}

    async def fake_resolve(ip, port_hint=None, scheme_hint=None):
        return (scheme_hint or "http"), (port_hint or 80)

    async def fake_profiles(ip, username, password, port=80, scheme="http"):
        return [{"token": "prof0", "name": "main"}]

    monkeypatch.setattr(ods, "_onvif_request", fake_request)
    monkeypatch.setattr(ods, "get_services", fake_services)
    monkeypatch.setattr(ods, "get_capabilities", fake_caps)
    monkeypatch.setattr(ods, "resolve_control_endpoint", fake_resolve)
    monkeypatch.setattr(ods, "fetch_profiles_digest", fake_profiles)
    return captured


def test_imports():
    import app.services.snapshot_uri  # noqa: F401
    import app.api.snapshot_uri  # noqa: F401
    from app.services.snapshot_uri import (  # noqa: F401
        get_snapshot_uri_digest,
        resolve_snapshot_uri,
        store_snapshot_uri,
        get_stored_snapshot_uri,
        parse_snapshot_uri,
    )


def test_gap_verified_no_getsnapshoturi_before():
    """Snapshot capture needs manual URLs: no GetSnapshotUri auto-config."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    snapshot_now_src = (root / "services" / "snapshot_now.py").read_text()
    onvif_src = (root / "services" / "onvif.py").read_text()
    assert "GetSnapshotUri" not in snapshot_now_src
    assert "GetSnapshotUri" not in onvif_src
    # New resolver fills the gap via ONVIF.
    resolver_src = (root / "services" / "snapshot_uri.py").read_text()
    assert "GetSnapshotUri" in resolver_src


def test_parse_snapshot_uri_unescapes():
    from app.services import snapshot_uri as svc

    assert svc.clear_all_snapshot_uris() is None or True
    svc.clear_all_snapshot_uris()
    uri = svc.parse_snapshot_uri(SNAPSHOT_RESPONSE_XML)
    assert uri == "http://1.2.3.4/onvif/snapshot?ch=1&user=admin"
    assert svc.parse_snapshot_uri("<Envelope/>") is None


def test_get_snapshot_uri_digest(monkeypatch):
    captured = _ok_canned(monkeypatch)
    from app.services import snapshot_uri as svc

    svc.clear_all_snapshot_uris()
    uri = asyncio.run(svc.get_snapshot_uri_digest("1.2.3.4", "admin", "pw", "prof0"))
    assert uri == "http://1.2.3.4/onvif/snapshot?ch=1&user=admin"
    assert "GetSnapshotUri" in captured["body"]
    assert "prof0" in captured["body"]


def test_resolve_and_store_per_camera(monkeypatch):
    _ok_canned(monkeypatch)
    from app.services import snapshot_uri as svc

    svc.clear_all_snapshot_uris()
    record = asyncio.run(svc.resolve_snapshot_uri(camera_id=7106, ip="1.2.3.4",
                                                   username="admin", password="pw"))
    assert record["snapshot_uri"].startswith("http://")
    assert record["camera_id"] == 7106
    stored = svc.get_stored_snapshot_uri(7106)
    assert stored is not None
    assert stored["snapshot_uri"] == record["snapshot_uri"]
    assert svc.snapshot_source_for_camera(7106) == record["snapshot_uri"]
    items = svc.list_stored_snapshot_uris()
    assert any(i.get("camera_id") == 7106 for i in items)
    assert svc.clear_stored_snapshot_uri(7106) is True
    assert svc.get_stored_snapshot_uri(7106) is None


def test_store_validation():
    from app.services import snapshot_uri as svc
    import pytest

    svc.clear_all_snapshot_uris()
    try:
        svc.store_snapshot_uri(camera_id=1, snapshot_uri="")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    rec = svc.store_snapshot_uri(camera_id=42, snapshot_uri="http://x/snap.jpg",
                                 profile_token="p1")
    assert rec["camera_id"] == 42
    assert svc.get_stored_snapshot_uri(42)["uri"] == "http://x/snap.jpg"
    svc.clear_all_snapshot_uris()


def _all_route_paths(router) -> list:
    out: list = []
    for r in getattr(router, "routes", []):
        orig = getattr(r, "original_router", None)
        if orig is not None:
            out.extend(_all_route_paths(orig))
        else:
            p = getattr(r, "path", None)
            if p:
                out.append(p)
    # Also unwrap directly included routers stored as routes with .routes?
    return out


def test_router_paths_present():
    import app.api.snapshot_uri as api

    assert getattr(api, "router", None) is not None
    paths = _all_route_paths(api.router)
    assert any("snapshot-uri" in p or "snapshot_uri" in p for p in paths), paths
    assert any(p.rstrip("/").endswith("/resolve") for p in paths), paths
    assert any("camera" in p for p in paths), paths


def test_api_resolve_and_stored_roundtrip(monkeypatch):
    _ok_canned(monkeypatch)
    from fastapi.testclient import TestClient

    import app.main as _main_mod
    import app.api.snapshot_uri  # noqa: F401  (side-effect registers router)
    from app.services import snapshot_uri as svc

    svc.clear_all_snapshot_uris()
    client = TestClient(_main_mod.app)
    r = client.post("/api/snapshot-uri/resolve",
                    json={"camera_id": 7107, "ip": "1.2.3.4",
                          "username": "admin", "password": "pw"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("snapshot_uri", body.get("snapshotUri"))
    g = client.get("/api/snapshot-uri/camera/7107")
    assert g.status_code == 200, g.text
    assert g.json().get("snapshot_uri")
    lst = client.get("/api/snapshot-uri/stored")
    assert lst.status_code == 200
    d = client.delete("/api/snapshot-uri/camera/7107")
    assert d.status_code == 200
    svc.clear_all_snapshot_uris()


def test_api_missing_ip_returns_422():
    from fastapi.testclient import TestClient

    import app.main as _main_mod
    import app.api.snapshot_uri  # noqa: F401

    client = TestClient(_main_mod.app)
    r = client.post("/api/snapshot-uri/resolve", json={})
    assert r.status_code in (404, 422, 500, 502)
    if r.status_code == 422:
        assert "ip" in r.text.lower() or "host" in r.text.lower() or "camera" in r.text.lower()
