import time

from fastapi.testclient import TestClient

from app.main import app
from app.services import thumbnails


def test_thumbnails_imports():
    import app.services.thumbnails  # noqa: F401
    import app.api.thumbnails  # noqa: F401
    from app.services.thumbnails import get_thumbnail, set_thumbnail, invalidate  # noqa: F401
    from app.api.thumbnails import router  # noqa: F401
    assert router is not None


def test_get_thumbnail_returns_jpeg():
    thumbnails.clear()
    data = thumbnails.get_thumbnail("cache-test-1")
    assert isinstance(data, (bytes, bytearray))
    assert bytes(data)[:2] == b"\xff\xd8"


def test_thumbnail_cache_hit_within_ttl():
    thumbnails.clear()
    first, meta1 = thumbnails.get_thumbnail_with_meta("cache-test-2", ttl=60)
    second, meta2 = thumbnails.get_thumbnail_with_meta("cache-test-2", ttl=60)
    assert bytes(first) == bytes(second)
    assert meta1["updated_at"] == meta2["updated_at"]
    assert meta2["cache"] == "HIT"


def test_thumbnail_invalidate_forces_regeneration():
    thumbnails.clear()
    _, meta1 = thumbnails.get_thumbnail_with_meta("cache-test-3", ttl=60)
    assert thumbnails.invalidate("cache-test-3") is True
    assert thumbnails.get_info("cache-test-3") is None
    _, meta2 = thumbnails.get_thumbnail_with_meta("cache-test-3", ttl=60)
    assert meta2["updated_at"] >= meta1["updated_at"]
    assert meta2["cache"] == "MISS"


def test_thumbnail_ttl_zero_always_stale():
    thumbnails.clear()
    assert thumbnails.is_fresh("cache-test-4", ttl=60) is False
    thumbnails.get_thumbnail("cache-test-4", ttl=60)
    assert thumbnails.is_fresh("cache-test-4", ttl=60) is True
    assert thumbnails.is_fresh("cache-test-4", ttl=0) is False


def test_set_thumbnail_custom_bytes():
    thumbnails.clear()
    custom = b"\xff\xd8\xff" + b"\x00" * 32
    thumbnails.set_thumbnail("cache-test-5", custom)
    assert bytes(thumbnails.get_thumbnail("cache-test-5", ttl=60)) == custom
    info = thumbnails.get_info("cache-test-5")
    assert info is not None
    assert info["size"] == len(custom)


def test_thumbnail_list_and_stats():
    thumbnails.clear()
    thumbnails.get_thumbnail("cache-test-6", ttl=60)
    thumbnails.get_thumbnail("cache-test-7", ttl=60)
    items = thumbnails.list_thumbnails()
    keys = {i["camera_id"] for i in items}
    assert "cache-test-6" in keys
    assert "cache-test-7" in keys
    stats = thumbnails.get_stats()
    assert stats["entries"] >= 2


def test_thumbnail_api_endpoints():
    thumbnails.clear()
    client = TestClient(app)

    r = client.get("/api/thumbnails/42")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/")
    assert r.content[:2] == b"\xff\xd8"
    assert r.headers.get("X-Cache") == "MISS"

    r2 = client.get("/api/thumbnails/42")
    assert r2.status_code == 200
    assert r2.headers.get("X-Cache") == "HIT"
    assert r2.content == r.content

    info = client.get("/api/thumbnails/42/info")
    assert info.status_code == 200
    body = info.json()
    assert body.get("cached") is True
    assert body.get("size", 0) > 0

    ref = client.post("/api/thumbnails/42/refresh")
    assert ref.status_code == 200
    assert ref.json().get("refreshed") is True

    listing = client.get("/api/thumbnails")
    assert listing.status_code == 200
    assert listing.json().get("count", 0) >= 1

    deleted = client.delete("/api/thumbnails/42")
    assert deleted.status_code == 200
    assert deleted.json().get("ok") is True

    r3 = client.get("/api/thumbnails/42")
    assert r3.status_code == 200
    assert r3.headers.get("X-Cache") == "MISS"


def test_thumbnail_api_dims_and_ttl_params():
    thumbnails.clear()
    client = TestClient(app)
    r = client.get("/api/thumbnails/77", params={"width": 160, "height": 120, "ttl": 60})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/")
    # Small pause to ensure distinct generation timestamps for forced refresh.
    time.sleep(0.01)
    r_refresh = client.get("/api/thumbnails/77", params={"refresh": "true"})
    assert r_refresh.status_code == 200
    assert r_refresh.headers.get("X-Cache") == "MISS"
