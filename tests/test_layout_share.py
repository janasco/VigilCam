"""Tests for layout export/import (sharing)."""

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services import layout_share as svc
from app.api import layout_share as layout_share_api


def _sample_layout(name="Lobby View", json_layout=None):
    return {
        "name": name,
        "json_layout": json_layout if json_layout is not None else {"grid": [1]},
        "id": 999,  # runtime field, must be stripped
        "created_at": "2024-01-01T00:00:00",
    }


def test_import():
    import app.services.layout_share  # noqa: F401
    import app.api.layout_share  # noqa: F401
    from app.services.layout_share import (  # noqa: F401
        LayoutShareService,
        layout_to_share,
        extract_shares,
        export_layouts,
    )
    from app.api.layout_share import router  # noqa: F401


def test_layout_to_share_strips_runtime_fields():
    share = svc.layout_to_share(_sample_layout())
    assert share["name"] == "Lobby View"
    assert share["json_layout"] == {"grid": [1]}
    assert "id" not in share
    assert "created_at" not in share
    assert svc.validate_share(share) is True


def test_validate_rejects_bad_shares():
    assert svc.validate_share({}) is False
    assert svc.validate_share({"json_layout": {"grid": [1]}}) is False
    assert svc.validate_share(None) is False
    assert svc.validate_share({"name": "  "}) is False
    assert svc.validate_share({"name": "x"}) is True
    assert svc.validate_share({"name": "x", "json_layout": None}) is True


def test_alias_keys_normalize():
    # title / layout aliases resolve to name / json_layout.
    share = svc.layout_to_share({"title": "Alias Name", "layout": {"grid": [2]}})
    assert share["name"] == "Alias Name"
    assert share["json_layout"] == {"grid": [2]}
    payload = svc.share_to_layout_payload({"layout_name": "N", "json": {"a": 1}})
    assert payload["name"] == "N"
    assert payload["json_layout"] == {"a": 1}
    # camera positions fold into json_layout.
    folded = svc.share_to_layout_payload(
        {"name": "FP", "camera_positions": [{"camera_id": 1, "x": 0.5, "y": 0.5}]}
    )
    assert folded["name"] == "FP"
    assert isinstance(folded["json_layout"], dict)
    assert "camera_positions" in folded["json_layout"]


def test_export_json_roundtrip():
    layouts = [_sample_layout("Cam A", {"grid": [1]}), _sample_layout("Cam B", {"grid": [2]})]
    text = svc.export_layouts_json(layouts)
    data = json.loads(text)
    assert data["kind"] == svc.LAYOUT_SHARE_KIND
    assert data["count"] == 2
    assert len(data["layouts"]) == 2
    back = svc.extract_shares(text)
    assert len(back) == 2
    assert back[0]["name"] == "Cam A"
    # Bare list and envelope both parse.
    assert svc.extract_shares(data) == svc.extract_shares(data["layouts"])
    assert svc.extract_shares({"items": data["layouts"]})[0]["name"] == "Cam A"


def test_import_layouts_data_skips_invalid():
    payload = {
        "version": "1.0",
        "kind": "layouts",
        "layouts": [
            _sample_layout("Good", {"grid": [1]}),
            {"json_layout": {"grid": [1]}},  # missing name
        ],
    }
    out = svc.import_layouts_data(payload)
    assert len(out) == 1
    assert out[0]["name"] == "Good"
    assert "id" not in out[0]
    out_all = svc.import_layouts_data(payload, skip_invalid=False)
    assert len(out_all) == 2


def test_save_and_load_file(tmp_path):
    layouts = [_sample_layout("File Layout", {"cells": []})]
    path = tmp_path / "layouts.json"
    saved = svc.save_layouts(path, layouts)
    assert str(path) in saved
    loaded = svc.load_layouts_file(path)
    assert len(loaded) == 1
    assert loaded[0]["name"] == "File Layout"
    assert svc.load_layouts_file(tmp_path / "missing.json") == []


