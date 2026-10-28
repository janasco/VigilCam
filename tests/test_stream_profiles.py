from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.stream_profiles import router as stream_profiles_router  # noqa: F401
from app.services import stream_profiles as sp_mod  # noqa: F401
from app.services.stream_profiles import (  # noqa: F401
    build_ffmpeg_inputs,
    get_profile,
    get_stream_profiles,
    ingestion_urls,
    resolve_roles,
    set_profile,
    set_stream_profiles,
    verify_roles,
    verify_single_source_camera_model,
    clear,
)


def test_stream_profiles_import():
    import app.api.stream_profiles  # noqa: F401
    import app.services.stream_profiles  # noqa: F401
    from app.api.stream_profiles import router  # noqa: F401
    from app.services.stream_profiles import set_profile, get_stream_profiles  # noqa: F401


def test_cameras_have_single_source_no_main_sub():
    """VigilCam cameras expose one ``source``; main/sub live in profiles layer."""
    from app.models.entities import Camera

    cols = [c.name for c in Camera.__table__.columns]
    lowered = [c.lower() for c in cols]
    assert "source" in lowered
    for banned in ("main_url", "sub_url", "main_stream", "sub_stream", "mainstream", "substream", "stream_profiles"):
        assert banned not in lowered, f"Camera should not have {banned} column (single source only)"

    report = verify_single_source_camera_model()
    assert report["single_source"] is True
    assert report["source_field"] == "source"
    assert report["has_main_sub_profile_columns"] is False

    from app.schemas import CameraBase

    fields = set(CameraBase.model_fields.keys())
    assert "source" in fields
    assert not ({"main_url", "sub_url", "main_stream", "sub_stream"} & fields)


def test_stream_profiles_service_crud_and_roles():
    sp_mod.clear()
    # Shinobi-style: high-res main for record, low-res sub for detect.
    main = set_profile(camera_id=1, name="main", url="rtsp://cam1/high", roles=["record", "live"])
    assert main["camera_id"] == 1
    assert main["name"] == "main"
    assert main["url"] == "rtsp://cam1/high"
    assert "record" in main["roles"]

    sub = set_profile(1, "sub", "rtsp://cam1/low", roles=["detect"])
    assert sub["name"] == "sub"
    assert sub["roles"] == ["detect"]

    data = get_stream_profiles(1)
    assert set(data["profiles"].keys()) == {"main", "sub"}
    assert data["main"]["url"] == "rtsp://cam1/high"
    assert data["sub"]["url"] == "rtsp://cam1/low"

    roles = resolve_roles(1)
    assert roles["record"] == "main"
    assert roles["detect"] == "sub"

    assert get_profile(1, "main")["url"] == "rtsp://cam1/high"
    assert sp_mod.profile_for_role(1, "detect")["name"] == "sub"
    assert sp_mod.profile_for_role(1, "record")["name"] == "main"

    # Frigate rule: each role used only once.
    check = verify_roles(1)
    assert check["ok"] is True
    assert check["roles"]["detect"] == "sub"

    # Frigate-style ffmpeg inputs split detect vs record.
    inputs = build_ffmpeg_inputs(1)
    by_role = {tuple(i["roles"]): i["path"] for i in inputs}
    assert len(inputs) == 2
    paths = {i["path"] for i in inputs}
    assert paths == {"rtsp://cam1/high", "rtsp://cam1/low"}

    # VigilCam ingestion mapping: main_url + sub_url.
    urls = ingestion_urls(1, fallback_source="rtsp://cam1/fallback")
    assert urls["main_url"] == "rtsp://cam1/high"
    assert urls["sub_url"] == "rtsp://cam1/low"

    # Fallback when no profiles: single source serves all roles.
    sp_mod.clear()
    fb_inputs = build_ffmpeg_inputs(99, fallback_source="rtsp://single/only")
    assert fb_inputs == [{"path": "rtsp://single/only", "roles": ["detect", "record"], "profile": "main"}]
    fb_urls = ingestion_urls(99, fallback_source="rtsp://single/only")
    assert fb_urls == {"main_url": "rtsp://single/only", "sub_url": None}


