from fastapi.testclient import TestClient

from app.main import app
from app.services import bookmarks


def test_bookmark_badges_import():
    import app.services.bookmark_badges  # noqa: F401
    import app.api.bookmark_badges  # noqa: F401
    from app.api.bookmark_badges import router  # noqa: F401
    from app.services.bookmark_badges import (  # noqa: F401
        badges_for_cameras,
        badges_for_recordings,
        count_for_camera,
        count_for_recording,
        counts_by_camera,
        counts_by_recording,
        get_badge_counts,
        total_count,
    )


def _seed():
    bookmarks.clear()
    bookmarks.create_bookmark(recording_id=1, camera_id=10, offset_seconds=1, label="a")
    bookmarks.create_bookmark(recording_id=1, camera_id=10, offset_seconds=2, label="b")
    bookmarks.create_bookmark(recording_id=2, camera_id=20, offset_seconds=3, label="c")


def test_bookmark_badges_service_counts():
    from app.services import bookmark_badges as badges

    _seed()
    assert badges.total_count() == 3
    assert badges.count_for_recording(1) == 2
    assert badges.count_for_recording(2) == 1
    assert badges.count_for_recording(999) == 0
    assert badges.count_for_camera(10) == 2
    assert badges.count_for_camera(20) == 1
    assert badges.count_for_camera(999) == 0

    by_rec = badges.counts_by_recording()
    assert by_rec.get("1") == 2
    assert by_rec.get("2") == 1
    by_cam = badges.counts_by_camera()
    assert by_cam.get("10") == 2
    assert by_cam.get("20") == 1

    payload = badges.get_badge_counts()
    assert payload["total"] == 3
    assert payload["by_recording"]["1"] == 2
    assert payload["by_camera"]["10"] == 2

    scoped = badges.get_badge_counts(recording_id=1)
    assert scoped["filtered_count"] == 2
    assert scoped["count"] == 2
    assert scoped["total"] == 3

    rec_badges = badges.badges_for_recordings()
    assert {b["recording_id"]: b["count"] for b in rec_badges} == {1: 2, 2: 1}
    cam_badges = badges.badges_for_cameras()
    assert {b["camera_id"]: b["count"] for b in cam_badges} == {10: 2, 20: 1}
    assert badges.badge_for_recording(1)["has_bookmarks"] is True
    assert badges.badge_for_recording(999) == {
        "recording_id": 999,
        "count": 0,
        "has_bookmarks": False,
    }


def test_bookmark_badges_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/bookmarks/counts" in paths
    assert "/api/bookmarks/summary" in paths
    assert "/api/bookmarks/count" in paths


def test_bookmark_badges_endpoints():
    _seed()
    client = TestClient(app)

    r = client.get("/api/bookmarks/counts")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 3
    assert body["by_recording"]["1"] == 2
    assert body["by_camera"]["10"] == 2

    r = client.get("/api/bookmarks/summary")
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 3

    r = client.get("/api/bookmarks/count", params={"recording_id": 1})
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 2

    r = client.get("/api/bookmarks/count", params={"camera_id": 20})
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 1

    r = client.get("/api/bookmarks/badges")
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 3
    assert any(b["count"] == 2 for b in r.json()["recording_badges"])

    r = client.get("/api/recordings/1/bookmarks/count")
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 2
    assert r.json()["has_bookmarks"] is True

    r = client.get("/api/cameras/10/bookmarks/count")
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 2

    r = client.get("/api/recordings/999/bookmarks/count")
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 0
    assert r.json()["has_bookmarks"] is False
