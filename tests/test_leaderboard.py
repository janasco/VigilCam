import asyncio

from fastapi.testclient import TestClient

from app.main import app


def test_leaderboard_import():
    import app.api.leaderboard  # noqa: F401
    import app.services.leaderboard  # noqa: F401
    from app.api.leaderboard import router  # noqa: F401
    from app.services.leaderboard import (  # noqa: F401
        build_leaderboard,
        get_leaderboard,
        get_top_groups,
        get_group_rank,
        list_rankings,
        status,
        leaderboard_service,
        LeaderboardService,
    )


def test_summary_has_aggregate_counts_without_ranking():
    # Verification: pre-existing dashboard returns aggregate counts only.
    from app.services.summary import get_dashboard_summary

    data = asyncio.run(get_dashboard_summary(None))
    assert isinstance(data, dict)
    for key in ("cameras", "events", "recordings", "storage", "system"):
        assert key in data
    for banned in ("leaderboard", "rankings", "ranking", "ranks", "top_groups"):
        assert banned not in data, f"summary should not rank: {banned}"
        for section in ("cameras", "events", "recordings", "storage", "system"):
            assert banned not in (data.get(section) or {})


def test_group_digest_has_counts_without_ranking():
    # Verification: pre-existing group digests aggregate counts only.
    from app.services import group_digest

    group_digest.clear()
    group_digest.record_notification(type="motion", message="m1", group="Lobby")
    group_digest.record_notification(type="motion", message="m2", group="Parking")
    d = group_digest.build_all_digests()
    assert d.get("total_groups") == 2
    assert d.get("by_group") == {"Lobby": 1, "Parking": 1}
    for banned in ("leaderboard", "rankings", "ranking", "ranks"):
        assert banned not in d, f"group digest should not rank: {banned}"
    single = group_digest.build_digest(group="Lobby")
    for banned in ("leaderboard", "rankings", "ranking", "rank", "ranks"):
        assert banned not in single, f"single digest should not rank: {banned}"
    group_digest.clear()


def test_service_builds_ranked_board():
    from app.services import group_digest
    from app.services.leaderboard import build_leaderboard

    group_digest.clear()
    for _ in range(3):
        group_digest.record_notification(type="motion", message="m", group="Lobby")
    group_digest.record_notification(type="motion", message="m", group="Parking")
    for _ in range(2):
        group_digest.record_notification(type="face", message="f", group="Garage")

    board = build_leaderboard()
    assert board.get("ok") is True
    assert board.get("total") == 6
    assert board.get("total_groups") == 3
    entries = board.get("leaderboard")
    assert isinstance(entries, list) and len(entries) == 3
    # Ordered desc by count with explicit ranks.
    assert [e["group"] for e in entries] == ["Lobby", "Garage", "Parking"]
    assert [e["count"] for e in entries] == [3, 2, 1]
    assert [e["rank"] for e in entries] == [1, 2, 3]
    assert board.get("by_group") == {"Lobby": 3, "Parking": 1, "Garage": 2}
    assert board.get("top_group", {}).get("group") == "Lobby"
    group_digest.clear()


def test_service_top_limit_and_rank_lookup():
    from app.services import group_digest
    from app.services.leaderboard import get_group_rank, get_top_groups

    group_digest.clear()
    for _ in range(3):
        group_digest.record_notification(type="motion", message="m", group="Lobby")
    group_digest.record_notification(type="motion", message="m", group="Parking")

    top = get_top_groups(limit=1)
    assert len(top) == 1
    assert top[0]["group"] == "Lobby"
    assert top[0]["rank"] == 1

    rank = get_group_rank(group="Parking")
    assert rank is not None
    assert rank["group"] == "Parking"
    assert rank["rank"] == 2
    assert get_group_rank(group="Missing") is None
    group_digest.clear()


def test_service_empty_board():
    from app.services import group_digest
    from app.services.leaderboard import build_leaderboard, status

    group_digest.clear()
    board = build_leaderboard()
    assert board.get("total") == 0
    assert board.get("total_groups") == 0
    assert board.get("leaderboard") == []
    assert board.get("top_group") is None
    st = status()
    assert st.get("service") == "leaderboard"
    assert st.get("total_groups") == 0


def test_service_class():
    from app.services import group_digest
    from app.services.leaderboard import LeaderboardService

    group_digest.clear()
    group_digest.record_notification(type="motion", message="m", group="Lobby")
    svc = LeaderboardService()
    board = svc.build_leaderboard()
    assert board.get("total_groups") == 1
    assert svc.get_group_rank(group="Lobby")["rank"] == 1
    assert len(svc.top(limit=1)) == 1
    assert svc.status()["service"] == "leaderboard"
    group_digest.clear()


def test_leaderboard_paths_present():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/leaderboard" in paths
    assert any("leaderboard" in p for p in paths)


def test_leaderboard_api_returns_ranking():
    from app.services import group_digest

    group_digest.clear()
    client = TestClient(app)
    group_digest.record_notification(type="motion", message="m1", group="Lobby")
    group_digest.record_notification(type="motion", message="m2", group="Lobby")
    group_digest.record_notification(type="motion", message="m3", group="Parking")

    r = client.get("/api/leaderboard")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data.get("total") == 3
    assert data.get("total_groups") == 2
    entries = data.get("leaderboard")
    assert [e["group"] for e in entries] == ["Lobby", "Parking"]
    assert entries[0]["rank"] == 1

    r2 = client.get("/api/leaderboard/top", params={"limit": 1})
    assert r2.status_code == 200, r2.text
    assert len(r2.json().get("top", [])) == 1

    r3 = client.get("/api/leaderboard/rank", params={"group": "Parking"})
    assert r3.status_code == 200, r3.text
    assert r3.json().get("group") == "Parking"
    assert r3.json().get("rank") == 2

    r4 = client.get("/api/leaderboard/Lobby")
    assert r4.status_code == 200, r4.text
    assert r4.json().get("group") == "Lobby"

    r5 = client.get("/api/leaderboard/status")
    assert r5.status_code == 200, r5.text
    assert r5.json().get("service") == "leaderboard"
    group_digest.clear()
