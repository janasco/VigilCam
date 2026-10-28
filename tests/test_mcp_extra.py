"""Extra MCP tool tests (live snapshot + recording search + security status).

Modeled on Agent DVR Claude extension capabilities: live snapshots,
recording metadata search, and arm/disarm status summary.
"""
from __future__ import annotations

import datetime
import json
import sqlite3
from pathlib import Path

import pytest

import app.mcp_core_server as mcp_server
from app.services import snapshot_now


def _make_db(path: Path) -> Path:
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE cameras (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "name VARCHAR(120) NOT NULL, kind VARCHAR(32) NOT NULL DEFAULT 'ip', "
        "source VARCHAR(512) NOT NULL DEFAULT '', enabled BOOLEAN NOT NULL DEFAULT 1)"
    )
    conn.execute(
        "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "camera_id INTEGER, kind VARCHAR(64) NOT NULL, label VARCHAR(120) NOT NULL, "
        "confidence FLOAT, payload TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.execute(
        "CREATE TABLE recordings (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "camera_id INTEGER NOT NULL, path VARCHAR(1024) NOT NULL, "
        "started_at DATETIME NOT NULL, ended_at DATETIME, size_bytes INTEGER, "
        "created_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.execute(
        "CREATE TABLE standing_rules (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "sentence VARCHAR(512) NOT NULL, target_object VARCHAR(120) NOT NULL, "
        "attributes TEXT, camera_id INTEGER, expires_at DATETIME NOT NULL, "
        "is_active BOOLEAN NOT NULL DEFAULT 1, "
        "created_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path / "test_mcp.db")
    monkeypatch.setattr(mcp_server, "DB_PATH", db_path)
    snap_dir = tmp_path / "snaps"
    snap_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(snapshot_now, "SNAPSHOT_DIR", snap_dir)
    monkeypatch.setenv("VIGILCAM_SNAPSHOT_DIR", str(snap_dir))
    try:
        from app.core import config as _config

        monkeypatch.setattr(_config, "SNAPSHOT_DIR", snap_dir)
    except Exception:
        pass
    return db_path


def _insert_camera(db_path: Path, name="front-door", source="rtsp://example/cam") -> int:
    conn = sqlite3.connect(str(db_path))
    cur = conn.execute(
        "INSERT INTO cameras (name, kind, source, enabled) VALUES (?, 'ip', ?, 1)",
        (name, source),
    )
    conn.commit()
    cid = cur.lastrowid
    conn.close()
    return cid


def test_imports():
    assert callable(mcp_server.get_live_snapshot)
    assert callable(mcp_server.search_recordings)
    assert callable(mcp_server.get_security_status)


def test_live_snapshot_by_name(isolated_db, tmp_path):
    cid = _insert_camera(isolated_db, name="front-door")
    out = json.loads(mcp_server.get_live_snapshot("front"))
    assert out.get("camera_id") == cid
    assert out.get("camera_name") == "front-door"
    assert out.get("snapshot_path")
    assert Path(out["snapshot_path"]).is_file()
    assert Path(out["snapshot_path"]).stat().st_size > 0


def test_live_snapshot_by_id(isolated_db):
    cid = _insert_camera(isolated_db, name="driveway")
    out = json.loads(mcp_server.get_live_snapshot(str(cid)))
    assert out.get("camera_id") == cid


def test_live_snapshot_unknown_camera(isolated_db):
    _insert_camera(isolated_db, name="porch")
    out = json.loads(mcp_server.get_live_snapshot("no-such-cam"))
    assert "error" in out


def test_live_snapshot_requires_name(isolated_db):
    out = json.loads(mcp_server.get_live_snapshot("  "))
    assert "error" in out


def test_search_recordings_filters_by_camera_and_day(isolated_db):
    c1 = _insert_camera(isolated_db, name="driveway")
    c2 = _insert_camera(isolated_db, name="porch")
    today = datetime.date.today()
    yesterday = today - datetime.timedelta(days=1)
    conn = sqlite3.connect(str(isolated_db))
    conn.execute(
        "INSERT INTO recordings (camera_id, path, started_at) VALUES (?, ?, ?)",
        (c1, "/tmp/a.mp4", f"{today.isoformat()} 10:00:00"),
    )
    conn.execute(
        "INSERT INTO recordings (camera_id, path, started_at) VALUES (?, ?, ?)",
        (c1, "/tmp/b.mp4", f"{yesterday.isoformat()} 10:00:00"),
    )
    conn.execute(
        "INSERT INTO recordings (camera_id, path, started_at) VALUES (?, ?, ?)",
        (c2, "/tmp/c.mp4", f"{today.isoformat()} 11:00:00"),
    )
    conn.commit()
    conn.close()

    out = json.loads(mcp_server.search_recordings("driveway", "today"))
    assert out["count"] == 1
    assert out["recordings"][0]["path"] == "/tmp/a.mp4"

    out_all = json.loads(mcp_server.search_recordings("", "all"))
    assert out_all["count"] == 3

    out_y = json.loads(mcp_server.search_recordings("driveway", "yesterday"))
    assert out_y["count"] == 1


def test_search_recordings_invalid_date(isolated_db):
    _insert_camera(isolated_db, name="driveway")
    out = json.loads(mcp_server.search_recordings("driveway", "not-a-date"))
    assert "error" in out


def test_search_recordings_unknown_camera(isolated_db):
    out = json.loads(mcp_server.search_recordings("ghost-cam", "all"))
    assert out["recordings"] == []
    assert out["count"] == 0


def test_search_recordings_limit_clamped(isolated_db):
    c1 = _insert_camera(isolated_db, name="driveway")
    conn = sqlite3.connect(str(isolated_db))
    for i in range(5):
        conn.execute(
            "INSERT INTO recordings (camera_id, path, started_at) VALUES (?, ?, ?)",
            (c1, f"/tmp/{i}.mp4", f"{datetime.date.today().isoformat()} 10:0{i}:00"),
        )
    conn.commit()
    conn.close()
    out = json.loads(mcp_server.search_recordings("driveway", "today", 2))
    assert out["count"] == 2


def test_security_status_counts(isolated_db, tmp_path, monkeypatch):
    state_file = tmp_path / "system_state.txt"
    state_file.write_text("arm", encoding="utf-8")
    monkeypatch.setattr(mcp_server, "STATE_PATH", state_file)
    cid = _insert_camera(isolated_db, name="driveway")
    today = datetime.date.today().isoformat()
    conn = sqlite3.connect(str(isolated_db))
    conn.execute(
        "INSERT INTO events (camera_id, kind, label, created_at) VALUES (?, 'motion', 'person', ?)",
        (cid, f"{today} 10:00:00"),
    )
    conn.execute(
        "INSERT INTO recordings (camera_id, path, started_at) VALUES (?, ?, ?)",
        (cid, "/tmp/a.mp4", f"{today} 10:00:00"),
    )
    future = (datetime.datetime.now() + datetime.timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S.%f")
    conn.execute(
        "INSERT INTO standing_rules (sentence, target_object, attributes, camera_id, expires_at, is_active) "
        "VALUES (?, ?, ?, ?, ?, 1)",
        ("tell me if a red car turns up", "car", '{"color": "red"}', cid, future),
    )
    conn.commit()
    conn.close()
    out = json.loads(mcp_server.get_security_status())
    assert out["system_state"] == "arm"
    assert out["cameras"] == 1
    assert out["events_today"] == 1
    assert out["recordings_today"] == 1
    assert out["active_rules"] == 1
