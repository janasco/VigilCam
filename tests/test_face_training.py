from fastapi.testclient import TestClient

from app.main import app
from app.services import face_training
from app.services.face_training import (
    cosine_similarity,
    mock_embedding,
    enroll_face,
    list_faces,
    get_face,
    delete_face,
    train,
    status,
    recognize,
    count,
)
from app.api.face_training import router


def test_face_training_import():
    import app.services.face_training  # noqa: F401
    import app.api.face_training  # noqa: F401
    from app.services.face_training import face_training_service  # noqa: F401
    from app.api.face_training import router  # noqa: F401


def test_face_training_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/face_training" in paths
    assert "/api/face_training/enroll" in paths
    assert "/api/face_training/train" in paths
    assert "/api/face_training/status" in paths
    assert "/api/face_training/match" in paths
    assert "/api/face_training/recognize" in paths


def test_mock_embedding_deterministic():
    a = mock_embedding("alice")
    b = mock_embedding("alice")
    c = mock_embedding("bob")
    assert len(a) == 128
    assert a == b
    assert cosine_similarity(a, b) == cosine_similarity(a, b)
    assert abs(cosine_similarity(a, b) - 1.0) < 1e-9
    assert cosine_similarity(a, c) < 0.99


def test_enroll_list_train_match():
    face_training.clear()
    rec = enroll_face(name="Alice")
    assert rec["name"] == "Alice"
    assert rec["samples"] == 1
    assert rec["trained"] is False
    assert count() == 1

    faces = list_faces()
    assert len(faces) == 1

    got = get_face("alice")
    assert got is not None
    assert got["name"] == "Alice"

    # second sample for same person accumulates
    enroll_face(name="Alice", image_base64="aGVsbG8=")
    got2 = get_face("alice")
    assert got2["samples"] == 2

    st_before = status()
    assert st_before["total"] == 1
    assert st_before["trained"] == 0

    trained = train(name="Alice")
    assert trained["trained"] is True
    assert trained["status"] == "trained"

    st_after = status()
    assert st_after["trained"] == 1
    assert st_after["percent"] == 100.0

    hit = recognize(name="Alice")
    assert hit["recognized"] is True
    assert hit["name"] == "Alice"
    assert hit["similarity"] >= 0.5

    miss = recognize(embedding=mock_embedding("person:some stranger xyz"),
                     threshold=0.99)
    assert miss["recognized"] is False

    assert delete_face("alice") is True
    assert count() == 0


def test_train_all_and_retrain():
    face_training.clear()
    enroll_face(name="Bob")
    enroll_face(name="Carol")
    res = train()
    assert isinstance(res, list)
    assert len(res) == 2
    assert status()["trained"] == 2
    # retrain single person still works
    one = train("bob")
    assert one["trained"] is True


def test_api_flow():
    client = TestClient(app)
    face_training.clear()

    r = client.post("/api/face_training/enroll", json={"name": "Dave"})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Dave"

    r = client.get("/api/face_training")
    assert r.status_code == 200, r.text
    assert len(r.json()) == 1

    r = client.post("/api/face_training/train", json={"name": "Dave"})
    assert r.status_code == 200, r.text
    assert r.json()["trained"] is True

    r = client.get("/api/face_training/status")
    assert r.status_code == 200, r.text
    assert r.json()["trained"] == 1

    r = client.post("/api/face_training/match", json={"name": "Dave"})
    assert r.status_code == 200, r.text
    assert r.json()["recognized"] is True

    r = client.post("/api/face_training/recognize", json={"name": "Dave"})
    assert r.status_code == 200, r.text
    assert r.json()["recognized"] is True

    r = client.get("/api/face_training/dave")
    assert r.status_code == 200, r.text

    r = client.delete("/api/face_training/dave")
    assert r.status_code == 200, r.text

    r = client.get("/api/face_training/count")
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 0
