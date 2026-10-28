"""Unacknowledged-events smart-filter tests: service + router.

Verification: ``app/services/bulk_ack.py`` already returns per-event results
suitable for UI progress display (``per_event`` / ``per_event_states`` /
``updated_event_ids`` / ``updated_count`` -- asserted below), so no
``ack_progress`` layer was built. This suite covers the complementary read
path: ``app/services/unacked_queue.py`` + ``app/api/unacked_queue.py``
(oldest unacked first).
"""

from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.unacked_queue import router as unacked_queue_router  # noqa: F401
from app.services import unacked_queue as unacked_queue_mod  # noqa: F401
from app.services import event_ack as event_ack_mod  # noqa: F401
from app.services.unacked_queue import (  # noqa: F401
    get_count,
    get_queue,
    is_unacked,
    normalize_order,
    peek_next,
    select_unacked,
)


def _ts(days_ago: float = 0.0, minutes_ago: float = 0.0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago, minutes=minutes_ago)).isoformat()


def test_unacked_queue_imports():
    import app.api.unacked_queue  # noqa: F401
    import app.services.unacked_queue  # noqa: F401
    from app.api.unacked_queue import router  # noqa: F401
    from app.services.unacked_queue import get_queue, peek_next, select_unacked  # noqa: F401
    assert router is not None


def test_bulk_ack_already_returns_per_event_results():
    """Pre-existing bulk_ack has UI progress data -- no ack_progress needed."""
    from app.services import bulk_ack as bulk_mod

    event_ack_mod.clear()
    result = bulk_mod.bulk_acknowledge(event_ids=[901, 902], actor="op")
    # Per-event rows for progress UIs.
    assert "per_event" in result
    assert "per_event_states" in result
    assert result["per_event"]["901"]["acknowledged"] is True
    assert result["per_event"]["902"]["acknowledged"] is True
    # Aggregate progress counts.
    assert result["updated_event_ids"] == [901, 902]
    assert result["updated_count"] == 2
    event_ack_mod.clear()


def test_unacked_queue_paths_present():
    route_paths = [getattr(r, "path", "") for r in unacked_queue_router.routes]
    assert "/api/events/unacked" in route_paths
    assert "/api/events/unacked-queue" in route_paths
    assert "/api/events/unacknowledged" in route_paths
    assert "/api/events/pending-ack" in route_paths
    assert "/api/events/unacked/next" in route_paths
    assert "/api/events/unacked/count" in route_paths
    # App auto-registers app/api/*.py routers via pkgutil (no main.py edits).
    import pathlib

    main_py = pathlib.Path(__file__).resolve().parent.parent / "app" / "main.py"
    text = main_py.read_text()
    assert "pkgutil.iter_modules" in text
    assert "importlib.import_module" in text
    # Isolated app still serves the endpoint (proves router is mountable).
    a = FastAPI()
    a.include_router(unacked_queue_router)
    client = TestClient(a)
    r = client.post("/api/events/unacked", json={"entries": []})
    assert r.status_code == 200, r.text
    assert r.json().get("unacked_count") == 0


def test_unacked_queue_order_normalize():
    assert normalize_order(None) == "oldest"
    assert normalize_order("oldest") == "oldest"
    assert normalize_order("asc") == "oldest"
    assert normalize_order("newest") == "newest"
    assert normalize_order("desc") == "newest"


def test_unacked_queue_select_oldest_first():
    event_ack_mod.clear()
    entries = [
        {"id": 701, "created_at": _ts(days_ago=1)},
        {"id": 702, "created_at": _ts(days_ago=3)},
        {"id": 703, "created_at": _ts(minutes_ago=5)},
    ]
    selected = select_unacked(entries=entries)
    assert [e["id"] for e in selected] == [702, 701, 703]
    # Newest-first opt-in reverses the queue.
    newest = select_unacked(entries=entries, order="newest")
    assert [e["id"] for e in newest] == [703, 701, 702]


def test_unacked_queue_skips_acked_and_escalated():
    event_ack_mod.clear()
    entries = [
        {"id": 711, "created_at": _ts(days_ago=2)},
        {"id": 712, "created_at": _ts(days_ago=4)},
        {"id": 713, "created_at": _ts(days_ago=5)},
    ]
    event_ack_mod.acknowledge(event_id=712, actor="op")
    event_ack_mod.escalate(event_id=713, actor="op", level="high")
    assert is_unacked(711) is True
    assert is_unacked(712) is False
    assert is_unacked(713) is False
    # Escalated excluded by default...
    selected = select_unacked(entries=entries)
    assert [e["id"] for e in selected] == [711]
    # ...unless explicitly included.
    selected_inc = select_unacked(entries=entries, include_escalated=True)
    assert [e["id"] for e in selected_inc] == [713, 711]


def test_unacked_queue_camera_and_label_filters():
    event_ack_mod.clear()
    entries = [
        {"id": 721, "camera_id": 1, "label": "person", "created_at": _ts(days_ago=2)},
        {"id": 722, "camera_id": 2, "label": "car", "created_at": _ts(days_ago=3)},
        {"id": 723, "camera_id": 1, "label": "car", "created_at": _ts(days_ago=1)},
    ]
    cam1 = select_unacked(entries=entries, camera_ids=[1])
    assert [e["id"] for e in cam1] == [721, 723]
    cars = select_unacked(entries=entries, labels=["car"])
    assert [e["id"] for e in cars] == [722, 723]


def test_unacked_queue_pagination_and_counts():
    event_ack_mod.clear()
    entries = [{"id": 730 + i, "created_at": _ts(days_ago=(i + 1))} for i in range(5)]
    queue = get_queue(entries=entries, limit=2, offset=0)
    assert queue["ok"] is True
    assert queue["oldest_first"] is True
    assert queue["unacked_count"] == 5
    assert queue["returned_count"] == 2
    assert queue["has_more"] is True
    assert queue["next_offset"] == 2
    page2 = get_queue(entries=entries, limit=2, offset=2)
    assert page2["returned_count"] == 2
    assert page2["has_more"] is True
    last = get_queue(entries=entries, limit=2, offset=4)
    assert last["returned_count"] == 1
    assert last["has_more"] is False
    # Oldest entry surfaces as next.
    assert queue["next_id"] is not None
    assert peek_next(entries=entries)["id"] == queue["next_id"]
    counts = get_count(entries=entries)
    assert counts["unacked_count"] == 5
    assert counts["ok"] is True


def test_unacked_queue_router_flow():
    event_ack_mod.clear()
    a = FastAPI()
    a.include_router(unacked_queue_router)
    client = TestClient(a)
    entries = [
        {"id": 741, "created_at": _ts(days_ago=1)},
        {"id": 742, "created_at": _ts(days_ago=3)},
    ]
    event_ack_mod.acknowledge(event_id=742, actor="op")

    r = client.post("/api/events/unacked", json={"entries": entries})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("unacked_count") == 1
    assert body.get("event_ids") == [741]
    assert body.get("oldest_first") is True

    n = client.post("/api/events/unacked/next", json={"entries": entries})
    assert n.status_code == 200, n.text
    assert n.json().get("next", {}).get("id") == 741

    c = client.get("/api/events/unacked/count")
    assert c.status_code == 200, c.text
    assert "unacked_count" in c.json()

    # Underscore alias + GET with query params.
    r2 = client.get("/api/events/unacked_queue", params={"limit": 10})
    assert r2.status_code == 200, r2.text

    # event-ack namespace alias serves the queue too.
    r3 = client.post("/api/event-ack/unacked", json={"entries": entries})
    assert r3.status_code == 200, r3.text
    assert r3.json().get("unacked_count") == 1
