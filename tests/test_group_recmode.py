import uuid

from fastapi.testclient import TestClient


def test_group_recmode_imports():
    import app.services.group_recmode  # noqa: F401
    import app.api.group_recmode  # noqa: F401
    from app.services.group_recmode import (  # noqa: F401
        VALID_RECORDING_MODES,
        bulk_set_group_recording_mode,
        normalize_recording_mode,
    )
    from app.api.group_recmode import router  # noqa: F401
    assert router is not None
    assert "motion" in VALID_RECORDING_MODES
    assert "always" in VALID_RECORDING_MODES
    assert "never" in VALID_RECORDING_MODES


def test_group_recmode_paths_present():
    from app.main import app

    paths = list(app.openapi()["paths"].keys())
    assert "/api/groups/bulk-recording-mode" in paths
    assert "/api/groups/bulk_recording_mode" in paths


def _unique(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def test_group_recmode_flow():
    from app.main import app

    client = TestClient(app)
    gname = _unique("grm")
    r = client.post("/api/groups", json={"name": gname})
    assert r.status_code == 201, r.text
    gid = r.json()["id"]
    cids = []
    try:
        for i in range(2):
            cr = client.post(
                "/api/cameras",
                json={"name": _unique(f"grmcam{i}"), "kind": "usb", "source": "0"},
            )
            assert cr.status_code == 201, cr.text
            cids.append(cr.json()["id"])
        ar = client.post("/api/groups/bulk-assign", json={"camera_ids": cids, "group_id": gid})
        assert ar.status_code == 200, ar.text

        # set recording-mode to always via canonical path
        set_r = client.post(
            "/api/groups/bulk-recording-mode",
            json={"group_ids": [gid], "recording_mode": "always"},
        )
        assert set_r.status_code == 200, set_r.text
        body = set_r.json()
        assert body.get("ok") is True
        assert body.get("recording_mode") == "always"
        assert body.get("updated_count") == 2
        assert sorted(body.get("updated_camera_ids", [])) == sorted(cids)

        for cid in cids:
            got = client.get(f"/api/cameras/{cid}")
            assert got.status_code == 200, got.text
            assert got.json().get("recording_mode") == "always"

        # alias key (mode) + underscore path
        set2 = client.post(
            "/api/groups/bulk_recording_mode",
            json={"group_ids": [gid], "mode": "motion"},
        )
        assert set2.status_code == 200, set2.text
        assert set2.json().get("recording_mode") == "motion"

        for cid in cids:
            got = client.get(f"/api/cameras/{cid}")
            assert got.status_code == 200, got.text
            assert got.json().get("recording_mode") == "motion"

        # invalid mode -> 400
        bad = client.post(
            "/api/groups/bulk-recording-mode",
            json={"group_ids": [gid], "recording_mode": "not-a-mode"},
        )
        assert bad.status_code == 400, bad.text

        # missing mode -> 400
        missing = client.post(
            "/api/groups/bulk-recording-mode", json={"group_ids": [gid]}
        )
        assert missing.status_code == 400, missing.text
    finally:
        for cid in cids:
            try:
                client.delete(f"/api/cameras/{cid}")
            except Exception:  # noqa: BLE001
                pass
        try:
            client.delete(f"/api/groups/{gid}")
        except Exception:  # noqa: BLE001
            pass


def test_group_recmode_service_helpers():
    from app.services import group_recmode as svc

    assert svc.normalize_recording_mode("Always") == "always"
    assert svc.normalize_recording_mode("MOTION") == "motion"
    assert svc.normalize_recording_mode("off") == "never"
    try:
        svc.normalize_recording_mode("bogus-mode")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    try:
        svc.normalize_recording_mode(None)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    assert svc.bulk_set is svc.bulk_set_group_recording_mode
    assert svc.set_group_recording_mode is svc.bulk_set_group_recording_mode
