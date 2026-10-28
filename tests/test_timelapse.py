import subprocess
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.services import timelapse as tl
from app.api import timelapse as timelapse_api


def _fake_ffmpeg_run(cmd, *args, **kwargs):
    assert "ffmpeg" in str(cmd[0])
    dest = str(cmd[-1])
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    Path(dest).write_bytes(b"fake-timelapse")
    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


def _make_images(tmp_path: Path, n: int = 3) -> Path:
    d = tmp_path / "frames"
    d.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (d / f"frame_{i:03d}.jpg").write_bytes(b"\xff\xd8\xff\xe0fake" + bytes([i]))
    return d


def test_imports():
    assert tl.build_timelapse is not None
    assert tl.TimelapseService is not None
    assert timelapse_api.router is not None


def test_parse_helpers():
    assert tl.parse_fps(30) == 30.0
    assert tl.parse_fps("24") == 24.0
    assert tl.parse_fps(0) is None
    assert tl.parse_fps(1000) is None
    assert tl.parse_factor(4) == 4.0
    assert tl.parse_factor("2x") == 2.0
    assert tl.parse_factor(0) is None
    assert tl.parse_interval(2) == 2.0
    assert tl.parse_interval(0) is None
    assert tl.coerce_factor(None) == 1.0
    assert tl.coerce_fps(None) == 30.0


def test_validate_ok_dir(tmp_path):
    d = _make_images(tmp_path)
    info = tl.validate_timelapse_params(str(d), fps=24, factor=2)
    assert info["fps"] == 24.0
    assert info["factor"] == 2.0


def test_validate_bad_fps(tmp_path):
    d = _make_images(tmp_path)
    try:
        tl.validate_timelapse_params(str(d), fps=0)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for fps=0")


def test_validate_bad_factor(tmp_path):
    d = _make_images(tmp_path)
    try:
        tl.validate_timelapse_params(str(d), factor=0)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for factor=0")


def test_validate_missing_source():
    try:
        tl.validate_timelapse_params(None)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for missing source")


def test_build_command_images(tmp_path):
    d = _make_images(tmp_path)
    out = tmp_path / "out.mp4"
    cmd = tl.build_ffmpeg_command(str(d), str(out), fps=24, factor=2.0)
    assert "-i" in cmd
    assert str(out) in cmd
    joined = " ".join(cmd)
    assert "libx264" in joined


def test_build_command_video_factor(tmp_path):
    src = tmp_path / "in.mp4"
    src.write_bytes(b"0123456789" * 100)
    out = tmp_path / "out.mp4"
    cmd = tl.build_ffmpeg_command(str(src), str(out), fps=30, factor=8.0)
    assert "setpts=PTS/8.0" in " ".join(cmd)


def test_build_mocked_dir(tmp_path):
    d = _make_images(tmp_path)
    out = tmp_path / "tl.mp4"
    with patch("app.services.timelapse.subprocess.run", _fake_ffmpeg_run):
        result = tl.build_timelapse(str(d), str(out), fps=24, factor=2.0)
    assert Path(str(result)).exists()
    assert Path(str(result)).read_bytes() == b"fake-timelapse"


def test_build_missing_source(tmp_path):
    try:
        tl.build_timelapse(str(tmp_path / "nope"), str(tmp_path / "o.mp4"))
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("expected FileNotFoundError")


def test_find_images(tmp_path):
    d = _make_images(tmp_path, n=4)
    found = tl.find_images(str(d))
    assert len(found) == 4


def test_jobs_lifecycle(tmp_path):
    d = _make_images(tmp_path)
    job = tl.create_job(str(d), fps=24, factor=2.0)
    assert job["status"] == "queued"
    assert tl.get_job(job["job_id"]) is not None
    assert any(j["job_id"] == job["job_id"] for j in tl.list_jobs())
    tl.delete_job(job["job_id"])
    assert tl.get_job(job["job_id"]) is None


def test_router_status():
    from app.main import app

    client = TestClient(app)
    for path in ("/api/timelapse/status", "/api/timelapse/health"):
        r = client.get(path)
        assert r.status_code == 200, (path, r.text)
        assert r.json().get("status") == "ok"


def test_router_build_mocked(tmp_path):
    from app.main import app

    client = TestClient(app)
    d = _make_images(tmp_path)
    out = tmp_path / "router_tl.mp4"
    with patch("app.services.timelapse.subprocess.run", _fake_ffmpeg_run):
        r = client.post(
            "/api/timelapse",
            json={"source": str(d), "output": str(out), "fps": 24, "factor": 2},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("output_path") == str(out)
    assert Path(str(out)).exists()


def test_router_build_missing_source():
    from app.main import app

    client = TestClient(app)
    r = client.post(
        "/api/timelapse",
        json={"source": "/nonexistent/frames", "fps": 24},
    )
    assert r.status_code in (400, 404)


def test_router_build_bad_factor(tmp_path):
    from app.main import app

    client = TestClient(app)
    d = _make_images(tmp_path)
    r = client.post(
        "/api/timelapse",
        json={"source": str(d), "factor": 0},
    )
    assert r.status_code == 400
