from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_logview_imports():
    import app.services.logview as svc  # noqa: F401
    import app.api.logview as api  # noqa: F401

    assert hasattr(api, "router")
    assert hasattr(svc, "tail_server_logs")
    assert hasattr(svc, "tail_log_file")
    assert hasattr(svc, "list_log_files")
    assert hasattr(svc, "detect_level")


def test_detect_and_normalize_levels():
    from app.services import logview as svc

    assert svc.normalize_level("error") == "ERROR"
    assert svc.normalize_level("warn") == "WARNING"
    assert svc.normalize_level("INFO") == "INFO"
    assert svc.normalize_level("bogus") is None
    assert svc.detect_level("2024-01-01 ERROR something failed") == "ERROR"
    assert svc.detect_level("plain line without level") is None
    assert svc.parse_levels("ERROR,WARNING") == {"ERROR", "WARNING"}
    assert svc.parse_levels(None) is None


def test_tail_and_level_filter(tmp_path):
    from app.services import logview as svc

    p = tmp_path / "server.log"
    p.write_text(
        "\n".join(
            [
                "2024-01-01 INFO server started",
                "2024-01-01 DEBUG debug detail",
                "2024-01-01 WARNING low disk",
                "2024-01-01 ERROR failed to open camera",
                "2024-01-01 CRITICAL system failure",
                "plain line no level",
            ]
        )
        + "\n"
    )

    all_res = svc.tail_log_file(str(p), lines=100)
    assert all_res["count"] == 6
    assert len(all_res["lines"]) == 6

    err = svc.tail_log_file(str(p), lines=100, level="ERROR")
    assert err["count"] == 1
    assert "ERROR" in err["lines"][0]

    multi = svc.tail_log_file(str(p), lines=100, level="ERROR,WARNING")
    assert multi["count"] == 2

    thresh = svc.tail_log_file(str(p), lines=100, min_level="ERROR")
    texts = " ".join(thresh["lines"])
    assert "ERROR" in texts
    assert "CRITICAL" in texts
    assert "INFO server started" not in texts

    searched = svc.tail_log_file(str(p), lines=100, search="camera")
    assert searched["count"] == 1

    limited = svc.tail_log_file(str(p), lines=2)
    assert limited["count"] == 2


def _mounted_client() -> TestClient:
    from app.api.logview import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_logview_router_tail_and_filter(tmp_path):
    from app.services import logview as svc

    p = tmp_path / "server.log"
    p.write_text("INFO ok\nERROR boom\nINFO fine\nERROR again\n")

    client = _mounted_client()
    # Tail explicit file via source param.
    r = client.get("/api/logview/tail", params={"source": str(p), "lines": 10})
    assert r.status_code == 200, r.text
    data = r.json()
    assert "lines" in data
    assert data["count"] == 4

    r2 = client.get("/api/logs/tail", params={"source": str(p), "level": "ERROR"})
    assert r2.status_code == 200, r2.text
    data2 = r2.json()
    assert data2["count"] == 2
    assert all("ERROR" in line for line in data2["lines"])

    r3 = client.get("/api/system/logs/tail", params={"source": str(p), "search": "boom"})
    assert r3.status_code == 200, r3.text
    assert r3.json()["count"] == 1

    # Alias root paths also serve tail.
    r4 = client.get("/api/logview", params={"source": str(p)})
    assert r4.status_code == 200, r4.text

    # Files + levels endpoints.
    rf = client.get("/api/logview/files")
    assert rf.status_code == 200, rf.text
    assert "files" in rf.json()

    rl = client.get("/api/logview/levels")
    assert rl.status_code == 200, rl.text
    assert "ERROR" in rl.json().get("levels", [])

    # Unknown source yields empty result, not 500.
    ru = client.get("/api/logview/tail", params={"source": "no-such-file-xyz.log"})
    assert ru.status_code == 200
