from __future__ import annotations

import json
import os
import re
import sqlite3
import datetime
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = Path(os.getenv("VIGILCAM_DB_PATH", str(PROJECT_ROOT / "data" / "vigilcam.db")))
STATE_PATH = Path(os.getenv("VIGILCAM_STATE_PATH", str(PROJECT_ROOT / "config" / "system_state.txt")))

mcp = MCPServer("VigilCam")


def _connect() -> sqlite3.Connection | None:
    try:
        if not DB_PATH.exists():
            return None
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        return None


_TIME_PATTERN = re.compile(r"next\s+(\d+)\s+(hour|minute|day)s?", re.IGNORECASE)
_COLOR_PATTERN = re.compile(r"\b(red|blue|green|black|white|silver|yellow)\b", re.IGNORECASE)
_OBJECT_PATTERN = re.compile(r"\b(car|truck|person|dog|cat|package|bicycle)\b", re.IGNORECASE)


def _compile_sentence(sentence: str) -> dict[str, Any] | None:
    time_match = _TIME_PATTERN.search(sentence)
    color_match = _COLOR_PATTERN.search(sentence)
    object_match = _OBJECT_PATTERN.search(sentence)

    if not object_match:
        return None

    duration = int(time_match.group(1)) if time_match else 24
    unit = time_match.group(2).lower() if time_match else "hours"

    now = datetime.datetime.now()
    if "minute" in unit:
        expires_at = now + datetime.timedelta(minutes=duration)
    elif "day" in unit:
        expires_at = now + datetime.timedelta(days=duration)
    else:
        expires_at = now + datetime.timedelta(hours=duration)

    attributes: dict[str, Any] = {}
    if color_match:
        attributes["color"] = color_match.group(1).lower()

    return {
        "sentence": sentence,
        "target_object": object_match.group(1).lower(),
        "attributes": attributes,
        "expires_at": expires_at,
    }


@mcp.tool()
def list_cameras() -> str:
    """Return active cameras and their basic state."""
    conn = _connect()
    if conn is None:
        return json.dumps({"cameras": [], "note": "Database not found"})
    try:
        rows = conn.execute(
            "SELECT id, name, kind, source, enabled FROM cameras ORDER BY id"
        ).fetchall()
        cameras = [dict(row) for row in rows]
        return json.dumps({"cameras": cameras})
    except Exception as exc:
        return json.dumps({"error": str(exc)})
    finally:
        conn.close()


@mcp.tool()
def query_alerts(camera_name: str, timeframe: str = "today") -> str:
    """Query security events for a camera."""
    conn = _connect()
    if conn is None:
        return json.dumps({"events": [], "note": "Database not found"})
    try:
        camera_ids = [row["id"] for row in conn.execute(
            "SELECT id FROM cameras WHERE name LIKE ?", (f"%{camera_name}%",)
        ).fetchall()]
        if camera_ids:
            placeholders = ",".join("?" for _ in camera_ids)
            query = (
                "SELECT id, camera_id, kind, label, confidence, created_at "
                f"FROM events WHERE camera_id IN ({placeholders}) "
            )
            params: list[Any] = list(camera_ids)
        else:
            query = (
                "SELECT id, camera_id, kind, label, confidence, created_at "
                "FROM events WHERE label LIKE ? "
            )
            params = [f"%{camera_name}%"]

        if timeframe and timeframe.lower() not in {"all", "today", ""}:
            pass
        if timeframe and timeframe.lower() == "today":
            today = datetime.date.today().isoformat()
            query += "AND created_at >= ? "
            params.append(today)

        query += "ORDER BY id DESC LIMIT 20"
        rows = conn.execute(query, params).fetchall()
        events = [dict(row) for row in rows]
        return json.dumps({"camera_name": camera_name, "timeframe": timeframe, "events": events})
    except Exception as exc:
        return json.dumps({"error": str(exc)})
    finally:
        conn.close()


@mcp.tool()
def get_incident_snapshot(alert_id: str) -> str:
    """Return a placeholder snapshot reference for an alert."""
    return json.dumps(
        {
            "alert_id": alert_id,
            "snapshot": "placeholder",
            "note": "Snapshot binary stored locally and returned on demand in production",
        }
    )


