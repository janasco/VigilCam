import base64

from fastapi.testclient import TestClient

from app.main import app
from app.services import face_recognition
from app.services.face_recognition import (
    detect_faces,
    recognize_faces,
    register_face,
    list_faces,
    get_face,
    delete_face,
    FaceRecognitionPipeline,
    FaceRecognitionPipelineConfig,
    FaceRead,
    _parse_bbox,
    _parse_recognition_response,
)


def test_face_recognition_import():
    import app.services.face_recognition  # noqa: F401
    import app.api.face_recognition  # noqa: F401
    from app.services.face_recognition import face_recognition_service  # noqa: F401
    from app.api.face_recognition import router  # noqa: F401


def test_face_recognition_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/face_recognition" in paths
    assert "/api/face_recognition/detect" in paths


def test_detect_faces_service():
    res = detect_faces(count=2)
    assert len(res) == 2
    assert res.get("count") == 2
    assert res[0]["face_id"] == "face_1"


def test_register_and_list():
    face_recognition.clear()
    rec = register_face(name="Alice", category="family")
    assert rec["name"] == "Alice"
    assert face_recognition.count() == 1
    got = get_face("alice")
    assert got is not None
    assert got["name"] == "Alice"
    faces = list_faces()
    assert faces.get("count") == 1
    assert delete_face("alice") is True
    assert face_recognition.count() == 0


def test_recognize_faces():
    face_recognition.clear()
    register_face(name="Bob", category="friend")
    res = recognize_faces(count=1)
    assert res.get("count") == 1
    assert res.get("recognized") is True


def test_pipeline_parse():
    bbox = _parse_bbox([1, 2, 3, 4])
    assert bbox == (1, 2, 3, 4)
    read = _parse_recognition_response(
        {"result": {"recognized": True, "person_id": "x", "name": "X", "similarity": 0.9, "face_bbox": [1, 2, 3, 4], "threshold": 0.5}}
    )
    assert isinstance(read, FaceRead)
    assert read.recognized is True


def test_api_detect():
    client = TestClient(app)
    r = client.post("/api/face_recognition/detect", json={"count": 1})
    assert r.status_code == 200, r.text
    assert r.json().get("count") == 1


def test_api_register():
    client = TestClient(app)
    face_recognition.clear()
    r = client.post("/api/face_recognition", json={"name": "Carol"})
    assert r.status_code == 200, r.text
    assert r.json().get("name") == "Carol"
