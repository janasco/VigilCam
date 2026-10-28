import inspect
from datetime import datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import app
from app.api.auto_rules import router as auto_rules_router  # noqa: F401
from app.services import auto_rules as auto_rules_service_mod  # noqa: F401
from app.services.auto_rules import (  # noqa: F401
    auto_rules_service,
    clear,
    create_rule,
    delete_rule,
    evaluate_rule,
    evaluate_rules,
    get_rule,
    list_rules,
    match_events,
    rule_matches,
    run_rules,
    update_rule,
)
from app.services.retention import RetentionManager


def _event(**overrides):
    base = {
        "id": 1,
        "camera_id": 1,
        "kind": "motion",
        "label": "person",
        "confidence": 0.9,
        "tags": [],
        "created_at": datetime.now().isoformat(),
    }
    base.update(overrides)
    return base


def test_auto_rules_import():
    import app.api.auto_rules  # noqa: F401
    import app.services.auto_rules  # noqa: F401
    from app.api.auto_rules import router  # noqa: F401
    from app.services.auto_rules import create_rule, list_rules, run_rules  # noqa: F401


def test_retention_is_only_age_and_size_with_no_condition_filters():
    sig = inspect.signature(RetentionManager.select_for_deletion)
    params = set(sig.parameters.keys())
    # Age/size knobs only -- no event-condition filters.
    assert "max_age_days" in params
    assert "max_total_bytes" in params
    for forbidden in ("label", "labels", "kind", "confidence", "min_confidence",
                      "max_confidence", "tags", "tag", "cause", "score", "event"):
        assert forbidden not in params, f"retention should not filter by {forbidden}: {params}"
    src = inspect.getsource(RetentionManager.select_for_deletion)
    lowered = src.lower()
    for forbidden in ("label", "confidence", "tags", "cause"):
        assert forbidden not in lowered


def test_auto_rules_paths_present():
    # The pkgutil auto-loader in app/main.py picks up app/api/auto_rules.py
    # without any main.py edit; the router is served live (FastAPI 0.143+
    # keeps included routers as lazy _IncludedRouter entries, so route by
    # live request instead of the very slow app.openapi() build).
    from app.api.auto_rules import router as _router

    assert any(
        getattr(r, "original_router", None) is _router for r in app.routes
    ), "auto_rules router not registered on app"
    client = TestClient(app)
    assert client.get("/api/auto-rules").status_code == 200
    assert client.get("/api/zmfilter").status_code == 200


def test_auto_rules_service_crud():
    auto_rules_service_mod.clear()
    created = create_rule(name="purge low-conf",
                          conditions={"max_confidence": 0.3},
                          actions={"auto_delete": True})
    assert created["id"] is not None
    assert created["name"] == "purge low-conf"
    assert created["actions"].get("auto_delete") is True

    items = list_rules()
    assert len(items) == 1

    fetched = get_rule(created["id"])
    assert fetched is not None
    assert fetched["name"] == "purge low-conf"

    updated = update_rule(created["id"], description="updated desc")
    assert updated is not None
    assert updated["description"] == "updated desc"
    # Conditions preserved across unrelated update.
    assert updated["conditions"].get("max_confidence") == 0.3

    assert delete_rule(created["id"]) is True
    assert get_rule(created["id"]) is None
    assert list_rules() == []


def test_auto_rules_service_validation():
    auto_rules_service_mod.clear()
    try:
        create_rule(conditions={"label": "x"})
    except ValueError as exc:
        assert "name" in str(exc).lower()
    else:
        raise AssertionError("expected ValueError for missing name")


def test_auto_rules_match_simple_conditions():
    auto_rules_service_mod.clear()
    rule = create_rule(name="door high-conf",
                       conditions={"label": "door", "min_confidence": 0.8},
                       actions={"auto_archive": True})
    good = _event(id=1, label="door", confidence=0.95)
    bad_label = _event(id=2, label="window", confidence=0.95)
    bad_conf = _event(id=3, label="door", confidence=0.1)
    assert rule_matches(rule, good) is True
    assert rule_matches(rule, bad_label) is False
    assert rule_matches(rule, bad_conf) is False
    assert [e["id"] for e in match_events(rule, [good, bad_label, bad_conf])] == [1]


def test_auto_rules_match_camera_and_tags_and_age():
    auto_rules_service_mod.clear()
    old = (datetime.now() - timedelta(days=10)).isoformat()
    new = datetime.now().isoformat()
    rule = create_rule(name="old cam1",
                       conditions={"camera_id": 1, "older_than_days": 5},
                       actions={"auto_delete": True})
    assert rule_matches(rule, _event(id=1, camera_id=1, created_at=old)) is True
    assert rule_matches(rule, _event(id=2, camera_id=1, created_at=new)) is False
    assert rule_matches(rule, _event(id=3, camera_id=2, created_at=old)) is False

    tag_rule = create_rule(name="tagged",
                           conditions={"tags": ["review"]},
                           actions={"auto_tag": ["done"]})
    assert rule_matches(tag_rule, _event(id=4, tags=["review", "night"])) is True
    assert rule_matches(tag_rule, _event(id=5, tags=["other"])) is False


