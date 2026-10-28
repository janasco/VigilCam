import base64

from fastapi.testclient import TestClient

from app.main import app
from app.services import semantic_search as ss


def test_semantic_search_import():
    import app.services.semantic_search  # noqa: F401
    import app.api.semantic_search  # noqa: F401


def test_service_add_and_search():
    ss.clear()
    d = ss.add_document(text="a red truck at the dock", tags=["vehicle"])
    assert d is not None
    assert ss.count() == 1
    results = ss.search("red truck")
    assert results
    assert results[0]["text"] == "a red truck at the dock"
    assert results[0]["score"] >= 0.5


def test_service_image_similarity():
    ss.clear()
    img = base64.b64encode(b"\xff\xd8\xff" + b"\x00" * 10).decode()
    ss.add_document(text="snap", image_base64=img)
    q = base64.b64encode(b"\xff\xd8\xff" + b"\x00" * 10).decode()
    results = ss.search(image_base64=q)
    assert results
    assert results[0]["similarity"] >= 0.0


def test_service_aliases():
    ss.clear()
    ss.index(text="hello world")
    assert ss.count() == 1
    assert ss.get(ss.list_documents()[0]["id"]) is not None
    ss.remove(ss.list_documents()[0]["id"])
    assert ss.count() == 0


def test_api_search_events():
    client = TestClient(app)
    cam = client.post("/api/cameras", json={"name": "sscam", "kind": "ip", "source": "rtsp://y"}).json()
    client.post(f"/api/cameras/{cam['id']}/detect", json={"simulate": True})
    r = client.get("/api/semantic_search", params={"q": "person"})
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_api_index_and_search_documents():
    client = TestClient(app)
    ss.clear()
    r = client.post("/api/semantic_search", json={"text": "indexed doc", "id": 42})
    assert r.status_code == 200
    r2 = client.get("/api/semantic_search", params={"q": "indexed"})
    assert r2.status_code == 200
    assert any(d.get("id") == 42 for d in r2.json())
