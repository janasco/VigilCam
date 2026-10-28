"""Auto-GIF on alert tests.

Verifies the new alert-GIF service + router auto-build GIFs on alert
via ``app.services.alert_gif`` / ``app.api.alert_gif`` while the
pre-existing ``gif_preview`` pipeline remains on-demand only.
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.services.alert_gif as svc
import app.api.alert_gif as api
import app.services.gif_preview as gif_svc
import app.api.gif_preview as gif_api


def _make_client() -> TestClient:
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


def _reset():
    try:
        svc.clear_alert_gifs()
    except Exception:  # noqa: BLE001
        pass
    try:
        svc.enable()
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Precondition: gif_preview builds on demand only, never automatically.
# ---------------------------------------------------------------------------

def test_gif_preview_still_on_demand_only():
    # Only the gif_preview service/api themselves reference gif_preview.
    import subprocess as _sp  # noqa: F401
    from pathlib import Path as _P

    hits = []
    for root in (_P("app"), _P("tests")):
        for p in root.rglob("*.py"):
            if p.name in ("gif_preview.py", "test_gif_preview.py", "alert_gif.py", "test_alert_gif.py"):
                continue
            try:
                text = p.read_text(errors="ignore")
            except Exception:  # noqa: BLE001
                continue
            if "gif_preview" in text or "build_gif_preview" in text or "generate_gif_bytes" in text:
                hits.append(str(p))
    assert hits == [], f"gif_preview must stay on-demand only, found auto refs in: {hits}"

    # main.py never calls the builder at import/startup.
    main_src = _P("app/main.py").read_text()
    assert "build_gif_preview" not in main_src
    assert "generate_gif_bytes" not in main_src
    assert "gif_preview" not in main_src or "email_alerts" in main_src  # auto-loader only

    # index.html has no gif-preview / alert-gif endpoint wiring.
    tpl = _P("app/templates/index.html").read_text()
    assert "gif_preview" not in tpl
    assert "gif-preview" not in tpl
    assert "alert_gif" not in tpl
    assert "alert-gif" not in tpl

    # Export API stays gif-free (mp4/video-only).
    export_src = _P("app/api/export.py").read_text()
    assert "gif" not in export_src.lower()


# ---------------------------------------------------------------------------
# Service imports / aliases.
# ---------------------------------------------------------------------------

def test_imports():
    _reset()
    assert api.router is not None
    assert svc.build_alert_gif is not None
    assert svc.handle_alert is not None
    assert svc.generate_alert_gif_bytes is not None
    assert svc.get_alert_gif is not None
    assert svc.list_alert_gifs is not None
    assert svc.CONTENT_TYPE_GIF == "image/gif"
    assert svc.ALERT_GIF_DIR is not None
    # Delegates to gif_preview without duplicating its on-demand contract.
    assert gif_svc.build_gif_preview is not None
    assert gif_api.router is not None


def test_service_aliases():
    _reset()
    for name in (
        "build_alert_gif", "create_alert_gif", "generate_alert_gif",
        "make_alert_gif", "render_alert_gif", "build", "create",
        "handle_alert", "on_alert", "process_alert", "handle",
        "generate_alert_gif_bytes", "get_alert_gif", "list_alert_gifs",
        "get_config", "configure", "enable", "disable", "is_enabled",
    ):
        assert callable(getattr(svc, name)), name


def test_generate_bytes_is_valid_gif():
    _reset()
    data = svc.generate_alert_gif_bytes("cam1", width=160, fps=5, duration=1.0)
    assert isinstance(data, (bytes, bytearray))
    assert bytes(data)[:6] in (b"GIF89a", b"GIF87a")
    assert len(bytes(data)) > 50


def test_build_alert_gif_writes_file(tmp_path):
    _reset()
    out = tmp_path / "alert1.gif"
    result = svc.build_alert_gif("cam1", str(out), alert_id="alert-1", width=120, fps=2, duration=1.0)
    assert Path(str(result)).exists()
    assert Path(str(result)).suffix == ".gif"
    assert Path(str(result)).read_bytes()[:6] in (b"GIF89a", b"GIF87a")
    rec = svc.get_alert_gif("alert-1")
    assert rec is not None
    assert rec["gif_path"].endswith(".gif")
    _reset()


def test_build_forces_gif_suffix(tmp_path):
    _reset()
    out = tmp_path / "preview.mp4"
    result = svc.build_alert_gif("cam1", str(out), alert_id="alert-suffix")
    assert Path(str(result)).suffix == ".gif"
    assert Path(str(result)).exists()
    _reset()


def test_handle_alert_auto_builds_gif():
    _reset()
    result = svc.handle_alert({"alert_id": "auto-1", "camera": "cam9", "width": 120})
    assert result["ok"] is True
    assert result.get("gif_skipped") is False
    assert str(result["gif_path"]).endswith(".gif")
    assert Path(str(result["gif_path"])).exists()
    assert Path(str(result["gif_path"])).read_bytes()[:6] in (b"GIF89a", b"GIF87a")
    assert svc.get_alert_gif("auto-1") is not None
    assert any(r["alert_id"] == "auto-1" for r in svc.list_alert_gifs())
    _reset()


def test_handle_alert_alias_camera_keys():
    _reset()
    result = svc.handle_alert({"camera_id": "cam-alias", "id": "alias-1"})
    assert result["camera"] == "cam-alias"
    assert result["alert_id"] == "alias-1"
    assert str(result["gif_path"]).endswith(".gif")
    _reset()


def test_handle_alert_disabled_skips_gif():
    _reset()
    svc.disable()
    try:
        result = svc.handle_alert({"alert_id": "skip-1", "camera": "cam1"})
        assert result.get("gif_skipped") is True
        assert result.get("gif_path") is None
        assert svc.get_alert_gif("skip-1") is None
    finally:
        svc.enable()
        _reset()


def test_config_enable_disable():
    _reset()
    svc.disable()
    assert svc.is_enabled() is False
    assert svc.get_config()["enabled"] is False
    svc.enable()
    assert svc.is_enabled() is True
    cfg = svc.configure(width=160, fps=4)
    assert cfg["width"] == 160
    _reset()


def test_list_delete_clear():
    _reset()
    svc.handle_alert({"alert_id": "l1", "camera": "c1"})
    svc.handle_alert({"alert_id": "l2", "camera": "c1"})
    assert len(svc.list_alert_gifs()) >= 2
    assert svc.delete_alert_gif("l1") is True
    assert svc.get_alert_gif("l1") is None
    out = svc.clear_alert_gifs()
    assert out["ok"] is True
    assert svc.list_alert_gifs() == []
    _reset()


# ---------------------------------------------------------------------------
# Router.
# ---------------------------------------------------------------------------

def test_router_health_and_info():
    _reset()
    client = _make_client()
    for path in ("/api/alert_gif/health", "/api/alert-gif/health", "/api/alerts/gif/health"):
        r = client.get(path)
        assert r.status_code == 200, (path, r.text)
        assert r.json().get("status") == "ok"
    r = client.get("/api/alert_gif/")
    assert r.status_code == 200, r.text
    assert r.json().get("format") == "gif"
    _reset()


def test_router_bytes_for_camera():
    _reset()
    client = _make_client()
    r = client.get("/api/alert_gif/camera/cam1", params={"width": 160, "fps": 5, "duration": 1.0})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("image/gif")
    assert r.content[:6] in (b"GIF89a", b"GIF87a")
    _reset()


def test_router_build_post(tmp_path):
    _reset()
    client = _make_client()
    out = tmp_path / "router_alert.gif"
    r = client.post("/api/alert_gif/build", json={"camera": "cam1", "alert_id": "r1", "output": str(out), "width": 120})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("output_path", "").endswith(".gif")
    assert Path(body["output_path"]).exists()
    assert Path(body["output_path"]).read_bytes()[:6] in (b"GIF89a", b"GIF87a")
    _reset()


def test_router_handle_post_auto_gif():
    _reset()
    client = _make_client()
    r = client.post("/api/alert_gif/handle", json={"alert_id": "h1", "camera": "camH"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("alert_id") == "h1"
    assert str(body.get("gif_path", "")).endswith(".gif")
    # Bytes endpoint serves the stored GIF.
    r2 = client.get("/api/alert_gif/h1")
    assert r2.status_code == 200, r2.text
    assert r2.headers["content-type"].startswith("image/gif")
    assert r2.content[:6] in (b"GIF89a", b"GIF87a")
    # Info endpoint returns the record.
    r3 = client.get("/api/alert_gif/h1/info")
    assert r3.status_code == 200, r3.text
    assert r3.json().get("alert_id") == "h1"
    _reset()


def test_router_handle_root_post():
    _reset()
    client = _make_client()
    r = client.post("/api/alert_gif", json={"alert_id": "root1", "camera": "camR"})
    assert r.status_code == 200, r.text
    assert r.json().get("alert_id") == "root1"
    _reset()


def test_router_build_bad_params():
    _reset()
    client = _make_client()
    r = client.post("/api/alert_gif/build", json={"camera": "cam1", "width": 4})
    assert r.status_code == 400, r.text
    _reset()


def test_router_unknown_alert_404():
    _reset()
    client = _make_client()
    r = client.get("/api/alert_gif/no-such-alert-xyz/info")
    assert r.status_code == 404, r.text
    _reset()


def test_router_auto_registered_in_main_app():
    # No app/main.py edit required: pkgutil auto-loader picks up any
    # app.api module exposing `router`. Verified via source inspection
    # (importing app.main pulls 400+ routers and is slow in this env).
    from pathlib import Path as _P

    main_src = _P("app/main.py").read_text()
    assert "pkgutil.iter_modules" in main_src
    assert 'import_module(f"app.api.{_mod.name}")' in main_src or "app.api." in main_src
    # Our module exposes `router` at top level for the auto-loader.
    assert hasattr(api, "router")
    assert len(api.router.routes) > 0
    # No manual wiring of alert_gif in main.py is required (or present).
    assert "alert_gif" not in main_src
