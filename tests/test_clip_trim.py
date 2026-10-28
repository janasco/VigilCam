import subprocess
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.services import clip_trim as ct
from app.api import clip_trim as clip_trim_api


def _fake_ffmpeg_copy(cmd, *args, **kwargs):
    # Emulate ffmpeg stream-copy trim by copying source -> dest (last arg).
    assert "ffmpeg" in str(cmd[0])
    src = None
    dest = str(cmd[-1])
    # Find input after -i
    if "-i" in cmd:
        src = cmd[cmd.index("-i") + 1]
    if src is not None and Path(str(src)).exists():
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(Path(str(src)).read_bytes())
    else:
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"fake-clip")
    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


def test_imports():
    assert ct.trim_clip is not None
    assert ct.ClipTrimService is not None
    assert clip_trim_api.router is not None


def test_parse_timestamp():
    assert ct.parse_timestamp(10) == 10.0
    assert ct.parse_timestamp("10") == 10.0
    assert ct.parse_timestamp("01:30") == 90.0
    assert ct.parse_timestamp("01:02:03") == 3723.0
    assert ct.parse_timestamp(None) is None
    assert ct.parse_timestamp("10s") == 10.0


def test_validate_ok():
    info = ct.validate_trim_params("/tmp/a.mp4", start=1, end=5)
    assert info["start"] == 1.0
    assert info["end"] == 5.0
    assert info["duration"] == 4.0


def test_validate_bad_range():
    try:
        ct.validate_trim_params("/tmp/a.mp4", start=5, end=2)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for end <= start")


def test_build_command_contains_times():
    cmd = ct.build_ffmpeg_command("/tmp/in.mp4", "/tmp/out.mp4", start=1.5, end=4.5)
    assert "-ss" in cmd
    assert "-t" in cmd or "-to" in cmd
    assert str("/tmp/out.mp4") in cmd


def test_trim_clip_mocked(tmp_path):
    src = tmp_path / "in.mp4"
    src.write_bytes(b"0123456789" * 100)
    out = tmp_path / "out.mp4"
    with patch("app.services.clip_trim.subprocess.run", _fake_ffmpeg_copy):
        result = ct.trim_clip(str(src), str(out), start=1, end=3)
    assert Path(str(result)).exists()
    assert Path(str(result)).read_bytes() == src.read_bytes()


def test_trim_missing_source(tmp_path):
    try:
        ct.trim_clip(str(tmp_path / "nope.mp4"), str(tmp_path / "o.mp4"), start=0, end=1)
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("expected FileNotFoundError")


def test_router_status():
    from app.main import app

    client = TestClient(app)
    for path in ("/api/clip-trim/status", "/api/clip_trim/status"):
        r = client.get(path)
        assert r.status_code == 200, (path, r.text)
        assert r.json().get("status") == "ok"


def test_router_trim_mocked(tmp_path):
    from app.main import app

    client = TestClient(app)
    src = tmp_path / "rec.mp4"
    src.write_bytes(b"abcdefghij" * 50)
    out = tmp_path / "trimmed.mp4"
    with patch("app.services.clip_trim.subprocess.run", _fake_ffmpeg_copy):
        r = client.post(
            "/api/clip-trim",
            json={"source": str(src), "start": 0, "end": 2, "output": str(out)},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("output_path") == str(out)
    assert Path(str(out)).exists()


def test_router_trim_missing_source():
    from app.main import app

    client = TestClient(app)
    r = client.post(
        "/api/clip-trim",
        json={"source": "/nonexistent/rec.mp4", "start": 0, "end": 1},
    )
    assert r.status_code in (400, 404)


def test_router_trim_bad_range(tmp_path):
    from app.main import app

    client = TestClient(app)
    src = tmp_path / "rec2.mp4"
    src.write_bytes(b"x" * 10)
    r = client.post(
        "/api/clip-trim",
        json={"source": str(src), "start": 5, "end": 2},
    )
    assert r.status_code == 400