@mcp.tool()
def set_system_state(mode: str) -> str:
    """Set the system profile to arm, disarm, or motion_only."""
    normalized = mode.strip().lower()
    if normalized not in {"arm", "disarm", "motion_only"}:
        return "Invalid mode"
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(normalized, encoding="utf-8")
    return f"VigilCam system status successfully changed to: {normalized.upper()}"


@mcp.tool()
def create_standing_rule(sentence: str, camera_id: Any = None) -> str:
    """Create a standing natural-language rule from a sentence."""
    conn = _connect()
    if conn is None:
        return json.dumps({"error": "Database not found"})
    compiled = _compile_sentence(sentence)
    if compiled is None:
        conn.close()
        return json.dumps({"error": "Could not parse a target object from sentence"})
    try:
        if camera_id is not None:
            camera_id = int(camera_id)
    except (TypeError, ValueError):
        pass
    try:
        now_str = compiled["expires_at"].strftime("%Y-%m-%d %H:%M:%S.%f")
        attrs = json.dumps(compiled["attributes"])
        cur = conn.execute(
            "INSERT INTO standing_rules "
            "(sentence, target_object, attributes, camera_id, expires_at, is_active, created_at) "
            "VALUES (?, ?, ?, ?, ?, 1, ?)",
            (
                compiled["sentence"],
                compiled["target_object"],
                attrs,
                camera_id,
                now_str,
                datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f"),
            ),
        )
        conn.commit()
        rule_id = cur.lastrowid
        return json.dumps(
            {
                "id": rule_id,
                "sentence": compiled["sentence"],
                "target_object": compiled["target_object"],
                "attributes": compiled["attributes"],
                "camera_id": camera_id,
                "expires_at": compiled["expires_at"].isoformat(),
            }
        )
    except Exception as exc:
        return json.dumps({"error": str(exc)})
    finally:
        conn.close()


