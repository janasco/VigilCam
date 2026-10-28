import subprocess
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.services import digest_video as dv
from app.api import digest_video as digest_video_api


def _fake_ffmpeg_run(cmd, *args, **kwargs):
    assert "ffmpeg" in str(cmd[0])
    dest = str(cmd[-1])
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    Path(dest).write_bytes(b"fake-digest-video")
    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


def _make_clips(tmp_path: Path, n: int = 3) -> list:
    d = tmp_path / "clips"
    d.mkdir(parents=True, exist_ok=True)
    out = []
    for i in range(n):
        # use timestamp-like names so date filtering can match
        p = d / f"20240115_{120000 + i * 100:06d}.mp4"
        p.write_bytes(b"\x00\x00\x00fake" + bytes([i]))
        out.append(p)
    return out


def test_imports():
    assert dv.build_digest_video is not None
    assert dv.build_daily_digest is not None
    assert dv.DigestVideoService is not None
    assert dv.digest_video_service is not None
    assert digest_video_api.router is not None


def test_parse_date_helpers():
    assert str(dv.parse_date("2024-01-15")) == "2024-01-15"
    assert str(dv.parse_date("20240115")) == "2024-01-15"
    assert dv.parse_date(None) is None
    assert dv.parse_fps(30) == 30.0
    assert dv.parse_fps(0) is None
    assert dv.parse_factor(4) == 4.0
    assert dv.parse_factor("2x") == 2.0
    assert dv.parse_factor(0) is None
    assert dv.coerce_factor(None) == 1.0
    assert dv.coerce_fps(None) == 30.0


def test_validate_ok_clips(tmp_path):
    clips = _make_clips(tmp_path)
    info = dv.validate_digest_params([str(p) for p in clips], camera="cam1", digest_date="2024-01-15")
    assert info["fps"] == 30.0
    assert info["factor"] == 1.0
    assert info["date"] == "2024-01-15"


def test_validate_missing_source():
    try:
        dv.validate_digest_params(None, camera=None)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for missing camera/clips")


def test_validate_bad_fps(tmp_path):
    clips = _make_clips(tmp_path)
    try:
        dv.validate_digest_params([str(p) for p in clips], fps=0)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for fps=0")


def test_build_command_clips(tmp_path):
    clips = _make_clips(tmp_path)
    out = tmp_path / "digest.mp4"
    cmd = dv.build_ffmpeg_command([str(p) for p in clips], str(out), fps=24, factor=1.0)
    assert "-i" in cmd
    assert str(out) in cmd
    joined = " ".join(cmd)
    assert "concat" in joined


def test_build_mocked_clips(tmp_path):
    clips = _make_clips(tmp_path)
    out = tmp_path / "digest.mp4"
    with patch("app.services.digest_video.subprocess.run", _fake_ffmpeg_run):
        result = dv.build_digest_video([str(p) for p in clips], str(out), digest_date="2024-01-15")
    assert Path(str(result)).exists()
    assert Path(str(result)).read_bytes() == b"fake-digest-video"


def test_build_missing_clips(tmp_path):
    try:
        dv.build_digest_video([str(tmp_path / "nope.mp4")], str(tmp_path / "o.mp4"))
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("expected FileNotFoundError")


def test_find_clips(tmp_path):
    clips = _make_clips(tmp_path, n=4)
    found = dv.find_clips(clips=[str(p) for p in clips])
    assert len(found) == 4


def test_jobs_lifecycle(tmp_path):
    clips = _make_clips(tmp_path)
    job = dv.create_job([str(p) for p in clips], digest_date="2024-01-15")
    assert job["status"] == "queued"
    assert dv.get_job(job["job_id"]) is not None
    assert any(j["job_id"] == job["job_id"] for j in dv.list_jobs())
    dv.delete_job(job["job_id"])
    assert dv.get_job(job["job_id"]) is None


def test_router_status():
    from app.main import app

    client = TestClient(app)
    for path in ("/api/digest-video/status", "/api/digest-video/health"):
        r = client.get(path)
        assert r.status_code == 200, (path, r.text)
        assert r.json().get("status") == "ok"


def test_router_build_mocked(tmp_path):
    from app.main import app

    client = TestClient(app)
    clips = _make_clips(tmp_path)
    out = tmp_path / "router_digest.mp4"
    with patch("app.services.digest_video.subprocess.run", _fake_ffmpeg_run):
        r = client.post(
            "/api/digest-video",
            json={"clips": [str(p) for p in clips], "date": "2024-01-15", "output": str(out)},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("output_path") == str(out)
    assert Path(str(out)).exists()


def test_router_build_missing_source():
    from app.main import app

    client = TestClient(app)
    r = client.post("/api/digest-video", json={})
    assert r.status_code == 400
    r2 = client.post(
        "/api/digest-video",
        json={"clips": ["/nonexistent/clip.mp4"]},
    )
    assert r2.status_code in (400, 404)


def test_router_build_bad_factor(tmp_path):
    from app.main import app

    client = TestClient(app)
    clips = _make_clips(tmp_path)
    r = client.post(
        "/api/digest-video",
        json={"clips": [str(p) for p in clips], "factor": 0},
    )
    assert r.status_code == 400