def test_auto_rules_zmstyle_terms_and_or():
    auto_rules_service_mod.clear()
    rule = create_rule(
        name="zmfilter terms",
        terms=[
            {"attr": "label", "op": "=", "val": "person"},
            {"attr": "confidence", "op": ">=", "val": 0.7, "cnj": "and"},
        ],
        actions={"auto_tag": ["needs-review"]},
    )
    assert rule_matches(rule, _event(id=1, label="person", confidence=0.9)) is True
    assert rule_matches(rule, _event(id=2, label="person", confidence=0.2)) is False

    or_rule = create_rule(
        name="or rule",
        terms=[
            {"attr": "label", "op": "=", "val": "person"},
            {"attr": "label", "op": "=", "val": "dog", "cnj": "or"},
        ],
        actions={"auto_archive": True},
    )
    assert rule_matches(or_rule, _event(id=3, label="dog")) is True
    assert rule_matches(or_rule, _event(id=4, label="cat")) is False

    like_rule = create_rule(
        name="like rule",
        terms=[{"attr": "label", "op": "LIKE", "val": "%oo%"}],
        actions={"auto_tag": ["fuzzy"]},
    )
    assert rule_matches(like_rule, _event(id=5, label="door")) is True
    assert rule_matches(like_rule, _event(id=6, label="cat")) is False


def test_auto_rules_evaluate_and_run_dry_run():
    auto_rules_service_mod.clear()
    rule = create_rule(name="auto archive doors",
                       conditions={"label": "door"},
                       actions={"auto_archive": True, "auto_delete": False})
    events = [_event(id=1, label="door"), _event(id=2, label="window")]
    res = evaluate_rule(rule, events, dry_run=True)
    assert res["matched_ids"] == [1]
    assert res["dry_run"] is True
    # Dry-run must not mutate.
    assert events[0].get("tags") == []

    summary = run_rules(events, dry_run=True)
    assert summary["matched_ids"] == [1]
    assert summary["deleted_ids"] == []

    del_rule = create_rule(name="purge windows",
                           conditions={"label": "window"},
                           actions={"auto_delete": True})
    summary2 = run_rules([_event(id=10, label="window")], dry_run=True)
    assert 10 in summary2["deleted_ids"]
    assert summary2["deleted_count"] == 1
    assert del_rule["id"] in [r["rule_id"] for r in summary2["rules"]]


def test_auto_rules_run_applies_tags():
    auto_rules_service_mod.clear()
    create_rule(name="tagger", conditions={"label": "cat"}, actions={"auto_tag": ["reviewed"]})
    events = [_event(id=1, label="cat", tags=[])]
    summary = run_rules(events, dry_run=False)
    assert summary["dry_run"] is False
    assert "reviewed" in events[0]["tags"]


def test_auto_rules_api_crud():
    auto_rules_service_mod.clear()
    a = FastAPI()
    a.include_router(auto_rules_router)
    client = TestClient(a)

    r = client.post("/api/auto-rules",
                    json={"name": "archive doors", "conditions": {"label": "door"},
                          "actions": {"auto_archive": True}})
    assert r.status_code in (200, 201), r.text
    created = r.json()
    assert created["name"] == "archive doors"
    fid = created["id"]

    lst = client.get("/api/auto-rules")
    assert lst.status_code == 200
    assert any(f["id"] == fid for f in lst.json())

    single = client.get(f"/api/auto-rules/{fid}")
    assert single.status_code == 200
    assert single.json()["id"] == fid

    qry = client.get(f"/api/auto-rules/{fid}/query")
    assert qry.status_code == 200
    assert qry.json()["conditions"].get("label") == "door"

    upd = client.put(f"/api/auto-rules/{fid}", json={"description": "renamed"})
    assert upd.status_code == 200
    assert upd.json()["description"] == "renamed"

    dele = client.delete(f"/api/auto-rules/{fid}")
    assert dele.status_code == 200
    assert client.get(f"/api/auto-rules/{fid}").status_code == 404


def test_auto_rules_api_aliases_and_errors():
    auto_rules_service_mod.clear()
    a = FastAPI()
    a.include_router(auto_rules_router)
    client = TestClient(a)

    r = client.post("/api/zmfilter",
                    json={"name": "alias check", "conditions": {"camera_id": 3},
                          "actions": {"auto_tag": ["x"]}})
    assert r.status_code in (200, 201), r.text
    fid = r.json()["id"]

    lst = client.get("/api/auto_rules")
    assert lst.status_code == 200
    assert any(f["id"] == fid for f in lst.json())

    bad = client.post("/api/auto-rules", json={"conditions": {"label": "x"}})
    assert bad.status_code == 400

    missing = client.get("/api/auto-rules/999999")
    assert missing.status_code == 404


def test_auto_rules_api_evaluate():
    auto_rules_service_mod.clear()
    a = FastAPI()
    a.include_router(auto_rules_router)
    client = TestClient(a)

    client.post("/api/auto-rules",
                json={"name": "doors", "conditions": {"label": "door"},
                      "actions": {"auto_delete": True}})
    payload = {"events": [_event(id=1, label="door"), _event(id=2, label="cat")]}
    r = client.post("/api/auto-rules/evaluate", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    # Collection-level evaluate returns run summary.
    assert body["matched_ids"] == [1] or 1 in body.get("matched_ids", [])

    created = client.get("/api/auto-rules").json()[0]
    r2 = client.post(f"/api/auto-rules/{created['id']}/run", json=payload)
    assert r2.status_code == 200, r2.text
    assert r2.json()["matched_ids"] == [1]