@mcp.tool()
def list_standing_rules(active_only: bool = True) -> str:
    """List standing rules stored in the database."""
    conn = _connect()
    if conn is None:
        return json.dumps({"rules": [], "note": "Database not found"})
    try:
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
        if active_only:
            rows = conn.execute(
                "SELECT id, sentence, target_object, attributes, camera_id, expires_at, is_active, created_at "
                "FROM standing_rules WHERE is_active = 1 AND expires_at > ? ORDER BY id DESC",
                (now,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, sentence, target_object, attributes, camera_id, expires_at, is_active, created_at "
                "FROM standing_rules ORDER BY id DESC"
            ).fetchall()
        rules = [dict(row) for row in rows]
        return json.dumps({"rules": rules})
    except Exception as exc:
        return json.dumps({"error": str(exc)})
    finally:
        conn.close()


@mcp.tool()
def expire_standing_rules() -> str:
    """Run expiration of standing rules."""
    conn = _connect()
    if conn is None:
        return json.dumps({"expired": 0, "note": "Database not found"})
    try:
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
        cur = conn.execute(
            "UPDATE standing_rules SET is_active = 0 WHERE is_active = 1 AND expires_at <= ?",
            (now,),
        )
        conn.commit()
        return json.dumps({"expired": cur.rowcount})
    except Exception as exc:
        return json.dumps({"error": str(exc)})
    finally:
        conn.close()


_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")

_MINIMAL_JPEG = (
    b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    b"\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342"
    b"\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00"
    b"\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b"
    b"\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xfb\xd2\x8a(\xa0\x00\xff\xd9"
)


def _resolve_snapshot_dir() -> Path:
    override = os.getenv("VIGILCAM_SNAPSHOT_DIR")
    if override:
        return Path(override).expanduser()
    try:
        from app.core import config as _config

        candidate = getattr(_config, "SNAPSHOT_DIR", None)
        if candidate is not None:
            return Path(candidate).expanduser()
    except Exception:
        pass
    return (PROJECT_ROOT / "data" / "snapshots").resolve()


def _write_placeholder_jpeg(dest: Path, label: str = "VigilCam") -> tuple[str, int]:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import Image, ImageDraw  # type: ignore

        img = Image.new("RGB", (640, 480), color=(20, 20, 30))
        draw = ImageDraw.Draw(img)
        draw.text((10, 230), str(label)[:48], fill=(255, 255, 255))
        import io as _io

        buf = _io.BytesIO()
        img.save(buf, format="JPEG", quality=70)
        data = buf.getvalue()
    except Exception:
        data = _MINIMAL_JPEG
    dest.write_bytes(data)
    return str(dest), len(data)


def _parse_recording_day(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"all", "any"}:
        return None
    lowered = text.lower()
    today = datetime.date.today()
    if lowered == "today":
        return today.isoformat()
    if lowered == "yesterday":
        return (today - datetime.timedelta(days=1)).isoformat()
    match = _DATE_RE.search(text)
    if match:
        try:
            return datetime.date.fromisoformat(match.group(1)).isoformat()
        except ValueError:
            raise ValueError(f"Invalid date: {value!r}. Use YYYY-MM-DD, 'today', or 'yesterday'.")
    raise ValueError(f"Invalid date: {value!r}. Use YYYY-MM-DD, 'today', or 'yesterday'.")


def _safe_count(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    try:
        row = conn.execute(sql, params).fetchone()
        if row is None:
            return 0
        try:
            return int(row[0])
        except (TypeError, ValueError, KeyError, IndexError):
            return 0
    except Exception:
        return 0


@mcp.tool()
def get_live_snapshot(camera_name: str) -> str:
    """Capture a live snapshot frame for a camera by name or id.

    Modeled on Agent DVR's live-image tool: resolves the camera, grabs one
    frame via the snapshot service (placeholder JPEG when no stream is
    reachable), and returns the file path plus metadata as JSON.
    """
    label = (camera_name or "").strip()
    if not label:
        return json.dumps({"error": "camera_name is required"})
    conn = _connect()
    if conn is None:
        return json.dumps({"error": "Database not found"})
    try:
        camera: dict[str, Any] | None = None
        try:
            wanted_id = int(label)
        except (TypeError, ValueError):
            wanted_id = None
        if wanted_id is not None:
            row = conn.execute(
                "SELECT id, name, kind, source, enabled FROM cameras WHERE id = ?",
                (wanted_id,),
            ).fetchone()
            if row is not None:
                camera = dict(row)
        if camera is None:
            row = conn.execute(
                "SELECT id, name, kind, source, enabled FROM cameras WHERE name LIKE ? ORDER BY id LIMIT 1",
                (f"%{label}%",),
            ).fetchone()
            if row is not None:
                camera = dict(row)
        if camera is None:
            return json.dumps({"error": f"Camera not found: {label}"})
    finally:
        conn.close()

    camera_id = camera.get("id")
    camera_label = str(camera.get("name") or label)
    source = camera.get("source") or ""

    try:
        from app.services.snapshot_now import capture_snapshot_now

        result = capture_snapshot_now(camera_id=camera_id, source=source or None, label=camera_label)
        snapshot_path = str(result.get("path") or result.get("snapshot_path") or "")
        if snapshot_path and Path(snapshot_path).is_file():
            return json.dumps(
                {
                    "camera_id": camera_id,
                    "camera_name": camera_label,
                    "snapshot_path": snapshot_path,
                    "size_bytes": result.get("size_bytes", result.get("size", 0)),
                    "method": result.get("method", "snapshot_service"),
                    "captured_at": result.get("captured_at"),
                }
            )
    except Exception:
        pass

    snap_dir = _resolve_snapshot_dir()
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = "snapshot_mcp_{}_{}_{}.jpg".format(camera_id, stamp, os.getpid())
    target = (snap_dir / filename).resolve()
    try:
        snapshot_path, size = _write_placeholder_jpeg(target, label=camera_label)
    except Exception as exc:
        return json.dumps({"error": f"Snapshot capture failed: {exc}"})
    return json.dumps(
        {
            "camera_id": camera_id,
            "camera_name": camera_label,
            "snapshot_path": snapshot_path,
            "size_bytes": size,
            "method": "placeholder-fallback",
            "captured_at": datetime.datetime.now().isoformat(),
        }
    )


@mcp.tool()
def search_recordings(camera_name: str = "", date: str = "today", limit: int = 20) -> str:
    """Search stored recording segments for a camera and day.

    Modeled on Agent DVR's recording-metadata tool ("show me the driveway
    camera this morning"). Accepts a camera name (substring) or blank for all
    cameras, a day ('today', 'yesterday', 'YYYY-MM-DD', or 'all'), and a limit.
    """
    try:
        day = _parse_recording_day(date)
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    try:
        limit_int = int(limit)
    except (TypeError, ValueError):
        limit_int = 20
    limit_int = max(1, min(100, limit_int))

    conn = _connect()
    if conn is None:
        return json.dumps({"recordings": [], "note": "Database not found"})
    try:
        camera_ids: list[Any] = []
        wanted = (camera_name or "").strip()
        if wanted:
            try:
                wanted_id = int(wanted)
            except (TypeError, ValueError):
                wanted_id = None
            if wanted_id is not None:
                row = conn.execute("SELECT id FROM cameras WHERE id = ?", (wanted_id,)).fetchone()
                if row is not None:
                    camera_ids = [row["id"]]
            if not camera_ids:
                camera_ids = [
                    row["id"]
                    for row in conn.execute(
                        "SELECT id FROM cameras WHERE name LIKE ?", (f"%{wanted}%",)
                    ).fetchall()
                ]
                if not camera_ids:
                    return json.dumps(
                        {
                            "camera_name": wanted,
                            "date": date,
                            "recordings": [],
                            "count": 0,
                            "note": f"Camera not found: {wanted}",
                        }
                    )
        query = (
            "SELECT r.id, r.camera_id, c.name AS camera_name, r.path, "
            "r.started_at, r.ended_at, r.size_bytes "
            "FROM recordings r LEFT JOIN cameras c ON c.id = r.camera_id WHERE 1=1 "
        )
        params: list[Any] = []
        if camera_ids:
            placeholders = ",".join("?" for _ in camera_ids)
            query += f"AND r.camera_id IN ({placeholders}) "
            params.extend(camera_ids)
        if day is not None:
            next_day = (
                datetime.date.fromisoformat(day) + datetime.timedelta(days=1)
            ).isoformat()
            query += "AND r.started_at >= ? AND r.started_at < ? "
            params.extend([day, next_day])
        query += "ORDER BY r.started_at DESC LIMIT ?"
        params.append(limit_int)
        rows = conn.execute(query, params).fetchall()
        recordings = [dict(row) for row in rows]
        return json.dumps(
            {
                "camera_name": wanted,
                "date": date,
                "day": day,
                "recordings": recordings,
                "count": len(recordings),
            }
        )
    except Exception as exc:
        return json.dumps({"error": str(exc)})
    finally:
        conn.close()


@mcp.tool()
def get_security_status() -> str:
    """Return armed state plus camera/event/rule/recording counts.

    Modeled on Agent DVR's profile/arm-disarm workflow: gives Claude a quick
    "is the system armed, what happened today" summary in one call.
    """
    try:
        state = STATE_PATH.read_text(encoding="utf-8").strip().lower() if STATE_PATH.exists() else "unknown"
    except Exception:
        state = "unknown"
    conn = _connect()
    if conn is None:
        return json.dumps({"system_state": state, "note": "Database not found"})
    try:
        today = datetime.date.today().isoformat()
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
        cameras = _safe_count(conn, "SELECT COUNT(*) FROM cameras")
        events_today = _safe_count(conn, "SELECT COUNT(*) FROM events WHERE created_at >= ?", (today,))
        active_rules = _safe_count(
            conn,
            "SELECT COUNT(*) FROM standing_rules WHERE is_active = 1 AND expires_at > ?",
            (now_str,),
        )
        recordings_today = _safe_count(
            conn, "SELECT COUNT(*) FROM recordings WHERE started_at >= ?", (today,)
        )
        return json.dumps(
            {
                "system_state": state,
                "cameras": cameras,
                "events_today": events_today,
                "active_rules": active_rules,
                "recordings_today": recordings_today,
            }
        )
    except Exception as exc:
        return json.dumps({"error": str(exc)})
    finally:
        conn.close()


def main() -> None:
    import asyncio

    asyncio.run(mcp.run_stdio_async())


if __name__ == "__main__":
    main()
