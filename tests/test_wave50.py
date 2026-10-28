import os

from fastapi.testclient import TestClient

from app.main import app
from app.services import floorplan_editor, layout_editor
from app.services.floorplan_editor import (
    FloorplanEditor,
    normalize_position,
    extract_camera_positions,
    apply_camera_positions,
)


def test_import_wave50():
    from app.main import app as _app
    paths = list(_app.openapi()["paths"].keys())
    assert "/api/layouts/{layout_id}/camera-positions" in paths
    assert "/api/floorplan/editor/preview" in paths


def test_floorplan_editor_service_basic():
    editor = FloorplanEditor(width=100, height=100)
    editor.add_marker(1, 10, 20)
    assert editor.positions[0]["x"] == 0.1
    editor.move_marker(1, 0.5, 0.5)
    assert editor.positions[0]["x"] == 0.5
    preview = editor.preview()
    assert preview["count"] == 1
    layout = editor.to_layout({"name": "x"})
    assert layout["camera_positions"][0]["x"] == 0.5


def test_layout_crud_camera_positions():
    client = TestClient(app)
    res = client.post("/api/layouts", json={"name": "wave50", "camera_positions": [{"camera_id": 1, "x": 5, "y": 10}], "width": 100, "height": 100})
    assert res.status_code == 201
    lid = res.json()["id"]
    assert res.json()["camera_positions"][0]["x"] == 0.05
    g = client.get(f"/api/layouts/{lid}/camera-positions")
    assert g.status_code == 200
    assert g.json()["camera_positions"][0]["y"] == 0.1