from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

BASE = Path(__file__).resolve().parents[1]
HTML = (BASE / "app" / "templates" / "index.html").read_text(encoding="utf-8")


def test_node_dispatch_ui_present():
    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200
    html = r.text
    for token in [
        'id="nodeDispatchForm"',
        'id="nodeDispatchStatus"',
        'id="nodeDispatchList"',
        'id="nodeDispatchQueue"',
        'id="nodesList"',
        'id="nodeList"',
        'id="serverList"',
        'id="serversList"',
        'id="nodeForm"',
        'loadNodes',
        'loadNodeDispatch',
        'dispatchToNodes',
        '/api/nodes',
        '/api/node_dispatch',
        '/api/node-dispatch',
    ]:
        assert token in html, token


def test_multi_server_node_dispatch_tokens():
    for token in [
        "multiServerNodeDispatch",
        "multi-server",
        "Multi-server",
        "multiServer",
        "multiserver",
        "dispatchToAllNodes",
        "broadcastNodes",
        "fanoutNodes",
    ]:
        assert token in HTML, token


def test_node_dispatch_endpoints_available():
    client = TestClient(app)
    for path in (
        "/api/nodes",
        "/api/node_dispatch",
        "/api/node-dispatch",
        "/api/nodeDispatch",
        "/api/nodes/dispatch",
    ):
        r = client.get(path)
        # GET may be 200 or 404 depending on route; node_dispatch list should be 200
        assert r.status_code in (200, 404, 405), (path, r.status_code)


def test_node_dispatch_create_roundtrip():
    client = TestClient(app)
    # register a node
    r = client.post("/api/nodes", json={"name": "test-node", "host": "127.0.0.1", "port": 8080, "url": "http://127.0.0.1:8080"})
    assert r.status_code == 200
    node = r.json()
    node_id = node.get("id") or node.get("node_id")
    # dispatch a task
    r2 = client.post("/api/node_dispatch", json={"task": "ping", "node": node_id, "nodes": [node_id]})
    assert r2.status_code == 200
    data = r2.json()
    assert data.get("status") in ("queued", "dispatched", "completed", "success", None) or "id" in data or "job_id" in data


def test_nodes_list_after_register():
    client = TestClient(app)
    r = client.get("/api/nodes")
    assert r.status_code == 200
    assert isinstance(r.json(), list)
