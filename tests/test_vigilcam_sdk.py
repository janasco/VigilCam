"""Tests for the vigilcam-py style client SDK (sdk/vigilcam_client.py).

Uses httpx.MockTransport so no live server is required. Covers the
required surface: cameras list/add, events list, recordings status,
snapshot, and rules -- plus import shape and error handling.
"""

import httpx

from sdk import vigilcam_client as sdk
from sdk.vigilcam_client import (
    VigilCamCamera,
    VigilCamClient,
    VigilCamConnectionError,
    VigilCamError,
)


def _client(handler):
    transport = httpx.MockTransport(handler)
    session = httpx.Client(transport=transport, base_url="http://testserver/")
    return VigilCamClient("http://testserver/", session=session)


def _json_response(request, payload, status=200):
    return httpx.Response(status, json=payload)


def test_imports_and_structure():
    assert sdk.VigilCamClient is VigilCamClient
    assert issubclass(VigilCamConnectionError, VigilCamError)
    # agent-py style surface
    for attr in (
        "get_cameras",
        "add_camera",
        "list_events",
        "recordings_status",
        "snapshot",
        "list_rules",
        "create_rule",
        "close",
        "is_available",
    ):
        assert hasattr(VigilCamClient, attr), attr


def test_build_server_url_trailing_slash():
    assert VigilCamClient._build_server_url("http://h:8080").endswith("/")
    assert VigilCamClient._build_server_url("http://h:8080/") == "http://h:8080/"


def test_cameras_list_and_add():
    seen = {}

    def handler(request: httpx.Request):
        if request.url.path == "/api/cameras" and request.method == "GET":
            return _json_response(request, [{"id": 1, "name": "cam1", "source": "rtsp://x", "kind": "ip"}])
        if request.url.path == "/api/cameras" and request.method == "POST":
            import json as _json

            seen["body"] = _json.loads(request.content.decode())
            return httpx.Response(201, json={"id": 2, "name": "cam2", "source": "rtsp://y", "kind": "ip"})
        raise AssertionError(f"unexpected {request.method} {request.url.path}")

    client = _client(handler)
    cams = client.get_cameras()
    assert len(cams) == 1
    assert isinstance(cams[0], VigilCamCamera)
    assert cams[0].id == 1
    assert cams[0].name == "cam1"

    created = client.add_camera(name="cam2", source="rtsp://y")
    assert created.id == 2
    assert seen["body"]["name"] == "cam2"
    assert seen["body"]["kind"] == "ip"

    # list alias
    assert len(client.list_cameras()) == 1


def test_camera_helpers_get_update_delete_snapshot_events():
    calls = []

    def handler(request: httpx.Request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/api/cameras/7" and request.method == "GET":
            return _json_response(request, {"id": 7, "name": "c7", "source": "rtsp://z", "kind": "ip"})
        if request.url.path == "/api/cameras/7" and request.method == "PATCH":
            return _json_response(request, {"id": 7, "name": "c7b", "source": "rtsp://z", "kind": "ip"})
        if request.url.path == "/api/cameras/7" and request.method == "DELETE":
            return httpx.Response(204, json={})
        if request.url.path == "/api/snapshot_now/capture":
            return _json_response(request, {"status": "captured", "success": True})
        if request.url.path == "/api/events":
            assert request.url.params["camera_id"] == "7"
            return _json_response(request, [{"id": 9, "camera_id": 7}])
        raise AssertionError(f"unexpected {request.method} {request.url.path}")

    client = _client(handler)
    cam = client.get_camera(7)
    assert cam.name == "c7"
    assert cam.snapshot_url == "api/snapshot_now/capture?camera_id=7"

    updated = client.update_camera(7, {"name": "c7b"})
    assert updated.name == "c7b"

    assert cam.snapshot() == {"status": "captured", "success": True}
    assert cam.get_events() == [{"id": 9, "camera_id": 7}]
    assert client.delete_camera(7) is True


def test_events_list_and_recordings_status_and_crud():
    def handler(request: httpx.Request):
        path = request.url.path
        if path == "/api/events":
            return _json_response(request, [{"id": 1, "label": "person"}])
        if path == "/api/recordings/status":
            return _json_response(request, {"active": 2})
        if path == "/api/recordings":
            return _json_response(request, [{"id": 5, "camera_id": 1}])
        if path == "/api/recordings/start":
            return httpx.Response(201, json={"status": "started"})
        if path == "/api/recordings/stop":
            return _json_response(request, {"status": "stopped"})
        raise AssertionError(f"unexpected {request.method} {path}")

    client = _client(handler)
    assert client.list_events(camera_id=1, limit=5) == [{"id": 1, "label": "person"}]
    assert client.get_events(label="person") == [{"id": 1, "label": "person"}]
    assert client.recordings_status() == {"active": 2}
    assert client.list_recordings(camera_id=1) == [{"id": 5, "camera_id": 1}]
    assert client.start_recording(1, "rtsp://main")["status"] == "started"
    assert client.stop_recording("cam1")["status"] == "stopped"


def test_snapshot_and_rules():
    def handler(request: httpx.Request):
        path = request.url.path
        if path == "/api/snapshot_now/capture":
            return _json_response(request, {"status": "captured", "ok": True})
        if path == "/api/snapshot_now/status":
            return _json_response(request, {"status": "idle", "ok": True})
        if path == "/api/rules/standing" and request.method == "GET":
            return _json_response(request, [{"id": 1, "sentence": "alert on person"}])
        if path == "/api/rules/standing" and request.method == "POST":
            return httpx.Response(201, json={"id": 2, "sentence": "alert on person"})
        if path == "/api/rules/standing/expire":
            return _json_response(request, {"expired": 1})
        raise AssertionError(f"unexpected {request.method} {path}")

    client = _client(handler)
    assert client.snapshot(camera_id=3)["status"] == "captured"
    assert client.snapshot_status()["status"] == "idle"
    assert client.list_rules() == [{"id": 1, "sentence": "alert on person"}]
    assert client.create_rule("alert on person")["id"] == 2
    assert client.add_rule("alert on person")["id"] == 2
    assert client.expire_rules() == {"expired": 1}


def test_connection_error_marks_unavailable():
    def handler(request: httpx.Request):
        raise httpx.ConnectError("refused")

    client = _client(handler)
    try:
        client.get_cameras()
    except VigilCamConnectionError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected VigilCamConnectionError")
    assert client.is_available is False


def test_http_error_raises_vigilcam_error():
    def handler(request: httpx.Request):
        return httpx.Response(404, json={"detail": "Camera not found"})

    client = _client(handler)
    try:
        client.get_camera(999)
    except VigilCamError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected VigilCamError")
