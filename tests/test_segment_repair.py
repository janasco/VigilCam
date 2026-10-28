import json
import subprocess
from pathlib import Path
from unittest.mock import patch

from app.services import segment_repair as sr


def _make_mp4(path: Path) -> None:
    import subprocess as sp

    sp.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=16x16:d=1", "-c:v", "libx264", "-t", "1", str(path)],
        capture_output=True,
    )


def test_import():
    assert sr.segment_repair is not None
    assert sr.SegmentRepairer is not None


def test_validate_good_segment_real(tmp_path):
    p = tmp_path / "good.mp4"
    _make_mp4(p)
    assert sr.validate_segment(p) is True


def test_validate_bad_segment_mock(tmp_path):
    p = tmp_path / "bad.mp4"
    p.write_bytes(b"not a video")

    def fake_run(cmd, *a, **k):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")

    with patch("app.services.segment_repair.subprocess.run", fake_run):
        assert sr.validate_segment(p) is False


def test_mark_bad_renames(tmp_path):
    p = tmp_path / "bad.mp4"
    p.write_bytes(b"x")
    bp = sr.mark_bad(p)
    assert bp == tmp_path / "bad.mp4.bad"
    assert not p.exists()
    assert bp.exists()
    assert sr.is_bad(p) is True


def test_scan_marks_bad(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    good = root / "good.mp4"
    bad = root / "bad.mp4"
    good.write_bytes(b"x")
    bad.write_bytes(b"y")

    def fake_run(cmd, *a, **k):
        if "bad.mp4" in " ".join(cmd):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="e")
        data = {"streams": [{"codec_type": "video", "codec_name": "h264"}], "format": {"duration": "1.0"}}
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(data), stderr="")

    with patch("app.services.segment_repair.subprocess.run", fake_run):
        rep = sr.scan_segments(root)
        assert rep["valid_count"] == 1
        assert rep["bad_count"] == 1
        assert (root / "bad.mp4.bad").exists()


def test_class_alias_and_methods(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    (root / "a.mp4").write_bytes(b"x")
    rep = sr.SegmentRepairer(root)
    assert hasattr(rep, "scan")
    assert callable(rep.mark_bad)