def test_stream_profiles_duplicate_role_rejected():
    sp_mod.clear()
    set_profile(camera_id=2, name="main", url="rtsp://c2/high", roles=["record", "live"])
    try:
        set_profile(camera_id=2, name="sub", url="rtsp://c2/low", roles=["record"])
        assert False, "duplicate role must raise"
    except ValueError as exc:
        assert "already assigned" in str(exc)

    try:
        set_profile(camera_id=2, name="sub", url="rtsp://c2/low", roles=["bogus"])
        assert False, "unknown role must raise"
    except ValueError as exc:
        assert "unknown role" in str(exc).lower()


def test_stream_profiles_bulk_and_aliases():
    sp_mod.clear()
    data = set_stream_profiles(3, {"main": {"url": "rtsp://c3/high", "roles": ["record"]}, "sub": "rtsp://c3/low"})
    # sub default roles include detect.
    assert data["profiles"]["main"]["url"] == "rtsp://c3/high"
    assert "detect" in data["profiles"]["sub"]["roles"]

    # Aliases: stream->live, sub_stream->sub.
    sp_mod.clear()
    p = set_profile(camera_id=4, name="sub_stream", url="rtsp://c4/low", roles="detection")
    assert p["name"] == "sub"
    assert p["roles"] == ["detect"]


def test_stream_profiles_api_crud():
    sp_mod.clear()
    a = FastAPI()
    a.include_router(stream_profiles_router)
    client = TestClient(a)

    r = client.put("/api/cameras/42/stream-profiles/main", json={"url": "rtsp://c42/high", "roles": ["record", "live"]})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "main"

    r2 = client.put("/api/cameras/42/stream-profiles/sub", json={"url": "rtsp://c42/low", "roles": ["detect"]})
    assert r2.status_code == 200, r2.text

    lst = client.get("/api/cameras/42/stream-profiles")
    assert lst.status_code == 200, lst.text
    body = lst.json()
    assert body["camera_id"] == 42
    assert body["profiles"]["main"]["url"] == "rtsp://c42/high"
    assert body["roles"]["detect"] == "sub"
    assert body["roles"]["record"] == "main"

    roles = client.get("/api/cameras/42/stream-profiles/roles")
    assert roles.status_code == 200
    assert roles.json()["roles"]["detect"] == "sub"

    ff = client.get("/api/cameras/42/stream-profiles/ffmpeg-inputs")
    assert ff.status_code == 200
    assert len(ff.json()["inputs"]) == 2

    ing = client.get("/api/cameras/42/stream-profiles/ingestion")
    assert ing.status_code == 200
    assert ing.json()["main_url"] == "rtsp://c42/high"
    assert ing.json()["sub_url"] == "rtsp://c42/low"

    one = client.get("/api/cameras/42/stream-profiles/sub")
    assert one.status_code == 200
    assert one.json()["url"] == "rtsp://c42/low"

    # Duplicate role across profiles -> 400.
    dup = client.put("/api/cameras/42/stream-profiles/sub", json={"url": "rtsp://c42/low2", "roles": ["record"]})
    assert dup.status_code == 400

    dele = client.delete("/api/cameras/42/stream-profiles/sub")
    assert dele.status_code == 200
    assert client.get("/api/cameras/42/stream-profiles/sub").status_code == 404

    # Underscore alias + top-level create/list.
    alt = client.get("/api/cameras/42/stream_profiles")
    assert alt.status_code == 200
    top = client.post("/api/stream-profiles", json={"camera_id": 7, "name": "main", "url": "rtsp://c7/high"})
    assert top.status_code == 200, top.text
    assert top.json()["camera_id"] == 7


def test_stream_profiles_paths_present():
    # Router itself must expose the expected paths (fast, no app import).
    route_paths = sorted({getattr(r, "path", "") for r in stream_profiles_router.routes})
    assert "/api/cameras/{camera_id}/stream-profiles" in route_paths
    assert "/api/cameras/{camera_id}/stream-profiles/{name}" in route_paths
    assert "/api/stream-profiles" in route_paths or "/api/stream_profiles" in route_paths
    # app/main.py auto-registers any router under app/api (pkgutil loader),
    # so no edit to app/main.py is needed; verify the loader exists.
    from pathlib import Path

    main_src = Path("app/main.py").read_text()
    assert "pkgutil.iter_modules" in main_src
    assert "include_router" in main_src
