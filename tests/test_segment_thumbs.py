import time

from fastapi.testclient import TestClient

from app.main import app
from app.services import segment_thumbs as st


def test_segment_thumbs_imports():
    import app.services.segment_thumbs  # noqa: F401
    import app.api.segment_thumbs  # noqa: F401
    from app.services.segment_thumbs import (  # noqa: F401
        get_segment_thumbnail,
        get_segment_thumbnail_with_meta,
        set_segment_thumbnail,
        list_segments,
        resolve_segment,
        invalidate,
        clear,
    )
    from app.api.segment_thumbs import router  # noqa: F401
    assert router is not None


def test_live_thumbnails_have_no_segment_support():
    # Live-frame cache is per-camera only: no segment/recording args.
    import inspect

    import app.services.thumbnails as live

    src = inspect.getsource(live.get_thumbnail)
    assert "segment" not in src
    sig = inspect.signature(live.get_thumbnail)
    assert "segment" not in sig.parameters
    assert "recording_id" not in sig.parameters
    # Segment service is keyed by segment, unlike live cache.
    seg_sig = inspect.signature(st.get_segment_thumbnail)
    assert "segment" in seg_sig.parameters


def test_get_segment_thumbnail_returns_jpeg(tmp_path):
    st.clear()
    seg = tmp_path / "20240101_120000_main.mp4"
    seg.write_bytes(b"\x00" * 64)
    data = st.get_segment_thumbnail(str(seg))
    assert isinstance(data, (bytes, bytearray))
    assert bytes(data)[:2] == b"\xff\xd8"


def test_segment_cache_hit_within_ttl(tmp_path):
    st.clear()
    seg = tmp_path / "20240101_121000_main.mp4"
    seg.write_bytes(b"\x00" * 64)
    first, meta1 = st.get_segment_thumbnail_with_meta(str(seg), ttl=60)
    second, meta2 = st.get_segment_thumbnail_with_meta(str(seg), ttl=60)
    assert bytes(first) == bytes(second)
    assert meta1["updated_at"] == meta2["updated_at"]
    assert meta2["cache"] == "HIT"


def test_segments_are_keyed_independently(tmp_path):
    st.clear()
    a = tmp_path / "20240101_122000_main.mp4"
    b = tmp_path / "20240101_123000_main.mp4"
    a.write_bytes(b"\x00" * 16)
    b.write_bytes(b"\x00" * 16)
    st.get_segment_thumbnail(str(a), ttl=60)
    st.get_segment_thumbnail(str(b), ttl=60)
    items = st.list_thumbnails()
    keys = {i["segment"] for i in items}
    assert str(a) in keys
    assert str(b) in keys
    stats = st.get_stats()
    assert stats["entries"] >= 2


def test_invalidate_and_clear(tmp_path):
    st.clear()
    seg = tmp_path / "20240101_124000_main.mp4"
    seg.write_bytes(b"\x00" * 16)
    st.get_segment_thumbnail(str(seg), ttl=60)
    assert st.invalidate(str(seg)) is True
    assert st.get_info(str(seg)) is None
    st.get_segment_thumbnail(str(seg), ttl=60)
    st.clear()
    assert st.get_stats()["entries"] == 0


def test_list_segments_scans_recording_dir(tmp_path):
    st.clear()
    cam_dir = tmp_path / "cam1"
    cam_dir.mkdir()
    (cam_dir / "20240101_120000_main.mp4").write_bytes(b"\x00" * 16)
    (cam_dir / "20240101_120500_main.mp4").write_bytes(b"\x00" * 16)
    found = st.list_segments(camera="cam1", recording_dir=str(tmp_path))
    assert len(found) >= 2
    assert all(f["path"].endswith(".mp4") for f in found)


def test_segment_api_endpoints(tmp_path):
    st.clear()
    seg = tmp_path / "20240101_130000_main.mp4"
    seg.write_bytes(b"\x00" * 64)
    client = TestClient(app)

    r = client.get("/api/segment-thumbs/image", params={"segment": str(seg)})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("image/")
    assert r.content[:2] == b"\xff\xd8"
    assert r.headers.get("X-Cache") == "MISS"

    r2 = client.get("/api/segment-thumbs/image", params={"segment": str(seg)})
    assert r2.status_code == 200
    assert r2.headers.get("X-Cache") == "HIT"
    assert r2.content == r.content

    info = client.get("/api/segment-thumbs/info", params={"segment": str(seg)})
    assert info.status_code == 200
    body = info.json()
    assert body.get("cached") is True
    assert body.get("size", 0) > 0

    listing = client.get("/api/segment-thumbs")
    assert listing.status_code == 200
    assert listing.json().get("count", 0) >= 1

    segs = client.get("/api/segment-thumbs/segments",
                      params={"recording_dir": str(tmp_path)})
    assert segs.status_code == 200
    assert segs.json().get("count", 0) >= 1

    status = client.get("/api/segment-thumbs/status")
    assert status.status_code == 200
    assert status.json().get("status") == "ok"


def test_segment_api_dims_offset_and_recording_alias(tmp_path):
    st.clear()
    seg = tmp_path / "20240101_131000_main.mp4"
    seg.write_bytes(b"\x00" * 64)
    client = TestClient(app)
    r = client.get("/api/segment-thumbs/image",
                   params={"segment": str(seg), "width": 160,
                           "height": 120, "offset": 2.5, "ttl": 60})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/")
    time.sleep(0.01)
    r_refresh = client.post("/api/segment-thumbs/refresh",
                            json={"segment": str(seg), "width": 160, "height": 120})
    assert r_refresh.status_code == 200
    assert r_refresh.json().get("refreshed") is True
    # Underscore alias serves the same image.
    r_alias = client.get("/api/segment_thumbs/image", params={"segment": str(seg)})
    assert r_alias.status_code == 200
    assert r_alias.content[:2] == b"\xff\xd8"
    # Recording-id pseudo-key returns a placeholder JPEG without DB.
    r_rec = client.get("/api/recordings/999999/thumb")
    assert r_rec.status_code == 200
    assert r_rec.headers["content-type"].startswith("image/")
