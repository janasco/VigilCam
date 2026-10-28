"""GIF preview export tests.

Verifies that the regular export pipeline remains mp4/video-only (no GIF
output) and that the new dedicated GIF preview service + router produce
valid ``image/gif`` payloads.
"""

from pathlib import Path
from unittest.mock import patch

import subprocess

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.services.gif_preview as svc
import app.api.gif_preview as api


def _make_client() -> TestClient:
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Export pipeline stays mp4/video-only (no GIF).
# ---------------------------------------------------------------------------

def test_export_defaults_to_mp4():
    from app.services import export as exp

    import inspect

    sig = inspect.signature(exp.ExportService.create_job)
    assert sig.parameters["format"].default == "mp4"
    sig2 = inspect.signature(exp.ExportService.export_range)
    assert sig2.parameters["format"].default == "mp4"


def test_export_api_has_no_gif_output():
    from app.api import export as export_api

    src = Path("app/api/export.py").read_text()
    assert "gif" not in src.lower()
    routes = [(r.path, list(getattr(r, "methods", []))) for r in export_api.router.routes]
    assert routes
    for path, _methods in routes:
        assert "gif" not in path.lower()
    # API defaults to mp4 in source.
    assert "mp4" in src


def test_export_service_video_segments_are_mp4_only():
    from app.services import export as exp

    service = exp.ExportService()
    found = service.find_segments("no-such-camera-xyz", "2024-01-01", "2024-01-02")
    assert found == []
    # concat_segments defaults to mp4 container when no format given.
    src = Path("app/services/export.py").read_text()
    assert 'or "mp4"' in src or "or 'mp4'" in src


# ---------------------------------------------------------------------------
# GIF preview service.
# ---------------------------------------------------------------------------

def test_imports():
    assert api.router is not None
    assert svc.build_gif_preview is not None
    assert svc.generate_gif_bytes is not None
    assert svc.validate_gif_params is not None
    assert svc.build_ffmpeg_command is not None
    assert svc.CONTENT_TYPE_GIF == "image/gif"
    assert svc.GIF_DIR is not None


def test_service_aliases():
    for name in (
        "generate_gif_preview", "create_gif_preview", "make_gif_preview",
        "render_gif_preview", "get_gif_preview", "preview", "get_preview",
        "generate", "build", "create",
    ):
        assert callable(getattr(svc, name)), name
    for name in ("validate", "validate_params", "coerce_fps", "coerce_duration", "parse_fps"):
        assert callable(getattr(svc, name)), name


def test_generate_bytes_is_valid_gif():
    data = svc.generate_gif_bytes("cam1", width=160, fps=5, duration=1.0)
    assert isinstance(data, (bytes, bytearray))
    assert bytes(data)[:6] in (b"GIF89a", b"GIF87a")
    assert len(bytes(data)) > 50


def test_generate_bytes_aliases_match():
    a = svc.generate_gif_bytes("alias-cam", width=120, fps=2, duration=1.0)
    b = svc.render_gif_bytes("alias-cam", width=120, fps=2, duration=1.0)
    assert bytes(a)[:6] in (b"GIF89a", b"GIF87a")
    assert bytes(b)[:6] in (b"GIF89a", b"GIF87a")


def test_validate_ok_and_aliases():
    info = svc.validate_gif_params("cam1", width=200, fps=5, duration=2.0)
    assert info["width"] == 200
    assert info["fps"] == 5.0
    assert info["duration"] == 2.0
    assert info["format"] == "gif"
    assert info["content_type"] == "image/gif"
    # alias kwargs
    info2 = svc.validate_gif_params(camera_id="cam2", w=160, frame_rate=4, seconds=1.0)
    assert info2["width"] == 160
    assert info2["fps"] == 4.0
    assert info2["duration"] == 1.0