def test_router_export_import_with_isolated_app():
    app = FastAPI()
    app.include_router(layout_share_api.router)
    client = TestClient(app)
    # Export against a fresh DB may be empty but must succeed.
    r = client.get("/api/layout-share/export")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "layouts" in body
    assert isinstance(body["layouts"], list)
    # Alias paths serve the same export.
    for path in ("/api/layout_share", "/api/layouts-share/export"):
        p = path if path.endswith("export") else path + "/export" if path == "/api/layouts-share/export" else path
        # Normalize: bare base should also export.
        rr = client.get(p if p.endswith("export") else p)
        assert rr.status_code == 200, (path, rr.text)
    # Validate endpoint works without DB.
    v = client.post(
        "/api/layout-share/validate",
        json={"layouts": [_sample_layout("V", {"grid": [1]}), {"json_layout": {}}]},
    )
    assert v.status_code == 200, v.text
    assert v.json()["count"] == 2
    assert v.json()["valid"] == 1
    # Import with no layouts is a 400.
    bad = client.post("/api/layout-share/import", json={"layouts": []})
    assert bad.status_code in (400, 422), bad.text


def test_router_import_export_roundtrip_real_app():
    from app.main import app as real_app

    client = TestClient(real_app)
    created_ids: list[int] = []
    try:
        # Pre-clean any leftovers from interrupted runs.
        try:
            pre = client.get("/api/layouts")
            if pre.status_code == 200:
                pre_items = pre.json()
                if isinstance(pre_items, dict):
                    pre_items = pre_items.get("layouts", pre_items.get("items", []))
                for lay in pre_items or []:
                    if lay.get("name") in ("LayoutShareTestLayout", "LayoutShareTestLayoutRestored"):
                        try:
                            client.delete(f"/api/layouts/{lay['id']}")
                        except Exception:  # noqa: BLE001
                            pass
        except Exception:  # noqa: BLE001
            pass
        # Create a layout to export.
        c = client.post(
            "/api/layouts",
            json={"name": "LayoutShareTestLayout", "json_layout": {"grid": [1]}},
        )
        assert c.status_code in (200, 201), c.text
        created_ids.append(c.json()["id"])

        exp = client.get("/api/layout-share/export")
        assert exp.status_code == 200, exp.text
        layouts = exp.json()["layouts"]
        assert any(p.get("name") == "LayoutShareTestLayout" for p in layouts)

        mine = [p for p in layouts if p.get("name") == "LayoutShareTestLayout"]
        assert mine, "exported layout missing"
        payload_share = dict(mine[0])
        payload_share["name"] = "LayoutShareTestLayoutRestored"
        payload_share.pop("id", None)
        imp = client.post("/api/layout-share/import", json={"layouts": [payload_share]})
        assert imp.status_code == 200, imp.text
        assert imp.json()["count"] == 1
        restored = imp.json()["layouts"][0]
        assert restored["name"] == "LayoutShareTestLayoutRestored"

        # Single-layout export by id.
        lst = client.get("/api/layouts")
        assert lst.status_code == 200
        target_id = None
        for item in lst.json():
            data = item if isinstance(item, dict) else {}
            if data.get("name") == "LayoutShareTestLayout":
                target_id = data.get("id")
                break
        assert target_id is not None
        one = client.get(f"/api/layout-share/export/{target_id}")
        assert one.status_code == 200, one.text
        assert one.json()["name"] == "LayoutShareTestLayout"

        # Track restored layout for cleanup.
        lst2 = client.get("/api/layouts")
        assert lst2.status_code == 200
        items = lst2.json()
        if isinstance(items, dict):
            items = items.get("layouts", items.get("items", []))
        for lay in items:
            if lay.get("name") in ("LayoutShareTestLayout", "LayoutShareTestLayoutRestored"):
                created_ids.append(lay["id"])
    finally:
        seen = set()
        for cid in created_ids:
            if cid in seen:
                continue
            seen.add(cid)
            try:
                client.delete(f"/api/layouts/{cid}")
            except Exception:  # noqa: BLE001
                pass


def test_paths_present_in_main_app():
    from app.main import app as main_app

    paths = list(main_app.openapi()["paths"].keys())
    assert "/api/layout-share/export" in paths
    assert "/api/layout-share/import" in paths
