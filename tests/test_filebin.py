from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.filebin import router as filebin_router
import app.services.filebin as svc


def _make_client():
    app = FastAPI()
    app.include_router(filebin_router)
    return TestClient(app)


def test_imports():
    import app.api.filebin  # noqa: F401
    import app.services.filebin  # noqa: F401
    assert filebin_router is not None
    assert hasattr(svc, "archive_file")
    assert hasattr(svc, "list_bin")
    assert hasattr(svc, "get_entry")
    assert hasattr(svc, "update_entry")
    assert hasattr(svc, "delete_entry")
    assert hasattr(svc, "clear_bin")
    assert hasattr(svc, "stats")


def test_service_archive_list_get_update_delete():
    svc.clear_bin(force=True)
    entry = svc.archive_file(name="front-door-clip.mp4", camera_id="1", note="motion at door")
    assert entry.get("id")
    assert entry.get("camera_id") == "1"
    assert entry.get("stored_path")

    items = svc.list_bin()
    assert any(str(e.get("id")) == str(entry.get("id")) for e in items)

    fetched = svc.get_entry(entry.get("id"))
    assert fetched is not None

    filtered = svc.list_bin(camera_id="1")
    assert filtered
    assert all(str(e.get("camera_id")) == "1" for e in filtered)

    updated = svc.update_entry(entry.get("id"), name="renamed.mp4")
    assert updated is not None
    assert updated.get("name") == "renamed.mp4"

    assert svc.delete_entry(entry.get("id")) is True
    assert svc.get_entry(entry.get("id")) is None


def test_service_aliases_and_lock():
    svc.clear_bin(force=True)
    a = svc.save_to_bin(name="a.mp4", camera_id="7")
    b = svc.add_file(name="b.mp4", camera_id="8")
    assert a.get("id") and b.get("id")
    assert len(svc.list_files()) >= 2
    assert svc.get(a.get("id")) is not None

    locked = svc.filebin_service.lock(a.get("id"))
    assert locked.get("locked") is True
    # Locked entries refuse plain delete.
    assert svc.delete_entry(a.get("id")) is False
    assert svc.delete_entry(a.get("id"), force=True) is True

    n = svc.clear(force=True)
    assert n >= 1
    assert svc.list_bin() == []


def test_service_copies_real_file(tmp_path):
    svc.clear_bin(force=True)
    src = tmp_path / "rec.mp4"
    src.write_bytes(b"0123456789" * 100)
    entry = svc.archive_file(source_path=str(src), name="saved.mp4", camera_id="2")
    assert entry.get("stored_path")
    from pathlib import Path
    assert Path(str(entry.get("stored_path"))).exists()
    assert entry.get("size_bytes") == len(b"0123456789" * 100)
    assert svc.delete_entry(entry.get("id")) is True
    assert not Path(str(entry.get("stored_path"))).exists()


def test_service_stats_and_search():
    svc.clear_bin(force=True)
    svc.archive_file(name="porch-motion.mp4", camera_id="3", note="person")
    svc.archive_file(name="garage.mp4", camera_id="4", note="car")
    s = svc.stats()
    assert s.get("count") >= 2
    found = svc.list_bin(search="porch")
    assert len(found) == 1
    assert svc.count() >= 2


def test_router_list_create_get_update_delete():
    svc.clear_bin(force=True)
    client = _make_client()

    r = client.post("/api/filebin", json={"name": "clip1.mp4", "camera_id": "3"})
    assert r.status_code in (200, 201), r.text
    created = r.json()
    eid = created.get("id") or created.get("entry_id")
    assert eid

    r_list = client.get("/api/filebin")
    assert r_list.status_code == 200, r_list.text
    assert isinstance(r_list.json(), list)
    assert any(str(e.get("id")) == str(eid) for e in r_list.json())

    r_get = client.get(f"/api/filebin/{eid}")
    assert r_get.status_code == 200, r_get.text

    r_put = client.put(f"/api/filebin/{eid}", json={"name": "renamed2.mp4"})
    assert r_put.status_code == 200, r_put.text
    assert r_put.json().get("name") == "renamed2.mp4"

    r_del = client.delete(f"/api/filebin/{eid}")
    assert r_del.status_code == 200, r_del.text

    r_missing = client.get(f"/api/filebin/{eid}")
    assert r_missing.status_code == 404


def test_router_alias_paths_stats_count():
    svc.clear_bin(force=True)
    svc.archive_file(name="x.mp4", camera_id="10")
    svc.archive_file(name="y.mp4", camera_id="11")
    client = _make_client()

    r = client.get("/api/file-bin", params={"camera_id": "10"})
    assert r.status_code == 200, r.text
    assert all(str(e.get("camera_id")) == "10" for e in r.json())

    r2 = client.get("/api/bin")
    assert r2.status_code == 200, r2.text
    assert isinstance(r2.json(), list)

    r3 = client.get("/api/archive")
    assert r3.status_code == 200, r3.text

    r_stats = client.get("/api/filebin/stats")
    assert r_stats.status_code == 200, r_stats.text
    assert r_stats.json().get("count") >= 2

    r_count = client.get("/api/filebin/count")
    assert r_count.status_code == 200
    assert r_count.json().get("count") >= 2


def test_router_lock_unlock_flow():
    svc.clear_bin(force=True)
    client = _make_client()
    r = client.post("/api/filebin", json={"name": "lockme.mp4"})
    eid = r.json().get("id")
    assert eid

    rl = client.post(f"/api/filebin/{eid}/lock")
    assert rl.status_code == 200, rl.text
    assert rl.json().get("locked") is True

    # Locked delete without force -> 409 or 404-family (must not succeed).
    rd = client.delete(f"/api/filebin/{eid}")
    assert rd.status_code in (404, 409), rd.text

    ru = client.post(f"/api/filebin/{eid}/unlock")
    assert ru.status_code == 200, ru.text

    rd2 = client.delete(f"/api/filebin/{eid}")
    assert rd2.status_code == 200, rd2.text