def test_validate_bad_width_fps_duration():
    for bad in ({"width": 4}, {"width": 5000}, {"fps": 0}, {"fps": 100}, {"duration": 0.1}, {"duration": 60}):
        try:
            svc.validate_gif_params("cam1", **bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for {bad}")


def test_build_command_contains_gif_filters(tmp_path):
    out = tmp_path / "preview.gif"
    cmd = svc.build_ffmpeg_command("input.mp4", str(out), width=160, fps=5, duration=2.0)
    joined = " ".join(cmd)
    assert "fps=" in joined
    assert "scale=" in joined
    assert "palettegen" in joined
    assert "paletteuse" in joined
    assert str(out) in cmd
    assert cmd[-1].endswith(".gif")


def test_build_gif_preview_writes_file(tmp_path):
    out = tmp_path / "cam1_preview.gif"
    result = svc.build_gif_preview("cam1", str(out), width=120, fps=2, duration=1.0)
    assert Path(str(result)).exists()
    assert Path(str(result)).suffix == ".gif"
    data = Path(str(result)).read_bytes()
    assert data[:6] in (b"GIF89a", b"GIF87a")


def test_build_gif_preview_forces_gif_suffix(tmp_path):
    out = tmp_path / "preview.mp4"
    result = svc.build_gif_preview("cam1", str(out), width=120, fps=2, duration=1.0)
    assert Path(str(result)).suffix == ".gif"
    assert Path(str(result)).exists()


def test_build_with_source_mocked(tmp_path):
    src = tmp_path / "in.mp4"
    src.write_bytes(b"0123456789" * 100)
    out = tmp_path / "out.gif"

    def _fake_run(cmd, *args, **kwargs):
        dest = str(cmd[-1])
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"GIF89afake")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("app.services.gif_preview.subprocess.run", _fake_run):
        result = svc.build_gif_preview("cam1", str(out), source=str(src))
    assert Path(str(result)).exists()


def test_jobs_lifecycle():
    job = svc.create_job("cam9", width=160, fps=5, duration=1.0)
    assert job["status"] == "queued"
    assert job["format"] == "gif"
    assert svc.get_job(job["job_id"]) is not None
    assert any(j["job_id"] == job["job_id"] for j in svc.list_jobs())
    assert svc.delete_job(job["job_id"]) is True
    assert svc.get_job(job["job_id"]) is None


# ---------------------------------------------------------------------------
# GIF preview router.
# ---------------------------------------------------------------------------

def test_router_health_and_info():
    client = _make_client()
    for path in ("/api/gif-preview/health", "/api/gif-preview/status"):
        r = client.get(path)
        assert r.status_code == 200, (path, r.text)
        assert r.json().get("status") == "ok"
    r = client.get("/api/gif-preview/")
    assert r.status_code == 200, r.text
    assert r.json().get("format") == "gif"


def test_router_preview_bytes_gif():
    client = _make_client()
    r = client.get("/api/gif-preview/cam1", params={"width": 160, "fps": 5, "duration": 1.0})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("image/gif")
    assert r.content[:6] in (b"GIF89a", b"GIF87a")


def test_router_preview_bytes_alias_prefix():
    client = _make_client()
    r = client.get("/api/gif_preview/cam1")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("image/gif")
    assert r.content[:6] in (b"GIF89a", b"GIF87a")


def test_router_preview_info_json():
    client = _make_client()
    r = client.get("/api/gif-preview/cam1/info")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("format") == "gif"
    assert body.get("content_type") == "image/gif"
    assert body.get("width", 0) > 0


def test_router_build_post(tmp_path):
    client = _make_client()
    out = tmp_path / "router_preview.gif"
    r = client.post("/api/gif-preview", json={"camera": "cam1", "output": str(out), "width": 120})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("output_path", "").endswith(".gif")
    assert Path(body["output_path"]).exists()
    assert Path(body["output_path"]).read_bytes()[:6] in (b"GIF89a", b"GIF87a")


def test_router_build_bad_params():
    client = _make_client()
    r = client.post("/api/gif-preview", json={"camera": "cam1", "width": 4})
    assert r.status_code == 400, r.text


def test_router_build_for_camera(tmp_path):
    client = _make_client()
    out = tmp_path / "cam_build.gif"
    r = client.post("/api/gif-preview/cam77/build", json={"output": str(out), "fps": 5})
    assert r.status_code == 200, r.text
    assert r.json().get("output_path", "").endswith(".gif")
