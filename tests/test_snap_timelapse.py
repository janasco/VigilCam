import subprocess
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.services import snap_timelapse as st
from app.api import snap_timelapse as snap_api


def _fake_ffmpeg_run(cmd, *args, **kwargs):
    assert "ffmpeg" in str(cmd[0])
    dest = str(cmd[-1])
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    Path(dest).write_bytes(b"fake-snap-timelapse")
    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


def _make_snaps(tmp_path: Path, n: int = 3) -> Path:
    d = tmp_path / "snaps"
    d.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (d / f"snapshot_{i:03d}.jpg").write_bytes(b"\xff\xd8\xff\xe0fake" + bytes([i]))
    return d


def test_imports():
    assert st.build_snap_timelapse is not None
    assert st.build_snapshot_timelapse is not None
    assert st.find_snapshots is not None
    assert st.SnapTimelapseService is not None
    assert st.snap_timelapse_service is not None
    assert snap_api.router is not None


def test_parse_helpers():
    assert st.parse_fps(30) == 30.0
    assert st.parse_fps("24") == 24.0
    assert st.parse_fps(0) is None
    assert st.parse_fps(1000) is None
    assert st.parse_factor(4) == 4.0
    assert st.parse_factor("2x") == 2.0
    assert st.parse_factor(0) is None
    assert st.coerce_factor(None) == 1.0
    assert st.coerce_fps(None) == 30.0
    assert st.parse_snap_date("2024-01-15").isoformat() == "2024-01-15"
    assert st.parse_snap_date(None) is None


def test_validate_ok_dir(tmp_path):
    d = _make_snaps(tmp_path)
    info = st.validate_snap_timelapse_params(str(d), fps=24)
    assert info["fps"] == 24.0
    assert info["factor"] == 1.0


def test_validate_bad_fps(tmp_path):
    d = _make_snaps(tmp_path)
    try:
        st.validate_snap_timelapse_params(str(d), fps=0)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for fps=0")


def test_validate_missing_source():
    try:
        st.validate_snap_timelapse_params(None, camera=None, snap_date=None, event_id=None)
        # bare scan falls back to snapshot dir, so force empty explicit list instead
        st.validate_snap_timelapse_params([])
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for empty snapshots")


def test_build_command_snapshots(tmp_path):
    d = _make_snaps(tmp_path)
    out = tmp_path / "out.mp4"
    cmd = st.build_ffmpeg_command(str(d), str(out), fps=24)
    assert "-i" in cmd
    assert str(out) in cmd
    joined = " ".join(cmd)
    assert "libx264" in joined
    assert "concat" in joined


def test_build_mocked_dir(tmp_path):
    d = _make_snaps(tmp_path)
    out = tmp_path / "snap_tl.mp4"
    with patch("app.services.snap_timelapse.subprocess.run", _fake_ffmpeg_run):
        result = st.build_snap_timelapse(str(d), str(out), fps=24)
    assert Path(str(result)).exists()
    assert Path(str(result)).read_bytes() == b"fake-snap-timelapse"


def test_build_missing_source(tmp_path):
    try:
        st.build_snap_timelapse(str(tmp_path / "nope"), str(tmp_path / "o.mp4"))
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("expected FileNotFoundError")


def test_build_empty_dir_no_snapshots(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    try:
        st.build_snap_timelapse(str(d), str(tmp_path / "o.mp4"))
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("expected FileNotFoundError for empty dir")


def test_find_snapshots(tmp_path):
    d = _make_snaps(tmp_path, n=4)
    (d / "notes.txt").write_text("ignore me")
    found = st.find_snapshots(str(d))
    assert len(found) == 4
    assert all(p.suffix.lower() == ".jpg" for p in found)


def test_jobs_lifecycle(tmp_path):
    d = _make_snaps(tmp_path)
    job = st.create_job(str(d), fps=24)
    assert job["status"] == "queued"
    assert st.get_job(job["job_id"]) is not None
    assert any(j["job_id"] == job["job_id"] for j in st.list_jobs())
    st.delete_job(job["job_id"])
    assert st.get_job(job["job_id"]) is None


def test_router_status():
    from app.main import app

    client = TestClient(app)
    for path in ("/api/snap-timelapse/status", "/api/snap-timelapse/health"):
        r = client.get(path)
        assert r.status_code == 200, (path, r.text)
        assert r.json().get("status") == "ok"


def test_router_build_mocked(tmp_path):
    from app.main import app

    client = TestClient(app)
    d = _make_snaps(tmp_path)
    out = tmp_path / "router_snap_tl.mp4"
    with patch("app.services.snap_timelapse.subprocess.run", _fake_ffmpeg_run):
        r = client.post(
            "/api/snap-timelapse",
            json={"snapshots": str(d), "output": str(out), "fps": 24},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("output_path") == str(out)
    assert Path(str(out)).exists()


def test_router_build_missing_source():
    from app.main import app

    client = TestClient(app)
    r = client.post(
        "/api/snap-timelapse",
        json={"snapshots": "/nonexistent/snaps", "fps": 24},
    )
    assert r.status_code in (400, 404)


def test_router_build_bad_fps(tmp_path):
    from app.main import app

    client = TestClient(app)
    d = _make_snaps(tmp_path)
    r = client.post(
        "/api/snap-timelapse",
        json={"snapshots": str(d), "fps": 0},
    )
    assert r.status_code == 400
